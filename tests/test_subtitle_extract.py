"""Regression tests for subtitle format selection and SRT cleanup."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from subtitle_extract import MAX_INPUT, clean_srt_file, subtitle_format


class SubtitleFormatTests(unittest.TestCase):
    def test_upload_limit_is_1_5_gb_decimal(self):
        self.assertEqual(MAX_INPUT, 1_500_000_000)

    def test_ass_and_ssa_preserve_ass_format(self):
        self.assertEqual(subtitle_format("ass"), (".ass", "ass"))
        self.assertEqual(subtitle_format("SSA"), (".ass", "ass"))

    def test_text_subtitles_use_srt_format(self):
        self.assertEqual(subtitle_format("subrip"), (".srt", "srt"))
        self.assertEqual(subtitle_format("webvtt"), (".srt", "srt"))

    def test_srt_cleaner_preserves_text_and_line_breaks(self):
        source = (
            "1\r\n00:00:01,000 --> 00:00:02,000\r\n"
            "<i>Hello</i><br>world &amp; everyone\r\n"
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "subtitle.srt"
            path.write_text(source, encoding="utf-8")
            clean_srt_file(path)
            cleaned = path.read_text(encoding="utf-8")
        self.assertIn("Hello\nworld & everyone", cleaned)
        self.assertIn("00:00:01,000 --> 00:00:02,000", cleaned)


if __name__ == "__main__":
    unittest.main()
