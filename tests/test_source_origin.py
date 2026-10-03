"""YouTube origin bookkeeping on a synthetic database (no network, no media)."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from moment_retrieval import db, source_origin


class SourceOriginTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn, create_backup=False)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_only_canonical_youtube_urls_are_recorded(self):
        path = Path(self.tmp.name) / "clip.mp4"
        self.assertEqual(
            source_origin.record_download(self.conn, path, "https://youtu.be/abcDEF12345"),
            "https://www.youtube.com/watch?v=abcDEF12345",
        )
        self.assertIsNone(source_origin.record_download(self.conn, path, "https://example.com/v.mp4"))
        self.assertEqual(
            source_origin.origin_url_for_video(self.conn, {"path": str(path)}),
            "https://www.youtube.com/watch?v=abcDEF12345",
        )

    def test_downloader_filename_is_a_fallback_only_for_its_exact_pattern(self):
        cases = {
            "20260101_120000_" + "abcDEF12345.mp4": "https://www.youtube.com/watch?v=abcDEF12345",
            "20260101_abcDEF12345.mp4": "https://www.youtube.com/watch?v=abcDEF12345",
            "my_holiday_abcDEF12345.mp4": None,
            "recording.mp4": None,
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                video = {"path": str(Path(self.tmp.name) / name)}
                self.assertEqual(source_origin.origin_url_for_video(self.conn, video), expected)

    def test_no_unlinked_match_for_non_youtube_or_unknown_url(self):
        self.assertEqual(source_origin.unlinked_videos_for_origin(self.conn, "https://example.com/x"), [])
        self.assertEqual(
            source_origin.unlinked_videos_for_origin(self.conn, "https://youtu.be/abcDEF12345"), []
        )


if __name__ == "__main__":
    unittest.main()
