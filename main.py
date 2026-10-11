"""
UBAgen — main.py
Entry point for UBAgen Discord Bot and FastAPI Monitoring Service.

Runs both the Discord gateway client (discord.py) and the HTTP monitoring endpoint (FastAPI/uvicorn)
concurrently within a single unified asyncio event loop, with clean signal handling and zero leaked secrets.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import os
import signal
import socket
import sys
import time
from typing import Any, Callable, Optional

try:
    import discord
    from discord.ext import commands
except ImportError:
    discord = None
    commands = None

try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # Lightweight fallback parser for .env if python-dotenv is not installed
    def _load_simple_env(env_path: str = ".env") -> None:
        if os.path.isfile(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, val = line.split("=", 1)
                        key = key.strip()
                        val = val.strip().strip("'\"")
                        if key and key not in os.environ:
                            os.environ[key] = val
            except Exception:
                pass
    _load_simple_env()

import uvicorn

from encoder_worker import notify_discord_webhook_sync, register_encoder_commands
from google_drive_oauth import (
    REMOVED_DRIVE_SLASH_COMMANDS,
    register_drive_oauth_commands,
    scrub_legacy_drive_commands_from_tree,
)
from health_api import create_health_app
from job_store import discover_and_apply_runtime_config, get_job_store
from rclone_manager import (
    LOG_COMMAND_NAME,
    install_rclone_error_buffer_handler,
    register_log_command,
)
from stats import BotTracker, install_log_buffer_handler, register_stats_command

from storage_manager import get_storage_manager
from subtitle_extract import (
    cleanup_stale_workdirs,
    log_runtime_binaries_status,
    register_subtitle_extract_command,
)

# ------------------------------------------------------------------------------
# Logging Configuration
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
install_log_buffer_handler()
install_rclone_error_buffer_handler()
logger = logging.getLogger("ubagen.main")

CANONICAL_SLASH_COMMAND_NAMES: tuple[str, ...] = (
    "stats",
    "altyazi",
    "encode",
    "encodeal",
    "is_durumu",
    "discord",
    LOG_COMMAND_NAME,
)


def register_all_unique_slash_commands(tree: Any, bot: Optional[Any] = None) -> list[str]:
    """
    Register the canonical UBAgen slash commands on `tree` idempotently,
    remove any legacy `/drive_*` commands, and guarantee zero duplicate definitions.
    """
    if tree is None:
        return []

    scrub_legacy_drive_commands_from_tree(tree)
    register_stats_command(tree, bot)
    register_subtitle_extract_command(tree)
    register_encoder_commands(tree, bot=bot)
    register_log_command(tree, bot=bot)
    register_drive_oauth_commands(tree)
    scrub_legacy_drive_commands_from_tree(tree)

    registered_names: list[str] = []
    if hasattr(tree, "get_commands") and callable(tree.get_commands):
        seen: set[str] = set()
        for cmd in list(tree.get_commands()):
            cmd_name = str(getattr(cmd, "name", "") or "").strip()
            if not cmd_name:
                continue
            if cmd_name in REMOVED_DRIVE_SLASH_COMMANDS or cmd_name in seen:
                if hasattr(tree, "remove_command"):
                    with suppress(Exception):
                        tree.remove_command(cmd_name)
                continue
            seen.add(cmd_name)
            registered_names.append(cmd_name)
    return registered_names


# ------------------------------------------------------------------------------
# UBAgen Bot Class
# ------------------------------------------------------------------------------
if commands is not None:
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
            self.tracker = BotTracker()
            self._guild_commands_synced = False
            self._global_commands_synced = False
            self._synced_guild_ids: set[int] = set()
            self.lifecycle_manager: Any = None

        def write_state_file(self) -> None:
            """Write current bot state to local tmp file for process sharing."""
            try:
                if self.is_ready():
                    raw_lat = getattr(self, "latency", None)
                    if isinstance(raw_lat, (int, float)) and raw_lat >= 0 and raw_lat == raw_lat and raw_lat != float("inf"):
                        self.tracker.latency_ms = round(float(raw_lat) * 1000, 1)
                    if getattr(self, "guilds", None) is not None:
                        self.tracker.guild_count = len(self.guilds)
                with open("/tmp/ubagen_bot_state.json", "w", encoding="utf-8") as f:
                    json.dump(self.tracker.to_dict(), f)
            except Exception:
                pass

        async def setup_hook(self) -> None:
            """Called automatically before bot login. Registers unique commands and syncs tree in a single controlled scope."""
            logger.info("Registering unique slash commands...")
            registered_names = register_all_unique_slash_commands(self.tree, self)
            logger.info("Registered %d unique slash command definition(s): %s", len(registered_names), ", ".join(registered_names))

            # Controlled single-scope sync: avoid registering both guild and global copies simultaneously
            if self.dev_guild_id:
                try:
                    dev_guild = discord.Object(id=self.dev_guild_id)
                    self.tree.clear_commands(guild=dev_guild)
                    self.tree.copy_global_to(guild=dev_guild)
                    synced = await self.tree.sync(guild=dev_guild)
                    self._synced_guild_ids.add(int(self.dev_guild_id))
                    self._guild_commands_synced = True
                    logger.info(
                        "⚡ Instantly synced %d unique slash command(s) to development guild ID: %d",
                        len(synced),
                        self.dev_guild_id,
                    )
                    self.tracker.sync_status = f"Synced {len(synced)} unique command(s) to dev guild {self.dev_guild_id}"
                except discord.errors.Forbidden as exc:
                    logger.warning(
                        "Could not sync slash commands to dev guild ID %d (403 Forbidden: %s). "
                        "The bot might not be in this server or lacks 'applications.commands' scope. "
                        "Attempting global command sync fallback...",
                        self.dev_guild_id,
                        exc,
                    )
                    self.tracker.sync_status = "Dev guild sync: 403 Forbidden (Missing Access). Bot needs applications.commands scope."
                    try:
                        synced = await self.tree.sync()
                        self._global_commands_synced = True
                        logger.info("🌐 Synced %d global slash command(s) as fallback.", len(synced))
                    except Exception as g_exc:
                        logger.warning("Global fallback sync error: %s", g_exc)
                except discord.errors.NotFound as exc:
                    logger.warning(
                        "Dev guild ID %d was not found (404 NotFound: %s). Falling back to global sync...",
                        self.dev_guild_id,
                        exc,
                    )
                    self.tracker.sync_status = "Dev guild not found (404). Falling back to global sync."
                    try:
                        synced = await self.tree.sync()
                        self._global_commands_synced = True
                        logger.info("🌐 Synced %d global slash command(s) as fallback.", len(synced))
                    except Exception as g_exc:
                        logger.warning("Global fallback sync error: %s", g_exc)
                except Exception as exc:
                    logger.warning("Dev guild sync encountered error (%s: %s). Attempting global sync...", type(exc).__name__, exc)
                    self.tracker.sync_status = f"Dev sync warning: {type(exc).__name__}"
                    try:
                        synced = await self.tree.sync()
                        self._global_commands_synced = True
                        logger.info("🌐 Synced %d global slash command(s) as fallback.", len(synced))
                    except Exception as g_exc:
                        logger.warning("Global fallback sync error: %s", g_exc)
            else:
                try:
                    synced = await self.tree.sync()
                    self._global_commands_synced = True
                    logger.info(
                        "🌐 Synced %d unique global slash command(s).",
                        len(synced),
                    )
                    self.tracker.sync_status = f"Synced {len(synced)} unique global command(s)"
                except Exception as exc:
                    logger.warning("Global slash command sync warning (%s): %s", type(exc).__name__, exc)
                    self.tracker.sync_status = f"Global sync warning: {type(exc).__name__}"

            self.write_state_file()

        async def on_connect(self) -> None:
            """Fires when the Discord gateway websocket connection is opened (before guild cache/ready)."""
            logger.info("Connected to Discord Gateway (handshaking & caching guilds...).")
            self.tracker.connected = True
            self.tracker.is_ready = False
            self.tracker.status = "connecting"
            if self.user:
                self.tracker.user = str(self.user)
                self.tracker.client_id = str(self.user.id)
            self.tracker.diagnostic_message = (
                "Connected to Discord Gateway (waiting for guild cache & command sync before ready...)"
            )
            self.write_state_file()

        async def on_interaction(self, interaction: Any) -> None:
            """Record Gateway interaction activity timestamp."""
            self.tracker.last_interaction_time = time.time()
            self.write_state_file()

        async def on_ready(self) -> None:
            """Fires when Discord gateway connection and guild cache are fully established."""
            if self.user:
                logger.info("==================================================")
                logger.info("🎉 UBAgen Bot is ONLINE!")
                logger.info("Logged in as : %s (ID: %s)", self.user, self.user.id)
                logger.info("Connected to : %d guild(s)", len(self.guilds))
                logger.info("Gateway Ping : %.1f ms", self.latency * 1000)
                logger.info("==================================================")

                self.tracker.connected = True
                self.tracker.is_ready = True
                self.tracker.status = "connected"
                self.tracker.user = str(self.user)
                self.tracker.client_id = str(self.user.id)
                self.tracker.invite_url = f"https://discord.com/oauth2/authorize?client_id={self.user.id}&permissions=2147485696&scope=bot%20applications.commands"
                self.tracker.guild_count = len(self.guilds)
                self.tracker.latency_ms = round(self.latency * 1000, 1)
                if not self.tracker.last_connected_time:
                    self.tracker.last_connected_time = time.time()
                self.tracker.diagnostic_message = "Connected to Discord Gateway."
                self.write_state_file()

                activity = discord.Activity(
                    type=discord.ActivityType.listening,
                    name="/stats",
                )
                try:
                    await self.change_presence(status=discord.Status.online, activity=activity)
                except Exception:
                    pass

            # Clean up any legacy duplicate guild-scoped command registrations when global sync is active
            # so commands never appear twice (global + guild) and removed /drive_* commands are purged.
            if not self._guild_commands_synced:
                cleaned_guilds = 0
                global_sync_succeeded = bool(self._global_commands_synced)
                if not global_sync_succeeded and not self.dev_guild_id:
                    try:
                        global_commands = await self.tree.sync()
                        global_sync_succeeded = True
                        self._global_commands_synced = True
                        self.tracker.sync_status = f"Synced {len(global_commands)} unique global command(s)"
                    except Exception as exc:
                        logger.warning("Global command sync from on_ready failed: %s", type(exc).__name__)
                        self.tracker.sync_status = f"Command sync warning: {type(exc).__name__}"

                if global_sync_succeeded:
                    for guild in list(self.guilds):
                        gid = getattr(guild, "id", None)
                        if gid is not None and int(gid) in self._synced_guild_ids:
                            continue
                        try:
                            self.tree.clear_commands(guild=guild)
                            await self.tree.sync(guild=guild)
                            if gid is not None:
                                self._synced_guild_ids.add(int(gid))
                            cleaned_guilds += 1
                        except Exception as exc:
                            logger.debug("Could not clear duplicate guild commands in guild %s: %s", gid, type(exc).__name__)
                    cmd_count = len(self.tree.get_commands())
                    self.tracker.sync_status = (
                        f"Synced {cmd_count} unique global command(s) (deduplicated {cleaned_guilds} server(s))"
                    )
                self._guild_commands_synced = global_sync_succeeded
                self.write_state_file()


        async def on_resumed(self) -> None:
            """Fires when a disconnected gateway session is successfully resumed."""
            logger.info("Discord Gateway session resumed.")
            self.tracker.connected = True
            self.tracker.is_ready = True
            self.tracker.status = "connected"
            self.tracker.diagnostic_message = "Gateway session resumed."
            # Refresh the latency snapshot after reconnect; otherwise the dashboard
            # can keep displaying the last pre-disconnect value.
            latency = getattr(self, "latency", None)
            self.tracker.latency_ms = round(latency * 1000, 1) if latency is not None else None
            self.tracker.guild_count = len(self.guilds)
            self.write_state_file()

        async def on_disconnect(self) -> None:
            """Fires when disconnected from Discord gateway."""
            logger.warning("Disconnected from Discord Gateway. discord.py is reconnecting...")
            self.tracker.connected = False
            self.tracker.is_ready = False
            self.tracker.status = "disconnected"
            self.tracker.diagnostic_message = "Connection temporarily interrupted. Reconnecting..."
            self.write_state_file()
else:
    UBAgenBot = None


# ------------------------------------------------------------------------------
# Configuration & Validation Helpers
# ------------------------------------------------------------------------------
def is_placeholder_bot_token(token: Optional[str]) -> bool:
    """Return True if DISCORD_BOT_TOKEN is empty or a template placeholder."""
    if not token:
        return True
    cleaned = token.strip()
    return not cleaned or cleaned in (
        "your_discord_bot_token_here",
        "MY_DISCORD_BOT_TOKEN",
        "CHANGE_ME",
    )


def parse_dev_guild_id() -> Optional[int]:
    """Parse and validate optional test guild ID (Snowflake format: 17-20 digits)."""
    guild_id_env = os.getenv("DISCORD_GUILD_ID", "").strip()
    if not guild_id_env:
        return None
    if guild_id_env.isdigit() and 17 <= len(guild_id_env) <= 20:
        try:
            dev_guild_id = int(guild_id_env)
            logger.info("Development Guild ID detected: %d (Enabling fast guild command sync)", dev_guild_id)
            return dev_guild_id
        except ValueError:
            return None
    logger.warning(
        "Invalid DISCORD_GUILD_ID format ('%s' is not a valid 17-20 digit Snowflake ID). "
        "Ignoring guild sync and falling back to global sync.",
        guild_id_env[:4] + "..." if len(guild_id_env) > 4 else guild_id_env,
    )
    return None


def validate_bind_host(raw_host: Optional[str], port: int = 8080) -> str:
    """
    Validate HTTP server bind host.
    Prevents crashes if a user mistakenly puts an admin token or URL into HOST.
    Never logs the raw value in case a secret was misconfigured in HOST.
    """
    candidate = (raw_host or "").strip()
    if not candidate:
        return "0.0.0.0"

    if candidate in ("0.0.0.0", "127.0.0.1", "localhost", "::", "::1"):
        return candidate

    # Reject values that look like tokens, URLs, or credentials
    if " " in candidate or "://" in candidate or "/" in candidate or len(candidate) > 64:
        logger.warning(
            "Invalid HOST bind address configured. Ensure HOST is a network bind address "
            "(e.g. '0.0.0.0') and not a token or URL. Use UBAGEN_ADMIN_TOKEN for admin authentication. "
            "Falling back to '0.0.0.0'."
        )
        return "0.0.0.0"

    try:
        socket.getaddrinfo(candidate, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
        return candidate
    except OSError:
        logger.warning(
            "Configured HOST bind address could not be resolved as a local network interface. "
            "Falling back to '0.0.0.0'."
        )
        return "0.0.0.0"


async def _wait_for_any_event(events: list[asyncio.Event], timeout: Optional[float] = None) -> None:
    """Wait until any of the provided asyncio.Events is set, with an optional timeout."""
    active_events = [ev for ev in events if ev is not None]
    if not active_events:
        if timeout is not None:
            await asyncio.sleep(timeout)
        return
    if any(ev.is_set() for ev in active_events):
        return

    wait_tasks = [asyncio.create_task(ev.wait()) for ev in active_events]
    try:
        done, _ = await asyncio.wait(
            wait_tasks,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done and timeout is not None:
            raise asyncio.TimeoutError()
    finally:
        for t in wait_tasks:
            if not t.done():
                t.cancel()
                with suppress(asyncio.CancelledError):
                    await t


# ------------------------------------------------------------------------------
# Resilient Bot Lifecycle Runner
# ------------------------------------------------------------------------------
async def run_discord_bot(
    bot: Any,
    token: str,
    shutdown_event: asyncio.Event,
    stop_event: Optional[asyncio.Event] = None,
    wake_event: Optional[asyncio.Event] = None,
    bot_factory: Optional[Callable[[], Any]] = None,
    on_bot_replaced: Optional[Callable[[Any], None]] = None,
    initial_retry_delay: float = 5.0,
    max_retry_delay: float = 60.0,
) -> None:
    """
    Runs the Discord bot lifecycle with resilient error handling, controlled retry,
    wake-on-command backoff interruption, and safe diagnostics.
    Never exposes tokens or logs secrets.
    Does NOT stop the HTTP monitoring server upon bot error or bot restart.
    """
    def _should_stop() -> bool:
        return shutdown_event.is_set() or (stop_event is not None and stop_event.is_set())

    tracker: BotTracker = getattr(bot, "tracker", None) or BotTracker()
    tracker.status = "connecting"
    tracker.diagnostic_message = "Connecting to Discord Gateway..."
    if hasattr(bot, "write_state_file"):
        bot.write_state_file()

    reconnect_delay = initial_retry_delay
    max_reconnect_delay = max_retry_delay

    while not _should_stop():
        if wake_event is not None and wake_event.is_set():
            wake_event.clear()

        # If a previous connection attempt closed the bot client, recreate a fresh bot instance
        # so slash commands, event handlers, and aiohttp sessions are cleanly reinitialized.
        if hasattr(bot, "is_closed") and callable(bot.is_closed) and bot.is_closed():
            if bot_factory is not None:
                prev_attempts = getattr(tracker, "reconnect_attempts", 0)
                prev_error_time = getattr(tracker, "last_error_time", None)
                recreated = bot_factory()
                if recreated is not None:
                    bot = recreated
                    tracker = getattr(bot, "tracker", None) or BotTracker()
                    tracker.reconnect_attempts = prev_attempts
                    tracker.last_error_time = prev_error_time
                    tracker.status = "connecting"
                    tracker.diagnostic_message = "Reconnecting to Discord Gateway..."
                    if hasattr(bot, "write_state_file"):
                        bot.write_state_file()
                    if on_bot_replaced is not None:
                        on_bot_replaced(bot)
            elif hasattr(bot, "clear") and callable(bot.clear):
                with suppress(Exception):
                    bot.clear()

        try:
            logger.info("Connecting to Discord Gateway...")
            await bot.start(token)
            # A normal return is expected only when shutdown/restart was requested. If the
            # Gateway session ends unexpectedly, keep the HTTP service alive and reconnect.
            if _should_stop():
                break

            tracker.reconnect_attempts = getattr(tracker, "reconnect_attempts", 0) + 1
            tracker.last_error_time = time.time()
            tracker.status = "disconnected"
            tracker.connected = False
            tracker.is_ready = False
            tracker.diagnostic_message = (
                f"Discord Gateway session ended unexpectedly. Reconnecting in {int(reconnect_delay)}s..."
            )
            if hasattr(bot, "write_state_file"):
                with suppress(Exception):
                    bot.write_state_file()
            logger.warning(
                "Discord Gateway session ended without a shutdown request. Retrying in %ds...",
                int(reconnect_delay),
            )
            if hasattr(bot, "close") and callable(bot.close):
                with suppress(Exception):
                    await bot.close()

            wait_events: list[asyncio.Event] = [shutdown_event]
            if stop_event is not None:
                wait_events.append(stop_event)
            if wake_event is not None:
                wait_events.append(wake_event)
            try:
                await _wait_for_any_event(wait_events, timeout=reconnect_delay)
            except asyncio.TimeoutError:
                reconnect_delay = min(reconnect_delay * 1.5, max_reconnect_delay)
                continue
            if _should_stop():
                break
            if wake_event is not None and wake_event.is_set():
                wake_event.clear()
                reconnect_delay = initial_retry_delay
            continue
        except asyncio.CancelledError:
            break
        except Exception as exc:
            if _should_stop():
                break

            tracker.reconnect_attempts = getattr(tracker, "reconnect_attempts", 0) + 1
            tracker.last_error_time = time.time()

            # Check for discord.errors.LoginFailure specifically
            is_login_failure = (
                (discord is not None and isinstance(exc, discord.errors.LoginFailure))
                or "LoginFailure" in type(exc).__name__
                or "improper token" in str(exc).lower()
            )

            if is_login_failure:
                logger.error(
                    "❌ Discord Authentication Failed (LoginFailure): The provided DISCORD_BOT_TOKEN was rejected by Discord. "
                    "The monitoring dashboard remains active. Please verify your token in your hosting environment."
                )
                tracker.status = "invalid_token"
                tracker.connected = False
                tracker.is_ready = False
                tracker.user = "Authentication Failed"
                tracker.diagnostic_message = (
                    "Authentication failed (LoginFailure): DISCORD_BOT_TOKEN was rejected by Discord. "
                    "Please check your bot token in your host configuration."
                )
                if hasattr(bot, "write_state_file"):
                    bot.write_state_file()
                if hasattr(bot, "close") and callable(bot.close):
                    with suppress(Exception):
                        await bot.close()
                # Stop retrying on invalid credentials, but keep waiting until shutdown or manual restart
                await _wait_for_any_event([shutdown_event, stop_event] if stop_event else [shutdown_event])
                break

            # Network-level connection errors
            is_network_err = (
                isinstance(exc, (OSError, TimeoutError))
                or (aiohttp is not None and isinstance(exc, aiohttp.ClientConnectorError))
                or (discord is not None and isinstance(exc, discord.errors.GatewayNotFound))
            )
            if is_network_err:
                tracker.status = "network_error"
                tracker.connected = False
                tracker.is_ready = False
                tracker.diagnostic_message = (
                    f"Network connection error ({type(exc).__name__}). Reconnecting in {int(reconnect_delay)}s..."
                )
                if hasattr(bot, "write_state_file"):
                    bot.write_state_file()
                logger.warning(
                    "Discord network connection error (%s). Retrying in %ds...",
                    type(exc).__name__,
                    int(reconnect_delay),
                )
            else:
                # Discord HTTP / API errors or other errors
                status_code = getattr(exc, "status", None)
                if status_code is not None:
                    tracker.status = "api_error"
                    tracker.diagnostic_message = (
                        f"Discord API error (HTTP {status_code}). Retrying in {int(reconnect_delay)}s..."
                    )
                else:
                    tracker.status = "disconnected"
                    tracker.diagnostic_message = (
                        f"Bot connection error ({type(exc).__name__}). Retrying in {int(reconnect_delay)}s..."
                    )
                tracker.connected = False
                tracker.is_ready = False
                if hasattr(bot, "write_state_file"):
                    bot.write_state_file()
                logger.warning(
                    "Discord connection exception (%s). Retrying in %ds...",
                    type(exc).__name__,
                    int(reconnect_delay),
                )

            # Ensure failed bot instance is cleanly closed before retry recreates it
            if hasattr(bot, "close") and callable(bot.close):
                with suppress(Exception):
                    await bot.close()

            # Wait with exponential backoff before reconnecting, unless shutdown, stop, or wake is signaled
            wait_events: list[asyncio.Event] = [shutdown_event]
            if stop_event is not None:
                wait_events.append(stop_event)
            if wake_event is not None:
                wait_events.append(wake_event)
            try:
                await _wait_for_any_event(
                    wait_events,
                    timeout=reconnect_delay,
                )
                if _should_stop():
                    break
                if wake_event is not None and wake_event.is_set():
                    wake_event.clear()
                    reconnect_delay = initial_retry_delay
                    logger.info("Wake-on-command signal received during backoff; reconnecting to Discord Gateway immediately...")
                    continue
                break
            except asyncio.TimeoutError:
                reconnect_delay = min(reconnect_delay * 1.5, max_reconnect_delay)


# ------------------------------------------------------------------------------
# Bot Lifecycle Manager (Safe In-Process Restart & Wake-on-Command Orchestration)
# ------------------------------------------------------------------------------
class BotLifecycleManager:
    """
    Coordinates the Discord bot lifecycle inside the unified Python process.
    Supports authenticated, lock-protected bot restarts and wake-on-command triggers without stopping FastAPI:
    - Cleanly closes the current Discord gateway session and cancels its task
    - Instantiates a fresh UBAgenBot (re-registering slash commands, events, and BotTracker)
    - Reconnects using the existing DISCORD_BOT_TOKEN with controlled retry on failure
    - Enforces concurrency locking and cooldown throttling across restart and wake requests
    """

    def __init__(
        self,
        bot_factory: Optional[Callable[[], Any]] = None,
        dev_guild_id: Optional[int] = None,
        shutdown_event: Optional[asyncio.Event] = None,
        cooldown_seconds: float = 15.0,
        wake_cooldown_seconds: float = 10.0,
        retry_delay: float = 5.0,
        health_app: Any = None,
    ) -> None:
        self.dev_guild_id = dev_guild_id
        self._custom_bot_factory = bot_factory
        self.shutdown_event = shutdown_event or asyncio.Event()
        self.cooldown_seconds = cooldown_seconds
        self.wake_cooldown_seconds = wake_cooldown_seconds
        self.retry_delay = retry_delay
        self.health_app = health_app

        self._restart_lock = asyncio.Lock()
        self._wake_lock = asyncio.Lock()
        self.is_restarting: bool = False
        self.last_restart_at: float = 0.0
        self.last_wake_at: float = 0.0
        self.last_wake_epoch: Optional[float] = None
        self.last_wake_reason: Optional[str] = None
        self.wake_attempts: int = 0
        self._stop_bot_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self.bot_task: Optional[asyncio.Task] = None

        self.bot: Any = self._create_bot_instance()
        if self.health_app is not None:
            self.attach_health_app(self.health_app)

    def _create_bot_instance(self) -> Any:
        bot_inst = None
        if self._custom_bot_factory is not None:
            bot_inst = self._custom_bot_factory()
        elif UBAgenBot is not None:
            guild_id = self.dev_guild_id if self.dev_guild_id is not None else parse_dev_guild_id()
            bot_inst = UBAgenBot(guild_id=guild_id)
        if bot_inst is not None:
            with suppress(Exception):
                bot_inst.lifecycle_manager = self
        return bot_inst

    def is_gateway_connected(self) -> bool:
        """Return True if the bot instance has an active Discord Gateway connection."""
        if self.bot is None:
            return False
        if hasattr(self.bot, "is_closed") and callable(self.bot.is_closed):
            with suppress(Exception):
                if bool(self.bot.is_closed()):
                    return False
        tracker = getattr(self.bot, "tracker", None)
        return bool(getattr(tracker, "connected", False)) if tracker is not None else False

    def is_bot_ready(self) -> bool:
        """
        Return True ONLY when the Discord bot is both connected to Gateway AND fully ready
        (guilds cached, setup complete, runner task alive). Never reports ready prematurely.
        """
        if not self.is_gateway_connected():
            return False
        if self.bot_task is not None and self.bot_task.done():
            return False
        tracker = getattr(self.bot, "tracker", None)
        tracker_ready = bool(getattr(tracker, "is_ready", False)) if tracker is not None else False
        bot_ready_fn = getattr(self.bot, "is_ready", None)
        live_ready = bool(bot_ready_fn()) if callable(bot_ready_fn) else tracker_ready
        return bool(tracker_ready and live_ready)

    def _update_active_bot(self, new_bot: Any) -> None:
        self.bot = new_bot
        if self.health_app is not None:
            self.health_app.state.bot = new_bot
            self.health_app.state.last_stats = None

    def attach_health_app(self, app: Any) -> None:
        """Bind this lifecycle manager to a FastAPI app instance."""
        self.health_app = app
        app.state.bot = self.bot
        app.state.bot_manager = self
        app.state.restart_bot = self.restart_bot
        app.state.ensure_bot_awake = self.ensure_bot_awake

    def get_bot_token(self) -> str:
        """Retrieve current DISCORD_BOT_TOKEN from environment without mutating or logging it."""
        return os.getenv("DISCORD_BOT_TOKEN", "").strip()

    def start(self) -> Optional[asyncio.Task]:
        """Start the Discord bot background task if token and runtime are available."""
        bot_token = self.get_bot_token()
        if self.bot is not None and not is_placeholder_bot_token(bot_token):
            self._stop_bot_event = asyncio.Event()
            self._wake_event = asyncio.Event()
            self.bot_task = asyncio.create_task(
                run_discord_bot(
                    self.bot,
                    bot_token,
                    self.shutdown_event,
                    stop_event=self._stop_bot_event,
                    wake_event=self._wake_event,
                    bot_factory=self._create_bot_instance,
                    on_bot_replaced=self._update_active_bot,
                    initial_retry_delay=self.retry_delay,
                )
            )
            return self.bot_task

        if is_placeholder_bot_token(bot_token):
            logger.info(
                "NOTICE: 'DISCORD_BOT_TOKEN' is not configured. UBAgen is running in Web Monitor mode. "
                "Discord bot will remain offline until a valid DISCORD_BOT_TOKEN is set in your environment."
            )
            if self.bot and hasattr(self.bot, "tracker"):
                self.bot.tracker.status = "not_configured"
                self.bot.tracker.connected = False
                self.bot.tracker.is_ready = False
                self.bot.tracker.diagnostic_message = "DISCORD_BOT_TOKEN is not configured. Web monitor is active."
                if hasattr(self.bot, "write_state_file"):
                    self.bot.write_state_file()
        elif not self.bot:
            logger.warning("NOTICE: discord.py is not installed; running in Web Monitor mode.")
        return None

    async def stop(self) -> None:
        """Alias for stop_bot() to cleanly stop the managed Discord bot worker."""
        await self.stop_bot()

    async def stop_bot(self) -> None:
        """Cleanly close the active Discord bot connection and stop its task without stopping FastAPI."""
        self._stop_bot_event.set()
        current_bot = self.bot
        current_task = self.bot_task

        if current_bot is not None:
            tracker = getattr(current_bot, "tracker", None)
            if tracker is not None:
                tracker.connected = False
                tracker.is_ready = False
                tracker.status = "connecting"
                tracker.diagnostic_message = "Closing existing Discord Gateway session for restart..."
            if hasattr(current_bot, "write_state_file"):
                with suppress(Exception):
                    current_bot.write_state_file()

            is_closed = False
            if hasattr(current_bot, "is_closed") and callable(current_bot.is_closed):
                with suppress(Exception):
                    is_closed = bool(current_bot.is_closed())

            if not is_closed and hasattr(current_bot, "close") and callable(current_bot.close):
                logger.info("Closing active Discord bot connection...")
                try:
                    await asyncio.wait_for(current_bot.close(), timeout=3.0)
                except asyncio.TimeoutError:
                    logger.warning("Timed out waiting for Discord bot.close(); forcing task cleanup.")
                except Exception as exc:
                    logger.warning("Error while closing Discord bot (%s).", type(exc).__name__)

        if current_task is not None and not current_task.done():
            try:
                await asyncio.wait_for(current_task, timeout=1.5)
            except (asyncio.TimeoutError, Exception):
                current_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await current_task

        self.bot_task = None

    async def ensure_bot_awake(
        self,
        reason: str = "command_trigger",
        wait_for_ready_timeout: float = 0.0,
        wait_for_ready_seconds: Optional[float] = None,
    ) -> tuple[int, dict[str, Any]]:
        """
        Idempotent, single-flight Wake-on-Command handler:
        - If the bot is already connected and ready, returns 200 without restarting it.
        - If a wake or restart is already running, coalesces concurrent requests (`deduplicated=True`)
          so simultaneous commands never start duplicate bot workers.
        - If the bot is sleeping in retry backoff, signals `_wake_event` to reconnect immediately.
        - If the bot task is stopped, closed, or disconnected, starts a fresh bot worker with cooldown protection.
        """
        clean_reason = (reason or "command_trigger").strip()[:80]
        if self.is_bot_ready():
            return 200, {
                "status": "already_ready",
                "woke": False,
                "woken": False,
                "ready": True,
                "deduplicated": False,
                "http_alive": True,
                "discord_connected": True,
                "bot_ready": True,
                "reason": clean_reason,
                "message": "Discord bot is already connected and ready.",
            }

        bot_token = self.get_bot_token()
        if is_placeholder_bot_token(bot_token):
            return 503, {
                "status": "not_configured",
                "woke": False,
                "woken": False,
                "ready": False,
                "http_alive": True,
                "discord_connected": False,
                "bot_ready": False,
                "error": "Cannot wake Discord bot: DISCORD_BOT_TOKEN is not configured on the server.",
            }

        if self.is_restarting or self._restart_lock.locked() or self._wake_lock.locked():
            ready_now = self.is_bot_ready()
            return 202, {
                "status": "wake_in_progress",
                "woke": False,
                "woken": False,
                "ready": ready_now,
                "deduplicated": True,
                "http_alive": True,
                "discord_connected": self.is_gateway_connected(),
                "bot_ready": ready_now,
                "reason": clean_reason,
                "message": "A bot wake or reconnect operation is already in progress.",
            }

        async with self._wake_lock:
            if self.is_bot_ready():
                return 200, {
                    "status": "already_ready",
                    "woke": False,
                    "woken": False,
                    "ready": True,
                    "deduplicated": False,
                    "http_alive": True,
                    "discord_connected": True,
                    "bot_ready": True,
                    "reason": clean_reason,
                    "message": "Discord bot is already connected and ready.",
                }

            tracker = getattr(self.bot, "tracker", None) if self.bot is not None else None
            tracker_status = getattr(tracker, "status", "disconnected") if tracker is not None else "disconnected"
            task_alive = self.bot_task is not None and not self.bot_task.done()

            # If a connection attempt is already actively in flight, coalesce without spawning another worker
            if task_alive and tracker_status == "connecting":
                self._wake_event.set()
                return 202, {
                    "status": "connecting",
                    "woke": False,
                    "woken": False,
                    "ready": False,
                    "deduplicated": True,
                    "http_alive": True,
                    "discord_connected": self.is_gateway_connected(),
                    "bot_ready": False,
                    "reason": clean_reason,
                    "message": "Discord bot is currently connecting to the Gateway.",
                }

            now = time.monotonic()
            if self.last_wake_at > 0 and (now - self.last_wake_at) < self.wake_cooldown_seconds and task_alive:
                remaining = max(1, int(round(self.wake_cooldown_seconds - (now - self.last_wake_at))))
                self._wake_event.set()
                return 429, {
                    "status": "cooldown",
                    "woke": False,
                    "woken": False,
                    "ready": False,
                    "deduplicated": True,
                    "http_alive": True,
                    "discord_connected": self.is_gateway_connected(),
                    "bot_ready": False,
                    "retry_after": remaining,
                    "error": f"Bot wake was triggered recently. Wait {remaining} seconds before trying again.",
                }

            self.last_wake_at = now
            self.last_wake_epoch = time.time()
            self.last_wake_reason = clean_reason
            self.wake_attempts += 1

            # Case A: Runner task is alive and waiting in retry backoff -> interrupt backoff immediately
            if task_alive and tracker_status in ("network_error", "api_error", "disconnected"):
                logger.info("Wake-on-command (%s): signaling active runner to reconnect immediately.", clean_reason)
                if tracker is not None:
                    tracker.status = "connecting"
                    tracker.diagnostic_message = f"Wake-on-command ({clean_reason}): reconnecting to Discord Gateway..."
                self._wake_event.set()
            else:
                # Case B: Bot task is not running, finished, or closed -> start a clean bot worker
                logger.info("Wake-on-command (%s): starting fresh Discord bot worker...", clean_reason)
                await self.stop_bot()
                new_bot = self._create_bot_instance()
                if new_bot is None:
                    return 503, {
                        "status": "unavailable",
                        "woke": False,
                        "woken": False,
                        "ready": False,
                        "http_alive": True,
                        "discord_connected": False,
                        "bot_ready": False,
                        "error": "The bot worker is not currently available to start.",
                    }
                new_tracker = getattr(new_bot, "tracker", None)
                if new_tracker is not None:
                    new_tracker.status = "connecting"
                    new_tracker.connected = False
                    new_tracker.is_ready = False
                    new_tracker.diagnostic_message = (
                        f"Wake-on-command ({clean_reason}) triggered. Connecting to Discord Gateway..."
                    )
                if hasattr(new_bot, "write_state_file"):
                    with suppress(Exception):
                        new_bot.write_state_file()

                self._update_active_bot(new_bot)
                self._stop_bot_event = asyncio.Event()
                self._wake_event = asyncio.Event()
                self.bot_task = asyncio.create_task(
                    run_discord_bot(
                        new_bot,
                        bot_token,
                        self.shutdown_event,
                        stop_event=self._stop_bot_event,
                        wake_event=self._wake_event,
                        bot_factory=self._create_bot_instance,
                        on_bot_replaced=self._update_active_bot,
                        initial_retry_delay=self.retry_delay,
                    )
                )

            effective_wait = (
                float(wait_for_ready_seconds)
                if wait_for_ready_seconds is not None
                else float(wait_for_ready_timeout)
            )
            if effective_wait > 0:
                deadline = time.monotonic() + min(effective_wait, 10.0)
                while time.monotonic() < deadline:
                    if self.is_bot_ready():
                        break
                    await asyncio.sleep(0.05)

            ready_now = self.is_bot_ready()
            return (200 if ready_now else 202), {
                "status": "ready" if ready_now else "waking",
                "woke": True,
                "woken": True,
                "ready": ready_now,
                "deduplicated": False,
                "http_alive": True,
                "discord_connected": self.is_gateway_connected(),
                "bot_ready": ready_now,
                "reason": clean_reason,
                "wake_attempts": self.wake_attempts,
                "message": (
                    "Discord bot woke up and is ready."
                    if ready_now
                    else "Wake-on-command accepted. Discord bot is connecting to Gateway."
                ),
            }

    async def restart_bot(self) -> tuple[int, dict[str, Any]]:
        """
        Safely restart the Discord bot in-process while keeping the HTTP server online.
        Returns (http_status_code, response_dict).
        """
        if self.is_restarting or self._restart_lock.locked():
            retry_after = max(1, int(round(self.cooldown_seconds)))
            return 429, {
                "error": f"A bot restart is already in progress. Wait {retry_after} seconds before trying again.",
                "retry_after": retry_after,
            }

        now = time.monotonic()
        if self.last_restart_at > 0 and (now - self.last_restart_at) < self.cooldown_seconds:
            remaining = max(1, int(round(self.cooldown_seconds - (now - self.last_restart_at))))
            return 429, {
                "error": f"A bot restart was requested recently. Wait {remaining} seconds before trying again.",
                "retry_after": remaining,
            }

        bot_token = self.get_bot_token()
        if is_placeholder_bot_token(bot_token):
            return 503, {
                "error": "Cannot restart Discord bot: DISCORD_BOT_TOKEN is not configured on the server.",
            }

        async with self._restart_lock:
            now = time.monotonic()
            if self.last_restart_at > 0 and (now - self.last_restart_at) < self.cooldown_seconds:
                remaining = max(1, int(round(self.cooldown_seconds - (now - self.last_restart_at))))
                return 429, {
                    "error": f"A bot restart was requested recently. Wait {remaining} seconds before trying again.",
                    "retry_after": remaining,
                }

            self.is_restarting = True
            try:
                logger.info("Authenticated Discord bot restart initiated.")
                await self.stop_bot()

                new_bot = self._create_bot_instance()
                if new_bot is None:
                    return 503, {
                        "error": "The bot worker is not currently available to restart.",
                    }

                tracker = getattr(new_bot, "tracker", None)
                if tracker is not None:
                    tracker.status = "connecting"
                    tracker.connected = False
                    tracker.is_ready = False
                    tracker.diagnostic_message = "Bot restart requested. Reconnecting to Discord Gateway..."
                if hasattr(new_bot, "write_state_file"):
                    with suppress(Exception):
                        new_bot.write_state_file()

                self._update_active_bot(new_bot)

                self._stop_bot_event = asyncio.Event()
                self._wake_event = asyncio.Event()
                self.bot_task = asyncio.create_task(
                    run_discord_bot(
                        new_bot,
                        bot_token,
                        self.shutdown_event,
                        stop_event=self._stop_bot_event,
                        wake_event=self._wake_event,
                        bot_factory=self._create_bot_instance,
                        on_bot_replaced=self._update_active_bot,
                        initial_retry_delay=self.retry_delay,
                    )
                )
                self.last_restart_at = time.monotonic()
                logger.info("Discord bot instance recreated and reconnection task started.")
                return 202, {
                    "status": "restarting",
                    "message": "Bot restart requested. The worker should reconnect shortly.",
                }
            except Exception as exc:
                logger.exception("Unexpected error during Discord bot restart (%s).", type(exc).__name__)
                return 500, {
                    "error": "Could not restart the Discord bot due to an internal error.",
                }
            finally:
                self.is_restarting = False


# ------------------------------------------------------------------------------
# Server & Bot Concurrency Orchestration
# ------------------------------------------------------------------------------
async def run_fastapi_server(app: Any, host: str, port: int, shutdown_event: asyncio.Event) -> None:
    """Run uvicorn server asynchronously until shutdown_event is triggered."""
    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)

    async def watch_shutdown():
        await shutdown_event.wait()
        server.should_exit = True

    watcher_task = asyncio.create_task(watch_shutdown())
    try:
        logger.info("Starting UBAgen Monitor on http://%s:%d/api/stats", host, port)
        await server.serve()
    finally:
        watcher_task.cancel()
        with suppress(asyncio.CancelledError):
            await watcher_task


def _is_port_available(host: str, port: int) -> bool:
    """Return True if TCP `port` can be bound on `host`."""
    bind_addr = "0.0.0.0" if host in ("", "0.0.0.0", "::") else host
    with suppress(Exception):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((bind_addr, port))
            return True
    return False


def resolve_safe_http_port(host: str, preferred_port: int) -> int:
    """
    Ensure the HTTP monitoring port is available before launching uvicorn.
    Prevents [Errno 98] Address already in use crashes when NGINX_PORT=8080
    or another companion process is already bound to the default port.
    """
    if _is_port_available(host, preferred_port):
        return preferred_port

    for candidate in (5050, 5051, 5052, 8081, 8082):
        if candidate != preferred_port and _is_port_available(host, candidate):
            logger.warning(
                "Port %d is already in use on %s; falling back to available port %d.",
                preferred_port,
                host,
                candidate,
            )
            return candidate
    return preferred_port


async def run_job_retention_sweeper_loop(
    shutdown_event: asyncio.Event,
    interval_seconds: float = 60.0,
) -> None:
    """
    Server-side scheduled background task enforcing the 7200-second (2-hour) retention policy:
    - Actively deletes expired video outputs from Google Cloud Storage and revokes download links.
    - Detects crashed/stale worker jobs and marks them as failed with Discord notifications.
    """
    while not shutdown_event.is_set():
        try:
            store = get_job_store()
            cleanup_info = await asyncio.to_thread(store.cleanup_expired_jobs)
            if cleanup_info.get("expired_jobs_processed", 0) > 0:
                logger.info(
                    "7200s Retention Sweeper: purged %d expired job(s) (%d GCS object(s) deleted).",
                    cleanup_info["expired_jobs_processed"],
                    cleanup_info["gcs_objects_deleted"],
                )
            recovered = await asyncio.to_thread(store.recover_stale_running_jobs)
            for crashed_job in recovered:
                logger.warning("Recovered crashed/stale worker job: %s", crashed_job.job_id)
                await asyncio.to_thread(notify_discord_webhook_sync, crashed_job)
        except Exception as exc:
            logger.debug("Job retention sweeper error: %s", exc)

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
            break
        except asyncio.TimeoutError:
            continue


async def main() -> None:
    """Main application loop coordinating bot and web service."""
    with suppress(Exception):
        auto_cfg = discover_and_apply_runtime_config()
        if auto_cfg.get("auto_applied_keys"):
            logger.info(
                "Auto-discovered and applied runtime configuration keys: %s",
                ", ".join(auto_cfg["auto_applied_keys"]),
            )

    ubagen_port_env = os.getenv("UBAGEN_HTTP_PORT", "").strip()

    raw_port_env = os.getenv("PORT", "8080").strip()
    nginx_port_env = os.getenv("NGINX_PORT", "").strip()

    if ubagen_port_env:
        port_str = ubagen_port_env
    elif nginx_port_env and nginx_port_env == raw_port_env:
        # Inside AI Studio container where nginx occupies PORT=8080, use companion port 5050
        port_str = "5050"
    else:
        port_str = raw_port_env

    try:
        http_port = int(port_str)
    except ValueError:
        logger.warning("Invalid PORT '%s'. Falling back to 8080.", port_str)
        http_port = 8080

    http_host = validate_bind_host(os.getenv("HOST", "0.0.0.0"), port=http_port)
    http_port = resolve_safe_http_port(http_host, http_port)
    dev_guild_id = parse_dev_guild_id()

    # Clean up any leftover temporary job directories, audit storage, and verify aria2c, ffmpeg, and ffprobe
    with suppress(Exception):
        await asyncio.to_thread(cleanup_stale_workdirs)
    with suppress(Exception):
        storage_report = await asyncio.to_thread(get_storage_manager().inspect)
        logger.info(
            "Storage Audit [%s]: mode=%s, persistent=%s, outputs=%s, temp=%s",
            storage_report.get("decision_branch"),
            storage_report.get("storage_mode"),
            storage_report.get("is_persistent"),
            storage_report.get("directories", {}).get("video_outputs_dir"),
            storage_report.get("directories", {}).get("download_temp_dir"),
        )
    with suppress(Exception):
        await asyncio.to_thread(log_runtime_binaries_status, True)

    shutdown_event = asyncio.Event()
    bot_manager = BotLifecycleManager(
        dev_guild_id=dev_guild_id,
        shutdown_event=shutdown_event,
    )
    health_app = create_health_app(bot=bot_manager.bot, bot_manager=bot_manager)

    # OS signal handlers for graceful shutdown (SIGINT, SIGTERM)
    loop = asyncio.get_running_loop()

    def handle_signal(sig_name: str) -> None:
        logger.info("Received %s signal. Initiating graceful shutdown...", sig_name)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig.name: handle_signal(s))
        except (NotImplementedError, RuntimeError):
            pass

    # Start FastAPI server
    fastapi_task = asyncio.create_task(
        run_fastapi_server(health_app, host=http_host, port=http_port, shutdown_event=shutdown_event)
    )

    # Start server-side 7200s GCS retention sweeper & crash recovery loop
    sweeper_task = asyncio.create_task(
        run_job_retention_sweeper_loop(shutdown_event, interval_seconds=60.0)
    )

    # Start Discord bot in background if token is supplied and valid
    bot_manager.start()

    # ONLY wait for shutdown_event or fastapi_task!
    # Discord bot errors, disconnects, or restarts DO NOT kill the web server!
    shutdown_waiter = asyncio.create_task(shutdown_event.wait())
    done, pending = await asyncio.wait(
        [fastapi_task, shutdown_waiter],
        return_when=asyncio.FIRST_COMPLETED,
    )

    logger.info("Shutdown initiated. Cleaning up active tasks...")
    shutdown_event.set()

    sweeper_task.cancel()
    with suppress(asyncio.CancelledError):
        await sweeper_task

    await bot_manager.stop_bot()

    for task in pending:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    # Remove temporary state file
    try:
        if os.path.exists("/tmp/ubagen_bot_state.json"):
            os.remove("/tmp/ubagen_bot_state.json")
    except Exception:
        pass

    logger.info("UBAgen backend successfully stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("UBAgen exited.")
