"""Comprehensive unit and integration tests for /altyazı_ayıklama (subtitle_extract.py)."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import discord

from main import UBAgenBot
from subtitle_extract import (
    BT_TRACKER_LIST,
    COLAB_TRACKERS,
    COMMAND_NAME,
    MAX_INPUT,
    build_aria2c_command,
    check_runtime_binary,
    clean_srt_file,
    create_job_workdir,
    ensure_runtime_binaries,
    execute_subtitle_extraction,
    find_video_files,
    log_runtime_binaries_status,
    register_subtitle_extract_command,
    select_subtitle_stream,
    subtitle_format,
    validate_magnet_uri,
)


class FakeResponse:
    def __init__(self) -> None:
        self.deferred = False
        self.thinking = False

    def is_done(self) -> bool:
        return self.deferred

    async def defer(self, *, thinking: bool = False, ephemeral: bool = False) -> None:
        self.deferred = True
        self.thinking = thinking


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, content: str = "", **kwargs) -> None:
        self.messages.append({"content": content, **kwargs})


class FakeInteraction:
    def __init__(self) -> None:
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.progress_updates: list[str] = []

    async def edit_original_response(self, *, content: str = "", **kwargs) -> None:
        self.progress_updates.append(content)


class SubtitleExtractTests(unittest.IsolatedAsyncioTestCase):
    def test_upload_limit_is_1_5_gb_decimal(self):
        self.assertEqual(MAX_INPUT, 1_500_000_000)

    def test_command_registration_and_parameters(self):
        bot = UBAgenBot()
        cmd = register_subtitle_extract_command(bot.tree)
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.name, "altyazı_ayıklama")
        self.assertEqual(COMMAND_NAME, "altyazı_ayıklama")

        registered = bot.tree.get_command("altyazı_ayıklama")
        self.assertIsNotNone(registered)
        params = {p.name: p for p in registered.parameters}
        self.assertIn("magnet", params)
        self.assertIn("dil", params)
        self.assertTrue(params["magnet"].required)
        self.assertFalse(params["dil"].required)
        self.assertEqual(params["dil"].default, "tur")

    def test_validate_magnet_uri(self):
        ok, val = validate_magnet_uri(
            "  magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Test  "
        )
        self.assertTrue(ok)
        self.assertTrue(val.startswith("magnet:?xt=urn:btih:"))

        ok_empty, _ = validate_magnet_uri("   ")
        self.assertFalse(ok_empty)

        ok_http, _ = validate_magnet_uri("https://example.com/file.torrent")
        self.assertFalse(ok_http)

        ok_no_btih, _ = validate_magnet_uri("magnet:?dn=MissingHash")
        self.assertFalse(ok_no_btih)

    def test_build_aria2c_command_preserves_colab_settings(self):
        with TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            magnet = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
            cmd = build_aria2c_command(magnet, workdir)
            self.assertEqual(cmd[0], "aria2c")
            self.assertIn("--seed-time=0", cmd)
            self.assertIn("--max-connection-per-server=16", cmd)
            self.assertIn("--split=16", cmd)
            self.assertIn("--enable-dht=true", cmd)
            self.assertIn(f"--bt-tracker={BT_TRACKER_LIST}", cmd)
            for tracker in COLAB_TRACKERS:
                self.assertIn(tracker, BT_TRACKER_LIST)
            self.assertIn(str(workdir), cmd)
            self.assertEqual(cmd[-1], magnet)

    def test_isolated_job_workdir_under_download_temp(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "download_temp"
            dir1 = create_job_workdir(base)
            dir2 = create_job_workdir(base)
            self.assertTrue(dir1.exists())
            self.assertTrue(dir2.exists())
            self.assertNotEqual(dir1, dir2)
            self.assertEqual(dir1.parent, base)
            self.assertEqual(dir2.parent, base)

    def test_find_video_files_prioritizes_mkv_and_mp4_by_size(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample_mkv = root / "sample.mkv"
            main_mkv = root / "nested" / "episode.mkv"
            avi_file = root / "larger_legacy.avi"
            aria2_ctrl = root / "episode.mkv.aria2"
            empty_mp4 = root / "empty.mp4"

            (root / "nested").mkdir()
            sample_mkv.write_bytes(b"x" * 100)
            main_mkv.write_bytes(b"x" * 5000)
            avi_file.write_bytes(b"x" * 9000)
            aria2_ctrl.write_bytes(b"ctrl")
            empty_mp4.write_bytes(b"")

            found = find_video_files(root)
            self.assertEqual(found, [main_mkv, sample_mkv, avi_file])

    def test_select_subtitle_stream_matches_language_and_prefers_text_over_bitmap(self):
        streams = [
            {"index": 2, "codec_name": "subrip", "tags": {"language": "eng"}},
            {"index": 3, "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "tur"}},
            {"index": 4, "codec_name": "subrip", "tags": {"language": "tur"}},
        ]
        chosen, matched = select_subtitle_stream(streams, "tur")
        self.assertTrue(matched)
        self.assertEqual(chosen["index"], 4)

        # Fallback when requested language is absent
        fallback_stream, matched_fallback = select_subtitle_stream(streams, "jpn")
        self.assertFalse(matched_fallback)
        self.assertEqual(fallback_stream["index"], 2)

    def test_ass_and_ssa_preserve_ass_format(self):
        self.assertEqual(subtitle_format("ass"), (".ass", "ass"))
        self.assertEqual(subtitle_format("SSA"), (".ass", "ass"))

    def test_text_subtitles_use_srt_format(self):
        self.assertEqual(subtitle_format("subrip"), (".srt", "srt"))
        self.assertEqual(subtitle_format("webvtt"), (".srt", "srt"))

    def test_srt_cleaner_preserves_text_and_line_breaks(self):
        source = (
            "1\r\n00:00:01,000 --> 00:00:02,000\r\n"
            "{\\an8}<i>Hello</i><br>world &amp; everyone\r\n"
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "subtitle.srt"
            path.write_text(source, encoding="utf-8")
            clean_srt_file(path)
            cleaned = path.read_text(encoding="utf-8")
        self.assertIn("Hello\nworld & everyone", cleaned)
        self.assertNotIn("{\\an8}", cleaned)
        self.assertIn("00:00:01,000 --> 00:00:02,000", cleaned)

    async def test_execute_subtitle_extraction_end_to_end_and_cleanup(self):
        interaction = FakeInteraction()
        magnet = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Movie"

        with TemporaryDirectory() as tmp:
            temp_root = Path(tmp) / "download_temp"

            def fake_run(args: list[str], timeout: int):
                exe = args[0]
                if exe == "aria2c":
                    out_dir = Path(args[args.index("-d") + 1])
                    video_file = out_dir / "Sample_Movie.mkv"
                    video_file.write_bytes(b"fake-video-content")
                    return subprocess.CompletedProcess(args, 0, stdout="OK", stderr="")
                if exe == "ffprobe":
                    payload = (
                        '{"streams": [{"index": 2, "codec_name": "subrip", '
                        '"tags": {"language": "tur", "title": "Turkish"}}]}'
                    )
                    return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")
                if exe == "ffmpeg":
                    out_sub = Path(args[-1])
                    out_sub.write_text(
                        "1\n00:00:01,000 --> 00:00:03,000\n<i>Merhaba Dünya!</i>\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                raise AssertionError(f"Unexpected command: {args}")

            with (
                patch("subtitle_extract.shutil.which", return_value="/usr/bin/mock"),
                patch("subtitle_extract._run", side_effect=fake_run),
            ):
                await execute_subtitle_extraction(
                    interaction,
                    magnet=magnet,
                    dil="tur",
                    base_temp_dir=temp_root,
                )

            # Verify immediate defer, progress updates, file attachment, and isolated workdir cleanup
            self.assertTrue(interaction.response.deferred)
            self.assertGreaterEqual(len(interaction.progress_updates), 3)
            self.assertEqual(len(interaction.followup.messages), 1)
            sent = interaction.followup.messages[0]
            self.assertIn("Altyazı başarıyla ayıklandı", sent["content"])
            self.assertIsInstance(sent["file"], discord.File)
            self.assertEqual(sent["file"].filename, "Sample_Movie_tur.srt")

            # Verify job workdir inside download_temp was cleaned up
            remaining_jobs = list(temp_root.glob("job_*"))
            self.assertEqual(remaining_jobs, [])

    async def test_execute_subtitle_extraction_handles_invalid_magnet_gracefully(self):
        interaction = FakeInteraction()
        await execute_subtitle_extraction(interaction, magnet="not_a_magnet_link", dil="tur")
        self.assertTrue(interaction.response.deferred)
        self.assertTrue(
            any("❌" in msg for msg in interaction.progress_updates),
            "Expected error message in progress updates",
        )

    def test_runtime_binaries_verification(self):
        statuses = log_runtime_binaries_status(auto_install=False)
        self.assertIn("aria2c", statuses)
        self.assertIn("ffmpeg", statuses)
        self.assertIn("ffprobe", statuses)
        for name in ("aria2c", "ffmpeg", "ffprobe"):
            info = statuses[name]
            self.assertEqual(info["name"], name)
            self.assertIn("installed", info)
            self.assertIn("executable", info)
            self.assertIn("path", info)
            self.assertIn("version", info)

        missing = check_runtime_binary("nonexistent_binary_xyz_999")
        self.assertFalse(missing["installed"])
        self.assertFalse(missing["executable"])
        self.assertIsNone(missing["path"])

        ensured = ensure_runtime_binaries(auto_install=False)
        self.assertEqual(set(ensured.keys()), {"aria2c", "ffmpeg", "ffprobe"})


if __name__ == "__main__":
    unittest.main()
