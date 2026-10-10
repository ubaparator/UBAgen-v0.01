"""
UBAgen — stats.py
Diagnostic system statistics collector and Discord '/stats' slash command.

Gathers comprehensive host, container, process, toolchain, and status telemetry
without blocking the Discord event loop or leaking sensitive tokens.
Provides real data for both the Discord slash command and the GET /api/stats endpoint.
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
import sys
import time
from typing import Any, Optional

try:
    import discord
    from discord import app_commands
except ImportError:
    discord = None
    app_commands = None

import psutil

logger = logging.getLogger("ubagen.stats")

# Process start time for uptime calculation
PROCESS_START_TIME = time.time()


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
# Container & Resource Limit Detection (Cgroups v1 & v2)
# ------------------------------------------------------------------------------
def detect_container_memory_limit() -> dict[str, Any]:
    """
    Detect container memory limits (cgroups v1 or v2).
    Distinguishes between visible host RAM and container quota.
    """
    result: dict[str, Any] = {
        "is_limited": False,
        "limit_bytes": None,
        "limit_str": "No container limit enforced",
        "usage_bytes": None,
        "usage_str": None,
        "type": "None",
        "description": "Full host memory visible (No container quota ceiling)",
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
                # Realistic container limit (less than 1 Petabyte)
                if limit_int < 10**15:
                    result["limit_bytes"] = limit_int
                    result["limit_str"] = format_bytes(limit_int)
                    result["is_limited"] = True
                    result["type"] = "cgroups v2"
                    result["description"] = f"{format_bytes(limit_int)} container limit enforced"
            if os.path.exists(cg2_curr_path):
                with open(cg2_curr_path, "r", encoding="utf-8") as f:
                    u_val = f.read().strip()
                if u_val.isdigit():
                    result["usage_bytes"] = int(u_val)
                    result["usage_str"] = format_bytes(int(u_val))
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
                if limit_int < (1024**5):  # Under 1 Petabyte
                    result["limit_bytes"] = limit_int
                    result["limit_str"] = format_bytes(limit_int)
                    result["is_limited"] = True
                    result["type"] = "cgroups v1"
                    result["description"] = f"{format_bytes(limit_int)} container limit enforced"
            if os.path.exists(cg1_curr_path):
                with open(cg1_curr_path, "r", encoding="utf-8") as f:
                    u_val = f.read().strip()
                if u_val.isdigit():
                    result["usage_bytes"] = int(u_val)
                    result["usage_str"] = format_bytes(int(u_val))
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
            timeout=2.5,
            check=False,
        )
        output = proc.stdout.strip() or proc.stderr.strip()
        first_line = output.splitlines()[0] if output else "Installed (Version unknown)"
        if len(first_line) > 55:
            first_line = first_line[:52] + "..."
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


def check_environment_variables() -> list[dict[str, str]]:
    """
    Verify the configuration state of key environment variables
    without exposing secret values or sensitive tokens.
    """
    results: list[dict[str, str]] = []

    # 1. DISCORD_BOT_TOKEN
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if token:
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

    # 2. PORT
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

    # 3. DISCORD_GUILD_ID
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

    # 4. ENVIRONMENT
    env_name = os.getenv("ENVIRONMENT", "production").strip()
    results.append({
        "name": "ENVIRONMENT",
        "status": env_name,
        "indicator": "✅",
        "type": "Configuration",
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
# Full Diagnostic Gathering (Synchronous Worker)
# ------------------------------------------------------------------------------
def collect_all_stats(bot: Any = None) -> dict[str, Any]:
    """
    Gathers all backend metrics synchronously.
    Handles bot being None, starting up, or disconnected gracefully.
    Used by both the Discord /stats slash command and the GET /api/stats endpoint.
    """
    now = time.time()

    # 1. Bot Connection Status
    is_ready = bool(bot and hasattr(bot, "is_ready") and bot.is_ready())
    latency_ms = (
        round(bot.latency * 1000, 1)
        if (is_ready and bot and getattr(bot, "latency", None) is not None)
        else None
    )
    bot_uptime_sec = (now - PROCESS_START_TIME) if is_ready else None
    bot_user_str = (
        str(bot.user)
        if (is_ready and bot and getattr(bot, "user", None))
        else ("Offline / Disconnected" if bot else "Bot Service Standalone / Disconnected")
    )
    guild_count = len(bot.guilds) if (is_ready and bot and getattr(bot, "guilds", None)) else 0

    # 2. CPU Metrics
    try:
        # Non-blocking tiny sample
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
        system_ram_free = vm.free
        system_ram_pct = vm.percent
    except Exception:
        system_ram_total = None
        system_ram_used = None
        system_ram_avail = None
        system_ram_free = None
        system_ram_pct = None

    # 4. Container Cgroups Limit
    container_limit_info = detect_container_memory_limit()

    # 5. Process Memory (UBAgen process itself)
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

    # 7. System & Backend Uptime
    try:
        boot_time = psutil.boot_time()
        system_uptime_sec = now - boot_time if boot_time > 0 else None
    except Exception:
        system_uptime_sec = None

    backend_uptime_sec = now - PROCESS_START_TIME

    # 8. External Binaries (FFmpeg & aria2)
    ffmpeg_info = check_binary("ffmpeg", "-version")
    aria2_info = check_binary("aria2c", "--version")

    # 9. Environment Variables
    env_statuses = check_environment_variables()

    return {
        "status": "online",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "epoch_time": now,
        "bot": {
            "is_ready": is_ready,
            "connected": is_ready,
            "latency_ms": latency_ms,
            "user": bot_user_str,
            "guild_count": guild_count,
            "uptime_seconds": bot_uptime_sec,
            "uptime_str": format_duration(bot_uptime_sec),
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
            "process_percent": round(proc_ram_pct, 2) if proc_ram_pct is not None else None,
        },
        "disk": {
            "total_bytes": disk_total,
            "total_str": format_bytes(disk_total),
            "used_bytes": disk_used,
            "used_str": format_bytes(disk_used),
            "free_bytes": disk_free,
            "free_str": format_bytes(disk_free),
            "percent": disk_pct,
        },
        "binaries": {
            "ffmpeg": ffmpeg_info,
            "aria2": aria2_info,
        },
        "env_vars": env_statuses,
    }


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

    bot_info = stats["bot"]
    compute = stats["compute"]
    mem = stats["memory"]
    disk = stats["disk"]
    binaries = stats["binaries"]
    env_vars = stats["env_vars"]

    is_ready = bot_info["is_ready"]
    cpu_pct = compute["cpu_usage_pct"] or 0
    ram_pct = mem["system_percent"] or 0

    if not is_ready:
        color = discord.Color.gold()
    elif cpu_pct > 85 or ram_pct > 85:
        color = discord.Color.red()
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
            f"**Backend Uptime:** {compute['backend_uptime_str']}",
        ]
    else:
        bot_status_lines = [
            "**Status:** 🔴 Gateway Offline / Disconnected",
            f"**Backend Uptime:** {compute['backend_uptime_str']}",
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

    # 3. Memory & Container Limits (Distinctly separated)
    sys_ram_total = mem["system_total_str"]
    sys_ram_avail = mem["system_available_str"]
    sys_ram_used = mem["system_used_str"]
    sys_ram_pct_str = f"{mem['system_percent']:.1f}%" if mem["system_percent"] is not None else "Unavailable"

    proc_rss_str = mem["process_rss_str"]
    proc_pct_str = f"{mem['process_percent']:.2f}%" if mem["process_percent"] is not None else "Unavailable"

    container_limit = mem["container_limit"]
    if container_limit["is_limited"]:
        container_note = f"⚠️ Limited to `{container_limit['limit_str']}` ({container_limit['type']})"
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
    disk_total_str = disk["total_str"]
    disk_used_str = disk["used_str"]
    disk_free_str = disk["free_str"]
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

    embed.add_field(
        name="🔑 Environment & Configuration",
        value="\n".join(env_lines),
        inline=True,
    )

    embed.set_footer(
        text="UBAgen Diagnostics • Note: Reported RAM & CPU may represent container allocation rather than host capacity."
    )

    return embed


# ------------------------------------------------------------------------------
# Discord Slash Command Registration
# ------------------------------------------------------------------------------
def register_stats_command(tree: Any, bot: Any) -> None:
    """
    Registers the /stats slash command with the Discord CommandTree.
    Uses interaction.response.defer() to prevent Discord 3-second timeouts.
    """
    if discord is None or tree is None:
        logger.warning("discord.py is not available; slash command registration omitted.")
        return

    @tree.command(name="stats", description="Displays backend infrastructure health, RAM, CPU, and dependencies.")
    async def stats_command(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False, thinking=True)

        try:
            # Offload blocking stat collection to thread
            stats = await asyncio.to_thread(collect_all_stats, bot)
            embed = build_stats_embed(stats)
            if embed:
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send("Failed to construct stats embed.")
        except Exception as exc:
            logger.exception("Error executing /stats command: %s", exc)
            error_embed = discord.Embed(
                title="UBAgen — Diagnostics Error",
                description=f"An error occurred while compiling backend statistics: `{type(exc).__name__}: {str(exc)}`",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=error_embed)


# ------------------------------------------------------------------------------
# Direct CLI Execution (JSON Output for API / Local Inspection)
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    if "--json" in sys.argv or "-j" in sys.argv:
        all_stats = collect_all_stats(None)
        print(json.dumps(all_stats, default=str))
        sys.exit(0)
    else:
        # Pretty print for terminal debugging
        all_stats = collect_all_stats(None)
        print(json.dumps(all_stats, indent=2, default=str))
