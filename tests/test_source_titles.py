import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moment_retrieval import config, source_titles


class SourceKeyTest(unittest.TestCase):
    def test_keys_come_from_urls_and_downloaded_file_names(self):
        cases = {
            "20261026_232323_v2911322799.audio.mp4": "twitch:2911322799",
            "v2805700980.mp4": "twitch:2805700980",
            "https://www.twitch.tv/videos/2890755256": "twitch:2890755256",
            "20260731_230004_hHfj-fuinfQ.mp4": "youtube:hHfj-fuinfQ",
            "PEs1OC0Kis8.mp4": "youtube:PEs1OC0Kis8",
            "https://www.youtube.com/watch?v=tUOMWH1TPPA": "youtube:tUOMWH1TPPA",
            "my_recording.mp4": None,
        }
        for text, expected in cases.items():
            self.assertEqual(source_titles.source_key(text), expected, text)


class SourceTitleStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(config, "LIBRARY_ROOT", Path(self.tmp.name))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_record_and_fetch_only_missing_titles(self):
        source_titles.record("https://www.twitch.tv/videos/111111", "  既知の  配信 ", "20261001")
        asked = []

        def extract(url):
            asked.append(url)
            if url.endswith("222222"):
                raise RuntimeError("VOD deleted")
            return {"title": "取り直した配信", "upload_date": "20261002"}

        found = source_titles.fetch_missing(
            ["20261001_000000_v111111.mp4", "20261002_000000_v222222.mp4", "20261003_000000_v333333.mp4"],
            extract=extract,
        )
        self.assertEqual(found, 1)
        self.assertEqual(len(asked), 2)
        data = source_titles.load()
        self.assertEqual(data["twitch:111111"]["title"], "既知の 配信")
        self.assertEqual(data["twitch:333333"], {"title": "取り直した配信", "upload_date": "20261002"})
        self.assertEqual(data["twitch:222222"]["title"], "")
        # The deleted VOD is not asked about again on the next start.
        asked.clear()
        source_titles.fetch_missing(["20261002_000000_v222222.mp4"], extract=extract)
        self.assertEqual(asked, [])


class LibraryChoiceLabelTest(unittest.TestCase):
    def test_newest_first_with_date_title_or_hints(self):
        import auto_publish_app

        videos = [
            {"video_id": "vid_a", "name": "20261001_000000_v111111.mp4", "duration_sec": 3600, "source_available": True},
            {"video_id": "vid_b", "name": "20261020_000000_v222222.audio.mp4", "duration_sec": 60, "source_available": True},
            {"video_id": "vid_c", "name": "v333333.mp4", "duration_sec": 90, "source_available": False},
        ]
        titles = {"twitch:111111": {"title": "雑談するぞ", "upload_date": "20261001"}}
        hints = {"vid_b": ["子犬が洗えない", "院試を諦める？"]}
        with patch.object(auto_publish_app, "library_videos", return_value=videos), \
                patch.object(source_titles, "load", return_value=titles), \
                patch.object(auto_publish_app, "_content_hints", return_value=hints):
            choices = auto_publish_app.library_choices()
        self.assertEqual([value for _label, value in choices], ["vid_b", "vid_a", "vid_c"])
        self.assertIn("10/20｜子犬が洗えない / 院試を諦める？", choices[0][0])
        self.assertIn("10/1｜雑談するぞ　1:00:00", choices[1][0])
        self.assertTrue(choices[2][0].startswith("日付不明｜v333333"))
        self.assertIn("共有", choices[2][0])
