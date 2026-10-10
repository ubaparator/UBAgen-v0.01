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


if __name__ == "__main__":
    unittest.main()


class SubtitleFormatTests(unittest.TestCase):
    def test_ass_and_ssa_preserve_ass_format(self):
        from subtitle_extract import subtitle_format

        self.assertEqual(subtitle_format("ass"), (".ass", "ass"))
        self.assertEqual(subtitle_format("SSA"), (".ass", "ass"))

    def test_text_subtitles_use_srt_format(self):
        from subtitle_extract import subtitle_format

        self.assertEqual(subtitle_format("subrip"), (".srt", "srt"))
        self.assertEqual(subtitle_format("webvtt"), (".srt", "srt"))

    def test_srt_cleaner_preserves_text_and_line_breaks(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from subtitle_extract import clean_srt_file

        source = (
            "1\\r\\n00:00:01,000 --> 00:00:02,000\\r\\n"
            "<i>Hello</i><br>world &amp; everyone\\r\\n"
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "subtitle.srt"
            path.write_text(source, encoding="utf-8")
            clean_srt_file(path)
            cleaned = path.read_text(encoding="utf-8")
        self.assertIn("Hello\\nworld & everyone", cleaned)
        self.assertIn("00:00:01,000 --> 00:00:02,000", cleaned)
