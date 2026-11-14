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

    def test_latest_by_path_prefers_waiting_over_an_earlier_failure(self):
        publish_queue.enqueue(self._paths("a"), interval_hours=1, now=NOW, clips=self.clips)
        publish_queue.run_due(now=NOW, change_privacy=_FakePrivacy(fail={"a"}))
        key = publish_queue._key(self._paths("a")[0])
        self.assertEqual(publish_queue.latest_by_path()[key]["state"], "failed")
        publish_queue.enqueue(self._paths("a"), interval_hours=1, now=NOW + 60, clips=self.clips)
        self.assertEqual(publish_queue.latest_by_path()[key]["state"], "queued")

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
        self.assertEqual(publish_queue.cancel_waiting(), (2, []))
        states = sorted(item["state"] for item in publish_queue.load_queue())
        self.assertEqual(states, ["cancelled", "cancelled", "done"])
        self.assertIsNone(publish_queue.run_due(now=NOW + 99 * HOUR, change_privacy=_FakePrivacy()))
        self.assertEqual(publish_queue.summary()["remaining"], 0)
        # A cancelled clip can be queued again.
        added, _ = publish_queue.enqueue(self._paths("b"), interval_hours=3, now=NOW + 99 * HOUR, clips=self.clips)
        self.assertEqual(len(added), 1)

    def _schedule_fake(self, fail=()):
        calls = []

        def schedule(path, publish_at):
            calls.append((Path(path).stem, publish_at))
            if Path(path).stem in fail:
                raise youtube_upload.UploadError("一度公開した動画は予約できません")
            return {"video_id": f"vid_{Path(path).stem}", "privacy_status": "private", "publish_at": "2027-01-01T00:00:00Z"}
        return schedule, calls

    def test_schedule_waiting_registers_all_but_the_one_due_now(self):
        publish_queue.enqueue(self._paths("a", "b", "c"), interval_hours=1, now=NOW, clips=self.clips)
        schedule, calls = self._schedule_fake()
        done, kept = publish_queue.schedule_waiting(now=NOW, schedule=schedule)
        self.assertEqual(calls, [("b", NOW + HOUR), ("c", NOW + 2 * HOUR)])
        self.assertEqual((len(done), kept), (2, []))
        states = {Path(item["path"]).stem: item["state"] for item in publish_queue.load_queue()}
        self.assertEqual(states, {"a": "queued", "b": "scheduled", "c": "scheduled"})
        # The app only publishes the queued one; scheduled ones are YouTube's job.
        fake = _FakePrivacy()
        publish_queue.run_due(now=NOW, change_privacy=fake)
        self.assertIsNone(publish_queue.run_due(now=NOW + 9 * HOUR, change_privacy=fake))
        self.assertEqual(fake.calls, [("a", "public")])
        # Scheduled clips stay out of the checkboxes and count as waiting.
        self.assertEqual(publish_queue.summary(now=NOW)["remaining"], 2)
        self.assertEqual(len(publish_queue.pending_by_path()), 2)

    def test_schedule_waiting_only_touches_given_paths(self):
        publish_queue.enqueue(self._paths("a", "b", "c"), interval_hours=1, now=NOW, clips=self.clips)
        schedule, calls = self._schedule_fake()
        publish_queue.schedule_waiting(now=NOW, paths=self._paths("c"), schedule=schedule)
        self.assertEqual([stem for stem, _ in calls], ["c"])

    def test_schedule_refused_stays_queued_with_reason(self):
        publish_queue.enqueue(self._paths("a", "b"), interval_hours=1, now=NOW, clips=self.clips)
        schedule, _calls = self._schedule_fake(fail={"b"})
        done, kept = publish_queue.schedule_waiting(now=NOW, schedule=schedule)
        self.assertEqual((done, [Path(item["path"]).stem for item in kept]), ([], ["b"]))
        item = next(item for item in publish_queue.load_queue() if Path(item["path"]).stem == "b")
        self.assertEqual(item["state"], "queued")
        self.assertIn("予約できません", item["message"])

    def test_new_items_go_after_scheduled_ones(self):
        publish_queue.enqueue(self._paths("a", "b"), interval_hours=2, now=NOW, clips=self.clips)
        publish_queue.schedule_waiting(now=NOW, schedule=self._schedule_fake()[0])
        added, _ = publish_queue.enqueue(self._paths("c"), interval_hours=2, now=NOW + 60, clips=self.clips)
        self.assertEqual(added[0]["not_before"], NOW + 4 * HOUR)

    def test_restart_keeps_scheduled_times(self):
        publish_queue.enqueue(self._paths("a", "b"), interval_hours=3, now=NOW, clips=self.clips)
        publish_queue.schedule_waiting(now=NOW, schedule=self._schedule_fake()[0])
        publish_queue.recover_interrupted(now=NOW + HOUR)
        times = {Path(item["path"]).stem: item["not_before"] for item in publish_queue.load_queue()}
        self.assertEqual(times["b"], NOW + 3 * HOUR)
        self.assertEqual(times["a"], NOW + 6 * HOUR)  # overdue queued one goes a full gap after the scheduled one

    def test_settle_marks_public_done_and_gives_up_late(self):
        publish_queue.enqueue(self._paths("a", "b", "c"), interval_hours=1, now=NOW, clips=self.clips)
        publish_queue.schedule_waiting(now=NOW, schedule=self._schedule_fake()[0])
        receipts = {"b": {"video_id": "vid_b", "privacy_status": "public"},
                    "c": {"video_id": "vid_c", "privacy_status": "private"}}
        refreshed, synced = [], []
        find = lambda path: made_clips.MadeClip(path=Path(path), job_id="", job_source="", created_at=NOW,
                                                source_name="", title="", batch=False,
                                                receipt=receipts[Path(path).stem])
        later = NOW + 2 * HOUR + publish_queue.SETTLE_GRACE_SEC
        settled = publish_queue.settle_scheduled(now=later, refresh=refreshed.append, find=find,
                                                 on_published=synced.append)
        self.assertEqual([(Path(item["path"]).stem, item["state"]) for item in settled], [("b", "done")])
        self.assertEqual(len(refreshed[0]), 2)
        self.assertEqual([receipt["video_id"] for receipt in synced], ["vid_b"])
        settled = publish_queue.settle_scheduled(now=NOW + 2 * HOUR + publish_queue.SETTLE_GIVE_UP_SEC,
                                                 refresh=refreshed.append, find=find)
        self.assertEqual([(Path(item["path"]).stem, item["state"]) for item in settled], [("c", "failed")])

    def test_cancel_removes_youtube_schedule(self):
        clips = [self.clips[0], _uploaded(self.root, "b", "unlisted"), self.clips[2]]
        publish_queue.enqueue(self._paths("a", "b", "c"), interval_hours=1, now=NOW, clips=clips)
        publish_queue.schedule_waiting(now=NOW, schedule=self._schedule_fake()[0])
        calls = []

        def unschedule(path, privacy):
            calls.append((Path(path).stem, privacy))
            if Path(path).stem == "c":
                raise youtube_upload.UploadError("quota exceeded")

        count, failed = publish_queue.cancel_waiting(now=NOW + 60, unschedule=unschedule)
        self.assertEqual(calls, [("b", "unlisted"), ("c", "private")])
        self.assertEqual((count, failed), (2, [("title c", "quota exceeded")]))
        states = {Path(item["path"]).stem: item["state"] for item in publish_queue.load_queue()}
        self.assertEqual(states, {"a": "cancelled", "b": "cancelled", "c": "scheduled"})

    def test_enqueue_skips_clip_scheduled_in_studio(self):
        clips = [_clip(self.root, "s", {"video_id": "vid_s", "privacy_status": "private",
                                         "publish_at": "2030-01-01T00:00:00Z"})]
        added, skipped = publish_queue.enqueue(self._paths("s"), now=NOW, clips=clips)
        self.assertEqual((added, skipped), ([], [("title s", "すでにYouTubeで公開予約されています")]))


if __name__ == "__main__":
    unittest.main()
