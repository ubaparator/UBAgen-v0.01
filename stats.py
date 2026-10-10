"""
UBAgen — stats.py
Diagnostic system statistics collector and Discord '/stats' slash command.

Gathers comprehensive host, container, process, toolchain, and credential status
without blocking the Discord event loop or leaking sensitive tokens.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import platform
import shutil
import subprocess
import time
from typing import Any

import discord
from discord import app_commands
import psutil

logger = logging.getLogger("ubagen.stats")

# Timestamp when the UBAgen bot process started
PROCESS_START_TIME = time.time()


# ------------------------------------------------------------------------------
# Formatting Utilities
# ------------------------------------------------------------------------------
def format_bytes(num_bytes: int | float | None) -> str:
    """Format bytes into a human-readable string (KB, MB, GB, TB)."""
    if num_bytes is None or num_bytes < 0:
        return "Unavailable"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:3.2f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} PB"


def format_duration(seconds: float | None) -> str:
    """Format duration in seconds to a human-readable string (e.g., 2d 4h 12m 30s)."""
    if seconds is None or seconds < 0:
        return "Unavailable"
    seconds = int(seconds)
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)

    parts: list[str] = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0 or days > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")

    return " ".join(parts)


# ------------------------------------------------------------------------------
# Container & Resource Limit Detection
# ------------------------------------------------------------------------------
def detect_container_memory_limit() -> dict[str, Any]:
    """
    Detect container memory limits (cgroups v1 or v2).
    Distinguishes between visible host RAM and container quota.
    """
    result: dict[str, Any] = {
        "limit_bytes": None,
        "usage_bytes": None,
        "is_limited": False,
        "type": "None",
        "description": "Host-level execution or unlimited container",
    }

    # 1. Check cgroups v2 (modern Linux kernels & Docker / Podman)
    cg2_max_path = "/sys/fs/cgroup/memory.max"
    cg2_curr_path = "/sys/fs/cgroup/memory.current"
    if os.path.exists(cg2_max_path):
        try:
            with open(cg2_max_path, "r", encoding="utf-8") as f:
                val = f.read().strip()
            if val.isdigit():
                limit_int = int(val)
                # If set to a realistic byte limit (less than 1 Petabyte)
                if limit_int < 10**15:
                    result["limit_bytes"] = limit_int
                    result["is_limited"] = True
                    result["type"] = "cgroups v2"
                    result["description"] = f"{format_bytes(limit_int)} (Container Limit)"
            if os.path.exists(cg2_curr_path):
                with open(cg2_curr_path, "r", encoding="utf-8") as f:
                    u_val = f.read().strip()
                if u_val.isdigit():
                    result["usage_bytes"] = int(u_val)
            if result["is_limited"]:
                return result
        except Exception as e:
            logger.debug("Failed to read cgroups v2 memory limit: %s", e)

    # 2. Check cgroups v1 (legacy Docker / older hosting environments)
    cg1_max_path = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
    cg1_curr_path = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
    if os.path.exists(cg1_max_path):
        try:
            with open(cg1_max_path, "r", encoding="utf-8") as f:
                val = f.read().strip()
            if val.isdigit():
                limit_int = int(val)
                # In cgroups v1, 'unlimited' is usually ~ 9223372036854771712 (0x7ffffffffffff000)
                if limit_int < (1024**5):  # Under 1 Petabyte
                    result["limit_bytes"] = limit_int
                    result["is_limited"] = True
                    result["type"] = "cgroups v1"
                    result["description"] = f"{format_bytes(limit_int)} (Container Limit)"
            if os.path.exists(cg1_curr_path):
                with open(cg1_curr_path, "r", encoding="utf-8") as f:
                    u_val = f.read().strip()
                if u_val.isdigit():
                    result["usage_bytes"] = int(u_val)
            if result["is_limited"]:
                return result
        except Exception as e:
            logger.debug("Failed to read cgroups v1 memory limit: %s", e)

    return result


# ------------------------------------------------------------------------------
# System & Binary Diagnostics
# ------------------------------------------------------------------------------
def check_binary(binary_name: str, version_arg: str = "-version") -> dict[str, Any]:
    """Check whether a binary is installed and retrieve its version safely."""
    path = shutil.which(binary_name)
    if not path:
        return {
            "installed": False,
            "version": "Not installed",
            "path": None,
        }

    try:
        proc = subprocess.run(
            [binary_name, version_arg],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        output = proc.stdout.strip() or proc.stderr.strip()
        first_line = output.splitlines()[0] if output else "Installed (Version unknown)"
        # Clean up overly verbose banner lines
        if len(first_line) > 50:
            first_line = first_line[:47] + "..."
        return {
            "installed": True,
            "version": first_line,
            "path": path,
        }
    except Exception as e:
        logger.debug("Failed to execute %s version check: %s", binary_name, e)
        return {
            "installed": True,
            "version": "Installed (Query error)",
            "path": path,
        }


def check_google_drive_credentials() -> dict[str, Any]:
    """
    Check if Google Drive credentials are configured without exposing secret values.
    Inspects standard environment variables and local credential files.
    """
    # Environment variables commonly used for Google Workspace / Cloud auth
    gdrive_env_keys = [
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_DRIVE_CREDENTIALS",
        "GDRIVE_SERVICE_ACCOUNT_JSON",
        "GDRIVE_CREDENTIALS_FILE",
        "GDRIVE_TOKEN_JSON",
    ]

    for key in gdrive_env_keys:
        val = os.getenv(key, "").strip()
        if not val:
            continue

        # Check if it points to an existing file
        if os.path.exists(val):
            return {
                "configured": True,
                "status": "Configured",
                "detail": f"File present via ${key}",
            }

        # Check if it contains raw JSON content
        if val.startswith("{") and val.endswith("}"):
            try:
                parsed = json.loads(val)
                type_name = parsed.get("type", "credentials")
                return {
                    "configured": True,
                    "status": "Configured",
                    "detail": f"Raw JSON ({type_name}) via ${key}",
                }
            except Exception:
                return {
                    "configured": True,
                    "status": "Configured",
                    "detail": f"JSON payload via ${key}",
                }

        # Value is set but not a file or JSON
        return {
            "configured": True,
            "status": "Configured",
            "detail": f"Set via ${key}",
        }

    # Also check well-known default file locations in project directory
    default_files = ["service-account.json", "credentials.json", "token.json"]
    for filename in default_files:
        if os.path.isfile(filename):
            return {
                "configured": True,
                "status": "Configured",
                "detail": f"Local '{filename}' file found",
            }

    return {
        "configured": False,
        "status": "Not Configured",
        "detail": "Optional for /stats test",
    }


def check_environment_variables() -> list[dict[str, str]]:
    """
    Verify the configuration state of key environment variables
    without printing any secret values or sensitive tokens.
    """
    results: list[dict[str, str]] = []

    # 1. DISCORD_BOT_TOKEN
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if token:
        # Validate minimal Discord token structure without leaking contents
        is_plausible = len(token) > 20 and "." in token
        results.append({
            "name": "DISCORD_BOT_TOKEN",
            "status": "Configured" if is_plausible else "Configured (Check Format)",
            "indicator": "✅" if is_plausible else "⚠️",
            "type": "Required",
        })
    else:
        results.append({
            "name": "DISCORD_BOT_TOKEN",
            "status": "Missing",
            "indicator": "❌",
            "type": "Required",
        })

    # 2. PORT (FastAPI health endpoint)
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

    # 3. DISCORD_GUILD_ID (Fast slash command development sync)
    guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
    if guild_id:
        results.append({
            "name": "DISCORD_GUILD_ID",
            "status": "Configured (Instant Dev Sync)",
            "indicator": "✅",
            "type": "Development",
        })
    else:
        results.append({
            "name": "DISCORD_GUILD_ID",
            "status": "Not Set (Global Sync)",
            "indicator": "⚪",
            "type": "Development",
        })

    # 4. Google Drive Credentials
    gdrive_status = check_google_drive_credentials()
    results.append({
        "name": "GOOGLE_DRIVE",
        "status": gdrive_status["status"],
        "indicator": "✅" if gdrive_status["configured"] else "⚪",
        "type": "Workspace",
    })

    return results


def get_operating_system_info() -> str:
    """Retrieve friendly operating system and distribution name."""
    system_name = platform.system()
    if system_name == "Linux":
        # Attempt to read Linux distribution name
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
# Full Diagnostic Gathering (Synchronous Worker)
# ------------------------------------------------------------------------------
def collect_all_stats(bot: discord.Client) -> dict[str, Any]:
    """
    Gathers all backend metrics synchronously.
    This function must be invoked inside asyncio.to_thread() to avoid blocking
    the Discord gateway event loop.
    """
    now = time.time()

    # 1. Bot Connection Status
    is_ready = bot.is_ready()
    latency_ms = round(bot.latency * 1000, 1) if bot.latency is not None and is_ready else None
    bot_uptime_sec = now - PROCESS_START_TIME
    bot_user_str = str(bot.user) if bot.user else "Unavailable"
    guild_count = len(bot.guilds) if is_ready else 0

    # 2. CPU Metrics
    try:
        # Sample CPU load with a tiny interval (non-blocking because inside worker thread)
        cpu_usage_pct = psutil.cpu_percent(interval=0.1)
    except Exception:
        cpu_usage_pct = None

    logical_cores = psutil.cpu_count(logical=True)
    physical_cores = psutil.cpu_count(logical=False)

    # 3. System RAM Metrics
    try:
        vm = psutil.virtual_memory()
        system_ram_total = vm.total
        system_ram_used = vm.used
        system_ram_avail = vm.available
        system_ram_pct = vm.percent
    except Exception:
        system_ram_total = None
        system_ram_used = None
        system_ram_avail = None
        system_ram_pct = None

    # 4. Container Cgroups Limit
    container_limit_info = detect_container_memory_limit()

    # 5. Process Memory (UBAgen itself)
    try:
        current_proc = psutil.Process()
        proc_mem = current_proc.memory_info()
        proc_ram_rss = proc_mem.rss
        proc_ram_pct = current_proc.memory_percent()
    except Exception:
        proc_ram_rss = None
        proc_ram_pct = None

    # 6. Disk Usage
    try:
        disk = psutil.disk_usage("/")
        disk_total = disk.total
        disk_used = disk.used
        disk_free = disk.free
        disk_pct = disk.percent
    except Exception:
        disk_total = None
        disk_used = None
        disk_free = None
        disk_pct = None

    # 7. System Uptime
    try:
        boot_time = psutil.boot_time()
        system_uptime_sec = now - boot_time if boot_time > 0 else None
    except Exception:
        system_uptime_sec = None

    # 8. External Binaries
    ffmpeg_info = check_binary("ffmpeg", "-version")
    aria2_info = check_binary("aria2c", "--version")

    # 9. Google Drive & Environment Status
    gdrive_status = check_google_drive_credentials()
    env_statuses = check_environment_variables()

    return {
        "bot": {
            "is_ready": is_ready,
            "latency_ms": latency_ms,
            "user": bot_user_str,
            "guild_count": guild_count,
            "uptime_seconds": bot_uptime_sec,
        },
        "compute": {
            "cpu_usage_pct": cpu_usage_pct,
            "logical_cores": logical_cores,
            "physical_cores": physical_cores,
            "os_name": get_operating_system_info(),
            "python_version": platform.python_version(),
            "python_impl": platform.python_implementation(),
            "system_uptime_sec": system_uptime_sec,
        },
        "memory": {
            "system_total": system_ram_total,
            "system_used": system_ram_used,
            "system_available": system_ram_avail,
            "system_percent": system_ram_pct,
            "container_limit": container_limit_info,
            "process_rss": proc_ram_rss,
            "process_percent": proc_ram_pct,
        },
        "disk": {
            "total": disk_total,
            "used": disk_used,
            "free": disk_free,
            "percent": disk_pct,
        },
        "binaries": {
            "ffmpeg": ffmpeg_info,
            "aria2": aria2_info,
        },
        "gdrive": gdrive_status,
        "env_vars": env_statuses,
    }


# ------------------------------------------------------------------------------
# Discord Embed Builder
# ------------------------------------------------------------------------------
def build_stats_embed(stats: dict[str, Any]) -> discord.Embed:
    """
    Constructs a clear, professional Discord embed conforming to all constraints.
    Separates System RAM, Process RAM, and Container limits.
    """
    bot_info = stats["bot"]
    compute = stats["compute"]
    mem = stats["memory"]
    disk = stats["disk"]
    binaries = stats["binaries"]
    gdrive = stats["gdrive"]
    env_vars = stats["env_vars"]

    # Choose embed color based on bot readiness and resource pressure
    is_ready = bot_info["is_ready"]
    cpu_pct = compute["cpu_usage_pct"] or 0
    ram_pct = mem["system_percent"] or 0

    if not is_ready:
        color = discord.Color.red()
    elif cpu_pct > 85 or ram_pct > 85:
        color = discord.Color.gold()
    else:
        color = discord.Color.from_rgb(88, 101, 242)  # Discord Blurple

    embed = discord.Embed(
        title="UBAgen — Backend Stats",
        description="Diagnostic report for cloud backend infrastructure & resource availability.",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    # 1. Bot Connection Status Field
    if is_ready:
        bot_latency_str = f"{bot_info['latency_ms']} ms" if bot_info["latency_ms"] is not None else "Unavailable"
        bot_status_lines = [
            f"**Status:** 🟢 Connected (`{bot_latency_str}` ping)",
            f"**Account:** `{bot_info['user']}`",
            f"**Guilds:** {bot_info['guild_count']} server(s) connected",
            f"**UBAgen Uptime:** {format_duration(bot_info['uptime_seconds'])}",
        ]
    else:
        bot_status_lines = [
            "**Status:** 🔴 Connecting / Disconnected",
            "**UBAgen Uptime:** " + format_duration(bot_info["uptime_seconds"]),
        ]
    embed.add_field(
        name="🤖 Bot Gateway Connection",
        value="\n".join(bot_status_lines),
        inline=False,
    )

    # 2. Host & CPU Compute
    cpu_pct_str = f"{compute['cpu_usage_pct']:.1f}%" if compute["cpu_usage_pct"] is not None else "Unavailable"
    logical_cores = compute["logical_cores"] if compute["logical_cores"] is not None else "Unavailable"
    physical_cores = compute["physical_cores"] if compute["physical_cores"] is not None else "Unavailable"
    sys_uptime_str = format_duration(compute["system_uptime_sec"])

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

    # 3. Memory & Container Limits (Distinctly separated)
    sys_ram_total = format_bytes(mem["system_total"])
    sys_ram_avail = format_bytes(mem["system_available"])
    sys_ram_used = format_bytes(mem["system_used"])
    sys_ram_pct_str = f"{mem['system_percent']:.1f}%" if mem["system_percent"] is not None else "Unavailable"

    proc_rss_str = format_bytes(mem["process_rss"])
    proc_pct_str = f"{mem['process_percent']:.2f}%" if mem["process_percent"] is not None else "Unavailable"

    container_limit = mem["container_limit"]
    if container_limit["is_limited"]:
        container_note = f"⚠️ Limited to `{format_bytes(container_limit['limit_bytes'])}` ({container_limit['type']})"
    else:
        container_note = "⚪ No cgroup limit (Full host memory)"

    memory_lines = [
        f"**System RAM:** {sys_ram_used} / {sys_ram_total} (`{sys_ram_pct_str}`)",
        f"**RAM Available:** {sys_ram_avail}",
        f"**Process RAM (UBAgen):** `{proc_rss_str}` ({proc_pct_str} of host)",
        f"**Container Quota:** {container_note}",
    ]
    embed.add_field(
        name="💾 Memory (RAM Allocation)",
        value="\n".join(memory_lines),
        inline=True,
    )

    # 4. Storage (Disk Space)
    disk_total_str = format_bytes(disk["total"])
    disk_used_str = format_bytes(disk["used"])
    disk_free_str = format_bytes(disk["free"])
    disk_pct_str = f"{disk['percent']:.1f}%" if disk["percent"] is not None else "Unavailable"

    disk_lines = [
        f"**Total Storage:** {disk_total_str}",
        f"**Used / Free:** {disk_used_str} / {disk_free_str} (`{disk_pct_str}` used)",
    ]
    embed.add_field(
        name="💿 Storage (Disk Volume)",
        value="\n".join(disk_lines),
        inline=False,
    )

    # 5. External Multimedia Binaries
    ffmpeg = binaries["ffmpeg"]
    aria2 = binaries["aria2"]

    ffmpeg_line = f"**FFmpeg:** {'✅ ' + ffmpeg['version'] if ffmpeg['installed'] else '❌ Not installed'}"
    aria2_line = f"**aria2:** {'✅ ' + aria2['version'] if aria2['installed'] else '❌ Not installed'}"

    embed.add_field(
        name="📦 Multimedia Toolchain",
        value=f"{ffmpeg_line}\n{aria2_line}",
        inline=True,
    )

    # 6. Credentials & Environment Check (No Secrets Revealed)
    env_lines: list[str] = []
    for item in env_vars:
        env_lines.append(f"{item['indicator']} **{item['name']}:** {item['status']}")

    gdrive_detail = f" ({gdrive['detail']})" if gdrive["configured"] else ""
    # Append gdrive summary if not already covered
    if not any(item["name"] == "GOOGLE_DRIVE" for item in env_vars):
        gdrive_icon = "✅" if gdrive["configured"] else "⚪"
        env_lines.append(f"{gdrive_icon} **Google Drive:** {gdrive['status']}{gdrive_detail}")

    embed.add_field(
        name="🔑 Environment & Credentials",
        value="\n".join(env_lines),
        inline=True,
    )

    # Embed Footer with Container Context Note
    embed.set_footer(
        text="UBAgen Diagnostics • Note: Reported RAM & CPU may represent container allocation rather than host capacity."
    )

    return embed


# ------------------------------------------------------------------------------
# Discord Slash Command Registration
# ------------------------------------------------------------------------------
def register_stats_command(tree: app_commands.CommandTree, bot: discord.Client) -> None:
    """
    Registers the /stats slash command with the Discord CommandTree.
    Uses interaction.response.defer() to prevent Discord 3-second timeouts.
    """

    @tree.command(name="stats", description="Displays backend infrastructure health, RAM, CPU, and dependencies.")
    async def stats_command(interaction: discord.Interaction) -> None:
        # Step 1: Immediately defer interaction to prevent gateway timeout
        await interaction.response.defer(ephemeral=False, thinking=True)

        try:
            # Step 2: Offload blocking system/disk/binary stat collection to worker thread
            stats = await asyncio.to_thread(collect_all_stats, bot)

            # Step 3: Construct Discord embed
            embed = build_stats_embed(stats)

            # Step 4: Send the completed embed as followup
            await interaction.followup.send(embed=embed)
        except Exception as exc:
            logger.exception("Error executing /stats command: %s", exc)
            error_embed = discord.Embed(
                title="UBAgen — Diagnostics Error",
                description=f"An error occurred while compiling backend statistics: `{type(exc).__name__}: {str(exc)}`",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=error_embed)
