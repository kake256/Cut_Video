"""Within-batch comparison of posted clips and the hint it adds to the selection prompt."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moment_retrieval import channel_performance, config

NOW = 1_800_000_000.0
DAY = 86400


def _video(vid, views, batch, *, age_days=5, seconds=80, privacy="public", title=None):
    return {"id": vid, "title": title or f"title {vid}", "published_at": NOW - age_days * DAY,
            "seconds": seconds, "privacy": privacy, "views": views, "batch": batch}


def _library():
    """Two batches that reached viewers, one dead batch, and clips that must be ignored."""
    videos = [_video(f"a{i}", views, "jobA") for i, views in enumerate([1800, 1100, 700, 300, 200, 150, 90, 20])]
    videos += [_video(f"b{i}", views, "jobB") for i, views in enumerate([1000, 400, 300, 250, 120, 60])]
    videos += [_video(f"d{i}", views, "jobDead") for i, views in enumerate([12, 8, 5, 3])]
    videos += [_video("new", 5000, "jobA", age_days=1), _video("long", 5000, "jobB", seconds=3600),
               _video("hidden", 5000, "jobB", privacy="unlisted")]
    return videos


class ScoringTest(unittest.TestCase):
    def test_dead_batches_and_unjudgeable_clips_are_left_out(self):
        ids = {video["id"] for video in channel_performance.scored(_library(), now=NOW)}
        self.assertFalse(ids & {"d0", "d1", "d2", "d3", "new", "long", "hidden"})
        self.assertEqual(len(ids), 14)

    def test_a_gap_in_posting_time_splits_a_job(self):
        late = [{**_video(f"l{i}", views, "jobA"), "published_at": NOW - 5 * DAY + 240}
                for i, views in enumerate([11, 10, 6])]
        old = [_video(f"o{i}", views, "jobOld", age_days=400) for i, views in enumerate([900, 20])]
        ids = {video["id"] for video in channel_performance.scored(_library() + late + old, now=NOW)}
        self.assertFalse(ids & {"l0", "l1", "l2", "o0", "o1"})
        self.assertIn("a7", ids)

    def test_views_are_relative_to_the_batch(self):
        by_id = {video["id"]: video for video in channel_performance.scored(_library(), now=NOW)}
        # jobB's median is 275: its 1000 views beat jobA's 700 (median 250) only slightly.
        self.assertAlmostEqual(by_id["b0"]["relative"], 1000 / 275)
        self.assertAlmostEqual(by_id["a2"]["relative"], 700 / 250)

    def test_examples_need_enough_judged_clips(self):
        good, poor = channel_performance.examples(_library(), count=3, now=NOW)
        self.assertEqual([video["id"] for video in good], ["a0", "a1", "b0"])
        self.assertEqual([video["id"] for video in poor], ["a7", "b5", "a6"])
        self.assertEqual(channel_performance.examples(_library()[:6], now=NOW), ([], []))

    def test_duplicate_uploads_show_once(self):
        videos = _library() + [_video("a0dup", 1800, "jobA", title="title a0")]
        good, _ = channel_performance.examples(videos, count=3, now=NOW)
        self.assertEqual([video["title"] for video in good].count("title a0"), 1)

    def test_iso_durations(self):
        self.assertEqual(channel_performance._seconds("PT1M19S"), 79)
        self.assertEqual(channel_performance._seconds("PT3H10M28S"), 11428)
        self.assertEqual(channel_performance._seconds("P0D"), 0)


class PromptTest(unittest.TestCase):
    def test_prompt_lists_both_sides(self):
        text = channel_performance.prompt_section(data={"videos": _library()}, now=NOW)
        self.assertIn("title a0（1800回）", text)
        self.assertIn("title a7（20回）", text)
        self.assertNotIn("title d0", text)

    def test_off_switch_and_too_little_data(self):
        with patch.dict(os.environ, {"CUT_PERFORMANCE_HINTS": "0"}):
            self.assertEqual(channel_performance.prompt_section(data={"videos": _library()}, now=NOW), "")
        self.assertEqual(channel_performance.prompt_section(data={"videos": []}, now=NOW), "")


class _Analytics:
    """reports().query(...).execute() returning fixed rows, recording each query."""

    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def reports(self):
        return self

    def query(self, **kwargs):
        self.queries.append(kwargs)
        return self

    def execute(self):
        return {"rows": self.rows}


class RetentionTest(unittest.TestCase):
    def test_kept_is_engaged_over_all_views(self):
        analytics = _Analytics([["a", 200, 50, 31.5], ["b", 0, 0, 0.0]])
        self.assertEqual(channel_performance.fetch_retention(analytics, now=NOW),
                         {"a": {"kept": 0.25, "watched": 0.315}})
        self.assertIn("engagedViews", analytics.queries[0]["metrics"])

    def test_prompt_shows_retention_when_known(self):
        videos = [{**video, "kept": 0.2} for video in _library()]
        text = channel_performance.prompt_section(data={"videos": videos}, now=NOW)
        self.assertIn("title a0（1800回、冒頭で離れなかった人20%）", text)
        self.assertIn("title a7（20回）", text)  # too few views for the share to mean much
        self.assertNotIn("最初の数秒", text)  # 11 clips with enough views, fewer than MIN_SCORED
        busier = [{**video, "views": video["views"] * 10} for video in videos]
        text = channel_performance.prompt_section(data={"videos": busier}, now=NOW)
        self.assertIn("80%が最初の数秒で離れている", text)
        plain = channel_performance.prompt_section(data={"videos": _library()}, now=NOW)
        self.assertNotIn("最初の数秒", plain)


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = patch.object(config, "CACHE_ROOT", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def _save(self, fetched_at):
        channel_performance.cache_path().write_text(
            json.dumps({"fetched_at": fetched_at, "videos": [_video("x", 1, "j")]}), encoding="utf-8")

    def test_fresh_cache_is_used_without_fetching(self):
        self._save(NOW - 3600)
        data = channel_performance.load(now=NOW, refresh_fn=lambda: self.fail("fetched"))
        self.assertEqual(data["videos"][0]["id"], "x")

    def test_failed_refresh_keeps_the_old_copy(self):
        self._save(NOW - 3 * DAY)

        def offline():
            raise OSError("offline")

        self.assertEqual(channel_performance.load(now=NOW, refresh_fn=offline)["videos"][0]["id"], "x")
        channel_performance.cache_path().unlink()
        self.assertIsNone(channel_performance.load(now=NOW, refresh_fn=offline))

    def test_refresh_joins_job_batches(self):
        jobs = Path(self.tmp.name) / "auto_jobs"
        jobs.mkdir()
        (jobs / "auto_1.json").write_text(json.dumps(
            {"job_id": "auto_1", "source_video_key": "twitch-video:v1", "uploads": [{"video_id": "v1"}]}),
            encoding="utf-8")
        fetched = [{"id": "v1", "title": "t", "published_at": NOW, "seconds": 60, "privacy": "public",
                    "views": 3, "likes": 0, "comments": 0},
                   {"id": "v2", "title": "u", "published_at": NOW, "seconds": 60, "privacy": "public",
                    "views": 3, "likes": 0, "comments": 0}]
        analytics = _Analytics([["v1", 10, 4, 25.0]])
        with patch.object(channel_performance, "fetch_videos", return_value=fetched), \
                patch("moment_retrieval.youtube_upload._service", return_value=object()):
            data = channel_performance.refresh(now=NOW, analytics_factory=lambda: analytics)
        self.assertEqual(data["videos"][0]["kept"], 0.4)
        self.assertEqual(data["videos"][0]["watched"], 0.25)
        self.assertNotIn("kept", data["videos"][1])
        self.assertEqual(data["videos"][0]["batch"], "auto_1")
        self.assertEqual(data["videos"][0]["source"], "twitch-video:v1")
        self.assertTrue(data["videos"][1]["batch"].startswith("hour:"))
        self.assertTrue(channel_performance.cache_path().is_file())


if __name__ == "__main__":
    unittest.main()
