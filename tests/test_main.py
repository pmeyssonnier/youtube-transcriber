from __future__ import annotations

import unittest

from app.main import find_active_duplicate, job_summary
from app.youtube import validate_cookie_browser, validate_youtube_url, youtube_video_identity


class UrlValidationTests(unittest.TestCase):
    def test_accepts_supported_single_video_urls(self) -> None:
        expected = "euuLzgjj7z4"
        for value in (
            "https://www.youtube.com/watch?v=euuLzgjj7z4&si=abc",
            "https://www.youtube.com/live/euuLzgjj7z4?si=abc",
            "https://youtu.be/euuLzgjj7z4?t=30",
            "https://www.youtube.com/embed/euuLzgjj7z4",
            "https://www.youtube.com/shorts/euuLzgjj7z4",
        ):
            with self.subTest(value=value):
                self.assertEqual(youtube_video_identity(value), expected)
                self.assertEqual(validate_youtube_url(value), value)

    def test_rejects_non_video_playlist_channel_and_deceptive_hosts(self) -> None:
        for value in (
            "https://example.com/video",
            "https://youtube.com.example.com/watch?v=euuLzgjj7z4",
            "file:///etc/passwd",
            "https://www.youtube.com/playlist?list=PL123",
            "https://www.youtube.com/@openai",
            "https://www.youtube.com/results?search_query=test",
            "https://www.youtube.com/watch?v=too-short",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_youtube_url(value)

    def test_cookie_browser_is_allowlisted(self) -> None:
        self.assertEqual(validate_cookie_browser(" Edge "), "edge")
        self.assertIsNone(validate_cookie_browser(""))
        with self.assertRaises(ValueError):
            validate_cookie_browser("custom;command")

    def test_detects_active_duplicate_including_legacy_job(self) -> None:
        jobs = [
            {
                "id": "active",
                "url": "https://www.youtube.com/live/euuLzgjj7z4?si=abc",
                "status": "transcribing",
            },
            {
                "id": "finished",
                "url": "https://youtu.be/dQw4w9WgXcQ",
                "status": "completed",
            },
        ]
        duplicate = find_active_duplicate(jobs, "https://www.youtube.com/watch?v=euuLzgjj7z4")
        self.assertIsNotNone(duplicate)
        self.assertEqual(duplicate["id"], "active")
        self.assertIsNone(find_active_duplicate(jobs, "https://youtu.be/dQw4w9WgXcQ"))

    def test_job_summary_does_not_include_segments(self) -> None:
        summary = job_summary({"id": "a" * 32, "status": "completed", "segments": [{"text": "secret"}]})
        self.assertNotIn("segments", summary)


if __name__ == "__main__":
    unittest.main()
