from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from rclone_manager import (
    RcloneAccountRecord,
    RcloneStorageManager,
    RcloneSecretCipher,
    collect_log_diagnostics,
    format_log_diagnostics_report,
    sanitize_rclone_text,
    is_rclone_backend_supported,
    get_rclone_supported_backends,
    resolve_rclone_binary,
)


class TestRcloneSystem(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ubagen_rclone_test_")
        self.db_path = Path(self.temp_dir) / "test_jobs.sqlite3"
        self.cfg_path = Path(self.temp_dir) / "rclone.conf"
        self.cipher = RcloneSecretCipher(key_file_dir=self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_cipher_encryption_and_decryption(self):
        secret = "super_secret_mega_password_123!"
        enc = self.cipher.encrypt(secret)
        self.assertNotEqual(enc, secret)
        dec = self.cipher.decrypt(enc)
        self.assertEqual(dec, secret)

    def test_account_creation_and_redaction(self):
        mgr = RcloneStorageManager(
            db_path=self.db_path,
            rclone_config_path=self.cfg_path,
            cipher=self.cipher,
        )

        with patch.object(mgr, "test_account_connection", return_value={"ok": True, "message": "Success"}):
            acc = mgr.add_mega_account(
                user="testuser@mega.nz",
                password="MySecretPassword99!",
                remote_name="mega_test_1",
                display_name="Test MEGA Account",
                remote_base_path="UBAgen_Outputs",
                test_after_add=False,
            )

        self.assertEqual(acc["remote_name"], "mega_test_1")
        self.assertEqual(acc["provider"], "mega")

        # Public dict MUST NOT leak the password or encrypted blob
        account_obj = mgr.get_account("mega_test_1")
        self.assertIsNotNone(account_obj)
        public_dict = account_obj.to_public_dict()
        self.assertNotIn("password", public_dict)
        self.assertNotIn("encrypted_password", public_dict)
        self.assertNotIn("MySecretPassword99!", json.dumps(public_dict))

        # Check that rclone.conf contains the remote
        conf_content = self.cfg_path.read_text(encoding="utf-8")
        self.assertIn("[mega_test_1]", conf_content)
        self.assertIn("type = mega", conf_content)

    def test_gdrive_auth_session_and_completion(self):
        mgr = RcloneStorageManager(
            db_path=self.db_path,
            rclone_config_path=self.cfg_path,
            cipher=self.cipher,
        )

        session = mgr.start_gdrive_auth_session(
            remote_name="gdrive_test_1",
            display_name="My Google Drive",
            scope="drive.file",
        )
        self.assertIn("session_id", session)
        self.assertEqual(session["remote_name"], "gdrive_test_1")

        mock_token_payload = json.dumps({
            "access_token": "ya29.mock_token_value_abc",
            "token_type": "Bearer",
            "refresh_token": "1//mock_refresh_token_xyz",
            "expiry": "2030-01-01T00:00:00Z",
        })

        with patch.object(mgr, "test_account_connection", return_value={"ok": True, "message": "Success"}):
            res = mgr.complete_gdrive_auth(
                session_id=session["session_id"],
                token_or_code=mock_token_payload,
                test_after_add=False,
            )

        self.assertTrue(res.get("ok", False) or res.get("status") in ("connected", "ready", "added"))
        acc = mgr.get_account("gdrive_test_1")
        self.assertIsNotNone(acc)
        self.assertEqual(acc.provider, "drive")

        # Verify tokens are NEVER leaked in public representations
        pub = acc.to_public_dict()
        self.assertNotIn("ya29.mock_token_value_abc", json.dumps(pub))
        self.assertNotIn("1//mock_refresh_token_xyz", json.dumps(pub))

    def test_pending_deletions_persisted_and_safe_disconnect(self):
        mgr = RcloneStorageManager(
            db_path=self.db_path,
            rclone_config_path=self.cfg_path,
            cipher=self.cipher,
        )

        # Add account
        mgr.add_mega_account(
            remote_name="remote_disc_test",
            user="user@mega.nz",
            password="pwd",
            display_name="Test Account",
            test_after_add=False,
        )

        # Schedule pending deletion
        due_time = time.time() + 7200
        del_record = mgr.schedule_pending_deletion(
            job_id="job_test_123",
            remote_name="remote_disc_test",
            provider="mega",
            remote_object_path="outputs/job_test_123/video.mp4",
            due_at=due_time,
        )
        self.assertIsNotNone(del_record)
        self.assertEqual(del_record.job_id, "job_test_123")

        # Disconnecting account must NOT delete remote files and MUST preserve pending deletions
        with patch.object(mgr, "_run_rclone_cli") as mock_rclone:
            disc_res = mgr.disconnect_account("remote_disc_test", remove_record=False)
            self.assertTrue(disc_res[0] if isinstance(disc_res, tuple) else disc_res.get("ok", True))
            # Verify no delete command was run on remote
            for call in mock_rclone.call_args_list:
                args = call[0][0] if call[0] else []
                self.assertNotIn("delete", args)
                self.assertNotIn("purge", args)

        # Check pending deletions are still present in database across restart
        mgr2 = RcloneStorageManager(
            db_path=self.db_path,
            rclone_config_path=self.cfg_path,
            cipher=self.cipher,
        )
        pending = mgr2.list_pending_deletions(remote_name="remote_disc_test")
        self.assertEqual(len(pending), 1)
        item = pending[0]
        obj_path = item["remote_object_path"] if isinstance(item, dict) else item.remote_object_path
        self.assertEqual(obj_path, "outputs/job_test_123/video.mp4")

    def test_diagnostic_log_redaction_guarantees(self):
        secret_token = "DISCORD_BOT_TOKEN_XYZ_SECRET_12345"
        sensitive_text = (
            f"Traceback with secret auth={secret_token} and "
            "password=MySuperSecretPassword! and refresh_token=1//xyz987"
        )
        redacted = sanitize_rclone_text(sensitive_text)
        self.assertNotIn("DISCORD_BOT_TOKEN_XYZ_SECRET_12345", redacted)
        self.assertNotIn("MySuperSecretPassword!", redacted)
        self.assertNotIn("1//xyz987", redacted)

        # Check full diagnostics report formatting
        diag = collect_log_diagnostics(run_live_remote_tests=False)
        report = format_log_diagnostics_report(diag)
        self.assertIsInstance(report, str)
        self.assertIn("RCLONE", report)

    def test_rclone_binary_includes_mega_backend(self):
        """Verify that the resolved rclone binary includes the official 'mega' backend."""
        rclone_bin = resolve_rclone_binary(auto_bootstrap=False, require_backend="mega")
        self.assertIsNotNone(rclone_bin, "A valid rclone binary supporting 'mega' must be found.")
        self.assertTrue(os.path.isfile(rclone_bin))
        self.assertTrue(os.access(rclone_bin, os.X_OK))

        backends = get_rclone_supported_backends(rclone_bin)
        self.assertIn(
            "mega",
            backends,
            f"The resolved rclone binary at {rclone_bin} lacks 'mega' backend. Supported backends: {sorted(list(backends))[:15]}...",
        )
        self.assertTrue(is_rclone_backend_supported(rclone_bin, "mega"))

    def test_mega_remote_creation_and_config_format(self):
        """Verify adding a MEGA remote writes 'type = mega', user, and pass properly without credential leaks."""
        mgr = RcloneStorageManager(
            db_path=self.db_path,
            rclone_config_path=self.cfg_path,
            cipher=self.cipher,
        )

        acc = mgr.add_mega_account(
            user="user_sample@test.org",
            password="TestPassword123#",
            remote_name="mega_depolama_test",
            display_name="Mega Depolama Test",
            remote_base_path="UBAgen_Outputs",
            test_after_add=False,
        )
        self.assertEqual(acc["remote_name"], "mega_depolama_test")
        self.assertEqual(acc["provider"], "mega")

        conf_content = self.cfg_path.read_text(encoding="utf-8")
        self.assertIn("[mega_depolama_test]", conf_content)
        self.assertIn("type = mega", conf_content)
        self.assertIn("user = user_sample@test.org", conf_content)
        self.assertIn("pass =", conf_content)
        # Cleartext password must NEVER appear in the configuration file or account dict
        self.assertNotIn("TestPassword123#", conf_content)
        self.assertNotIn("TestPassword123#", json.dumps(acc))

    def test_connection_test_distinguishes_backend_missing_vs_auth_failure(self):
        """Verify that test_account_connection detects missing backend explicitly without misattributing it to auth failure."""
        mgr = RcloneStorageManager(
            db_path=self.db_path,
            rclone_config_path=self.cfg_path,
            cipher=self.cipher,
        )

        mgr.add_mega_account(
            user="user@example.com",
            password="password",
            remote_name="mega_unit_test",
            test_after_add=False,
        )

        # 1. Simulate binary missing the backend
        with patch("rclone_manager.is_rclone_backend_supported", return_value=False), \
             patch("rclone_manager.resolve_rclone_binary", return_value=None):
            result = mgr.test_account_connection("mega_unit_test")
            self.assertFalse(result.get("ok"))
            self.assertTrue(result.get("backend_missing"))
            self.assertIn("depolama backend'ini içermiyor", result.get("error", ""))

        # 2. Simulate invalid credentials (backend is present, but credentials fail)
        class DummyProcess:
            returncode = 1
            stdout = ""
            stderr = "2026/10/12 12:00:00 Failed to create file system for \"mega_unit_test:\": Bad credentials or login failed: [Bad username/password]"

        with patch("rclone_manager.is_rclone_backend_supported", return_value=True), \
             patch.object(mgr, "_run_rclone_cli", return_value=DummyProcess()):
            result2 = mgr.test_account_connection("mega_unit_test")
            # Result must NOT claim success, and must NOT report backend_missing
            self.assertEqual(result2.get("status"), "error")
            self.assertFalse(result2.get("last_test_ok", True))
            self.assertFalse(result2.get("backend_missing", False))
            self.assertIn("Bad credentials", result2.get("last_error", ""))


if __name__ == "__main__":
    unittest.main()
