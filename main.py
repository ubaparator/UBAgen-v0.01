"""
UBAgen — main.py
Entry point for UBAgen Discord Bot and FastAPI Monitoring Service.

Runs both the Discord gateway client (discord.py) and the HTTP monitoring endpoint (FastAPI/uvicorn)
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

try:
    import discord
    from discord.ext import commands
except ImportError:
    discord = None
    commands = None

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
else:
    UBAgenBot = None


# ------------------------------------------------------------------------------
# Server & Bot Concurrency Orchestration
# ------------------------------------------------------------------------------
async def run_fastapi_server(app, host: str, port: int, shutdown_event: asyncio.Event) -> None:
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
    load_dotenv()

    bot_token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
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

    fastapi_task = asyncio.create_task(
        run_fastapi_server(health_app, host=http_host, port=http_port, shutdown_event=shutdown_event)
    )

    tasks_to_wait = [fastapi_task]

    # Start Discord bot only if token is supplied and discord is available
    bot_task = None
    if bot and bot_token:
        async def start_discord_bot():
            try:
                logger.info("Connecting to Discord Gateway...")
                await bot.start(bot_token)
            except Exception as exc:
                logger.error("Discord bot encountered error: %s", exc)
                # Do not kill the web server if Discord connection fails; keep monitoring active
                if hasattr(bot, "is_ready") and not bot.is_ready():
                    logger.warning("Web API will remain online while Discord is disconnected.")

        bot_task = asyncio.create_task(start_discord_bot())
        tasks_to_wait.append(bot_task)
    else:
        if not bot_token:
            logger.warning(
                "NOTICE: 'DISCORD_BOT_TOKEN' is not set. UBAgen is running in Web Monitor mode. "
                "Discord bot will remain disconnected until DISCORD_BOT_TOKEN is set in your environment."
            )
        elif not bot:
            logger.warning("NOTICE: discord.py is not installed; running in Web Monitor mode.")

    shutdown_waiter = asyncio.create_task(shutdown_event.wait())
    tasks_to_wait.append(shutdown_waiter)

    done, pending = await asyncio.wait(
        tasks_to_wait,
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
