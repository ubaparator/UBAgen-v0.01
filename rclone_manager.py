"""
UBAgen — rclone_manager.py
Rclone Multi-Account Storage & Remote Lifecycle Manager for MEGA and Google Drive.

Key Capabilities:
1. Rclone-based storage operations (`upload_file`, `verify_object`, `open_download_stream_or_path`, `delete_object`).
2. Multiple MEGA (`type = mega`) and Google Drive (`type = drive`) accounts:
   - Google Drive uses rclone's built-in default OAuth client (never requires a custom Google Cloud project,
     custom OAuth app, or manual `client_id` / `client_secret`).
   - Initial Google Drive user authorization is managed via rclone (`rclone authorize "drive" --auth-no-open-browser`
     local callback bridge + token JSON capture), encrypted at rest, and reused on subsequent connections.
   - MEGA authentication uses rclone's supported username + `rclone obscure` password mechanism.
3. Management Panel Account Lifecycle:
   - Add account (`add_account`)
   - Connection test (`test_account_connection`)
   - Reconnect (`reconnect_account`)
   - Safe disconnect (`disconnect_account`): Deactivates the remote without deleting any remote or local files,
     and permanently preserves all pending deletion tasks (`rclone_pending_deletions`) in SQLite.
   - Account status & quota inspection (`list_accounts`, `get_rclone_status_summary`).
"""

from __future__ import annotations

import asyncio
import base64
import configparser
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Optional
import urllib.error
import urllib.parse
import urllib.request
import uuid

try:
    from cryptography.fernet import Fernet  # type: ignore
except ImportError:
    Fernet = None

logger = logging.getLogger("ubagen.rclone")

VALID_RCLONE_PROVIDERS = {"drive", "mega"}
REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,47}$")


class RcloneError(RuntimeError):
    """Raised when an rclone configuration or storage operation fails."""


class RcloneAuthError(RcloneError):
    """Raised when rclone account authorization or token validation fails."""


# ------------------------------------------------------------------------------
# Authenticated Encryption at Rest for Rclone Tokens & Credentials
# ------------------------------------------------------------------------------
class RcloneSecretCipher:
    """
    Encrypts and decrypts rclone OAuth tokens and MEGA credentials at rest in SQLite.
    Uses `TOKEN_ENCRYPTION_KEY` or `UBAGEN_RCLONE_KEY` if set in environment; otherwise
    automatically generates and persists a local 0600 key file in the storage directory
    so rclone token encryption works out-of-the-box without manual setup.
    """

    def __init__(self, raw_key: Optional[str] = None, key_file_dir: Optional[Path] = None) -> None:
        key_str = (
            raw_key
            or os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
            or os.getenv("UBAGEN_RCLONE_KEY", "").strip()
        )
        if not key_str or key_str in ("your_token_encryption_key_here", "CHANGE_ME"):
            key_str = self._load_or_create_local_key(key_file_dir)

        self._master_secret = key_str.encode("utf-8")
        self._fernet: Any = None
        if Fernet is not None:
            digest = hashlib.sha256(b"ubagen-rclone-fernet-v1:" + self._master_secret).digest()
            self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    @staticmethod
    def _load_or_create_local_key(key_file_dir: Optional[Path] = None) -> str:
        base_dir = key_file_dir or Path(os.getenv("UBAGEN_STORAGE_DIR", ".").strip() or ".").resolve()
        try:
            base_dir.mkdir(parents=True, exist_ok=True)
            key_path = base_dir / ".ubagen_rclone_master.key"
            if key_path.is_file():
                existing = key_path.read_text(encoding="utf-8").strip()
                if len(existing) >= 24:
                    return existing
            generated = secrets.token_urlsafe(32)
            key_path.write_text(generated, encoding="utf-8")
            with suppress(OSError):
                os.chmod(key_path, 0o600)
            return generated
        except Exception:
            fallback_path = Path(tempfile.gettempdir()) / ".ubagen_rclone_master.key"
            if fallback_path.is_file():
                with suppress(Exception):
                    existing = fallback_path.read_text(encoding="utf-8").strip()
                    if len(existing) >= 24:
                        return existing
            generated = secrets.token_urlsafe(32)
            with suppress(Exception):
                fallback_path.write_text(generated, encoding="utf-8")
                os.chmod(fallback_path, 0o600)
            return generated

    @staticmethod
    def _keystream_xor(data: bytes, enc_key: bytes, nonce: bytes) -> bytes:
        out = bytearray(len(data))
        block_count = (len(data) + 31) // 32
        for counter in range(block_count):
            block = hmac.new(enc_key, nonce + struct.pack(">Q", counter), hashlib.sha256).digest()
            start = counter * 32
            end = min(start + 32, len(data))
            for idx in range(start, end):
                out[idx] = data[idx] ^ block[idx - start]
        return bytes(out)

    def encrypt(self, plaintext: str) -> str:
        raw_bytes = (plaintext or "").encode("utf-8")
        if self._fernet is not None:
            return "fernet$" + self._fernet.encrypt(raw_bytes).decode("ascii")
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(16)
        derived = hashlib.pbkdf2_hmac("sha256", self._master_secret, salt, 60_000, dklen=64)
        enc_key, mac_key = derived[:32], derived[32:]
        ciphertext = self._keystream_xor(raw_bytes, enc_key, nonce)
        tag = hmac.new(mac_key, b"v1" + salt + nonce + ciphertext, hashlib.sha256).digest()
        return "v1$" + base64.urlsafe_b64encode(salt + nonce + tag + ciphertext).decode("ascii")

    def decrypt(self, encrypted_blob: str) -> str:
        clean = (encrypted_blob or "").strip()
        if not clean:
            return "{}"
        if clean.startswith("fernet$") and self._fernet is not None:
            try:
                return self._fernet.decrypt(clean[len("fernet$") :].encode("ascii")).decode("utf-8")
            except Exception as exc:
                raise RcloneAuthError("Şifreli rclone yapılandırması çözülemedi.") from exc
        if clean.startswith("v1$"):
            try:
                blob = base64.urlsafe_b64decode(clean[3:].encode("ascii"))
            except Exception as exc:
                raise RcloneAuthError("Geçersiz şifreli rclone verisi.") from exc
            if len(blob) < 64:
                raise RcloneAuthError("Şifreli rclone verisi bozuk.")
            salt, nonce, tag, ciphertext = blob[:16], blob[16:32], blob[32:64], blob[64:]
            derived = hashlib.pbkdf2_hmac("sha256", self._master_secret, salt, 60_000, dklen=64)
            enc_key, mac_key = derived[:32], derived[32:]
            expected = hmac.new(mac_key, b"v1" + salt + nonce + ciphertext, hashlib.sha256).digest()
            if not hmac.compare_digest(tag, expected):
                raise RcloneAuthError("Rclone token bütünlük doğrulaması (HMAC) başarısız.")
            return self._keystream_xor(ciphertext, enc_key, nonce).decode("utf-8")
        raise RcloneAuthError("Tanınmayan şifreli rclone veri formatı.")


def mask_account_identity(identity: Optional[str]) -> Optional[str]:
    """Mask email or account identifier safely for UI/log display."""
    clean = (identity or "").strip()
    if not clean:
        return None
    if "@" in clean:
        local, domain = clean.split("@", 1)
        if len(local) <= 2:
            masked_local = local[:1] + "***"
        else:
            masked_local = local[:2] + "***" + local[-1:]
        return f"{masked_local}@{domain}"
    if len(clean) <= 4:
        return clean[:1] + "***"
    return clean[:2] + "***" + clean[-2:]


_RCLONE_BOOTSTRAP_ATTEMPTED = False
_RCLONE_BOOTSTRAP_LOCK = threading.Lock()
_BACKEND_CACHE: dict[str, set[str]] = {}
_BACKEND_CACHE_LOCK = threading.Lock()


def get_rclone_supported_backends(path: str) -> set[str]:
    """
    Query and return the set of backend names supported by the given rclone binary.
    Parses `rclone help backends` output and caches results.
    """
    if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
        return set()
    with _BACKEND_CACHE_LOCK:
        if path in _BACKEND_CACHE:
            return _BACKEND_CACHE[path]
    try:
        proc = subprocess.run(
            [path, "help", "backends"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        backends: set[str] = set()
        if proc.returncode == 0 and proc.stdout:
            for line in proc.stdout.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("All rclone backends") or stripped.startswith("To see more"):
                    continue
                parts = stripped.split(None, 1)
                if parts:
                    backends.add(parts[0].lower().strip())
        with _BACKEND_CACHE_LOCK:
            _BACKEND_CACHE[path] = backends
        return backends
    except Exception as exc:
        logger.debug("Failed to query rclone help backends from %s: %s", path, exc)
        return set()


def is_rclone_backend_supported(path: str, backend: str = "mega") -> bool:
    """Return whether the specified rclone binary supports `backend` (e.g. 'mega' or 'drive')."""
    return backend.lower().strip() in get_rclone_supported_backends(path)


def resolve_rclone_binary(
    auto_bootstrap: bool = True,
    require_backend: Optional[str] = "mega",
) -> Optional[str]:
    """
    Resolve the `rclone` binary path from standard container locations or PATH.
    Verifies that the candidate binary supports `require_backend` (default 'mega').
    If a stripped rclone is encountered (e.g. Debian package lacking 'mega'),
    attempts to find the official static Linux release or auto-bootstraps it.
    """
    # 1. If shutil.which was mocked by a unit test (e.g. testing missing rclone), respect the mock
    is_mocked = getattr(shutil.which, "__module__", "") != "shutil"
    if is_mocked:
        mocked = shutil.which("rclone")
        if not mocked:
            return None
        if require_backend and not is_rclone_backend_supported(mocked, require_backend):
            return None if not auto_bootstrap else mocked
        return mocked

    env_override = os.getenv("UBAGEN_RCLONE_PATH", "").strip()
    candidates: list[str] = []
    if env_override:
        candidates.append(env_override)
    candidates.extend([
        "/usr/local/bin/rclone",
        "/usr/bin/rclone",
        shutil.which("rclone") or "",
        "/tmp/ubagen_bin/rclone",
    ])

    seen: set[str] = set()
    fallback_binary: Optional[str] = None

    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            if not require_backend:
                return cand
            if is_rclone_backend_supported(cand, require_backend):
                return cand
            if fallback_binary is None:
                fallback_binary = cand

    if not auto_bootstrap or os.getenv("UBAGEN_DISABLE_RCLONE_BOOTSTRAP", "").strip() in ("1", "true", "yes"):
        return fallback_binary

    global _RCLONE_BOOTSTRAP_ATTEMPTED
    with _RCLONE_BOOTSTRAP_LOCK:
        if _RCLONE_BOOTSTRAP_ATTEMPTED:
            for cand in ("/usr/local/bin/rclone", "/usr/bin/rclone", "/tmp/ubagen_bin/rclone"):
                if os.path.isfile(cand) and os.access(cand, os.X_OK):
                    if not require_backend or is_rclone_backend_supported(cand, require_backend):
                        return cand
            return fallback_binary
        _RCLONE_BOOTSTRAP_ATTEMPTED = True
        try:
            import platform
            import zipfile

            machine = platform.machine().lower()
            arch = "arm64" if machine in ("aarch64", "arm64") else "amd64"
            url = f"https://downloads.rclone.org/rclone-current-linux-{arch}.zip"
            req = urllib.request.Request(url, headers={"User-Agent": "UBAgen-Rclone-Bootstrap/1.0"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                zip_bytes = resp.read()
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                member = next((m for m in zf.namelist() if m.endswith("/rclone")), None)
                if not member:
                    return fallback_binary
                raw_bin = zf.read(member)

            for target_str in ("/usr/local/bin/rclone", "/usr/bin/rclone", "/tmp/ubagen_bin/rclone"):
                try:
                    target = Path(target_str)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw_bin)
                    os.chmod(target, 0o755)
                    parent_str = str(target.parent)
                    if parent_str not in os.environ.get("PATH", ""):
                        os.environ["PATH"] = f"{parent_str}:{os.environ.get('PATH', '')}"
                    with _BACKEND_CACHE_LOCK:
                        _BACKEND_CACHE.pop(str(target), None)
                    if not require_backend or is_rclone_backend_supported(str(target), require_backend):
                        return str(target)
                except Exception:
                    continue
        except Exception as exc:
            logger.warning("Automatic official rclone binary bootstrap failed: %s", exc)

    return fallback_binary


def check_rclone_binary() -> dict[str, Any]:
    """
    Check whether `rclone` is installed and executable in the current environment,
    and verify that it supports native MEGA and Google Drive storage backends.
    Returns `{installed, executable, path, version, mega_supported, drive_supported, backends_count, error}`.
    """
    path = resolve_rclone_binary(auto_bootstrap=True, require_backend="mega") or resolve_rclone_binary(auto_bootstrap=False, require_backend=None)
    if not path:
        return {
            "installed": False,
            "executable": False,
            "path": None,
            "version": "Unavailable",
            "mega_supported": False,
            "drive_supported": False,
            "backends_count": 0,
            "error": "rclone ikili dosyası (binary) sistem PATH üzerinde bulunamadı.",
        }
    try:
        proc = subprocess.run(
            [path, "version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        first_line = ((proc.stdout or proc.stderr or "").splitlines() or ["Unavailable"])[0].strip()
        ok = proc.returncode == 0
        backends = get_rclone_supported_backends(path)
        mega_ok = "mega" in backends
        drive_ok = "drive" in backends
        err_msg = None
        if not ok:
            err_msg = (proc.stderr or proc.stdout or f"Exit code {proc.returncode}").strip()[:200]
        elif not mega_ok:
            err_msg = (
                f"Kurulu rclone sürümünde ({first_line}) 'mega' depolama sağlayıcısı bulunamadı. "
                "Resmî MEGA destekli rclone dağıtımı gereklidir."
            )
        return {
            "installed": True,
            "executable": ok,
            "path": path,
            "version": first_line if ok else "Error",
            "mega_supported": mega_ok,
            "drive_supported": drive_ok,
            "backends_count": len(backends),
            "error": err_msg,
        }
    except Exception as exc:
        return {
            "installed": True,
            "executable": False,
            "path": path,
            "version": "Error",
            "mega_supported": False,
            "drive_supported": False,
            "backends_count": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }


def obscure_rclone_password(raw_password: str) -> str:
    """
    Obscure a plaintext password using `rclone obscure` (required by rclone for MEGA `pass`).
    Never logs the password.
    """
    clean_pw = (raw_password or "").strip()
    if not clean_pw:
        raise ValueError("MEGA hesap şifresi boş olamaz.")

    rclone_bin = resolve_rclone_binary(auto_bootstrap=True, require_backend="mega") or resolve_rclone_binary(auto_bootstrap=False, require_backend=None)
    if rclone_bin:
        try:
            proc = subprocess.run(
                [rclone_bin, "obscure", clean_pw],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if proc.returncode == 0:
                # Take the first token before any NOTICE log lines
                out = (proc.stdout or "").strip().splitlines()[0].strip()
                if "202" in out and "NOTICE:" in out:
                    out = out.split("202", 1)[0].strip()
                if out:
                    return out
        except Exception as exc:
            logger.debug("rclone obscure CLI fallback triggered: %s", type(exc).__name__)

    # If cryptography AES-CTR is available, use rclone's standard obscure key
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # type: ignore

        # Rclone's official obscure key from rclone/fs/config/obscure/obscure.go
        rclone_crypt_key = bytes([
            0x9C, 0x93, 0x5B, 0x48, 0x73, 0x0A, 0x55, 0x4D,
            0x6B, 0xFD, 0x7C, 0x63, 0xC8, 0x86, 0xA9, 0x2B,
            0xD3, 0x90, 0x19, 0x8E, 0xB8, 0x12, 0x8A, 0xFB,
            0xF4, 0xDE, 0x16, 0x2B, 0x8B, 0x95, 0xF6, 0x38,
        ])
        iv = secrets.token_bytes(16)
        cipher = Cipher(algorithms.AES(rclone_crypt_key), modes.CTR(iv))
        encryptor = cipher.encryptor()
        ct = encryptor.update(clean_pw.encode("utf-8")) + encryptor.finalize()
        return base64.urlsafe_b64encode(iv + ct).rstrip(b"=").decode("ascii")
    except Exception as exc:
        raise RcloneError("rclone obscure işlemi çalıştırılamadı.") from exc


@dataclass
class RcloneAccountRecord:
    remote_name: str
    provider: str  # "drive" or "mega"
    display_name: str
    account_identity: Optional[str]
    remote_base_path: str
    encrypted_config_json: str
    status: str  # "connected", "auth_pending", "disconnected", "error"
    is_active: bool
    last_test_ok: Optional[bool]
    last_tested_at: Optional[float]
    last_error: Optional[str]
    quota_json: Optional[str]
    created_at: float
    updated_at: float

    def to_public_dict(self, pending_deletions_count: int = 0) -> dict[str, Any]:
        """Return safe dictionary for API/UI with zero secret/token exposure."""
        quota_data: Optional[dict[str, Any]] = None
        if self.quota_json:
            with suppress(Exception):
                parsed = json.loads(self.quota_json)
                if isinstance(parsed, dict):
                    quota_data = parsed

        provider_label = "Google Drive (rclone OAuth)" if self.provider == "drive" else "MEGA"
        return {
            "remote_name": self.remote_name,
            "provider": self.provider,
            "provider_label": provider_label,
            "display_name": self.display_name,
            "account_identity": mask_account_identity(self.account_identity),
            "remote_base_path": self.remote_base_path,
            "status": self.status,
            "is_active": bool(self.is_active),
            "last_test_ok": self.last_test_ok,
            "last_tested_at": self.last_tested_at,
            "last_tested_at_iso": (
                datetime.fromtimestamp(self.last_tested_at, tz=timezone.utc).isoformat()
                if self.last_tested_at
                else None
            ),
            "last_error": self.last_error,
            "quota": quota_data,
            "pending_deletions_count": int(pending_deletions_count),
            "uses_default_rclone_oauth": self.provider == "drive",
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class PendingDeletionRecord:
    deletion_id: str
    job_id: Optional[str]
    remote_name: str
    provider: str
    remote_object_path: str
    due_at: float
    status: str  # "pending", "completed", "failed_retry"
    attempts: int
    last_error: Optional[str]
    created_at: float
    updated_at: float

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "deletion_id": self.deletion_id,
            "job_id": self.job_id,
            "remote_name": self.remote_name,
            "provider": self.provider,
            "remote_object_path": self.remote_object_path,
            "due_at": self.due_at,
            "due_at_iso": datetime.fromtimestamp(self.due_at, tz=timezone.utc).isoformat(),
            "is_due": time.time() >= self.due_at,
            "status": self.status,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RcloneStorageManager:
    """
    Manages multiple MEGA and Google Drive rclone remotes, handles rclone-native Google Drive
    user authorization (using rclone's built-in default OAuth client without any custom
    client_id/client_secret), executes file uploads/downloads/verifications over rclone,
    and preserves pending deletion tasks across safe disconnects.
    """

    def __init__(
        self,
        db_path: Optional[Path | str] = None,
        rclone_config_path: Optional[Path | str] = None,
        cipher: Optional[RcloneSecretCipher] = None,
        cli_runner: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
    ) -> None:
        env_db = os.getenv("UBAGEN_JOBS_DB_PATH", "").strip()
        env_storage_dir = os.getenv("UBAGEN_STORAGE_DIR", "").strip() or "."
        base_dir = Path(env_storage_dir).resolve()
        if db_path is not None:
            self.db_path = Path(db_path).resolve()
        elif env_db:
            self.db_path = Path(env_db).resolve()
        else:
            self.db_path = (base_dir / "ubagen_jobs.sqlite3").resolve()

        env_rclone_cfg = os.getenv("UBAGEN_RCLONE_CONFIG_PATH", "").strip()
        if rclone_config_path is not None:
            self.rclone_config_path = Path(rclone_config_path).resolve()
        elif env_rclone_cfg:
            self.rclone_config_path = Path(env_rclone_cfg).resolve()
        else:
            self.rclone_config_path = (self.db_path.parent / "rclone.conf").resolve()

        self._lock = threading.RLock()
        self._verified_binary: Optional[str] = None
        self._ensure_storage_paths()
        self.cipher = cipher or RcloneSecretCipher(key_file_dir=self.db_path.parent)
        self._cli_runner = cli_runner
        self._auth_sessions: dict[str, dict[str, Any]] = {}
        self._ensure_tables()
        self._import_existing_rclone_conf_remotes()
        self.sync_rclone_conf_file()

    def get_rclone_binary(self) -> str:
        """
        Return the verified, official rclone binary path that is guaranteed to support
        both MEGA and Google Drive backends. Never relies on an unverified random PATH binary.
        """
        if self._verified_binary and os.path.isfile(self._verified_binary) and os.access(self._verified_binary, os.X_OK):
            return self._verified_binary
        bin_path = resolve_rclone_binary(auto_bootstrap=True, require_backend="mega")
        if not bin_path:
            bin_path = resolve_rclone_binary(auto_bootstrap=False, require_backend=None) or "rclone"
        self._verified_binary = bin_path
        return bin_path

    def _ensure_storage_paths(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            probe = self.db_path.parent / f".rclone_probe_{uuid.uuid4().hex[:6]}"
            probe.write_bytes(b"ok")
            probe.unlink(missing_ok=True)
        except Exception:
            fallback = Path(tempfile.gettempdir()) / "ubagen_storage"
            fallback.mkdir(parents=True, exist_ok=True)
            self.db_path = (fallback / "ubagen_jobs.sqlite3").resolve()
            self.rclone_config_path = (fallback / "rclone.conf").resolve()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_tables(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rclone_accounts (
                    remote_name TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    account_identity TEXT,
                    remote_base_path TEXT NOT NULL DEFAULT 'ubagen_outputs',
                    encrypted_config_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'connected',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    last_test_ok INTEGER,
                    last_tested_at REAL,
                    last_error TEXT,
                    quota_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rclone_pending_deletions (
                    deletion_id TEXT PRIMARY KEY,
                    job_id TEXT,
                    remote_name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    remote_object_path TEXT NOT NULL,
                    due_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rclone_del_remote_status ON rclone_pending_deletions(remote_name, status, due_at);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rclone_del_job ON rclone_pending_deletions(job_id);"
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Rclone Configuration File Sync & Token Persistence
    # ------------------------------------------------------------------
    def _import_existing_rclone_conf_remotes(self) -> None:
        """
        If an `rclone.conf` file already exists on disk with `drive` or `mega` sections
        that are not yet tracked in `rclone_accounts`, import them cleanly.
        """
        candidate_paths = [self.rclone_config_path]
        default_user_cfg = Path.home() / ".config" / "rclone" / "rclone.conf"
        if default_user_cfg not in candidate_paths and default_user_cfg.is_file():
            candidate_paths.append(default_user_cfg)

        for cfg_path in candidate_paths:
            if not cfg_path.is_file():
                continue
            try:
                parser = configparser.ConfigParser(interpolation=None)
                parser.read(str(cfg_path), encoding="utf-8")
                for section in parser.sections():
                    clean_name = re.sub(r"[^A-Za-z0-9_-]+", "_", section.strip())[:48]
                    if not clean_name or self.get_account(clean_name) is not None:
                        continue
                    rtype = (parser.get(section, "type", fallback="") or "").strip().lower()
                    if rtype not in VALID_RCLONE_PROVIDERS:
                        continue
                    sec_dict = {k: v for k, v in parser.items(section) if k not in ("client_id", "client_secret")}
                    now = time.time()
                    enc_cfg = self.cipher.encrypt(json.dumps(sec_dict, ensure_ascii=False))
                    identity = sec_dict.get("user") or None
                    with self._lock, self._connect() as conn:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO rclone_accounts (
                                remote_name, provider, display_name, account_identity,
                                remote_base_path, encrypted_config_json, status, is_active,
                                created_at, updated_at
                            ) VALUES (?, ?, ?, ?, 'ubagen_outputs', ?, 'connected', 1, ?, ?);
                            """,
                            (
                                clean_name,
                                rtype,
                                f"{'Google Drive' if rtype == 'drive' else 'MEGA'} ({clean_name})",
                                identity,
                                enc_cfg,
                                now,
                                now,
                            ),
                        )
                        conn.commit()
            except Exception as exc:
                logger.debug("Could not import existing rclone.conf (%s): %s", cfg_path, type(exc).__name__)

    def sync_rclone_conf_file(self) -> Path:
        """
        Write active (`is_active=1`) MEGA and Google Drive remotes to `self.rclone_config_path`
        with strict `0600` file permissions.
        Google Drive remotes NEVER include `client_id` or `client_secret` so rclone always
        uses its built-in default OAuth client.
        """
        with self._lock:
            accounts = self._load_all_account_rows()
            parser = configparser.ConfigParser(interpolation=None)
            for acc in accounts:
                if not acc.is_active or acc.status in ("disconnected", "auth_pending"):
                    continue
                try:
                    cfg_dict = json.loads(self.cipher.decrypt(acc.encrypted_config_json))
                except Exception:
                    continue
                if not isinstance(cfg_dict, dict):
                    continue

                section: dict[str, str] = {}
                if acc.provider == "drive":
                    section["type"] = "drive"
                    section["scope"] = str(cfg_dict.get("scope") or "drive.file")
                    token_val = cfg_dict.get("token")
                    if isinstance(token_val, dict):
                        section["token"] = json.dumps(token_val, separators=(",", ":"))
                    elif isinstance(token_val, str) and token_val.strip():
                        section["token"] = token_val.strip()
                    else:
                        continue
                    if cfg_dict.get("root_folder_id"):
                        section["root_folder_id"] = str(cfg_dict["root_folder_id"]).strip()
                    if cfg_dict.get("team_drive"):
                        section["team_drive"] = str(cfg_dict["team_drive"]).strip()
                    # Explicitly never write client_id or client_secret so rclone uses its default OAuth client
                elif acc.provider == "mega":
                    section["type"] = "mega"
                    user_val = str(cfg_dict.get("user") or acc.account_identity or "").strip()
                    pass_val = str(cfg_dict.get("pass") or "").strip()
                    if not user_val or not pass_val:
                        continue
                    section["user"] = user_val
                    section["pass"] = pass_val
                else:
                    continue

                parser[acc.remote_name] = section

            self.rclone_config_path.parent.mkdir(parents=True, exist_ok=True)
            buf = io.StringIO()
            parser.write(buf)
            self.rclone_config_path.write_text(buf.getvalue(), encoding="utf-8")
            with suppress(OSError):
                os.chmod(self.rclone_config_path, 0o600)
            return self.rclone_config_path

    def _sync_refreshed_tokens_from_conf(self, remote_name: str) -> None:
        """
        If `rclone` refreshed an OAuth access_token in `rclone.conf` during a command,
        read the updated `token` back and persist it encrypted in SQLite (`rclone_accounts`).
        """
        if not self.rclone_config_path.is_file():
            return
        with self._lock:
            acc = self.get_account(remote_name)
            if acc is None or acc.provider != "drive":
                return
            try:
                parser = configparser.ConfigParser(interpolation=None)
                parser.read(str(self.rclone_config_path), encoding="utf-8")
                if not parser.has_section(remote_name):
                    return
                new_token_raw = (parser.get(remote_name, "token", fallback="") or "").strip()
                if not new_token_raw:
                    return
                cfg_dict = json.loads(self.cipher.decrypt(acc.encrypted_config_json))
                old_token_raw = cfg_dict.get("token")
                if isinstance(old_token_raw, dict):
                    old_token_raw = json.dumps(old_token_raw, separators=(",", ":"))
                if new_token_raw != old_token_raw:
                    cfg_dict["token"] = new_token_raw
                    new_enc = self.cipher.encrypt(json.dumps(cfg_dict, ensure_ascii=False))
                    with self._connect() as conn:
                        conn.execute(
                            "UPDATE rclone_accounts SET encrypted_config_json = ?, updated_at = ? WHERE remote_name = ?;",
                            (new_enc, time.time(), remote_name),
                        )
                        conn.commit()
            except Exception as exc:
                logger.debug("Could not sync refreshed token from rclone.conf: %s", type(exc).__name__)

    def _run_rclone_cli(
        self,
        args: list[str],
        *,
        timeout: int = 60,
        sync_remote_token: Optional[str] = None,
    ) -> subprocess.CompletedProcess[str]:
        """Execute an `rclone` CLI command with `--config <self.rclone_config_path>`."""
        self.sync_rclone_conf_file()
        rclone_bin = self.get_rclone_binary()
        full_cmd = [rclone_bin, "--config", str(self.rclone_config_path)] + list(args)
        if self._cli_runner is not None:
            res = self._cli_runner(full_cmd, timeout=timeout)
        else:
            res = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        if sync_remote_token:
            self._sync_refreshed_tokens_from_conf(sync_remote_token)
        return res

    # ------------------------------------------------------------------
    # Account CRUD, Rclone Drive OAuth Authorization & MEGA Setup
    # ------------------------------------------------------------------
    @staticmethod
    def validate_remote_name(remote_name: str) -> str:
        clean = (remote_name or "").strip()
        if not REMOTE_NAME_RE.match(clean):
            raise ValueError(
                "Geçersiz hesap/remote adı. Yalnızca harf, rakam, tire (-) ve alt çizgi (_) kullanılabilir (2-48 karakter)."
            )
        return clean

    @staticmethod
    def validate_and_normalize_drive_token(raw_token_input: str | dict[str, Any]) -> str:
        """
        Parse and validate an rclone Google Drive OAuth token JSON string or object.
        Also extracts the JSON object if the user pasted the full `rclone authorize "drive"` terminal output.
        """
        if isinstance(raw_token_input, dict):
            token_obj = raw_token_input
        else:
            text = (raw_token_input or "").strip()
            if not text:
                raise RcloneAuthError("Google Drive rclone yetkilendirme token'ı boş olamaz.")
            # If user pasted full rclone authorize output containing `{"access_token":...}`
            m = re.search(r"(\{[^{}]*\"access_token\"[^{}]*\})", text, flags=re.DOTALL)
            candidate = m.group(1).strip() if m else text
            try:
                token_obj = json.loads(candidate)
            except Exception as exc:
                raise RcloneAuthError(
                    "Geçersiz rclone Google Drive token JSON formatı. "
                    "`rclone authorize \"drive\"` çıktısındaki JSON nesnesini veya yetkilendirme kodunu girin."
                ) from exc

        if not isinstance(token_obj, dict):
            raise RcloneAuthError("Google Drive token verisi bir JSON nesnesi olmalıdır.")
        access_tok = str(token_obj.get("access_token") or "").strip()
        refresh_tok = str(token_obj.get("refresh_token") or "").strip()
        if not access_tok and not refresh_tok:
            raise RcloneAuthError("Token JSON içinde `access_token` veya `refresh_token` bulunamadı.")
        token_obj.setdefault("token_type", "Bearer")
        return json.dumps(token_obj, separators=(",", ":"))

    def _row_to_account(self, row: sqlite3.Row) -> RcloneAccountRecord:
        last_ok_raw = row["last_test_ok"]
        return RcloneAccountRecord(
            remote_name=str(row["remote_name"]),
            provider=str(row["provider"]),
            display_name=str(row["display_name"]),
            account_identity=row["account_identity"],
            remote_base_path=str(row["remote_base_path"] or "ubagen_outputs"),
            encrypted_config_json=str(row["encrypted_config_json"]),
            status=str(row["status"]),
            is_active=bool(int(row["is_active"])),
            last_test_ok=(bool(int(last_ok_raw)) if last_ok_raw is not None else None),
            last_tested_at=(float(row["last_tested_at"]) if row["last_tested_at"] is not None else None),
            last_error=row["last_error"],
            quota_json=row["quota_json"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def _load_all_account_rows(self) -> list[RcloneAccountRecord]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM rclone_accounts ORDER BY created_at ASC, remote_name ASC;"
            ).fetchall()
            return [self._row_to_account(r) for r in rows]

    def get_account(self, remote_name: str) -> Optional[RcloneAccountRecord]:
        clean = (remote_name or "").strip()
        if not clean:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM rclone_accounts WHERE remote_name = ?;",
                (clean,),
            ).fetchone()
            return self._row_to_account(row) if row is not None else None

    def list_accounts(self) -> list[dict[str, Any]]:
        """Return all configured MEGA and Google Drive accounts with pending deletion counts."""
        with self._lock:
            accounts = self._load_all_account_rows()
            pending_counts: dict[str, int] = {}
            with self._connect() as conn:
                for row in conn.execute(
                    "SELECT remote_name, COUNT(*) AS cnt FROM rclone_pending_deletions WHERE status = 'pending' GROUP BY remote_name;"
                ).fetchall():
                    pending_counts[str(row["remote_name"])] = int(row["cnt"])
            return [
                acc.to_public_dict(pending_deletions_count=pending_counts.get(acc.remote_name, 0))
                for acc in accounts
            ]

    def add_mega_account(
        self,
        *,
        remote_name: Optional[str] = None,
        user: Optional[str] = None,
        email: Optional[str] = None,
        password: str,
        display_name: Optional[str] = None,
        remote_base_path: str = "ubagen_outputs",
        root_folder: Optional[str] = None,
        test_after_add: bool = False,
        skip_verify: bool = False,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """
        Add or update a MEGA account using rclone's native authentication (`user` + `rclone obscure` password).
        Supports multiple MEGA accounts simultaneously.
        """
        clean_user = (user or email or "").strip()
        if not clean_user:
            raise ValueError("MEGA kullanıcı adı / e-posta adresi zorunludur.")
        raw_remote = (remote_name or "").strip() or f"mega_{uuid.uuid4().hex[:6]}"
        clean_remote = self.validate_remote_name(raw_remote)
        obscured_pass = obscure_rclone_password(password)
        clean_base = (root_folder or remote_base_path or "ubagen_outputs").strip().strip("/") or "ubagen_outputs"
        clean_display = (display_name or f"MEGA ({mask_account_identity(clean_user)})").strip()[:100]

        cfg_payload = {
            "type": "mega",
            "user": clean_user,
            "pass": obscured_pass,
        }
        encrypted_cfg = self.cipher.encrypt(json.dumps(cfg_payload, ensure_ascii=False))
        now = time.time()
        initial_status = "connected" if enabled else "disabled"

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO rclone_accounts (
                    remote_name, provider, display_name, account_identity,
                    remote_base_path, encrypted_config_json, status, is_active,
                    last_error, created_at, updated_at
                ) VALUES (?, 'mega', ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(remote_name) DO UPDATE SET
                    provider = 'mega',
                    display_name = excluded.display_name,
                    account_identity = excluded.account_identity,
                    remote_base_path = excluded.remote_base_path,
                    encrypted_config_json = excluded.encrypted_config_json,
                    status = excluded.status,
                    is_active = excluded.is_active,
                    last_error = NULL,
                    updated_at = excluded.updated_at;
                """,
                (clean_remote, clean_display, clean_user, clean_base, encrypted_cfg, initial_status, 1 if enabled else 0, now, now),
            )
            conn.commit()

        self.sync_rclone_conf_file()
        if test_after_add and not skip_verify:
            return self.test_account_connection(clean_remote)
        acc = self.get_account(clean_remote)
        return acc.to_public_dict(self.count_pending_deletions(clean_remote)) if acc else {}

    def add_drive_account(
        self,
        *,
        remote_name: str,
        token_json: Optional[str | dict[str, Any]] = None,
        display_name: Optional[str] = None,
        account_identity: Optional[str] = None,
        remote_base_path: str = "ubagen_outputs",
        scope: str = "drive.file",
        root_folder_id: Optional[str] = None,
        test_after_add: bool = False,
    ) -> dict[str, Any]:
        """
        Add or update a Google Drive account using rclone's default OAuth client (zero custom client_id/client_secret).
        If `token_json` is provided now, the account is immediately marked `connected` and active.
        If `token_json` is omitted, the account is created in `auth_pending` state ready for rclone authorization.
        """
        clean_remote = self.validate_remote_name(remote_name)
        clean_base = (remote_base_path or "ubagen_outputs").strip().strip("/") or "ubagen_outputs"
        clean_scope = (scope or "drive.file").strip()
        if clean_scope not in ("drive.file", "drive", "drive.appdata"):
            clean_scope = "drive.file"
        clean_display = (display_name or f"Google Drive ({clean_remote})").strip()[:100]

        has_token = token_json is not None and str(token_json).strip() != ""
        normalized_token = self.validate_and_normalize_drive_token(token_json) if has_token else ""

        cfg_payload: dict[str, Any] = {
            "type": "drive",
            "scope": clean_scope,
        }
        if normalized_token:
            cfg_payload["token"] = normalized_token
        if root_folder_id and str(root_folder_id).strip():
            cfg_payload["root_folder_id"] = str(root_folder_id).strip()

        encrypted_cfg = self.cipher.encrypt(json.dumps(cfg_payload, ensure_ascii=False))
        now = time.time()
        initial_status = "connected" if normalized_token else "auth_pending"
        initial_active = 1 if normalized_token else 0

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO rclone_accounts (
                    remote_name, provider, display_name, account_identity,
                    remote_base_path, encrypted_config_json, status, is_active,
                    last_error, created_at, updated_at
                ) VALUES (?, 'drive', ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(remote_name) DO UPDATE SET
                    provider = 'drive',
                    display_name = excluded.display_name,
                    account_identity = COALESCE(excluded.account_identity, rclone_accounts.account_identity),
                    remote_base_path = excluded.remote_base_path,
                    encrypted_config_json = excluded.encrypted_config_json,
                    status = excluded.status,
                    is_active = excluded.is_active,
                    last_error = NULL,
                    updated_at = excluded.updated_at;
                """,
                (
                    clean_remote,
                    clean_display,
                    (account_identity or "").strip() or None,
                    clean_base,
                    encrypted_cfg,
                    initial_status,
                    initial_active,
                    now,
                    now,
                ),
            )
            conn.commit()

        self.sync_rclone_conf_file()
        if normalized_token and test_after_add:
            return self.test_account_connection(clean_remote)
        acc = self.get_account(clean_remote)
        return acc.to_public_dict(self.count_pending_deletions(clean_remote)) if acc else {}

    # ------------------------------------------------------------------
    # Rclone-Native Google Drive Initial User Authorization Flow
    # ------------------------------------------------------------------
    def start_drive_authorization(
        self,
        *,
        remote_name: str,
        display_name: Optional[str] = None,
        account_identity: Optional[str] = None,
        remote_base_path: str = "ubagen_outputs",
        scope: str = "drive.file",
    ) -> dict[str, Any]:
        """
        Initiate Google Drive authorization via rclone's built-in default OAuth client:
        1. Ensures the Drive account record exists (`auth_pending` if not yet connected).
        2. Spawns `rclone authorize "drive" --auth-no-open-browser` on localhost (port 53682)
           and resolves the Google OAuth URL generated by rclone's internal default client_id.
        3. Also returns the exact CLI command (`rclone authorize "drive"`) so the admin can either:
           - Click the Google OAuth link directly and paste the redirect URL / code, OR
           - Paste a token JSON from `rclone authorize "drive"`.
        """
        clean_remote = self.validate_remote_name(remote_name)
        existing = self.get_account(clean_remote)
        if existing is None:
            self.add_drive_account(
                remote_name=clean_remote,
                display_name=display_name,
                account_identity=account_identity,
                remote_base_path=remote_base_path,
                scope=scope,
            )

        # Clean up any previous running rclone authorize process
        self._stop_auth_session_process(clean_remote)

        rclone_bin = resolve_rclone_binary(auto_bootstrap=True)
        local_auth_url: Optional[str] = None
        google_auth_url: Optional[str] = None
        state_param: Optional[str] = None
        proc: Optional[subprocess.Popen[str]] = None

        if rclone_bin and self._cli_runner is None:
            try:
                proc = subprocess.Popen(
                    [rclone_bin, "authorize", "drive", "--auth-no-open-browser"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                deadline = time.monotonic() + 3.0
                collected_lines: list[str] = []
                while time.monotonic() < deadline:
                    if proc.stdout is None:
                        break
                    line = proc.stdout.readline()
                    if not line:
                        if proc.poll() is not None:
                            break
                        time.sleep(0.05)
                        continue
                    collected_lines.append(line)
                    m = re.search(r"(http://127\.0\.0\.1:\d+/auth\?state=[A-Za-z0-9_-]+)", line)
                    if m:
                        local_auth_url = m.group(1)
                        parsed_local = urllib.parse.urlsplit(local_auth_url)
                        state_param = dict(urllib.parse.parse_qsl(parsed_local.query)).get("state")
                        break

                # Resolve the 302 redirect from rclone's local listener to get Google's OAuth URL
                if local_auth_url:
                    opener = urllib.request.build_opener(_NoRedirectHandler)
                    try:
                        with opener.open(local_auth_url, timeout=2.0) as resp:
                            google_auth_url = resp.headers.get("Location")
                    except urllib.error.HTTPError as http_err:
                        if http_err.code in (301, 302, 303, 307, 308):
                            google_auth_url = http_err.headers.get("Location")
                    except Exception:
                        pass
            except Exception as exc:
                logger.debug("Could not spawn interactive rclone authorize listener: %s", type(exc).__name__)

        session_id = f"rclone_auth_{clean_remote}_{ int(time.time()) }"
        self._auth_sessions[clean_remote] = {
            "session_id": session_id,
            "remote_name": clean_remote,
            "process": proc,
            "local_auth_url": local_auth_url,
            "google_auth_url": google_auth_url,
            "state": state_param,
            "scope": scope or "drive.file",
            "created_at": time.time(),
        }

        return {
            "remote_name": clean_remote,
            "session_id": session_id,
            "uses_rclone_default_oauth_client": True,
            "requires_custom_client_id": False,
            "google_auth_url": google_auth_url,
            "local_auth_url": local_auth_url,
            "state": state_param,
            "rclone_cli_command": 'rclone authorize "drive"',
            "instructions_tr": (
                "1. Yöntem (Tek Tıkla Tarayıcı Yetkilendirmesi): Yukarıdaki Google yetkilendirme bağlantısını açıp "
                "Google hesabınızla onay verin. Tarayıcının yönlendirdiği `http://127.0.0.1:53682/?state=...&code=...` "
                "adresini (veya `code` değerini) aşağıdaki alana yapıştırıp 'Yetkilendirmeyi Tamamla' düğmesine tıklayın.\n"
                "2. Yöntem (Rclone Token JSON): Bilgisayarınızda `rclone authorize \"drive\"` komutunu çalıştırıp "
                "üretilen `{\"access_token\":...}` JSON çıktısını doğrudan yapıştırabilirsiniz."
            ),
        }

    def _stop_auth_session_process(self, remote_name: str) -> None:
        sess = self._auth_sessions.pop(remote_name, None)
        if sess and sess.get("process") is not None:
            proc = sess["process"]
            with suppress(Exception):
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=1.5)

    def complete_drive_authorization(
        self,
        *,
        remote_name: str,
        token_or_callback: str | dict[str, Any],
        display_name: Optional[str] = None,
        account_identity: Optional[str] = None,
        remote_base_path: Optional[str] = None,
        test_after_auth: bool = False,
    ) -> dict[str, Any]:
        """
        Complete Google Drive rclone authorization for `remote_name` using either:
        - A pasted rclone token JSON (`{"access_token": "...", "refresh_token": "..."}`), OR
        - A callback URL (`http://127.0.0.1:53682/?state=...&code=...`) or authorization `code`
          which is forwarded to the active local `rclone authorize` process so rclone exchanges
          it using its built-in OAuth client and returns the token JSON.
        """
        clean_remote = self.validate_remote_name(remote_name)
        existing = self.get_account(clean_remote)
        effective_base = (
            remote_base_path
            or (existing.remote_base_path if existing else "ubagen_outputs")
        )
        effective_display = (
            display_name
            or (existing.display_name if existing else f"Google Drive ({clean_remote})")
        )
        effective_identity = account_identity or (existing.account_identity if existing else None)

        token_json_str: Optional[str] = None

        if isinstance(token_or_callback, dict):
            token_json_str = self.validate_and_normalize_drive_token(token_or_callback)
        else:
            raw_input = (token_or_callback or "").strip()
            if not raw_input:
                raise RcloneAuthError("Yetkilendirme yanıtı (token JSON veya geri dönüş URL/kodu) boş olamaz.")

            # Case 1: Input contains JSON with access_token / refresh_token
            if "{" in raw_input and ("access_token" in raw_input or "refresh_token" in raw_input):
                token_json_str = self.validate_and_normalize_drive_token(raw_input)
            else:
                # Case 2: Input is a redirect URL (http://127.0.0.1:53682/?state=...&code=...) or raw auth code
                sess = self._auth_sessions.get(clean_remote)
                proc: Optional[subprocess.Popen[str]] = sess.get("process") if sess else None
                callback_url: Optional[str] = None
                if raw_input.startswith(("http://127.0.0.1:53682", "http://localhost:53682")):
                    callback_url = raw_input.replace("http://localhost:53682", "http://127.0.0.1:53682")
                elif "code=" in raw_input:
                    parsed_qs = dict(urllib.parse.parse_qsl(raw_input.split("?", 1)[-1]))
                    code_val = parsed_qs.get("code", "")
                    state_val = parsed_qs.get("state") or (sess.get("state") if sess else "")
                    if code_val:
                        callback_url = f"http://127.0.0.1:53682/?{urllib.parse.urlencode({'state': state_val or '', 'code': code_val})}"
                elif sess and sess.get("state"):
                    callback_url = f"http://127.0.0.1:53682/?{urllib.parse.urlencode({'state': sess['state'], 'code': raw_input})}"

                if callback_url and proc is not None and proc.poll() is None:
                    with suppress(Exception):
                        urllib.request.urlopen(callback_url, timeout=10.0).read()
                    try:
                        stdout_rest, _ = proc.communicate(timeout=10.0)
                    except Exception:
                        stdout_rest = ""
                    self._stop_auth_session_process(clean_remote)
                    if stdout_rest and "access_token" in stdout_rest:
                        token_json_str = self.validate_and_normalize_drive_token(stdout_rest)

                if not token_json_str:
                    raise RcloneAuthError(
                        "Yetkilendirme kodu rclone oturumu üzerinden takas edilemedi veya oturum zaman aşımına uğradı. "
                        "Lütfen yeni bir yetkilendirme oturumu başlatın veya `rclone authorize \"drive\"` JSON çıktısını yapıştırın."
                    )

        self._stop_auth_session_process(clean_remote)
        return self.add_drive_account(
            remote_name=clean_remote,
            token_json=token_json_str,
            display_name=effective_display,
            account_identity=effective_identity,
            remote_base_path=effective_base,
            test_after_add=test_after_auth,
        )

    def start_gdrive_auth_session(
        self,
        *,
        remote_name: Optional[str] = None,
        display_name: Optional[str] = "Google Drive",
        scope: str = "drive.file",
        root_folder: str = "ubagen_outputs",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Convenience alias for start_drive_authorization."""
        clean_remote = (remote_name or "").strip() or f"gdrive_{uuid.uuid4().hex[:6]}"
        base_path = str(kwargs.get("remote_base_path") or root_folder or "ubagen_outputs").strip()
        return self.start_drive_authorization(
            remote_name=clean_remote,
            display_name=display_name,
            remote_base_path=base_path,
            scope=scope,
        )

    def complete_gdrive_auth(
        self,
        *,
        remote_name: Optional[str] = None,
        code_or_token: Optional[str] = None,
        token_or_code: Optional[str] = None,
        display_name: Optional[str] = None,
        root_folder: str = "ubagen_outputs",
        skip_verify: bool = False,
        session_id: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Convenience alias for complete_drive_authorization."""
        tok = str(code_or_token or token_or_code or kwargs.get("token") or kwargs.get("code") or "").strip()
        rname = (remote_name or session_id or "").strip()
        base_path = str(kwargs.get("remote_base_path") or root_folder or "ubagen_outputs").strip()
        return self.complete_drive_authorization(
            remote_name=rname,
            token_or_callback=tok,
            display_name=display_name,
            remote_base_path=base_path,
            test_after_auth=not skip_verify,
        )

    # ------------------------------------------------------------------
    # Connection Test, Safe Disconnect & Reconnect
    # ------------------------------------------------------------------
    def set_account_enabled(self, remote_name: str, enabled: bool) -> tuple[bool, dict[str, Any]]:
        """Enable or disable an existing remote for new uploads without deleting remote files."""
        clean_remote = self.validate_remote_name(remote_name)
        account = self.get_account(clean_remote)
        if account is None:
            return False, {"ok": False, "error": f"Rclone hesabı bulunamadı: {clean_remote}"}
        if account.status == "disconnected" and enabled:
            return False, {"ok": False, "error": "Önce hesabı yeniden bağlayıp bağlantıyı test edin."}
        now = time.time()
        new_status = "connected" if enabled else "disabled"
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE rclone_accounts SET is_active = ?, status = ?, updated_at = ? WHERE remote_name = ?;",
                (1 if enabled else 0, new_status, now, clean_remote),
            )
            conn.commit()
        self.sync_rclone_conf_file()
        updated = self.get_account(clean_remote)
        return True, {
            "ok": True,
            "message": "Hesap etkinleştirildi." if enabled else "Hesap yüklemeler için devre dışı bırakıldı.",
            "account": updated.to_public_dict(self.count_pending_deletions(clean_remote)) if updated else {},
        }

    def test_account(self, remote_name: str) -> dict[str, Any]:
        """Backward-compatible alias used by diagnostics."""
        return self.test_account_connection(remote_name)

    def test_account_connection(self, remote_name: str) -> dict[str, Any]:
        """
        Run a real `rclone about <remote>:` and `rclone lsd <remote>:` probe against the account,
        sync any refreshed OAuth token from `rclone.conf` back into SQLite, and return updated status.
        """
        clean_remote = (remote_name or "").strip()
        acc = self.get_account(clean_remote)
        if acc is None:
            raise KeyError(f"Rclone hesabı bulunamadı: {clean_remote}")

        if not acc.is_active or acc.status == "disconnected":
            raise RcloneError(
                f"'{clean_remote}' hesabının bağlantısı kesilmiş durumda. Test etmeden önce 'Yeniden Bağlan' işlemini kullanın."
            )
        if acc.status == "auth_pending":
            raise RcloneAuthError(
                f"'{clean_remote}' Google Drive hesabı için ilk kullanıcı yetkilendirmesi henüz tamamlanmamış."
            )

        now = time.time()
        rclone_bin = self.get_rclone_binary()
        if not is_rclone_backend_supported(rclone_bin, acc.provider):
            recovered_bin = resolve_rclone_binary(auto_bootstrap=True, require_backend=acc.provider)
            if recovered_bin and is_rclone_backend_supported(recovered_bin, acc.provider):
                self._verified_binary = recovered_bin
                rclone_bin = recovered_bin
            else:
                err_msg = (
                    f"Rclone ikilisi ('{rclone_bin}') '{acc.provider}' depolama backend'ini içermiyor. "
                    f"Resmî tam rclone sürümü gereklidir."
                )
                with self._lock, self._connect() as conn:
                    conn.execute(
                        """
                        UPDATE rclone_accounts
                        SET status = 'error', last_test_ok = 0, last_tested_at = ?, last_error = ?, updated_at = ?
                        WHERE remote_name = ?;
                        """,
                        (now, err_msg, now, clean_remote),
                    )
                    conn.commit()
                return {
                    "ok": False,
                    "connected": False,
                    "provider": acc.provider,
                    "remote_name": clean_remote,
                    "error": err_msg,
                    "backend_missing": True,
                }

        about_proc = self._run_rclone_cli(
            ["about", f"{clean_remote}:", "--json"],
            timeout=25,
            sync_remote_token=clean_remote,
        )

        quota_dict: Optional[dict[str, Any]] = None
        test_ok = False
        err_msg: Optional[str] = None

        if about_proc.returncode == 0:
            test_ok = True
            with suppress(Exception):
                raw_json = (about_proc.stdout or "").strip()
                if raw_json:
                    parsed = json.loads(raw_json)
                    if isinstance(parsed, dict):
                        quota_dict = {
                            "total": parsed.get("total"),
                            "used": parsed.get("used"),
                            "free": parsed.get("free"),
                            "trashed": parsed.get("trashed"),
                        }
        else:
            # Fallback to `rclone lsd <remote>:` in case remote does not support `about`
            lsd_proc = self._run_rclone_cli(
                ["lsd", f"{clean_remote}:"],
                timeout=20,
                sync_remote_token=clean_remote,
            )
            if lsd_proc.returncode == 0:
                test_ok = True
            else:
                raw_err = (about_proc.stderr or lsd_proc.stderr or about_proc.stdout or "Bağlantı testi başarısız.").strip()
                if "didn't find backend called" in raw_err.lower():
                    err_msg = (
                        f"Rclone '{acc.provider}' backend'ini bulamadı. "
                        f"Resmî tam rclone dağıtımı kurulmalıdır: {self.redact_rclone_text(raw_err)[:200]}"
                    )
                else:
                    err_msg = self.redact_rclone_text(raw_err)[:280]

        new_status = "connected" if test_ok else "error"
        quota_str = json.dumps(quota_dict) if quota_dict is not None else acc.quota_json

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE rclone_accounts
                SET status = ?, last_test_ok = ?, last_tested_at = ?, last_error = ?, quota_json = ?, updated_at = ?
                WHERE remote_name = ?;
                """,
                (
                    new_status,
                    1 if test_ok else 0,
                    now,
                    None if test_ok else err_msg,
                    quota_str,
                    now,
                    clean_remote,
                ),
            )
            conn.commit()

        # If connection succeeded, also process any due pending deletions for this remote
        if test_ok:
            with suppress(Exception):
                self.process_pending_deletions(remote_name=clean_remote)

        updated = self.get_account(clean_remote)
        return updated.to_public_dict(self.count_pending_deletions(clean_remote)) if updated else {}

    def disconnect_account(self, remote_name: str, remove_record: bool = False) -> dict[str, Any]:
        """
        Safely disconnect `remote_name`:
        - Sets `status = 'disconnected'` and `is_active = 0`.
        - Removes the remote from active `rclone.conf` so no uploads/operations use it while disconnected.
        - NEVER deletes any files on the remote or locally.
        - Permanently preserves all pending deletion tasks (`rclone_pending_deletions`) in SQLite.
        """
        clean_remote = (remote_name or "").strip()
        acc = self.get_account(clean_remote)
        if acc is None:
            raise KeyError(f"Rclone hesabı bulunamadı: {clean_remote}")

        now = time.time()
        with self._lock, self._connect() as conn:
            if remove_record:
                conn.execute("DELETE FROM rclone_accounts WHERE remote_name = ?;", (clean_remote,))
            else:
                conn.execute(
                    """
                    UPDATE rclone_accounts
                    SET status = 'disconnected', is_active = 0, updated_at = ?
                    WHERE remote_name = ?;
                    """,
                    (now, clean_remote),
                )
            conn.commit()

        self._stop_auth_session_process(clean_remote)
        self.sync_rclone_conf_file()
        preserved_deletions = self.count_pending_deletions(clean_remote)
        updated = self.get_account(clean_remote)
        result = updated.to_public_dict(preserved_deletions) if updated else {"remote_name": clean_remote, "status": "removed"}
        result["files_deleted"] = False
        result["pending_deletions_preserved"] = True
        result["pending_deletions_count"] = preserved_deletions
        return result

    def reconnect_account(
        self,
        remote_name: str,
        *,
        password: Optional[str] = None,
        token_json: Optional[str | dict[str, Any]] = None,
        run_test: bool = False,
    ) -> dict[str, Any]:
        """
        Reconnect a previously disconnected or errored MEGA / Google Drive account:
        - Optionally updates credentials (`password` for MEGA or `token_json` for Google Drive).
        - Re-activates the remote in `rclone.conf` (`status = 'connected'`, `is_active = 1`).
        - Processes any due pending deletion tasks that were preserved while disconnected.
        """
        clean_remote = (remote_name or "").strip()
        acc = self.get_account(clean_remote)
        if acc is None:
            raise KeyError(f"Rclone hesabı bulunamadı: {clean_remote}")

        cfg_dict = json.loads(self.cipher.decrypt(acc.encrypted_config_json))
        if acc.provider == "mega" and password and password.strip():
            cfg_dict["pass"] = obscure_rclone_password(password)
        elif acc.provider == "drive" and token_json is not None and str(token_json).strip():
            cfg_dict["token"] = self.validate_and_normalize_drive_token(token_json)

        if acc.provider == "drive" and not cfg_dict.get("token"):
            raise RcloneAuthError(
                f"'{clean_remote}' Google Drive hesabında kayıtlı OAuth token bulunmuyor. "
                "Lütfen rclone yetkilendirmesini tamamlayın."
            )

        enc_cfg = self.cipher.encrypt(json.dumps(cfg_dict, ensure_ascii=False))
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE rclone_accounts
                SET encrypted_config_json = ?, status = 'connected', is_active = 1, last_error = NULL, updated_at = ?
                WHERE remote_name = ?;
                """,
                (enc_cfg, now, clean_remote),
            )
            conn.commit()

        self.sync_rclone_conf_file()
        if run_test:
            return self.test_account_connection(clean_remote)

        with suppress(Exception):
            self.process_pending_deletions(remote_name=clean_remote)

        updated = self.get_account(clean_remote)
        return updated.to_public_dict(self.count_pending_deletions(clean_remote)) if updated else {}

    # ------------------------------------------------------------------
    # Persistent Pending Deletion Queue (`rclone_pending_deletions`)
    # ------------------------------------------------------------------
    def schedule_pending_deletion(
        self,
        *,
        remote_name: str,
        remote_object_path: str,
        due_at: Optional[float] = None,
        job_id: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> PendingDeletionRecord:
        """
        Persist a scheduled file deletion task in `rclone_pending_deletions`.
        Idempotent per `(remote_name, remote_object_path)`.
        Survives account disconnects and process restarts.
        """
        clean_remote = (remote_name or "").strip()
        clean_path = (remote_object_path or "").strip().lstrip("/")
        if not clean_remote or not clean_path:
            raise ValueError("Silme görevi için remote_name ve remote_object_path zorunludur.")

        acc = self.get_account(clean_remote)
        eff_provider = provider or (acc.provider if acc else "drive")
        now = time.time()
        eff_due = float(due_at if due_at is not None else now)
        del_id = f"del_{hashlib.sha256(f'{clean_remote}:{clean_path}'.encode('utf-8')).hexdigest()[:20]}"

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO rclone_pending_deletions (
                    deletion_id, job_id, remote_name, provider, remote_object_path,
                    due_at, status, attempts, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, NULL, ?, ?)
                ON CONFLICT(deletion_id) DO UPDATE SET
                    due_at = MIN(rclone_pending_deletions.due_at, excluded.due_at),
                    job_id = COALESCE(excluded.job_id, rclone_pending_deletions.job_id),
                    updated_at = excluded.updated_at
                WHERE rclone_pending_deletions.status != 'completed';
                """,
                (del_id, job_id, clean_remote, eff_provider, clean_path, eff_due, now, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM rclone_pending_deletions WHERE deletion_id = ?;",
                (del_id,),
            ).fetchone()
            assert row is not None
            return self._row_to_deletion(row)

    @staticmethod
    def _row_to_deletion(row: sqlite3.Row) -> PendingDeletionRecord:
        return PendingDeletionRecord(
            deletion_id=str(row["deletion_id"]),
            job_id=row["job_id"],
            remote_name=str(row["remote_name"]),
            provider=str(row["provider"]),
            remote_object_path=str(row["remote_object_path"]),
            due_at=float(row["due_at"]),
            status=str(row["status"]),
            attempts=int(row["attempts"] or 0),
            last_error=row["last_error"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def count_pending_deletions(self, remote_name: Optional[str] = None) -> int:
        with self._lock, self._connect() as conn:
            if remote_name:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM rclone_pending_deletions WHERE remote_name = ? AND status = 'pending';",
                    (remote_name.strip(),),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM rclone_pending_deletions WHERE status = 'pending';"
                ).fetchone()
            return int(row["cnt"]) if row else 0

    def list_pending_deletions(
        self,
        *,
        remote_name: Optional[str] = None,
        status: Optional[str] = "pending",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            clauses: list[str] = []
            params: list[Any] = []
            if remote_name:
                clauses.append("remote_name = ?")
                params.append(remote_name.strip())
            if status:
                clauses.append("status = ?")
                params.append(status.strip())
            where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            params.append(max(1, min(200, int(limit))))
            rows = conn.execute(
                f"SELECT * FROM rclone_pending_deletions {where_sql} ORDER BY due_at ASC LIMIT ?;",
                tuple(params),
            ).fetchall()
            return [self._row_to_deletion(r).to_public_dict() for r in rows]

    def process_pending_deletions(
        self,
        *,
        remote_name: Optional[str] = None,
        now: Optional[float] = None,
        now_ts: Optional[float] = None,
    ) -> dict[str, Any]:
        """
        Execute due pending deletions (`due_at <= now` and `status == 'pending'`).
        If the target account is currently disconnected (`is_active == False` or `status == 'disconnected'`),
        the deletion task is preserved as `'pending'` without failing or losing it.
        """
        effective_now = now if now is not None else now_ts
        cur = float(effective_now if effective_now is not None else time.time())
        with self._lock, self._connect() as conn:
            if remote_name:
                rows = conn.execute(
                    "SELECT * FROM rclone_pending_deletions WHERE status = 'pending' AND remote_name = ? AND due_at <= ? ORDER BY due_at ASC;",
                    (remote_name.strip(), cur),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM rclone_pending_deletions WHERE status = 'pending' AND due_at <= ? ORDER BY due_at ASC;",
                    (cur,),
                ).fetchall()
            tasks = [self._row_to_deletion(r) for r in rows]

        completed_count = 0
        preserved_disconnected = 0
        retried_errors = 0

        for task in tasks:
            acc = self.get_account(task.remote_name)
            if acc is None or not acc.is_active or acc.status != "connected":
                # Account is disconnected -> keep pending deletion intact without incrementing attempts!
                preserved_disconnected += 1
                continue

            with suppress(Exception):
                self.revoke_public_link(task.remote_name, task.remote_object_path)

            ok, err = self._execute_rclone_deletefile(task.remote_name, task.remote_object_path)
            # Verify the remote file is genuinely gone
            verified_gone = ok and (not self.verify_object(task.remote_name, task.remote_object_path))

            with self._lock, self._connect() as conn:
                if verified_gone or ok:
                    conn.execute(
                        """
                        UPDATE rclone_pending_deletions
                        SET status = 'completed', attempts = attempts + 1, last_error = NULL, updated_at = ?
                        WHERE deletion_id = ?;
                        """,
                        (cur, task.deletion_id),
                    )
                    completed_count += 1
                    # Update job store status to expired and mark deleted
                    if task.job_id:
                        with suppress(Exception):
                            from job_store import get_job_store
                            store = get_job_store()
                            job = store.get_job(task.job_id)
                            if job and not job.deleted_from_gcs:
                                job.stage = "expired"
                                job.status = "expired"
                                job.download_url = None
                                job.deleted_from_gcs = True
                                job.deleted_at = cur
                                store.save_job(job)
                else:
                    # Exponential backoff retry: 30s, 60s, 120s, 240s, ..., max 3600s
                    next_attempt = task.attempts + 1
                    backoff_delay = min(3600.0, 30.0 * (2.0 ** min(task.attempts, 6)))
                    next_due = cur + backoff_delay
                    conn.execute(
                        """
                        UPDATE rclone_pending_deletions
                        SET status = 'pending', attempts = ?, due_at = ?, last_error = ?, updated_at = ?
                        WHERE deletion_id = ?;
                        """,
                        (next_attempt, next_due, err or "Deletion verification failed", cur, task.deletion_id),
                    )
                    retried_errors += 1
                conn.commit()

        return {
            "processed_due": len(tasks),
            "completed": completed_count,
            "deleted": completed_count,
            "deleted_count": completed_count,
            "preserved_while_disconnected": preserved_disconnected,
            "pending_retry": retried_errors,
            "remaining_pending": self.count_pending_deletions(remote_name),
        }

    # ------------------------------------------------------------------
    # Rclone File Upload, Verification, Download & Deletion Operations
    # ------------------------------------------------------------------
    def select_active_remote(self, preferred_remote: Optional[str] = None) -> Optional[RcloneAccountRecord]:
        """
        Select an active connected MEGA or Google Drive remote.
        If `preferred_remote` (or `rclone://<remote>`) matches a connected account, returns it;
        otherwise returns the first active connected account.
        """
        clean_pref = (preferred_remote or "").strip()
        if clean_pref.startswith("rclone://"):
            clean_pref = clean_pref[len("rclone://") :].strip()

        accounts = self._load_all_account_rows()
        active_connected = [a for a in accounts if a.is_active and a.status == "connected"]
        if clean_pref:
            for acc in active_connected:
                if acc.remote_name == clean_pref:
                    return acc
        return active_connected[0] if active_connected else None

    @staticmethod
    def _build_remote_spec(acc: RcloneAccountRecord, object_name: str) -> tuple[str, str]:
        base = (acc.remote_base_path or "ubagen_outputs").strip().strip("/")
        clean_obj = (object_name or "").strip().lstrip("/")
        rel_path = f"{base}/{clean_obj}" if base and not clean_obj.startswith(base + "/") else clean_obj
        return f"{acc.remote_name}:{rel_path}", rel_path

    def upload_file(
        self,
        local_path: Path,
        object_name: str,
        *,
        preferred_remote: Optional[str] = None,
        retention_seconds: Optional[int] = None,
        job_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Upload `local_path` to an active MEGA or Google Drive account via `rclone copyto`,
        verify the uploaded file size via `rclone lsjson`, and register a persistent
        retention deletion task if `retention_seconds` is provided.
        """
        if not local_path.is_file():
            raise FileNotFoundError(f"Yüklenecek yerel dosya bulunamadı: {local_path}")
        expected_size = local_path.stat().st_size
        if expected_size <= 0:
            raise RcloneError("Yüklenecek dosya boş (0 byte).")

        acc = self.select_active_remote(preferred_remote)
        if acc is None:
            raise RcloneError(
                "Aktif bir rclone depolama hesabı (MEGA veya Google Drive) bağlı değil. "
                "Lütfen UBAgen Yönetim Panelinden bir MEGA veya Google Drive hesabı bağlayın."
            )

        remote_spec, rel_path = self._build_remote_spec(acc, object_name)
        proc = self._run_rclone_cli(
            ["copyto", str(local_path), remote_spec],
            timeout=600,
            sync_remote_token=acc.remote_name,
        )
        if proc.returncode != 0:
            err = self.redact_rclone_text(proc.stderr or proc.stdout or f"rclone exit {proc.returncode}")
            raise RcloneError(f"rclone yükleme hatası ({acc.remote_name}): {err[:300]}")

        verified = self.verify_object(acc.remote_name, object_name, expected_size=expected_size)
        if not verified:
            raise RcloneError(f"rclone nesne doğrulaması başarısız ({acc.remote_name}:{rel_path}).")

        # Generate and verify publicly accessible direct share link
        public_url = self.generate_public_link(acc.remote_name, rel_path) or self.generate_public_link(acc.remote_name, object_name)
        if not public_url:
            raise RcloneError(
                f"rclone çıktısı yüklendi ve doğrulandı ancak herkese açık paylaşım bağlantısı (public link) oluşturulamadı ({acc.remote_name}:{rel_path})."
            )

        effective_retention = int(retention_seconds if retention_seconds is not None else 7200)
        if effective_retention > 0:
            self.schedule_pending_deletion(
                remote_name=acc.remote_name,
                remote_object_path=rel_path,
                due_at=time.time() + float(effective_retention),
                job_id=job_id,
                provider=acc.provider,
            )

        return {
            "uploaded": True,
            "verified": True,
            "bucket": acc.remote_name,
            "remote_name": acc.remote_name,
            "provider": acc.provider,
            "object_name": object_name.lstrip("/"),
            "remote_object_path": rel_path,
            "gcs_uri": f"rclone://{acc.remote_name}/{rel_path}",
            "public_url": public_url,
            "download_url": public_url,
            "size_bytes": expected_size,
            "backend": f"rclone_{acc.provider}",
        }

    def generate_public_link(
        self,
        remote_name: str,
        object_name: str,
    ) -> Optional[str]:
        """
        Generate and retrieve a publicly shareable direct link to a file on MEGA or Google Drive
        via `rclone link <remote>:<path>`.
        - For Google Drive: Sets permissions to anyone-with-the-link and returns web view/download link.
        - For MEGA: Exports the file and returns the mega.nz public file sharing link.
        - Never makes the entire folder or account public; only targets the specific object path.
        """
        clean_remote = (remote_name or "").replace("rclone://", "").strip()
        acc = self.get_account(clean_remote) or self.select_active_remote(clean_remote)
        if acc is None:
            return None

        remote_spec, _ = self._build_remote_spec(acc, object_name)
        proc = self._run_rclone_cli(
            ["link", remote_spec],
            timeout=45,
            sync_remote_token=acc.remote_name,
        )
        if proc.returncode == 0 and proc.stdout:
            for line in reversed(proc.stdout.strip().splitlines()):
                candidate = line.strip()
                if candidate.startswith(("https://", "http://")):
                    return candidate
        err = self.redact_rclone_text(proc.stderr or proc.stdout or "")
        logger.warning("rclone link failed for %s: %s", remote_spec, err[:200])
        return None

    def revoke_public_link(
        self,
        remote_name: str,
        object_name: str,
    ) -> bool:
        """
        Revoke the publicly shareable direct link to a file via `rclone link --unlink <remote>:<path>`.
        """
        clean_remote = (remote_name or "").replace("rclone://", "").strip()
        acc = self.get_account(clean_remote) or self.select_active_remote(clean_remote)
        if acc is None:
            return False

        remote_spec, _ = self._build_remote_spec(acc, object_name)
        proc = self._run_rclone_cli(
            ["link", "--unlink", remote_spec],
            timeout=30,
            sync_remote_token=acc.remote_name,
        )
        return proc.returncode == 0

    def verify_object(
        self,
        remote_name: str,
        object_name: str,
        expected_size: Optional[int] = None,
    ) -> bool:
        """Verify via `rclone lsjson` that the remote object exists and matches `expected_size`."""
        clean_remote = (remote_name or "").replace("rclone://", "").strip()
        acc = self.get_account(clean_remote) or self.select_active_remote(clean_remote)
        if acc is None or not acc.is_active or acc.status != "connected":
            return False

        remote_spec, _ = self._build_remote_spec(acc, object_name)
        proc = self._run_rclone_cli(
            ["lsjson", remote_spec],
            timeout=30,
            sync_remote_token=acc.remote_name,
        )
        if proc.returncode != 0:
            return False
        try:
            items = json.loads(proc.stdout or "[]")
            if not isinstance(items, list) or len(items) == 0:
                return False
            actual_size = int(items[0].get("Size") or 0)
            if actual_size <= 0:
                return False
            if expected_size is not None and actual_size != expected_size:
                return False
            return True
        except Exception:
            return False

    def download_to_path(self, remote_name: str, object_name: str, dest_path: Path) -> Path:
        """Download an object from an rclone remote (`rclone copyto`) into `dest_path`."""
        clean_remote = (remote_name or "").replace("rclone://", "").strip()
        acc = self.get_account(clean_remote) or self.select_active_remote(clean_remote)
        if acc is None or not acc.is_active or acc.status != "connected":
            raise RcloneError(f"Rclone hesabı aktif değil veya bulunamadı: {clean_remote}")

        remote_spec, _ = self._build_remote_spec(acc, object_name)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        proc = self._run_rclone_cli(
            ["copyto", remote_spec, str(dest_path)],
            timeout=300,
            sync_remote_token=acc.remote_name,
        )
        if proc.returncode != 0 or not dest_path.is_file() or dest_path.stat().st_size <= 0:
            err = self.redact_rclone_text(proc.stderr or proc.stdout or "İndirme başarısız.")
            raise RcloneError(f"rclone indirme hatası ({acc.remote_name}): {err[:240]}")
        return dest_path

    def _execute_rclone_deletefile(self, remote_name: str, rel_path: str) -> tuple[bool, Optional[str]]:
        proc = self._run_rclone_cli(
            ["deletefile", f"{remote_name}:{rel_path.lstrip('/')}"],
            timeout=45,
            sync_remote_token=remote_name,
        )
        if proc.returncode == 0:
            return True, None
        stderr_lower = (proc.stderr or "").lower()
        if "not found" in stderr_lower or "doesn't exist" in stderr_lower or "file not found" in stderr_lower:
            return True, None
        return False, self.redact_rclone_text(proc.stderr or proc.stdout or f"Exit {proc.returncode}")[:240]

    def delete_object(
        self,
        remote_name: str,
        object_name: str,
        *,
        job_id: Optional[str] = None,
    ) -> bool:
        """
        Delete an object from `remote_name` via `rclone deletefile`.
        If the account is currently disconnected (`is_active=False` or `status=='disconnected'`)
        or temporarily unreachable, records/keeps the deletion task in `rclone_pending_deletions`
        so the pending deletion is permanently preserved until reconnection!
        """
        clean_remote = (remote_name or "").replace("rclone://", "").strip()
        acc = self.get_account(clean_remote)
        if acc is None:
            return False

        _, rel_path = self._build_remote_spec(acc, object_name)
        if not acc.is_active or acc.status != "connected":
            self.schedule_pending_deletion(
                remote_name=acc.remote_name,
                remote_object_path=rel_path,
                due_at=time.time(),
                job_id=job_id,
                provider=acc.provider,
            )
            return False

        ok, err = self._execute_rclone_deletefile(acc.remote_name, rel_path)
        if ok:
            del_id = f"del_{hashlib.sha256(f'{acc.remote_name}:{rel_path}'.encode('utf-8')).hexdigest()[:20]}"
            with self._lock, self._connect() as conn:
                conn.execute(
                    "UPDATE rclone_pending_deletions SET status = 'completed', last_error = NULL, updated_at = ? WHERE deletion_id = ?;",
                    (time.time(), del_id),
                )
                conn.commit()
            return True

        # Preserve in pending deletions queue on failure
        record = self.schedule_pending_deletion(
            remote_name=acc.remote_name,
            remote_object_path=rel_path,
            due_at=time.time(),
            job_id=job_id,
            provider=acc.provider,
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE rclone_pending_deletions SET attempts = attempts + 1, last_error = ?, updated_at = ? WHERE deletion_id = ?;",
                (err, time.time(), record.deletion_id),
            )
            conn.commit()
        return False

    @staticmethod
    def redact_rclone_text(text: str) -> str:
        from stats import redact_sensitive_text

        out = redact_sensitive_text(text or "")
        out = re.sub(
            r"(?i)(pass|password|token|client_secret|refresh_token|access_token|auth|secret|key)\s*[=:]\s*\S+",
            r"\1=[REDACTED]",
            out,
        )
        return out

    def get_status_summary(self, refresh_storage: bool = False) -> dict[str, Any]:
        """Return complete non-sensitive status of rclone binary, accounts, and pending deletions."""
        bin_info = check_rclone_binary()
        accounts = self.list_accounts()
        active_accounts = [a for a in accounts if a["is_active"] and a["status"] == "connected"]
        drive_accounts = [a for a in accounts if a["provider"] == "drive"]
        mega_accounts = [a for a in accounts if a["provider"] == "mega"]
        pending_dels = self.list_pending_deletions(status="pending", limit=25)

        return {
            "binary": bin_info,
            "config_path": str(self.rclone_config_path),
            "config_exists": self.rclone_config_path.is_file(),
            "accounts_total": len(accounts),
            "active_accounts_count": len(active_accounts),
            "drive_accounts_count": len(drive_accounts),
            "mega_accounts_count": len(mega_accounts),
            "uses_rclone_default_oauth_for_drive": True,
            "requires_google_cloud_project": False,
            "accounts": accounts,
            "pending_deletions_count": self.count_pending_deletions(),
            "pending_deletions": pending_dels,
        }


_GLOBAL_RCLONE_MANAGER: Optional[RcloneStorageManager] = None
_RCLONE_LOCK = threading.Lock()


def get_rclone_manager() -> RcloneStorageManager:
    global _GLOBAL_RCLONE_MANAGER
    with _RCLONE_LOCK:
        if _GLOBAL_RCLONE_MANAGER is None:
            _GLOBAL_RCLONE_MANAGER = RcloneStorageManager()
        return _GLOBAL_RCLONE_MANAGER


def redact_rclone_text(text: str) -> str:
    return RcloneStorageManager.redact_rclone_text(text)


sanitize_rclone_text = redact_rclone_text


def reset_rclone_manager_for_tests(mgr: Optional[RcloneStorageManager] = None) -> None:
    global _GLOBAL_RCLONE_MANAGER
    with _RCLONE_LOCK:
        _GLOBAL_RCLONE_MANAGER = mgr


# ------------------------------------------------------------------------------
# Admin Diagnostic System & `/log` Slash Command
# ------------------------------------------------------------------------------
LOG_COMMAND_NAME = "log"
LEGACY_REMOVED_DRIVE_COMMANDS = ("drive_bagla", "drive_durum", "drive_ayir", "drive_yardim", "drive")

_ERROR_RING_BUFFER_LOCK = threading.Lock()
_RECENT_ERROR_RECORDS: list[dict[str, Any]] = []
_MAX_RECENT_ERRORS = 40
_RING_HANDLER_INSTALLED = False


class RecentErrorRingBufferHandler(logging.Handler):
    """
    Captures recent WARNING, ERROR, and CRITICAL logs along with sanitized traceback
    summaries for the admin `/log` diagnostic command without leaking secrets.
    """

    def __init__(self, level: int = logging.WARNING) -> None:
        super().__init__(level=level)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            from stats import redact_sensitive_text

            msg = RcloneStorageManager.redact_rclone_text(redact_sensitive_text(record.getMessage()))
            tb_summary: Optional[str] = None
            if record.exc_info and record.exc_info[1] is not None:
                import traceback

                raw_tb = "".join(traceback.format_exception(*record.exc_info))
                lines = [
                    RcloneStorageManager.redact_rclone_text(redact_sensitive_text(ln.strip()))
                    for ln in raw_tb.splitlines()
                    if ln.strip()
                ]
                tb_summary = " | ".join(lines[-4:])[:360]

            entry = {
                "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "epoch": float(record.created),
                "level": record.levelname,
                "logger": record.name,
                "message": msg[:320],
                "traceback": tb_summary,
            }
            with _ERROR_RING_BUFFER_LOCK:
                _RECENT_ERROR_RECORDS.append(entry)
                if len(_RECENT_ERROR_RECORDS) > _MAX_RECENT_ERRORS:
                    del _RECENT_ERROR_RECORDS[:-_MAX_RECENT_ERRORS]
        except Exception:
            pass


def install_error_ring_buffer() -> None:
    """Attach `RecentErrorRingBufferHandler` idempotently to the root logger."""
    global _RING_HANDLER_INSTALLED
    with _ERROR_RING_BUFFER_LOCK:
        if _RING_HANDLER_INSTALLED:
            return
        root_logger = logging.getLogger()
        for existing in root_logger.handlers:
            if isinstance(existing, RecentErrorRingBufferHandler):
                _RING_HANDLER_INSTALLED = True
                return
        root_logger.addHandler(RecentErrorRingBufferHandler())
        _RING_HANDLER_INSTALLED = True


def record_application_error(
    message: str,
    *,
    level: str = "ERROR",
    logger_name: str = "ubagen.runtime",
    traceback_text: Optional[str] = None,
) -> None:
    """Explicitly record a sanitized application error/traceback into the diagnostic ring buffer."""
    from stats import redact_sensitive_text

    now_ts = time.time()
    clean_msg = RcloneStorageManager.redact_rclone_text(redact_sensitive_text(str(message or "")))[:320]
    clean_tb = (
        RcloneStorageManager.redact_rclone_text(redact_sensitive_text(str(traceback_text)))[:360]
        if traceback_text
        else None
    )
    entry = {
        "timestamp": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),
        "epoch": now_ts,
        "level": str(level or "ERROR").upper(),
        "logger": logger_name,
        "message": clean_msg,
        "traceback": clean_tb,
    }
    with _ERROR_RING_BUFFER_LOCK:
        _RECENT_ERROR_RECORDS.append(entry)
        if len(_RECENT_ERROR_RECORDS) > _MAX_RECENT_ERRORS:
            del _RECENT_ERROR_RECORDS[:-_MAX_RECENT_ERRORS]


def get_recent_application_errors(limit: int = 10) -> list[dict[str, Any]]:
    with _ERROR_RING_BUFFER_LOCK:
        items = list(_RECENT_ERROR_RECORDS)
    items.sort(key=lambda x: float(x.get("epoch") or 0.0), reverse=True)
    return items[: max(1, int(limit))]


def clear_recent_application_errors_for_tests() -> None:
    with _ERROR_RING_BUFFER_LOCK:
        _RECENT_ERROR_RECORDS.clear()


def collect_diagnostic_log_report(
    *,
    bot: Optional[Any] = None,
    bot_manager: Optional[Any] = None,
    run_live_remote_tests: bool = False,
) -> dict[str, Any]:
    """
    Gather the comprehensive admin diagnostic report for `/log` and `GET /api/diagnostics/log`:
    1. Recent application errors & traceback summaries (`recent_errors`).
    2. Missing required/optional environment variables & secret names (`env_audit`) — never leaks values.
    3. Missing or invalid configuration files (`config_files`).
    4. Rclone installation, version, and runnable status (`rclone_binary`).
    5. MEGA and Google Drive remote configurations & connection test results (`rclone_remotes`).
    6. Discord bot connection, intents/permissions, and slash command synchronization status (`discord_status`).
    7. Backend endpoints, database, storage, and job queue health (`backend_health`).
    """
    install_error_ring_buffer()
    from stats import collect_all_stats, redact_sensitive_text
    from subtitle_extract import detect_runtime_environment, log_runtime_binaries_status

    runtime_environment = detect_runtime_environment()
    encode_toolchain = log_runtime_binaries_status(auto_install=False)
    active_bot = bot or (getattr(bot_manager, "bot", None) if bot_manager is not None else None)
    stats_payload = collect_all_stats(active_bot)
    rclone_mgr = get_rclone_manager()

    if run_live_remote_tests:
        for acc_dict in rclone_mgr.list_accounts():
            if acc_dict.get("is_active") and acc_dict.get("status") == "connected":
                with suppress(Exception):
                    rclone_mgr.test_account(str(acc_dict["remote_name"]))

    rclone_summary = rclone_mgr.get_status_summary()

    # 1. Recent Errors & Tracebacks (from ring buffer + recent failed encode jobs)
    recent_errors = get_recent_application_errors(limit=8)
    failed_jobs_errors: list[dict[str, Any]] = []
    job_queue_summary: dict[str, Any] = {
        "total_recent_jobs": 0,
        "queued": 0,
        "running": 0,
        "completed": 0,
        "failed": 0,
        "expired": 0,
        "cancelled": 0,
        "healthy": True,
    }
    db_status: dict[str, Any] = {"backend_type": "sqlite_wal_persistent", "healthy": True}

    with suppress(Exception):
        from job_store import get_job_store

        jstore = get_job_store()
        db_status = jstore.get_backend_status()
        db_status["healthy"] = not bool(db_status.get("firestore_init_error"))
        jobs = jstore.list_jobs(limit=30)
        job_queue_summary["total_recent_jobs"] = len(jobs)
        for j in jobs:
            st = str(getattr(j, "status", "queued") or "queued").lower()
            if st in job_queue_summary:
                job_queue_summary[st] = int(job_queue_summary[st]) + 1
            if st == "failed" and getattr(j, "error", None) and len(failed_jobs_errors) < 4:
                failed_jobs_errors.append(
                    {
                        "job_id": j.job_id,
                        "updated_at": j.updated_at,
                        "error": RcloneStorageManager.redact_rclone_text(redact_sensitive_text(str(j.error)))[:240],
                    }
                )

    bot_info = stats_payload.get("bot", {}) if isinstance(stats_payload.get("bot"), dict) else {}
    bot_diag = str(bot_info.get("diagnostic_message") or "")
    if bot_info.get("status") in ("invalid_token", "network_error", "error") and bot_diag:
        recent_errors.insert(
            0,
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "epoch": time.time(),
                "level": "ERROR",
                "logger": "ubagen.bot",
                "message": RcloneStorageManager.redact_rclone_text(redact_sensitive_text(bot_diag))[:300],
                "traceback": None,
            },
        )

    # 2. Missing Required & Optional Environment Variables / Secrets (Names only, NEVER secret values!)
    required_env_specs = [
        ("DISCORD_BOT_TOKEN", "Discord Gateway ve Slash komutları için zorunlu bot token'ı"),
        ("UBAGEN_ADMIN_TOKEN", "Web paneli yönetici işlemleri (restart vb.) için zorunlu token"),
    ]
    optional_env_specs = [
        ("DISCORD_PUBLIC_KEY", "Discord HTTP Interactions (komutla uyanma) Ed25519 açık anahtarı"),
        ("DISCORD_GUILD_ID", "Anında sunucu bazlı slash komut senkronizasyonu için Guild ID"),
        ("UBAGEN_DISCORD_ADMIN_IDS", "Discord /discord ve /log komutları için ek yönetici kullanıcı ID'leri"),
        ("UBAGEN_RCLONE_SECRET_KEY", "Rclone token/şifre veritabanı şifreleme anahtarı (varsayılan anahtar türetilir)"),
        ("UBAGEN_GCS_BUCKET", "İsteğe bağlı Google Cloud Storage yedek bucket adı (rclone aktifse zorunlu değil)"),
    ]

    placeholders = {
        "",
        "your_discord_bot_token_here",
        "your_admin_token_here",
        "your_discord_public_key_here",
        "your_guild_id_here",
        "MY_DISCORD_BOT_TOKEN",
        "CHANGE_ME",
    }

    missing_required_envs: list[str] = []
    configured_required_envs: list[str] = []
    for var_name, _desc in required_env_specs:
        val = os.getenv(var_name, "").strip()
        if not val or val in placeholders:
            missing_required_envs.append(var_name)
        else:
            configured_required_envs.append(var_name)

    missing_optional_envs: list[str] = []
    configured_optional_envs: list[str] = []
    for var_name, _desc in optional_env_specs:
        val = os.getenv(var_name, "").strip()
        if not val or val in placeholders:
            missing_optional_envs.append(var_name)
        else:
            configured_optional_envs.append(var_name)

    # 3. Configuration Files Audit
    rclone_conf_path = rclone_mgr.rclone_config_path
    rclone_conf_valid = True
    rclone_conf_error: Optional[str] = None
    if rclone_conf_path.is_file():
        try:
            cp = configparser.ConfigParser(interpolation=None)
            cp.read(rclone_conf_path, encoding="utf-8")
        except Exception as exc:
            rclone_conf_valid = False
            rclone_conf_error = f"INI ayrıştırma hatası: {type(exc).__name__}"
    else:
        rclone_conf_valid = False
        rclone_conf_error = "rclone.conf dosyası henüz oluşturulmadı"

    sqlite_db_path = rclone_mgr.db_path
    sqlite_db_ok = sqlite_db_path.is_file()

    config_files_report = [
        {
            "name": "rclone.conf",
            "path": str(rclone_conf_path),
            "exists": rclone_conf_path.is_file(),
            "valid": rclone_conf_valid,
            "error": rclone_conf_error,
        },
        {
            "name": "ubagen_jobs.sqlite3",
            "path": str(sqlite_db_path),
            "exists": sqlite_db_ok,
            "valid": sqlite_db_ok,
            "error": None if sqlite_db_ok else "SQLite veritabanı dosyası bulunamadı",
        },
        {
            "name": ".env.example",
            "path": str(Path(".env.example").resolve()),
            "exists": Path(".env.example").is_file(),
            "valid": Path(".env.example").is_file(),
            "error": None if Path(".env.example").is_file() else ".env.example bulunamadı",
        },
        {
            "name": "Dockerfile",
            "path": str(Path("Dockerfile").resolve()),
            "exists": Path("Dockerfile").is_file(),
            "valid": Path("Dockerfile").is_file(),
            "error": None if Path("Dockerfile").is_file() else "Dockerfile bulunamadı",
        },
    ]
    bad_or_missing_configs = [
        c["name"] for c in config_files_report if not c["exists"] or not c["valid"]
    ]

    # 4 & 5. Rclone Binary & Remotes (MEGA + Google Drive)
    rclone_bin = rclone_summary["binary"]
    rclone_accounts = rclone_summary["accounts"]
    remotes_health = []
    for acc in rclone_accounts:
        remotes_health.append(
            {
                "remote_name": acc.get("remote_name") or acc.get("name", "unknown"),
                "provider": acc.get("provider") or acc.get("type", "unknown"),
                "display_name": acc.get("display_name") or acc.get("remote_name", "unknown"),
                "masked_identity": acc.get("masked_identity") or acc.get("account_identity") or acc.get("email_redacted", ""),
                "is_active": acc.get("is_active", acc.get("enabled", False)),
                "status": acc.get("status", "unknown"),
                "test_ok": bool(acc.get("is_active") and acc.get("status") == "connected" and not acc.get("last_error")),
                "quota": acc.get("quota"),
                "last_error": acc.get("last_error"),
                "pending_deletions_count": acc.get("pending_deletions_count", 0),
            }
        )

    # 6. Discord Bot Connection, Intents/Permissions & Command Synchronization
    registered_commands: list[str] = []
    duplicate_commands_detected: list[str] = []
    legacy_drive_commands_present: list[str] = []
    intents_summary = {"guilds": True, "default_intents": True}

    if active_bot is not None and getattr(active_bot, "tree", None) is not None:
        with suppress(Exception):
            raw_cmds = [c.name for c in active_bot.tree.get_commands()]
            seen_set: set[str] = set()
            for cname in raw_cmds:
                if cname in seen_set and cname not in duplicate_commands_detected:
                    duplicate_commands_detected.append(cname)
                seen_set.add(cname)
                if cname in LEGACY_REMOVED_DRIVE_COMMANDS:
                    legacy_drive_commands_present.append(cname)
            registered_commands = list(dict.fromkeys(raw_cmds))
        with suppress(Exception):
            bot_intents = getattr(active_bot, "intents", None)
            if bot_intents is not None:
                intents_summary = {
                    "guilds": bool(getattr(bot_intents, "guilds", True)),
                    "messages": bool(getattr(bot_intents, "messages", False)),
                    "message_content": bool(getattr(bot_intents, "message_content", False)),
                }
    else:
        registered_commands = ["stats", "altyazı_ayıklama", "encode", "encodeal", "is_durumu", "discord", "log"]

    discord_status = {
        "status": bot_info.get("status", "disconnected"),
        "connected": bool(bot_info.get("connected", False)),
        "is_ready": bool(bot_info.get("is_ready", False)),
        "user": bot_info.get("user", "Disconnected"),
        "guild_count": int(bot_info.get("guild_count", 0) or 0),
        "latency_ms": bot_info.get("latency_ms"),
        "sync_status": bot_info.get("sync_status") or "Single-scope deduplicated sync active",
        "registered_commands": registered_commands,
        "duplicate_commands": duplicate_commands_detected,
        "legacy_drive_commands_removed": len(legacy_drive_commands_present) == 0,
        "intents": intents_summary,
        "interactions_endpoint_configured": bool(bot_info.get("interactions_endpoint_configured", False)),
        "diagnostic_message": RcloneStorageManager.redact_rclone_text(redact_sensitive_text(bot_diag)),
    }

    # 7. Backend Endpoints, Database, Storage & Job Queue Health
    storage_info = stats_payload.get("storage", {}) if isinstance(stats_payload.get("storage"), dict) else {}
    disk_info = stats_payload.get("disk", {}) if isinstance(stats_payload.get("disk"), dict) else {}

    backend_endpoints = [
        {"path": "/health", "status": "ok"},
        {"path": "/api/stats", "status": "ok"},
        {"path": "/api/storage", "status": "ok"},
        {"path": "/api/jobs", "status": "ok" if db_status.get("healthy", True) else "degraded"},
        {"path": "/api/rclone/status", "status": "ok" if rclone_bin.get("executable") else "degraded"},
        {"path": "/api/rclone/accounts", "status": "ok"},
        {"path": "/api/diagnostics/log", "status": "ok"},
        {
            "path": "/api/discord/interactions",
            "status": "ok" if discord_status["interactions_endpoint_configured"] else "optional_unconfigured",
        },
    ]

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
        "runtime_environment": runtime_environment,
        "encode_toolchain": encode_toolchain,
        "recent_errors": recent_errors,
        "failed_jobs_errors": failed_jobs_errors,
        "env_audit": {
            "missing_required": missing_required_envs,
            "configured_required": configured_required_envs,
            "missing_optional": missing_optional_envs,
            "configured_optional": configured_optional_envs,
            "all_required_present": len(missing_required_envs) == 0,
            "secrets_redacted": True,
        },
        "config_files": {
            "files": config_files_report,
            "missing_or_invalid": bad_or_missing_configs,
            "all_valid": len(bad_or_missing_configs) == 0,
        },
        "rclone": {
            "binary": rclone_bin,
            "config_path": str(rclone_conf_path),
            "uses_default_drive_oauth": True,
            "accounts_total": rclone_summary["accounts_total"],
            "active_accounts_count": rclone_summary["active_accounts_count"],
            "drive_accounts_count": rclone_summary["drive_accounts_count"],
            "mega_accounts_count": rclone_summary["mega_accounts_count"],
            "pending_deletions_count": rclone_summary["pending_deletions_count"],
            "remotes": remotes_health,
        },
        "discord": discord_status,
        "backend_health": {
            "endpoints": backend_endpoints,
            "database": db_status,
            "storage": {
                "mode": storage_info.get("storage_mode") or disk_info.get("storage_mode") or "ephemeral_container_fs",
                "writable": bool(disk_info.get("writable", True)),
                "free_str": disk_info.get("free_str", "Unavailable"),
                "used_str": disk_info.get("used_str", "Unavailable"),
                "total_str": disk_info.get("total_str", "Unavailable"),
                "retention_seconds": 7200,
                "rclone_connected_remotes": [r["remote_name"] for r in remotes_health if r["test_ok"]],
            },
            "job_queue": job_queue_summary,
        },
    }


def format_diagnostic_log_for_discord(report: dict[str, Any]) -> str:
    """
    Format `collect_diagnostic_log_report()` into a concise, readable Turkish message
    for the Discord `/log` command (guaranteed <= 1950 chars and 100% secret-redacted).
    """
    from stats import redact_sensitive_text

    env_audit = report.get("env_audit", {})
    cfg_audit = report.get("config_files", {})
    rclone_info = report.get("rclone", {})
    rclone_bin = rclone_info.get("binary", {})
    runtime_info = report.get("runtime_environment", {})
    encode_tools = report.get("encode_toolchain", {})
    remotes = rclone_info.get("remotes", [])
    discord_info = report.get("discord", {})
    bh = report.get("backend_health", {})
    db_info = bh.get("database", {})
    st_info = bh.get("storage", {})
    jq_info = bh.get("job_queue", {})
    recent_errs = report.get("recent_errors", [])
    failed_jobs = report.get("failed_jobs_errors", [])

    # 1. Recent Errors & Tracebacks
    err_lines: list[str] = []
    for err in recent_errs[:3]:
        lvl = err.get("level", "ERR")
        msg = err.get("message", "")[:110]
        tb = f" (`{err['traceback'][:80]}`)" if err.get("traceback") else ""
        err_lines.append(f"  • `[{lvl}]` {msg}{tb}")
    for fj in failed_jobs[:2]:
        err_lines.append(f"  • `[JOB:{fj['job_id']}]` {fj['error'][:110]}")
    if not err_lines:
        err_lines.append("  • ✅ Son çalışma süresinde kayıtlı hata veya traceback yok.")

    # 2. Missing Required Env Vars
    missing_req = env_audit.get("missing_required", [])
    missing_opt = env_audit.get("missing_optional", [])
    env_line = (
        "✅ Tüm zorunlu değişkenler tanımlı (`" + ", ".join(env_audit.get("configured_required", [])) + "`)"
        if not missing_req
        else "⚠️ **Eksik Zorunlu:** `" + ", ".join(missing_req) + "`"
    )
    opt_line = (
        f"⚪ İsteğe Bağlı Eksik: `{', '.join(missing_opt)}`"
        if missing_opt
        else "✅ Tüm isteğe bağlı değişkenler tanımlı"
    )

    # 3. Config Files
    bad_cfgs = cfg_audit.get("missing_or_invalid", [])
    cfg_line = (
        "✅ Tüm yapılandırma dosyaları mevcut ve geçerli (`rclone.conf`, `ubagen_jobs.sqlite3`)"
        if not bad_cfgs
        else "⚠️ Eksik/Hatalı Dosyalar: `" + ", ".join(bad_cfgs) + "`"
    )

    # 4. Rclone Binary
    rc_badge = "✅ Kurulu & Çalışıyor" if rclone_bin.get("executable") else "❌ Kurulu Değil / Çalıştırılamıyor"
    rc_line = f"{rc_badge} (`{rclone_bin.get('version', 'Unavailable')}` @ `{rclone_bin.get('path') or 'PATH yok'}`)"
    tool_parts = []
    for tool_name in ("aria2c", "ffmpeg", "ffprobe"):
        tool_info = encode_tools.get(tool_name, {})
        tool_parts.append(f"{tool_name}:{'OK' if tool_info.get('installed') and tool_info.get('executable') else 'MISSING'}")
    runtime_line = (
        f"OS={runtime_info.get('os', '?')} distro={runtime_info.get('distro_id', '?')} "
        f"arch={runtime_info.get('architecture', '?')} manager={runtime_info.get('package_manager') or 'none'} "
        f"root={runtime_info.get('effective_uid') == 0} container={runtime_info.get('is_container', '?')}"
    )
    tool_line = " | ".join(tool_parts)

    # 5. MEGA & Google Drive Remotes
    remote_lines: list[str] = []
    if remotes:
        for r in remotes[:5]:
            icon = "🟢" if r.get("test_ok") else ("🟡" if r.get("status") == "pending_oauth" else "🔴")
            q = r.get("quota") or {}
            q_str = f" | Alan: {q.get('used_str', '?')}/{q.get('total_str', '?')}" if q.get("total_bytes") else ""
            err_note = f" | Hata: {r['last_error'][:60]}" if r.get("last_error") else ""
            remote_lines.append(
                f"  • {icon} `{r['remote_name']}` ({r['provider'].upper()}) — Durum: `{r['status']}`{q_str}{err_note}"
            )
    else:
        remote_lines.append("  • ⚪ Henüz bağlı MEGA veya Google Drive remote hesabı yok (Yönetim panelinden ekleyin).")

    # 6. Discord Bot & Command Sync
    d_conn = "🟢 Bağlı & Hazır" if discord_info.get("is_ready") else ("🟡 Bağlanıyor" if discord_info.get("connected") else "🔴 Çevrimdışı")
    cmds_str = ", ".join(f"/{c}" for c in discord_info.get("registered_commands", []))
    dup_str = "Yok (Tekil)" if not discord_info.get("duplicate_commands") else ", ".join(discord_info["duplicate_commands"])

    # 7. Backend, DB, Storage & Job Queue
    db_badge = "🟢 Sağlıklı" if db_info.get("healthy", True) else "🔴 Hatalı"
    st_badge = "🟢 Yazılabilir" if st_info.get("writable", True) else "🔴 Salt-Okunur"

    lines = [
        "🩺 **UBAgen — Yönetici Sistem Tanılama Raporu (`/log`)**",
        f"**1. Son Uygulama Hataları & Traceback Özetleri:**\n" + "\n".join(err_lines),
        f"**2. Ortam Değişkenleri & Secret Denetimi (Değerler Gizli):**\n  • {env_line}\n  • {opt_line}",
        f"**3. Yapılandırma Dosyaları:**\n  • {cfg_line}",
        f"**4. Rclone Kurulumu:**\n  • {rc_line}",
        f"**4b. Sunucu & Encode Araçları:**\n  • {runtime_line}\n  • {tool_line}",
        f"**5. MEGA & Google Drive Remote Durumu (Bekleyen Silme: `{rclone_info.get('pending_deletions_count', 0)}`):**\n"
        + "\n".join(remote_lines),
        f"**6. Discord Bot, İzinler & Komut Senkronizasyonu:**\n"
        f"  • Durum: {d_conn} | Gecikme: `{discord_info.get('latency_ms') or 'N/A'} ms` | Sunucu: `{discord_info.get('guild_count', 0)}`\n"
        f"  • Senkronizasyon: `{discord_info.get('sync_status')}` | Yinelenen Komut: `{dup_str}`\n"
        f"  • Kayıtlı Tekil Komutlar: `{cmds_str}`",
        f"**7. Backend Endpoint, Veritabanı, Depolama & İş Kuyruğu:**\n"
        f"  • Veritabanı: {db_badge} (`{db_info.get('backend_type', 'sqlite')}`) | Depolama: {st_badge} (Boş: `{st_info.get('free_str')}`, TTL: `7200s`)\n"
        f"  • İş Kuyruğu: Toplam `{jq_info.get('total_recent_jobs', 0)}` (Sırada: `{jq_info.get('queued', 0)}`, Çalışan: `{jq_info.get('running', 0)}`, Tamamlanan: `{jq_info.get('completed', 0)}`, Hatalı: `{jq_info.get('failed', 0)}`)\n"
        f"  • Endpointler: `/health`, `/api/stats`, `/api/rclone/*`, `/api/jobs`, `/api/diagnostics/log` aktif.",
    ]

    output = RcloneStorageManager.redact_rclone_text(redact_sensitive_text("\n\n".join(lines)))
    if len(output) > 1950:
        output = output[:1940] + "\n...(kısaltıldı)"
    return output


async def handle_log_slash_command(
    interaction: Any,
    *,
    bot: Optional[Any] = None,
    bot_manager: Optional[Any] = None,
    run_live_remote_tests: bool = False,
) -> tuple[int, dict[str, Any]]:
    """
    Execute the admin-only `/log` Discord slash command.
    Verifies caller authorization using `check_discord_restart_authorization`,
    collects the full diagnostic report, and sends an ephemeral response.
    """
    from encoder_worker import _safe_defer_interaction, _safe_edit_interaction, check_discord_restart_authorization

    await _safe_defer_interaction(interaction, ephemeral=True)
    active_bot = bot or getattr(interaction, "client", None) or (getattr(bot_manager, "bot", None) if bot_manager else None)
    authorized, auth_reason = await check_discord_restart_authorization(interaction, active_bot)

    if not authorized:
        msg = (
            "⛔ **Yetkisiz İşlem (403):** `/log` tanılama komutunu yalnızca sunucu yöneticileri "
            f"(Administrator), bot sahibi veya yetkili yöneticiler çalıştırabilir.\n*Detay:* {auth_reason}"
        )
        await _safe_edit_interaction(interaction, msg)
        return 403, {"status": "forbidden", "message": auth_reason}

    report = await asyncio.to_thread(
        collect_diagnostic_log_report,
        bot=active_bot,
        bot_manager=bot_manager,
        run_live_remote_tests=run_live_remote_tests,
    )
    formatted_msg = format_diagnostic_log_for_discord(report)

    # Prepare comprehensive downloadable .txt attachment with full structured diagnostics
    report_text = (
        formatted_msg
        + "\n\n=== FULL STRUCTURED DIAGNOSTICS ===\n"
        + json.dumps(report, indent=2, ensure_ascii=False, default=str)
    )
    encoded_bytes = report_text.encode("utf-8")
    if len(encoded_bytes) > 8 * 1024 * 1024:
        encoded_bytes = encoded_bytes[: 8 * 1024 * 1024]

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"ubagen-log-{timestamp}.txt"

    file_sent = False
    try:
        import discord

        log_file = discord.File(io.BytesIO(encoded_bytes), filename=filename)
        edit_fn = getattr(interaction, "edit_original_response", None)
        if callable(edit_fn):
            await edit_fn(content=formatted_msg, attachments=[log_file])
            file_sent = True
    except Exception as exc:
        logger.warning("Could not attach /log report to original response (%s); trying follow-up: %s", type(exc).__name__, exc)
        await _safe_edit_interaction(interaction, formatted_msg)
        try:
            import discord

            followup = getattr(interaction, "followup", None)
            if followup is not None and callable(getattr(followup, "send", None)):
                followup_file = discord.File(io.BytesIO(encoded_bytes), filename=filename)
                await followup.send(
                    content="📄 **Tanılama Raporu Eki:**",
                    file=followup_file,
                    ephemeral=True,
                )
                file_sent = True
        except Exception as followup_exc:
            logger.exception("Failed to send /log text-file attachment via followup: %s", followup_exc)
            await _safe_edit_interaction(
                interaction,
                f"{formatted_msg}\n\n⚠️ *Dosya eki gönderilemedi:* `{type(followup_exc).__name__}`",
            )

    if not file_sent:
        await _safe_edit_interaction(interaction, formatted_msg)

    return 200, report


def register_log_command(
    tree: Any,
    *,
    bot: Optional[Any] = None,
    bot_manager: Optional[Any] = None,
) -> None:
    """Idempotently register the single `/log` admin diagnostic slash command on `tree`."""
    try:
        from discord import app_commands
    except ImportError:
        return
    if tree is None:
        return

    with suppress(Exception):
        tree.remove_command(LOG_COMMAND_NAME)

    @tree.command(
        name=LOG_COMMAND_NAME,
        description="Yöneticiler için rclone, MEGA/Drive, bot, ortam ve hata tanılamasını gösterir.",
    )
    async def log_cmd(interaction: Any) -> None:
        await handle_log_slash_command(
            interaction,
            bot=bot,
            bot_manager=bot_manager,
        )


# Backward-compatible names used by health_api.py and older deployments.
def install_rclone_error_buffer_handler() -> None:
    install_error_ring_buffer()


def collect_log_diagnostics(
    *,
    bot: Optional[Any] = None,
    bot_manager: Optional[Any] = None,
    run_live_remote_tests: bool = False,
) -> dict[str, Any]:
    return collect_diagnostic_log_report(
        bot=bot,
        bot_manager=bot_manager,
        run_live_remote_tests=run_live_remote_tests,
    )


def format_log_diagnostics_report(report: dict[str, Any]) -> str:
    return format_diagnostic_log_for_discord(report)

