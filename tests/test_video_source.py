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


if __name__ == "__main__":
    unittest.main()
