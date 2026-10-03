import json
import contextlib
import io
import sys
import types
import unittest
import urllib.request
from unittest.mock import patch

from moment_retrieval.youtube_captions import (
    CaptionError, fetch_youtube_captions, normalize_youtube_url, _parse_cues, _fetch_bytes, _NoRedirect,
)


class _YDL:
    info = {}
    options = None
    def __init__(self, options): type(self).options = options
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def extract_info(self, url, download=False):
        if download: raise AssertionError("caption inspection must not download")
        return dict(type(self).info)


def _payload(*events):
    return json.dumps({"events": list(events)}).encode()


class CaptionTests(unittest.TestCase):
    def setUp(self):
        self.module = types.ModuleType("yt_dlp")
        self.module.YoutubeDL = _YDL
        self.video_id = "syn00000001"
        self.url = f"https://www.youtube.com/api/timedtext?v={self.video_id}&lang=ja"
        _YDL.info = {"id": self.video_id, "availability": "public", "title": "<b>unsafe</b>", "subtitles": {"ja": [{"ext": "json3", "url": self.url}]}}

    def test_normalizes_only_single_public_youtube_videos(self):
        self.assertEqual(normalize_youtube_url(f"https://youtu.be/{self.video_id}?si=x"), f"https://www.youtube.com/watch?v={self.video_id}")
        self.assertEqual(normalize_youtube_url(f"https://youtube.com/shorts/{self.video_id}"), f"https://www.youtube.com/watch?v={self.video_id}")
        for value in ("http://youtube.com/watch?v=syn00000001", "https://user@youtube.com/watch?v=syn00000001", "https://youtube.com:444/watch?v=syn00000001", "https://evil.invalid/watch?v=syn00000001", "https://youtube.com/watch?v=syn00000001&list=x", "https://youtube.com/watch?v=short"):
            with self.assertRaises(CaptionError): normalize_youtube_url(value)

    def test_fetch_uses_metadata_only_manual_then_auto_then_fallback(self):
        body = _payload({"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "A &amp; <b>B</b>"}]})
        with patch.dict(sys.modules, {"yt_dlp": self.module}), patch("moment_retrieval.youtube_captions._fetch_bytes", return_value=body):
            preview = fetch_youtube_captions(f"https://youtu.be/{self.video_id}")
        self.assertFalse(preview.is_automatic)
        self.assertEqual(preview.cues[0].text, "A & B")
        self.assertTrue(_YDL.options["skip_download"])
        self.assertTrue(_YDL.options["noplaylist"])
        self.assertFalse(_YDL.options["cachedir"])
        self.assertEqual(_YDL.options["retries"], 0)
        self.assertEqual(_YDL.options["extractor_retries"], 0)
        self.assertNotIn("cookiefile", _YDL.options)
        self.assertNotIn("writesubtitles", _YDL.options)
        _YDL.info = {"id": self.video_id, "availability": "public", "automatic_captions": {"en": [{"ext": "json3", "url": self.url}]}}
        with patch.dict(sys.modules, {"yt_dlp": self.module}), patch("moment_retrieval.youtube_captions._fetch_bytes", return_value=body):
            preview = fetch_youtube_captions(f"https://youtu.be/{self.video_id}", "ja")
        self.assertEqual((preview.language, preview.is_automatic), ("en", True))
        translated = self.url + "&tlang=ja"
        _YDL.info = {"id": self.video_id, "availability": "public", "subtitles": {"en": [{"ext": "json3", "url": self.url}]}, "automatic_captions": {"ja": [{"ext": "json3", "url": translated}]}}
        with patch.dict(sys.modules, {"yt_dlp": self.module}), patch("moment_retrieval.youtube_captions._fetch_bytes", return_value=body):
            preview = fetch_youtube_captions(f"https://youtu.be/{self.video_id}", "ja")
        self.assertEqual((preview.language, preview.is_automatic), ("en", False))

    def test_rejects_absent_bad_or_unsafe_caption_data(self):
        _YDL.info = {"id": self.video_id, "availability": "public"}
        with patch.dict(sys.modules, {"yt_dlp": self.module}):
            with self.assertRaisesRegex(CaptionError, "CAPTIONS_UNAVAILABLE"):
                fetch_youtube_captions(f"https://youtu.be/{self.video_id}")
        _YDL.info = {"id": self.video_id, "availability": "public", "subtitles": {"ja": [{"ext": "json3", "url": self.url}]}}
        for body, code in [
            (_payload({"tStartMs": -1, "dDurationMs": 1, "segs": [{"utf8": "x"}]}), "INVALID_TIMING"),
            (b"not json", "INVALID_CAPTIONS"),
        ]:
            with patch.dict(sys.modules, {"yt_dlp": self.module}), patch("moment_retrieval.youtube_captions._fetch_bytes", return_value=body):
                with self.assertRaisesRegex(CaptionError, code): fetch_youtube_captions(f"https://youtu.be/{self.video_id}")
        _YDL.info = {"id": "wrong000000"[:11], "availability": "public"}
        with patch.dict(sys.modules, {"yt_dlp": self.module}):
            with self.assertRaisesRegex(CaptionError, "VIDEO_MISMATCH"): fetch_youtube_captions(f"https://youtu.be/{self.video_id}")
        _YDL.info = {"id": self.video_id, "availability": "private"}
        with patch.dict(sys.modules, {"yt_dlp": self.module}):
            with self.assertRaisesRegex(CaptionError, "VIDEO_UNAVAILABLE"):
                fetch_youtube_captions(f"https://youtu.be/{self.video_id}")

    def test_parser_rejects_oversize_and_drops_only_exact_duplicates(self):
        duplicate = {"tStartMs": 0, "dDurationMs": 1, "segs": [{"utf8": "same"}]}
        cues = _parse_cues(_payload(duplicate, duplicate, {"tStartMs": 1, "dDurationMs": 1, "segs": [{"utf8": "same"}]}))
        self.assertEqual([(cue.start_ms, cue.text) for cue in cues], [(0, "same"), (1, "same")])
        with self.assertRaisesRegex(CaptionError, "CAPTION_CUE_TOO_LARGE"):
            _parse_cues(_payload({"tStartMs": 0, "dDurationMs": 1, "segs": [{"utf8": "x" * 8_001}]}))

    def test_parser_rejects_non_monotonic_and_giant_timing(self):
        for events in (
            ({"tStartMs": 10, "dDurationMs": 1, "segs": [{"utf8": "a"}]}, {"tStartMs": 9, "dDurationMs": 1, "segs": [{"utf8": "b"}]}),
            ({"tStartMs": 9e99, "dDurationMs": 1, "segs": [{"utf8": "a"}]},),
            ({"tStartMs": 10 ** 400, "dDurationMs": 1, "segs": [{"utf8": "a"}]},),
            ({"tStartMs": float("nan"), "dDurationMs": 1, "segs": [{"utf8": "a"}]},),
            ({"tStartMs": 0, "dDurationMs": float("inf"), "segs": [{"utf8": "a"}]},),
        ):
            with self.assertRaisesRegex(CaptionError, "INVALID_TIMING"):
                _parse_cues(_payload(*events))

    def test_metadata_errors_and_non_public_states_are_safe(self):
        class BrokenYDL(_YDL):
            def extract_info(self, url, download=False): raise RuntimeError("signed URL should never leak")
        broken = types.ModuleType("yt_dlp")
        broken.YoutubeDL = BrokenYDL
        with patch.dict(sys.modules, {"yt_dlp": broken}):
            with self.assertRaisesRegex(CaptionError, "METADATA_FETCH_FAILED") as raised:
                fetch_youtube_captions(f"https://youtu.be/{self.video_id}")
        self.assertNotIn("signed URL", str(raised.exception))
        for state, live in (("", False), ("unlisted", False), ("private", False), ("public", True)):
            _YDL.info = {"id": self.video_id, "availability": state, "is_live": live}
            with patch.dict(sys.modules, {"yt_dlp": self.module}):
                with self.assertRaisesRegex(CaptionError, "VIDEO_UNAVAILABLE"):
                    fetch_youtube_captions(f"https://youtu.be/{self.video_id}")
        _YDL.info = {"id": self.video_id, "availability": "public", "title": "x" * 501, "subtitles": {"ja": [{"ext": "json3", "url": self.url}]}}
        with patch.dict(sys.modules, {"yt_dlp": self.module}), patch("moment_retrieval.youtube_captions._fetch_bytes", return_value=_payload({"tStartMs": 0, "dDurationMs": 1, "segs": [{"utf8": "x"}]})):
            with self.assertRaisesRegex(CaptionError, "TITLE_TOO_LARGE"):
                fetch_youtube_captions(f"https://youtu.be/{self.video_id}")

    def test_invalid_caption_track_falls_back_without_stdout(self):
        bad_port = f"https://www.youtube.com:bad/api/timedtext?v={self.video_id}"
        _YDL.info = {"id": self.video_id, "availability": "public", "automatic_captions": {"ja": [{"ext": "json3", "url": bad_port}], "en": [{"ext": "json3", "url": self.url}]}}
        output = io.StringIO()
        body = _payload({"tStartMs": 0, "dDurationMs": 1, "segs": [{"utf8": "x"}]})
        with contextlib.redirect_stdout(output), patch.dict(sys.modules, {"yt_dlp": self.module}), patch("moment_retrieval.youtube_captions._fetch_bytes", return_value=body):
            preview = fetch_youtube_captions(f"https://youtu.be/{self.video_id}")
        self.assertEqual((preview.language, preview.is_automatic), ("en", True))
        self.assertEqual(output.getvalue(), "")

    def test_transport_errors_are_safe_and_bounded(self):
        class Response:
            headers = {"Content-Length": str(4 * 1024 * 1024 + 1)}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, _size): raise AssertionError("must reject size before reading")
        class Opener:
            def open(self, _request, timeout): return Response()
        with patch("moment_retrieval.youtube_captions.urllib.request.build_opener", return_value=Opener()):
            with self.assertRaisesRegex(CaptionError, "CAPTIONS_TOO_LARGE"):
                _fetch_bytes(self.url)
        class FailingOpener:
            def open(self, _request, timeout): raise OSError("redirect to secret signed URL")
        with patch("moment_retrieval.youtube_captions.urllib.request.build_opener", return_value=FailingOpener()):
            with self.assertRaisesRegex(CaptionError, "CAPTION_FETCH_FAILED") as raised:
                _fetch_bytes(self.url)
        self.assertNotIn("secret", str(raised.exception))
        request = urllib.request.Request(self.url)
        with self.assertRaisesRegex(Exception, "redirect blocked"):
            _NoRedirect().redirect_request(request, None, 302, "Found", {}, "https://evil.invalid/")


if __name__ == "__main__":
    unittest.main()
