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
import sys
import time
from typing import Any, Optional

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

from health_api import create_health_app
from stats import register_stats_command, BotTracker
from subtitle_extract import register_subtitle_extract_command

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

        def write_state_file(self) -> None:
            """Write current bot state to local tmp file for process sharing."""
            try:
                with open("/tmp/ubagen_bot_state.json", "w", encoding="utf-8") as f:
                    json.dump(self.tracker.to_dict(), f)
            except Exception:
                pass

        async def setup_hook(self) -> None:
            """Called automatically before bot login. Registers commands and syncs tree."""
            logger.info("Registering slash commands...")
            register_stats_command(self.tree, self)
            register_subtitle_extract_command(self.tree)

            # Sync slash commands with robust fallback
            if self.dev_guild_id:
                try:
                    dev_guild = discord.Object(id=self.dev_guild_id)
                    self.tree.copy_global_to(guild=dev_guild)
                    synced = await self.tree.sync(guild=dev_guild)
                    logger.info(
                        "⚡ Instantly synced %d slash command(s) to development guild ID: %d",
                        len(synced),
                        self.dev_guild_id,
                    )
                    self.tracker.sync_status = f"Synced {len(synced)} command(s) to dev guild {self.dev_guild_id}"
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
                        logger.info("🌐 Synced %d global slash command(s) as fallback.", len(synced))
                    except Exception as g_exc:
                        logger.warning("Global fallback sync error: %s", g_exc)
                except Exception as exc:
                    logger.warning("Dev guild sync encountered error (%s: %s). Attempting global sync...", type(exc).__name__, exc)
                    self.tracker.sync_status = f"Dev sync warning: {type(exc).__name__}"
                    try:
                        synced = await self.tree.sync()
                        logger.info("🌐 Synced %d global slash command(s) as fallback.", len(synced))
                    except Exception as g_exc:
                        logger.warning("Global fallback sync error: %s", g_exc)
            else:
                try:
                    synced = await self.tree.sync()
                    logger.info(
                        "🌐 Synced %d global slash command(s). (Note: Global sync may take up to 1 hour to propagate)",
                        len(synced),
                    )
                    self.tracker.sync_status = f"Synced {len(synced)} global command(s)"
                except Exception as exc:
                    logger.warning("Global slash command sync warning (%s): %s", type(exc).__name__, exc)
                    self.tracker.sync_status = f"Global sync warning: {type(exc).__name__}"

            self.write_state_file()

        async def on_connect(self) -> None:
            """Fires when the Discord gateway websocket connection is opened."""
            logger.info("Connected to Discord Gateway.")
            self.tracker.connected = True
            self.tracker.status = "connected"
            self.tracker.diagnostic_message = "Connected to Discord Gateway (caching guilds...)"
            self.write_state_file()

        async def on_ready(self) -> None:
            """Fires when Discord gateway connection and guild cache are fully established."""
            # Guild-scoped copies appear immediately in every server the bot has joined.
            # Keep global registration too, so commands also propagate to future servers.
            if not self._guild_commands_synced:
                synced_guilds = 0
                global_sync_succeeded = False
                for guild in list(self.guilds):
                    try:
                        self.tree.copy_global_to(guild=guild)
                        await self.tree.sync(guild=guild)
                        synced_guilds += 1
                    except Exception as exc:
                        logger.warning("Could not sync commands to guild %s: %s", guild.id, type(exc).__name__)
                try:
                    global_commands = await self.tree.sync()
                    global_sync_succeeded = True
                    logger.info(
                        "Slash commands synced globally (%d) and to %d current guild(s).",
                        len(global_commands),
                        synced_guilds,
                    )
                    self.tracker.sync_status = (
                        f"{len(global_commands)} global command(s); synced to {synced_guilds} current server(s)"
                    )
                except Exception as exc:
                    logger.warning("Global command sync from on_ready failed: %s", type(exc).__name__)
                    self.tracker.sync_status = f"Command sync warning: {type(exc).__name__}"
                # Only mark sync complete after Discord confirms the global sync.
                # If it failed, a later gateway READY event can retry registration.
                self._guild_commands_synced = global_sync_succeeded
                self.write_state_file()

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

        async def on_resumed(self) -> None:
            """Fires when a disconnected gateway session is successfully resumed."""
            logger.info("Discord Gateway session resumed.")
            self.tracker.connected = True
            self.tracker.is_ready = True
            self.tracker.status = "connected"
            self.tracker.diagnostic_message = "Gateway session resumed."
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
# Resilient Bot Lifecycle Runner
# ------------------------------------------------------------------------------
async def run_discord_bot(bot: Any, token: str, shutdown_event: asyncio.Event) -> None:
    """
    Runs the Discord bot lifecycle with resilient error handling and safe diagnostics.
    Never exposes tokens or logs secrets.
    Does NOT stop the HTTP monitoring server upon bot error.
    """
    tracker: BotTracker = getattr(bot, "tracker", None) or BotTracker()
    tracker.status = "connecting"
    tracker.diagnostic_message = "Connecting to Discord Gateway..."
    if hasattr(bot, "write_state_file"):
        bot.write_state_file()

    reconnect_delay = 5.0
    max_reconnect_delay = 60.0

    while not shutdown_event.is_set():
        try:
            logger.info("Connecting to Discord Gateway...")
            await bot.start(token)
            # If bot.start returns normally without error (e.g. clean logout)
            break
        except Exception as exc:
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
                # Stop retrying on invalid credentials, but keep waiting so web server remains alive
                await shutdown_event.wait()
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
                tracker.diagnostic_message = f"Network connection error ({type(exc).__name__}). Reconnecting in {int(reconnect_delay)}s..."
                if hasattr(bot, "write_state_file"):
                    bot.write_state_file()
                logger.warning("Discord network connection error: %s. Retrying in %ds...", exc, int(reconnect_delay))
            else:
                # Discord HTTP / API errors or other errors
                status_code = getattr(exc, "status", None)
                if status_code is not None:
                    tracker.status = "api_error"
                    tracker.diagnostic_message = f"Discord API error (HTTP {status_code}). Retrying in {int(reconnect_delay)}s..."
                else:
                    tracker.status = "disconnected"
                    tracker.diagnostic_message = f"Bot connection error ({type(exc).__name__}). Retrying in {int(reconnect_delay)}s..."
                tracker.connected = False
                tracker.is_ready = False
                if hasattr(bot, "write_state_file"):
                    bot.write_state_file()
                logger.warning("Discord connection exception (%s): %s. Retrying in %ds...", type(exc).__name__, exc, int(reconnect_delay))

            # Wait with exponential backoff before reconnecting, unless shutdown is signaled
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=reconnect_delay)
                break
            except asyncio.TimeoutError:
                reconnect_delay = min(reconnect_delay * 1.5, max_reconnect_delay)


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


async def main() -> None:
    """Main application loop coordinating bot and web service."""
    bot_token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    is_placeholder_token = not bot_token or bot_token in (
        "your_discord_bot_token_here",
        "MY_DISCORD_BOT_TOKEN",
        "CHANGE_ME",
    )

    port_str = os.getenv("UBAGEN_HTTP_PORT", "").strip() or os.getenv("PORT", "8080").strip()
    try:
        http_port = int(port_str)
    except ValueError:
        logger.warning("Invalid PORT '%s'. Falling back to 8080.", port_str)
        http_port = 8080

    http_host = os.getenv("HOST", "0.0.0.0").strip()

    # Parse and validate optional test guild ID (Snowflake format: 17-20 digits)
    guild_id_env = os.getenv("DISCORD_GUILD_ID", "").strip()
    dev_guild_id: Optional[int] = None
    if guild_id_env:
        if guild_id_env.isdigit() and 17 <= len(guild_id_env) <= 20:
            try:
                dev_guild_id = int(guild_id_env)
                logger.info("Development Guild ID detected: %d (Enabling fast guild command sync)", dev_guild_id)
            except ValueError:
                dev_guild_id = None
        else:
            logger.warning(
                "Invalid DISCORD_GUILD_ID format ('%s' is not a valid 17-20 digit Snowflake ID). "
                "Ignoring guild sync and falling back to global sync.",
                guild_id_env[:4] + "..." if len(guild_id_env) > 4 else guild_id_env,
            )

    # Instantiate bot if discord.py is available
    bot = UBAgenBot(guild_id=dev_guild_id) if UBAgenBot is not None else None
    health_app = create_health_app(bot=bot)

    shutdown_event = asyncio.Event()

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

    # Start Discord bot in background if token is supplied and valid
    bot_task = None
    if bot and not is_placeholder_token:
        bot_task = asyncio.create_task(run_discord_bot(bot, bot_token, shutdown_event))
    else:
        if is_placeholder_token:
            logger.info(
                "NOTICE: 'DISCORD_BOT_TOKEN' is not configured. UBAgen is running in Web Monitor mode. "
                "Discord bot will remain offline until a valid DISCORD_BOT_TOKEN is set in your environment."
            )
            if bot and hasattr(bot, "tracker"):
                bot.tracker.status = "not_configured"
                bot.tracker.diagnostic_message = "DISCORD_BOT_TOKEN is not configured. Web monitor is active."
                bot.write_state_file()
        elif not bot:
            logger.warning("NOTICE: discord.py is not installed; running in Web Monitor mode.")

    # ONLY wait for shutdown_event or fastapi_task!
    # Discord bot errors or disconnects DO NOT kill the web server!
    shutdown_waiter = asyncio.create_task(shutdown_event.wait())
    done, pending = await asyncio.wait(
        [fastapi_task, shutdown_waiter],
        return_when=asyncio.FIRST_COMPLETED,
    )

    logger.info("Shutdown initiated. Cleaning up active tasks...")
    shutdown_event.set()

    if bot and not bot.is_closed():
        logger.info("Closing Discord bot connection...")
        try:
            await bot.close()
        except Exception as e:
            logger.debug("Error while closing bot: %s", e)

    if bot_task and not bot_task.done():
        bot_task.cancel()
        with suppress(asyncio.CancelledError):
            await bot_task

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
