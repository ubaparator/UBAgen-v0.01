import unittest

from subtitle_extract import validate_video_source


class ValidateVideoSourceTests(unittest.TestCase):
    def test_accepts_direct_https_video_url(self):
        ok, value = validate_video_source("https://cdn.example.com/video.mp4?token=abc")
        self.assertTrue(ok)
        self.assertEqual(value, "https://cdn.example.com/video.mp4?token=abc")

    def test_accepts_http_url(self):
        ok, value = validate_video_source("http://media.example.org/watch")
        self.assertTrue(ok)
        self.assertEqual(value, "http://media.example.org/watch")

    def test_preserves_valid_magnet_compatibility(self):
        magnet = "magnet:?xt=urn:btih:0123456789012345678901234567890123456789"
        ok, value = validate_video_source(magnet)
        self.assertTrue(ok)
        self.assertEqual(value, magnet)

    def test_rejects_non_http_scheme(self):
        ok, _ = validate_video_source("file:///etc/passwd")
        self.assertFalse(ok)

    def test_rejects_localhost(self):
        ok, _ = validate_video_source("http://localhost:8080/private")
        self.assertFalse(ok)

    def test_rejects_private_literal_ip(self):
        ok, _ = validate_video_source("http://127.0.0.1/video.mp4")
        self.assertFalse(ok)

    def test_rejects_credentials_in_url(self):
        ok, _ = validate_video_source("https://user:pass@media.example.org/video.mp4")
        self.assertFalse(ok)

    def test_rejects_cloud_metadata_and_internal_hosts(self):
        for bad_url in (
            "http://169.254.169.254/latest/meta-data/",
            "http://metadata.google.internal/computeMetadata/v1/",
            "http://service.internal/video.mp4",
            "http://printer.local/stream.mp4",
            "http://10.10.1.5/video.mp4",
            "http://192.168.1.10/video.mp4",
            "http://[::ffff:127.0.0.1]/video.mp4",
        ):
            ok, _ = validate_video_source(bad_url)
            self.assertFalse(ok, f"Expected SSRF rejection for {bad_url}")

    def test_worker_executes_direct_https_video_source(self):
        import subprocess
        import tempfile
        from pathlib import Path
        from encoder_worker import execute_encode_job_sync
        from job_store import GCSOutputStorage, PersistentJobStore

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "jobs.sqlite3"
            gcs_dir = tmp_path / "gcs_mock"
            gcs_dir.mkdir(parents=True, exist_ok=True)
            gcs = GCSOutputStorage(bucket_name="test-bucket", local_mirror_dir=gcs_dir)
            store = PersistentJobStore(db_path=db_path, gcs_storage=gcs, use_firestore=False)

            sub_file = tmp_path / "external.srt"
            sub_file.write_text("1\n00:00:00,000 --> 00:00:00,800\nMerhaba UBAgen\n", encoding="utf-8")

            job = store.create_job(
                user_id="123456789012345678",
                user_name="tester",
                magnet="https://cdn.example.com/videos/sample.mp4",
                codec="h264",
                subtitle_lang="tur",
                subtitle_source=str(sub_file),
            )

            # Create a tiny valid MP4 using ffmpeg so execute_encode_job_sync can encode it
            def fake_download_runner(source_url: str, download_dir: Path):
                self.assertEqual(source_url, "https://cdn.example.com/videos/sample.mp4")
                out_video = download_dir / "sample.mp4"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        "color=c=black:s=160x90:r=10:d=1",
                        "-f",
                        "lavfi",
                        "-i",
                        "anullsrc=r=44100:cl=mono",
                        "-shortest",
                        "-c:v",
                        "libx264",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "aac",
                        str(out_video),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return subprocess.CompletedProcess(args=["http-download"], returncode=0, stdout=str(out_video), stderr="")

            completed = execute_encode_job_sync(
                job.job_id,
                job_store=store,
                download_runner=fake_download_runner,
            )
            self.assertEqual(completed.status, "completed", f"Expected completed, got {completed.status}: {completed.error_message}")
            self.assertTrue(completed.gcs_upload_verified)


if __name__ == "__main__":
    unittest.main()
