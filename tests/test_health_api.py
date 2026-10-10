"""Focused regression tests for the FastAPI monitoring API.

These tests exercise route handlers directly, avoiding an optional HTTP test client
dependency while still checking the responses produced by the application.
"""
import json
import unittest
from unittest.mock import patch

from fastapi.responses import JSONResponse

from health_api import create_health_app


def endpoint_for(app, path):
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"Route not found: {path}")


class HealthApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_stats_aliases_return_fresh_data_without_cache(self):
        payload = {"status": "online", "marker": "fresh"}
        app = create_health_app()

        with patch("health_api.collect_all_stats", return_value=payload) as collect:
            for path in ("/api/stats", "/stats"):
                response = await endpoint_for(app, path)()
                self.assertIsInstance(response, JSONResponse)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(json.loads(response.body), payload)
                self.assertIn("no-store", response.headers["cache-control"])
            self.assertEqual(collect.call_count, 2)

    async def test_stats_returns_typed_degraded_payload_when_first_collection_fails(self):
        app = create_health_app()

        with patch("health_api.collect_all_stats", side_effect=RuntimeError("test failure")):
            response = await endpoint_for(app, "/api/stats")()

        body = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], "degraded")
        self.assertIsNone(body["memory"]["system_total_bytes"])
        self.assertIsNone(body["compute"]["cpu_usage_pct"])
        self.assertNotIn("test failure", json.dumps(body))

    async def test_stats_preserves_last_good_snapshot_after_collection_failure(self):
        app = create_health_app()
        fresh_payload = {"status": "online", "marker": "last-known-good"}

        with patch(
            "health_api.collect_all_stats",
            side_effect=[fresh_payload, RuntimeError("temporary failure")],
        ):
            first = await endpoint_for(app, "/api/stats")()
            second = await endpoint_for(app, "/api/stats")()

        self.assertEqual(json.loads(first.body), fresh_payload)
        self.assertEqual(json.loads(second.body), fresh_payload)

    async def test_health_endpoint_is_alive_without_discord_bot(self):
        app = create_health_app()
        response = await endpoint_for(app, "/health")()
        body = json.loads(response.body)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], "healthy")
        self.assertEqual(body["http"], "alive")
        self.assertFalse(body["discord_bot"]["connected"])
        self.assertFalse(body["discord_bot"]["ready"])

    async def test_restart_bot_returns_503_when_admin_token_not_configured(self):
        app = create_health_app()
        with patch.dict("os.environ", {"UBAGEN_ADMIN_TOKEN": "", "HOST": "0.0.0.0"}, clear=False):
            response = await endpoint_for(app, "/api/admin/restart-bot")(
                authorization="Bearer 0.0.0.0"
            )
        body = json.loads(response.body)
        self.assertEqual(response.status_code, 503)
        self.assertIn("UBAGEN_ADMIN_TOKEN", body["error"])

    async def test_restart_bot_rejects_missing_or_invalid_bearer_token_with_401(self):
        secret = "super-secret-admin-token-999"
        app = create_health_app()
        with patch.dict(
            "os.environ",
            {"UBAGEN_ADMIN_TOKEN": secret, "DISCORD_BOT_TOKEN": "MTIzNDU2Nzg5MDEyMzQ1Njc4.Ga.bc"},
            clear=False,
        ):
            missing_res = await endpoint_for(app, "/api/admin/restart-bot")(authorization=None)
            wrong_res = await endpoint_for(app, "/api/admin/restart-bot")(
                authorization="Bearer wrong-token"
            )
            malformed_res = await endpoint_for(app, "/api/admin/restart-bot")(
                authorization=f"Basic {secret}"
            )

        for res in (missing_res, wrong_res, malformed_res):
            self.assertEqual(res.status_code, 401)
            payload = json.loads(res.body)
            self.assertIn("Unauthorized", payload["error"])
            self.assertNotIn(secret, json.dumps(payload))

    async def test_restart_bot_rejects_disallowed_http_methods_with_405(self):
        app = create_health_app()

        class DummyRequest:
            def __init__(self, method: str):
                self.method = method
                self.headers = {"authorization": "Bearer any"}

        for bad_method in ("GET", "PUT", "DELETE", "PATCH"):
            res = await endpoint_for(app, "/api/admin/restart-bot")(
                request=DummyRequest(bad_method)
            )
            self.assertEqual(res.status_code, 405)
            self.assertIn("POST", res.headers.get("allow", ""))


if __name__ == "__main__":
    unittest.main()
