"""Private-only YouTube upload with a fake API client (no network, no OAuth)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moment_retrieval import config, youtube_upload


class _Status:
    def __init__(self, value):
        self.value = value

    def progress(self):
        return self.value


class _Request:
    def __init__(self, body):
        self.body = body
        self.steps = [(_Status(0.5), None), (None, {"id": "abcDEF12345", "status": {"privacyStatus": "private"}})]

    def next_chunk(self):
        return self.steps.pop(0)


class _Service:
    def __init__(self):
        self.inserted = []

    def videos(self):
        return self

    def insert(self, part, body, media_body):
        self.inserted.append(body)
        return _Request(body)

    def update(self, part, body):
        self.updated = body
        privacy = getattr(self, "result_privacy", "public")
        return type("_Exec", (), {"execute": lambda _self: {"status": {"privacyStatus": privacy}}})()


class PublishTest(unittest.TestCase):
    def test_publish_sets_public_and_reports_locked_private(self):
        service = _Service()
        result = youtube_upload.publish("abcDEF12345", service_factory=lambda: service)
        self.assertEqual(service.updated["status"]["privacyStatus"], "public")
        self.assertIn("watch?v=abcDEF12345", result["watch_url"])
        service.result_privacy = "private"
        with self.assertRaises(youtube_upload.UploadError):
            youtube_upload.publish("abcDEF12345", service_factory=lambda: service)


class YouTubeUploadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.video = self.root / "clip_見どころ.mp4"
        self.video.write_bytes(b"synthetic")

    def tearDown(self):
        self.tmp.cleanup()

    def test_sidecar_metadata_is_used_and_privacy_is_always_private(self):
        self.video.with_suffix(".metadata.json").write_text(json.dumps({
            "title": "<b>決算</b>の瞬間" + "あ" * 200, "description": "説明", "tags": ["株", "決算", ""],
        }, ensure_ascii=False), encoding="utf-8")
        body = youtube_upload.metadata_for(self.video).request_body()
        self.assertEqual(body["status"]["privacyStatus"], "private")
        self.assertNotIn("<", body["snippet"]["title"])
        self.assertLessEqual(len(body["snippet"]["title"]), youtube_upload.MAX_TITLE)
        self.assertEqual(body["snippet"]["tags"], ["株", "決算"])

    def test_filename_is_the_fallback_title(self):
        self.assertEqual(youtube_upload.metadata_for(self.video).title, "clip_見どころ")

    def test_upload_writes_receipt_and_never_uploads_twice(self):
        service = _Service()
        messages = list(youtube_upload.upload_private(self.video, service_factory=lambda: service))
        receipt = messages[-1][1]
        self.assertEqual(receipt["video_id"], "abcDEF12345")
        self.assertIn("studio.youtube.com/video/abcDEF12345", receipt["studio_url"])
        self.assertTrue(any("50%" in message for message, _ in messages))
        again = list(youtube_upload.upload_private(self.video, service_factory=lambda: service))
        self.assertEqual(len(service.inserted), 1)
        self.assertIn("省略", again[0][0])

    def test_non_mp4_is_rejected(self):
        other = self.root / "notes.txt"
        other.write_text("x", encoding="utf-8")
        with self.assertRaises(youtube_upload.UploadError):
            list(youtube_upload.upload_private(other, service_factory=_Service))

    def test_setup_reports_missing_client_secret_location(self):
        with patch.object(config, "LIBRARY_ROOT", self.root):
            problems = youtube_upload.setup_problems()
        self.assertTrue(any("youtube_client_secret.json" in item for item in problems))




class SavedPathMappingTest(unittest.TestCase):
    def test_cached_gradio_copy_maps_back_to_the_saved_clip(self):
        import app

        with tempfile.TemporaryDirectory() as out, tempfile.TemporaryDirectory() as cache:
            original = Path(out) / "video__vid_x" / "clip.mp4"
            original.parent.mkdir()
            original.write_bytes(b"synthetic clip")
            cached = Path(cache) / "clip.mp4"
            cached.write_bytes(b"synthetic clip")
            self.assertEqual(app._saved_highlight_paths([str(cached)], out), [original.resolve()])
            self.assertEqual(app._saved_highlight_paths([str(original)], out), [original.resolve()])


if __name__ == "__main__":
    unittest.main()
