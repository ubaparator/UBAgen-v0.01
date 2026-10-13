"""
UBAgen — discord_interactions.py
Discord HTTP Interactions Endpoint (Ed25519 Signature Verification, PING/PONG,
Wake-on-Command Orchestration, and Asynchronous Slash Command Execution).

Why this module exists:
- When the Discord Gateway WebSocket disconnects or when a Cloud Run Service scales down / throttles
  background CPU between HTTP requests, Discord cannot deliver slash commands over a dead WebSocket.
- By configuring Discord's "Interactions Endpoint URL" (`POST /api/discord/interactions`), Discord
  delivers slash commands as signed HTTPS POST requests directly to the web service.
- Each incoming request:
  1. Verifies Discord's `X-Signature-Ed25519` and `X-Signature-Timestamp` headers against
     `DISCORD_PUBLIC_KEY` using Ed25519 (PyNaCl / cryptography / pure-Python RFC 8032 fallback).
  2. Responds to `PING` (`type=1`) with `PONG` (`{"type": 1}`) within milliseconds.
  3. On `APPLICATION_COMMAND` (`type=2`), triggers `BotLifecycleManager.ensure_bot_awake()` so a
     disconnected Gateway bot worker wakes up and reconnects (with single-flight locking, cooldown,
     and controlled retry), while immediately acknowledging the interaction (`type=5` deferred callback)
     within Discord's 3-second deadline and executing the command via Discord's Interaction Webhook REST API.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import json
import logging
import os
import time
from typing import Any, Optional
import urllib.error
import urllib.parse
import urllib.request

from stats import redact_sensitive_text

logger = logging.getLogger("ubagen.interactions")

# Optional C-accelerated Ed25519 libraries (used first when available)
try:
    from nacl.signing import VerifyKey as _NaclVerifyKey
except ImportError:
    _NaclVerifyKey = None

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey as _CryptoEd25519PublicKey,
    )
except ImportError:
    _CryptoEd25519PublicKey = None

# Discord Interaction Types
INTERACTION_TYPE_PING = 1
INTERACTION_TYPE_APPLICATION_COMMAND = 2

# Discord Interaction Response Types
CALLBACK_TYPE_PONG = 1
CALLBACK_TYPE_CHANNEL_MESSAGE_WITH_SOURCE = 4
CALLBACK_TYPE_DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE = 5

INTERACTION_RESPONSE_PONG = CALLBACK_TYPE_PONG
INTERACTION_RESPONSE_CHANNEL_MESSAGE_WITH_SOURCE = CALLBACK_TYPE_CHANNEL_MESSAGE_WITH_SOURCE
INTERACTION_RESPONSE_DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE = CALLBACK_TYPE_DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE

# Ephemeral message flag (1 << 6)
DISCORD_FLAG_EPHEMERAL = 64

# Default replay-protection window for X-Signature-Timestamp (seconds)
DEFAULT_MAX_TIMESTAMP_SKEW_SECONDS = 300.0


# ------------------------------------------------------------------------------
# Pure-Python RFC 8032 Ed25519 Implementation (Zero-Dependency Fallback & Test Signer)
# ------------------------------------------------------------------------------
_Q = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493


def _inv(x: int) -> int:
    return pow(x, _Q - 2, _Q)


_D = (-121665 * _inv(121666)) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(_D * y * y + 1) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = (x * _I) % _Q
    if x % 2 != 0:
        x = _Q - x
    return x


_BY = (4 * _inv(5)) % _Q
_BX = _xrecover(_BY)
_B = (_BX % _Q, _BY % _Q, 1, (_BX * _BY) % _Q)
_IDENTITY = (0, 1, 1, 0)


def _edwards_add(
    p1: tuple[int, int, int, int],
    p2: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    x1, y1, z1, t1 = p1
    x2, y2, z2, t2 = p2
    a = ((y1 - x1) * (y2 - x2)) % _Q
    b = ((y1 + x1) * (y2 + x2)) % _Q
    c = (t1 * 2 * _D * t2) % _Q
    d = (z1 * 2 * z2) % _Q
    e = (b - a) % _Q
    f = (d - c) % _Q
    g = (d + c) % _Q
    h = (b + a) % _Q
    return (e * f) % _Q, (g * h) % _Q, (f * g) % _Q, (e * h) % _Q


def _edwards_double(p: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x1, y1, z1, _ = p
    a = (x1 * x1) % _Q
    b = (y1 * y1) % _Q
    c = (2 * z1 * z1) % _Q
    h = (a + b) % _Q
    e = (h - ((x1 + y1) * (x1 + y1) % _Q)) % _Q
    g = (a - b) % _Q
    f = (c + g) % _Q
    return (e * f) % _Q, (g * h) % _Q, (f * g) % _Q, (e * h) % _Q


def _scalarmult(p: tuple[int, int, int, int], e: int) -> tuple[int, int, int, int]:
    res = _IDENTITY
    cur = p
    scalar = e
    while scalar > 0:
        if scalar & 1:
            res = _edwards_add(res, cur)
        cur = _edwards_double(cur)
        scalar >>= 1
    return res


def _encode_point(p: tuple[int, int, int, int]) -> bytes:
    x, y, z, _ = p
    zi = _inv(z)
    xp = (x * zi) % _Q
    yp = (y * zi) % _Q
    val = yp | ((xp & 1) << 255)
    return val.to_bytes(32, "little")


def _decode_point(b: bytes) -> tuple[int, int, int, int]:
    if len(b) != 32:
        raise ValueError("Invalid Ed25519 point length")
    unpacked = int.from_bytes(b, "little")
    sign = (unpacked >> 255) & 1
    y = unpacked & ((1 << 255) - 1)
    if y >= _Q:
        raise ValueError("Ed25519 y-coordinate out of range")
    u = (y * y - 1) % _Q
    v = (_D * y * y + 1) % _Q
    if v == 0:
        raise ValueError("Invalid Ed25519 denominator")
    xx = (u * _inv(v)) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = (x * _I) % _Q
    if (x * x - xx) % _Q != 0:
        raise ValueError("Point is not on Ed25519 curve")
    if x == 0 and sign == 1:
        raise ValueError("Invalid negative zero x-coordinate")
    if (x & 1) != sign:
        x = _Q - x
    return x, y, 1, (x * y) % _Q


def _points_equal(
    p1: tuple[int, int, int, int],
    p2: tuple[int, int, int, int],
) -> bool:
    x1, y1, z1, _ = p1
    x2, y2, z2, _ = p2
    if (x1 * z2 - x2 * z1) % _Q != 0:
        return False
    if (y1 * z2 - y2 * z1) % _Q != 0:
        return False
    return True


def _ed25519_verify_rfc8032(public_key_bytes: bytes, message: bytes, signature_bytes: bytes) -> bool:
    """RFC 8032 Section 5.1.7 Ed25519 signature verification."""
    if len(public_key_bytes) != 32 or len(signature_bytes) != 64:
        return False
    r_bytes = signature_bytes[:32]
    s_bytes = signature_bytes[32:]
    s_int = int.from_bytes(s_bytes, "little")
    if s_int < 0 or s_int >= _L:
        return False
    try:
        a_point = _decode_point(public_key_bytes)
        r_point = _decode_point(r_bytes)
    except ValueError:
        return False

    h = hashlib.sha512(r_bytes + public_key_bytes + message).digest()
    k_int = int.from_bytes(h, "little") % _L

    lhs = _scalarmult(_B, s_int)
    rhs = _edwards_add(r_point, _scalarmult(a_point, k_int))
    return _points_equal(lhs, rhs)


def generate_ed25519_test_keypair_and_sign(
    message: bytes,
    seed_32_bytes: Optional[bytes] = None,
) -> tuple[str, str]:
    """
    Deterministic RFC 8032 Ed25519 keypair derivation and signing helper for unit tests.
    Returns `(public_key_hex, signature_hex)`.
    """
    seed = seed_32_bytes if (seed_32_bytes and len(seed_32_bytes) == 32) else hashlib.sha256(b"ubagen-ed25519-test-seed").digest()
    digest = hashlib.sha512(seed).digest()
    a_bytes = bytearray(digest[:32])
    a_bytes[0] &= 248
    a_bytes[31] &= 127
    a_bytes[31] |= 64
    a_scalar = int.from_bytes(a_bytes, "little")
    prefix = digest[32:]

    pub_point = _scalarmult(_B, a_scalar)
    pub_bytes = _encode_point(pub_point)

    r_scalar = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % _L
    r_point = _scalarmult(_B, r_scalar)
    r_bytes = _encode_point(r_point)

    k_scalar = int.from_bytes(hashlib.sha512(r_bytes + pub_bytes + message).digest(), "little") % _L
    s_scalar = (r_scalar + k_scalar * a_scalar) % _L
    sig_bytes = r_bytes + s_scalar.to_bytes(32, "little")

    return pub_bytes.hex(), sig_bytes.hex()


# ------------------------------------------------------------------------------
# Discord Ed25519 Request Signature Verification
# ------------------------------------------------------------------------------
def get_configured_discord_public_key() -> str:
    """Return the configured Discord application Ed25519 public key (hex) without logging secrets."""
    for env_key in ("DISCORD_PUBLIC_KEY", "DISCORD_APPLICATION_PUBLIC_KEY"):
        val = os.getenv(env_key, "").strip()
        if val and val not in ("your_discord_public_key_here", "CHANGE_ME"):
            return val
    return ""


def verify_discord_ed25519_signature(
    *,
    public_key_hex: str,
    signature_hex: Optional[str],
    timestamp: Optional[str] = None,
    timestamp_header: Optional[str] = None,
    raw_body: bytes = b"",
    max_timestamp_skew_seconds: Optional[float] = None,
) -> tuple[bool, str]:
    """
    Verify Discord's `X-Signature-Ed25519` and `X-Signature-Timestamp` headers against `raw_body`.
    Returns `(is_valid, diagnostic_reason)`.
    """
    clean_pub = (public_key_hex or "").strip()
    if not clean_pub:
        return False, "DISCORD_PUBLIC_KEY yapılandırılmamış."

    clean_sig = (signature_hex or "").strip()
    effective_ts = timestamp if timestamp is not None else timestamp_header
    clean_ts = (effective_ts or "").strip()
    if not clean_sig or not clean_ts:
        return False, "Eksik imza başlığı (X-Signature-Ed25519 veya X-Signature-Timestamp bulunamadı)."

    try:
        pub_bytes = bytes.fromhex(clean_pub)
        sig_bytes = bytes.fromhex(clean_sig)
    except ValueError:
        return False, "Geçersiz onaltılık (hex) imza veya açık anahtar formatı."

    if len(pub_bytes) != 32 or len(sig_bytes) != 64:
        return False, "Geçersiz Ed25519 anahtar (32 bayt) veya imza (64 bayt) uzunluğu."

    # Optional replay protection when timestamp is a numeric epoch second value
    skew_limit = max_timestamp_skew_seconds
    if skew_limit is None:
        raw_skew_env = os.getenv("UBAGEN_DISCORD_MAX_SIGNATURE_SKEW_SECONDS", "").strip()
        if raw_skew_env:
            with suppress(ValueError):
                skew_limit = float(raw_skew_env)
        elif os.getenv("UBAGEN_SKIP_TIMESTAMP_SKEW_CHECK", "").strip().lower() not in ("1", "true", "yes"):
            skew_limit = DEFAULT_MAX_TIMESTAMP_SKEW_SECONDS

    if skew_limit is not None and skew_limit > 0 and clean_ts.isdigit():
        try:
            ts_val = float(clean_ts)
            # Only enforce skew if timestamp looks like a realistic current-era Unix epoch (> 2020-01-01)
            # while allowing synthetic Discord portal test timestamps when UBAGEN_ALLOW_SYNTHETIC_DISCORD_TS is set.
            if ts_val > 1_577_836_800 and os.getenv("UBAGEN_ALLOW_SYNTHETIC_DISCORD_TS", "").strip().lower() not in ("1", "true", "yes"):
                if abs(time.time() - ts_val) > skew_limit:
                    return False, "İmza zaman damgası (X-Signature-Timestamp) süresi geçmiş."
        except ValueError:
            pass

    signed_message = clean_ts.encode("utf-8") + (raw_body or b"")

    # 1. Prefer PyNaCl if installed
    if _NaclVerifyKey is not None:
        try:
            verify_key = _NaclVerifyKey(pub_bytes)
            verify_key.verify(signed_message, sig_bytes)
            return True, "verified_pynacl"
        except Exception:
            return False, "Ed25519 imza doğrulaması başarısız (PyNaCl)."

    # 2. Prefer cryptography if installed
    if _CryptoEd25519PublicKey is not None:
        try:
            pub_obj = _CryptoEd25519PublicKey.from_public_bytes(pub_bytes)
            pub_obj.verify(sig_bytes, signed_message)
            return True, "verified_cryptography"
        except Exception:
            return False, "Ed25519 imza doğrulaması başarısız (cryptography)."

    # 3. Pure-Python RFC 8032 Ed25519 verification
    if _ed25519_verify_rfc8032(pub_bytes, signed_message, sig_bytes):
        return True, "verified_rfc8032"
    return False, "Ed25519 imza doğrulaması başarısız."


# ------------------------------------------------------------------------------
# HTTP Interaction Adapter (Bridges Discord Webhook Callbacks to Shared Command Handlers)
# ------------------------------------------------------------------------------
class _HttpPermissions:
    def __init__(self, raw_permissions: Any) -> None:
        perm_int = 0
        if raw_permissions is not None:
            with suppress(ValueError, TypeError):
                perm_int = int(str(raw_permissions).strip())
        self.value = perm_int
        # Discord ADMINISTRATOR permission bit is 1 << 3 (0x8)
        self.administrator = bool((perm_int & 0x8) == 0x8)


class _HttpUser:
    def __init__(self, user_data: dict[str, Any], raw_permissions: Any = None) -> None:
        self.id = str(user_data.get("id") or "").strip()
        self.name = str(user_data.get("username") or user_data.get("global_name") or "discord_user").strip()
        self.guild_permissions = _HttpPermissions(raw_permissions)

    def __str__(self) -> str:
        return self.name


class _HttpAttachment:
    def __init__(self, att_data: dict[str, Any]) -> None:
        self.id = str(att_data.get("id") or "")
        self.filename = str(att_data.get("filename") or "")
        self.size = att_data.get("size")
        self.content_type = att_data.get("content_type")
        self.url = str(att_data.get("url") or att_data.get("proxy_url") or "")


class _HttpResponseProxy:
    """Represents an interaction response that has already been deferred by the HTTP 200 callback."""

    def __init__(self) -> None:
        self._done = True

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **kwargs: Any) -> None:
        self._done = True


class _HttpFollowupProxy:
    def __init__(self, adapter: HttpDiscordInteractionAdapter) -> None:
        self._adapter = adapter

    async def send(self, content: Optional[str] = None, **kwargs: Any) -> bool:
        embed = kwargs.get("embed")
        return await self._adapter.edit_original_response(content=content, embed=embed)


class HttpDiscordInteractionAdapter:
    """
    Adapts an incoming Discord HTTP Interaction JSON payload (`APPLICATION_COMMAND`)
    to the interface expected by UBAgen's slash command handlers (`submit_encode_job_from_interaction`,
    `handle_job_status_slash_command`, `handle_discord_restart_command`, etc.).
    Updates `@original` via Discord's Interaction Webhook REST API without requiring a live Gateway socket.
    """

    def __init__(
        self,
        payload: dict[str, Any],
        *,
        bot: Optional[Any] = None,
        webhook_sender: Optional[Any] = None,
    ) -> None:
        self.raw_payload = payload
        self.client = bot
        self.application_id = str(
            payload.get("application_id")
            or os.getenv("DISCORD_APPLICATION_ID", "").strip()
            or os.getenv("DISCORD_CLIENT_ID", "").strip()
        )
        self.token = str(payload.get("token") or "").strip()
        self.guild_id = str(payload.get("guild_id") or "").strip() or None
        self.channel_id = str(payload.get("channel_id") or "").strip() or None

        member = payload.get("member") if isinstance(payload.get("member"), dict) else {}
        user_dict = (
            member.get("user")
            if isinstance(member.get("user"), dict)
            else (payload.get("user") if isinstance(payload.get("user"), dict) else {})
        )
        raw_perms = member.get("permissions")
        self.user = _HttpUser(user_dict, raw_permissions=raw_perms)
        self.response = _HttpResponseProxy()
        self.followup = _HttpFollowupProxy(self)
        self._webhook_sender = webhook_sender
        self.edited_messages: list[dict[str, Any]] = []

    def _patch_original_sync(self, body_dict: dict[str, Any]) -> bool:
        self.edited_messages.append(body_dict)
        if callable(self._webhook_sender):
            return bool(self._webhook_sender(self.application_id, self.token, body_dict))

        if not self.application_id or not self.token:
            return False

        url = (
            f"https://discord.com/api/v10/webhooks/"
            f"{urllib.parse.quote(self.application_id)}/"
            f"{urllib.parse.quote(self.token)}/messages/@original"
        )
        raw_data = json.dumps(body_dict).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=raw_data,
            method="PATCH",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "UBAgen-HttpInteraction/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return 200 <= resp.status < 300
        except Exception as exc:
            logger.debug(
                "HTTP interaction webhook PATCH failed (%s): %s",
                type(exc).__name__,
                redact_sensitive_text(str(exc)),
            )
            return False

    async def edit_original_response(
        self,
        *,
        content: Optional[str] = None,
        embed: Optional[Any] = None,
        **kwargs: Any,
    ) -> bool:
        body: dict[str, Any] = {}
        if content is not None:
            body["content"] = str(content)
        if embed is not None:
            if hasattr(embed, "to_dict") and callable(embed.to_dict):
                body["embeds"] = [embed.to_dict()]
            elif isinstance(embed, dict):
                body["embeds"] = [embed]
        if not body:
            body["content"] = "✅ İşlem tamamlandı."
        return await asyncio.to_thread(self._patch_original_sync, body)


def _extract_command_options(data_obj: dict[str, Any]) -> tuple[dict[str, Any], dict[str, _HttpAttachment]]:
    """Extract slash command options and resolved attachments from Discord interaction `data`."""
    options_map: dict[str, Any] = {}
    attachments_map: dict[str, _HttpAttachment] = {}

    resolved = data_obj.get("resolved") if isinstance(data_obj.get("resolved"), dict) else {}
    raw_attachments = resolved.get("attachments") if isinstance(resolved.get("attachments"), dict) else {}
    for att_id, att_info in raw_attachments.items():
        if isinstance(att_info, dict):
            attachments_map[str(att_id)] = _HttpAttachment(att_info)

    raw_options = data_obj.get("options") if isinstance(data_obj.get("options"), list) else []
    for opt in raw_options:
        if not isinstance(opt, dict):
            continue
        name = str(opt.get("name") or "").strip()
        if not name:
            continue
        opt_type = opt.get("type")
        val = opt.get("value")
        # Option type 11 is ATTACHMENT in Discord Application Commands
        if opt_type == 11 and val is not None and str(val) in attachments_map:
            options_map[name] = attachments_map[str(val)]
        else:
            options_map[name] = val

    return options_map, attachments_map


async def execute_http_command_background(
    payload: dict[str, Any],
    *,
    bot: Optional[Any] = None,
    bot_manager: Optional[Any] = None,
    webhook_sender: Optional[Any] = None,
) -> dict[str, Any]:
    """
    Execute a deferred Discord slash command received via HTTP Interaction Endpoint,
    while waking up the Discord Gateway bot worker if it was disconnected.
    """
    from encoder_worker import (
        DISCORD_RESTART_COMMAND_NAME,
        ENCODE_COMMAND_NAME,
        ENCODEAL_COMMAND_NAME,
        JOB_STATUS_COMMAND_NAME,
        handle_discord_restart_command,
        handle_encode_slash_command,
        handle_encodeal_slash_command,
        handle_job_status_slash_command,
    )
    from stats import build_stats_embed, collect_all_stats, format_compact_stats_message

    data_obj = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    cmd_name = str(data_obj.get("name") or "").strip().lower()
    options, attachments = _extract_command_options(data_obj)

    active_bot = bot or (getattr(bot_manager, "bot", None) if bot_manager is not None else None)
    adapter = HttpDiscordInteractionAdapter(
        payload,
        bot=active_bot,
        webhook_sender=webhook_sender,
    )

    # 1. Trigger Wake-on-Command if bot_manager is available and command is not itself `/discord` restart
    wake_result: Optional[dict[str, Any]] = None
    if bot_manager is not None and hasattr(bot_manager, "ensure_bot_awake") and cmd_name != DISCORD_RESTART_COMMAND_NAME:
        try:
            _, wake_result = await bot_manager.ensure_bot_awake(reason=f"discord_command:{cmd_name or 'unknown'}")
        except Exception as exc:
            logger.warning("Wake-on-command trigger warning (%s): %s", type(exc).__name__, redact_sensitive_text(str(exc)))

    # 2. Route to the existing shared command handlers
    try:
        if cmd_name == ENCODE_COMMAND_NAME:
            job = await handle_encode_slash_command(
                adapter,
                magnet=str(options.get("magnet") or ""),
                codec=str(options.get("codec") or "h264"),
                dil=str(options.get("dil") or "tur"),
                altyazi_url=str(options["altyazi_url"]) if options.get("altyazi_url") is not None else None,
                intro_url=str(options["intro_url"]) if options.get("intro_url") is not None else None,
            )
            return {"command": cmd_name, "status": "dispatched", "job_id": job.job_id if job else None, "wake": wake_result}

        if cmd_name == ENCODEAL_COMMAND_NAME:
            altyazi_att = options.get("altyazi")
            if not isinstance(altyazi_att, _HttpAttachment) and altyazi_att is not None:
                altyazi_att = attachments.get(str(altyazi_att))
            intro_att = options.get("intro")
            if not isinstance(intro_att, _HttpAttachment) and intro_att is not None:
                intro_att = attachments.get(str(intro_att))

            job = await handle_encodeal_slash_command(
                adapter,
                magnet=str(options.get("magnet") or ""),
                altyazi=altyazi_att,
                intro=intro_att,
                codec=str(options.get("codec") or "h264"),
                dil=str(options.get("dil") or "tur"),
            )
            return {"command": cmd_name, "status": "dispatched", "job_id": job.job_id if job else None, "wake": wake_result}

        if cmd_name == JOB_STATUS_COMMAND_NAME:
            await handle_job_status_slash_command(
                adapter,
                is_id=str(options.get("is_id") or ""),
            )
            return {"command": cmd_name, "status": "completed", "wake": wake_result}

        if cmd_name == DISCORD_RESTART_COMMAND_NAME:
            code, res_payload = await handle_discord_restart_command(
                adapter,
                bot=active_bot,
                bot_manager=bot_manager,
            )
            return {"command": cmd_name, "status": "completed", "http_status": code, "result": res_payload}

        if cmd_name == "log":
            from rclone_manager import handle_log_slash_command
            code, res_payload = await handle_log_slash_command(
                adapter,
                bot=active_bot,
                bot_manager=bot_manager,
            )
            return {"command": cmd_name, "status": "completed", "http_status": code, "result": res_payload}

        if cmd_name == "stats":
            stats_data = await asyncio.to_thread(collect_all_stats, active_bot)
            embed = build_stats_embed(stats_data)
            wake_note = ""
            if wake_result and wake_result.get("woken"):
                wake_note = "⚡ **Komutla Uyanma Tetiklendi:** Discord Gateway botu arka planda yeniden bağlanıyor.\n"
            summary_text = format_compact_stats_message(stats_data, max_len=950, wake_note=wake_note)
            if len(summary_text) > 950:
                summary_text = summary_text[:947] + "..."
            await adapter.edit_original_response(content=summary_text, embed=embed)
            return {"command": cmd_name, "status": "completed", "wake": wake_result}

        if cmd_name in ("drive_bagla", "drive_durum", "drive_ayir", "drive_yardim", "drive bağla", "drive yardım"):
            msg = (
                "ℹ️ **Komut Kaldırıldı:** Google Drive ve MEGA bulut depolama bağlantıları artık Discord slash komutlarıyla değil, "
                "doğrudan **UBAgen Yönetim Paneli (Rclone Bulut Depolama)** arayüzünden yönetilmektedir."
            )
            await adapter.edit_original_response(content=msg)
            return {"command": cmd_name, "status": "removed_legacy_command", "wake": wake_result}

        # Any other command (e.g. /altyazı_ayıklama) wakes the Gateway bot and informs the user accurately
        wake_status = (wake_result or {}).get("status", "unknown")
        if wake_status in ("already_ready",):
            msg = (
                f"ℹ️ `/{cmd_name}` komutu alındı. Bot Gateway bağlantısı aktif; "
                "komutu tekrar çalıştırabilirsiniz."
            )
        else:
            msg = (
                f"⚡ **Bot Uyandırılıyor (`/{cmd_name}`):** Discord bot servisi uyku/kopma durumundan "
                "çıkarıldı ve Gateway bağlantısı yeniden kuruluyor. Lütfen birkaç saniye içinde komutu tekrar deneyin."
            )
        await adapter.edit_original_response(content=msg)
        return {"command": cmd_name, "status": "waking_gateway", "wake": wake_result}

    except Exception as exc:
        safe_err = redact_sensitive_text(str(exc))[:300]
        logger.exception("Error executing HTTP Discord interaction command '/%s': %s", cmd_name, safe_err)
        with suppress(Exception):
            await adapter.edit_original_response(
                content=f"❌ Komut işlenirken hata oluştu: {safe_err}"
            )
        return {"command": cmd_name, "status": "error", "error": safe_err}


def get_interaction_endpoint_status(bot_manager: Optional[Any] = None) -> dict[str, Any]:
    """
    Return safe diagnostic metadata for the Discord HTTP Interaction Endpoint,
    wake-on-command readiness, and Cloud Run scaling/CPU lifecycle requirements.
    Never exposes any secret keys or tokens.
    """
    pub_key = get_configured_discord_public_key()
    pub_key_valid = False
    if pub_key:
        try:
            pub_key_valid = len(bytes.fromhex(pub_key)) == 32
        except ValueError:
            pub_key_valid = False

    base_url = (
        os.getenv("UBAGEN_PUBLIC_BASE_URL", "").strip()
        or os.getenv("APP_URL", "").strip()
        or os.getenv("SERVICE_URL", "").strip()
    ).rstrip("/")
    interactions_url = f"{base_url}/api/discord/interactions" if base_url else "/api/discord/interactions"

    k_service = os.getenv("K_SERVICE", "").strip() or None
    min_instances_raw = os.getenv("CLOUD_RUN_MIN_INSTANCES", "").strip()
    cpu_throttling_raw = os.getenv("CLOUD_RUN_CPU_THROTTLING", "").strip()

    bot_inst = getattr(bot_manager, "bot", None) if bot_manager is not None else None
    tracker = getattr(bot_inst, "tracker", None) if bot_inst is not None else None

    discord_connected = bool(getattr(tracker, "connected", False)) if tracker else False
    bot_ready = bool(getattr(tracker, "is_ready", False)) if tracker else False
    if bot_manager is not None and hasattr(bot_manager, "is_bot_ready"):
        with suppress(Exception):
            bot_ready = bool(bot_manager.is_bot_ready())

    return {
        "http_alive": True,
        "discord_connected": discord_connected,
        "bot_ready": bot_ready,
        "endpoint_path": "/api/discord/interactions",
        "interactions_url": interactions_url,
        "public_key_configured": pub_key_valid,
        "signature_algorithm": "Ed25519 (X-Signature-Ed25519 + X-Signature-Timestamp)",
        "wake_on_command_enabled": True,
        "wake_cooldown_seconds": float(getattr(bot_manager, "wake_cooldown_seconds", 10.0)) if bot_manager else 10.0,
        "last_wake_at": getattr(bot_manager, "last_wake_epoch", None) if bot_manager else None,
        "last_wake_reason": getattr(bot_manager, "last_wake_reason", None) if bot_manager else None,
        "wake_attempts": int(getattr(bot_manager, "wake_attempts", 0)) if bot_manager else 0,
        "cloud_run_lifecycle": {
            "is_cloud_run_service": bool(k_service),
            "k_service": k_service,
            "min_instances_configured": min_instances_raw or "0 (scale-to-zero default)",
            "cpu_throttling": cpu_throttling_raw or "request-scoped (default unless --no-cpu-throttling)",
            "scale_to_zero_note": (
                "Tamamen 0 instance'a kapanmış bir Cloud Run servisi kendi iç zamanlayıcısıyla uyanamaz. "
                "Komutla uyanma için Discord Developer Portal'da 'Interactions Endpoint URL' olarak "
                f"`{interactions_url}` tanımlanmalı (`DISCORD_PUBLIC_KEY` ile) veya harici bir kontrol "
                "servisi `POST /api/discord/wake` uç noktasını tetiklemelidir. Gateway WebSocket'in sürekli "
                "açık kalması isteniyorsa Cloud Run servisinde `--min-instances=1` ve `--no-cpu-throttling` gerekir."
            ),
        },
    }


async def handle_discord_interaction_request(
    *,
    raw_body: bytes,
    signature_header: Optional[str],
    timestamp_header: Optional[str],
    app: Optional[Any] = None,
    public_key_hex: Optional[str] = None,
    webhook_sender: Optional[Any] = None,
    wait_for_background: bool = False,
) -> tuple[int, dict[str, Any]]:
    """
    Top-level HTTP handler for `POST /api/discord/interactions` and `/discord/interactions`:
    1. Checks `DISCORD_PUBLIC_KEY` configuration (503 if not configured).
    2. Verifies Ed25519 signature (`X-Signature-Ed25519` + `X-Signature-Timestamp`) and 300s replay window (401 on failure).
    3. Responds to Discord PING (`type=1`) with PONG (`{"type": 1}`, HTTP 200).
    4. On Slash Command (`type=2`), triggers Wake-on-Command (`wake_bot` / `ensure_bot_awake`),
       schedules asynchronous command execution via `execute_http_command_background`, and
       immediately returns `{"type": 5}` (Deferred response) within Discord's 3-second callback deadline.
    """
    pub_key = (public_key_hex if public_key_hex is not None else get_configured_discord_public_key()).strip()
    if not pub_key:
        return 503, {
            "error": (
                "Discord Interactions Endpoint is not configured: DISCORD_PUBLIC_KEY is missing on the server."
            )
        }

    sig_ok, sig_reason = verify_discord_ed25519_signature(
        public_key_hex=pub_key,
        signature_hex=signature_header,
        timestamp_header=timestamp_header,
        raw_body=raw_body,
    )
    if not sig_ok:
        logger.warning("Rejected Discord interaction with invalid signature: %s", redact_sensitive_text(sig_reason))
        return 401, {"error": f"Invalid request signature: {sig_reason}"}

    try:
        payload = json.loads((raw_body or b"").decode("utf-8"))
        if not isinstance(payload, dict):
            return 400, {"error": "Interaction payload must be a JSON object."}
    except Exception:
        return 400, {"error": "Malformed JSON interaction payload."}

    interaction_type = payload.get("type")

    # 1. Discord PING -> PONG verification handshake
    if interaction_type == INTERACTION_TYPE_PING:
        return 200, {"type": INTERACTION_RESPONSE_PONG}

    # 2. Discord Slash Command (`APPLICATION_COMMAND`)
    if interaction_type == INTERACTION_TYPE_APPLICATION_COMMAND:
        data_obj = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        cmd_name = str(data_obj.get("name") or "").strip().lower()

        bot = getattr(app.state, "bot", None) if (app is not None and hasattr(app, "state")) else None
        bot_manager = getattr(app.state, "bot_manager", None) if (app is not None and hasattr(app, "state")) else None
        wake_fn = getattr(app.state, "wake_bot", None) if (app is not None and hasattr(app, "state")) else None

        # Trigger Wake-on-Command immediately if a wake hook is registered on app.state
        if wake_fn is not None and callable(wake_fn) and cmd_name != "discord":
            try:
                res = wake_fn(reason=f"slash_command:{cmd_name or 'unknown'}")
                if asyncio.iscoroutine(res):
                    await res
            except TypeError:
                res = wake_fn()
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:
                logger.warning("Wake hook warning (%s): %s", type(exc).__name__, redact_sensitive_text(str(exc)))

        bg_coro = execute_http_command_background(
            payload,
            bot=bot,
            bot_manager=bot_manager,
            webhook_sender=webhook_sender,
        )
        if wait_for_background:
            await bg_coro
        else:
            asyncio.create_task(bg_coro)

        if cmd_name in ("log", "drive_bagla", "drive_durum", "drive_ayir", "drive_yardim", "drive bağla", "drive yardım"):
            return 200, {
                "type": INTERACTION_RESPONSE_DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE,
                "data": {"flags": 64},
            }

        return 200, {"type": INTERACTION_RESPONSE_DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE}

    return 400, {"error": f"Unsupported Discord interaction type: {interaction_type}"}

