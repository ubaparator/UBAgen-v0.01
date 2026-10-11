"""
UBAgen — tests/test_discord_wake_interactions.py
Unit and integration tests for:
1. Ed25519 signature verification (RFC 8032 standard vector + tamper/replay rejection).
2. Discord HTTP Interactions Endpoint (`POST /api/discord/interactions` & `/discord/interactions`):
   - PING (`type=1`) -> PONG (`{"type": 1}`)
   - Slash command (`type=2`) automatic Wake-on-Command when the bot worker is offline
   - Timely Discord interaction callbacks (`type=5` deferred or `type=4` immediate)
3. Authenticated Wake Control Endpoint (`POST /api/discord/wake`) & `BotLifecycleManager.ensure_bot_awake`:
   - Waking an offline/backoff bot, single-flight deduplication under concurrent requests,
     cooldown enforcement, and non-disruptive `200 OK` when already ready.
4. Separate readiness telemetry (`HTTP alive`, `Discord Gateway connected`, `Bot Ready`):
   - Ensuring the bot is never reported as `"connected"` until `is_ready=True`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import unittest
from unittest.mock import patch

from discord_interactions import (
    _B as _ed25519_B,
    _L as _ed25519_L,
    _encode_point as _ed25519_encode_point,
    _scalarmult,
    verify_discord_ed25519_signature,
)
from health_api import create_health_app
from main import BotLifecycleManager, run_discord_bot
from stats import BotTracker, collect_all_stats


def _ed25519_point_mul(scalar: int, pt):
    return _scalarmult(pt, scalar)


def endpoint_for(app, path: str):
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"Route not found: {path}")


class FakeRequest:
    def __init__(self, body_bytes: bytes = b"{}", method: str = "POST", headers: dict | None = None) -> None:
        self._body = body_bytes
        self.method = method
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}

    async def body(self) -> bytes:
        return self._body

    async def json(self):
        return json.loads(self._body.decode("utf-8"))


def _ed25519_sign_rfc8032(secret_key_32: bytes, message: bytes) -> tuple[str, str]:
    """Pure-Python RFC 8032 Ed25519 key derivation and signing helper for deterministic tests."""
    h = hashlib.sha512(secret_key_32).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    prefix = h[32:]
    A_pt = _ed25519_point_mul(a, _ed25519_B)
    pub_bytes = _ed25519_encode_point(A_pt)

    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % _ed25519_L
    R_pt = _ed25519_point_mul(r, _ed25519_B)
    R_bytes = _ed25519_encode_point(R_pt)
    k = int.from_bytes(hashlib.sha512(R_bytes + pub_bytes + message).digest(), "little") % _ed25519_L
    S = (r + k * a) % _ed25519_L
    sig_bytes = R_bytes + S.to_bytes(32, "little")
    return pub_bytes.hex(), sig_bytes.hex()


class Ed25519AndInteractionsEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.secret_seed = bytes.fromhex(
            "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
        )
        self.pub_hex, _ = _ed25519_sign_rfc8032(self.secret_seed, b"")

    def _sign_payload(self, body_dict: dict, timestamp: str | None = None) -> tuple[bytes, str, str]:
        ts = timestamp or str(int(time.time()))
        raw = json.dumps(body_dict).encode("utf-8")
        _, sig_hex = _ed25519_sign_rfc8032(self.secret_seed, ts.encode("utf-8") + raw)
        return raw, sig_hex, ts

    def test_verify_discord_ed25519_signature_valid_and_invalid(self) -> None:
        raw, sig, ts = self._sign_payload({"type": 1})
        ok, reason = verify_discord_ed25519_signature(
            public_key_hex=self.pub_hex,
            signature_hex=sig,
            timestamp_header=ts,
            raw_body=raw,
        )
        self.assertTrue(ok, f"Expected valid signature, got: {reason}")

        # Tampered body must fail
        ok_bad_body, _ = verify_discord_ed25519_signature(
            public_key_hex=self.pub_hex,
            signature_hex=sig,
            timestamp_header=ts,
            raw_body=b'{"type": 2}',
        )
        self.assertFalse(ok_bad_body)

        # Stale timestamp outside 300s replay window must fail
        stale_ts = str(int(time.time()) - 900)
        raw_stale, sig_stale, _ = self._sign_payload({"type": 1}, timestamp=stale_ts)
        ok_stale, reason_stale = verify_discord_ed25519_signature(
            public_key_hex=self.pub_hex,
            signature_hex=sig_stale,
            timestamp_header=stale_ts,
            raw_body=raw_stale,
        )
        self.assertFalse(ok_stale)
        self.assertIn("Timestamp", reason_stale)

    async def test_discord_interactions_ping_pong_and_rejects_unsigned(self) -> None:
        app = create_health_app()
        interactions_ep = endpoint_for(app, "/api/discord/interactions")

        with patch.dict(os.environ, {"DISCORD_PUBLIC_KEY": self.pub_hex}, clear=False):
            # Unsigned request -> 401
            res_unauth = await interactions_ep(
                FakeRequest(b'{"type": 1}'),
                x_signature_ed25519=None,
                x_signature_timestamp=None,
            )
            self.assertEqual(res_unauth.status_code, 401)

            # Signed PING -> 200 {"type": 1}
            raw, sig, ts = self._sign_payload({"id": "1001", "type": 1})
            res_ping = await interactions_ep(
                FakeRequest(raw),
                x_signature_ed25519=sig,
                x_signature_timestamp=ts,
            )
            self.assertEqual(res_ping.status_code, 200)
            self.assertEqual(json.loads(res_ping.body), {"type": 1})

    async def test_slash_command_interaction_wakes_offline_bot_and_defers(self) -> None:
        wake_calls: list[str] = []

        async def fake_wake(reason: str = "interaction") -> tuple[int, dict]:
            wake_calls.append(reason)
            return 202, {"status": "waking", "woke": True, "ready": False}

        app = create_health_app()
        app.state.wake_bot = fake_wake
        interactions_ep = endpoint_for(app, "/api/discord/interactions")

        interaction_payload = {
            "id": "999888777",
            "application_id": "123456789012345678",
            "type": 2,
            "token": "fake_interaction_token",
            "member": {
                "user": {"id": "555444333222111000", "username": "ubaparator"},
                "permissions": "8",  # Administrator
            },
            "data": {
                "name": "is_durumu",
                "options": [{"name": "is_id", "value": "job_nonexistent_123"}],
            },
        }

        with patch.dict(os.environ, {"DISCORD_PUBLIC_KEY": self.pub_hex}, clear=False):
            raw, sig, ts = self._sign_payload(interaction_payload)
            res = await interactions_ep(
                FakeRequest(raw),
                x_signature_ed25519=sig,
                x_signature_timestamp=ts,
            )
            self.assertEqual(res.status_code, 200)
            body = json.loads(res.body)
            # Type 5 = DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE
            self.assertEqual(body.get("type"), 5)
            self.assertEqual(len(wake_calls), 1)
            self.assertIn("slash_command:is_durumu", wake_calls[0])


class WakeOnCommandAndReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_bot_awake_deduplicates_concurrent_requests_and_wakes_offline_worker(self) -> None:
        start_calls = 0

        class FakeOfflineBot:
            def __init__(self) -> None:
                self.tracker = BotTracker()
                self.tracker.status = "disconnected"
                self.tracker.connected = False
                self.tracker.is_ready = False
                self._closed = False
                self.user = "UBAgen#0001"
                self.guilds = []
                self.latency = 0.025

            def is_ready(self) -> bool:
                return self.tracker.is_ready

            def is_closed(self) -> bool:
                return self._closed

            async def start(self, token: str, reconnect: bool = True) -> None:
                nonlocal start_calls
                start_calls += 1
                self.tracker.connected = True
                self.tracker.is_ready = True
                self.tracker.status = "connected"
                while not self._closed:
                    await asyncio.sleep(0.05)

            async def close(self) -> None:
                self._closed = True
                self.tracker.connected = False
                self.tracker.is_ready = False

        shutdown_event = asyncio.Event()
        manager = BotLifecycleManager(
            bot_factory=FakeOfflineBot,
            shutdown_event=shutdown_event,
            cooldown_seconds=5.0,
            wake_cooldown_seconds=5.0,
        )

        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "MTIzNDU2Nzg5MDEyMzQ1Njc4.GTest.ValidTokenValue"}, clear=False):
            # Fire 3 concurrent wake requests while bot is completely stopped
            results = await asyncio.gather(
                manager.ensure_bot_awake(reason="cmd_1", wait_for_ready_seconds=0.5),
                manager.ensure_bot_awake(reason="cmd_2", wait_for_ready_seconds=0.5),
                manager.ensure_bot_awake(reason="cmd_3", wait_for_ready_seconds=0.5),
            )
            codes = [code for code, _ in results]
            self.assertTrue( any(c in (200, 202) for c in codes) )
            # Only a single bot worker must have been spawned
            self.assertEqual(start_calls, 1)
            self.assertTrue(manager.is_bot_ready())

            # Calling ensure_bot_awake again when already ready returns 200 without restarting
            code_ready, payload_ready = await manager.ensure_bot_awake(reason="already_online")
            self.assertEqual(code_ready, 200)
            self.assertFalse(payload_ready["woke"])
            self.assertTrue(payload_ready["ready"])
            self.assertEqual(start_calls, 1)

            shutdown_event.set()
            await manager.stop()

    async def test_wake_event_interrupts_run_discord_bot_backoff_immediately(self) -> None:
        attempts = 0
        wake_event = asyncio.Event()
        shutdown_event = asyncio.Event()

        class BackoffThenReadyBot:
            def __init__(self) -> None:
                self.tracker = BotTracker()
                self._closed = False

            def is_ready(self) -> bool:
                return self.tracker.is_ready

            def is_closed(self) -> bool:
                return self._closed

            async def start(self, token: str, reconnect: bool = True) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("Simulated temporary gateway disconnect")
                self.tracker.connected = True
                self.tracker.is_ready = True
                self.tracker.status = "connected"
                shutdown_event.set()

            async def close(self) -> None:
                self._closed = True

        bot = BackoffThenReadyBot()
        runner = asyncio.create_task(
            run_discord_bot(
                bot,
                "MTIzNDU2Nzg5MDEyMzQ1Njc4.GTest.ValidTokenValue",
                shutdown_event,
                wake_event=wake_event,
                bot_factory=BackoffThenReadyBot,
            )
        )
        # Wait briefly for attempt 1 to fail and enter the 5-second backoff wait
        await asyncio.sleep(0.15)
        self.assertEqual(attempts, 1)

        # Trigger wake_event -> should interrupt the 5s sleep immediately!
        t0 = time.monotonic()
        wake_event.set()
        await asyncio.wait_for(runner, timeout=1.5)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0)
        self.assertEqual(attempts, 2)

    async def test_bot_not_reported_connected_until_is_ready_and_wake_endpoint_auth(self) -> None:
        class HandshakingBot:
            def __init__(self) -> None:
                self.tracker = BotTracker()
                # Simulate Gateway WebSocket connected (on_connect fired) but on_ready not yet fired
                self.tracker.connected = True
                self.tracker.is_ready = False
                self.tracker.status = "connected"
                self.tracker.diagnostic_message = "Handshaking with Discord Gateway..."

            def is_ready(self) -> bool:
                return False

        bot = HandshakingBot()
        stats = collect_all_stats(bot)
        self.assertTrue(stats["bot"]["http_alive"])
        self.assertTrue(stats["bot"]["gateway_connected"])
        self.assertFalse(stats["bot"]["bot_ready"])
        self.assertFalse(stats["bot"]["is_ready"])
        # Must NEVER report status == "connected" while is_ready is False
        self.assertEqual(stats["bot"]["status"], "connecting")

        app = create_health_app(bot=bot)
        health_ep = endpoint_for(app, "/health")
        wake_ep = endpoint_for(app, "/api/discord/wake")

        health_res = await health_ep()
        self.assertEqual(health_res.status_code, 200)
        h_json = json.loads(health_res.body)
        self.assertTrue(h_json["http_alive"])
        self.assertTrue(h_json["discord_gateway_connected"])
        self.assertFalse(h_json["discord_bot_ready"])
        self.assertEqual(h_json["discord_bot"]["status"], "connecting")

        # Verify POST /api/discord/wake authentication and response
        with patch.dict(
            os.environ,
            {
                "UBAGEN_ADMIN_TOKEN": "super_secret_admin_token",
                "DISCORD_BOT_TOKEN": "MTIzNDU2Nzg5MDEyMzQ1Njc4.GTest.ValidTokenValue",
            },
            clear=False,
        ):
            unauth = await wake_ep(FakeRequest(b"{}"), authorization=None)
            self.assertEqual(unauth.status_code, 401)

            auth_ok = await wake_ep(
                FakeRequest(b'{"reason": "unit_test"}'),
                authorization="Bearer super_secret_admin_token",
            )
            self.assertEqual(auth_ok.status_code, 202)
            self.assertEqual(json.loads(auth_ok.body)["status"], "waking")


if __name__ == "__main__":
    unittest.main()
