"""Choosing stalled Shorts, queueing them spaced out, and reposting one with a fake YouTube."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moment_retrieval import config, made_clips, repost, youtube_upload

NOW = 1_800_000_000.0
DAY = 86400


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class _Status:
    def progress(self):
        return 1.0


class _Request:
    def __init__(self):
        self.steps = [(_Status(), None), (None, {"id": "vidNEW", "status": {"privacyStatus": "private"}})]

    def next_chunk(self, num_retries=0):
        return self.steps.pop(0)


class _Service:
    """videos/channels/playlistItems with just what upload, set_privacy and the title guard call."""

    def __init__(self, newest_titles=(), fail_publish=False):
        self.newest_titles = list(newest_titles)
        self.fail_publish = fail_publish
        self.inserted, self.updated = [], []
        self._kind = ""

    def videos(self):
        self._kind = "videos"
        return self

    def channels(self):
        self._kind = "channels"
        return self

    def playlistItems(self):
        self._kind = "playlist"
        return self

    def insert(self, part, body, media_body):
        self.inserted.append(body)
        return _Request()

    def update(self, part, body):
        self.updated.append((body["id"], body["status"]["privacyStatus"]))
        if self.fail_publish and body["id"] == "vidNEW":
            return _Exec(RuntimeError("quota"))
        return _Exec({"status": {"privacyStatus": body["status"]["privacyStatus"]}})

    def list(self, **kwargs):
        if self._kind == "channels":
            return _Exec({"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
        if self._kind == "playlist":
            return _Exec({"items": [{"snippet": {"title": title}, "contentDetails": {"videoId": "vidOLD"}}
                                    for title in self.newest_titles]})
        return _Exec({"items": [{"status": {"privacyStatus": "public", "uploadStatus": "processed"}}]})


def _video(vid, views, *, age_days=5, privacy="public", seconds=80):
    return {"id": vid, "title": f"見出し{vid}", "published_at": NOW - age_days * DAY, "seconds": seconds,
            "privacy": privacy, "views": views}


class RepostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        patcher = patch.object(config, "CACHE_ROOT", root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.folder = root / "clips" / "src__vid_1"
        self.folder.mkdir(parents=True)
        self.clips = []
        for vid in ("vidOLD", "vidB", "vidBUSY", "vidHIT", "vidNEWISH", "vidPRIVATE"):
            path = self.folder / f"{vid}_short.mp4"
            path.write_bytes(b"mp4")
            path.with_suffix(".metadata.json").write_text(json.dumps({"title": f"見出し{vid}"}), encoding="utf-8")
            receipt = {"video_id": vid, "privacy_status": "public", "title": f"見出し{vid}"}
            self.clips.append(made_clips.MadeClip(path, "auto_1", "", 0.0, "src", f"見出し{vid}", True, receipt))
        self.data = {"videos": [_video("vidOLD", 3), _video("vidB", 12), _video("vidBUSY", 5),
                                _video("vidHIT", 900), _video("vidNEWISH", 2, age_days=1),
                                _video("vidPRIVATE", 1, privacy="private"), _video("vidNOFILE", 1)]}

    def _candidates(self, **kwargs):
        return repost.candidates(data=self.data, clips=self.clips, now=NOW, **kwargs)

    def test_candidates_are_old_public_low_view_clips_with_files(self):
        self.assertEqual({item["video_id"] for item in self._candidates()}, {"vidOLD", "vidB", "vidBUSY"})
        self.assertEqual({item["video_id"] for item in self._candidates(max_views=10)}, {"vidOLD", "vidBUSY"})

    def test_queue_spaces_items_and_skips_queued_ones(self):
        added = repost.enqueue(self._candidates()[:2], interval_hours=3, now=NOW)
        self.assertEqual([item["not_before"] for item in added], [NOW, NOW + 3 * 3600])
        later = repost.enqueue(self._candidates(), interval_hours=3, now=NOW + 60)
        self.assertEqual([item["not_before"] for item in later], [NOW + 6 * 3600])
        self.assertEqual(self._candidates(), [])
        self.assertEqual(repost.cancel_waiting(), 3)
        self.assertEqual(len(self._candidates()), 3)

    def test_due_item_uploads_a_copy_hides_the_old_video_and_publishes(self):
        repost.enqueue([item for item in self._candidates() if item["video_id"] == "vidOLD"], now=NOW)
        service = _Service()
        result = repost.run_due(now=NOW, service_factory=lambda: service)
        self.assertEqual(result["state"], "done")
        self.assertEqual(service.inserted[0]["snippet"]["title"], "見出しvidOLD")
        self.assertEqual(service.updated, [("vidOLD", "private"), ("vidNEW", "public")])
        copy = self.folder / "reposts" / "vidOLD_short.mp4"
        self.assertTrue(youtube_upload.receipt_path(copy).is_file())
        self.assertEqual(repost.load_queue()[0]["new_video_id"], "vidNEW")
        self.assertIsNone(repost.run_due(now=NOW, service_factory=lambda: service))

    def test_only_one_item_per_call_and_not_before_its_time(self):
        repost.enqueue(self._candidates()[:2], interval_hours=3, now=NOW)
        service = _Service()
        repost.run_due(now=NOW, service_factory=lambda: service)
        self.assertIsNone(repost.run_due(now=NOW + 3600, service_factory=lambda: service))
        self.assertEqual(len(service.inserted), 1)

    def test_old_title_still_among_newest_uploads_waits(self):
        repost.enqueue([item for item in self._candidates() if item["video_id"] == "vidBUSY"], now=NOW)
        service = _Service(newest_titles=["見出しvidBUSY"])
        result = repost.run_due(now=NOW, service_factory=lambda: service)
        self.assertEqual(result["state"], "queued")
        self.assertEqual(service.inserted, [])
        self.assertEqual(service.updated, [])
        self.assertEqual(repost.load_queue()[0]["not_before"], NOW + repost.TITLE_BUSY_RETRY_SEC)

    def test_failed_publish_puts_the_old_video_back(self):
        repost.enqueue([item for item in self._candidates() if item["video_id"] == "vidOLD"], now=NOW)
        service = _Service(fail_publish=True)
        result = repost.run_due(now=NOW, service_factory=lambda: service)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(service.updated, [("vidOLD", "private"), ("vidNEW", "public"), ("vidOLD", "public")])

    def test_interrupted_item_goes_back_to_waiting(self):
        repost.enqueue(self._candidates()[:1], now=NOW)
        items = repost.load_queue()
        items[0]["state"] = "running"
        repost._save_queue(items)
        repost.recover_interrupted()
        self.assertEqual(repost.load_queue()[0]["state"], "queued")


if __name__ == "__main__":
    unittest.main()
