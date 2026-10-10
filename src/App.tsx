/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import {
  Server,
  FileCode,
  Copy,
  Check,
  Cpu,
  HardDrive,
  Activity,
  Bot,
  ExternalLink,
  ShieldCheck,
  Terminal,
  HelpCircle,
  Play,
  Layers,
  Sparkles,
  Download,
} from 'lucide-react';

// Pre-packaged file contents for direct 1-click inspection and copy
const PROJECT_FILES: Record<string, { desc: string; lang: string; content: string }> = {
  'main.py': {
    desc: 'Unified entrypoint coordinating Discord.py & FastAPI in one event loop',
    lang: 'python',
    content: `"""
UBAgen — main.py
Entry point for UBAgen Discord Bot and FastAPI Health Service.

Runs both the Discord gateway client (discord.py) and the HTTP health endpoint (FastAPI/uvicorn)
concurrently within a single unified asyncio event loop, with clean signal handling and zero leaked secrets.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import os
import signal
import sys
from typing import Optional

import discord
from discord.ext import commands
from dotenv import load_dotenv
import uvicorn

from health_api import create_health_app
from stats import register_stats_command

# ------------------------------------------------------------------------------
# Logging Configuration
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ubagen.main")


# ------------------------------------------------------------------------------
# UBAgen Bot Class
# ------------------------------------------------------------------------------
class UBAgenBot(commands.Bot):
    """
    Custom Bot instance managing slash command lifecycle and events.
    Uses discord.Intents.default() without requiring privileged intents.
    """

    def __init__(self, guild_id: Optional[int] = None) -> None:
        intents = discord.Intents.default()
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )
        self.dev_guild_id = guild_id

    async def setup_hook(self) -> None:
        """Called automatically before bot login. Registers commands and syncs tree."""
        logger.info("Registering slash commands...")
        register_stats_command(self.tree, self)

        if self.dev_guild_id:
            dev_guild = discord.Object(id=self.dev_guild_id)
            self.tree.copy_global_to(guild=dev_guild)
            synced = await self.tree.sync(guild=dev_guild)
            logger.info(
                "⚡ Instantly synced %d slash command(s) to development guild ID: %d",
                len(synced),
                self.dev_guild_id,
            )
        else:
            synced = await self.tree.sync()
            logger.info(
                "🌐 Synced %d global slash command(s). (Note: Global sync may take up to 1 hour to propagate)",
                len(synced),
            )

    async def on_ready(self) -> None:
        """Fires when Discord gateway connection is established."""
        if self.user:
            logger.info("==================================================")
            logger.info("🎉 UBAgen Bot is ONLINE!")
            logger.info("Logged in as : %s (ID: %s)", self.user, self.user.id)
            logger.info("Connected to : %d guild(s)", len(self.guilds))
            logger.info("Gateway Ping : %.1f ms", self.latency * 1000)
            logger.info("==================================================")

            activity = discord.Activity(
                type=discord.ActivityType.listening,
                name="/stats",
            )
            await self.change_presence(status=discord.Status.online, activity=activity)


# ------------------------------------------------------------------------------
# Server & Bot Concurrency Orchestration
# ------------------------------------------------------------------------------
async def run_fastapi_server(app, host: str, port: int, shutdown_event: asyncio.Event) -> None:
    """Run uvicorn server asynchronously until shutdown_event is triggered."""
    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    async def watch_shutdown():
        await shutdown_event.wait()
        server.should_exit = True

    watcher_task = asyncio.create_task(watch_shutdown())
    try:
        logger.info("Starting FastAPI health endpoint on http://%s:%d/health", host, port)
        await server.serve()
    finally:
        watcher_task.cancel()
        with suppress(asyncio.CancelledError):
            await watcher_task


async def main() -> None:
    """Main application loop coordinating bot and web service."""
    load_dotenv()

    bot_token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not bot_token:
        logger.critical(
            "CRITICAL: 'DISCORD_BOT_TOKEN' environment variable is missing or empty! "
            "Please set DISCORD_BOT_TOKEN in your environment or hosting dashboard."
        )
        sys.exit(1)

    port_str = os.getenv("PORT", "8080").strip()
    try:
        http_port = int(port_str)
    except ValueError:
        logger.warning("Invalid PORT '%s'. Falling back to 8080.", port_str)
        http_port = 8080

    http_host = os.getenv("HOST", "0.0.0.0").strip()

    guild_id_env = os.getenv("DISCORD_GUILD_ID", "").strip()
    dev_guild_id: Optional[int] = None
    if guild_id_env:
        try:
            dev_guild_id = int(guild_id_env)
            logger.info("Development Guild ID detected: %d (Enabling instant slash sync)", dev_guild_id)
        except ValueError:
            logger.warning("Invalid DISCORD_GUILD_ID '%s'. Ignoring development guild sync.", guild_id_env)

    bot = UBAgenBot(guild_id=dev_guild_id)
    health_app = create_health_app(bot=bot)

    shutdown_event = asyncio.Event()

    loop = asyncio.get_running_loop()

    def handle_signal(sig_name: str) -> None:
        logger.info("Received %s signal. Initiating graceful shutdown...", sig_name)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig.name: handle_signal(s))
        except (NotImplementedError, RuntimeError):
            pass

    fastapi_task = asyncio.create_task(
        run_fastapi_server(health_app, host=http_host, port=http_port, shutdown_event=shutdown_event)
    )

    async def start_discord_bot():
        try:
            logger.info("Connecting to Discord Gateway...")
            await bot.start(bot_token)
        except discord.errors.LoginFailure:
            logger.critical(
                "CRITICAL: Failed to log in to Discord. The provided DISCORD_BOT_TOKEN is invalid or expired."
            )
            shutdown_event.set()
        except discord.errors.PrivilegedIntentsRequired:
            logger.critical(
                "CRITICAL: Privileged intents error. Ensure default intents are enabled in code."
            )
            shutdown_event.set()
        except Exception as exc:
            logger.exception("Unexpected error during Discord bot execution: %s", exc)
            shutdown_event.set()

    bot_task = asyncio.create_task(start_discord_bot())

    shutdown_waiter = asyncio.create_task(shutdown_event.wait())
    done, pending = await asyncio.wait(
        [fastapi_task, bot_task, shutdown_waiter],
        return_when=asyncio.FIRST_COMPLETED,
    )

    logger.info("Shutdown initiated. Cleaning up active tasks...")
    shutdown_event.set()

    if not bot.is_closed():
        logger.info("Closing Discord bot connection...")
        try:
            await bot.close()
        except Exception as e:
            logger.debug("Error while closing bot: %s", e)

    for task in pending:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    logger.info("UBAgen backend successfully stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("UBAgen exited.")`,
  },
  'stats.py': {
    desc: 'Discord /stats slash command, non-blocking telemetry & cgroup memory inspection',
    lang: 'python',
    content: `"""
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
PROCESS_START_TIME = time.time()


def format_bytes(num_bytes: int | float | None) -> str:
    if num_bytes is None or num_bytes < 0:
        return "Unavailable"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:3.2f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} PB"


def format_duration(seconds: float | None) -> str:
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


def detect_container_memory_limit() -> dict[str, Any]:
    result: dict[str, Any] = {
        "limit_bytes": None,
        "usage_bytes": None,
        "is_limited": False,
        "type": "None",
        "description": "Host-level execution or unlimited container",
    }
    cg2_max_path = "/sys/fs/cgroup/memory.max"
    cg2_curr_path = "/sys/fs/cgroup/memory.current"
    if os.path.exists(cg2_max_path):
        try:
            with open(cg2_max_path, "r", encoding="utf-8") as f:
                val = f.read().strip()
            if val.isdigit():
                limit_int = int(val)
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

    cg1_max_path = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
    cg1_curr_path = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
    if os.path.exists(cg1_max_path):
        try:
            with open(cg1_max_path, "r", encoding="utf-8") as f:
                val = f.read().strip()
            if val.isdigit():
                limit_int = int(val)
                if limit_int < (1024**5):
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


def check_binary(binary_name: str, version_arg: str = "-version") -> dict[str, Any]:
    path = shutil.which(binary_name)
    if not path:
        return {"installed": False, "version": "Not installed", "path": None}
    try:
        proc = subprocess.run([binary_name, version_arg], capture_output=True, text=True, timeout=3.0, check=False)
        output = proc.stdout.strip() or proc.stderr.strip()
        first_line = output.splitlines()[0] if output else "Installed (Version unknown)"
        if len(first_line) > 50:
            first_line = first_line[:47] + "..."
        return {"installed": True, "version": first_line, "path": path}
    except Exception as e:
        logger.debug("Failed to execute %s version check: %s", binary_name, e)
        return {"installed": True, "version": "Installed (Query error)", "path": path}


def check_google_drive_credentials() -> dict[str, Any]:
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
        if os.path.exists(val):
            return {"configured": True, "status": "Configured", "detail": f"File present via \${key}"}
        if val.startswith("{") and val.endswith("}"):
            try:
                parsed = json.loads(val)
                type_name = parsed.get("type", "credentials")
                return {"configured": True, "status": "Configured", "detail": f"Raw JSON ({type_name}) via \${key}"}
            except Exception:
                return {"configured": True, "status": "Configured", "detail": f"JSON payload via \${key}"}
        return {"configured": True, "status": "Configured", "detail": f"Set via \${key}"}

    default_files = ["service-account.json", "credentials.json", "token.json"]
    for filename in default_files:
        if os.path.isfile(filename):
            return {"configured": True, "status": "Configured", "detail": f"Local '{filename}' file found"}

    return {"configured": False, "status": "Not Configured", "detail": "Optional for /stats test"}


def check_environment_variables() -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
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
        results.append({"name": "DISCORD_BOT_TOKEN", "status": "Missing", "indicator": "❌", "type": "Required"})

    port = os.getenv("PORT", "").strip()
    if port:
        results.append({"name": "PORT", "status": f"{port} (Configured)", "indicator": "✅", "type": "HTTP Server"})
    else:
        results.append({"name": "PORT", "status": "8080 (Default)", "indicator": "⚪", "type": "HTTP Server"})

    guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
    if guild_id:
        results.append({"name": "DISCORD_GUILD_ID", "status": "Configured (Instant Dev Sync)", "indicator": "✅", "type": "Development"})
    else:
        results.append({"name": "DISCORD_GUILD_ID", "status": "Not Set (Global Sync)", "indicator": "⚪", "type": "Development"})

    gdrive_status = check_google_drive_credentials()
    results.append({
        "name": "GOOGLE_DRIVE",
        "status": gdrive_status["status"],
        "indicator": "✅" if gdrive_status["configured"] else "⚪",
        "type": "Workspace",
    })
    return results


def get_operating_system_info() -> str:
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


def collect_all_stats(bot: discord.Client) -> dict[str, Any]:
    now = time.time()
    is_ready = bot.is_ready()
    latency_ms = round(bot.latency * 1000, 1) if bot.latency is not None and is_ready else None
    bot_uptime_sec = now - PROCESS_START_TIME
    bot_user_str = str(bot.user) if bot.user else "Unavailable"
    guild_count = len(bot.guilds) if is_ready else 0

    try:
        cpu_usage_pct = psutil.cpu_percent(interval=0.1)
    except Exception:
        cpu_usage_pct = None

    logical_cores = psutil.cpu_count(logical=True)
    physical_cores = psutil.cpu_count(logical=False)

    try:
        vm = psutil.virtual_memory()
        system_ram_total = vm.total
        system_ram_used = vm.used
        system_ram_avail = vm.available
        system_ram_pct = vm.percent
    except Exception:
        system_ram_total = system_ram_used = system_ram_avail = system_ram_pct = None

    container_limit_info = detect_container_memory_limit()

    try:
        current_proc = psutil.Process()
        proc_mem = current_proc.memory_info()
        proc_ram_rss = proc_mem.rss
        proc_ram_pct = current_proc.memory_percent()
    except Exception:
        proc_ram_rss = proc_ram_pct = None

    try:
        disk = psutil.disk_usage("/")
        disk_total, disk_used, disk_free, disk_pct = disk.total, disk.used, disk.free, disk.percent
    except Exception:
        disk_total = disk_used = disk_free = disk_pct = None

    try:
        boot_time = psutil.boot_time()
        system_uptime_sec = now - boot_time if boot_time > 0 else None
    except Exception:
        system_uptime_sec = None

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
        "disk": {"total": disk_total, "used": disk_used, "free": disk_free, "percent": disk_pct},
        "binaries": {
            "ffmpeg": check_binary("ffmpeg", "-version"),
            "aria2": check_binary("aria2c", "--version"),
        },
        "gdrive": check_google_drive_credentials(),
        "env_vars": check_environment_variables(),
    }


def build_stats_embed(stats: dict[str, Any]) -> discord.Embed:
    bot_info = stats["bot"]
    compute = stats["compute"]
    mem = stats["memory"]
    disk = stats["disk"]
    binaries = stats["binaries"]
    gdrive = stats["gdrive"]
    env_vars = stats["env_vars"]

    is_ready = bot_info["is_ready"]
    cpu_pct = compute["cpu_usage_pct"] or 0
    ram_pct = mem["system_percent"] or 0

    if not is_ready:
        color = discord.Color.red()
    elif cpu_pct > 85 or ram_pct > 85:
        color = discord.Color.gold()
    else:
        color = discord.Color.from_rgb(88, 101, 242)

    embed = discord.Embed(
        title="UBAgen — Backend Stats",
        description="Diagnostic report for cloud backend infrastructure & resource availability.",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    if is_ready:
        latency_str = f"{bot_info['latency_ms']} ms" if bot_info["latency_ms"] is not None else "Unavailable"
        bot_status_lines = [
            f"**Status:** 🟢 Connected (\`{latency_str}\` ping)",
            f"**Account:** \`{bot_info['user']}\`",
            f"**Guilds:** {bot_info['guild_count']} server(s) connected",
            f"**UBAgen Uptime:** {format_duration(bot_info['uptime_seconds'])}",
        ]
    else:
        bot_status_lines = [
            "**Status:** 🔴 Connecting / Disconnected",
            "**UBAgen Uptime:** " + format_duration(bot_info["uptime_seconds"]),
        ]
    embed.add_field(name="🤖 Bot Gateway Connection", value="\\n".join(bot_status_lines), inline=False)

    cpu_pct_str = f"{compute['cpu_usage_pct']:.1f}%" if compute["cpu_usage_pct"] is not None else "Unavailable"
    logical_cores = compute["logical_cores"] if compute["logical_cores"] is not None else "Unavailable"
    physical_cores = compute["physical_cores"] if compute["physical_cores"] is not None else "Unavailable"
    sys_uptime_str = format_duration(compute["system_uptime_sec"])

    compute_lines = [
        f"**CPU Usage:** \`{cpu_pct_str}\`",
        f"**CPU Cores:** {logical_cores} logical ({physical_cores} physical)",
        f"**OS:** {compute['os_name']}",
        f"**Python:** v{compute['python_version']} ({compute['python_impl']})",
        f"**Host Uptime:** {sys_uptime_str}",
    ]
    embed.add_field(name="🖥️ Compute & Platform", value="\\n".join(compute_lines), inline=True)

    sys_ram_total = format_bytes(mem["system_total"])
    sys_ram_avail = format_bytes(mem["system_available"])
    sys_ram_used = format_bytes(mem["system_used"])
    sys_ram_pct_str = f"{mem['system_percent']:.1f}%" if mem["system_percent"] is not None else "Unavailable"

    proc_rss_str = format_bytes(mem["process_rss"])
    proc_pct_str = f"{mem['process_percent']:.2f}%" if mem["process_percent"] is not None else "Unavailable"

    container_limit = mem["container_limit"]
    if container_limit["is_limited"]:
        container_note = f"⚠️ Limited to \`{format_bytes(container_limit['limit_bytes'])}\` ({container_limit['type']})"
    else:
        container_note = "⚪ No cgroup limit (Full host memory)"

    memory_lines = [
        f"**System RAM:** {sys_ram_used} / {sys_ram_total} (\`{sys_ram_pct_str}\`)",
        f"**RAM Available:** {sys_ram_avail}",
        f"**Process RAM (UBAgen):** \`{proc_rss_str}\` ({proc_pct_str} of host)",
        f"**Container Quota:** {container_note}",
    ]
    embed.add_field(name="💾 Memory (RAM Allocation)", value="\\n".join(memory_lines), inline=True)

    disk_total_str = format_bytes(disk["total"])
    disk_used_str = format_bytes(disk["used"])
    disk_free_str = format_bytes(disk["free"])
    disk_pct_str = f"{disk['percent']:.1f}%" if disk["percent"] is not None else "Unavailable"
    disk_lines = [
        f"**Total Storage:** {disk_total_str}",
        f"**Used / Free:** {disk_used_str} / {disk_free_str} (\`{disk_pct_str}\` used)",
    ]
    embed.add_field(name="💿 Storage (Disk Volume)", value="\\n".join(disk_lines), inline=False)

    ffmpeg = binaries["ffmpeg"]
    aria2 = binaries["aria2"]
    ffmpeg_line = f"**FFmpeg:** {'✅ ' + ffmpeg['version'] if ffmpeg['installed'] else '❌ Not installed'}"
    aria2_line = f"**aria2:** {'✅ ' + aria2['version'] if aria2['installed'] else '❌ Not installed'}"
    embed.add_field(name="📦 Multimedia Toolchain", value=f"{ffmpeg_line}\\n{aria2_line}", inline=True)

    env_lines: list[str] = []
    for item in env_vars:
        env_lines.append(f"{item['indicator']} **{item['name']}:** {item['status']}")

    gdrive_detail = f" ({gdrive['detail']})" if gdrive["configured"] else ""
    if not any(item["name"] == "GOOGLE_DRIVE" for item in env_vars):
        gdrive_icon = "✅" if gdrive["configured"] else "⚪"
        env_lines.append(f"{gdrive_icon} **Google Drive:** {gdrive['status']}{gdrive_detail}")

    embed.add_field(name="🔑 Environment & Credentials", value="\\n".join(env_lines), inline=True)

    embed.set_footer(
        text="UBAgen Diagnostics • Note: Reported RAM & CPU may represent container allocation rather than host capacity."
    )
    return embed


def register_stats_command(tree: app_commands.CommandTree, bot: discord.Client) -> None:
    @tree.command(name="stats", description="Displays backend infrastructure health, RAM, CPU, and dependencies.")
    async def stats_command(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False, thinking=True)
        try:
            stats = await asyncio.to_thread(collect_all_stats, bot)
            embed = build_stats_embed(stats)
            await interaction.followup.send(embed=embed)
        except Exception as exc:
            logger.exception("Error executing /stats command: %s", exc)
            error_embed = discord.Embed(
                title="UBAgen — Diagnostics Error",
                description=f"An error occurred while compiling backend statistics: \`{type(exc).__name__}: {str(exc)}\`",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=error_embed)
`,
  },
  'health_api.py': {
    desc: 'Lightweight FastAPI HTTP service with GET /health for container orchestration',
    lang: 'python',
    content: `"""
UBAgen — health_api.py
Lightweight FastAPI application providing HTTP health check endpoints.

Exposes:
- GET /health : Diagnostic status for container orchestration (Docker, Cloud Run, Render, Railway).
- GET /       : Service identification.

Ensures sensitive secrets (e.g. DISCORD_BOT_TOKEN) are never leaked.
"""

from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

SERVICE_START_TIME = time.time()


def create_health_app(bot: Any = None) -> FastAPI:
    app = FastAPI(
        title="UBAgen Health API",
        description="Lightweight HTTP health check service for UBAgen Discord Bot backend",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
    )
    app.state.bot = bot

    @app.get("/health", summary="Backend Health Check")
    async def health_check() -> JSONResponse:
        current_bot = getattr(app.state, "bot", None)
        bot_is_ready = bool(current_bot and current_bot.is_ready())
        latency_ms = (
            round(current_bot.latency * 1000, 2)
            if (bot_is_ready and current_bot.latency is not None)
            else None
        )
        bot_user = str(current_bot.user) if (bot_is_ready and current_bot.user) else None
        uptime_sec = int(time.time() - SERVICE_START_TIME)

        payload = {
            "status": "healthy" if bot_is_ready else "starting",
            "service": "ubagen-backend",
            "http": "alive",
            "discord_bot": {
                "connected": bot_is_ready,
                "latency_ms": latency_ms,
                "user": bot_user,
                "ready": bot_is_ready,
            },
            "uptime_seconds": uptime_sec,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return JSONResponse(content=payload, status_code=200)

    @app.get("/", summary="Root Index")
    async def root_index() -> dict[str, Any]:
        return {
            "service": "UBAgen Discord Bot Backend",
            "status": "online",
            "health_endpoint": "/health",
            "version": "1.0.0",
        }

    return app
`,
  },
  'requirements.txt': {
    desc: 'Production Python package requirements',
    lang: 'plaintext',
    content: `discord.py>=2.4.0
fastapi>=0.115.0
uvicorn[standard]>=0.32.0
psutil>=6.0.0
python-dotenv>=1.0.1
aiohttp>=3.10.0`,
  },
  'Dockerfile': {
    desc: 'Python 3.12-slim production container with FFmpeg, aria2 & non-root user',
    lang: 'dockerfile',
    content: `# ==============================================================================
# UBAgen Discord Bot Backend Dockerfile
# Base: Python 3.12 Slim (Debian Bookworm)
# ==============================================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    DEBIAN_FRONTEND=noninteractive \\
    PORT=8080 \\
    HOST=0.0.0.0

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ffmpeg \\
    aria2 \\
    curl \\
    procps \\
    ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN groupadd -r ubagen && useradd -r -g ubagen -d /app -s /sbin/nologin ubagen

COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip && \\
    pip install --no-cache-dir -r requirements.txt

COPY . /app/
RUN chown -R ubagen:ubagen /app

USER ubagen
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \\
    CMD curl -f http://localhost:\${PORT:-8080}/health || exit 1

CMD ["python", "main.py"]`,
  },
  '.dockerignore': {
    desc: 'Build exclusions to keep container images lean',
    lang: 'plaintext',
    content: `__pycache__
*.pyc
*.pyo
*.pyd
.Python
env/
venv/
.venv/
.env
.git
.gitignore
node_modules/
dist/
.DS_Store
*.log`,
  },
  '.env.example': {
    desc: 'Environment template documented with instructions',
    lang: 'bash',
    content: `# ==============================================================================
# UBAgen Discord Bot Backend — Environment Variables Configuration
# ==============================================================================
# [REQUIRED] Discord Bot Token
DISCORD_BOT_TOKEN="your_discord_bot_token_here"

# [OPTIONAL] Fast Dev Guild Sync for Instant Slash Command Testing
DISCORD_GUILD_ID=""

# [OPTIONAL] HTTP Health Endpoint Configuration
PORT=8080
HOST="0.0.0.0"

# [OPTIONAL] Google Drive Credentials Configuration (Checked in /stats)
GOOGLE_APPLICATION_CREDENTIALS=""

# [OPTIONAL] Environment Identifier
ENVIRONMENT="production"`,
  },
  'README.md': {
    desc: 'Comprehensive deployment, hosting and troubleshooting manual',
    lang: 'markdown',
    content: `# UBAgen — Backend Infrastructure & Diagnostic Service
Production-ready backend for the UBAgen Discord bot and cloud diagnostic health service, built with Python 3.12, discord.py, FastAPI, and Docker.
See the full README.md in the project workspace for comprehensive instructions.`,
  },
};

export default function App() {
  const [selectedFile, setSelectedFile] = useState<string>('main.py');
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<'files' | 'preview' | 'guide'>('files');

  const copyToClipboard = (text: string, key: string) => {
    navigator.clipboard.writeText(text);
    setCopiedKey(key);
    setTimeout(() => setCopiedKey(null), 2000);
  };

  const fileData = PROJECT_FILES[selectedFile];

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans selection:bg-indigo-500 selection:text-white">
      {/* Top Header */}
      <header className="border-b border-slate-800 bg-slate-900/80 backdrop-blur sticky top-0 z-20 px-6 py-4 flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-indigo-600 flex items-center justify-center shadow-lg shadow-indigo-600/30 text-white font-bold">
            <Bot className="w-6 h-6" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-lg font-bold tracking-tight text-white">UBAgen Backend</h1>
              <span className="px-2 py-0.5 text-xs font-semibold rounded bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">
                Python 3.12 Ready
              </span>
              <span className="px-2 py-0.5 text-xs font-medium rounded bg-indigo-500/20 text-indigo-300 border border-indigo-500/30">
                Docker + FastAPI
              </span>
            </div>
            <p className="text-xs text-slate-400">
              Cloud Infrastructure Diagnostic &amp; Discord /stats Command
            </p>
          </div>
        </div>

        {/* View Tabs */}
        <div className="flex items-center bg-slate-800/80 p-1 rounded-xl border border-slate-700/60">
          <button
            onClick={() => setActiveTab('files')}
            className={`px-3 py-1.5 text-xs font-medium rounded-lg flex items-center gap-1.5 transition-all ${
              activeTab === 'files'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-slate-300 hover:text-white'
            }`}
          >
            <FileCode className="w-3.5 h-3.5" />
            Generated Files ({Object.keys(PROJECT_FILES).length})
          </button>
          <button
            onClick={() => setActiveTab('preview')}
            className={`px-3 py-1.5 text-xs font-medium rounded-lg flex items-center gap-1.5 transition-all ${
              activeTab === 'preview'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-slate-300 hover:text-white'
            }`}
          >
            <Bot className="w-3.5 h-3.5" />
            /stats Embed Preview
          </button>
          <button
            onClick={() => setActiveTab('guide')}
            className={`px-3 py-1.5 text-xs font-medium rounded-lg flex items-center gap-1.5 transition-all ${
              activeTab === 'guide'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-slate-300 hover:text-white'
            }`}
          >
            <Server className="w-3.5 h-3.5" />
            Deployment Manual
          </button>
        </div>
      </header>

      {/* Main Content Area */}
      <main className="flex-1 max-w-7xl w-full mx-auto p-4 sm:p-6 flex flex-col gap-6">
        {/* Quick Highlights Banner */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-3.5 flex items-center gap-3">
            <div className="w-9 h-9 rounded-lg bg-indigo-500/10 text-indigo-400 flex items-center justify-center shrink-0">
              <Bot className="w-5 h-5" />
            </div>
            <div>
              <div className="text-xs text-slate-400 font-medium">Discord Command</div>
              <div className="text-sm font-semibold text-slate-200">/stats (Deferred)</div>
            </div>
          </div>

          <div className="bg-slate-900 border border-slate-800 rounded-xl p-3.5 flex items-center gap-3">
            <div className="w-9 h-9 rounded-lg bg-emerald-500/10 text-emerald-400 flex items-center justify-center shrink-0">
              <Activity className="w-5 h-5" />
            </div>
            <div>
              <div className="text-xs text-slate-400 font-medium">Health Endpoint</div>
              <div className="text-sm font-semibold text-slate-200">GET /health (FastAPI)</div>
            </div>
          </div>

          <div className="bg-slate-900 border border-slate-800 rounded-xl p-3.5 flex items-center gap-3">
            <div className="w-9 h-9 rounded-lg bg-sky-500/10 text-sky-400 flex items-center justify-center shrink-0">
              <Cpu className="w-5 h-5" />
            </div>
            <div>
              <div className="text-xs text-slate-400 font-medium">Memory Isolation</div>
              <div className="text-sm font-semibold text-slate-200">Cgroups v1 &amp; v2 Quota</div>
            </div>
          </div>

          <div className="bg-slate-900 border border-slate-800 rounded-xl p-3.5 flex items-center gap-3">
            <div className="w-9 h-9 rounded-lg bg-amber-500/10 text-amber-400 flex items-center justify-center shrink-0">
              <ShieldCheck className="w-5 h-5" />
            </div>
            <div>
              <div className="text-xs text-slate-400 font-medium">Security Guard</div>
              <div className="text-sm font-semibold text-slate-200">Zero Leaked Secrets</div>
            </div>
          </div>
        </div>

        {/* TAB 1: FILES INSPECTOR & 1-CLICK COPY */}
        {activeTab === 'files' && (
          <div className="grid grid-cols-1 lg:grid-cols-12 gap-6 flex-1 items-start">
            {/* File List Sidebar */}
            <div className="lg:col-span-4 bg-slate-900 border border-slate-800 rounded-2xl overflow-hidden flex flex-col shadow-xl">
              <div className="p-4 border-b border-slate-800 bg-slate-900/90 flex items-center justify-between">
                <span className="text-xs font-semibold uppercase tracking-wider text-slate-400 flex items-center gap-2">
                  <Layers className="w-4 h-4 text-indigo-400" />
                  Backend Source Files
                </span>
                <span className="text-[11px] bg-slate-800 text-slate-400 px-2 py-0.5 rounded-full font-mono">
                  8 files
                </span>
              </div>
              <div className="divide-y divide-slate-800/60 p-2">
                {Object.entries(PROJECT_FILES).map(([filename, data]) => {
                  const isSelected = selectedFile === filename;
                  return (
                    <button
                      key={filename}
                      onClick={() => setSelectedFile(filename)}
                      className={`w-full text-left p-3 rounded-xl transition-all flex items-start gap-3 my-0.5 ${
                        isSelected
                          ? 'bg-indigo-600/15 border border-indigo-500/40 text-white'
                          : 'hover:bg-slate-800/70 text-slate-300'
                      }`}
                    >
                      <div
                        className={`mt-0.5 p-1.5 rounded-lg ${
                          isSelected
                            ? 'bg-indigo-600 text-white'
                            : 'bg-slate-800 text-slate-400'
                        }`}
                      >
                        <FileCode className="w-4 h-4" />
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center justify-between">
                          <span className="text-sm font-semibold font-mono text-slate-100 truncate">
                            {filename}
                          </span>
                          {filename === 'main.py' && (
                            <span className="text-[10px] bg-indigo-500/30 text-indigo-300 px-1.5 py-0.2 rounded font-sans">
                              Entry
                            </span>
                          )}
                          {filename === 'stats.py' && (
                            <span className="text-[10px] bg-emerald-500/30 text-emerald-300 px-1.5 py-0.2 rounded font-sans">
                              /stats
                            </span>
                          )}
                        </div>
                        <p className="text-xs text-slate-400 truncate mt-0.5">{data.desc}</p>
                      </div>
                    </button>
                  );
                })}
              </div>
            </div>

            {/* Code Viewer */}
            <div className="lg:col-span-8 bg-slate-900 border border-slate-800 rounded-2xl overflow-hidden shadow-2xl flex flex-col">
              <div className="p-4 border-b border-slate-800 bg-slate-950/70 flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-mono font-bold text-indigo-400 bg-indigo-500/10 px-2.5 py-1 rounded-md border border-indigo-500/20">
                    {selectedFile}
                  </span>
                  <span className="text-xs text-slate-400 hidden sm:inline">
                    {fileData.desc}
                  </span>
                </div>
                <button
                  onClick={() => copyToClipboard(fileData.content, selectedFile)}
                  className="px-3 py-1.5 text-xs font-medium rounded-lg bg-indigo-600 hover:bg-indigo-500 active:scale-95 text-white flex items-center gap-1.5 shadow-md shadow-indigo-600/20 transition-all cursor-pointer"
                >
                  {copiedKey === selectedFile ? (
                    <>
                      <Check className="w-3.5 h-3.5 text-emerald-300" />
                      Copied to Clipboard!
                    </>
                  ) : (
                    <>
                      <Copy className="w-3.5 h-3.5" />
                      Copy {selectedFile}
                    </>
                  )}
                </button>
              </div>

              {/* Code text block */}
              <div className="relative bg-slate-950 p-4 overflow-x-auto max-h-[620px] font-mono text-xs leading-relaxed text-slate-300 border-t border-slate-800/40">
                <pre>
                  <code>{fileData.content}</code>
                </pre>
              </div>
            </div>
          </div>
        )}

        {/* TAB 2: DISCORD EMBED PREVIEW */}
        {activeTab === 'preview' && (
          <div className="max-w-3xl mx-auto w-full flex flex-col gap-4">
            <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 text-xs text-slate-300 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Sparkles className="w-4 h-4 text-indigo-400" />
                <span>
                  Exact visual replica of the Discord <strong>/stats</strong> embed response in Discord Dark Theme.
                </span>
              </div>
              <span className="text-emerald-400 font-mono">200 OK • Deferral Active</span>
            </div>

            {/* Authentic Discord Message Bubble */}
            <div className="bg-[#313338] rounded-xl p-5 border border-[#1e1f22] text-[#dbdee1] font-sans shadow-2xl flex flex-col gap-3">
              {/* Interaction Header */}
              <div className="flex items-center gap-2 text-xs text-[#949ba4]">
                <Bot className="w-3.5 h-3.5 text-[#5865f2]" />
                <span className="font-semibold text-white">ilovenato</span> used{' '}
                <span className="text-[#5865f2] font-semibold">/stats</span>
              </div>

              {/* Bot User Identity */}
              <div className="flex items-center gap-3">
                <div className="w-10 h-10 rounded-full bg-[#5865f2] flex items-center justify-center text-white font-bold text-sm shrink-0">
                  UB
                </div>
                <div>
                  <div className="flex items-center gap-1.5">
                    <span className="text-white font-semibold text-sm">UBAgen</span>
                    <span className="bg-[#5865f2] text-white text-[10px] px-1 py-0.2 rounded font-bold uppercase tracking-wider">
                      BOT
                    </span>
                    <span className="text-xs text-[#949ba4]">Today at 10:35 AM</span>
                  </div>
                </div>
              </div>

              {/* The Embed Box */}
              <div className="ml-0 sm:ml-12 border-l-4 border-[#5865f2] bg-[#2b2d31] rounded-r-lg p-4 flex flex-col gap-4">
                <div>
                  <h3 className="text-white font-bold text-base">UBAgen — Backend Stats</h3>
                  <p className="text-xs text-[#b5bac1] mt-0.5">
                    Diagnostic report for cloud backend infrastructure &amp; resource availability.
                  </p>
                </div>

                {/* Field 1: Bot Gateway */}
                <div className="bg-[#1e1f22]/60 p-3 rounded-lg border border-[#3f4147]/40">
                  <div className="text-xs font-bold text-white mb-1.5 flex items-center gap-1.5">
                    🤖 Bot Gateway Connection
                  </div>
                  <div className="text-xs text-[#dbdee1] space-y-0.5">
                    <div>
                      <strong>Status:</strong> 🟢 Connected (<code className="bg-[#1e1f22] px-1 rounded text-[#5865f2]">41.8 ms</code> ping)
                    </div>
                    <div>
                      <strong>Account:</strong> <code className="bg-[#1e1f22] px-1 rounded text-[#949ba4]">UBAgen#4821 (ID: 12948291048)</code>
                    </div>
                    <div>
                      <strong>Guilds:</strong> 1 server(s) connected
                    </div>
                    <div>
                      <strong>UBAgen Uptime:</strong> 1h 24m 12s
                    </div>
                  </div>
                </div>

                {/* Fields Grid */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  {/* Compute */}
                  <div className="bg-[#1e1f22]/60 p-3 rounded-lg border border-[#3f4147]/40">
                    <div className="text-xs font-bold text-white mb-1.5">🖥️ Compute &amp; Platform</div>
                    <div className="text-xs text-[#dbdee1] space-y-0.5">
                      <div>
                        <strong>CPU Usage:</strong> <code className="bg-[#1e1f22] px-1 rounded text-emerald-400">14.2%</code>
                      </div>
                      <div>
                        <strong>CPU Cores:</strong> 4 logical (2 physical)
                      </div>
                      <div>
                        <strong>OS:</strong> Linux (Debian GNU/Linux 12 bookworm)
                      </div>
                      <div>
                        <strong>Python:</strong> v3.12.2 (CPython)
                      </div>
                      <div>
                        <strong>Host Uptime:</strong> 4d 18h 42m
                      </div>
                    </div>
                  </div>

                  {/* Memory */}
                  <div className="bg-[#1e1f22]/60 p-3 rounded-lg border border-[#3f4147]/40">
                    <div className="text-xs font-bold text-white mb-1.5">💾 Memory (RAM Allocation)</div>
                    <div className="text-xs text-[#dbdee1] space-y-0.5">
                      <div>
                        <strong>System RAM:</strong> 1.62 GB / 4.00 GB (<code className="bg-[#1e1f22] px-1 rounded text-emerald-400">40.5%</code>)
                      </div>
                      <div>
                        <strong>RAM Available:</strong> 2.38 GB free
                      </div>
                      <div>
                        <strong>Process RAM (UBAgen):</strong> <code className="bg-[#1e1f22] px-1 rounded text-indigo-300">44.80 MB</code> (1.12% of host)
                      </div>
                      <div>
                        <strong>Container Quota:</strong> ⚠️ Limited to <code className="bg-[#1e1f22] px-1 rounded text-amber-300">512.00 MB</code> (cgroups v2)
                      </div>
                    </div>
                  </div>
                </div>

                {/* Storage */}
                <div className="bg-[#1e1f22]/60 p-3 rounded-lg border border-[#3f4147]/40">
                  <div className="text-xs font-bold text-white mb-1.5">💿 Storage (Disk Volume)</div>
                  <div className="text-xs text-[#dbdee1]">
                    <strong>Total Storage:</strong> 49.09 GB &bull; <strong>Used / Free:</strong> 12.18 GB / 36.91 GB (<code className="bg-[#1e1f22] px-1 rounded text-emerald-400">24.8%</code> used)
                  </div>
                </div>

                {/* Two Column Binaries and Environment */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  {/* Multimedia Binaries */}
                  <div className="bg-[#1e1f22]/60 p-3 rounded-lg border border-[#3f4147]/40">
                    <div className="text-xs font-bold text-white mb-1.5">📦 Multimedia Toolchain</div>
                    <div className="text-xs text-[#dbdee1] space-y-1">
                      <div>
                        <strong>FFmpeg:</strong> ✅ ffmpeg version 6.1.1-3ubuntu1
                      </div>
                      <div>
                        <strong>aria2:</strong> ✅ aria2 version 1.36.0
                      </div>
                    </div>
                  </div>

                  {/* Credentials Check */}
                  <div className="bg-[#1e1f22]/60 p-3 rounded-lg border border-[#3f4147]/40">
                    <div className="text-xs font-bold text-white mb-1.5">🔑 Environment &amp; Credentials</div>
                    <div className="text-xs text-[#dbdee1] space-y-1">
                      <div>✅ <strong>DISCORD_BOT_TOKEN:</strong> Configured</div>
                      <div>✅ <strong>PORT:</strong> 8080 (Configured)</div>
                      <div>✅ <strong>DISCORD_GUILD_ID:</strong> Configured (Instant Dev Sync)</div>
                      <div>⚪ <strong>Google Drive:</strong> Not Configured (Optional)</div>
                    </div>
                  </div>
                </div>

                {/* Footer Note */}
                <div className="text-[11px] text-[#949ba4] border-t border-[#3f4147]/40 pt-2 flex items-center justify-between">
                  <span>
                    UBAgen Diagnostics &bull; Note: Reported RAM &amp; CPU may represent container allocation rather than host capacity.
                  </span>
                  <span>Today at 10:35 AM</span>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* TAB 3: STEP-BY-STEP DEPLOYMENT & CHECKLIST */}
        {activeTab === 'guide' && (
          <div className="max-w-4xl mx-auto w-full flex flex-col gap-6">
            {/* Quick Steps Card */}
            <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl flex flex-col gap-6">
              <div>
                <h2 className="text-lg font-bold text-white flex items-center gap-2">
                  <Terminal className="w-5 h-5 text-indigo-400" />
                  Quick Deployment Guide (Render, Railway, VPS, Docker)
                </h2>
                <p className="text-xs text-slate-400 mt-1">
                  How to deploy the 8 files to your cloud container service in 3 simple steps.
                </p>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <div className="bg-slate-950 p-4 rounded-xl border border-slate-800/80 flex flex-col gap-2">
                  <div className="w-7 h-7 rounded-lg bg-indigo-600 text-white font-bold text-xs flex items-center justify-center">
                    1
                  </div>
                  <h4 className="text-sm font-semibold text-slate-200">Get Bot Token</h4>
                  <p className="text-xs text-slate-400 leading-relaxed">
                    Open Discord Developer Portal &rarr; Applications &rarr; New App &rarr; Bot &rarr; Reset Token &rarr; Copy.
                  </p>
                </div>

                <div className="bg-slate-950 p-4 rounded-xl border border-slate-800/80 flex flex-col gap-2">
                  <div className="w-7 h-7 rounded-lg bg-indigo-600 text-white font-bold text-xs flex items-center justify-center">
                    2
                  </div>
                  <h4 className="text-sm font-semibold text-slate-200">Invite Bot to Server</h4>
                  <p className="text-xs text-slate-400 leading-relaxed">
                    Under OAuth2 &rarr; URL Generator, check <code className="text-indigo-300">bot</code> and <code className="text-indigo-300">applications.commands</code> scopes.
                  </p>
                </div>

                <div className="bg-slate-950 p-4 rounded-xl border border-slate-800/80 flex flex-col gap-2">
                  <div className="w-7 h-7 rounded-lg bg-indigo-600 text-white font-bold text-xs flex items-center justify-center">
                    3
                  </div>
                  <h4 className="text-sm font-semibold text-slate-200">Set Environment Variable</h4>
                  <p className="text-xs text-slate-400 leading-relaxed">
                    In your hosting dashboard (Render, Railway, Fly.io), set <code className="text-indigo-300">DISCORD_BOT_TOKEN</code>.
                  </p>
                </div>
              </div>

              {/* Docker One-liner */}
              <div className="bg-slate-950 p-4 rounded-xl border border-slate-800">
                <div className="text-xs font-semibold text-slate-300 mb-2 flex items-center justify-between">
                  <span>Deploy with Docker CLI:</span>
                  <button
                    onClick={() =>
                      copyToClipboard(
                        'docker build -t ubagen:latest .\ndocker run -d --restart unless-stopped -p 8080:8080 -e DISCORD_BOT_TOKEN="your_token" ubagen:latest',
                        'docker-cli'
                      )
                    }
                    className="text-indigo-400 hover:text-indigo-300 text-xs flex items-center gap-1"
                  >
                    {copiedKey === 'docker-cli' ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
                    Copy Commands
                  </button>
                </div>
                <pre className="text-xs font-mono text-emerald-400 bg-black/60 p-3 rounded-lg overflow-x-auto">
                  <code>
{`# 1. Build container image
docker build -t ubagen:latest .

# 2. Run container in background
docker run -d \\
  --name ubagen-service \\
  --restart unless-stopped \\
  -p 8080:8080 \\
  -e DISCORD_BOT_TOKEN="your_token_here" \\
  -e DISCORD_GUILD_ID="your_server_id_optional" \\
  ubagen:latest`}
                  </code>
                </pre>
              </div>

              {/* Troubleshooting Reference */}
              <div className="border-t border-slate-800 pt-4 flex flex-col gap-2">
                <h4 className="text-xs font-bold text-slate-300 uppercase tracking-wider">
                  Key Diagnostic Highlights
                </h4>
                <div className="text-xs text-slate-400 space-y-1.5">
                  <div>
                    &bull; <strong className="text-slate-200">Instant vs Global Slash Sync:</strong> Providing{' '}
                    <code className="text-indigo-300">DISCORD_GUILD_ID</code> ensures the <code className="text-indigo-300">/stats</code> command is available in your server immediately upon start. Global registration can take up to 60 minutes.
                  </div>
                  <div>
                    &bull; <strong className="text-slate-200">Health Probes:</strong> Point your cloud provider's health check path to{' '}
                    <code className="text-indigo-300">/health</code> on port 8080.
                  </div>
                  <div>
                    &bull; <strong className="text-slate-200">No Privileged Intents Required:</strong> Slash commands do not read raw messages and do not require Message Content Intent.
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}
      </main>

      {/* Footer */}
      <footer className="border-t border-slate-900 bg-slate-950 px-6 py-4 text-center text-xs text-slate-500">
        UBAgen Backend Infrastructure Engine &bull; Ready for copy-paste deployment to any cloud hosting service.
      </footer>
    </div>
  );
}
