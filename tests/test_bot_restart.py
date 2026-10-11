"""End-to-end unit and integration tests for Discord bot restart across Python/FastAPI, Node/Express, and Frontend URL resolution."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from health_api import create_health_app
from main import BotLifecycleManager, run_discord_bot, validate_bind_host
from stats import BotTracker


def endpoint_for(app, path: str):
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"Route not found: {path}")


class FakeDiscordBot:
    """Controllable fake Discord bot instance for testing lifecycle transitions."""

    def __init__(self, fail_starts: int = 0, close_delay: float = 0.0) -> None:
        self.tracker = BotTracker()
        self.closed = False
        self.started_tokens: list[str] = []
        self.close_calls = 0
        self.fail_starts = fail_starts
        self.close_delay = close_delay
        self._run_gate = asyncio.Event()

    def is_closed(self) -> bool:
        return self.closed

    def is_ready(self) -> bool:
        return self.tracker.is_ready

    def write_state_file(self) -> None:
        pass

    async def start(self, token: str) -> None:
        self.started_tokens.append(token)
        if self.fail_starts > 0:
            self.fail_starts -= 1
            raise OSError("Simulated temporary gateway connection error")
        self.tracker.connected = True
        self.tracker.is_ready = True
        self.tracker.status = "connected"
        self.tracker.user = "UBAgenTest#0001"
        self.tracker.diagnostic_message = "Connected to Discord Gateway."
        await self._run_gate.wait()

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_delay > 0:
            await asyncio.sleep(self.close_delay)
        self.closed = True
        self.tracker.connected = False
        self.tracker.is_ready = False
        self.tracker.status = "disconnected"
        self._run_gate.set()


class BotRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_reconnects_when_bot_start_returns_unexpectedly(self):
        started: list[str] = []
        shutdown_event = asyncio.Event()

        class ImmediateReturnBot:
            def __init__(self) -> None:
                self.tracker = BotTracker()
                self.closed = False

            def is_closed(self) -> bool:
                return self.closed

            def write_state_file(self) -> None:
                pass

            async def start(self, token: str) -> None:
                started.append(token)
                # Simulate discord.py returning without a shutdown/restart request.

            async def close(self) -> None:
                self.closed = True
                self.tracker.connected = False
                self.tracker.is_ready = False

        def factory() -> ImmediateReturnBot:
            return ImmediateReturnBot()

        bot = factory()
        task = asyncio.create_task(
            run_discord_bot(
                bot,
                "test-discord-token",
                shutdown_event,
                bot_factory=factory,
                initial_retry_delay=0.02,
                max_retry_delay=0.05,
            )
        )
        try:
            for _ in range(60):
                if len(started) >= 2:
                    break
                await asyncio.sleep(0.005)
            self.assertGreaterEqual(len(started), 2, "unexpected normal return must trigger a reconnect")
            self.assertFalse(task.done(), "runner must stay alive until explicit shutdown")
        finally:
            shutdown_event.set()
            await asyncio.wait_for(task, timeout=1.0)

    async def test_valid_restart_closes_old_bot_recreates_instance_and_reconnects(self):
        admin_secret = "test-admin-secret-token-12345"
        discord_token = "MTIzNDU2Nzg5MDEyMzQ1Njc4.Ga1234.FakeTokenValueForTesting"
        created_bots: list[FakeDiscordBot] = []

        def factory() -> FakeDiscordBot:
            b = FakeDiscordBot()
            created_bots.append(b)
            return b

        shutdown_event = asyncio.Event()
        with patch.dict(
            "os.environ",
            {"UBAGEN_ADMIN_TOKEN": admin_secret, "DISCORD_BOT_TOKEN": discord_token},
            clear=False,
        ):
            manager = BotLifecycleManager(
                bot_factory=factory,
                shutdown_event=shutdown_event,
                cooldown_seconds=15.0,
                retry_delay=0.05,
            )
            app = create_health_app(bot=manager.bot, bot_manager=manager)
            manager.start()

            # Allow initial bot to connect
            await asyncio.sleep(0.05)
            self.assertEqual(len(created_bots), 1)
            first_bot = created_bots[0]
            self.assertTrue(first_bot.tracker.connected)
            self.assertEqual(first_bot.started_tokens, [discord_token])

            # Trigger authenticated restart
            res = await endpoint_for(app, "/api/admin/restart-bot")(
                authorization=f"Bearer {admin_secret}"
            )
            body = json.loads(res.body)
            self.assertEqual(res.status_code, 202)
            self.assertEqual(body["status"], "restarting")

            # Verify first bot was closed and a new bot instance was created and connected with same token
            await asyncio.sleep(0.05)
            self.assertTrue(first_bot.is_closed())
            self.assertEqual(first_bot.close_calls, 1)
            self.assertEqual(len(created_bots), 2)
            second_bot = created_bots[1]
            self.assertIs(app.state.bot, second_bot)
            self.assertTrue(second_bot.tracker.connected)
            self.assertEqual(second_bot.started_tokens, [discord_token])

            shutdown_event.set()
            await manager.stop_bot()

    async def test_consecutive_and_concurrent_restarts_are_throttled_with_429(self):
        admin_secret = "throttle-secret-token-789"
        discord_token = "MTIzNDU2Nzg5MDEyMzQ1Njc4.Ga1234.FakeTokenValueForTesting"
        created_bots: list[FakeDiscordBot] = []

        def factory() -> FakeDiscordBot:
            b = FakeDiscordBot(close_delay=0.08)
            created_bots.append(b)
            return b

        shutdown_event = asyncio.Event()
        with patch.dict(
            "os.environ",
            {"UBAGEN_ADMIN_TOKEN": admin_secret, "DISCORD_BOT_TOKEN": discord_token},
            clear=False,
        ):
            manager = BotLifecycleManager(
                bot_factory=factory,
                shutdown_event=shutdown_event,
                cooldown_seconds=10.0,
                retry_delay=0.05,
            )
            app = create_health_app(bot=manager.bot, bot_manager=manager)
            manager.start()
            await asyncio.sleep(0.03)

            restart_endpoint = endpoint_for(app, "/api/admin/restart-bot")
            # Fire two concurrent restart requests
            r1, r2 = await asyncio.gather(
                restart_endpoint(authorization=f"Bearer {admin_secret}"),
                restart_endpoint(authorization=f"Bearer {admin_secret}"),
            )
            statuses = sorted([r1.status_code, r2.status_code])
            self.assertEqual(statuses, [202, 429])

            # Immediate follow-up request within cooldown window also returns 429
            r3 = await restart_endpoint(authorization=f"Bearer {admin_secret}")
            self.assertEqual(r3.status_code, 429)
            self.assertIn("retry-after", {k.lower() for k in r3.headers.keys()})

            shutdown_event.set()
            await manager.stop_bot()

    async def test_stats_and_health_remain_accessible_while_bot_is_restarting(self):
        admin_secret = "concurrent-health-secret"
        discord_token = "MTIzNDU2Nzg5MDEyMzQ1Njc4.Ga1234.FakeTokenValueForTesting"

        def factory() -> FakeDiscordBot:
            return FakeDiscordBot(close_delay=0.12)

        shutdown_event = asyncio.Event()
        with patch.dict(
            "os.environ",
            {"UBAGEN_ADMIN_TOKEN": admin_secret, "DISCORD_BOT_TOKEN": discord_token},
            clear=False,
        ):
            manager = BotLifecycleManager(
                bot_factory=factory,
                shutdown_event=shutdown_event,
                cooldown_seconds=15.0,
            )
            app = create_health_app(bot=manager.bot, bot_manager=manager)
            manager.start()
            await asyncio.sleep(0.03)

            restart_task = asyncio.create_task(
                endpoint_for(app, "/api/admin/restart-bot")(
                    authorization=f"Bearer {admin_secret}"
                )
            )
            await asyncio.sleep(0.02)

            # Query /health and /api/stats while restart_task is actively closing the old bot
            with patch("health_api.collect_all_stats", return_value={"status": "online", "during": "restart"}):
                stats_res = await endpoint_for(app, "/api/stats")()
                health_res = await endpoint_for(app, "/health")()

            self.assertEqual(stats_res.status_code, 200)
            self.assertEqual(json.loads(stats_res.body)["status"], "online")
            self.assertEqual(health_res.status_code, 200)
            self.assertEqual(json.loads(health_res.body)["http"], "alive")

            restart_res = await restart_task
            self.assertEqual(restart_res.status_code, 202)

            shutdown_event.set()
            await manager.stop_bot()

    async def test_controlled_retry_on_reconnect_failure_after_restart(self):
        admin_secret = "retry-secret-token"
        discord_token = "MTIzNDU2Nzg5MDEyMzQ1Njc4.Ga1234.FakeTokenValueForTesting"
        created_bots: list[FakeDiscordBot] = []

        def factory() -> FakeDiscordBot:
            # The restarted bot (second instance) fails its first start() and succeeds on retry
            fail_count = 1 if len(created_bots) == 1 else 0
            b = FakeDiscordBot(fail_starts=fail_count)
            created_bots.append(b)
            return b

        shutdown_event = asyncio.Event()
        with patch.dict(
            "os.environ",
            {"UBAGEN_ADMIN_TOKEN": admin_secret, "DISCORD_BOT_TOKEN": discord_token},
            clear=False,
        ):
            manager = BotLifecycleManager(
                bot_factory=factory,
                shutdown_event=shutdown_event,
                cooldown_seconds=15.0,
                retry_delay=0.05,
            )
            app = create_health_app(bot=manager.bot, bot_manager=manager)
            manager.start()
            await asyncio.sleep(0.03)

            res = await endpoint_for(app, "/api/admin/restart-bot")(
                authorization=f"Bearer {admin_secret}"
            )
            self.assertEqual(res.status_code, 202)

            # Wait enough time for first start() failure + 0.05s controlled backoff + retry success
            await asyncio.sleep(0.18)
            self.assertTrue(app.state.bot.tracker.connected)
            self.assertGreaterEqual(app.state.bot.tracker.reconnect_attempts, 1)

            shutdown_event.set()
            await manager.stop_bot()

    def test_validate_bind_host_rejects_tokens_and_urls(self):
        self.assertEqual(validate_bind_host("0.0.0.0"), "0.0.0.0")
        self.assertEqual(validate_bind_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(validate_bind_host("https://example.com"), "0.0.0.0")
        self.assertEqual(validate_bind_host("Bearer secret-token-value"), "0.0.0.0")
        self.assertEqual(validate_bind_host("x" * 80), "0.0.0.0")

    def test_node_express_and_frontend_url_resolution_suite(self):
        """Run TypeScript verification for server.ts and src/restartBot.ts."""
        if not os.path.isdir("node_modules"):
            self.skipTest("node_modules not yet installed; run npm install first.")
        proc = subprocess.run(
            ["npx", "tsx", "tests/node_restart.test.ts"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            self.fail(f"Node/Frontend test suite failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main()
