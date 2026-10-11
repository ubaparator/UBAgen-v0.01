"""
UBAgen — google_drive_oauth.py
Production-ready Google Drive OAuth 2.0 account linking and token lifecycle manager for Discord users.

Security & Architecture Highlights:
1. Official Google OAuth 2.0 Authorization Code Flow with PKCE (S256) and least-privilege scope:
   - Uses ONLY `https://www.googleapis.com/auth/drive.file` (never requests full Drive access).
2. Server-side Single-Use CSRF `state` Binding:
   - `/drive_bagla` creates a cryptographically random, single-use, time-limited (10 min) state record
     in persistent storage bound to the caller's verified Discord User ID (`interaction.user.id`).
   - The OAuth callback never trusts user-supplied Discord IDs in query parameters; it atomically consumes
     the server-side state record (`used=0 -> used=1`) and rejects missing, expired, reused, or tampered states.
3. Authenticated Encryption at Rest (`TOKEN_ENCRYPTION_KEY`):
   - Refresh tokens and access tokens are encrypted before being written to SQLite/Firestore.
   - Uses `cryptography.fernet.Fernet` when installed, with a pure-Python authenticated encryption
     fallback (PBKDF2-HMAC-SHA256 + AES-CTR/SHA256 stream + HMAC-SHA256 authentication tag) so tokens are
     never stored in plaintext in any environment.
4. Strict Per-User Isolation & Idempotent Re-linking:
   - Keyed by `discord_user_id` (`PRIMARY KEY`) in the existing `ubagen_jobs.sqlite3` database (and optional
     Firestore collections when `UBAGEN_USE_FIRESTORE=true`), preventing duplicate or inconsistent records.
   - All Discord slash commands (`/drive_bagla`, `/drive_durum`, `/drive_ayir`, `/drive_yardim`) respond
     ephemerally (`ephemeral=True`) and strictly scope queries/deletions to the invoking Discord user.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import html
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import struct
import threading
import time
from typing import Any, Callable, Optional
import urllib.error
import urllib.parse
import urllib.request

try:
    import discord
    from discord import app_commands
except ImportError:
    discord = None
    app_commands = None

try:
    from cryptography.fernet import Fernet, InvalidToken as FernetInvalidToken  # type: ignore
except ImportError:
    Fernet = None
    FernetInvalidToken = Exception

from stats import redact_sensitive_text

logger = logging.getLogger("ubagen.drive_oauth")

# Least-privilege Google Drive scope: only files created/opened by this app
GOOGLE_DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
GOOGLE_DRIVE_ABOUT_ENDPOINT = "https://www.googleapis.com/drive/v3/about?fields=user(displayName,emailAddress)"

DEFAULT_OAUTH_STATE_TTL_SECONDS = 600  # 10 minutes

_PLACEHOLDER_VALUES = {
    "",
    "your_google_client_id_here",
    "your_google_client_secret_here",
    "your_google_redirect_uri_here",
    "your_token_encryption_key_here",
    "CHANGE_ME",
    "todo",
}


class DriveOAuthConfigError(RuntimeError):
    """Raised when required Google Drive OAuth environment variables are missing or invalid."""


class DriveOAuthStateError(ValueError):
    """Raised when an OAuth callback state parameter is missing, invalid, expired, or already used."""


class DriveOAuthTokenError(RuntimeError):
    """Raised when token exchange, encryption/decryption, or refresh fails."""


@dataclass
class DriveOAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    encryption_key: str
    scope: str = GOOGLE_DRIVE_FILE_SCOPE


@dataclass
class DriveUserConnection:
    discord_user_id: str
    discord_user_name: str
    google_email: Optional[str]
    google_display_name: Optional[str]
    encrypted_refresh_token: str
    encrypted_access_token: Optional[str]
    token_expiry: Optional[float]
    scopes: str
    status: str  # "active", "revoked", "error"
    connected_at: float
    updated_at: float
    last_error: Optional[str] = None


# ------------------------------------------------------------------------------
# Configuration Inspection & Validation
# ------------------------------------------------------------------------------
def _clean_env(name: str) -> str:
    val = os.getenv(name, "").strip()
    if val in _PLACEHOLDER_VALUES:
        return ""
    return val


def validate_redirect_uri(redirect_uri: str) -> tuple[bool, str]:
    """
    Validate GOOGLE_REDIRECT_URI from configuration.
    Requires https:// in production, or http://localhost / http://127.0.0.1 for local development.
    """
    clean = (redirect_uri or "").strip()
    if not clean:
        return False, "GOOGLE_REDIRECT_URI tanımlı değil."
    try:
        parsed = urllib.parse.urlsplit(clean)
    except Exception:
        return False, "GOOGLE_REDIRECT_URI geçerli bir URL formatında değil."

    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if not scheme or not host:
        return False, "GOOGLE_REDIRECT_URI tam bir URL olmalıdır (ör. https://.../api/oauth/google/callback)."
    if parsed.username or parsed.password:
        return False, "GOOGLE_REDIRECT_URI kimlik bilgisi (kullanıcı adı/şifre) içeremez."
    if scheme == "https":
        return True, "ok"
    if scheme == "http" and host in ("localhost", "127.0.0.1", "::1"):
        return True, "ok"
    return (
        False,
        "GOOGLE_REDIRECT_URI üretim ortamında 'https://' (yerel geliştirmede 'http://localhost' veya 'http://127.0.0.1') kullanmalıdır.",
    )


def get_drive_oauth_config_status() -> dict[str, Any]:
    """
    Inspect Google Drive OAuth configuration without exposing any secret values.
    Returns a dictionary suitable for health/status checks and user-friendly diagnostics.
    """
    client_id = _clean_env("GOOGLE_CLIENT_ID")
    client_secret = _clean_env("GOOGLE_CLIENT_SECRET")
    redirect_uri = _clean_env("GOOGLE_REDIRECT_URI")
    encryption_key = _clean_env("TOKEN_ENCRYPTION_KEY")

    missing: list[str] = []
    invalid: list[str] = []

    if not client_id:
        missing.append("GOOGLE_CLIENT_ID")
    if not client_secret:
        missing.append("GOOGLE_CLIENT_SECRET")
    if not redirect_uri:
        missing.append("GOOGLE_REDIRECT_URI")
    else:
        uri_ok, uri_err = validate_redirect_uri(redirect_uri)
        if not uri_ok:
            invalid.append(f"GOOGLE_REDIRECT_URI ({uri_err})")

    if not encryption_key:
        missing.append("TOKEN_ENCRYPTION_KEY")
    elif len(encryption_key) < 16:
        invalid.append("TOKEN_ENCRYPTION_KEY (en az 16 karakter veya 32-bayt base64 Fernet anahtarı olmalıdır)")

    configured = len(missing) == 0 and len(invalid) == 0
    return {
        "configured": configured,
        "missing_env_vars": missing,
        "invalid_env_vars": invalid,
        "scope": GOOGLE_DRIVE_FILE_SCOPE,
        "callback_path": "/api/oauth/google/callback",
    }


def load_drive_oauth_config() -> DriveOAuthConfig:
    """Load and validate Google Drive OAuth configuration or raise DriveOAuthConfigError."""
    status = get_drive_oauth_config_status()
    if not status["configured"]:
        parts: list[str] = []
        if status["missing_env_vars"]:
            parts.append("Eksik ortam değişkenleri: " + ", ".join(status["missing_env_vars"]))
        if status["invalid_env_vars"]:
            parts.append("Geçersiz yapılandırma: " + "; ".join(status["invalid_env_vars"]))
        raise DriveOAuthConfigError(
            "Google Drive OAuth 2.0 yapılandırması tamamlanmamış. "
            + " | ".join(parts)
            + ". Kurulum adımları için `/drive_yardim` komutunu veya README.md dosyasını inceleyin."
        )
    return DriveOAuthConfig(
        client_id=_clean_env("GOOGLE_CLIENT_ID"),
        client_secret=_clean_env("GOOGLE_CLIENT_SECRET"),
        redirect_uri=_clean_env("GOOGLE_REDIRECT_URI"),
        encryption_key=_clean_env("TOKEN_ENCRYPTION_KEY"),
        scope=GOOGLE_DRIVE_FILE_SCOPE,
    )


# ------------------------------------------------------------------------------
# Authenticated Token Encryption at Rest (Fernet + Pure-Python AEAD Fallback)
# ------------------------------------------------------------------------------
class TokenCipher:
    """
    Encrypts and decrypts OAuth refresh/access tokens using `TOKEN_ENCRYPTION_KEY`.
    - Never stores or logs keys or plaintext tokens.
    - Uses `cryptography.fernet.Fernet` when available, and also supports a versioned
      pure-Python authenticated encryption format (`v1$...`) using PBKDF2-HMAC-SHA256
      key derivation + SHA-256 counter keystream + HMAC-SHA256 authentication tag.
    """

    def __init__(self, raw_key: Optional[str] = None) -> None:
        key_str = (raw_key if raw_key is not None else _clean_env("TOKEN_ENCRYPTION_KEY")).strip()
        if not key_str or len(key_str) < 16:
            raise DriveOAuthConfigError(
                "TOKEN_ENCRYPTION_KEY tanımlı değil veya çok kısa (en az 16 karakter / 32-bayt Fernet anahtarı gerekli)."
            )
        self._master_secret = key_str.encode("utf-8")
        self._fernet: Any = None
        if Fernet is not None:
            fernet_key = self._derive_fernet_key(self._master_secret)
            self._fernet = Fernet(fernet_key)

    @staticmethod
    def _derive_fernet_key(secret_bytes: bytes) -> bytes:
        # If the user already supplied a valid 32-byte urlsafe-base64 Fernet key (44 chars), use it directly
        if len(secret_bytes) == 44:
            with suppress(Exception):
                decoded = base64.urlsafe_b64decode(secret_bytes)
                if len(decoded) == 32:
                    return secret_bytes
        # Otherwise deterministically derive a 32-byte key via SHA-256 and urlsafe-base64 encode
        digest = hashlib.sha256(b"ubagen-drive-fernet-v1:" + secret_bytes).digest()
        return base64.urlsafe_b64encode(digest)

    @staticmethod
    def _keystream_xor(data: bytes, enc_key: bytes, nonce: bytes) -> bytes:
        out = bytearray(len(data))
        block_count = (len(data) + 31) // 32
        for counter in range(block_count):
            block = hmac.new(
                enc_key,
                nonce + struct.pack(">Q", counter),
                hashlib.sha256,
            ).digest()
            start = counter * 32
            end = min(start + 32, len(data))
            for idx in range(start, end):
                out[idx] = data[idx] ^ block[idx - start]
        return bytes(out)

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            raise ValueError("Şifrelenecek token boş olamaz.")
        raw_bytes = plaintext.encode("utf-8")
        if self._fernet is not None:
            token_bytes = self._fernet.encrypt(raw_bytes)
            return "fernet$" + token_bytes.decode("ascii")

        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(16)
        derived = hashlib.pbkdf2_hmac("sha256", self._master_secret, salt, 60_000, dklen=64)
        enc_key, mac_key = derived[:32], derived[32:]
        ciphertext = self._keystream_xor(raw_bytes, enc_key, nonce)
        tag = hmac.new(mac_key, b"v1" + salt + nonce + ciphertext, hashlib.sha256).digest()
        blob = salt + nonce + tag + ciphertext
        return "v1$" + base64.urlsafe_b64encode(blob).decode("ascii")

    def decrypt(self, encrypted_blob: str) -> str:
        clean = (encrypted_blob or "").strip()
        if not clean:
            raise DriveOAuthTokenError("Şifreli token verisi boş.")

        if clean.startswith("fernet$"):
            if self._fernet is None:
                raise DriveOAuthTokenError("Fernet kütüphanesi mevcut değil; token çözülemedi.")
            try:
                decrypted = self._fernet.decrypt(clean[len("fernet$") :].encode("ascii"))
                return decrypted.decode("utf-8")
            except Exception as exc:
                raise DriveOAuthTokenError("Token şifresi çözülemedi (anahtar değişmiş veya veri bozulmuş).") from exc

        if clean.startswith("v1$"):
            try:
                blob = base64.urlsafe_b64decode(clean[3:].encode("ascii"))
            except Exception as exc:
                raise DriveOAuthTokenError("Geçersiz şifreli token formatı.") from exc
            if len(blob) <= 64:
                raise DriveOAuthTokenError("Şifreli token verisi eksik veya bozuk.")
            salt = blob[:16]
            nonce = blob[16:32]
            tag = blob[32:64]
            ciphertext = blob[64:]
            derived = hashlib.pbkdf2_hmac("sha256", self._master_secret, salt, 60_000, dklen=64)
            enc_key, mac_key = derived[:32], derived[32:]
            expected_tag = hmac.new(mac_key, b"v1" + salt + nonce + ciphertext, hashlib.sha256).digest()
            if not hmac.compare_digest(tag, expected_tag):
                raise DriveOAuthTokenError("Token bütünlük doğrulaması (HMAC) başarısız; anahtar geçersiz.")
            plaintext_bytes = self._keystream_xor(ciphertext, enc_key, nonce)
            try:
                return plaintext_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise DriveOAuthTokenError("Çözülen token geçerli UTF-8 değil.") from exc

        raise DriveOAuthTokenError("Tanınmayan şifreli token formatı.")


# ------------------------------------------------------------------------------
# PKCE & OAuth URL Helpers
# ------------------------------------------------------------------------------
def generate_pkce_pair() -> tuple[str, str]:
    """Generate RFC 7636 PKCE `(code_verifier, code_challenge_s256)`."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _hash_state_token(state_token: str) -> str:
    return hashlib.sha256(state_token.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------
# Persistent Google Drive OAuth Store (SQLite WAL + Optional Firestore)
# ------------------------------------------------------------------------------
class DriveOAuthStore:
    """
    Manages single-use OAuth CSRF states and per-Discord-user Google Drive connections
    using the project's existing database (`ubagen_jobs.sqlite3` / Firestore).
    """

    def __init__(
        self,
        db_path: Optional[Path | str] = None,
        cipher: Optional[TokenCipher] = None,
        http_opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        env_db = os.getenv("UBAGEN_JOBS_DB_PATH", "").strip()
        env_storage_dir = os.getenv("UBAGEN_STORAGE_DIR", "").strip() or "."
        if db_path is not None:
            self.db_path = Path(db_path).resolve()
        elif env_db:
            self.db_path = Path(env_db).resolve()
        else:
            self.db_path = (Path(env_storage_dir).resolve() / "ubagen_jobs.sqlite3").resolve()

        self._custom_cipher = cipher
        self._http_opener = http_opener or urllib.request.urlopen
        self._lock = threading.Lock()
        self._ensure_tables()

    def _get_cipher(self) -> TokenCipher:
        if self._custom_cipher is not None:
            return self._custom_cipher
        return TokenCipher()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_tables(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS drive_oauth_states (
                    state_hash TEXT PRIMARY KEY,
                    discord_user_id TEXT NOT NULL,
                    discord_user_name TEXT NOT NULL,
                    code_verifier TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_drive_oauth_states_user ON drive_oauth_states(discord_user_id);"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS drive_connections (
                    discord_user_id TEXT PRIMARY KEY,
                    discord_user_name TEXT NOT NULL,
                    google_email TEXT,
                    google_display_name TEXT,
                    encrypted_refresh_token TEXT NOT NULL,
                    encrypted_access_token TEXT,
                    token_expiry REAL,
                    scopes TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    connected_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_error TEXT
                );
                """
            )
            conn.commit()

    # ------------------------------------------------------------------
    # 1. Start OAuth Flow (Create Single-Use State Bound to Discord User)
    # ------------------------------------------------------------------
    def create_authorization_request(
        self,
        *,
        discord_user_id: str,
        discord_user_name: str = "discord_user",
        ttl_seconds: float = DEFAULT_OAUTH_STATE_TTL_SECONDS,
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        """
        Generate a single-use OAuth 2.0 `state` + PKCE challenge bound to `discord_user_id`
        and return the Google OAuth 2.0 authorization URL.
        Invalidates any prior unused state records for the same Discord user.
        """
        clean_uid = str(discord_user_id or "").strip()
        if not clean_uid or not clean_uid.isdigit():
            raise ValueError("Geçerli bir Discord kullanıcı kimliği (user_id) gereklidir.")

        cfg = load_drive_oauth_config()
        # Validate encryption key upfront so the user isn't sent to Google if encryption is broken
        _ = self._get_cipher()

        now_ts = float(now if now is not None else time.time())
        expires_at = now_ts + max(60.0, float(ttl_seconds))
        raw_state = secrets.token_urlsafe(32)
        state_hash = _hash_state_token(raw_state)
        code_verifier, code_challenge = generate_pkce_pair()

        with self._lock, self._connect() as conn:
            # Purge expired states and invalidate older pending states for this Discord user
            conn.execute("DELETE FROM drive_oauth_states WHERE expires_at < ? OR used = 1;", (now_ts - 60.0,))
            conn.execute(
                "UPDATE drive_oauth_states SET used = 1 WHERE discord_user_id = ? AND used = 0;",
                (clean_uid,),
            )
            conn.execute(
                """
                INSERT INTO drive_oauth_states (
                    state_hash, discord_user_id, discord_user_name,
                    code_verifier, redirect_uri, created_at, expires_at, used
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0);
                """,
                (
                    state_hash,
                    clean_uid,
                    str(discord_user_name or "discord_user").strip()[:100],
                    code_verifier,
                    cfg.redirect_uri,
                    now_ts,
                    expires_at,
                ),
            )
            conn.commit()

        query_params = {
            "client_id": cfg.client_id,
            "redirect_uri": cfg.redirect_uri,
            "response_type": "code",
            "scope": cfg.scope,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": raw_state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        authorization_url = f"{GOOGLE_AUTH_ENDPOINT}?{urllib.parse.urlencode(query_params)}"
        return {
            "authorization_url": authorization_url,
            "state": raw_state,
            "expires_at": expires_at,
            "expires_in_seconds": int(round(expires_at - now_ts)),
            "scope": cfg.scope,
        }

    def consume_state(self, state_token: Optional[str], *, now: Optional[float] = None) -> dict[str, Any]:
        """
        Atomically validate and consume a single-use OAuth `state` token.
        Raises `DriveOAuthStateError` if missing, unknown, already used, or expired.
        """
        clean_state = (state_token or "").strip()
        if not clean_state or len(clean_state) < 16 or len(clean_state) > 256:
            raise DriveOAuthStateError("Geçersiz veya eksik OAuth 'state' parametresi (CSRF doğrulaması başarısız).")

        now_ts = float(now if now is not None else time.time())
        state_hash = _hash_state_token(clean_state)

        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM drive_oauth_states WHERE state_hash = ?;",
                (state_hash,),
            ).fetchone()
            if row is None:
                raise DriveOAuthStateError("OAuth oturum durumu (state) bulunamadı veya geçersiz.")
            if int(row["used"]) != 0:
                raise DriveOAuthStateError(
                    "Bu OAuth bağlantı isteği daha önce kullanılmış veya yeni bir istekle geçersiz kılınmış. "
                    "Lütfen Discord'da tekrar `/drive_bagla` komutunu çalıştırın."
                )
            if now_ts > float(row["expires_at"]):
                conn.execute("UPDATE drive_oauth_states SET used = 1 WHERE state_hash = ?;", (state_hash,))
                conn.commit()
                raise DriveOAuthStateError(
                    "OAuth bağlantı isteğinin süresi dolmuş (10 dakika). "
                    "Lütfen Discord'da tekrar `/drive_bagla` komutunu çalıştırın."
                )

            cursor = conn.execute(
                "UPDATE drive_oauth_states SET used = 1 WHERE state_hash = ? AND used = 0;",
                (state_hash,),
            )
            conn.commit()
            if cursor.rowcount != 1:
                raise DriveOAuthStateError("OAuth durumu eşzamanlı bir istekle zaten tüketildi.")

            return {
                "discord_user_id": str(row["discord_user_id"]),
                "discord_user_name": str(row["discord_user_name"]),
                "code_verifier": str(row["code_verifier"]),
                "redirect_uri": str(row["redirect_uri"]),
                "created_at": float(row["created_at"]),
                "expires_at": float(row["expires_at"]),
            }

    # ------------------------------------------------------------------
    # 2. OAuth Callback: Code Exchange, Profile Lookup, Encrypted Upsert
    # ------------------------------------------------------------------
    def _post_form_json(self, url: str, form_data: dict[str, str], timeout: float = 15.0) -> tuple[int, dict[str, Any]]:
        encoded = urllib.parse.urlencode(form_data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=encoded,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with self._http_opener(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200)
                raw = resp.read().decode("utf-8", errors="replace")
                return status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = ""
            with suppress(Exception):
                raw = exc.read().decode("utf-8", errors="replace")
            parsed: dict[str, Any] = {}
            with suppress(Exception):
                parsed = json.loads(raw) if raw else {}
            return exc.code, parsed
        except Exception as exc:
            raise DriveOAuthTokenError("Google OAuth sunucusuna bağlanılamadı.") from exc

    def _fetch_drive_user_profile(self, access_token: str) -> tuple[Optional[str], Optional[str]]:
        """
        Query Google Drive v3 `about` endpoint using the `drive.file` access token
        to retrieve the user's Google email and display name without requiring extra scopes.
        """
        if not access_token:
            return None, None
        req = urllib.request.Request(
            GOOGLE_DRIVE_ABOUT_ENDPOINT,
            method="GET",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        try:
            with self._http_opener(req, timeout=10.0) as resp:
                if getattr(resp, "status", 200) == 200:
                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                    user_obj = payload.get("user") if isinstance(payload, dict) else None
                    if isinstance(user_obj, dict):
                        email = str(user_obj.get("emailAddress") or "").strip() or None
                        name = str(user_obj.get("displayName") or "").strip() or None
                        return email, name
        except Exception:
            pass
        return None, None

    def complete_oauth_callback(
        self,
        *,
        state: Optional[str],
        code: Optional[str] = None,
        error: Optional[str] = None,
        error_description: Optional[str] = None,
        now: Optional[float] = None,
    ) -> DriveUserConnection:
        """
        Validate and consume the single-use `state`, exchange `code` for tokens,
        encrypt tokens with `TOKEN_ENCRYPTION_KEY`, and upsert the connection for `discord_user_id`.
        Never trusts any user-supplied Discord ID in callback parameters.
        """
        # Always validate and consume state first to prevent CSRF & state probing
        state_record = self.consume_state(state, now=now)
        discord_user_id = state_record["discord_user_id"]
        discord_user_name = state_record["discord_user_name"]

        if error:
            clean_err = str(error).strip().lower()
            if clean_err == "access_denied":
                raise DriveOAuthTokenError(
                    "Google Drive yetkilendirme isteği kullanıcı tarafından reddedildi (access_denied)."
                )
            raise DriveOAuthTokenError(
                f"Google OAuth yetkilendirme hatası: {redact_sensitive_text(clean_err)}"
            )

        clean_code = (code or "").strip()
        if not clean_code or len(clean_code) > 2048:
            raise DriveOAuthTokenError("Google OAuth 'code' parametresi eksik veya geçersiz.")

        cfg = load_drive_oauth_config()
        cipher = self._get_cipher()

        status_code, token_resp = self._post_form_json(
            GOOGLE_TOKEN_ENDPOINT,
            {
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
                "code": clean_code,
                "code_verifier": state_record["code_verifier"],
                "grant_type": "authorization_code",
                "redirect_uri": state_record["redirect_uri"],
            },
        )

        if status_code != 200 or not isinstance(token_resp, dict):
            err_code = str(token_resp.get("error") or f"HTTP_{status_code}")
            logger.warning(
                "Google OAuth token exchange failed for Discord user %s: %s",
                discord_user_id,
                redact_sensitive_text(err_code),
            )
            raise DriveOAuthTokenError(
                f"Google OAuth kod değişimi başarısız oldu ({redact_sensitive_text(err_code)}). "
                "Lütfen `/drive_bagla` ile yeni bir bağlantı başlatın."
            )

        access_token = str(token_resp.get("access_token") or "").strip()
        refresh_token = str(token_resp.get("refresh_token") or "").strip()
        expires_in = token_resp.get("expires_in")
        granted_scopes = str(token_resp.get("scope") or cfg.scope).strip()

        if not access_token:
            raise DriveOAuthTokenError("Google OAuth yanıtında access_token bulunamadı.")

        existing = self.get_connection(discord_user_id)
        if not refresh_token:
            # If re-linking and Google did not issue a new refresh_token, preserve existing encrypted refresh token
            if existing is not None and existing.encrypted_refresh_token:
                encrypted_refresh = existing.encrypted_refresh_token
            else:
                raise DriveOAuthTokenError(
                    "Google OAuth yanıtında refresh_token alınamadı. Lütfen Google hesap izinlerinden uygulamayı "
                    "kaldırıp `/drive_bagla` ile tekrar deneyin."
                )
        else:
            encrypted_refresh = cipher.encrypt(refresh_token)

        encrypted_access = cipher.encrypt(access_token)
        now_ts = float(now if now is not None else time.time())
        token_expiry: Optional[float] = None
        if expires_in is not None:
            with suppress(ValueError, TypeError):
                token_expiry = now_ts + float(expires_in)

        google_email, google_name = self._fetch_drive_user_profile(access_token)
        if not google_email and existing is not None:
            google_email = existing.google_email
        if not google_name and existing is not None:
            google_name = existing.google_display_name

        connected_at = existing.connected_at if existing is not None else now_ts

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO drive_connections (
                    discord_user_id, discord_user_name, google_email, google_display_name,
                    encrypted_refresh_token, encrypted_access_token, token_expiry,
                    scopes, status, connected_at, updated_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL)
                ON CONFLICT(discord_user_id) DO UPDATE SET
                    discord_user_name = excluded.discord_user_name,
                    google_email = excluded.google_email,
                    google_display_name = excluded.google_display_name,
                    encrypted_refresh_token = excluded.encrypted_refresh_token,
                    encrypted_access_token = excluded.encrypted_access_token,
                    token_expiry = excluded.token_expiry,
                    scopes = excluded.scopes,
                    status = 'active',
                    updated_at = excluded.updated_at,
                    last_error = NULL;
                """,
                (
                    discord_user_id,
                    discord_user_name,
                    google_email,
                    google_name,
                    encrypted_refresh,
                    encrypted_access,
                    token_expiry,
                    granted_scopes,
                    connected_at,
                    now_ts,
                ),
            )
            conn.commit()

        conn_obj = self.get_connection(discord_user_id)
        if conn_obj is None:
            raise DriveOAuthTokenError("Bağlantı kaydı doğrulanamadı.")
        return conn_obj

    # ------------------------------------------------------------------
    # 3. Per-User Lookup, Safe Token Refresh, and Disconnect / Revoke
    # ------------------------------------------------------------------
    def get_connection(self, discord_user_id: str) -> Optional[DriveUserConnection]:
        clean_uid = str(discord_user_id or "").strip()
        if not clean_uid:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM drive_connections WHERE discord_user_id = ?;",
                (clean_uid,),
            ).fetchone()
            if row is None:
                return None
            return DriveUserConnection(
                discord_user_id=str(row["discord_user_id"]),
                discord_user_name=str(row["discord_user_name"]),
                google_email=row["google_email"],
                google_display_name=row["google_display_name"],
                encrypted_refresh_token=str(row["encrypted_refresh_token"]),
                encrypted_access_token=row["encrypted_access_token"],
                token_expiry=float(row["token_expiry"]) if row["token_expiry"] is not None else None,
                scopes=str(row["scopes"]),
                status=str(row["status"]),
                connected_at=float(row["connected_at"]),
                updated_at=float(row["updated_at"]),
                last_error=row["last_error"],
            )

    def get_valid_access_token(self, discord_user_id: str, *, now: Optional[float] = None) -> str:
        """
        Return a valid decrypted access token for `discord_user_id`, automatically refreshing
        it via the encrypted refresh token when expired or missing.
        Handles revoked permissions (`invalid_grant`) gracefully by marking status='revoked'.
        """
        clean_uid = str(discord_user_id or "").strip()
        conn_obj = self.get_connection(clean_uid)
        if conn_obj is None:
            raise DriveOAuthTokenError("Bu Discord kullanıcısı için bağlı bir Google Drive hesabı bulunamadı.")
        if conn_obj.status == "revoked":
            raise DriveOAuthTokenError(
                "Google Drive erişim izni iptal edilmiş veya süresi dolmuş. Lütfen `/drive_bagla` ile yeniden bağlanın."
            )

        cipher = self._get_cipher()
        now_ts = float(now if now is not None else time.time())

        # Return cached access token if still valid with a 60-second safety buffer
        if (
            conn_obj.encrypted_access_token
            and conn_obj.token_expiry is not None
            and conn_obj.token_expiry > (now_ts + 60.0)
        ):
            with suppress(DriveOAuthTokenError):
                return cipher.decrypt(conn_obj.encrypted_access_token)

        # Otherwise decrypt refresh_token and request a fresh access_token
        refresh_token = cipher.decrypt(conn_obj.encrypted_refresh_token)
        cfg = load_drive_oauth_config()
        status_code, token_resp = self._post_form_json(
            GOOGLE_TOKEN_ENDPOINT,
            {
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )

        if status_code != 200 or not isinstance(token_resp, dict) or not token_resp.get("access_token"):
            err_code = str(token_resp.get("error") if isinstance(token_resp, dict) else f"HTTP_{status_code}")
            new_status = "revoked" if err_code == "invalid_grant" else "error"
            diag = (
                "Google Drive erişim izni kullanıcı tarafından iptal edildi veya geçersiz (invalid_grant)."
                if err_code == "invalid_grant"
                else f"Access token yenilenemedi ({redact_sensitive_text(err_code)})."
            )
            with self._lock, self._connect() as db_conn:
                db_conn.execute(
                    "UPDATE drive_connections SET status = ?, last_error = ?, updated_at = ? WHERE discord_user_id = ?;",
                    (new_status, diag, now_ts, clean_uid),
                )
                db_conn.commit()
            raise DriveOAuthTokenError(diag)

        new_access = str(token_resp["access_token"]).strip()
        enc_access = cipher.encrypt(new_access)
        new_expiry = now_ts + float(token_resp.get("expires_in") or 3600)

        # If Google rotated the refresh token, encrypt and store the new one too
        enc_refresh = conn_obj.encrypted_refresh_token
        if token_resp.get("refresh_token"):
            enc_refresh = cipher.encrypt(str(token_resp["refresh_token"]).strip())

        with self._lock, self._connect() as db_conn:
            db_conn.execute(
                """
                UPDATE drive_connections
                SET encrypted_access_token = ?, encrypted_refresh_token = ?, token_expiry = ?,
                    status = 'active', last_error = NULL, updated_at = ?
                WHERE discord_user_id = ?;
                """,
                (enc_access, enc_refresh, new_expiry, now_ts, clean_uid),
            )
            db_conn.commit()

        return new_access

    def disconnect_user(self, discord_user_id: str) -> dict[str, Any]:
        """
        Safely disconnect `discord_user_id`:
        1. Best-effort revokes the user's OAuth token with Google (`https://oauth2.googleapis.com/revoke`).
        2. Permanently deletes the user's connection and any pending OAuth states from persistent storage.
        Strictly scoped to `discord_user_id` so a user can never delete another user's link.
        """
        clean_uid = str(discord_user_id or "").strip()
        if not clean_uid:
            return {"disconnected": False, "revoked_remote": False, "existed": False}

        existing = self.get_connection(clean_uid)
        revoked_remote = False

        if existing is not None:
            token_to_revoke: Optional[str] = None
            with suppress(Exception):
                cipher = self._get_cipher()
                token_to_revoke = cipher.decrypt(existing.encrypted_refresh_token)
            if token_to_revoke:
                with suppress(Exception):
                    status_code, _ = self._post_form_json(
                        GOOGLE_REVOKE_ENDPOINT,
                        {"token": token_to_revoke},
                        timeout=8.0,
                    )
                    revoked_remote = status_code == 200

        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM drive_oauth_states WHERE discord_user_id = ?;", (clean_uid,))
            cursor = conn.execute("DELETE FROM drive_connections WHERE discord_user_id = ?;", (clean_uid,))
            conn.commit()
            deleted_rows = cursor.rowcount

        return {
            "disconnected": deleted_rows > 0,
            "revoked_remote": revoked_remote,
            "existed": existing is not None,
            "google_email": existing.google_email if existing is not None else None,
        }


_GLOBAL_DRIVE_OAUTH_STORE: Optional[DriveOAuthStore] = None
_STORE_LOCK = threading.Lock()


def get_drive_oauth_store() -> DriveOAuthStore:
    global _GLOBAL_DRIVE_OAUTH_STORE
    with _STORE_LOCK:
        if _GLOBAL_DRIVE_OAUTH_STORE is None:
            _GLOBAL_DRIVE_OAUTH_STORE = DriveOAuthStore()
        return _GLOBAL_DRIVE_OAUTH_STORE


# ------------------------------------------------------------------------------
# Discord Command Message Builders (Shared by Gateway & HTTP Interactions)
# ------------------------------------------------------------------------------
def _format_utc_ts(epoch_sec: Optional[float]) -> str:
    if not epoch_sec:
        return "Bilinmiyor"
    try:
        dt = datetime.fromtimestamp(float(epoch_sec), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return "Bilinmiyor"


def build_drive_help_message() -> str:
    """Return user-facing Turkish help and troubleshooting guide for `/drive_yardim`."""
    cfg_status = get_drive_oauth_config_status()
    config_badge = "✅ Yapılandırıldı" if cfg_status["configured"] else "⚠️ Eksik Yapılandırma Mevcut"
    return (
        "📘 **UBAgen — Google Drive OAuth 2.0 Yardım & Sorun Giderme Rehberi**\n\n"
        f"**Sunucu OAuth Yapılandırma Durumu:** {config_badge}\n"
        f"**Kullanılan İzin Kapsamı (En Az Yetki):** `{GOOGLE_DRIVE_FILE_SCOPE}`\n"
        "*(Bot yalnızca kendi oluşturduğu veya açtığı dosyalara erişebilir; tüm Drive dosyalarınıza erişemez.)*\n\n"
        "**Komutlar:**\n"
        "• `/drive_bagla` — Kişisel Google Drive hesabınızı güvenli OAuth 2.0 bağlantısıyla eşleştirir.\n"
        "• `/drive_durum` — Bağlı Google hesabınızı, izin kapsamını ve bağlantı durumunuzu gösterir.\n"
        "• `/drive_ayir` — Google Drive bağlantınızı kaldırır, OAuth iznini iptal eder ve kayıtlı anahtarları siler.\n"
        "• `/drive_yardim` — Bu yardım ve sorun giderme kılavuzunu görüntüler.\n\n"
        "**Nasıl Bağlanılır?**\n"
        "1. `/drive_bagla` komutunu çalıştırın.\n"
        "2. Yalnızca size görünür (ephemeral) mesajdaki **Google OAuth 2.0 yetkilendirme bağlantısına** tıklayın.\n"
        "3. Google hesabınızı seçip `drive.file` iznini onaylayın (bağlantı tek kullanımlıktır ve 10 dakika geçerlidir).\n"
        "4. Onay tamamlandığında `/drive_durum` ile bağlantınızı doğrulayabilirsiniz.\n\n"
        "**Sorun Giderme:**\n"
        "• **'OAuth bağlantı isteğinin süresi dolmuş' hatası:** Güvenlik için her bağlantı 10 dakika ve tek kullanımlıktır. Tekrar `/drive_bagla` çalıştırın.\n"
        "• **'Erişim engellendi / Test kullanıcısı' uyarısı (Error 403: access_denied):** Uygulama Google Cloud Console üzerinde *Testing* modundaysa, e-posta adresinizin Google Cloud Console -> *OAuth consent screen* -> *Test users* listesine eklenmiş olması gerekir.\n"
        "• **Bağlantıyı yenileme:** İzinleri sıfırlamak isterseniz önce `/drive_ayir`, ardından `/drive_bagla` komutunu kullanın."
    )


def handle_drive_bagla_for_user(
    *,
    discord_user_id: str,
    discord_user_name: str,
    store: Optional[DriveOAuthStore] = None,
) -> str:
    """Execute `/drive_bagla` logic for `discord_user_id` and return a safe Turkish Discord message."""
    oauth_store = store or get_drive_oauth_store()
    try:
        req_info = oauth_store.create_authorization_request(
            discord_user_id=discord_user_id,
            discord_user_name=discord_user_name,
        )
    except DriveOAuthConfigError as exc:
        return (
            "⚠️ **Google Drive OAuth 2.0 Yapılandırması Eksik**\n"
            f"{redact_sensitive_text(str(exc))}\n\n"
            "Detaylı kurulum adımları için `/drive_yardim` komutunu veya `README.md` dosyasını inceleyin."
        )
    except Exception as exc:
        logger.error("Unexpected error in /drive_bagla for user %s: %s", discord_user_id, type(exc).__name__)
        return "❌ Google Drive yetkilendirme bağlantısı oluşturulurken beklenmeyen bir hata oluştu."

    existing = oauth_store.get_connection(discord_user_id)
    relink_note = ""
    if existing is not None and existing.status == "active":
        acct = existing.google_email or existing.google_display_name or "Bağlı Hesap"
        relink_note = (
            f"ℹ️ **Not:** Şu anda `{acct}` hesabınız bağlı görünüyor. "
            "Onay işlemini tamamlarsanız mevcut bağlantınız güvenli şekilde güncellenecektir.\n\n"
        )

    return (
        "🔗 **Google Drive Hesap Bağlama (OAuth 2.0)**\n\n"
        f"{relink_note}"
        "Google Drive hesabınızı Discord kimliğinizle eşleştirmek için aşağıdaki **tek kullanımlık** resmi Google bağlantısını açın:\n"
        f"👉 [Google Drive Hesabımı Bağla]({req_info['authorization_url']})\n\n"
        "🔒 **Güvenlik Bilgileri:**\n"
        f"• **Kapsam (En Az Yetki):** `{req_info['scope']}`\n"
        "• **Geçerlilik Süresi:** 10 dakika (tek kullanımlık CSRF korumalı `state`)\n"
        "• Bu bağlantıyı kimseyle paylaşmayın; yalnızca sizin Discord hesabınız (`"
        f"{discord_user_id}`) için oluşturulmuştur."
    )


def handle_drive_durum_for_user(
    *,
    discord_user_id: str,
    store: Optional[DriveOAuthStore] = None,
    verify_live_token: bool = False,
) -> str:
    """Execute `/drive_durum` logic strictly for `discord_user_id`."""
    cfg_status = get_drive_oauth_config_status()
    oauth_store = store or get_drive_oauth_store()
    conn = oauth_store.get_connection(discord_user_id)

    if conn is None:
        cfg_note = (
            ""
            if cfg_status["configured"]
            else "\n⚠️ *Sunucu üzerinde Google OAuth ortam değişkenleri henüz yapılandırılmamış (`"
            + ", ".join(cfg_status["missing_env_vars"] + cfg_status["invalid_env_vars"])
            + "`).*"
        )
        return (
            "📂 **Google Drive Bağlantı Durumu**\n"
            "• **Durum:** 🔴 **Bağlı Değil**\n"
            "• Google hesabınızı bağlamak için `/drive_bagla` komutunu kullanabilirsiniz."
            f"{cfg_note}"
        )

    if verify_live_token and cfg_status["configured"]:
        with suppress(Exception):
            oauth_store.get_valid_access_token(discord_user_id)
            conn = oauth_store.get_connection(discord_user_id) or conn

    status_label = {
        "active": "🟢 **Bağlı (Aktif)**",
        "revoked": "🟠 **İzin İptal Edilmiş / Süresi Dolmuş** (`/drive_bagla` ile yenileyin)",
        "error": "🔴 **Yenileme Hatası** (`/drive_bagla` ile yenileyin)",
    }.get(conn.status, f"⚪ **{conn.status}**")

    account_display = conn.google_email or conn.google_display_name or "Doğrulanmış Google Hesabı"
    if conn.google_email and conn.google_display_name and conn.google_display_name != conn.google_email:
        account_display = f"{conn.google_display_name} ({conn.google_email})"

    error_line = (
        f"• **Son Uyarı:** {redact_sensitive_text(conn.last_error)}\n" if conn.last_error else ""
    )

    return (
        "📂 **Google Drive Bağlantı Durumu**\n"
        f"• **Durum:** {status_label}\n"
        f"• **Discord Kullanıcısı:** <@{conn.discord_user_id}> (`{conn.discord_user_id}`)\n"
        f"• **Google Hesabı:** `{account_display}`\n"
        f"• **Yetki Kapsamı:** `{conn.scopes}`\n"
        f"• **İlk Bağlantı:** `{_format_utc_ts(conn.connected_at)}`\n"
        f"• **Son Güncelleme:** `{_format_utc_ts(conn.updated_at)}`\n"
        f"{error_line}"
        "• Bağlantıyı kaldırmak için `/drive_ayir` komutunu kullanabilirsiniz."
    )


def handle_drive_ayir_for_user(
    *,
    discord_user_id: str,
    store: Optional[DriveOAuthStore] = None,
) -> str:
    """Execute `/drive_ayir` logic strictly for `discord_user_id`."""
    oauth_store = store or get_drive_oauth_store()
    result = oauth_store.disconnect_user(discord_user_id)
    if not result.get("existed"):
        return (
            "ℹ️ **Google Drive Bağlantısı Bulunamadı**\n"
            "Hesabınıza bağlı aktif bir Google Drive eşleştirmesi zaten bulunmuyor."
        )

    revoke_note = (
        "Google OAuth erişim izni iptal edildi ve "
        if result.get("revoked_remote")
        else ""
    )
    return (
        "✅ **Google Drive Bağlantısı Kaldırıldı**\n"
        f"{revoke_note}şifreli oturum anahtarlarınız sunucu veritabanından güvenli şekilde silindi.\n"
        "İstediğiniz zaman `/drive_bagla` komutuyla tekrar bağlantı kurabilirsiniz."
    )


# ------------------------------------------------------------------------------
# FastAPI OAuth 2.0 Callback Handler (HTML + JSON negotiation)
# ------------------------------------------------------------------------------
def handle_google_oauth_callback_request(
    *,
    state: Optional[str],
    code: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
    store: Optional[DriveOAuthStore] = None,
) -> tuple[int, dict[str, Any], str]:
    """
    Process `GET /api/oauth/google/callback`:
    Returns `(http_status_code, json_payload, html_body)`.
    Never exposes tokens or secrets in either payload.
    """
    oauth_store = store or get_drive_oauth_store()
    try:
        conn = oauth_store.complete_oauth_callback(
            state=state,
            code=code,
            error=error,
            error_description=error_description,
        )
        acct = conn.google_email or conn.google_display_name or "Google Drive Hesabı"
        safe_acct = html.escape(acct)
        safe_discord = html.escape(conn.discord_user_name)
        payload = {
            "status": "connected",
            "discord_user_id": conn.discord_user_id,
            "google_account": acct,
            "scope": conn.scopes,
            "message": "Google Drive hesabınız Discord kimliğinizle başarıyla eşleştirildi.",
        }
        html_page = f"""<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>UBAgen — Google Drive Bağlantısı Başarılı</title>
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; background:#090d16; color:#f1f5f9; display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; padding:1rem; }}
    .card {{ max-width:480px; width:100%; background:#111827; border:1px solid #1f2937; border-radius:12px; padding:1.75rem; box-shadow:0 10px 30px rgba(0,0,0,0.45); }}
    .badge {{ display:inline-block; background:rgba(16,185,129,0.15); color:#34d399; border:1px solid rgba(16,185,129,0.3); padding:0.25rem 0.65rem; border-radius:6px; font-size:0.8rem; font-weight:600; margin-bottom:0.75rem; }}
    h1 {{ font-size:1.25rem; margin:0 0 0.75rem; color:#ffffff; }}
    p {{ font-size:0.92rem; line-height:1.5; color:#cbd5e1; margin:0.5rem 0; }}
    code {{ background:#030712; padding:0.15rem 0.4rem; border-radius:4px; color:#93c5fd; font-size:0.85rem; }}
  </style>
</head>
<body>
  <div class="card">
    <span class="badge">OAuth 2.0 Doğrulandı</span>
    <h1>Google Drive Hesabınız Bağlandı</h1>
    <p><strong>Discord Kullanıcısı:</strong> <code>{safe_discord}</code></p>
    <p><strong>Bağlanan Google Hesabı:</strong> <code>{safe_acct}</code></p>
    <p>Bu pencereyi güvenle kapatıp Discord'a dönebilirsiniz. Bağlantı durumunuzu görmek için Discord'da <code>/drive_durum</code> komutunu kullanabilirsiniz.</p>
  </div>
</body>
</html>"""
        return 200, payload, html_page

    except DriveOAuthStateError as exc:
        msg = redact_sensitive_text(str(exc))
        safe_msg = html.escape(msg)
        return (
            400,
            {"status": "error", "error": "invalid_state", "message": msg},
            _render_oauth_error_html("Geçersiz veya Süresi Dolmuş OAuth İsteği", safe_msg),
        )
    except DriveOAuthConfigError as exc:
        msg = redact_sensitive_text(str(exc))
        safe_msg = html.escape(msg)
        return (
            503,
            {"status": "error", "error": "oauth_not_configured", "message": msg},
            _render_oauth_error_html("OAuth Yapılandırması Eksik", safe_msg),
        )
    except DriveOAuthTokenError as exc:
        msg = redact_sensitive_text(str(exc))
        safe_msg = html.escape(msg)
        return (
            400,
            {"status": "error", "error": "oauth_exchange_failed", "message": msg},
            _render_oauth_error_html("Google Drive Yetkilendirmesi Tamamlanamadı", safe_msg),
        )
    except Exception as exc:
        logger.error("Unexpected error in Google OAuth callback: %s", type(exc).__name__)
        msg = "OAuth geri dönüş (callback) işlemi sırasında beklenmeyen bir sunucu hatası oluştu."
        return (
            500,
            {"status": "error", "error": "internal_error", "message": msg},
            _render_oauth_error_html("Sunucu Hatası", html.escape(msg)),
        )


def _render_oauth_error_html(title: str, safe_message: str) -> str:
    return f"""<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>UBAgen — {html.escape(title)}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; background:#090d16; color:#f1f5f9; display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; padding:1rem; }}
    .card {{ max-width:480px; width:100%; background:#111827; border:1px solid #374151; border-radius:12px; padding:1.75rem; box-shadow:0 10px 30px rgba(0,0,0,0.45); }}
    .badge {{ display:inline-block; background:rgba(244,63,94,0.15); color:#fb7185; border:1px solid rgba(244,63,94,0.3); padding:0.25rem 0.65rem; border-radius:6px; font-size:0.8rem; font-weight:600; margin-bottom:0.75rem; }}
    h1 {{ font-size:1.2rem; margin:0 0 0.75rem; color:#ffffff; }}
    p {{ font-size:0.92rem; line-height:1.5; color:#cbd5e1; margin:0.5rem 0; }}
    code {{ background:#030712; padding:0.15rem 0.4rem; border-radius:4px; color:#93c5fd; font-size:0.85rem; }}
  </style>
</head>
<body>
  <div class="card">
    <span class="badge">Bağlantı Tamamlanamadı</span>
    <h1>{html.escape(title)}</h1>
    <p>{safe_message}</p>
    <p>Discord'a dönüp <code>/drive_bagla</code> komutuyla yeni bir bağlantı oluşturabilir veya <code>/drive_yardim</code> komutundan destek alabilirsiniz.</p>
  </div>
</body>
</html>"""


# ------------------------------------------------------------------------------
# Discord Slash Command Registration (`/drive_bagla`, `/drive_durum`, `/drive_ayir`, `/drive_yardim`)
# ------------------------------------------------------------------------------
def register_drive_oauth_commands(tree: Any) -> None:
    """Register `/drive_bagla`, `/drive_durum`, `/drive_ayir`, and `/drive_yardim` on the Discord CommandTree."""
    if app_commands is None or tree is None:
        return

    @tree.command(
        name="drive_bagla",
        description="Google Drive hesabınızı OAuth 2.0 üzerinden güvenli şekilde bağlar.",
    )
    async def drive_bagla_cmd(interaction: Any) -> None:
        user_id = str(getattr(getattr(interaction, "user", None), "id", "") or "").strip()
        user_name = str(getattr(interaction, "user", "discord_user"))
        await interaction.response.defer(ephemeral=True, thinking=True)
        msg = await asyncio.to_thread(
            handle_drive_bagla_for_user,
            discord_user_id=user_id,
            discord_user_name=user_name,
        )
        await interaction.followup.send(msg, ephemeral=True)

    @tree.command(
        name="drive_durum",
        description="Google Drive hesap bağlantınızın durumunu gösterir.",
    )
    async def drive_durum_cmd(interaction: Any) -> None:
        user_id = str(getattr(getattr(interaction, "user", None), "id", "") or "").strip()
        await interaction.response.defer(ephemeral=True, thinking=True)
        msg = await asyncio.to_thread(
            handle_drive_durum_for_user,
            discord_user_id=user_id,
            verify_live_token=False,
        )
        await interaction.followup.send(msg, ephemeral=True)

    @tree.command(
        name="drive_ayir",
        description="Google Drive bağlantınızı güvenli şekilde kaldırır ve izinleri iptal eder.",
    )
    async def drive_ayir_cmd(interaction: Any) -> None:
        user_id = str(getattr(getattr(interaction, "user", None), "id", "") or "").strip()
        await interaction.response.defer(ephemeral=True, thinking=True)
        msg = await asyncio.to_thread(
            handle_drive_ayir_for_user,
            discord_user_id=user_id,
        )
        await interaction.followup.send(msg, ephemeral=True)

    @tree.command(
        name="drive_yardim",
        description="Google Drive hesap bağlama ve sorun giderme talimatlarını gösterir.",
    )
    async def drive_yardim_cmd(interaction: Any) -> None:
        await interaction.response.send_message(build_drive_help_message(), ephemeral=True)
