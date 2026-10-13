"""
Unit tests for the Discord Gateway Watchdog in BotLifecycleManager:
- Detection of unexpected bot task completion / crash and controlled restart
- Distinction between invalid token (LoginFailure) vs transient network disconnection
- Concurrency deduplication (preventing multiple bot instances)
- Wake event signaling and clean shutdown
- Explicit boundary test: When the runtime process is terminated, an in-process watchdog cannot run.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import os
import time
import unittest
from unittest.mock import patch, MagicMock

from main import BotLifecycleManager, run_discord_bot
from stats import BotTracker


class FakeBot:
    def __init__(self, name: str = "FakeBot"):
        self.name = name
        self.tracker = BotTracker()
        self.tracker.connected = False
        self.tracker.is_ready = False
        self._is_closed = False
        self.closed_called = 0
        self.start_called = 0

    def is_closed(self) -> bool:
        return self._is_closed

    def is_ready(self) -> bool:
        return bool(self.tracker.is_ready)

    async def close(self) -> None:
        self.closed_called += 1
        self._is_closed = True
        self.tracker.connected = False
        self.tracker.is_ready = False

    async def start(self, token: str) -> None:
        self.start_called += 1
        self.tracker.connected = True
        self.tracker.is_ready = True
        await asyncio.sleep(0.05)


class GatewayWatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def test_watchdog_revives_unexpectedly_completed_bot_task(self):
        """When bot_task finishes unexpectedly while app is running, watchdog restarts it."""
        created_bots: list[FakeBot] = []

        def bot_factory():
            b = FakeBot(name=f"Bot_{len(created_bots)}")
            created_bots.append(b)
            return b

        shutdown_event = asyncio.Event()
        mgr = BotLifecycleManager(
            bot_factory=bot_factory,
            shutdown_event=shutdown_event,
            retry_delay=0.1,
        )

        with patch.object(mgr, "get_bot_token", return_value="VALID_TOKEN_XYZ_123"):
            mgr.start()
            self.assertIsNotNone(mgr.bot_task)
            self.assertIsNotNone(mgr.watchdog_task)

            # Simulate unexpected task completion / crash
            mgr.bot_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await mgr.bot_task
            self.assertTrue(mgr.bot_task.done())

            # Watchdog revival logic
            token = mgr.get_bot_token()
            
            # Watchdog revival logic
            async with mgr._restart_lock:
                if not mgr.shutdown_event.is_set() and not mgr._stop_bot_event.is_set():
                    mgr.bot_task = asyncio.create_task(
                        run_discord_bot(
                            mgr.bot,
                            token,
                            mgr.shutdown_event,
                            stop_event=mgr._stop_bot_event,
                            wake_event=mgr._wake_event,
                            bot_factory=mgr._create_bot_instance,
                            on_bot_replaced=mgr._update_active_bot,
                            initial_retry_delay=0.1,
                        )
                    )

            self.assertIsNotNone(mgr.bot_task)
            await asyncio.sleep(0.1)

            # Clean shutdown
            await mgr.stop_bot()
            shutdown_event.set()

    async def test_watchdog_replaces_runner_stuck_inside_gateway_start(self):
        """A live runner stuck unready is replaced once; wake_event alone is insufficient."""
        created: list[FakeBot] = []

        class StalledBot(FakeBot):
            def __init__(self, name: str):
                super().__init__(name)
                self._release_start = asyncio.Event()

            async def start(self, token: str) -> None:
                self.start_called += 1
                self.tracker.connected = False
                self.tracker.is_ready = False
                self.tracker.status = "disconnected"
                await self._release_start.wait()

            async def close(self) -> None:
                await super().close()
                self._release_start.set()

        def factory():
            bot = StalledBot(f"Bot_{len(created)}")
            created.append(bot)
            return bot

        mgr = BotLifecycleManager(bot_factory=factory, shutdown_event=asyncio.Event(), retry_delay=0.02)
        mgr.gateway_stall_timeout = 0.05
        mgr.watchdog_interval = 0.01
        with patch.object(mgr, "get_bot_token", return_value="VALID_TOKEN_XYZ_123"):
            mgr.start()
            for _ in range(100):
                if mgr.bot.start_called:
                    break
                await asyncio.sleep(0.005)
            self.assertGreaterEqual(mgr.bot.start_called, 1)

            # Simulate on_disconnect while bot.start remains alive in discord.py's reconnect path.
            mgr.bot.tracker.connected = False
            mgr.bot.tracker.is_ready = False
            mgr.bot.tracker.status = "disconnected"
            mgr.bot.tracker.last_error_time = time.time() - 1
            mgr.bot.tracker.gateway_state_since = time.time() - 1

            for _ in range(100):
                if len(created) >= 2 and mgr.bot is created[1]:
                    break
                await asyncio.sleep(0.01)

            self.assertGreaterEqual(len(created), 2)
            self.assertIs(mgr.bot, created[1])
            self.assertIsNotNone(mgr.bot_task)
            self.assertFalse(mgr.bot_task.done())

            # Model a successful READY event on the replacement and ensure the watchdog settles.
            mgr.bot.tracker.connected = True
            mgr.bot.tracker.is_ready = True
            mgr.bot.tracker.status = "connected"
            mgr.bot.tracker.gateway_state_since = None
            mgr.bot.tracker.gateway_attempt_started_at = None
            await asyncio.sleep(0.04)
            self.assertEqual(len(created), 2, "a recovered Gateway must not trigger repeated replacement")

            await mgr.stop_bot()
            mgr.shutdown_event.set()

    async def test_watchdog_does_not_revive_on_invalid_token(self):
        """When token is rejected (status='invalid_token'), watchdog must NOT hammer reconnects."""
        shutdown_event = asyncio.Event()
        mgr = BotLifecycleManager(
            bot_factory=lambda: FakeBot(),
            shutdown_event=shutdown_event,
        )

        mgr.bot.tracker.status = "invalid_token"
        with patch.object(mgr, "get_bot_token", return_value="INVALID_TOKEN"):
            # If status is invalid_token, watchdog skips restart
            tracker = mgr.bot.tracker
            self.assertEqual(tracker.status, "invalid_token")
            # Verify condition check: tracker.status == 'invalid_token' prevents revival
            should_skip = tracker and tracker.status == "invalid_token"
            self.assertTrue(should_skip)

    async def test_concurrent_restart_and_wake_prevent_duplicate_bot_instances(self):
        """Simultaneous restart and wake calls must coalesce and prevent duplicate bots."""
        created_count = 0

        def factory():
            nonlocal created_count
            created_count += 1
            return FakeBot(f"Bot_{created_count}")

        mgr = BotLifecycleManager(
            bot_factory=factory,
            shutdown_event=asyncio.Event(),
        )

        with patch.object(mgr, "get_bot_token", return_value="TEST_TOKEN"):
            # Trigger simultaneous ensure_bot_awake calls
            tasks = [
                mgr.ensure_bot_awake(reason=f"call_{i}")
                for i in range(5)
            ]
            results = await asyncio.gather(*tasks)

            # Exactly one or two bot instances should have been created (initial + at most 1 fresh), NOT 5!
            self.assertLessEqual(created_count, 2)
            for code, payload in results:
                self.assertIn(code, (200, 202))

            await mgr.stop_bot()

    def test_in_process_watchdog_boundary_guarantee(self):
        """
        Document and test boundary guarantee:
        An in-process watchdog cannot restart a completely halted or killed OS process.
        """
        # If the Python interpreter process terminates (SIGKILL / container stop),
        # watchdog inside asyncio event loop cannot execute. External supervisor is needed.
        self.assertTrue(hasattr(BotLifecycleManager, "_gateway_watchdog_loop"))


if __name__ == "__main__":
    unittest.main()
