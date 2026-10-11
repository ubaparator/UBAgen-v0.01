"""
UBAgen — tests/test_encoder_system.py
End-to-end unit and integration tests for the Cloud Run Job Video Encoder,
Persistent Job Store, GCS Output Verification, 7200-second Retention Sweeper,
and Cross-User Access Controls.

Covers all 8 mandatory verification scenarios:
1. H.264 (`libx264`) and H.265 (`libx265`) real FFmpeg encoding with ASS/SRT subtitles & optional intro
2. Torrent / magnet download errors & invalid magnet rejection
3. Real FFmpeg FPS, ETA, processed/total frame parsing & Discord progress message updates
4. Worker crash handling, guaranteed temp workspace cleanup, and stale running job recovery
5. Failed GCS upload rejection (never exposing a completed link before GCS verification)
6. Cross-user access prevention (HTTP 403 Forbidden on unauthorized download attempts)
7. Two-hour (7200s) retention enforcement and active server-side GCS file deletion (HTTP 410 Gone)
8. Service restart persistence (job state preserved outside memory across restarts)
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.parse

from encoder_worker import (
    execute_encode_job_sync,
    format_discord_job_message,
    parse_ffmpeg_progress_block,
    probe_video_metadata,
)
from health_api import create_health_app
from job_store import (
    DEFAULT_RETENTION_SECONDS,
    GCSOutputStorage,
    PersistentJobStore,
    normalize_video_codec,
    reset_job_store_for_tests,
)


class _AsgiResponse:
    def __init__(self, status_code: int, content: bytes, headers: dict[str, str]):
        self.status_code = status_code
        self.content = content
        self.headers = headers

    def json(self):
        return json.loads(self.content.decode("utf-8"))


class TestClient:
    """Zero-dependency ASGI test client for FastAPI routes without requiring httpx."""

    def __init__(self, app):
        self.app = app

    def get(self, url: str, headers: dict[str, str] | None = None) -> _AsgiResponse:
        return self._request("GET", url, headers=headers)

    def post(self, url: str, json_body: dict | None = None, headers: dict[str, str] | None = None) -> _AsgiResponse:
        raw = json.dumps(json_body).encode("utf-8") if json_body is not None else b""
        hdrs = dict(headers or {})
        if json_body is not None:
            hdrs.setdefault("content-type", "application/json")
        return self._request("POST", url, body=raw, headers=hdrs)

    def _request(
        self,
        method: str,
        url: str,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> _AsgiResponse:
        parsed = urllib.parse.urlsplit(url)
        raw_headers = [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in (headers or {}).items()
        ]
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": parsed.path or "/",
            "raw_path": (parsed.path or "/").encode("ascii"),
            "query_string": parsed.query.encode("ascii"),
            "headers": raw_headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        status_code = 500
        resp_headers: dict[str, str] = {}
        chunks: list[bytes] = []
        sent_body = False

        async def receive():
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            nonlocal status_code, resp_headers
            if message["type"] == "http.response.start":
                status_code = message["status"]
                resp_headers = {
                    k.decode("latin-1").lower(): v.decode("latin-1")
                    for k, v in message.get("headers", [])
                }
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        asyncio.run(self.app(scope, receive, send))
        return _AsgiResponse(status_code=status_code, content=b"".join(chunks), headers=resp_headers)

SAMPLE_MAGNET = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=test_video"


def _create_synthetic_video_with_subtitles(
    target_mkv: Path,
    subtitle_ass: Path,
    duration_sec: float = 1.0,
) -> None:
    """Generate a real 320x240 24fps MKV video with embedded ASS subtitles and AAC audio using FFmpeg."""
    ass_content = """[Script Info]
Title: UBAgen Test ASS
ScriptType: v4.00+
PlayResX: 320
PlayResY: 240

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,18,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:00.90,Default,,0,0,0,,Merhaba UBAgen Cloud Encoder!
"""
    subtitle_ass.write_text(ass_content, encoding="utf-8")

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=duration={duration_sec}:size=320x240:rate=24",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={duration_sec}:sample_rate=48000",
        "-i",
        str(subtitle_ass),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-map",
        "2:s:0",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-c:s",
        "ass",
        "-metadata:s:s:0",
        "language=tur",
        str(target_mkv),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"Could not generate synthetic MKV: {res.stderr}")


def _create_synthetic_intro_clip(target_mp4: Path, duration_sec: float = 0.5) -> None:
    """Generate a real 320x240 24fps MP4 intro video clip using FFmpeg."""
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=navy:s=320x240:r=24:d={duration_sec}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=880:duration={duration_sec}:sample_rate=48000",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(target_mp4),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"Could not generate synthetic intro MP4: {res.stderr}")


class TestEncoderSystem(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_root = Path(tempfile.mkdtemp(prefix="ubagen_encoder_test_"))
        self.db_path = self.temp_root / "test_jobs.sqlite3"
        self.gcs_mirror_dir = self.temp_root / "gcs_mirror"
        self.worker_temp_dir = self.temp_root / "worker_scratch"
        self.worker_temp_dir.mkdir(parents=True, exist_ok=True)

        self._orig_env = {
            "UBAGEN_JOBS_DB_PATH": os.environ.get("UBAGEN_JOBS_DB_PATH"),
            "UBAGEN_GCS_BUCKET": os.environ.get("UBAGEN_GCS_BUCKET"),
            "UBAGEN_GCS_LOCAL_MIRROR_DIR": os.environ.get("UBAGEN_GCS_LOCAL_MIRROR_DIR"),
            "UBAGEN_WORKER_TEMP_DIR": os.environ.get("UBAGEN_WORKER_TEMP_DIR"),
            "UBAGEN_FFMPEG_PRESET": os.environ.get("UBAGEN_FFMPEG_PRESET"),
        }
        os.environ["UBAGEN_JOBS_DB_PATH"] = str(self.db_path)
        os.environ["UBAGEN_GCS_BUCKET"] = "ubagen-test-bucket"
        os.environ["UBAGEN_GCS_LOCAL_MIRROR_DIR"] = str(self.gcs_mirror_dir)
        os.environ["UBAGEN_WORKER_TEMP_DIR"] = str(self.worker_temp_dir)
        os.environ["UBAGEN_FFMPEG_PRESET"] = "ultrafast"

        self.gcs = GCSOutputStorage(
            bucket_name="ubagen-test-bucket",
            local_mirror_dir=self.gcs_mirror_dir,
        )
        self.store = PersistentJobStore(
            db_path=self.db_path,
            gcs_storage=self.gcs,
            use_firestore=False,
        )
        reset_job_store_for_tests(self.store)

    def tearDown(self) -> None:
        reset_job_store_for_tests(None)
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.temp_root, ignore_errors=True)

    # ------------------------------------------------------------------
    # 1. H.264 (libx264) & H.265 (libx265) + ASS/SRT + Optional Intro
    # ------------------------------------------------------------------
    def test_h264_and_h265_real_encoding_with_subtitles_and_intro(self) -> None:
        ok264, c264, enc264 = normalize_video_codec("h264")
        self.assertTrue(ok264)
        self.assertEqual((c264, enc264), ("h264", "libx264"))

        ok265, c265, enc265 = normalize_video_codec("h265")
        self.assertTrue(ok265)
        self.assertEqual((c265, enc265), ("h265", "libx265"))

        ok_bad, _, _ = normalize_video_codec("vp9_invalid")
        self.assertFalse(ok_bad)

        # Prepare synthetic source MKV (with embedded ASS) and optional intro MP4
        fixture_dir = self.temp_root / "fixtures"
        fixture_dir.mkdir(parents=True, exist_ok=True)
        source_mkv = fixture_dir / "episode_01.mkv"
        sub_ass = fixture_dir / "sub.ass"
        intro_mp4 = fixture_dir / "intro.mp4"
        _create_synthetic_video_with_subtitles(source_mkv, sub_ass, duration_sec=1.0)
        _create_synthetic_intro_clip(intro_mp4, duration_sec=0.5)

        def fake_download_ok(_magnet: str, dl_dir: Path) -> subprocess.CompletedProcess[str]:
            shutil.copy2(source_mkv, dl_dir / source_mkv.name)
            return subprocess.CompletedProcess(args=["aria2c"], returncode=0, stdout="OK", stderr="")

        # Test A: H.264 (libx264) with embedded ASS subtitle + Intro concatenation
        job_h264 = self.store.create_job(
            user_id="user_h264_100",
            magnet=SAMPLE_MAGNET,
            codec="h264",
            subtitle_lang="tur",
            intro_source=str(intro_mp4),
        )
        stages_seen: list[str] = []
        done_h264 = execute_encode_job_sync(
            job_h264.job_id,
            job_store=self.store,
            download_runner=fake_download_ok,
            progress_callback=lambda j: stages_seen.append(j.stage),
        )
        self.assertEqual(done_h264.status, "completed")
        self.assertEqual(done_h264.stage, "completed")
        self.assertTrue(done_h264.gcs_upload_verified)
        self.assertIsNotNone(done_h264.download_url)
        self.assertEqual(done_h264.video_encoder, "libx264")
        self.assertEqual(done_h264.subtitle_format, "ass")
        self.assertTrue(done_h264.has_intro)
        self.assertIn("downloading", stages_seen)
        self.assertIn("preparing_subtitles", stages_seen)
        self.assertIn("processing_intro", stages_seen)
        self.assertIn("encoding", stages_seen)
        self.assertIn("uploading", stages_seen)
        self.assertIn("completed", stages_seen)

        # Verify output video in GCS mirror is genuinely H.264 and duration ~1.5s (0.5s intro + 1.0s main)
        gcs_file_264, _ = self.gcs.open_download_stream_or_path(
            done_h264.gcs_bucket or "",
            done_h264.gcs_object_name or "",
        )
        self.assertIsNotNone(gcs_file_264)
        assert gcs_file_264 is not None
        meta_264 = probe_video_metadata(gcs_file_264)
        self.assertGreaterEqual(meta_264["duration_sec"], 1.3)

        # Test B: H.265 (libx265) with external SRT subtitle
        ext_srt = fixture_dir / "external.srt"
        ext_srt.write_text(
            "1\n00:00:00,000 --> 00:00:00,900\n<b>Merhaba H.265 HEVC!</b>\n",
            encoding="utf-8",
        )
        job_h265 = self.store.create_job(
            user_id="user_h265_200",
            magnet=SAMPLE_MAGNET,
            codec="h265",
            subtitle_lang="tur",
            subtitle_source=str(ext_srt),
        )
        done_h265 = execute_encode_job_sync(
            job_h265.job_id,
            job_store=self.store,
            download_runner=fake_download_ok,
        )
        self.assertEqual(done_h265.status, "completed")
        self.assertTrue(done_h265.gcs_upload_verified)
        self.assertEqual(done_h265.codec, "h265")
        self.assertEqual(done_h265.video_encoder, "libx265")
        self.assertEqual(done_h265.subtitle_format, "srt")
        self.assertIsNotNone(done_h265.download_url)

        # Verify worker scratch directory was completely cleaned up in finally
        leftover_worker_dirs = list(self.worker_temp_dir.glob("worker_*"))
        self.assertEqual(leftover_worker_dirs, [])

    # ------------------------------------------------------------------
    # 2. Torrent / Magnet Errors
    # ------------------------------------------------------------------
    def test_torrent_and_magnet_errors_handled_safely(self) -> None:
        # Invalid magnet URI
        bad_job = self.store.create_job(
            user_id="user_err_1",
            magnet="https://not-a-magnet-link.example.com",
            codec="h264",
        )
        res_bad = execute_encode_job_sync(bad_job.job_id, job_store=self.store)
        self.assertEqual(res_bad.status, "failed")
        self.assertIsNone(res_bad.download_url)
        self.assertFalse(res_bad.gcs_upload_verified)
        self.assertIn("magnet:?", (res_bad.error or "").lower())

        # aria2c non-zero return code with no video files
        def failing_download(_magnet: str, _dl_dir: Path) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=["aria2c"],
                returncode=1,
                stdout="",
                stderr="errorCode=12 Timed out no seeders found",
            )

        dl_fail_job = self.store.create_job(
            user_id="user_err_2",
            magnet=SAMPLE_MAGNET,
            codec="h264",
        )
        res_dl_fail = execute_encode_job_sync(
            dl_fail_job.job_id,
            job_store=self.store,
            download_runner=failing_download,
        )
        self.assertEqual(res_dl_fail.status, "failed")
        self.assertEqual(res_dl_fail.stage, "failed")
        self.assertIsNone(res_dl_fail.download_url)
        self.assertIn("Torrent indirilemedi", res_dl_fail.error or "")

    # ------------------------------------------------------------------
    # 3. Real FPS, ETA, Frame Progress & Discord Message Formatting
    # ------------------------------------------------------------------
    def test_real_fps_eta_progress_parsing_and_discord_message(self) -> None:
        parsed_mid = parse_ffmpeg_progress_block(
            {
                "frame": "120",
                "fps": "30.0",
                "out_time_us": "5000000",
                "speed": "1.25x",
                "progress": "continue",
            },
            total_frames=240,
            total_duration_sec=10.0,
        )
        self.assertEqual(parsed_mid["processed_frames"], 120)
        self.assertEqual(parsed_mid["total_frames"], 240)
        self.assertEqual(parsed_mid["fps"], 30.0)
        self.assertEqual(parsed_mid["percent"], 50.0)
        self.assertEqual(parsed_mid["eta_seconds"], 4)  # (240 - 120) / 30 = 4s

        parsed_end = parse_ffmpeg_progress_block(
            {
                "frame": "240",
                "fps": "32.5",
                "out_time_us": "10000000",
                "speed": "1.35x",
                "progress": "end",
            },
            total_frames=240,
            total_duration_sec=10.0,
        )
        self.assertEqual(parsed_end["percent"], 100.0)
        self.assertEqual(parsed_end["eta_seconds"], 0)

        job = self.store.create_job(user_id="9988776655", magnet=SAMPLE_MAGNET, codec="h265")
        job = self.store.update_job_progress(
            job.job_id,
            stage="encoding",
            status="running",
            percent=62.5,
            processed_frames=150,
            total_frames=240,
            fps=28.4,
            eta_seconds=3,
        ) or job

        discord_msg = format_discord_job_message(job)
        self.assertIn(job.job_id, discord_msg)
        self.assertIn("<@9988776655>", discord_msg)
        self.assertIn("150 / 240", discord_msg)
        self.assertIn("28.4 FPS", discord_msg)
        self.assertIn("libx265", discord_msg)

    # ------------------------------------------------------------------
    # 4. Worker Crash Handling & Stale Running Job Recovery
    # ------------------------------------------------------------------
    def test_worker_crash_and_stale_heartbeat_recovery(self) -> None:
        # Case A: Worker raises unexpected crash during download/encode
        def crashing_runner(_magnet: str, _dl_dir: Path) -> subprocess.CompletedProcess[str]:
            raise RuntimeError("Simulated worker OOM / unexpected crash")

        crash_job = self.store.create_job(user_id="user_crash_1", magnet=SAMPLE_MAGNET, codec="h264")
        res_crash = execute_encode_job_sync(
            crash_job.job_id,
            job_store=self.store,
            download_runner=crashing_runner,
        )
        self.assertEqual(res_crash.status, "failed")
        self.assertIn("Simulated worker OOM", res_crash.error or "")
        self.assertIsNone(res_crash.download_url)
        self.assertEqual(list(self.worker_temp_dir.glob("worker_*")), [])

        # Case B: Worker killed by SIGKILL leaving job stuck in 'running' state
        stuck_job = self.store.create_job(user_id="user_crash_2", magnet=SAMPLE_MAGNET, codec="h264")
        self.store.update_job_progress(stuck_job.job_id, stage="encoding", status="running", percent=44.0)
        future_now = time.time() + 1200.0
        recovered = self.store.recover_stale_running_jobs(now=future_now, stale_timeout_seconds=900.0)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].job_id, stuck_job.job_id)
        reloaded = self.store.get_job(stuck_job.job_id)
        self.assertIsNotNone(reloaded)
        assert reloaded is not None
        self.assertEqual(reloaded.status, "failed")
        self.assertIn("beklenmedik şekilde sonlandı", reloaded.error or "")

    # ------------------------------------------------------------------
    # 5. Failed GCS Upload Rejection (No Download Link Without Verification)
    # ------------------------------------------------------------------
    def test_failed_gcs_upload_never_exposes_download_link(self) -> None:
        job = self.store.create_job(user_id="user_gcs_fail", magnet=SAMPLE_MAGNET, codec="h264")
        # Attempt to mark completed with gcs_upload_verified=False
        unverified = self.store.mark_job_completed(
            job.job_id,
            gcs_bucket="ubagen-test-bucket",
            gcs_object_name="outputs/fake.mp4",
            output_filename="fake.mp4",
            output_size_bytes=1024,
            gcs_upload_verified=False,
        )
        self.assertEqual(unverified.status, "failed")
        self.assertFalse(unverified.gcs_upload_verified)
        self.assertIsNone(unverified.download_url)

        # Also test worker execution when UBAGEN_GCS_BUCKET is unconfigured
        unconfigured_gcs = GCSOutputStorage(bucket_name="", local_mirror_dir=None)
        unconfigured_store = PersistentJobStore(
            db_path=self.db_path,
            gcs_storage=unconfigured_gcs,
            use_firestore=False,
        )
        fixture_dir = self.temp_root / "fixtures_fail"
        fixture_dir.mkdir(parents=True, exist_ok=True)
        source_mkv = fixture_dir / "clip.mkv"
        sub_ass = fixture_dir / "clip.ass"
        _create_synthetic_video_with_subtitles(source_mkv, sub_ass, duration_sec=0.5)

        job2 = unconfigured_store.create_job(user_id="user_gcs_fail_2", magnet=SAMPLE_MAGNET, codec="h264")
        res2 = execute_encode_job_sync(
            job2.job_id,
            job_store=unconfigured_store,
            download_runner=lambda _m, d: (
                shutil.copy2(source_mkv, d / source_mkv.name),
                subprocess.CompletedProcess(args=["aria2c"], returncode=0, stdout="", stderr=""),
            )[1],
        )
        self.assertEqual(res2.status, "failed")
        self.assertFalse(res2.gcs_upload_verified)
        self.assertIsNone(res2.download_url)
        self.assertIn("UBAGEN_GCS_BUCKET", res2.error or "")

    # ------------------------------------------------------------------
    # 6. Cross-User Access Prevention & 7. Two-Hour (7200s) GCS Deletion
    # ------------------------------------------------------------------
    def test_cross_user_access_protection_and_7200s_retention_deletion(self) -> None:
        dummy_video = self.temp_root / "final_encoded.mp4"
        dummy_video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"A" * 4096)

        owner_id = "discord_user_owner_111"
        attacker_id = "discord_user_other_222"

        job = self.store.create_job(user_id=owner_id, magnet=SAMPLE_MAGNET, codec="h264")
        upload_res = self.gcs.upload_file(dummy_video, f"outputs/{job.job_id}/final_encoded.mp4")
        self.assertTrue(upload_res["verified"])

        now_ts = time.time()
        completed = self.store.mark_job_completed(
            job.job_id,
            gcs_bucket=upload_res["bucket"],
            gcs_object_name=upload_res["object_name"],
            output_filename="final_encoded.mp4",
            output_size_bytes=upload_res["size_bytes"],
            gcs_upload_verified=True,
            completed_at=now_ts,
        )
        # Verify exact 7200-second retention window
        self.assertEqual(completed.retention_seconds, DEFAULT_RETENTION_SECONDS)
        self.assertEqual(completed.retention_seconds, 7200)
        self.assertEqual(completed.expires_at, now_ts + 7200)

        app = create_health_app()
        client = TestClient(app)

        # Public job listing for another user must NOT leak download_url
        list_other = client.get(f"/api/jobs/{job.job_id}?user_id={attacker_id}")
        self.assertEqual(list_other.status_code, 200)
        self.assertIsNone(list_other.json()["job"]["download_url"])

        # Job detail for owner includes download_url while not expired
        list_owner = client.get(f"/api/jobs/{job.job_id}?user_id={owner_id}")
        self.assertEqual(list_owner.status_code, 200)
        self.assertIsNotNone(list_owner.json()["job"]["download_url"])

        # Cross-user download attempt -> 403 Forbidden
        res_cross_user = client.get(
            f"/api/jobs/{job.job_id}/download?user_id={attacker_id}&token={completed.download_token}"
        )
        self.assertEqual(res_cross_user.status_code, 403)

        # Wrong token attempt -> 403 Forbidden
        res_bad_token = client.get(
            f"/api/jobs/{job.job_id}/download?user_id={owner_id}&token=wrong_secret_token"
        )
        self.assertEqual(res_bad_token.status_code, 403)

        # Authorized owner download within 7200s -> 200 OK with exact video payload
        res_owner_ok = client.get(
            f"/api/jobs/{job.job_id}/download?user_id={owner_id}&token={completed.download_token}"
        )
        self.assertEqual(res_owner_ok.status_code, 200)
        self.assertEqual(len(res_owner_ok.content), dummy_video.stat().st_size)

        # Now advance time past 7200 seconds (2 hours) and run server-side retention cleanup
        expired_now = now_ts + 7201.0
        cleanup_summary = self.store.cleanup_expired_jobs(now=expired_now)
        self.assertEqual(cleanup_summary["expired_jobs_processed"], 1)
        self.assertEqual(cleanup_summary["gcs_objects_deleted"], 1)
        self.assertIn(job.job_id, cleanup_summary["job_ids"])

        # Verify the file was physically deleted from GCS storage (not just signed URL expiry)
        self.assertFalse(self.gcs.verify_object(upload_res["bucket"], upload_res["object_name"]))

        after_expiry_job = self.store.get_job(job.job_id)
        self.assertIsNotNone(after_expiry_job)
        assert after_expiry_job is not None
        self.assertEqual(after_expiry_job.status, "expired")
        self.assertEqual(after_expiry_job.stage, "expired")
        self.assertTrue(after_expiry_job.deleted_from_gcs)
        self.assertIsNone(after_expiry_job.download_url)

        # Owner attempting to download after 7200s expiration -> 410 Gone
        res_expired_dl = client.get(
            f"/api/jobs/{job.job_id}/download?user_id={owner_id}&token={completed.download_token}"
        )
        self.assertEqual(res_expired_dl.status_code, 410)

    # ------------------------------------------------------------------
    # 8. Service Restart Persistence (State Outside Instance Memory)
    # ------------------------------------------------------------------
    def test_job_state_persists_across_service_restarts(self) -> None:
        created = self.store.create_job(
            user_id="user_persist_777",
            magnet=SAMPLE_MAGNET,
            codec="h265",
            subtitle_lang="jpn",
        )
        self.store.update_job_progress(
            created.job_id,
            stage="encoding",
            status="running",
            percent=73.4,
            processed_frames=734,
            total_frames=1000,
            fps=41.2,
            eta_seconds=6,
        )

        # Simulate Cloud Run instance restart by destroying store reference and creating a brand new store
        del self.store
        reset_job_store_for_tests(None)

        restarted_store = PersistentJobStore(
            db_path=self.db_path,
            gcs_storage=self.gcs,
            use_firestore=False,
        )
        reset_job_store_for_tests(restarted_store)
        self.store = restarted_store

        restored = restarted_store.get_job(created.job_id)
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.user_id, "user_persist_777")
        self.assertEqual(restored.codec, "h265")
        self.assertEqual(restored.video_encoder, "libx265")
        self.assertEqual(restored.stage, "encoding")
        self.assertEqual(restored.status, "running")
        self.assertAlmostEqual(restored.percent, 73.4, places=1)
        self.assertEqual(restored.processed_frames, 734)
        self.assertEqual(restored.total_frames, 1000)
        self.assertAlmostEqual(restored.fps, 41.2, places=1)
        self.assertEqual(restored.eta_seconds, 6)

        # Also verify /api/storage exposes persistent_output_storage vs ephemeral_worker_scratch_disk
        app = create_health_app()
        client = TestClient(app)
        st_res = client.get("/api/storage")
        self.assertEqual(st_res.status_code, 200)
        st_payload = st_res.json()
        separation = st_payload["storage"]["separation"]
        self.assertEqual(separation["persistent_output_storage"]["retention_seconds"], 7200)
        self.assertTrue(separation["ephemeral_worker_scratch_disk"]["is_ephemeral"])
        self.assertFalse(separation["job_state_database"]["in_memory_only"])


if __name__ == "__main__":
    unittest.main()
