"""
Unit and integration tests for Discord Google Drive OAuth 2.0 account linking (`google_drive_oauth.py`).

Covers:
1. OAuth 2.0 Authorization URL generation, PKCE (S256), and least-privilege scope (`drive.file`).
2. CSRF `state` validation (single-use atomic consumption, expiration, replay rejection, tampered state rejection).
3. Callback success and error scenarios (user denial `access_denied`, invalid code, missing config, FastAPI routes).
4. Authenticated encryption/decryption of refresh & access tokens (`TOKEN_ENCRYPTION_KEY`, Fernet + AEAD v1 fallback).
5. Strict per-Discord-user isolation, idempotent re-linking (no duplicate rows), and cross-user protection.
6. Automatic access token refresh, `invalid_grant` revocation detection, and `/drive_ayir` remote revocation + deletion.
7. Discord slash commands (`/drive_bagla`, `/drive_durum`, `/drive_ayir`, `/drive_yardim`) via Gateway & HTTP Interactions.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

from google_drive_oauth import (
    GOOGLE_DRIVE_FILE_SCOPE,
    DriveOAuthConfigError,
    DriveOAuthStateError,
    DriveOAuthStore,
    DriveOAuthTokenError,
    TokenCipher,
    build_drive_help_message,
    get_drive_oauth_config_status,
    handle_drive_ayir_for_user,
    handle_drive_bagla_for_user,
    handle_drive_durum_for_user,
    handle_google_oauth_callback_request,
    register_drive_oauth_commands,
    validate_redirect_uri,
)
from health_api import create_health_app
from stats import redact_sensitive_text


class _FakeHTTPResponse:
    def __init__(self, status: int, payload: dict | str) -> None:
        self.status = status
        if isinstance(payload, dict):
            self._raw = json.dumps(payload).encode("utf-8")
        else:
            self._raw = str(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class TestGoogleDriveOAuth(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_oauth.sqlite3"
        self.test_env = {
            "GOOGLE_CLIENT_ID": "1234567890-testclient.apps.googleusercontent.com",
            "GOOGLE_CLIENT_SECRET": "GOCSPX-test_secret_value_do_not_log_999",
            "GOOGLE_REDIRECT_URI": "https://ubagen.example.com/api/oauth/google/callback",
            "TOKEN_ENCRYPTION_KEY": "test_master_encryption_key_32_bytes_minimum_length!",
            "UBAGEN_JOBS_DB_PATH": str(self.db_path),
        }
        self.env_patcher = patch.dict(os.environ, self.test_env, clear=False)
        self.env_patcher.start()

    def tearDown(self) -> None:
        self.env_patcher.stop()
        self.temp_dir.cleanup()

    def _make_mock_opener(
        self,
        *,
        token_status: int = 200,
        token_payload: dict | None = None,
        about_status: int = 200,
        about_payload: dict | None = None,
        revoke_status: int = 200,
        captured_requests: list | None = None,
    ):
        def _opener(req, timeout=15.0):
            url = req.full_url
            body = req.data.decode("utf-8") if req.data else ""
            if captured_requests is not None:
                captured_requests.append({"url": url, "method": req.get_method(), "body": body})

            if "oauth2.googleapis.com/token" in url:
                payload = (
                    token_payload
                    if token_payload is not None
                    else {
                        "access_token": "ya29.mock_access_token_value_123456",
                        "refresh_token": "1//0mock_refresh_token_value_abcdef",
                        "expires_in": 3600,
                        "scope": GOOGLE_DRIVE_FILE_SCOPE,
                        "token_type": "Bearer",
                    }
                )
                return _FakeHTTPResponse(token_status, payload)

            if "googleapis.com/drive/v3/about" in url:
                payload = (
                    about_payload
                    if about_payload is not None
                    else {
                        "user": {
                            "displayName": "UBA Test User",
                            "emailAddress": "uba.user@example.com",
                        }
                    }
                )
                return _FakeHTTPResponse(about_status, payload)

            if "oauth2.googleapis.com/revoke" in url:
                return _FakeHTTPResponse(revoke_status, {})

            raise AssertionError(f"Unexpected HTTP request URL in test: {url}")

        return _opener

    # ------------------------------------------------------------------
    # 1. OAuth Authorization URL, Least-Privilege Scope & Redirect Validation
    # ------------------------------------------------------------------
    def test_authorization_url_generation_and_least_privilege_scope(self) -> None:
        store = DriveOAuthStore(db_path=self.db_path)
        auth_req = store.create_authorization_request(
            discord_user_id="111222333444555666",
            discord_user_name="ubaparator",
            now=1_700_000_000.0,
        )

        self.assertEqual(auth_req["scope"], GOOGLE_DRIVE_FILE_SCOPE)
        self.assertEqual(auth_req["expires_in_seconds"], 600)
        parsed = urllib.parse.urlsplit(auth_req["authorization_url"])
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "accounts.google.com")
        params = dict(urllib.parse.parse_qsl(parsed.query))

        self.assertEqual(params["client_id"], self.test_env["GOOGLE_CLIENT_ID"])
        self.assertEqual(params["redirect_uri"], self.test_env["GOOGLE_REDIRECT_URI"])
        self.assertEqual(params["response_type"], "code")
        self.assertEqual(params["scope"], "https://www.googleapis.com/auth/drive.file")
        self.assertEqual(params["access_type"], "offline")
        self.assertEqual(params["prompt"], "consent")
        self.assertEqual(params["code_challenge_method"], "S256")
        self.assertTrue(len(params["code_challenge"]) >= 40)
        self.assertEqual(params["state"], auth_req["state"])

    def test_redirect_uri_validation_rules(self) -> None:
        ok_https, _ = validate_redirect_uri("https://app.example.com/api/oauth/google/callback")
        self.assertTrue(ok_https)

        ok_local, _ = validate_redirect_uri("http://localhost:3000/api/oauth/google/callback")
        self.assertTrue(ok_local)

        ok_http_remote, err_http = validate_redirect_uri("http://example.com/api/oauth/google/callback")
        self.assertFalse(ok_http_remote)
        self.assertIn("https://", err_http)

        ok_creds, _ = validate_redirect_uri("https://user:pass@example.com/api/oauth/google/callback")
        self.assertFalse(ok_creds)

    # ------------------------------------------------------------------
    # 2. State CSRF Protection, Replay Prevention, Expiration & Tampering
    # ------------------------------------------------------------------
    def test_state_csrf_validation_single_use_and_expiry(self) -> None:
        store = DriveOAuthStore(db_path=self.db_path)
        now_ts = 1_700_000_000.0
        req = store.create_authorization_request(
            discord_user_id="111222333444555666",
            discord_user_name="user_one",
            ttl_seconds=600,
            now=now_ts,
        )
        state_token = req["state"]

        # Tampered or missing state must be rejected
        with self.assertRaises(DriveOAuthStateError):
            store.consume_state(None, now=now_ts + 10)
        with self.assertRaises(DriveOAuthStateError):
            store.consume_state("tampered_state_token_that_does_not_exist_12345", now=now_ts + 10)

        # Valid first consumption succeeds and returns bound Discord user
        consumed = store.consume_state(state_token, now=now_ts + 30)
        self.assertEqual(consumed["discord_user_id"], "111222333444555666")
        self.assertEqual(consumed["discord_user_name"], "user_one")

        # Second consumption (replay attack) must fail
        with self.assertRaises(DriveOAuthStateError):
            store.consume_state(state_token, now=now_ts + 31)

        # Expired state must fail
        req_exp = store.create_authorization_request(
            discord_user_id="111222333444555666",
            discord_user_name="user_one",
            ttl_seconds=120,
            now=now_ts,
        )
        with self.assertRaises(DriveOAuthStateError):
            store.consume_state(req_exp["state"], now=now_ts + 121.0)

    # ------------------------------------------------------------------
    # 3. Token Encryption & Decryption (Fernet + Pure-Python AEAD v1)
    # ------------------------------------------------------------------
    def test_token_encryption_decryption_and_wrong_key_rejection(self) -> None:
        cipher_a = TokenCipher("first_secret_encryption_key_for_testing_12345")
        cipher_b = TokenCipher("second_different_encryption_key_for_testing_999")

        raw_refresh = "1//0g_sensitive_refresh_token_value_secret"
        encrypted = cipher_a.encrypt(raw_refresh)
        self.assertNotIn(raw_refresh, encrypted)
        self.assertTrue(encrypted.startswith(("fernet$", "v1$")))
        self.assertEqual(cipher_a.decrypt(encrypted), raw_refresh)

        # Decrypting with wrong key must raise DriveOAuthTokenError
        with self.assertRaises(DriveOAuthTokenError):
            cipher_b.decrypt(encrypted)

        # Also explicitly verify pure-Python v1$ AEAD fallback
        with patch("google_drive_oauth.Fernet", None):
            fallback_cipher = TokenCipher("first_secret_encryption_key_for_testing_12345")
            v1_blob = fallback_cipher.encrypt(raw_refresh)
            self.assertTrue(v1_blob.startswith("v1$"))
            self.assertNotIn(raw_refresh, v1_blob)
            self.assertEqual(fallback_cipher.decrypt(v1_blob), raw_refresh)
            with self.assertRaises(DriveOAuthTokenError):
                TokenCipher("wrong_key_for_v1_fallback_verification_0000").decrypt(v1_blob)

    def test_sensitive_token_and_secret_redaction(self) -> None:
        raw_log = (
            f"Secret={self.test_env['GOOGLE_CLIENT_SECRET']} "
            f"EncKey={self.test_env['TOKEN_ENCRYPTION_KEY']} "
            "Access=ya29.a0AfB_byCdefghijklmnopqrstuvwxyz123456 "
            "Refresh=1//04abcdefghijklmnopqrstuvwxyz987654321"
        )
        scrubbed = redact_sensitive_text(raw_log)
        self.assertNotIn(self.test_env["GOOGLE_CLIENT_SECRET"], scrubbed)
        self.assertNotIn(self.test_env["TOKEN_ENCRYPTION_KEY"], scrubbed)
        self.assertNotIn("ya29.a0AfB_byCdefghijklmnopqrstuvwxyz123456", scrubbed)
        self.assertNotIn("1//04abcdefghijklmnopqrstuvwxyz987654321", scrubbed)

    # ------------------------------------------------------------------
    # 4. Callback Success, Error Scenarios, Multi-User Isolation & Re-linking
    # ------------------------------------------------------------------
    def test_callback_success_encrypted_storage_and_idempotent_relink(self) -> None:
        captured: list = []
        opener = self._make_mock_opener(captured_requests=captured)
        store = DriveOAuthStore(db_path=self.db_path, http_opener=opener)

        user_1 = "100000000000000001"
        user_2 = "200000000000000002"

        req_1 = store.create_authorization_request(discord_user_id=user_1, discord_user_name="alice")
        conn_1 = store.complete_oauth_callback(state=req_1["state"], code="auth_code_alice_1")

        self.assertEqual(conn_1.discord_user_id, user_1)
        self.assertEqual(conn_1.google_email, "uba.user@example.com")
        self.assertEqual(conn_1.status, "active")

        # Verify SQLite stores ONLY encrypted tokens, never plaintext
        with sqlite3.connect(str(self.db_path)) as db:
            row = db.execute(
                "SELECT encrypted_refresh_token, encrypted_access_token FROM drive_connections WHERE discord_user_id = ?;",
                (user_1,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertNotIn("1//0mock_refresh_token_value_abcdef", row[0])
            self.assertNotIn("ya29.mock_access_token_value_123456", row[1])

        # User 2 is completely isolated and initially not connected
        self.assertIsNone(store.get_connection(user_2))
        durum_user_2 = handle_drive_durum_for_user(discord_user_id=user_2, store=store)
        self.assertIn("Bağlı Değil", durum_user_2)
        self.assertNotIn("uba.user@example.com", durum_user_2)

        # Re-linking User 1 updates existing row without creating duplicate records
        req_1_relink = store.create_authorization_request(discord_user_id=user_1, discord_user_name="alice_updated")
        conn_1_updated = store.complete_oauth_callback(state=req_1_relink["state"], code="auth_code_alice_2")
        self.assertEqual(conn_1_updated.discord_user_name, "alice_updated")

        with sqlite3.connect(str(self.db_path)) as db:
            count = db.execute("SELECT COUNT(*) FROM drive_connections WHERE discord_user_id = ?;", (user_1,)).fetchone()[0]
            self.assertEqual(count, 1)

    def test_callback_error_scenarios_access_denied_and_exchange_failure(self) -> None:
        store = DriveOAuthStore(
            db_path=self.db_path,
            http_opener=self._make_mock_opener(token_status=400, token_payload={"error": "invalid_grant"}),
        )
        req_denied = store.create_authorization_request(discord_user_id="300000000000000003")
        with self.assertRaises(DriveOAuthTokenError) as ctx_denied:
            store.complete_oauth_callback(state=req_denied["state"], error="access_denied")
        self.assertIn("reddedildi", str(ctx_denied.exception))

        req_bad_code = store.create_authorization_request(discord_user_id="300000000000000003")
        with self.assertRaises(DriveOAuthTokenError) as ctx_exchange:
            store.complete_oauth_callback(state=req_bad_code["state"], code="expired_auth_code")
        self.assertIn("invalid_grant", str(ctx_exchange.exception))

    # ------------------------------------------------------------------
    # 5. Token Refresh, Invalid Grant Revocation Detection & Disconnect (/drive_ayir)
    # ------------------------------------------------------------------
    def test_token_refresh_revoked_detection_and_cross_user_disconnect_safety(self) -> None:
        now_ts = 1_700_000_000.0
        captured: list = []
        opener = self._make_mock_opener(captured_requests=captured)
        store = DriveOAuthStore(db_path=self.db_path, http_opener=opener)

        user_owner = "400000000000000004"
        user_other = "500000000000000005"

        req = store.create_authorization_request(discord_user_id=user_owner, now=now_ts)
        store.complete_oauth_callback(state=req["state"], code="code_owner", now=now_ts)

        # Within expiry window -> returns cached decrypted access token without hitting token endpoint again
        pre_count = len(captured)
        token_cached = store.get_valid_access_token(user_owner, now=now_ts + 120)
        self.assertEqual(token_cached, "ya29.mock_access_token_value_123456")
        self.assertEqual(len(captured), pre_count)

        # Past expiry window -> automatically refreshes using decrypted refresh_token
        store._http_opener = self._make_mock_opener(
            token_status=200,
            token_payload={"access_token": "ya29.refreshed_access_token_999", "expires_in": 3600},
            captured_requests=captured,
        )
        token_refreshed = store.get_valid_access_token(user_owner, now=now_ts + 4000)
        self.assertEqual(token_refreshed, "ya29.refreshed_access_token_999")

        # Simulate user revoking permission in Google Account settings (invalid_grant on refresh)
        store._http_opener = self._make_mock_opener(
            token_status=400,
            token_payload={"error": "invalid_grant"},
        )
        with self.assertRaises(DriveOAuthTokenError):
            store.get_valid_access_token(user_owner, now=now_ts + 9000)
        conn_after_revoke = store.get_connection(user_owner)
        self.assertIsNotNone(conn_after_revoke)
        assert conn_after_revoke is not None
        self.assertEqual(conn_after_revoke.status, "revoked")

        # Calling /drive_ayir as user_other must NOT affect user_owner
        ayir_other_msg = handle_drive_ayir_for_user(discord_user_id=user_other, store=store)
        self.assertIn("Bulunamadı", ayir_other_msg)
        self.assertIsNotNone(store.get_connection(user_owner))

        # Calling /drive_ayir as user_owner revokes remotely and deletes record
        store._http_opener = self._make_mock_opener(revoke_status=200, captured_requests=captured)
        ayir_owner_msg = handle_drive_ayir_for_user(discord_user_id=user_owner, store=store)
        self.assertIn("Kaldırıldı", ayir_owner_msg)
        self.assertIsNone(store.get_connection(user_owner))

    # ------------------------------------------------------------------
    # 6. FastAPI Callback & Status Endpoints + Discord Slash Commands
    # ------------------------------------------------------------------
    def test_fastapi_oauth_callback_and_slash_commands(self) -> None:
        opener = self._make_mock_opener()
        store = DriveOAuthStore(db_path=self.db_path, http_opener=opener)

        with patch("google_drive_oauth._GLOBAL_DRIVE_OAUTH_STORE", store):
            app = create_health_app()
            route_paths = {getattr(r, "path", None) for r in app.routes}
            self.assertIn("/api/oauth/google/callback", route_paths)
            self.assertIn("/oauth/google/callback", route_paths)
            self.assertIn("/api/oauth/google/status", route_paths)

            # Check get_drive_oauth_config_status does not leak secrets
            status_json = get_drive_oauth_config_status()
            self.assertTrue(status_json["configured"])
            self.assertEqual(status_json["scope"], GOOGLE_DRIVE_FILE_SCOPE)
            self.assertNotIn(self.test_env["GOOGLE_CLIENT_SECRET"], json.dumps(status_json))
            self.assertNotIn(self.test_env["TOKEN_ENCRYPTION_KEY"], json.dumps(status_json))

            # /drive_bagla command message
            discord_uid = "777888999000111222"
            bagla_msg = handle_drive_bagla_for_user(
                discord_user_id=discord_uid,
                discord_user_name="tester_777",
                store=store,
            )
            self.assertIn("accounts.google.com/o/oauth2/v2/auth", bagla_msg)
            self.assertIn(GOOGLE_DRIVE_FILE_SCOPE, bagla_msg)

            # Extract generated state from URL in message
            start_idx = bagla_msg.index("https://accounts.google.com")
            end_idx = bagla_msg.index(")", start_idx)
            auth_url = bagla_msg[start_idx:end_idx]
            query_params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(auth_url).query))
            state_val = query_params["state"]

            # Complete callback via handler (even if an attacker tries to supply another Discord ID elsewhere)
            status_code, cb_json, cb_html = handle_google_oauth_callback_request(
                state=state_val,
                code="valid_code_777",
                store=store,
            )
            self.assertEqual(status_code, 200)
            self.assertEqual(cb_json["status"], "connected")
            self.assertEqual(cb_json["discord_user_id"], discord_uid)
            self.assertIn("Google Drive Hesabınız Bağlandı", cb_html)
            self.assertNotIn("ya29.", cb_html)
            self.assertNotIn("1//0", cb_html)

            # Replaying the same callback state must return 400 invalid_state
            replay_code, replay_json, _ = handle_google_oauth_callback_request(
                state=state_val,
                code="valid_code_777",
                store=store,
            )
            self.assertEqual(replay_code, 400)
            self.assertEqual(replay_json["error"], "invalid_state")

            # Check /drive_durum and /drive_yardim
            durum_msg = handle_drive_durum_for_user(discord_user_id=discord_uid, store=store)
            self.assertIn("Bağlı (Aktif)", durum_msg)
            self.assertIn("uba.user@example.com", durum_msg)

            yardim_msg = build_drive_help_message()
            self.assertIn("/drive_bagla", yardim_msg)
            self.assertIn("/drive_durum", yardim_msg)
            self.assertIn("/drive_ayir", yardim_msg)
            self.assertIn("/drive_yardim", yardim_msg)

    def test_discord_command_tree_removes_legacy_drive_commands(self) -> None:
        from main import UBAgenBot

        bot = UBAgenBot()
        asyncio.run(bot.setup_hook())
        command_names = [cmd.name for cmd in bot.tree.get_commands()]
        registered_names = set(command_names)
        for removed_cmd in ("drive_bagla", "drive_durum", "drive_ayir", "drive_yardim", "drive"):
            self.assertNotIn(removed_cmd, registered_names)
        self.assertIn("log", registered_names)
        self.assertEqual(len(command_names), len(set(command_names)), "Slash command names must be unique")


if __name__ == "__main__":
    unittest.main()
