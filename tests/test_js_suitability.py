"""Which picked clips become JS-drawn Shorts: js_score from the picking AI and 「JS本数」.

Everything AI- and render-related is mocked; the library is a temporary SQLite file.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cut_mcp
from moment_retrieval import agent_runner, auto_pipeline, db, js_suitability, jsdraw_pipeline, oneshot_selector
from moment_retrieval.mcp_library import LibraryTools
from tests.test_jsdraw_pipeline import _Isolated


class PromptAndSchemaTest(unittest.TestCase):
    def test_one_call_schema_has_js_score_only_when_asked(self):
        self.assertIs(oneshot_selector.output_schema(), oneshot_selector.OUTPUT_SCHEMA)
        off = oneshot_selector.output_schema(True, False)["properties"]["clips"]["items"]
        self.assertNotIn("js_score", off["properties"])
        self.assertNotIn("js_score", off["required"])
        on = oneshot_selector.output_schema(True, True)["properties"]["clips"]["items"]
        self.assertEqual(on["properties"]["js_score"]["type"], "integer")
        self.assertIn("js_score", on["required"])
        self.assertIn("js_reason", on["required"])
        self.assertIn("summary", on["required"])
        # the shared constant is never changed in place
        self.assertNotIn("js_score", oneshot_selector.OUTPUT_SCHEMA["properties"]["clips"]["items"]["properties"])

    def test_one_call_prompt_mentions_js_score_only_when_asked(self):
        args = dict(clip_count=3, min_sec=20, max_sec=60, want_summary=True)
        self.assertNotIn("js_score", oneshot_selector.user_prompt("[0:00:00] a", **args))
        self.assertIn("js_score", oneshot_selector.user_prompt("[0:00:00] a", **args, want_js_score=True))

    def test_cli_prompt_mentions_js_score_only_when_asked(self):
        for transcript in ("", "[0:00:00] #1-2 本文"):
            args = dict(want_summary=True, transcript=transcript, transcript_revision="r1" if transcript else "")
            self.assertNotIn("js_score", agent_runner.ClipRequest("vid_x", 3, **args).prompt())
            request = agent_runner.ClipRequest("vid_x", 3, want_js_score=True, **args)
            self.assertIn("js_score", request.prompt())
            self.assertIn("js_reason", request.prompt())

    def test_mcp_proposal_schema_has_js_score_only_when_asked(self):
        off, on = cut_mcp.proposal_schema(False), cut_mcp.proposal_schema(True)
        self.assertNotIn("js_score", off["properties"])
        self.assertIn("js_score", on["required"])
        item = {"start_segment_id": 1, "end_segment_id": 2, "title": "t", "reason": "r"}
        cut_mcp._validate(item, off)
        with self.assertRaises(cut_mcp.ToolError):  # extra field rejected when off
            cut_mcp._validate({**item, "js_score": 50, "js_reason": "数字"}, off)
        cut_mcp._validate({**item, "js_score": 50, "js_reason": "数字"}, on)
        with self.assertRaises(cut_mcp.ToolError):  # required when on
            cut_mcp._validate(item, on)
        with self.assertRaises(cut_mcp.ToolError):
            cut_mcp._validate({**item, "js_score": 101, "js_reason": "x"}, on)

    def test_mcp_server_gets_the_flag_per_run_only(self):
        with patch.dict(os.environ, {"CUT_VIDEO_JS_SCORE": "1", "CUT_VIDEO_OTHER": "y"}):
            off = agent_runner._mcp_server(False)["env"]
            on = agent_runner._mcp_server(True)["env"]
        self.assertNotIn("CUT_VIDEO_JS_SCORE", off)
        self.assertEqual(off["CUT_VIDEO_OTHER"], "y")
        self.assertEqual(on["CUT_VIDEO_JS_SCORE"], "1")

    def test_both_cli_commands_carry_the_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent_runner.build_command("claude", "claude", Path(tmp), js_score=True)
            config = json.loads((Path(tmp) / "cut_mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(config["mcpServers"]["cut"]["env"]["CUT_VIDEO_JS_SCORE"], "1")
            codex = agent_runner.build_command("codex", "codex", Path(tmp), js_score=True)
            self.assertIn('mcp_servers.cut_auto.env.CUT_VIDEO_JS_SCORE="1"', codex)
            codex_off = agent_runner.build_command("codex", "codex", Path(tmp))
            self.assertFalse(any("CUT_VIDEO_JS_SCORE" in part for part in codex_off))


class CleanTest(unittest.TestCase):
    def test_scores_are_clamped_and_bad_ones_dropped(self):
        self.assertEqual(js_suitability.clean({"js_score": 150, "js_reason": " 金額 "}),
                         {"js_score": 100, "js_reason": "金額"})
        self.assertEqual(js_suitability.clean({"js_score": -3})["js_score"], 0)
        self.assertEqual(js_suitability.clean({"js_score": "80"}), {})
        self.assertEqual(js_suitability.clean({"js_score": True}), {})
        self.assertEqual(js_suitability.clean({}), {})


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "index.db"
        conn = self.connect()
        db.init_db(conn, create_backup=False)
        storage = "vid_" + "d" * 32
        db.insert_video(conn, storage, str(Path(self.tmp.name) / "s.mp4"), 120)
        for index in range(8):
            db.insert_segment(conn, storage, SimpleNamespace(start=index * 10.0, end=index * 10.0 + 9,
                                                             text=f"発話{index}", words=[]))
        self.revision = db.mark_asr_complete(conn, storage)
        self.video_id = db.public_video_id(conn, storage)
        self.ids = [int(r["segment_id"]) for r in db.get_segments(conn, storage)]
        conn.close()
        self.tools = LibraryTools(self.connect)

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _candidates(self, run_id):
        conn = self.connect()
        try:
            return db.get_highlight_candidates(conn, run_id)
        finally:
            conn.close()

    def test_score_and_reason_are_stored_in_metadata(self):
        result = self.tools.propose_clips(self.video_id, self.revision, [
            {"start_segment_id": self.ids[0], "end_segment_id": self.ids[2], "title": "金額の話", "reason": "r",
             "js_score": 87, "js_reason": "金額が出る"},
            {"start_segment_id": self.ids[4], "end_segment_id": self.ids[6], "title": "雑談", "reason": "r"},
        ], min_duration_sec=20, max_duration_sec=60)
        stored = self._candidates(result["highlight_run_id"])
        self.assertEqual(stored[0]["metadata"], {"js_score": 87, "js_reason": "金額が出る"})
        self.assertEqual(js_suitability.score_of(stored[0]), 87)
        self.assertEqual(stored[1]["metadata"], {})
        self.assertIsNone(js_suitability.score_of(stored[1]))
        self.assertNotIn("metadata_json", stored[0])

    @unittest.skipIf(sqlite3.sqlite_version_info < (3, 35, 0), "DROP COLUMN needs SQLite 3.35")
    def test_old_database_without_the_column_still_works(self):
        result = self.tools.propose_clips(self.video_id, self.revision, [
            {"start_segment_id": self.ids[0], "end_segment_id": self.ids[2], "title": "古い候補", "reason": "r"},
        ], min_duration_sec=20, max_duration_sec=60)
        conn = self.connect()
        conn.execute("ALTER TABLE highlight_candidates DROP COLUMN metadata_json")
        conn.commit()
        old = db.get_highlight_candidates(conn, result["highlight_run_id"])
        self.assertEqual(old[0]["metadata"], {})  # read before migration
        db.init_db(conn, create_backup=False)  # adds the column back
        self.assertEqual(db.get_highlight_candidates(conn, result["highlight_run_id"])[0]["metadata"], {})
        conn.close()

    def test_one_call_selector_keeps_and_saves_the_score(self):
        segments = [{"segment_id": i, "start_sec": i * 10.0, "end_sec": i * 10.0 + 9} for i in range(8)]
        kept, _rejected = oneshot_selector.check_proposals(segments, [
            {"start": "0:00:00", "end": "0:00:30", "title": "a", "reason": "r", "js_score": 70, "js_reason": "手順"},
            {"start": "0:00:40", "end": "0:01:10", "title": "b", "reason": "r"},
        ], min_sec=20, max_sec=60)
        self.assertEqual((kept[0]["js_score"], kept[0]["js_reason"]), (70, "手順"))
        self.assertNotIn("js_score", kept[1])
        library = SimpleNamespace(propose_clips=Mock(return_value={}))
        conn = self.connect()
        with patch.object(oneshot_selector.db, "get_conn", return_value=conn), \
                patch.object(oneshot_selector.db, "get_active_transcript_revision", return_value="r1"):
            oneshot_selector.save_proposals("vid_x", {"clips": kept}, library=library)
        sent = library.propose_clips.call_args.args[2]
        self.assertEqual(sent[0]["js_score"], 70)
        self.assertNotIn("js_score", sent[1])


class PickTest(unittest.TestCase):
    def test_top_n_by_score_in_proposal_order(self):
        self.assertEqual(js_suitability.pick([40, 90, 70, 10], 2), [1, 2])

    def test_ties_keep_proposal_order(self):
        self.assertEqual(js_suitability.pick([50, 80, 80, 80], 2), [1, 2])

    def test_missing_scores_use_proposal_order(self):
        self.assertEqual(js_suitability.pick([None, None, None], 2), [0, 1])

    def test_scored_clips_come_before_unscored_ones(self):
        self.assertEqual(js_suitability.pick([None, 5, None, 60], 3), [0, 1, 3])

    def test_count_above_the_clips_and_zero_mean_all(self):
        self.assertEqual(js_suitability.pick([1, 2], 5), [0, 1])
        self.assertEqual(js_suitability.pick([1, 2, 3], 0), [0, 1, 2])


class FinishAssignmentTest(unittest.TestCase):
    def _run(self, scores, count, js_effect=None):
        from moment_retrieval import finishing, relisten

        outputs = [f"clip{i}.mp4" for i in range(len(scores))]
        candidates = {output: (None if score is None else
                               {"metadata": {"js_score": score, "js_reason": f"理由{i}"}})
                      for i, (output, score) in enumerate(zip(outputs, scores))}
        job = auto_pipeline.AutoJob(job_id="auto_rank", source="x", agent="claude", finish=True, jsdraw=True,
                                    jsdraw_count=count, highlight_run_id="run_1", outputs=list(outputs))
        pipeline = auto_pipeline.AutoPipeline(steps={})
        with patch.object(jsdraw_pipeline, "candidate_for", side_effect=lambda clip, _run: candidates[clip.name]), \
                patch.object(jsdraw_pipeline, "make_for_clip",
                             side_effect=js_effect, return_value={"seconds": 50.0, "usd": 0.1}) as js, \
                patch.object(finishing, "finish_exported_clip",
                             return_value=finishing.FinishPlan(hook_text="引き")) as normal, \
                patch.object(relisten, "release_model"):
            pipeline._finish(job)
        js_clips = sorted(call.args[0].name for call in js.call_args_list)
        normal_clips = sorted(call.args[0].name for call in normal.call_args_list)
        return job, js, js_clips, normal_clips

    def test_top_scores_get_js_and_the_rest_the_normal_finish(self):
        job, js, js_clips, normal_clips = self._run([40, 90, None, 75], 2)
        self.assertEqual(js_clips, ["clip1.mp4", "clip3.mp4"])
        self.assertEqual(normal_clips, ["clip0.mp4", "clip2.mp4"])
        suitability = {call.args[0].name: call.kwargs["suitability"] for call in js.call_args_list}
        self.assertEqual(suitability["clip1.mp4"], {"js_score": 90, "js_reason": "理由1"})
        text = "\n".join(job.log)
        self.assertIn("JS描画は2/4本", text)
        self.assertIn("90点・理由1", text)
        self.assertIn("75点・理由3", text)

    def test_without_scores_proposal_order_is_used(self):
        job, _js, js_clips, _normal = self._run([None, None, None], 1)
        self.assertEqual(js_clips, ["clip0.mp4"])
        self.assertTrue(any("提案順" in line for line in job.log))

    def test_failed_js_clip_falls_back_and_no_other_clip_is_promoted(self):
        job, js, js_clips, normal_clips = self._run([90, 10, 20], 1, js_effect=RuntimeError("描画失敗"))
        self.assertEqual(js_clips, ["clip0.mp4"])
        self.assertEqual(js.call_count, 1)
        self.assertEqual(normal_clips, ["clip0.mp4", "clip1.mp4", "clip2.mp4"])
        self.assertTrue(any("JS描画に失敗" in line for line in job.log))

    def test_old_job_without_a_count_draws_every_clip(self):
        _job, _js, js_clips, normal_clips = self._run([10, 20], 0)
        self.assertEqual(js_clips, ["clip0.mp4", "clip1.mp4"])
        self.assertEqual(normal_clips, [])

    def test_submit_caps_the_count_at_the_clip_count(self):
        pipeline = auto_pipeline.AutoPipeline(steps={})
        with patch.object(pipeline, "_ensure_worker"), patch.object(pipeline, "_save"):
            self.assertEqual(pipeline.submit("https://youtu.be/abcDEF12345", "claude", clip_count=2,
                                             jsdraw=True, jsdraw_count=5).jsdraw_count, 2)
            self.assertEqual(pipeline.submit("https://youtu.be/abcDEF12345", "claude", clip_count=4,
                                             jsdraw=True, jsdraw_count=0).jsdraw_count, 1)
            self.assertEqual(pipeline.submit("https://youtu.be/abcDEF12345", "claude", clip_count=5,
                                             jsdraw=True).jsdraw_count, 3)


class JsdrawRecordTest(_Isolated):
    def test_score_is_written_into_the_jsdraw_json(self):
        clip, _video_id, run_id, _source = self._clip()

        def fake_make_short(video_path, start, end, **kwargs):
            out = Path(kwargs["out_dir"])
            out.mkdir(parents=True, exist_ok=True)
            (out / "x.mp4").write_bytes(b"js")
            return {"mp4": str(out / "x.mp4"), "usd": 0.1, "seconds": 50.0}

        with patch("moment_retrieval.used_ranges.for_video", return_value=[]):
            jsdraw_pipeline.make_for_clip(clip, model="sonnet", highlight_run_id=run_id, make_short=fake_make_short,
                                          suitability={"js_score": 82, "js_reason": "手順がある"})
        record = json.loads(clip.with_suffix(".jsdraw.json").read_text(encoding="utf-8"))
        self.assertEqual((record["js_score"], record["js_reason"]), (82, "手順がある"))
        self.assertIsNotNone(jsdraw_pipeline.candidate_for(clip, run_id))
        self.assertIsNone(jsdraw_pipeline.candidate_for(clip, ""))


class CountSettingTest(unittest.TestCase):
    def setUp(self):
        import auto_publish_app
        from moment_retrieval import config

        self.app = auto_publish_app
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = patch.object(config, "LIBRARY_ROOT", Path(self.tmp.name))
        root.start()
        self.addCleanup(root.stop)

    def test_default_is_three(self):
        self.assertEqual(self.app._saved_jsdraw_count(), 3)

    def test_count_is_remembered_and_keeps_other_settings(self):
        self.app.remember_jsdraw(True, "opus")
        self.app.remember_jsdraw_count(5, 10)
        self.assertEqual(self.app._saved_jsdraw_count(), 5)
        self.assertEqual(self.app._saved_jsdraw(), (True, "opus"))
        self.app.remember_jsdraw(True, "sonnet")
        self.assertEqual(self.app._saved_jsdraw_count(), 5)

    def test_count_is_capped_at_the_clip_count_and_shown_only_when_checked(self):
        update = self.app.jsdraw_count_update(True, 5, 2)
        self.assertEqual((update["value"], update["maximum"], update["visible"]), (2, 2, True))
        self.assertFalse(self.app.jsdraw_count_update(False, 3, 5)["visible"])
        self.assertEqual(self.app.jsdraw_count_update(True, 0, 5)["value"], 1)
        self.assertEqual(self.app.jsdraw_count_update(True, None, 5)["value"], 3)
        self.app.remember_jsdraw_count(9, 4)
        self.assertEqual(self.app._saved_jsdraw_count(), 4)

    def test_saved_count_is_restored_on_load(self):
        self.app.remember_agent("claude", "sonnet")
        self.app.remember_jsdraw(True, "fable")
        self.app.remember_jsdraw_count(2, 10)
        update = self.app.apply_jsdraw_count_settings(5)
        self.assertEqual((update["value"], update["visible"]), (2, True))
        self.app.remember_jsdraw(False, "fable")
        self.assertFalse(self.app.apply_jsdraw_count_settings(5)["visible"])

    def test_start_passes_the_capped_count(self):
        pipeline = SimpleNamespace(submit=Mock(return_value=SimpleNamespace(job_id="auto_x")))
        with patch.object(self.app, "_auto_pipeline", return_value=pipeline), \
                patch.object(self.app, "_require_agent_ready"), patch.object(self.app, "_require_jsdraw_ready"), \
                patch.object(self.app, "auto_jobs_view", return_value=("jobs", None, None)), \
                patch.object(self.app.gr, "Info"):
            self.app.auto_submit("url", "https://youtu.be/abcDEF12345", [], "claude", "", 2, "", 20, 60, "blur",
                                 False, "", True, True, True, "fable", 6)
        self.assertEqual(pipeline.submit.call_args.kwargs["jsdraw_count"], 2)


if __name__ == "__main__":
    unittest.main()
