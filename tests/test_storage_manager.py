"""Unit and integration tests for storage_manager.py, /api/storage, /api/logs, and /api/outputs."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from health_api import create_health_app
from stats import get_recent_logs, install_log_buffer_handler, redact_sensitive_text
from storage_manager import (
    StorageManager,
    detect_deployment_environment,
    inspect_path_metrics,
    parse_proc_mounts,
    verify_directory_writable,
)


def endpoint_for(app, path: str):
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"Route not found: {path}")


class StorageManagerTests(unittest.IsolatedAsyncioTestCase):
    def test_real_runtime_mounts_and_writable_probe(self):
        mounts = parse_proc_mounts()
        self.assertGreater(len(mounts), 0)
        with TemporaryDirectory() as tmp:
            ok, err = verify_directory_writable(Path(tmp))
            self.assertTrue(ok)
            self.assertIsNone(err)
            metrics = inspect_path_metrics(Path(tmp), mounts)
            self.assertTrue(metrics["writable"])
            self.assertIsNotNone(metrics["total_bytes"])
            self.assertGreater(metrics["total_bytes"], 0)
            self.assertIsNotNone(metrics["free_bytes"])

    def test_ephemeral_overlay_does_not_claim_200gb_persistent_guarantee(self):
        with TemporaryDirectory() as tmp:
            mgr = StorageManager(base_storage_dir=tmp)
            fake_mounts = [
                {"device": "none", "mount_point": "/", "fs_type": "overlay", "options": "rw"},
                {"device": "none", "mount_point": "/tmp", "fs_type": "overlay", "options": "rw"},
            ]
            with (
                patch("storage_manager.parse_proc_mounts", return_value=fake_mounts),
                patch.dict(
                    "os.environ",
                    {
                        "UBAGEN_PERSISTENT_VOLUME_VERIFIED": "",
                        "UBAGEN_GCS_BUCKET": "",
                        "GCS_BUCKET_NAME": "",
                        "GOOGLE_DRIVE_CREDENTIALS": "",
                    },
                    clear=False,
                ),
            ):
                report = mgr.inspect()
                self.assertFalse(report["is_persistent"])
                self.assertFalse(report["persistence_verified"])
                self.assertFalse(report["200gb_persistent_guaranteed"])
                self.assertEqual(report["decision_branch"], "B_EPHEMERAL_CONTAINER_STORAGE")
                self.assertEqual(report["storage_mode"], "ephemeral_container_fs")

    def test_persistent_ext4_mount_activates_branch_a(self):
        with TemporaryDirectory() as tmp:
            mgr = StorageManager(base_storage_dir=tmp)
            resolved_tmp = str(Path(tmp).resolve())
            fake_mounts = [
                {"device": "/dev/sdb1", "mount_point": resolved_tmp, "fs_type": "ext4", "options": "rw"},
            ]
            with patch("storage_manager.parse_proc_mounts", return_value=fake_mounts):
                report = mgr.inspect()
                self.assertTrue(report["is_persistent"])
                self.assertTrue(report["persistence_verified"])
                self.assertEqual(report["decision_branch"], "A_PERSISTENT_DISK")
                self.assertEqual(report["storage_mode"], "persistent_disk")

    def test_separated_directories_and_output_lifecycle_and_traversal_guard(self):
        with TemporaryDirectory() as tmp:
            mgr = StorageManager(base_storage_dir=tmp, retention_hours=1.0)
            self.assertNotEqual(mgr.video_outputs_dir, mgr.download_temp_dir)
            self.assertNotEqual(mgr.video_outputs_dir, mgr.encode_temp_dir)

            dl_job = mgr.create_download_job_dir()
            enc_job = mgr.create_encode_job_dir()
            self.assertTrue(dl_job.exists())
            self.assertTrue(enc_job.exists())
            self.assertEqual(dl_job.parent, mgr.download_temp_dir)
            self.assertEqual(enc_job.parent, mgr.encode_temp_dir)

            # Create a sample output artifact inside encode_job and store it to video_outputs
            temp_video = enc_job / "encoded_episode.mp4"
            temp_video.write_bytes(b"video-binary-payload-12345")
            stored = mgr.store_output_file(temp_video, "episode_01_tr.mp4")
            self.assertEqual(stored["filename"], "episode_01_tr.mp4")
            self.assertIn("/api/outputs/episode_01_tr.mp4", stored["download_url"])

            # Clean up temp dirs and verify video_outputs file remains intact
            removed = mgr.cleanup_stale_temp_dirs()
            self.assertEqual(removed, 2)
            self.assertFalse(dl_job.exists())
            self.assertFalse(enc_job.exists())

            outputs = mgr.list_output_files()
            self.assertEqual(len(outputs), 1)
            self.assertEqual(outputs[0]["filename"], "episode_01_tr.mp4")

            # Verify safe resolution and path traversal protection
            resolved = mgr.resolve_output_file("episode_01_tr.mp4")
            self.assertIsNotNone(resolved)
            self.assertIsNone(mgr.resolve_output_file("../main.py"))
            self.assertIsNone(mgr.resolve_output_file("/etc/passwd"))
            self.assertIsNone(mgr.resolve_output_file(".hidden"))

            # Verify TTL expiration cleanup
            out_file = Path(stored["path"])
            old_time = time.time() - 7200
            os.utime(out_file, (old_time, old_time))
            cleanup_res = mgr.cleanup_outputs()
            self.assertEqual(cleanup_res["removed_files"], 1)
            self.assertEqual(mgr.list_output_files(), [])

    def test_log_redaction_never_leaks_tokens(self):
        install_log_buffer_handler()
        secret_bot = "MTIzNDU2Nzg5MDEyMzQ1Njc4.Ga1234.SuperSecretBotToken999"
        secret_admin = "AdminSecretValueXYZ987654"
        with patch.dict(
            "os.environ",
            {"DISCORD_BOT_TOKEN": secret_bot, "UBAGEN_ADMIN_TOKEN": secret_admin},
            clear=False,
        ):
            scrubbed = redact_sensitive_text(
                f"Auth header Bearer {secret_admin} and bot token {secret_bot}"
            )
            self.assertNotIn(secret_bot, scrubbed)
            self.assertNotIn(secret_admin, scrubbed)
            self.assertIn("[REDACTED]", scrubbed)

    async def test_health_api_storage_and_logs_endpoints(self):
        app = create_health_app()
        logging.getLogger("ubagen.test").info("Test log entry for /api/logs verification")

        storage_res = await endpoint_for(app, "/api/storage")()
        storage_body = json.loads(storage_res.body)
        self.assertEqual(storage_res.status_code, 200)
        self.assertEqual(storage_body["status"], "online")
        self.assertIn("storage", storage_body)
        self.assertIn("outputs", storage_body)

        logs_res = await endpoint_for(app, "/api/logs")(limit=50)
        logs_body = json.loads(logs_res.body)
        self.assertEqual(logs_res.status_code, 200)
        self.assertEqual(logs_body["status"], "online")
        self.assertIsInstance(logs_body["logs"], list)

        missing_file_res = await endpoint_for(app, "/api/outputs/{filename}")("nonexistent_file.mp4")
        self.assertEqual(missing_file_res.status_code, 404)


if __name__ == "__main__":
    unittest.main()
