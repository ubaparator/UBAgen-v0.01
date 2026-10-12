"""
Unit tests verifying strict character limit enforcement for /stats:
Discord messages must be strictly <= 1023 characters under ALL input combinations
(normal, empty, degraded, and adversarial worst-case inputs with oversized fields).
"""

from __future__ import annotations

import unittest
from stats import format_compact_stats_message, collect_all_stats, BotTracker


class StatsCharacterLimitTests(unittest.TestCase):
    def test_standard_stats_strictly_under_1024_chars(self):
        """Standard stats output must be concise and strictly <= 1023 characters."""
        stats = {
            "bot": {
                "connected": True,
                "is_ready": True,
                "latency_ms": 23.4,
                "user": "UBAgenBot#1234",
                "uptime_str": "3d 12h 45m",
            },
            "compute": {
                "cpu_usage_pct": 14.2,
                "logical_cores": 4,
                "physical_cores": 2,
                "backend_uptime_str": "3d 12h 45m 10s",
                "system_uptime_str": "15d 4h",
                "os_name": "Linux 6.6.0-x86_64",
            },
            "memory": {
                "system_used_str": "1.45 GB",
                "system_total_str": "7.80 GB",
                "system_percent": 18.6,
                "process_rss_str": "184.20 MB",
            },
            "disk": {
                "used_str": "12.30 GB",
                "total_str": "196.50 GB",
                "percent": 6.3,
                "is_persistent": True,
                "fs_type": "ext4",
                "mount_point": "/app/applet",
            },
            "binaries": {
                "ffmpeg": {"installed": True, "version": "4.4.2"},
                "ffprobe": {"installed": True, "version": "4.4.2"},
                "aria2": {"installed": True, "version": "1.36.0"},
            },
        }

        msg = format_compact_stats_message(stats, max_len=1023)
        self.assertIsInstance(msg, str)
        self.assertLessEqual(len(msg), 1023, f"Output exceeded 1023 characters: len={len(msg)}")
        self.assertIn("UBAgen Sistem Durumu", msg)
        self.assertIn("Gateway", msg)
        self.assertIn("CPU", msg)
        self.assertIn("RAM", msg)
        self.assertIn("Disk", msg)

    def test_empty_and_degraded_dictionary_handling(self):
        """Empty or degraded dictionary must produce valid output <= 1023 chars without crashing."""
        for sample in ({}, {"status": "degraded"}, {"bot": None, "compute": {}}):
            msg = format_compact_stats_message(sample, max_len=1023)
            self.assertIsInstance(msg, str)
            self.assertLessEqual(len(msg), 1023)
            self.assertGreater(len(msg), 0)

    def test_adversarial_oversized_fields_strictly_clamped_to_1023(self):
        """Adversarial worst case with 5000+ char strings must strictly remain <= 1023 chars."""
        oversized_text = "A" * 6000
        stats = {
            "bot": {
                "connected": False,
                "is_ready": False,
                "user": oversized_text,
                "status": oversized_text,
                "diagnostic_message": oversized_text,
                "latency_ms": 999999.0,
            },
            "compute": {
                "cpu_usage_pct": 99.9,
                "logical_cores": 128,
                "os_name": oversized_text,
                "backend_uptime_str": oversized_text,
            },
            "memory": {
                "system_used_str": oversized_text,
                "system_total_str": oversized_text,
                "process_rss_str": oversized_text,
            },
            "disk": {
                "mount_point": oversized_text,
                "fs_type": oversized_text,
                "used_str": oversized_text,
                "total_str": oversized_text,
            },
            "binaries": {
                "ffmpeg": {"installed": True, "version": oversized_text},
                "aria2": {"installed": True, "version": oversized_text},
            },
        }
        huge_wake_note = "⚡ " + "WAKE_" * 800 + "\n"

        msg = format_compact_stats_message(stats, max_len=1023, wake_note=huge_wake_note)
        self.assertIsInstance(msg, str)
        self.assertLessEqual(
            len(msg),
            1023,
            f"Adversarial stats output reached {len(msg)} characters! Must be <= 1023.",
        )

    def test_exact_character_limit_boundaries(self):
        """Ensure boundary enforcement at custom limits like 250, 500, 1023."""
        stats = {"bot": {"connected": True, "is_ready": True}}
        for boundary in (200, 500, 1023):
            msg = format_compact_stats_message(stats, max_len=boundary)
            self.assertLessEqual(len(msg), boundary)


if __name__ == "__main__":
    unittest.main()
