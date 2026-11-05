"""Publishing checked clips one at a time, spaced out, with a fake privacy change (no YouTube calls)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moment_retrieval import config, made_clips, publish_queue, youtube_upload

NOW = 1_800_000_000.0
HOUR = 3600


def _clip(root: Path, name: str, receipt=None) -> made_clips.MadeClip:
    path = root / f"{name}.mp4"
    path.write_bytes(b"x")
    return made_clips.MadeClip(path=path, job_id="auto_1", job_source="", created_at=NOW, source_name="src",
                               title=f"title {name}", batch=False, receipt=receipt)


def _uploaded(root, name, privacy="private"):
    return _clip(root, name, {"video_id": f"vid_{name}", "privacy_status": privacy})


class _FakePrivacy:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def __call__(self, path, privacy):
        self.calls.append((Path(path).stem, privacy))
        if Path(path).stem in self.fail:
            raise youtube_upload.UploadError("quota exceeded")
        return {"video_id": f"vid_{Path(path).stem}", "privacy_status": privacy,
                "watch_url": f"https://www.youtube.com/watch?v=vid_{Path(path).stem}"}


class PublishQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        patcher = patch.object(config, "CACHE_ROOT", self.root / "cache")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        # Never reach the real YouTube API from these tests.
        guard = patch.object(made_clips, "change_privacy", side_effect=AssertionError("real change_privacy"))
        guard.start()
        self.addCleanup(guard.stop)
        self.clips = [_uploaded(self.root, name) for name in ("a", "b", "c")]

    def _paths(self, *names):
        return [str(self.root / f"{name}.mp4") for name in names]

    def _times(self):
        return {Path(item["path"]).stem: item["not_before"] for item in publish_queue.load_queue()
                if item["state"] == "queued"}

    def test_enqueue_spaces_items_by_interval(self):
        added, skipped = publish_queue.enqueue(self._paths("a", "b", "c"), interval_hours=3, now=NOW,
                                               clips=self.clips)
        self.assertEqual(skipped, [])
        self.assertEqual([item["not_before"] for item in added], [NOW, NOW + 3 * HOUR, NOW + 6 * HOUR])

    def test_second_enqueue_goes_after_last_pending(self):
        publish_queue.enqueue(self._paths("a", "b"), interval_hours=2, now=NOW, clips=self.clips)
        added, _ = publish_queue.enqueue(self._paths("c"), interval_hours=2, now=NOW + 60, clips=self.clips)
        self.assertEqual(added[0]["not_before"], NOW + 4 * HOUR)

    def test_enqueue_skips_duplicates_unuploaded_and_public(self):
        clips = self.clips + [_clip(self.root, "new"), _uploaded(self.root, "pub", "public")]
        publish_queue.enqueue(self._paths("a"), interval_hours=3, now=NOW, clips=clips)
        added, skipped = publish_queue.enqueue(self._paths("a", "new", "pub", "missing", "b"),
                                               interval_hours=3, now=NOW, clips=clips)
        self.assertEqual([Path(item["path"]).stem for item in added], ["b"])
        reasons = dict(skipped)
        self.assertEqual(reasons["a.mp4"], "すでに公開待ちです")
        self.assertEqual(reasons["title new"], "まだアップロードしていません")
        self.assertEqual(reasons["title pub"], "すでに公開です")
        self.assertIn("missing.mp4", reasons)
        self.assertEqual(len(publish_queue.load_queue()), 2)

    def test_run_due_publishes_one_at_a_time_and_records_done(self):
        publish_queue.enqueue(self._paths("a", "b"), interval_hours=3, now=NOW, clips=self.clips)
        fake, synced = _FakePrivacy(), []
        with patch.object(publish_queue.time, "time", return_value=NOW + 10):
            first = publish_queue.run_due(now=NOW + 10, change_privacy=fake, on_published=synced.append)
            second = publish_queue.run_due(now=NOW + 20, change_privacy=fake)
        self.assertEqual(first["state"], "done")
        self.assertIsNone(second)  # b is not due yet
        self.assertEqual(fake.calls, [("a", "public")])
        self.assertEqual([receipt["video_id"] for receipt in synced], ["vid_a"])
        done = next(item for item in publish_queue.load_queue() if item["state"] == "done")
        self.assertIn("vid_a", done["message"])
        # b keeps at least the full gap after a was actually published.
        self.assertGreaterEqual(self._times()["b"], NOW + 10 + 3 * HOUR)

    def test_failure_is_recorded_and_next_item_still_runs(self):
        publish_queue.enqueue(self._paths("a", "b"), interval_hours=1, now=NOW, clips=self.clips)
        fake = _FakePrivacy(fail={"a"})
        failed = publish_queue.run_due(now=NOW, change_privacy=fake)
        self.assertEqual(failed["state"], "failed")
        item = next(item for item in publish_queue.load_queue() if Path(item["path"]).stem == "a")
        self.assertEqual((item["state"], item["message"]), ("failed", "quota exceeded"))
        done = publish_queue.run_due(now=NOW + HOUR, change_privacy=fake)
        self.assertEqual(done["state"], "done")
        recent = publish_queue.summary(now=NOW + HOUR)["recent"]
        self.assertEqual({entry["state"] for entry in recent}, {"done", "failed"})

    def test_enqueue_after_recent_publish_waits_for_the_gap(self):
        publish_queue.enqueue(self._paths("a"), interval_hours=3, now=NOW, clips=self.clips)
        with patch.object(publish_queue.time, "time", return_value=NOW):
            publish_queue.run_due(now=NOW, change_privacy=_FakePrivacy())
        added, _ = publish_queue.enqueue(self._paths("b"), interval_hours=3, now=NOW + HOUR, clips=self.clips)
        self.assertEqual(added[0]["not_before"], NOW + 3 * HOUR)

    def test_restart_respaces_overdue_items_from_now(self):
        publish_queue.enqueue(self._paths("a", "b", "c"), interval_hours=3, now=NOW, clips=self.clips)
        later = NOW + 24 * HOUR  # the app was closed past every slot
        publish_queue.recover_interrupted(now=later)
        self.assertEqual(self._times(), {"a": later, "b": later + 3 * HOUR, "c": later + 6 * HOUR})
        fake = _FakePrivacy()
        with patch.object(publish_queue.time, "time", return_value=later):
            publish_queue.run_due(now=later, change_privacy=fake)
            self.assertIsNone(publish_queue.run_due(now=later + 1, change_privacy=fake))
        self.assertEqual(fake.calls, [("a", "public")])

    def test_restart_keeps_future_slots(self):
        publish_queue.enqueue(self._paths("a", "b", "c"), interval_hours=3, now=NOW, clips=self.clips)
        publish_queue.recover_interrupted(now=NOW + 4 * HOUR)  # a and b overdue, c still ahead
        self.assertEqual(self._times(), {"a": NOW + 4 * HOUR, "b": NOW + 7 * HOUR, "c": NOW + 10 * HOUR})

    def test_recover_puts_running_item_back_first(self):
        publish_queue.enqueue(self._paths("a", "b"), interval_hours=3, now=NOW, clips=self.clips)
        items = publish_queue.load_queue()
        items[0]["state"] = "running"
        publish_queue._save_queue(items)
        later = NOW + HOUR
        publish_queue.recover_interrupted(now=later)
        self.assertEqual(self._times(), {"a": later, "b": later + 3 * HOUR})

    def test_run_due_waits_while_one_is_running(self):
        publish_queue.enqueue(self._paths("a", "b"), interval_hours=0.001, now=NOW, clips=self.clips)
        items = publish_queue.load_queue()
        items[0]["state"] = "running"
        publish_queue._save_queue(items)
        self.assertIsNone(publish_queue.run_due(now=NOW + HOUR, change_privacy=_FakePrivacy()))

    def test_cancel_waiting(self):
        publish_queue.enqueue(self._paths("a", "b", "c"), interval_hours=3, now=NOW, clips=self.clips)
        publish_queue.run_due(now=NOW, change_privacy=_FakePrivacy())
        self.assertEqual(publish_queue.cancel_waiting(), 2)
        states = sorted(item["state"] for item in publish_queue.load_queue())
        self.assertEqual(states, ["cancelled", "cancelled", "done"])
        self.assertIsNone(publish_queue.run_due(now=NOW + 99 * HOUR, change_privacy=_FakePrivacy()))
        self.assertEqual(publish_queue.summary()["remaining"], 0)
        # A cancelled clip can be queued again.
        added, _ = publish_queue.enqueue(self._paths("b"), interval_hours=3, now=NOW + 99 * HOUR, clips=self.clips)
        self.assertEqual(len(added), 1)


if __name__ == "__main__":
    unittest.main()
