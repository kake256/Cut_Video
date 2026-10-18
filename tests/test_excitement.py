"""Excitement levels from audio/chat signals and how they reach the highlight prompts (no ffmpeg, network or LLM)."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from moment_retrieval import db, excitement, signals
from moment_retrieval.highlight_analysis import (
    _anchor_prompt, _boundary_label_prompt, _metadata_prompt, run_highlight_analysis,
)


def _profile(levels_per_second):
    return {"version": 1, "loudness_step": 0.1, "speech": [],
            "loudness_db": [level for level in levels_per_second for _ in range(10)]}


class ProfileTest(unittest.TestCase):
    def test_audio_only_marks_the_loud_bin_against_the_streams_own_baseline(self):
        levels = [-30 + (second % 3) for second in range(300)]
        levels[150:152] = [-8, -8]  # a shout in bin 15
        profile = excitement.compute_profile(_profile(levels), None)
        self.assertEqual(profile["sources"], ["audio"])
        by_bin = {int(item["t"]): item["level"] for item in profile["bins"]}
        self.assertEqual(by_bin[150], 10)
        self.assertLessEqual(max(level for t, level in by_bin.items() if t != 150), 2)

    def test_quiet_stream_and_loud_stream_are_judged_against_themselves(self):
        quiet = [-40 + (s % 3) for s in range(300)]
        loud = [-15 + (s % 3) for s in range(300)]
        for levels in (quiet, loud):
            levels_profile = excitement.compute_profile(_profile(levels), None)
            self.assertLessEqual(max(item["level"] for item in levels_profile["bins"]), 2)

    def test_chat_burst_and_laughs_are_shifted_earlier_for_chat_lag(self):
        messages = [[float(t), "なるほど"] for t in range(0, 600, 10)]  # one message per bin
        messages += [[305.0 + i * 0.1, "ｗｗｗ"] for i in range(20)]  # reaction to ~300 s
        profile = excitement.compute_profile(None, {"messages": messages}, duration_sec=600)
        self.assertEqual(profile["sources"], ["chat"])
        top = max(profile["bins"], key=lambda item: item["level"])
        self.assertEqual(top["t"], 300.0)  # 305 s minus the 5 s lag
        self.assertGreaterEqual(top["level"], 9)
        self.assertEqual(top["laugh"], 20)
        summary = excitement.chapter_summary(profile, 290, 320)
        line = excitement.summary_line(summary)
        self.assertTrue(line.startswith("盛り上がり: 最大"))
        self.assertIn("コメント", line)
        self.assertIn("笑い多め", line)

    def test_chat_offset_is_respected(self):
        messages = [[t + 100.0, "x"] for t in range(0, 600, 10)] + [[405.0, "草"]] * 10
        profile = excitement.compute_profile(None, {"messages": messages, "offset_sec": 100.0}, duration_sec=600)
        top = max(profile["bins"], key=lambda item: item["level"])
        self.assertEqual(top["t"], 300.0)

    def test_laugh_detection(self):
        for text in ("www", "ｗｗｗ", "草", "笑った", "lol", "それはw"):
            self.assertTrue(excitement.is_laugh(text), text)
        for text in ("wow", "おはよう", "new"):
            self.assertFalse(excitement.is_laugh(text), text)

    def test_no_signals_means_no_levels(self):
        profile = excitement.compute_profile(None, None, duration_sec=100)
        self.assertEqual(profile["sources"], [])
        self.assertIsNone(excitement.level_between(profile, 0, 10))
        self.assertEqual(excitement.summary_line(excitement.chapter_summary(profile, 0, 50)), "")
        self.assertIsNone(excitement.level_between(None, 0, 10))


class StoredProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn, create_backup=False)
        self.source = Path(self.tmp.name, "v.mp4")
        self.source.write_bytes(b"x")
        self.video_id = "vid_" + "c" * 32
        db.insert_video(self.conn, self.video_id, str(self.source), 300)
        self.storage_id = db.get_video(self.conn, self.video_id)["video_id"]
        self.cache = mock.patch.object(signals.config, "CACHE_ROOT", Path(self.tmp.name))
        self.cache.start()

    def tearDown(self):
        self.cache.stop()
        self.conn.close()
        self.tmp.cleanup()

    def _build(self, source, target):
        Path(target).write_text(json.dumps(_profile([-30] * 300)), encoding="utf-8")

    def test_profile_is_stored_and_recomputed_only_when_inputs_change(self):
        with mock.patch.object(excitement, "compute_profile", wraps=excitement.compute_profile) as compute:
            with mock.patch("moment_retrieval.speech_regions.build_profile", side_effect=self._build):
                first = excitement.ensure_profile(self.conn, self.video_id)
                again = excitement.ensure_profile(self.conn, self.video_id)
                self.assertEqual(first, again)
                self.assertEqual(compute.call_count, 1)
                signals.save_chat(self.storage_id, {"url": "u", "messages": [[5.0, "草"]]})
                with_chat = excitement.ensure_profile(self.conn, self.video_id)
        self.assertEqual(compute.call_count, 2)
        self.assertEqual(with_chat["sources"], ["audio", "chat"])
        stored = db.get_excitement_profile(self.conn, self.video_id)
        self.assertEqual(stored["version"], excitement.VERSION)
        self.assertEqual(stored["profile"], with_chat)

    def test_no_audio_and_no_chat_returns_none(self):
        self.assertIsNone(excitement.ensure_profile(self.conn, self.video_id, build_audio=False))

    def test_empty_chat_record_is_treated_as_no_chat(self):
        signals.save_chat(self.storage_id, {"url": "u", "messages": [], "unavailable": "なし"})
        self.assertIsNone(excitement.ensure_profile(self.conn, self.video_id, build_audio=False))

    def test_fetch_chat_once_skips_when_cached_and_records_unavailable(self):
        from moment_retrieval import chat_history, source_origin

        with mock.patch.object(source_origin, "origin_url_for_video", return_value="https://www.twitch.tv/videos/1"), \
             mock.patch.object(chat_history, "fetch", side_effect=chat_history.ChatUnavailable("なし")) as fetch:
            excitement.fetch_chat_once(self.conn, self.video_id)
            excitement.fetch_chat_once(self.conn, self.video_id)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(signals.load_chat(self.storage_id)["messages"], [])

    def test_deleting_the_video_removes_its_profile(self):
        db.save_excitement_profile(self.conn, self.video_id, version=1, inputs={}, profile={"bins": []})
        db.delete_video(self.conn, self.video_id)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM excitement_profiles").fetchone()[0], 0)


def _level_profile(levels, sources=("audio", "chat")):
    return {"bin_sec": 10.0, "sources": list(sources), "chat_baseline": 2.0, "laugh_baseline": 0.5,
            "bins": [{"t": i * 10.0, "level": level, "audio_z": 0.0, "chat": 2 + 7 * (level > 5),
                      "laugh": 4 * (level > 5)} for i, level in enumerate(levels)]}


class PromptTest(unittest.TestCase):
    def test_chapter_line_and_segment_levels_are_added_only_with_a_profile(self):
        chapter = {"ordinal": 0, "title": "章", "summary": "要約", "tags": [], "start_sec": 0.0, "end_sec": 40.0}
        segments = [{"segment_id": 1, "start_sec": 0.0, "end_sec": 5.0, "text": "あ"},
                    {"segment_id": 2, "start_sec": 30.0, "end_sec": 35.0, "text": "い"}]
        selection = {"reason": "理由", "category": "分類"}
        profile = _level_profile([1, 2, 3, 9])

        plain = _metadata_prompt([chapter], 3)
        self.assertNotIn("excitement", plain)
        with_line = _metadata_prompt([chapter], 3, profile)
        payload = json.loads(with_line.split("chapters=", 1)[1])
        self.assertEqual(payload[0]["excitement"], "盛り上がり: 最大9/10, コメント1.9倍, 笑い多め")
        self.assertNotIn("start_sec", payload[0])  # stage 1 stays metadata only

        self.assertNotIn("excitement", _anchor_prompt(chapter, selection, segments, 20.0, 90.0))
        anchor = _anchor_prompt(chapter, selection, segments, 20.0, 90.0, profile)
        rows = json.loads(anchor.split("segments=", 1)[1].split("\n", 1)[0])
        self.assertEqual([row["excitement"] for row in rows], [1, 9])
        self.assertIn("0〜10", anchor)

        boundary = _boundary_label_prompt(segments, 1, 20.0, 90.0, profile)
        rows = json.loads(boundary.split("allowed_segments=", 1)[1])
        self.assertEqual([row["excitement"] for row in rows], [1, 9])


class _Provider:
    name = "synthetic-local"

    def __init__(self):
        self.prompts = []

    def generate(self, *, model, prompt, output_schema=None):
        self.prompts.append(prompt)
        if "chapters=" in prompt:
            return '{"candidates":[{"chapter_ordinal":0,"reason":"盛り上がる場面です","category":"笑い"}]}'
        if "allowed_segments=" in prompt:
            rows = json.loads(prompt.split("allowed_segments=", 1)[1])
            return json.dumps({"start_segment_id": rows[0]["segment_id"], "end_segment_id": rows[-1]["segment_id"],
                               "title": "見どころ", "summary": "内容の要約です", "reason": "笑いが起きたため",
                               "category": "笑い", "tags": ["笑い"]}, ensure_ascii=False)
        if "needs_previous" in prompt:
            return '{"needs_previous":false,"needs_next":false}'
        rows = json.loads(prompt.split("segments=", 1)[1].split("\n", 1)[0])
        return json.dumps({"anchor_segment_id": rows[-1]["segment_id"]})


class RunWithExcitementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn, create_backup=False)
        self.video_id = "vid_" + "d" * 32
        db.insert_video(self.conn, self.video_id, str(Path(self.tmp.name) / "synthetic.mp4"), 80)
        for start in range(0, 40, 5):
            db.insert_segment(self.conn, self.video_id,
                              SimpleNamespace(start=float(start), end=float(start + 5), text="合成テキスト", words=[]))
        self.revision = db.mark_asr_complete(self.conn, self.video_id)
        self.analysis_id = db.create_analysis_run(self.conn, self.video_id, self.revision,
                                                  provider="test", model="test", prompt_version="v")
        db.replace_analysis_chapters(self.conn, self.analysis_id, [
            {"start_segment_id": 1, "end_segment_id": 8, "start_sec": 0, "end_sec": 40,
             "title": "章題", "summary": "章の要約です", "tags": ["話題"]}])
        db.update_analysis_run(self.conn, self.analysis_id, status="ready", summary="要約です", tags=["話題"], result={})

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_levels_reach_both_stages_and_are_recorded_for_the_cards(self):
        provider = _Provider()
        before = [tuple(r) for r in self.conn.execute("SELECT segment_id, start_sec, end_sec, text FROM asr_segments")]
        result = run_highlight_analysis(self.conn, self.video_id, self.revision, self.analysis_id, provider, "fake",
                                        requested_count=3, excitement=_level_profile([1, 2, 3, 9]))
        self.assertIn('"excitement":"盛り上がり: 最大9/10', provider.prompts[0])
        self.assertTrue(any('"excitement":9' in prompt for prompt in provider.prompts if "segments=" in prompt))
        run = db.get_highlight_run(self.conn, result["highlight_run_id"])
        self.assertTrue(run["prompt_version"].endswith("+excitement-v1"))
        self.assertEqual(run["result"]["excitement"]["sources"], ["audio", "chat"])
        self.assertEqual(len(run["result"]["excitement"]["candidates"]), result["candidate_count"])
        self.assertTrue(run["result"]["excitement"]["candidates"][0].startswith("盛り上がり: 最大9/10"))
        self.assertEqual(before, [tuple(r) for r in self.conn.execute(
            "SELECT segment_id, start_sec, end_sec, text FROM asr_segments")])

    def test_without_profile_prompts_and_version_are_unchanged(self):
        provider = _Provider()
        result = run_highlight_analysis(self.conn, self.video_id, self.revision, self.analysis_id, provider, "fake",
                                        requested_count=3, excitement={"sources": [], "bins": []})
        self.assertFalse(any("excitement" in prompt for prompt in provider.prompts))
        run = db.get_highlight_run(self.conn, result["highlight_run_id"])
        self.assertNotIn("excitement", run["prompt_version"])
        self.assertNotIn("excitement", run["result"])


if __name__ == "__main__":
    unittest.main()
