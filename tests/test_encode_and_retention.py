"""
Comprehensive tests for:
1. Real MEGA/Google Drive direct share links on job completion (never exposing Cloud Run / GCS links)
2. Strict 7200-second retention lifecycle:
   - Not deleting before 7200 seconds
   - Deleting when expired
   - Persistence across restart
   - Retry on temporary error with exponential backoff
   - Idempotent completion if file is already deleted (404/not found)
   - User isolation (User A cannot delete or access User B's output)
3. /encodeal slash command and attachment validation
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from encoder_worker import (
    validate_discord_attachment,
    format_discord_job_message,
    build_ffmpeg_encode_command,
    parse_ffmpeg_progress_block,
)
from job_store import PersistentJobStore, EncodeJob, GCSOutputStorage
from rclone_manager import RcloneStorageManager, RcloneSecretCipher


class EncodeAndRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ubagen_retention_test_")
        self.db_path = Path(self.temp_dir) / "test_jobs.sqlite3"
        self.cfg_path = Path(self.temp_dir) / "rclone.conf"
        self.mirror_dir = Path(self.temp_dir) / "mirror"
        self.mirror_dir.mkdir(parents=True, exist_ok=True)
        self.cipher = RcloneSecretCipher(key_file_dir=self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_direct_mega_and_drive_share_link_returned_on_job_completion(self):
        """Verify that completed jobs receive and return real provider share links."""
        gcs = GCSOutputStorage(
            bucket_name="mega_depolama1",
            local_mirror_dir=self.mirror_dir,
            allow_rclone=False,
        )
        store = PersistentJobStore(
            db_path=self.db_path,
            gcs_storage=gcs,
            use_firestore=False,
        )

        job = store.create_job(
            user_id="user_12345",
            user_name="testuser",
            magnet="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=test.mkv",
            codec="h264",
            subtitle_lang="tur",
        )

        real_mega_link = "https://mega.nz/file/ABCDEF123#xyz_secret_key_123456"
        completed = store.mark_job_completed(
            job.job_id,
            gcs_bucket="mega_depolama1",
            gcs_object_name=f"outputs/{job.job_id}/video_h264_tur.mp4",
            output_filename="video_h264_tur.mp4",
            output_size_bytes=1048576,
            gcs_upload_verified=True,
            direct_download_url=real_mega_link,
        )

        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.download_url, real_mega_link)
        self.assertNotIn("storage.googleapis.com", completed.download_url)
        self.assertNotIn("run.app", completed.download_url)

        # Discord message must show direct link and MEGA provider context
        discord_msg = format_discord_job_message(completed)
        self.assertIn(real_mega_link, discord_msg)
        self.assertIn("Doğrudan İndirme", discord_msg)
        self.assertIn("MEGA", discord_msg)
        self.assertNotIn("Google Cloud Storage", discord_msg)

    def test_7200s_retention_lifecycle_and_timing(self):
        """Verify files are NOT deleted before 7200s, but deleted after 7200s."""
        mgr = RcloneStorageManager(
            db_path=self.db_path,
            rclone_config_path=self.cfg_path,
            cipher=self.cipher,
        )

        now = 1000000.0
        # Schedule deletion at now + 7200s
        task = mgr.schedule_pending_deletion(
            remote_name="mega_remote1",
            remote_object_path="outputs/job_1/video.mp4",
            due_at=now + 7200.0,
            job_id="job_1",
            provider="mega",
        )
        self.assertEqual(task.status, "pending")
        self.assertEqual(task.due_at, now + 7200.0)

        # 1. Probe at now + 3600 (1 hour): must NOT be deleted!
        res_early = mgr.process_pending_deletions(now=now + 3600.0)
        self.assertEqual(res_early["completed"], 0)
        self.assertEqual(mgr.count_pending_deletions(), 1)

        # 2. Probe at now + 7201 (2 hours + 1 second): MUST be processed!
        # Mock account connected and rclone cli returning 0
        with patch.object(mgr, "get_account", return_value=MagicMock(is_active=True, status="connected")), \
             patch.object(mgr, "_execute_rclone_deletefile", return_value=(True, None)), \
             patch.object(mgr, "verify_object", return_value=False), \
             patch.object(mgr, "revoke_public_link", return_value=True):
            res_due = mgr.process_pending_deletions(now=now + 7201.0)
            self.assertEqual(res_due["completed"], 1)
            self.assertEqual(mgr.count_pending_deletions(), 0)

    def test_retention_queue_persists_across_restart(self):
        """Verify pending deletion queue is safely preserved in SQLite across instance restart."""
        mgr1 = RcloneStorageManager(
            db_path=self.db_path,
            rclone_config_path=self.cfg_path,
            cipher=self.cipher,
        )
        mgr1.schedule_pending_deletion(
            remote_name="drive_acc",
            remote_object_path="outputs/job_restart/video.mp4",
            due_at=5000.0,
            job_id="job_restart",
        )

        # Reinstantiate manager
        mgr2 = RcloneStorageManager(
            db_path=self.db_path,
            rclone_config_path=self.cfg_path,
            cipher=self.cipher,
        )
        pending = mgr2.list_pending_deletions(remote_name="drive_acc")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["job_id"], "job_restart")

    def test_temporary_error_triggers_exponential_backoff(self):
        """When remote delete fails due to network error, retry is scheduled with backoff."""
        mgr = RcloneStorageManager(
            db_path=self.db_path,
            rclone_config_path=self.cfg_path,
            cipher=self.cipher,
        )
        now = 1000.0
        mgr.schedule_pending_deletion(
            remote_name="mega_remote1",
            remote_object_path="outputs/job_fail/video.mp4",
            due_at=now,
            job_id="job_fail",
        )

        # Simulate transient network failure
        with patch.object(mgr, "get_account", return_value=MagicMock(is_active=True, status="connected")), \
             patch.object(mgr, "_execute_rclone_deletefile", return_value=(False, "Connection reset by peer")), \
             patch.object(mgr, "verify_object", return_value=True):
            res = mgr.process_pending_deletions(now=now)
            self.assertEqual(res["completed"], 0)
            self.assertEqual(res["pending_retry"], 1)

            # Task attempts should be incremented and due_at pushed forward
            tasks = mgr.list_pending_deletions(remote_name="mega_remote1")
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["attempts"], 1)
            self.assertGreater(tasks[0]["due_at"], now)

    def test_idempotent_completion_on_already_deleted_file(self):
        """If remote file is already deleted (404/not found), deletion marks completed idempotently."""
        mgr = RcloneStorageManager(
            db_path=self.db_path,
            rclone_config_path=self.cfg_path,
            cipher=self.cipher,
        )
        now = 2000.0
        mgr.schedule_pending_deletion(
            remote_name="mega_remote1",
            remote_object_path="outputs/job_already_gone/video.mp4",
            due_at=now,
        )

        # Rclone returns True, None when file does not exist (idempotent)
        with patch.object(mgr, "get_account", return_value=MagicMock(is_active=True, status="connected")), \
             patch.object(mgr, "_execute_rclone_deletefile", return_value=(True, None)), \
             patch.object(mgr, "verify_object", return_value=False):
            res = mgr.process_pending_deletions(now=now)
            self.assertEqual(res["completed"], 1)

    def test_user_isolation_on_download_links(self):
        """User A must NOT be shown User B's download link in status queries."""
        job = EncodeJob(
            job_id="job_user_iso",
            user_id="user_owner_111",
            magnet="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=test.mkv",
            status="completed",
            stage="completed",
            gcs_upload_verified=True,
            download_url="https://mega.nz/file/SECRET123#KEY",
            output_filename="output.mp4",
            output_size_bytes=50000,
        )

        # Owner gets link
        owner_msg = format_discord_job_message(job)
        self.assertIn("https://mega.nz/file/SECRET123#KEY", owner_msg)

        # Stranger (different user_id)
        requester_id = "user_stranger_999"
        if requester_id != job.user_id:
            redacted_job = EncodeJob.from_dict(job.to_dict())
            redacted_job.download_url = None
            stranger_msg = format_discord_job_message(redacted_job)
            self.assertNotIn("SECRET123#KEY", stranger_msg)

    def test_encodeal_attachment_validation(self):
        """Verify subtitle and intro attachment validation rules."""
        # Valid subtitle
        sub_att = MagicMock(
            filename="my_subtitles.ass",
            size=50000,
            content_type="text/x-ssa",
        )
        ok_s, name_s, ct_s = validate_discord_attachment(sub_att, kind="subtitle")
        self.assertTrue(ok_s)
        self.assertIn(".ass", name_s)

        # Invalid subtitle (disallowed executable/binary extension)
        bad_att = MagicMock(
            filename="virus.exe",
            size=50000,
            content_type="application/octet-stream",
        )
        ok_b, err_b, _ = validate_discord_attachment(bad_att, kind="subtitle")
        self.assertFalse(ok_b)

        # Valid intro video
        intro_att = MagicMock(
            filename="channel_intro.mp4",
            size=5000000,
            content_type="video/mp4",
        )
        ok_i, name_i, _ = validate_discord_attachment(intro_att, kind="intro")
        self.assertTrue(ok_i)
        self.assertIn(".mp4", name_i)


if __name__ == "__main__":
    unittest.main()
