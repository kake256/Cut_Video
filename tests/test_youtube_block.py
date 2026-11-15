import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from moment_retrieval import config, youtube_block
from moment_retrieval.downloader import DownloadError, download_sections, download_video
from moment_retrieval.youtube_block import YouTubeBlocked

BOT_ERROR = ("ERROR: [youtube] abcdefghijk: Sign in to confirm you\u2019re not a bot. "
             "Use --cookies-from-browser or --cookies for the authentication.")
YOUTUBE_URL = "https://www.youtube.com/watch?v=abcdefghijk"
TWITCH_URL = "https://www.twitch.tv/videos/2919381021"


class _FakeYoutubeDL:
    error = None
    calls = 0

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def extract_info(self, url, download=False, process=True):
        type(self).calls += 1
        if type(self).error is not None:
            raise Exception(type(self).error)
        if download:
            Path(self.options["outtmpl"].replace("%(ext)s", "mp4")).write_bytes(b"synthetic")
        return {"id": "abcdefghijk", "title": "t", "upload_date": "20261101", "ext": "mp4"}

    def download(self, urls):
        type(self).calls += 1
        if type(self).error is not None:
            raise Exception(type(self).error)


class YouTubeBlockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = patch.object(config, "LIBRARY_ROOT", Path(self.tmp.name))
        root.start()
        self.addCleanup(root.stop)
        _FakeYoutubeDL.error = None
        _FakeYoutubeDL.calls = 0
        fake = types.ModuleType("yt_dlp")
        fake.YoutubeDL = _FakeYoutubeDL
        utils = types.ModuleType("yt_dlp.utils")
        utils.download_range_func = lambda *_args: None
        modules = patch.dict(sys.modules, {"yt_dlp": fake, "yt_dlp.utils": utils})
        modules.start()
        self.addCleanup(modules.stop)
        self.save_dir = Path(self.tmp.name) / "video"
        gap = patch.object(youtube_block, "MIN_GAP_SEC", 0)
        gap.start()
        self.addCleanup(gap.stop)
        youtube_block.forget_info()
        self.addCleanup(youtube_block.forget_info)

    def test_retry_sleep_functions_accept_yt_dlp_keyword(self):
        # yt-dlp's RetryManager calls ``sleep_func(n=attempts)``; a different name crashed downloads on retry.
        for sleep_func in youtube_block.polite_options(YOUTUBE_URL)["retry_sleep_functions"].values():
            self.assertEqual(5.0, sleep_func(n=0))
            self.assertEqual(120.0, sleep_func(n=9))

    def test_recognises_youtube_hosts_only(self):
        self.assertTrue(youtube_block.is_youtube(YOUTUBE_URL))
        self.assertTrue(youtube_block.is_youtube("https://youtu.be/abcdefghijk"))
        self.assertTrue(youtube_block.is_youtube("https://m.youtube.com/watch?v=abcdefghijk"))
        self.assertFalse(youtube_block.is_youtube(TWITCH_URL))
        self.assertFalse(youtube_block.is_youtube("https://notyoutube.com/watch?v=x"))
        self.assertFalse(youtube_block.is_youtube(r"F:\video\20261101_abcdefghijk.mp4"))

    def test_bot_check_on_metadata_starts_cooldown_and_next_call_sends_nothing(self):
        _FakeYoutubeDL.error = BOT_ERROR
        with self.assertRaises(YouTubeBlocked) as first:
            list(download_video(YOUTUBE_URL, self.save_dir))
        self.assertEqual(_FakeYoutubeDL.calls, 1)
        self.assertIn(youtube_block.BOT_MARKER, str(first.exception))  # the daily batch looks for this
        self.assertIsInstance(first.exception, DownloadError)  # existing handlers still show it
        self.assertGreater(youtube_block.blocked_until(), time.time())

        _FakeYoutubeDL.error = None
        with self.assertRaises(YouTubeBlocked):
            list(download_video(YOUTUBE_URL, self.save_dir, audio_only=True))
        with self.assertRaises(YouTubeBlocked):
            download_sections(YOUTUBE_URL, [(0, 10), (20, 30)], self.save_dir)
        self.assertEqual(_FakeYoutubeDL.calls, 1)  # nothing more was sent to YouTube

    def test_other_sites_are_not_blocked(self):
        youtube_block.record(BOT_ERROR)
        messages = list(download_video(TWITCH_URL, self.save_dir))
        self.assertIsNotNone(messages[-1][1])

    def test_other_failures_do_not_start_a_cooldown(self):
        _FakeYoutubeDL.error = "HTTP Error 503: Service Unavailable"
        with self.assertRaises(DownloadError) as caught:
            list(download_video(YOUTUBE_URL, self.save_dir))
        self.assertNotIsInstance(caught.exception, YouTubeBlocked)
        self.assertEqual(youtube_block.blocked_until(), 0.0)

    def test_section_bot_check_starts_cooldown(self):
        _FakeYoutubeDL.error = BOT_ERROR
        with self.assertRaises(YouTubeBlocked):
            download_sections(YOUTUBE_URL, [(0, 10)], self.save_dir, workers=1)
        self.assertGreater(youtube_block.blocked_until(), 0)

    def test_expired_cooldown_allows_one_probe_and_success_clears_it(self):
        youtube_block.record(BOT_ERROR, now=time.time() - youtube_block.COOLDOWN_SEC - 1)
        self.assertEqual(youtube_block.blocked_until(), 0.0)
        list(download_video(YOUTUBE_URL, self.save_dir))
        self.assertFalse(youtube_block.store_path().exists())

    def test_expired_cooldown_and_new_bot_check_starts_a_new_one(self):
        youtube_block.record(BOT_ERROR, now=time.time() - youtube_block.COOLDOWN_SEC - 1)
        _FakeYoutubeDL.error = BOT_ERROR
        with self.assertRaises(YouTubeBlocked):
            list(download_video(YOUTUBE_URL, self.save_dir))
        self.assertGreater(youtube_block.blocked_until(), time.time())

    def test_title_lookup_skips_youtube_during_cooldown_without_marking_it_missing(self):
        from moment_retrieval import source_titles

        youtube_block.record(BOT_ERROR)
        asked = []
        found = source_titles.fetch_missing(["20261101_abcdefghijk.mp4"], extract=lambda url: asked.append(url))
        self.assertEqual((found, asked), (0, []))
        self.assertNotIn("youtube:abcdefghijk", source_titles.load())

    def test_title_lookup_bot_check_is_not_recorded_as_a_missing_video(self):
        from moment_retrieval import source_titles

        def refuse(_url):
            raise Exception(BOT_ERROR)

        source_titles.fetch_missing(["20261101_abcdefghijk.mp4"], extract=refuse)
        self.assertNotIn("youtube:abcdefghijk", source_titles.load())
        self.assertGreater(youtube_block.blocked_until(), 0)


    # ---- keeping YouTube traffic small ----

    def test_video_id_from_common_url_shapes(self):
        for url in (YOUTUBE_URL, "https://youtu.be/abcdefghijk?t=5", "https://www.youtube.com/live/abcdefghijk",
                    "https://www.youtube.com/shorts/abcdefghijk"):
            self.assertEqual(youtube_block.video_id(url), "abcdefghijk", url)
        self.assertEqual(youtube_block.video_id(TWITCH_URL), "")

    def test_downloaded_video_is_found_by_id_without_asking_youtube(self):
        self.save_dir.mkdir(parents=True)
        existing = self.save_dir / "sample_abcdefghijk.mp4"
        existing.write_bytes(b"x")
        messages = list(download_video(YOUTUBE_URL, self.save_dir))
        self.assertEqual(messages[-1][1], existing)
        self.assertEqual(_FakeYoutubeDL.calls, 0)

    def test_audio_download_reuses_info_fetched_by_an_earlier_step(self):
        youtube_block.remember_info(YOUTUBE_URL, {"id": "abcdefghijk", "title": "t", "upload_date": "20261101",
                                                  "formats": [{"format_id": "140", "vcodec": "none"}]})
        list(download_video(YOUTUBE_URL, self.save_dir))
        self.assertEqual(_FakeYoutubeDL.calls, 1)  # only the download itself

    def test_youtube_sections_are_fetched_one_at_a_time_with_polite_options(self):
        seen = []

        class Recording(_FakeYoutubeDL):
            active = 0

            def download(self, urls):
                type(self).active += 1
                seen.append((type(self).active, self.options))
                Path(self.options["outtmpl"].replace("%(ext)s", "mp4")).write_bytes(b"x")
                time.sleep(0.02)
                type(self).active -= 1

        sys.modules["yt_dlp"].YoutubeDL = Recording
        download_sections(YOUTUBE_URL, [(0, 10), (20, 30), (40, 50)], self.save_dir, workers=4)
        self.assertEqual(max(active for active, _ in seen), 1)
        self.assertEqual(seen[0][1]["concurrent_fragment_downloads"], 1)
        self.assertLessEqual(seen[0][1]["retries"], 5)

    def test_polite_options_apply_to_youtube_only(self):
        self.assertEqual(youtube_block.polite_options(TWITCH_URL), {})
        options = youtube_block.polite_options(YOUTUBE_URL)
        self.assertEqual(options["concurrent_fragment_downloads"], 1)
        self.assertGreaterEqual(options["retry_sleep_functions"]["http"](0), 5)

    def test_pace_spaces_out_youtube_requests(self):
        with patch.object(youtube_block, "MIN_GAP_SEC", 0.2):
            started = time.monotonic()
            youtube_block.pace(YOUTUBE_URL)
            youtube_block.pace(YOUTUBE_URL)
            youtube_block.pace(TWITCH_URL)  # other sites are not slowed down
            self.assertGreaterEqual(time.monotonic() - started, 0.19)
            self.assertLess(time.monotonic() - started, 0.5)


if __name__ == "__main__":
    unittest.main()
