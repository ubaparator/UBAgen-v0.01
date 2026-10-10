"""
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
        # discord.Intents.default() includes GUILDS intent, sufficient for slash commands.
        # Privileged intents (Message Content, Server Members) are NOT required.
        intents = discord.Intents.default()

        super().__init__(
            command_prefix="!",  # Prefix is fallback; /stats is registered as slash command
            intents=intents,
            help_command=None,
        )
        self.dev_guild_id = guild_id

    async def setup_hook(self) -> None:
        """Called automatically before bot login. Registers commands and syncs tree."""
        logger.info("Registering slash commands...")
        register_stats_command(self.tree, self)

        # Sync slash commands
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

            # Set bot activity
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
        log_level="warning",  # Keep health check spam out of logs
        access_log=False,
    )
    server = uvicorn.Server(config)

    # Monitor shutdown event
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
    # 1. Load local environment variables from .env if present
    load_dotenv()

    # 2. Extract configuration
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

    # 3. Instantiate bot and health API
    bot = UBAgenBot(guild_id=dev_guild_id)
    health_app = create_health_app(bot=bot)

    shutdown_event = asyncio.Event()

    # 4. Attach OS signal handlers for graceful shutdown (SIGINT, SIGTERM)
    loop = asyncio.get_running_loop()

    def handle_signal(sig_name: str) -> None:
        logger.info("Received %s signal. Initiating graceful shutdown...", sig_name)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig.name: handle_signal(s))
        except (NotImplementedError, RuntimeError):
            # Windows or restricted environments might not support signal handlers
            pass

    # 5. Launch both tasks
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

    # Wait for either shutdown trigger or abnormal completion
    shutdown_waiter = asyncio.create_task(shutdown_event.wait())
    done, pending = await asyncio.wait(
        [fastapi_task, bot_task, shutdown_waiter],
        return_when=asyncio.FIRST_COMPLETED,
    )

    logger.info("Shutdown initiated. Cleaning up active tasks...")

    # Signal HTTP server to exit
    shutdown_event.set()

    # Gracefully close Discord connection
    if not bot.is_closed():
        logger.info("Closing Discord bot connection...")
        try:
            await bot.close()
        except Exception as e:
            logger.debug("Error while closing bot: %s", e)

    # Await remaining tasks
    for task in pending:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    logger.info("UBAgen backend successfully stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("UBAgen exited.")
