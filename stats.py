"""
UBAgen — stats.py
Diagnostic system statistics collector and Discord '/stats' slash command.

Gathers accurate host, container, process, toolchain, and status telemetry
without blocking the Discord event loop or leaking sensitive tokens.
Provides real measurements for both the Discord slash command and the GET /api/stats endpoint.
"""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from contextlib import suppress
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Optional

try:
    from storage_manager import get_storage_manager
except ImportError:
    get_storage_manager = None

# Optional psutil with pure-procfs fallback
try:
    import psutil
except ImportError:
    psutil = None

# Optional discord.py
try:
    import discord
    from discord import app_commands
except ImportError:
    discord = None
    app_commands = None

logger = logging.getLogger("ubagen.stats")

# Process start time for uptime calculation
PROCESS_START_TIME = time.time()


# ------------------------------------------------------------------------------
# Thread-Safe Redacted Log Ring Buffer (for GET /api/logs)
# ------------------------------------------------------------------------------
_LOG_LOCK = threading.Lock()
_LOG_BUFFER: deque[dict[str, Any]] = deque(maxlen=250)
_LOG_HANDLER_INSTALLED = False


def redact_sensitive_text(text: str) -> str:
    """Scrub any potential tokens, bearer credentials, or secret env values from log lines."""
    if not text:
        return ""
    cleaned = text
    for env_key in (
        "DISCORD_BOT_TOKEN",
        "UBAGEN_ADMIN_TOKEN",
        "GOOGLE_DRIVE_CREDENTIALS",
        "GOOGLE_CLIENT_SECRET",
        "TOKEN_ENCRYPTION_KEY",
    ):
        secret_val = os.getenv(env_key, "").strip()
        if secret_val and len(secret_val) >= 6:
            cleaned = cleaned.replace(secret_val, "[REDACTED]")
    cleaned = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._\-+/=]+", r"\1[REDACTED]", cleaned)
    cleaned = re.sub(r"\bya29\.[A-Za-z0-9._\-]+", "[REDACTED_GOOGLE_ACCESS_TOKEN]", cleaned)
    cleaned = re.sub(r"\b1//[A-Za-z0-9._\-]{16,}", "[REDACTED_GOOGLE_REFRESH_TOKEN]", cleaned)
    return cleaned


class RedactedRingLogHandler(logging.Handler):
    """Captures application log records into a bounded thread-safe ring buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = redact_sensitive_text(record.getMessage())
            entry = {
                "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "epoch": round(record.created, 3),
                "level": record.levelname,
                "logger": record.name,
                "message": msg,
            }
            with _LOG_LOCK:
                _LOG_BUFFER.append(entry)
        except Exception:
            pass


def install_log_buffer_handler() -> None:
    """Attach the ring buffer log handler once to the root logger."""
    global _LOG_HANDLER_INSTALLED
    if _LOG_HANDLER_INSTALLED:
        return
    with _LOG_LOCK:
        if _LOG_HANDLER_INSTALLED:
            return
        root_logger = logging.getLogger()
        handler = RedactedRingLogHandler(level=logging.INFO)
        root_logger.addHandler(handler)
        _LOG_HANDLER_INSTALLED = True


def get_recent_logs(limit: int = 100) -> list[dict[str, Any]]:
    """Return up to `limit` recent redacted log entries (newest last)."""
    install_log_buffer_handler()
    safe_limit = max(1, min(int(limit or 100), 250))
    with _LOG_LOCK:
        items = list(_LOG_BUFFER)
    return items[-safe_limit:]


# ------------------------------------------------------------------------------
# Discord Client ID & Invite Helpers
# ------------------------------------------------------------------------------
def extract_discord_client_id(bot_instance: Any = None) -> Optional[str]:
    """
    Safely retrieves the Discord Client/Application ID from:
    1. The connected bot's user object (bot.user.id)
    2. DISCORD_CLIENT_ID or DISCORD_APPLICATION_ID environment variables
    3. The first segment of DISCORD_BOT_TOKEN (base64-encoded Client ID in Discord tokens)
    Never exposes or logs the bot token itself.
    """
    if bot_instance and getattr(bot_instance, "user", None):
        user_id = getattr(bot_instance.user, "id", None)
        if user_id:
            return str(user_id)

    for env_key in ("DISCORD_CLIENT_ID", "DISCORD_APPLICATION_ID"):
        val = os.getenv(env_key, "").strip()
        if val.isdigit() and len(val) >= 15:
            return val

    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if token and "." in token and token not in ("your_discord_bot_token_here", "MY_DISCORD_BOT_TOKEN", "CHANGE_ME"):
        try:
            part = token.split(".")[0].strip()
            padded = part + "=" * (-len(part) % 4)
            decoded = base64.b64decode(padded).decode("utf-8")
            if decoded.isdigit() and len(decoded) >= 15:
                return decoded
        except Exception:
            pass

    return None


def generate_discord_invite_url(client_id: Optional[str], permissions: int = 2147485696) -> Optional[str]:
    """
    Generates standard Discord OAuth2 bot invite URL.
    Default permissions 2147485696 includes Use Application Commands (2147483648) and Send Messages (2048).
    """
    if not client_id:
        return None
    return f"https://discord.com/oauth2/authorize?client_id={client_id}&permissions={permissions}&scope=bot%20applications.commands"


# ------------------------------------------------------------------------------
# Formatting Utilities
# ------------------------------------------------------------------------------
def format_bytes(num_bytes: int | float | None) -> str:
    """Format bytes into a human-readable string (B, KB, MB, GB, TB)."""
    if num_bytes is None or num_bytes < 0:
        return "Unavailable"
    num = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num) < 1024.0:
            return f"{num:3.2f} {unit}"
        num /= 1024.0
    return f"{num:.2f} PB"


def format_duration(seconds: float | None) -> str:
    """Format duration in seconds to a human-readable string (e.g., 2d 4h 12m 30s)."""
    if seconds is None or seconds < 0:
        return "Unavailable"
    secs = int(seconds)
    days, remainder = divmod(secs, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, s = divmod(remainder, 60)

    parts: list[str] = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0 or days > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{s}s")

    return " ".join(parts) if parts else "0s"


# ------------------------------------------------------------------------------
# Persistent Gateway Lifecycle & AI Studio / Cloud Run Environment Telemetry
# ------------------------------------------------------------------------------
LIFECYCLE_DB_LOCK = threading.Lock()


def get_lifecycle_db_path() -> Path:
    """Resolve persistent SQLite DB path for lifecycle telemetry."""
    base_dir = os.getenv("UBAGEN_STORAGE_DIR", "").strip()
    if base_dir and os.path.isdir(base_dir):
        return Path(base_dir) / "ubagen_jobs.sqlite3"
    local_p = Path("ubagen_jobs.sqlite3").resolve()
    if local_p.parent.exists() and os.access(str(local_p.parent), os.W_OK):
        return local_p
    return Path("/tmp/ubagen_jobs.sqlite3")


def record_gateway_lifecycle_event(
    event_type: str,
    duration_sec: Optional[float] = None,
    details: str = "",
) -> None:
    """Safely log gateway lifecycle transitions (startup, ready, disconnect, resume, shutdown)."""
    db_path = get_lifecycle_db_path()
    try:
        with LIFECYCLE_DB_LOCK:
            with sqlite3.connect(str(db_path), timeout=5.0) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS gateway_lifecycle_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        duration_sec REAL,
                        details TEXT
                    );
                    """
                )
                conn.execute(
                    "INSERT INTO gateway_lifecycle_history (event_type, timestamp, duration_sec, details) VALUES (?, ?, ?, ?)",
                    (event_type, time.time(), duration_sec, details[:500] if details else None),
                )
    except Exception as exc:
        logger.debug("Could not record gateway lifecycle event: %s", exc)


def get_latest_lifecycle_summary() -> dict[str, Any]:
    """Retrieve previous session duration and restart history from persistent database."""
    db_path = get_lifecycle_db_path()
    if not db_path.exists():
        return {"previous_session_sec": None, "previous_session_str": None, "last_event": None}
    try:
        with LIFECYCLE_DB_LOCK:
            with sqlite3.connect(str(db_path), timeout=3.0) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT event_type, timestamp, duration_sec, details
                    FROM gateway_lifecycle_history
                    WHERE duration_sec IS NOT NULL AND duration_sec > 0
                    ORDER BY id DESC LIMIT 1;
                    """
                )
                row = cursor.fetchone()
                if row:
                    dur = float(row["duration_sec"])
                    return {
                        "previous_session_sec": dur,
                        "previous_session_str": format_duration(dur),
                        "last_event": row["event_type"],
                    }
    except Exception:
        pass
    return {"previous_session_sec": None, "previous_session_str": None, "last_event": None}


def detect_gateway_shutdown_info(
    bot_tracker: Any = None,
    now: Optional[float] = None,
    backend_uptime: Optional[float] = None,
) -> dict[str, Any]:
    """
    Analyzes Google AI Studio / Cloud Run execution environment and Gateway session lifetimes.
    Strictly differentiates between:
    1. Exact platform timeout limits (e.g., CLOUD_RUN_TIMEOUT_SECONDS=3600)
    2. Estimated remaining runtime (which varies based on idle scale-to-zero and CPU throttling)
    3. Measured continuous session durations and time since last disconnect
    4. Explicit 'Bilinmiyor' / 'Ölçülemiyor' fallbacks when data cannot be measured.
    """
    cur_now = now or time.time()
    effective_uptime = backend_uptime if backend_uptime is not None else (cur_now - PROCESS_START_TIME)

    # 1. Platform-level limit detection
    cr_timeout_env = os.getenv("CLOUD_RUN_TIMEOUT_SECONDS", "").strip()
    is_cloud_run = bool(os.getenv("K_SERVICE") or os.getenv("K_REVISION") or cr_timeout_env)
    platform_limit_sec: Optional[int] = None
    platform_limit_str: str = "Bilinmiyor"
    limit_type: str = "unknown"
    is_exact_platform_limit: bool = False

    if cr_timeout_env.isdigit():
        platform_limit_sec = int(cr_timeout_env)
        platform_limit_str = f"{platform_limit_sec}s ({format_duration(platform_limit_sec)})"
        limit_type = "platform_configured"
        is_exact_platform_limit = True
        platform_desc = (
            f"Google Cloud Run azami istek yürütme sınırı (CLOUD_RUN_TIMEOUT_SECONDS={platform_limit_sec}). "
            "Aktif HTTP trafiği olmadığında Cloud Run örneği boşta kalma nedeniyle daha erken sonlandırabilir."
        )
    elif is_cloud_run:
        platform_limit_sec = 3600
        platform_limit_str = "1 saat (3600s)"
        limit_type = "platform_configured"
        is_exact_platform_limit = True
        platform_desc = (
            "Google Cloud Run varsayılan azami timeout sınırı (3600s). "
            "Boşta kalma (scale-to-zero) durumunda daha erken sonlandırılabilir."
        )
    else:
        platform_limit_sec = None
        platform_limit_str = "Bilinmiyor"
        limit_type = "unknown"
        is_exact_platform_limit = False
        platform_desc = "Platform tarafından bildirilen kesin bir kapanma süresi sınırı bulunmuyor."

    # 2. Observed continuous session measurement
    is_ready = False
    is_connected = False
    last_conn: Optional[float] = None
    last_err: Optional[float] = None

    if bot_tracker:
        is_ready = bool(getattr(bot_tracker, "is_ready", False))
        is_connected = bool(getattr(bot_tracker, "connected", False) or is_ready)
        last_conn = getattr(bot_tracker, "last_connected_time", None)
        last_err = getattr(bot_tracker, "last_error_time", None)

    observed_session_sec: Optional[float] = None
    observed_session_str: str = "Ölçülemiyor"
    if is_ready and isinstance(last_conn, (int, float)) and last_conn > 0:
        observed_session_sec = max(0.0, cur_now - float(last_conn))
        observed_session_str = format_duration(observed_session_sec)

    time_since_disconnect_sec: Optional[float] = None
    time_since_disconnect_str: Optional[str] = None
    if not is_connected and isinstance(last_err, (int, float)) and last_err > 0:
        time_since_disconnect_sec = max(0.0, cur_now - float(last_err))
        time_since_disconnect_str = format_duration(time_since_disconnect_sec)

    # 3. Estimated remaining lifetime calculation
    estimated_remaining_sec: Optional[float] = None
    estimated_remaining_str: str = "Ölçülemiyor"
    if platform_limit_sec is not None and effective_uptime is not None:
        estimated_remaining_sec = max(0.0, float(platform_limit_sec) - float(effective_uptime))
        estimated_remaining_str = format_duration(estimated_remaining_sec)
        uncertainty_note = (
            "Tahmini ve belirsizdir: Cloud Run boşta kalma politikası (scale-to-zero) ve CPU kısma "
            "nedeniyle aktif web trafiği kesilirse örnek 3600s dolmadan önce kapanabilir."
        )
    else:
        uncertainty_note = (
            "Platformdan kesin bir kapanma süresi bilgisi alınamadığından sabit bir değer uydurulmamıştır. "
            "Ölçüm 'Bilinmiyor / Ölçülemiyor' olarak raporlanmaktadır."
        )

    # 4. Historical lifecycle summary
    history = get_latest_lifecycle_summary()

    # 5. Display label / summary
    if is_ready and observed_session_sec is not None:
        summary_display = f"Aktif: {observed_session_str} (Limit: {platform_limit_str})"
    elif not is_connected and time_since_disconnect_str:
        summary_display = f"Kopuk: {time_since_disconnect_str} önce"
    else:
        summary_display = "Ölçülemiyor"

    return {
        "label": "Gateway Kapanma Süresi",
        "platform_limit_seconds": platform_limit_sec,
        "platform_limit_str": platform_limit_str,
        "limit_type": limit_type,
        "is_exact_platform_limit": is_exact_platform_limit,
        "platform_limit_description": platform_desc,
        "observed_session_duration_sec": observed_session_sec,
        "observed_session_duration_str": observed_session_str,
        "time_since_last_disconnect_sec": time_since_disconnect_sec,
        "time_since_last_disconnect_str": time_since_disconnect_str,
        "estimated_remaining_seconds": estimated_remaining_sec,
        "estimated_remaining_str": estimated_remaining_str,
        "uncertainty_note": uncertainty_note,
        "previous_session_duration_sec": history.get("previous_session_sec"),
        "previous_session_duration_str": history.get("previous_session_str"),
        "summary_display": summary_display,
    }


# ------------------------------------------------------------------------------
# Bot Tracker Class
# ------------------------------------------------------------------------------
class BotTracker:
    """
    Thread-safe, robust tracking structure for Discord bot lifecycle and gateway state.
    Provides diagnostic messages without exposing tokens or credentials.
    Separates HTTP liveness, Gateway WebSocket connection, and full Bot Readiness.
    """
    def __init__(self) -> None:
        self.status: str = "not_configured"  # not_configured, connecting, connected, invalid_token, network_error, api_error, sync_error, disconnected, library_missing
        self.connected: bool = False
        self.is_ready: bool = False
        self.latency_ms: Optional[float] = None
        self.user: Optional[str] = None
        self.guild_count: int = 0
        self.diagnostic_message: str = "DISCORD_BOT_TOKEN is not configured. Web monitor is active."
        self.last_connected_time: Optional[float] = None
        self.sync_status: Optional[str] = None
        self.reconnect_attempts: int = 0
        self.last_error_time: Optional[float] = None
        self.client_id: Optional[str] = extract_discord_client_id()
        self.invite_url: Optional[str] = generate_discord_invite_url(self.client_id)
        self.wake_in_progress: bool = False
        self.wake_attempts: int = 0
        self.last_wake_time: Optional[float] = None
        self.last_wake_reason: Optional[str] = None

    def to_dict(self, now: Optional[float] = None) -> dict[str, Any]:
        cur_now = now or time.time()
        uptime_sec = (cur_now - self.last_connected_time) if (self.is_ready and self.last_connected_time) else None

        # Never report status="connected" until the bot is genuinely ready (is_ready == True)
        effective_status = self.status
        if effective_status == "connected" and not self.is_ready:
            effective_status = "connecting"

        # Determine friendly display name for user
        if self.user:
            display_user = self.user
        elif self.is_ready:
            display_user = "Connected"
        elif self.connected or effective_status == "connecting":
            display_user = "Connecting..."
        elif effective_status == "invalid_token":
            display_user = "Authentication Failed"
        elif effective_status == "network_error":
            display_user = "Network Error"
        elif effective_status == "not_configured":
            display_user = "Not configured"
        else:
            display_user = "Disconnected"

        cid = self.client_id or extract_discord_client_id()
        inv_url = self.invite_url or generate_discord_invite_url(cid)
        pub_key = os.getenv("DISCORD_PUBLIC_KEY", "").strip()
        interactions_configured = bool(len(pub_key) == 64)
        shutdown_telemetry = detect_gateway_shutdown_info(self, now=cur_now, backend_uptime=uptime_sec)

        return {
            "status": effective_status,
            "connected": bool(self.connected),
            "is_ready": bool(self.is_ready),
            "http_alive": True,
            "gateway_connected": bool(self.connected),
            "bot_ready": bool(self.is_ready),
            "diagnostic_message": self.diagnostic_message,
            "latency_ms": self.latency_ms,
            "user": display_user,
            "guild_count": self.guild_count,
            "last_connected_time": self.last_connected_time,
            "uptime_seconds": uptime_sec,
            "uptime_str": format_duration(uptime_sec),
            "sync_status": self.sync_status,
            "client_id": cid,
            "invite_url": inv_url,
            "reconnect_attempts": int(getattr(self, "reconnect_attempts", 0)),
            "wake_in_progress": bool(getattr(self, "wake_in_progress", False)),
            "wake_attempts": int(getattr(self, "wake_attempts", 0)),
            "last_wake_time": getattr(self, "last_wake_time", None),
            "last_wake_reason": getattr(self, "last_wake_reason", None),
            "interactions_endpoint_configured": interactions_configured,
            "interactions_endpoint_path": "/api/discord/interactions",
            "wake_endpoint_path": "/api/discord/wake",
            "gateway_shutdown_info": shutdown_telemetry,
        }


# ------------------------------------------------------------------------------
# CPU Sampling Helper (Prevents uninitialized 0.0 on first call)
# ------------------------------------------------------------------------------
class AccurateCpuSampler:
    """
    Measures CPU percentage accurately without uninitialized 0.0 artifacts
    or blocking delays.
    """
    def __init__(self) -> None:
        self.last_procfs_total: Optional[int] = None
        self.last_procfs_idle: Optional[int] = None
        self.last_sample_time: float = 0.0
        self.cached_pct: Optional[float] = None

        if psutil is not None:
            try:
                # Prime psutil so subsequent calls have a non-zero time baseline
                psutil.cpu_percent(interval=None)
            except Exception:
                pass

    def get_percent(self) -> Optional[float]:
        now = time.time()
        # Cache reading for 0.5s to prevent thrashing
        if self.cached_pct is not None and (now - self.last_sample_time) < 0.5:
            return self.cached_pct

        if psutil is not None:
            try:
                pct = psutil.cpu_percent(interval=None)
                # If psutil returns 0.0 right after startup, take a tiny 0.05s baseline once
                if pct == 0.0 and (now - PROCESS_START_TIME) < 2.0:
                    pct = psutil.cpu_percent(interval=0.05)
                self.cached_pct = float(pct)
                self.last_sample_time = now
                return self.cached_pct
            except Exception as e:
                logger.debug("psutil cpu_percent error: %s", e)

        # Procfs fallback
        pct = self._read_procfs_cpu()
        if pct is not None:
            self.cached_pct = pct
            self.last_sample_time = now
            return pct

        return self.cached_pct

    def _read_procfs_cpu(self) -> Optional[float]:
        if not os.path.exists("/proc/stat"):
            return None
        try:
            with open("/proc/stat", "r", encoding="utf-8") as f:
                line = f.readline()
            if not line.startswith("cpu "):
                return None
            fields = [int(x) for x in line.split()[1:]]
            if len(fields) < 4:
                return None
            # fields: user, nice, system, idle, iowait, irq, softirq, steal
            idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
            total = sum(fields)

            if self.last_procfs_total is not None and self.last_procfs_idle is not None:
                total_delta = total - self.last_procfs_total
                idle_delta = idle - self.last_procfs_idle
                self.last_procfs_total = total
                self.last_procfs_idle = idle
                if total_delta > 0:
                    busy_delta = max(0, total_delta - idle_delta)
                    return round((busy_delta / total_delta) * 100.0, 1)
            else:
                self.last_procfs_total = total
                self.last_procfs_idle = idle
                return None
        except Exception:
            return None
        return None

_CPU_SAMPLER = AccurateCpuSampler()


# ------------------------------------------------------------------------------
# Container Quota & Resource Limit Detection (Cgroups v1 & v2)
# ------------------------------------------------------------------------------
def detect_container_resource_limits() -> dict[str, Any]:
    """
    Detect container CPU and Memory quotas in Linux namespaces (cgroups v1 or v2).
    Accurately determines whether the container has restricted memory/CPU ceilings.
    """
    result: dict[str, Any] = {
        "is_limited": False,
        "memory_limited": False,
        "cpu_limited": False,
        "limit_bytes": None,
        "limit_str": "No container memory quota detected",
        "usage_bytes": None,
        "usage_str": None,
        "cpu_quota_cores": None,
        "cpu_quota_str": "No container CPU quota detected",
        "type": "None",
        "description": "Full host resources visible (No container cgroup quotas enforced)",
    }

    # 1. Check Cgroups v2 Memory Limit
    cg2_mem_max = "/sys/fs/cgroup/memory.max"
    cg2_mem_curr = "/sys/fs/cgroup/memory.current"
    if os.path.exists(cg2_mem_max):
        try:
            with open(cg2_mem_max, "r", encoding="utf-8") as f:
                val = f.read().strip()
            if val.isdigit():
                limit_int = int(val)
                # Realistic container limit (less than 1 Petabyte)
                if limit_int < 10**15:
                    result["limit_bytes"] = limit_int
                    result["limit_str"] = format_bytes(limit_int)
                    result["memory_limited"] = True
                    result["is_limited"] = True
                    result["type"] = "cgroups v2"
                    result["description"] = f"{format_bytes(limit_int)} container memory ceiling enforced"
            if os.path.exists(cg2_mem_curr):
                with open(cg2_mem_curr, "r", encoding="utf-8") as f:
                    u_val = f.read().strip()
                if u_val.isdigit():
                    result["usage_bytes"] = int(u_val)
                    result["usage_str"] = format_bytes(int(u_val))
        except Exception as e:
            logger.debug("Failed to read cgroups v2 memory limit: %s", e)

    # 2. Check Cgroups v1 Memory Limit (if v2 memory wasn't limited)
    if not result["memory_limited"]:
        cg1_mem_max = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
        cg1_mem_curr = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
        if os.path.exists(cg1_mem_max):
            try:
                with open(cg1_mem_max, "r", encoding="utf-8") as f:
                    val = f.read().strip()
                if val.isdigit():
                    limit_int = int(val)
                    if limit_int < (1024**5):  # Under 1 Petabyte (unlimited in cgroups v1 is ~9.22e18)
                        result["limit_bytes"] = limit_int
                        result["limit_str"] = format_bytes(limit_int)
                        result["memory_limited"] = True
                        result["is_limited"] = True
                        result["type"] = "cgroups v1"
                        result["description"] = f"{format_bytes(limit_int)} container memory ceiling enforced"
                if os.path.exists(cg1_mem_curr):
                    with open(cg1_mem_curr, "r", encoding="utf-8") as f:
                        u_val = f.read().strip()
                    if u_val.isdigit():
                        result["usage_bytes"] = int(u_val)
                        result["usage_str"] = format_bytes(int(u_val))
            except Exception as e:
                logger.debug("Failed to read cgroups v1 memory limit: %s", e)

    # 3. Check Cgroups CPU Quota (v2 and v1)
    # Cgroups v2: /sys/fs/cgroup/cpu.max ($QUOTA $PERIOD)
    cg2_cpu_max = "/sys/fs/cgroup/cpu.max"
    if os.path.exists(cg2_cpu_max):
        try:
            with open(cg2_cpu_max, "r", encoding="utf-8") as f:
                parts = f.read().strip().split()
            if len(parts) == 2 and parts[0] != "max" and parts[0].isdigit() and parts[1].isdigit():
                quota = int(parts[0])
                period = int(parts[1])
                if period > 0 and quota > 0:
                    cores = round(quota / period, 2)
                    result["cpu_quota_cores"] = cores
                    result["cpu_quota_str"] = f"{cores:.1f} core(s) (cgroups v2)"
                    result["cpu_limited"] = True
                    result["is_limited"] = True
        except Exception as e:
            logger.debug("Failed to read cgroups v2 cpu quota: %s", e)

    # Cgroups v1: /sys/fs/cgroup/cpu/cpu.cfs_quota_us
    if not result["cpu_limited"]:
        cg1_cpu_quota = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
        cg1_cpu_period = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"
        if os.path.exists(cg1_cpu_quota) and os.path.exists(cg1_cpu_period):
            try:
                with open(cg1_cpu_quota, "r", encoding="utf-8") as f:
                    q_str = f.read().strip()
                with open(cg1_cpu_period, "r", encoding="utf-8") as f:
                    p_str = f.read().strip()
                if q_str.lstrip("-").isdigit() and p_str.isdigit():
                    quota = int(q_str)
                    period = int(p_str)
                    if quota > 0 and period > 0:
                        cores = round(quota / period, 2)
                        result["cpu_quota_cores"] = cores
                        result["cpu_quota_str"] = f"{cores:.1f} core(s) (cgroups v1)"
                        result["cpu_limited"] = True
                        result["is_limited"] = True
            except Exception as e:
                logger.debug("Failed to read cgroups v1 cpu quota: %s", e)

    return result


# ------------------------------------------------------------------------------
# System & Binary Diagnostics (Cached for Performance)
# ------------------------------------------------------------------------------
_BINARY_CACHE: dict[str, dict[str, Any]] = {}
_BINARY_CACHE_EXPIRY: float = 0.0


def clear_binary_cache() -> None:
    """Clear the cached binary inspection results so newly installed tools are detected immediately."""
    global _BINARY_CACHE_EXPIRY
    _BINARY_CACHE.clear()
    _BINARY_CACHE_EXPIRY = 0.0


def check_binary(binary_name: str, version_arg: str = "-version") -> dict[str, Any]:
    """
    Check whether a binary is installed and retrieve its version safely.
    Caches positive results for 60 seconds to avoid spawning subprocesses on every poll tick.
    """
    global _BINARY_CACHE_EXPIRY
    now = time.time()
    if now < _BINARY_CACHE_EXPIRY and binary_name in _BINARY_CACHE:
        cached = _BINARY_CACHE[binary_name]
        if cached.get("installed"):
            return cached

    path = shutil.which(binary_name)
    if not path:
        _BINARY_CACHE.pop(binary_name, None)
        return {
            "installed": False,
            "version": "Not installed",
            "path": None,
        }

    try:
        proc = subprocess.run(
            [path, version_arg],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        output = proc.stdout.strip() or proc.stderr.strip()
        first_line = output.splitlines()[0] if output else "Installed (Version unknown)"
        if len(first_line) > 55:
            first_line = first_line[:52] + "..."
        res = {
            "installed": True,
            "version": first_line,
            "path": path,
        }
    except Exception as e:
        logger.debug("Failed to execute %s version check: %s", binary_name, e)
        res = {
            "installed": True,
            "version": "Installed (Query error)",
            "path": path,
        }

    _BINARY_CACHE[binary_name] = res
    _BINARY_CACHE_EXPIRY = now + 60.0
    return res


def check_environment_variables() -> list[dict[str, str]]:
    """
    Verify configuration state of environment variables without leaking any secrets.
    """
    results: list[dict[str, str]] = []

    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if token:
        # Check minimal Discord token structure without leaking contents
        is_plausible = len(token) > 20 and "." in token
        results.append({
            "name": "DISCORD_BOT_TOKEN",
            "status": "Configured" if is_plausible else "Configured (Plausibility warning)",
            "indicator": "✅" if is_plausible else "⚠️",
            "type": "Required for Bot",
        })
    else:
        results.append({
            "name": "DISCORD_BOT_TOKEN",
            "status": "Not configured",
            "indicator": "⚪",
            "type": "Required for Bot",
        })

    admin_token = os.getenv("UBAGEN_ADMIN_TOKEN", "").strip()
    if admin_token:
        results.append({
            "name": "UBAGEN_ADMIN_TOKEN",
            "status": "Configured (Bot Restart Enabled)",
            "indicator": "✅",
            "type": "Admin Security",
        })
    else:
        results.append({
            "name": "UBAGEN_ADMIN_TOKEN",
            "status": "Not configured (Bot Restart Disabled)",
            "indicator": "⚪",
            "type": "Admin Security",
        })

    host_val = os.getenv("HOST", "").strip()
    if host_val:
        is_valid_host = (
            host_val in ("0.0.0.0", "127.0.0.1", "localhost", "::", "::1")
            or (" " not in host_val and "://" not in host_val and "/" not in host_val and len(host_val) <= 64)
        )
        results.append({
            "name": "HOST",
            "status": f"{host_val} (Bind Interface)" if is_valid_host else "Invalid Bind Host (Fallback: 0.0.0.0)",
            "indicator": "✅" if is_valid_host else "⚠️",
            "type": "HTTP Server",
        })
    else:
        results.append({
            "name": "HOST",
            "status": "0.0.0.0 (Default Bind Interface)",
            "indicator": "⚪",
            "type": "HTTP Server",
        })

    port = os.getenv("PORT", "").strip()
    if port:
        results.append({
            "name": "PORT",
            "status": f"{port} (Configured)",
            "indicator": "✅",
            "type": "HTTP Server",
        })
    else:
        results.append({
            "name": "PORT",
            "status": "8080 (Default)",
            "indicator": "⚪",
            "type": "HTTP Server",
        })

    guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
    if guild_id:
        if guild_id.isdigit() and 17 <= len(guild_id) <= 20:
            results.append({
                "name": "DISCORD_GUILD_ID",
                "status": "Configured (Instant Dev Sync)",
                "indicator": "✅",
                "type": "Development",
            })
        else:
            results.append({
                "name": "DISCORD_GUILD_ID",
                "status": "Invalid Format (Must be numeric Snowflake)",
                "indicator": "⚠️",
                "type": "Development",
            })
    else:
        results.append({
            "name": "DISCORD_GUILD_ID",
            "status": "Not Set (Global Sync)",
            "indicator": "⚪",
            "type": "Development",
        })

    env_name = os.getenv("ENVIRONMENT", "production").strip()
    results.append({
        "name": "ENVIRONMENT",
        "status": env_name,
        "indicator": "✅",
        "type": "Configuration",
    })

    # Google Drive credentials check (without exposing secrets or tokens)
    gdrive_env = (
        os.getenv("GOOGLE_DRIVE_CREDENTIALS", "").strip()
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        or os.getenv("GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON", "").strip()
        or os.getenv("GOOGLE_SERVICE_ACCOUNT_KEY", "").strip()
    )
    gdrive_candidate_files = [
        "credentials.json",
        "service_account.json",
        "client_secret.json",
        "google_drive_credentials.json",
    ]
    gdrive_file_found = any(os.path.isfile(f) for f in gdrive_candidate_files)
    if gdrive_env or gdrive_file_found:
        results.append({
            "name": "GOOGLE_DRIVE_CREDENTIALS",
            "status": "Configured" if not gdrive_file_found else "Configured (Credential file detected)",
            "indicator": "✅",
            "type": "Cloud Storage",
        })
    else:
        results.append({
            "name": "GOOGLE_DRIVE_CREDENTIALS",
            "status": "Not configured",
            "indicator": "⚪",
            "type": "Cloud Storage",
        })

    gcs_bucket = os.getenv("UBAGEN_GCS_BUCKET", "").strip() or os.getenv("GCS_BUCKET_NAME", "").strip()
    results.append({
        "name": "UBAGEN_GCS_BUCKET",
        "status": f"Configured ({gcs_bucket} • 7200s TTL)" if gcs_bucket else "Not configured (Required for persistent GCS outputs)",
        "indicator": "✅" if gcs_bucket else "⚪",
        "type": "Cloud Storage",
    })

    job_name = os.getenv("UBAGEN_CLOUD_RUN_JOB_NAME", "").strip()
    raw_mode = os.getenv("UBAGEN_ENCODER_MODE", "").strip().lower()
    encoder_mode = raw_mode or "service_worker"
    in_proc_mode = encoder_mode in ("service_worker", "local_service_worker", "in_process", "local", "isolated_subprocess")
    local_fb = os.getenv("UBAGEN_ALLOW_LOCAL_WORKER_FALLBACK", "").strip().lower() in ("1", "true", "yes")
    results.append({
        "name": "UBAGEN_ENCODER_MODE",
        "status": (
            f"{encoder_mode} (Yerel Worker & Rclone Depolama — Google Cloud Gerektirmez)"
            if in_proc_mode
            else f"{encoder_mode} (Cloud Run Job v2 Execution Mode)"
        ),
        "indicator": "✅",
        "type": "Encoder Mode",
    })
    results.append({
        "name": "UBAGEN_CLOUD_RUN_JOB_NAME",
        "status": (
            f"Configured ({job_name})"
            if job_name
            else (
                "Optional (Bypassed in service_worker mode)"
                if in_proc_mode
                else (
                    "Not configured (Dev local worker fallback enabled)"
                    if local_fb
                    else "Not configured (Required for production Cloud Run Job dispatch)"
                )
            )
        ),
        "indicator": "✅" if (job_name or in_proc_mode) else ("⚠️" if local_fb else "⚪"),
        "type": "Cloud Run Job",
    })

    discord_pub_key = os.getenv("DISCORD_PUBLIC_KEY", "").strip()
    valid_pub_key = bool(re.fullmatch(r"[0-9a-fA-F]{64}", discord_pub_key))
    results.append({
        "name": "DISCORD_PUBLIC_KEY",
        "status": (
            "Configured (Ed25519 Signed HTTP Interactions & Wake-on-Command Active)"
            if valid_pub_key
            else (
                "Invalid Format (Expected 64-char hex Ed25519 public key)"
                if discord_pub_key
                else "Not configured (Set for signed HTTP Interactions wake-on-command)"
            )
        ),
        "indicator": "✅" if valid_pub_key else ("⚠️" if discord_pub_key else "⚪"),
        "type": "Discord Wake-on-Command",
    })

    gcp_proj = (
        os.getenv("UBAGEN_GCP_PROJECT_ID", "").strip()
        or os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    )
    gcp_reg = os.getenv("UBAGEN_GCP_REGION", "europe-west2").strip() or "europe-west2"
    results.append({
        "name": "UBAGEN_GCP_PROJECT_ID",
        "status": f"Configured ({gcp_proj} • {gcp_reg})" if gcp_proj else "Not configured (Set UBAGEN_GCP_PROJECT_ID)",
        "indicator": "✅" if gcp_proj else "⚪",
        "type": "Google Cloud",
    })

    use_fs = os.getenv("UBAGEN_USE_FIRESTORE", "").strip().lower() in ("1", "true", "yes")
    results.append({
        "name": "UBAGEN_USE_FIRESTORE",
        "status": "Enabled (Firestore Authoritative Store)" if use_fs else "Disabled (SQLite WAL Persistent Store)",
        "indicator": "✅" if use_fs else "⚪",
        "type": "Database",
    })

    discord_admins = os.getenv("UBAGEN_DISCORD_ADMIN_IDS", "").strip() or os.getenv("DISCORD_OWNER_ID", "").strip()
    results.append({
        "name": "UBAGEN_DISCORD_ADMIN_IDS",
        "status": "Configured (Explicit Admin IDs + Bot Owner/Server Admin)" if discord_admins else "Default (Bot Owner & Server Administrators)",
        "indicator": "✅",
        "type": "Discord Security",
    })

    rclone_accounts_count = 0
    rclone_connected_count = 0
    with suppress(Exception):
        from rclone_manager import get_rclone_manager
        rc_mgr = get_rclone_manager()
        accs = rc_mgr.list_accounts()
        rclone_accounts_count = len(accs)
        rclone_connected_count = sum(1 for a in accs if a.get("status") == "connected" and a.get("enabled", True))

    results.append({
        "name": "RCLONE (MEGA & Google Drive)",
        "status": (
            f"Active ({rclone_connected_count}/{rclone_accounts_count} remote connected • Built-in OAuth)"
            if rclone_accounts_count > 0
            else "Ready (Built-in Rclone OAuth • Manage via Web Panel)"
        ),
        "indicator": "✅" if rclone_connected_count > 0 else "⚪",
        "type": "Rclone Cloud Storage",
    })
    token_enc_key = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
    results.append({
        "name": "TOKEN_ENCRYPTION_KEY",
        "status": (
            "Configured (Encrypted Token Storage Active)"
            if len(token_enc_key) >= 16 and token_enc_key not in ("your_token_encryption_key_here", "CHANGE_ME")
            else "Auto-Managed (Rclone Protected Config & SQLite Store)"
        ),
        "indicator": "✅" if (len(token_enc_key) >= 16 and token_enc_key not in ("your_token_encryption_key_here", "CHANGE_ME")) else "⚪",
        "type": "Storage Security",
    })

    return results


def get_operating_system_info() -> str:
    """Retrieve friendly operating system and distribution name."""
    system_name = platform.system()
    if system_name == "Linux":
        try:
            if os.path.exists("/etc/os-release"):
                with open("/etc/os-release", "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("PRETTY_NAME="):
                            pretty = line.split("=", 1)[1].strip().strip('"')
                            return f"Linux ({pretty})"
        except Exception:
            pass
        return f"Linux {platform.release()}"
    return f"{system_name} {platform.release()}"


# ------------------------------------------------------------------------------
# Pure Python Fallbacks for Hardware Metrics (When psutil is not installed)
# ------------------------------------------------------------------------------
def get_meminfo_native() -> tuple[Optional[int], Optional[int], Optional[int]]:
    """Reads /proc/meminfo to return (total_bytes, used_bytes, available_bytes)."""
    mem_total: Optional[int] = None
    mem_avail: Optional[int] = None
    mem_free: Optional[int] = None

    if os.path.exists("/proc/meminfo"):
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        key = parts[0].strip()
                        val_str = parts[1].strip().split()[0]
                        if val_str.isdigit():
                            val_bytes = int(val_str) * 1024
                            if key == "MemTotal":
                                mem_total = val_bytes
                            elif key == "MemAvailable":
                                mem_avail = val_bytes
                            elif key == "MemFree":
                                mem_free = val_bytes
            if mem_avail is None:
                mem_avail = mem_free
            if mem_total is not None and mem_avail is not None:
                used = max(0, mem_total - mem_avail)
                return mem_total, used, mem_avail
        except Exception:
            pass

    return None, None, None


def get_process_rss_native() -> Optional[int]:
    """Reads /proc/self/status for VmRSS without fabricating defaults."""
    if os.path.exists("/proc/self/status"):
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        val_str = line.split(":")[1].strip().split()[0]
                        if val_str.isdigit():
                            return int(val_str) * 1024
        except Exception:
            pass
    return None


def get_system_uptime_native() -> Optional[float]:
    """Reads /proc/uptime without fabricating defaults."""
    if os.path.exists("/proc/uptime"):
        try:
            with open("/proc/uptime", "r", encoding="utf-8") as f:
                return float(f.read().split()[0])
        except Exception:
            pass
    return None


# ------------------------------------------------------------------------------
# Full Diagnostic Gathering (Synchronous Worker)
# ------------------------------------------------------------------------------
def collect_all_stats(bot: Any = None) -> dict[str, Any]:
    """
    Gathers all backend metrics accurately.
    Returns real measurements or explicit null / Unavailable.
    Never uses fabricated fallback numbers for RAM or process memory.
    """
    now = time.time()

    # 1. Bot Connection Status
    # Extract status tracker if attached to bot or read from active instance state file
    bot_tracker = getattr(bot, "tracker", None) if bot else None
    sync_status = None
    bot_client_id = None
    bot_invite_url = None
    reconnect_attempts = 0
    wake_in_progress = False
    wake_attempts = 0
    last_wake_time = None
    last_wake_reason = None
    cached_gw_shutdown = None

    state_file = "/tmp/ubagen_bot_state.json"
    if not bot_tracker and os.path.exists(state_file):
        try:
            mtime = os.path.getmtime(state_file)
            if now - mtime < 120.0:
                with open(state_file, "r", encoding="utf-8") as f:
                    cached_dict = json.load(f)
                if isinstance(cached_dict, dict) and "status" in cached_dict:
                    is_ready = bool(cached_dict.get("is_ready", False))
                    is_connected = bool(cached_dict.get("connected", False))
                    bot_status_str = cached_dict.get("status", "disconnected")
                    if bot_status_str == "connected" and not is_ready:
                        bot_status_str = "connecting"
                    latency_ms = cached_dict.get("latency_ms")
                    bot_user_str = cached_dict.get("user", "Disconnected")
                    guild_count = cached_dict.get("guild_count", 0)
                    bot_diag_msg = cached_dict.get("diagnostic_message", "")
                    last_conn = cached_dict.get("last_connected_time")
                    if is_ready and isinstance(last_conn, (int, float)) and last_conn > 0:
                        bot_uptime_sec = max(0.0, now - float(last_conn))
                    else:
                        bot_uptime_sec = cached_dict.get("uptime_seconds")
                    sync_status = cached_dict.get("sync_status")
                    bot_client_id = cached_dict.get("client_id")
                    bot_invite_url = cached_dict.get("invite_url")
                    reconnect_attempts = int(cached_dict.get("reconnect_attempts", 0))
                    wake_in_progress = bool(cached_dict.get("wake_in_progress", False))
                    wake_attempts = int(cached_dict.get("wake_attempts", 0))
                    last_wake_time = cached_dict.get("last_wake_time")
                    last_wake_reason = cached_dict.get("last_wake_reason")
                    cached_gw_shutdown = cached_dict.get("gateway_shutdown_info")
                    bot_tracker = "cached"
        except Exception as e:
            logger.debug("Failed reading state file: %s", e)

    if bot_tracker and bot_tracker != "cached":
        # Dynamically refresh gateway latency and guild count from the live bot instance
        if bot is not None:
            try:
                live_ready = bool(hasattr(bot, "is_ready") and callable(bot.is_ready) and bot.is_ready())
                if live_ready:
                    bot_tracker.connected = True
                    bot_tracker.is_ready = True
                    bot_tracker.status = "connected"
                    if getattr(bot, "user", None):
                        bot_tracker.user = str(bot.user)
                        if getattr(bot.user, "id", None):
                            bot_tracker.client_id = str(bot.user.id)
                    if not bot_tracker.last_connected_time:
                        bot_tracker.last_connected_time = now
                    raw_lat = getattr(bot, "latency", None)
                    if isinstance(raw_lat, (int, float)) and raw_lat >= 0 and raw_lat == raw_lat and raw_lat != float("inf"):
                        bot_tracker.latency_ms = round(float(raw_lat) * 1000, 1)
                    live_guilds = getattr(bot, "guilds", None)
                    if live_guilds is not None:
                        bot_tracker.guild_count = len(live_guilds)
                    if hasattr(bot, "write_state_file") and callable(bot.write_state_file):
                        bot.write_state_file()
            except Exception:
                pass

        bot_dict = bot_tracker.to_dict(now)
        bot_status_str = bot_dict["status"]
        is_ready = bot_dict["is_ready"]
        is_connected = bot_dict["connected"]
        latency_ms = bot_dict["latency_ms"]
        bot_user_str = bot_dict["user"]
        guild_count = bot_dict["guild_count"]
        bot_diag_msg = bot_dict["diagnostic_message"]
        bot_uptime_sec = bot_dict["uptime_seconds"]
        sync_status = bot_dict["sync_status"]
        bot_client_id = bot_dict.get("client_id")
        bot_invite_url = bot_dict.get("invite_url")
        reconnect_attempts = int(bot_dict.get("reconnect_attempts", 0))
        wake_in_progress = bool(bot_dict.get("wake_in_progress", False))
        wake_attempts = int(bot_dict.get("wake_attempts", 0))
        last_wake_time = bot_dict.get("last_wake_time")
        last_wake_reason = bot_dict.get("last_wake_reason")
    elif bot_tracker != "cached":
        is_ready = bool(bot and hasattr(bot, "is_ready") and bot.is_ready())
        is_connected = bool(
            is_ready
            or (bot and hasattr(bot, "is_closed") and callable(bot.is_closed) and not bot.is_closed() and getattr(bot, "ws", None) is not None)
        )
        latency_ms = (
            round(bot.latency * 1000, 1)
            if (is_ready and bot and getattr(bot, "latency", None) is not None)
            else None
        )
        bot_uptime_sec = (now - PROCESS_START_TIME) if is_ready else None
        token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
        is_placeholder = not token or token in ("your_discord_bot_token_here", "MY_DISCORD_BOT_TOKEN", "CHANGE_ME")

        if is_ready and bot and getattr(bot, "user", None):
            bot_user_str = str(bot.user)
            bot_status_str = "connected"
            bot_diag_msg = "Connected to Discord Gateway."
        elif is_connected:
            bot_user_str = "Connecting..."
            bot_status_str = "connecting"
            bot_diag_msg = "Connected to Discord Gateway WebSocket; waiting for bot readiness."
        elif not is_placeholder:
            bot_user_str = "Disconnected"
            bot_status_str = "disconnected"
            bot_diag_msg = "Discord token is configured. Bot service is offline or connecting."
        else:
            bot_user_str = "Not configured"
            bot_status_str = "not_configured"
            bot_diag_msg = "DISCORD_BOT_TOKEN is not configured. Web monitor is active."
        guild_count = len(bot.guilds) if (is_ready and bot and getattr(bot, "guilds", None)) else 0

    if not bot_client_id:
        bot_client_id = extract_discord_client_id(bot)
    if not bot_invite_url and bot_client_id:
        bot_invite_url = generate_discord_invite_url(bot_client_id)
    pub_key_val = os.getenv("DISCORD_PUBLIC_KEY", "").strip()
    interactions_configured = bool(re.fullmatch(r"[0-9a-fA-F]{64}", pub_key_val))

    # 2. CPU Metrics
    cpu_usage_pct = _CPU_SAMPLER.get_percent()
    if psutil is not None:
        logical_cores = psutil.cpu_count(logical=True)
        physical_cores = psutil.cpu_count(logical=False)
    else:
        logical_cores = os.cpu_count()
        physical_cores = max(1, logical_cores // 2) if logical_cores else None

    # 3. System RAM Metrics (No fabricated defaults!)
    system_ram_total: Optional[int] = None
    system_ram_used: Optional[int] = None
    system_ram_avail: Optional[int] = None
    system_ram_free: Optional[int] = None
    system_ram_pct: Optional[float] = None

    if psutil is not None:
        try:
            vm = psutil.virtual_memory()
            system_ram_total = vm.total
            system_ram_used = vm.used
            system_ram_avail = vm.available
            system_ram_free = vm.free
            system_ram_pct = float(vm.percent)
        except Exception as e:
            logger.debug("psutil virtual_memory error: %s", e)

    if system_ram_total is None:
        tot, used, avail = get_meminfo_native()
        if tot is not None and tot > 0:
            system_ram_total = tot
            system_ram_used = used
            system_ram_avail = avail
            system_ram_free = avail
            if used is not None:
                system_ram_pct = round((used / tot) * 100.0, 1)

    # 4. Container Resource Limits (CPU & Memory)
    container_limit_info = detect_container_resource_limits()

    # 5. Process Memory (UBAgen process itself - No fabricated defaults!)
    proc_ram_rss: Optional[int] = None
    proc_ram_pct: Optional[float] = None

    if psutil is not None:
        try:
            current_proc = psutil.Process()
            proc_mem = current_proc.memory_info()
            proc_ram_rss = proc_mem.rss
            raw_pct = current_proc.memory_percent()
            proc_ram_pct = round(float(raw_pct), 2)
        except Exception as e:
            logger.debug("psutil Process memory error: %s", e)

    if proc_ram_rss is None:
        proc_ram_rss = get_process_rss_native()
        if proc_ram_rss is not None and system_ram_total is not None and system_ram_total > 0:
            proc_ram_pct = round((proc_ram_rss / system_ram_total) * 100.0, 2)

    # 6. Disk Usage & Storage Infrastructure Audit
    disk_total: Optional[int] = None
    disk_used: Optional[int] = None
    disk_free: Optional[int] = None
    disk_pct: Optional[float] = None
    storage_audit: Optional[dict[str, Any]] = None

    if get_storage_manager is not None:
        try:
            storage_audit = get_storage_manager().inspect()
        except Exception as e:
            logger.debug("StorageManager.inspect error: %s", e)

    try:
        stat = os.statvfs("/")
        disk_total = stat.f_blocks * stat.f_frsize
        disk_free = stat.f_bavail * stat.f_frsize
        disk_used = max(0, disk_total - disk_free)
        if disk_total > 0:
            disk_pct = round((disk_used / disk_total) * 100.0, 1)
    except Exception as e:
        logger.debug("os.statvfs error: %s", e)

    root_mount = (storage_audit or {}).get("mounts", {}).get("root", {})
    out_mount = (storage_audit or {}).get("mounts", {}).get("video_outputs", {})
    storage_usage = (storage_audit or {}).get("usage", {})
    storage_dirs = (storage_audit or {}).get("directories", {})

    # 7. System & Backend Uptime
    system_uptime_sec: Optional[float] = None
    if psutil is not None:
        try:
            boot_time = psutil.boot_time()
            if boot_time > 0:
                system_uptime_sec = now - boot_time
        except Exception:
            pass

    if system_uptime_sec is None:
        system_uptime_sec = get_system_uptime_native()

    backend_uptime_sec = now - PROCESS_START_TIME

    # 8. External Binaries (FFmpeg, ffprobe, aria2 & rclone)
    ffmpeg_info = check_binary("ffmpeg", "-version")
    ffprobe_info = check_binary("ffprobe", "-version")
    aria2_info = check_binary("aria2c", "--version")
    rclone_info = check_binary("rclone", "version")
    if not rclone_info.get("installed"):
        with suppress(Exception):
            from rclone_manager import get_rclone_manager
            rc_bin = get_rclone_manager().check_rclone_binary()
            if rc_bin.get("installed"):
                rclone_info = {
                    "installed": True,
                    "version": rc_bin.get("version") or "Installed",
                    "path": rc_bin.get("path"),
                }

    # 9. Environment Variables
    env_statuses = check_environment_variables()

    # 10. Gateway Shutdown & Lifecycle Telemetry
    gw_shutdown_info = detect_gateway_shutdown_info(
        bot_tracker=bot_tracker if bot_tracker != "cached" else None,
        now=now,
        backend_uptime=backend_uptime_sec,
    )
    if bot_tracker == "cached" and isinstance(cached_gw_shutdown, dict):
        for k in ("observed_session_duration_sec", "observed_session_duration_str", "summary_display"):
            if cached_gw_shutdown.get(k) is not None and gw_shutdown_info.get(k) in (None, "Ölçülemiyor"):
                gw_shutdown_info[k] = cached_gw_shutdown[k]

    return {
        "status": "online",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "epoch_time": now,
        "bot": {
            "status": bot_status_str,
            "connected": is_connected,
            "is_ready": is_ready,
            "http_alive": True,
            "gateway_connected": bool(is_connected),
            "bot_ready": bool(is_ready),
            "diagnostic_message": bot_diag_msg,
            "latency_ms": latency_ms,
            "user": bot_user_str,
            "guild_count": guild_count,
            "uptime_seconds": bot_uptime_sec,
            "uptime_str": format_duration(bot_uptime_sec),
            "sync_status": sync_status,
            "client_id": bot_client_id,
            "invite_url": bot_invite_url,
            "reconnect_attempts": reconnect_attempts,
            "wake_in_progress": wake_in_progress,
            "wake_attempts": wake_attempts,
            "last_wake_time": last_wake_time,
            "last_wake_reason": last_wake_reason,
            "interactions_endpoint_configured": interactions_configured,
            "interactions_endpoint_path": "/api/discord/interactions",
            "wake_endpoint_path": "/api/discord/wake",
            "gateway_shutdown_info": gw_shutdown_info,
        },
        "compute": {
            "cpu_usage_pct": cpu_usage_pct,
            "logical_cores": logical_cores,
            "physical_cores": physical_cores,
            "os_name": get_operating_system_info(),
            "python_version": platform.python_version(),
            "python_impl": platform.python_implementation(),
            "system_uptime_sec": system_uptime_sec,
            "system_uptime_str": format_duration(system_uptime_sec),
            "backend_uptime_sec": backend_uptime_sec,
            "backend_uptime_str": format_duration(backend_uptime_sec),
        },
        "memory": {
            "system_total_bytes": system_ram_total,
            "system_total_str": format_bytes(system_ram_total),
            "system_used_bytes": system_ram_used,
            "system_used_str": format_bytes(system_ram_used),
            "system_available_bytes": system_ram_avail,
            "system_available_str": format_bytes(system_ram_avail),
            "system_free_bytes": system_ram_free,
            "system_free_str": format_bytes(system_ram_free),
            "system_percent": system_ram_pct,
            "container_limit": container_limit_info,
            "process_rss_bytes": proc_ram_rss,
            "process_rss_str": format_bytes(proc_ram_rss),
            "process_percent": proc_ram_pct,
        },
        "disk": {
            "total_bytes": disk_total,
            "total_str": format_bytes(disk_total),
            "used_bytes": disk_used,
            "used_str": format_bytes(disk_used),
            "free_bytes": disk_free,
            "free_str": format_bytes(disk_free),
            "percent": disk_pct,
            "mount_point": out_mount.get("mount_point") or root_mount.get("mount_point") or "/",
            "fs_type": out_mount.get("fs_type") or root_mount.get("fs_type") or "unknown",
            "writable": bool(out_mount.get("writable", True)),
            "is_persistent": bool((storage_audit or {}).get("is_persistent", False)),
            "persistence_verified": bool((storage_audit or {}).get("persistence_verified", False)),
            "storage_mode": (storage_audit or {}).get("storage_mode", "ephemeral_container_fs"),
            "decision_branch": (storage_audit or {}).get("decision_branch", "B_EPHEMERAL_CONTAINER_STORAGE"),
            "summary": (storage_audit or {}).get("summary", ""),
            "video_outputs_dir": storage_dirs.get("video_outputs_dir"),
            "download_temp_dir": storage_dirs.get("download_temp_dir"),
            "encode_temp_dir": storage_dirs.get("encode_temp_dir"),
            "video_outputs_str": storage_usage.get("video_outputs_str", "0.00 B"),
            "video_outputs_count": storage_usage.get("video_outputs_count", 0),
            "temp_workspace_str": storage_usage.get("temp_workspace_str", "0.00 B"),
            "effective_budget_str": storage_usage.get("effective_budget_str", format_bytes(disk_free)),
        },
        "storage": storage_audit,
        "gateway_shutdown_info": gw_shutdown_info,
        "binaries": {
            "ffmpeg": ffmpeg_info,
            "ffprobe": ffprobe_info,
            "aria2": aria2_info,
            "rclone": rclone_info,
        },
        "env_vars": env_statuses,
    }


# ------------------------------------------------------------------------------
# Compact Character-Bounded Stats Message (< 950 Chars Strict Limit)
# ------------------------------------------------------------------------------
DEFAULT_STATS_MAX_LEN = 950


def format_compact_stats_message(
    stats: dict[str, Any],
    max_len: int = DEFAULT_STATS_MAX_LEN,
    wake_note: str = "",
) -> str:
    """
    Format a high-density, strictly character-bounded (<= max_len, max 950)
    Discord markdown status report.
    Prioritizes:
    1. Gateway connection & latency
    2. Bot & system uptime
    3. CPU & RAM usage (or explicit 'Ölçülemiyor')
    4. Active encode jobs
    5. Health / last error state
    6. Disk storage & essential binaries

    Guaranteed never to exceed max_len (and strictly <= 950) under any combination of inputs or errors.
    """
    effective_max = max(100, min(max_len, DEFAULT_STATS_MAX_LEN))
    clean_wake = (wake_note or "").strip()
    if clean_wake and not clean_wake.endswith("\n"):
        clean_wake += "\n"

    bot_info = stats.get("bot") if isinstance(stats.get("bot"), dict) else {}
    compute = stats.get("compute") if isinstance(stats.get("compute"), dict) else {}
    mem = stats.get("memory") if isinstance(stats.get("memory"), dict) else {}
    disk = stats.get("disk") if isinstance(stats.get("disk"), dict) else {}
    binaries = stats.get("binaries") if isinstance(stats.get("binaries"), dict) else {}

    # 1. Gateway & Bot status
    is_ready = bool(bot_info.get("is_ready"))
    connected = bool(bot_info.get("connected") or bot_info.get("gateway_connected"))
    bot_status = "🟢 Bağlı" if is_ready else ("🟡 Bağlanıyor" if connected else "🔴 Çevrimdışı")
    lat_val = bot_info.get("latency_ms")
    lat_str = f" ({lat_val:.0f}ms ping)" if isinstance(lat_val, (int, float)) and lat_val >= 0 else ""
    bot_user = str(bot_info.get("user") or "").strip()
    bot_acc_str = f" [`{bot_user[:24]}`]" if bot_user and bot_user != "None" else ""

    lines: list[str] = [
        "📊 **UBAgen Sistem Durumu**",
        f"• **Gateway:** {bot_status}{lat_str}{bot_acc_str}",
    ]

    # 2. Uptime
    bot_uptime = bot_info.get("uptime_str") or compute.get("backend_uptime_str") or compute.get("system_uptime_str")
    if bot_uptime:
        lines.append(f"• **Çalışma Süresi:** `{str(bot_uptime)[:32]}`")

    # 3. CPU & RAM (explicitly state when unmeasurable)
    cpu_pct = compute.get("cpu_usage_pct")
    cpu_str = f"%{cpu_pct:.1f}" if isinstance(cpu_pct, (int, float)) else "Ölçülemiyor"
    cores = compute.get("logical_cores")
    cores_str = f" ({cores} çekirdek)" if isinstance(cores, int) else ""

    ram_used = mem.get("system_used_str")
    ram_total = mem.get("system_total_str")
    if ram_used and ram_total:
        ram_pct = mem.get("system_percent")
        ram_pct_str = f" (%{ram_pct:.1f})" if isinstance(ram_pct, (int, float)) else ""
        proc_rss = mem.get("process_rss_str")
        proc_str = f" | Süreç: `{proc_rss}`" if proc_rss else ""
        ram_summary = f"{ram_used}/{ram_total}{ram_pct_str}{proc_str}"
    else:
        ram_summary = "Ölçülemiyor"

    lines.append(f"• **CPU:** `{cpu_str}`{cores_str} | **RAM:** {ram_summary}")

    # 4. Aktif Encode İşleri
    active_jobs = stats.get("active_jobs_count")
    if active_jobs is None and isinstance(stats.get("jobs"), dict):
        active_jobs = stats.get("jobs", {}).get("active_count")
    if active_jobs is None:
        with suppress(Exception):
            from job_store import get_job_store
            active_jobs = len(get_job_store().list_jobs(status="running", limit=50))
    if isinstance(active_jobs, int):
        active_str = f"{active_jobs} iş çalışıyor" if active_jobs > 0 else "0 aktif iş"
        lines.append(f"• **Aktif Encode:** `{active_str}`")

    # 5. Son Hata veya Genel Sağlık Durumu
    last_err = bot_info.get("last_error") or stats.get("last_error") or stats.get("error")
    if last_err:
        clean_err = redact_sensitive_text(str(last_err))[:60]
        lines.append(f"• **Son Hata:** `{clean_err}`")
    else:
        health_label = "🟢 Sağlıklı" if is_ready else ("🟡 Bağlanıyor" if connected else "🔴 Çevrimdışı")
        lines.append(f"• **Genel Sağlık:** {health_label}")

    # 6. Disk & Storage (secondary)
    disk_used = disk.get("used_str")
    disk_total = disk.get("total_str")
    if disk_used and disk_total:
        disk_pct = disk.get("percent")
        disk_pct_str = f" (%{disk_pct:.1f})" if isinstance(disk_pct, (int, float)) else ""
        is_persistent = disk.get("is_persistent")
        persist_label = "Kalıcı" if is_persistent else "Geçici"
        lines.append(f"• **Disk:** {disk_used}/{disk_total}{disk_pct_str} `[{persist_label}]`")

    # 7. Araçlar (secondary)
    ff_ok = bool((binaries.get("ffmpeg") or {}).get("installed"))
    aria_ok = bool((binaries.get("aria2") or {}).get("installed"))
    tool_status = f"FFmpeg: {'✅' if ff_ok else '❌'} | aria2: {'✅' if aria_ok else '❌'}"
    try:
        from rclone_manager import get_rclone_manager
        mgr = get_rclone_manager()
        accs = mgr.list_accounts()
        connected_remotes = [a["remote_name"] for a in accs if a.get("is_active") and a.get("status") == "connected"]
        if connected_remotes:
            tool_status += f" | Rclone: ✅ ({len(connected_remotes)} bağlı)"
        else:
            tool_status += " | Rclone: ⚠️"
    except Exception:
        pass
    lines.append(f"• **Araçlar:** {tool_status}")

    # Combine with optional wake note
    body = "\n".join(lines)
    full_message = f"{clean_wake}{body}" if clean_wake else body

    if len(full_message) <= effective_max:
        return full_message

    # Over length: progressively drop secondary lines from the bottom
    while len(lines) > 2 and len(f"{clean_wake}{chr(10).join(lines)}") > effective_max:
        lines.pop()

    candidate = f"{clean_wake}{chr(10).join(lines)}"
    if len(candidate) <= effective_max:
        return candidate

    return candidate[: effective_max - 3] + "..."


# ------------------------------------------------------------------------------
# Discord Embed Builder
# ------------------------------------------------------------------------------
def build_stats_embed(stats: dict[str, Any]) -> Any:
    """
    Constructs a clear, professional Discord embed conforming to all constraints.
    Separates System RAM, Process RAM, and Container limits.
    """
    if discord is None:
        return None

    bot_info = stats.get("bot") if isinstance(stats.get("bot"), dict) else {}
    compute = stats.get("compute") if isinstance(stats.get("compute"), dict) else {}
    mem = stats.get("memory") if isinstance(stats.get("memory"), dict) else {}
    disk = stats.get("disk") if isinstance(stats.get("disk"), dict) else {}
    binaries = stats.get("binaries") if isinstance(stats.get("binaries"), dict) else {}
    env_vars = stats.get("env_vars") if isinstance(stats.get("env_vars"), list) else []

    is_ready = bool(bot_info.get("is_ready"))
    cpu_pct = compute.get("cpu_usage_pct") or 0
    ram_pct = mem.get("system_percent") or 0

    if not is_ready:
        color = discord.Color.gold()
    elif cpu_pct > 85 or ram_pct > 85:
        color = discord.Color.red()
    else:
        color = discord.Color.from_rgb(88, 101, 242)

    embed = discord.Embed(
        title="UBAgen — Backend Stats",
        description="Diagnostic report for cloud backend infrastructure & resource availability.",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    # 1. Bot Connection Status Field
    if is_ready:
        bot_latency_str = f"{bot_info.get('latency_ms')} ms" if bot_info.get("latency_ms") is not None else "Unavailable"
        bot_status_lines = [
            f"**Status:** 🟢 Connected (`{bot_latency_str}` ping)",
            f"**Account:** `{bot_info.get('user', 'Unknown')}`",
            f"**Guilds:** {bot_info.get('guild_count', 0)} server(s) connected",
            f"**Bot Uptime:** {bot_info.get('uptime_str', 'N/A')}",
            f"**Backend Uptime:** {compute.get('backend_uptime_str', 'N/A')}",
        ]
        if bot_info.get("invite_url"):
            bot_status_lines.append(f"**Invite Bot:** [Add to Discord Server]({bot_info['invite_url']})")
    else:
        bot_status_lines = [
            f"**Status:** 🔴 {str(bot_info.get('status', 'unknown')).replace('_', ' ').title()}",
            f"**Diagnostic:** {str(bot_info.get('diagnostic_message', 'No diagnostic'))[:120]}",
            f"**Backend Uptime:** {compute.get('backend_uptime_str', 'N/A')}",
        ]
        if bot_info.get("invite_url"):
            bot_status_lines.append(f"**Invite Bot:** [Add to Discord Server]({bot_info['invite_url']})")
    embed.add_field(
        name="🤖 Bot Gateway Connection",
        value="\n".join(bot_status_lines),
        inline=False,
    )

    # 2. Host & CPU Compute
    cpu_pct_str = f"{compute['cpu_usage_pct']:.1f}%" if compute["cpu_usage_pct"] is not None else "Unavailable"
    logical_cores = compute["logical_cores"] if compute["logical_cores"] is not None else "Unavailable"
    physical_cores = compute["physical_cores"] if compute["physical_cores"] is not None else "Unavailable"
    sys_uptime_str = compute["system_uptime_str"]

    compute_lines = [
        f"**CPU Usage:** `{cpu_pct_str}`",
        f"**CPU Cores:** {logical_cores} logical ({physical_cores} physical)",
        f"**OS:** {compute['os_name']}",
        f"**Python:** v{compute['python_version']} ({compute['python_impl']})",
        f"**Host Uptime:** {sys_uptime_str}",
    ]
    embed.add_field(
        name="🖥️ Compute & Platform",
        value="\n".join(compute_lines),
        inline=True,
    )

    # 3. Memory & Container Limits
    sys_ram_total = mem["system_total_str"]
    sys_ram_avail = mem["system_available_str"]
    sys_ram_used = mem["system_used_str"]
    sys_ram_pct_str = f"{mem['system_percent']:.1f}%" if mem["system_percent"] is not None else "Unavailable"

    proc_rss_str = mem["process_rss_str"]
    proc_pct_str = f"{mem['process_percent']:.2f}%" if mem["process_percent"] is not None else "Unavailable"

    container_limit = mem["container_limit"]
    if container_limit["is_limited"]:
        container_note = f"⚠️ Limited to `{container_limit['limit_str']}` ({container_limit['type']})"
        if container_limit["cpu_limited"]:
            container_note += f"\nCPU Quota: `{container_limit['cpu_quota_str']}`"
    else:
        container_note = "⚪ No cgroup limit (Full host resources visible)"

    memory_lines = [
        f"**System RAM:** {sys_ram_used} / {sys_ram_total} (`{sys_ram_pct_str}`)",
        f"**RAM Available:** {sys_ram_avail}",
        f"**Process RAM (UBAgen):** `{proc_rss_str}` ({proc_pct_str} of host)",
        f"**Container Quota:** {container_note}",
    ]
    embed.add_field(
        name="💾 Memory & Limits",
        value="\n".join(memory_lines),
        inline=True,
    )

    # 4. Storage (Disk Space & Persistence Classification)
    disk_total_str = disk["total_str"]
    disk_used_str = disk["used_str"]
    disk_free_str = disk["free_str"]
    disk_pct_str = f"{disk['percent']:.1f}%" if disk["percent"] is not None else "Unavailable"
    fs_type = disk.get("fs_type", "unknown")
    mount_pt = disk.get("mount_point", "/")
    is_persistent = disk.get("is_persistent", False)
    persistence_label = (
        f"🟢 Persistent Disk (`{fs_type}` on `{mount_pt}`)"
        if is_persistent
        else f"⚠️ Ephemeral Container FS (`{fs_type}` on `{mount_pt}` — wiped on restart/redeploy)"
    )

    disk_lines = [
        f"**Total Storage:** {disk_total_str}",
        f"**Used / Free:** {disk_used_str} / {disk_free_str} (`{disk_pct_str}` used)",
        f"**Persistence:** {persistence_label}",
        f"**Workspaces:** `video_outputs` ({disk.get('video_outputs_str', '0.00 B')}) • `download_temp` ({disk.get('temp_workspace_str', '0.00 B')})",
    ]
    embed.add_field(
        name="💿 Storage (Disk Volume)",
        value="\n".join(disk_lines),
        inline=False,
    )

    # 5. External Multimedia Binaries
    ffmpeg = binaries["ffmpeg"]
    ffprobe = binaries.get("ffprobe", {"installed": False, "version": "Not installed"})
    aria2 = binaries["aria2"]

    ffmpeg_line = f"**FFmpeg:** {'✅ ' + ffmpeg['version'] if ffmpeg['installed'] else '❌ Not installed'}"
    ffprobe_line = f"**ffprobe:** {'✅ ' + ffprobe['version'] if ffprobe['installed'] else '❌ Not installed'}"
    aria2_line = f"**aria2:** {'✅ ' + aria2['version'] if aria2['installed'] else '❌ Not installed'}"

    embed.add_field(
        name="📦 Multimedia Toolchain",
        value=f"{ffmpeg_line}\n{ffprobe_line}\n{aria2_line}",
        inline=True,
    )

    # 6. Credentials & Environment Check
    env_lines: list[str] = []
    for item in env_vars:
        env_lines.append(f"{item['indicator']} **{item['name']}:** {item['status']}")

    embed.add_field(
        name="🔑 Environment & Configuration",
        value="\n".join(env_lines),
        inline=True,
    )

    embed.set_footer(
        text="UBAgen Diagnostics • Note: Reported resources may represent container allocation rather than host capacity."
    )

    return embed


# ------------------------------------------------------------------------------
# Discord Slash Command Registration
# ------------------------------------------------------------------------------
def register_stats_command(tree: Any, bot: Any) -> None:
    """Registers the /stats slash command with the Discord CommandTree."""
    if discord is None or tree is None:
        logger.warning("discord.py is not available; slash command registration omitted.")
        return

    # Guard against duplicate registration
    if hasattr(tree, "get_command") and tree.get_command("stats") is not None:
        logger.debug("Slash command /stats is already registered. Skipping duplicate registration.")
        return

    @tree.command(name="stats", description="Displays backend infrastructure health, RAM, CPU, and dependencies.")
    async def stats_command(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False, thinking=True)

        try:
            stats = await asyncio.to_thread(collect_all_stats, bot)
            message = format_compact_stats_message(stats, max_len=DEFAULT_STATS_MAX_LEN)
            if len(message) > DEFAULT_STATS_MAX_LEN:
                message = message[: DEFAULT_STATS_MAX_LEN - 3] + "..."
            embed = build_stats_embed(stats)
            await interaction.followup.send(content=message, embed=embed)
        except Exception as exc:
            logger.exception("Error executing /stats command: %s", exc)
            safe_err = redact_sensitive_text(str(exc))[:200]
            err_msg = f"❌ **UBAgen — İstatistik Hatası:** `{type(exc).__name__}: {safe_err}`"[:DEFAULT_STATS_MAX_LEN]
            await interaction.followup.send(content=err_msg)


# ------------------------------------------------------------------------------
# Direct CLI Execution (JSON Output for API / Local Inspection)
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    if "--json" in sys.argv or "-j" in sys.argv:
        all_stats = collect_all_stats(None)
        print(json.dumps(all_stats, default=str))
        sys.exit(0)
    else:
        all_stats = collect_all_stats(None)
        print(json.dumps(all_stats, indent=2, default=str))
