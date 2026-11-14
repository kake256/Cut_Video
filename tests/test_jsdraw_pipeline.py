"""Auto-publish option: make each picked clip as a JS-drawn Short (all API and rendering mocked)."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from moment_retrieval import agent_runner, auto_pipeline, config, db, jsdraw_pipeline, jsdraw_short, oneshot_selector


def _segments(count=200, step=5.0):
    return [{"segment_id": i, "start_sec": i * step, "end_sec": i * step + step} for i in range(count)]


class WidenRangeTest(unittest.TestCase):
    def test_short_clip_grows_evenly_to_about_ninety_seconds_on_segment_edges(self):
        start, end = jsdraw_pipeline.widen_range(502.0, 543.0, _segments(), stream_end=1000.0)
        self.assertEqual((start, end), (480.0, 565.0))  # 41 s + 24.5 s each side, snapped inward
        self.assertGreaterEqual(end - start, 80.0)
        self.assertLessEqual(start, 502.0)
        self.assertGreaterEqual(end, 543.0)

    def test_long_clip_passes_through(self):
        self.assertEqual(jsdraw_pipeline.widen_range(100.0, 230.0, _segments(), stream_end=1000.0), (100.0, 230.0))

    def test_stream_start_gives_the_rest_to_the_other_side(self):
        start, end = jsdraw_pipeline.widen_range(10.0, 40.0, _segments(), stream_end=1000.0)
        self.assertEqual(start, 0.0)
        self.assertEqual(end, 90.0)

    def test_stream_end_is_not_crossed(self):
        start, end = jsdraw_pipeline.widen_range(960.0, 990.0, _segments(), stream_end=1000.0)
        self.assertLessEqual(end, 1000.0)
        self.assertEqual(start, 910.0)

    def test_posted_ranges_are_not_crossed_but_the_clips_own_record_is_ignored(self):
        used = [{"start_sec": 450.0, "end_sec": 490.0},  # posted just before
                {"start_sec": 500.0, "end_sec": 540.0}]  # this clip's own record (overlaps it)
        start, end = jsdraw_pipeline.widen_range(500.0, 540.0, _segments(), stream_end=1000.0, used=used)
        self.assertEqual(start, 490.0)
        self.assertEqual(end, 580.0)

    def test_boxed_in_clip_stays_short(self):
        used = [{"start_sec": 400.0, "end_sec": 495.0}, {"start_sec": 535.0, "end_sec": 600.0}]
        start, end = jsdraw_pipeline.widen_range(500.0, 530.0, _segments(), stream_end=1000.0, used=used)
        self.assertEqual((start, end), (495.0, 535.0))


class LengthGuaranteeTest(unittest.TestCase):
    def test_total_over_the_limit_is_trimmed_at_the_quietest_point(self):
        off = 100.0
        env = [60.0] * 20000
        for i in range(round((175.0 - off) * 100) - 5, round((175.0 - off) * 100) + 6):
            env[i] = 5.0  # a pause at 175.0 s
        segs = [[110.0, 140.0], [150.0, 182.0]]  # 62 s
        fitted = jsdraw_short.fit_length(segs, env, off)
        self.assertLessEqual(jsdraw_short.segs_total(fitted), jsdraw_short.SEGS_MAX_SEC + 1e-6)
        self.assertEqual(fitted[0], [110.0, 140.0])
        self.assertAlmostEqual(fitted[1][1], 175.0, places=1)

    def test_tiny_last_segment_is_dropped(self):
        env = [50.0] * 20000
        segs = [[0.0, 56.0], [60.0, 61.0]]
        fitted = jsdraw_short.fit_length(segs, env, 0.0)
        self.assertEqual(fitted, [[0.0, 56.0]])

    def test_in_range_total_is_left_alone(self):
        segs = [[0.0, 30.0], [40.0, 65.0]]
        self.assertEqual(jsdraw_short.fit_length(segs, [50.0] * 9000, 0.0), segs)

    def test_out_of_range_total_is_named_in_the_fix_request(self):
        self.assertIn("71.5秒", jsdraw_short.length_note(71.5))
        self.assertIn("55秒前後", jsdraw_short.length_note(71.5))
        self.assertIn("40.0秒", jsdraw_short.length_note(40.0))
        self.assertEqual(jsdraw_short.length_note(55.0), "")
        self.assertLessEqual(jsdraw_short.SEGS_MAX_SEC + jsdraw_short.TAIL, 58.0)


class _Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "video").mkdir()
        self.patches = [
            patch.object(config, "LIBRARY_ROOT", root / "lib"),
            patch.object(config, "CACHE_ROOT", root / "cache"),
            patch.object(config, "SOURCE_ROOTS", (root / "video",)),
            patch.object(config, "DB_PATH", root / "lib" / "index.db"),
        ]
        for item in self.patches:
            item.start()
        self.root = root

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def _clip(self, start=500.0, end=540.0):
        """A library video (1000 s of 5 s segments), a proposal run and an exported clip's sidecar."""
        source = self.root / "video" / "src.mp4"
        source.write_bytes(b"x")
        conn = db.get_conn()
        db.init_db(conn, create_backup=False)
        storage = "vid_" + "b" * 32
        db.insert_video(conn, storage, str(source), 1000.0)
        for i in range(200):
            db.insert_segment(conn, storage, SimpleNamespace(start=i * 5.0, end=i * 5.0 + 5.0, text=f"発話{i}",
                                                             words=[]))
        db.mark_asr_complete(conn, storage)
        video_id = db.public_video_id(conn, storage)
        conn.close()
        from moment_retrieval.mcp_library import LibraryTools

        revision = oneshot_selector.preloaded_transcript(video_id)[1]
        conn = db.get_conn()
        ids = {float(r["start_sec"]): int(r["segment_id"])
               for r in db.get_segments(conn, video_id, transcript_revision=revision)}
        conn.close()
        run = LibraryTools().propose_clips(video_id, revision, [{
            "start_segment_id": ids[start], "end_segment_id": ids[end - 5.0], "title": "借金ルート",
            "reason": "笑える", "summary": "免許代を親に借りる流れ"}])
        clip = self.root / "clips" / "a_short.mp4"
        clip.parent.mkdir()
        clip.write_bytes(b"old")
        clip.with_suffix(".metadata.json").write_text(json.dumps({
            "title": "借金ルート", "description": "説明",
            "source_range": {"video_id": video_id, "start_sec": start, "end_sec": end}}, ensure_ascii=False),
            encoding="utf-8")
        return clip, video_id, run["highlight_run_id"], source


class MakeForClipTest(_Isolated):
    def test_clip_is_replaced_with_the_js_short_and_keeps_its_metadata(self):
        clip, video_id, run_id, source = self._clip()
        seen = {}

        def fake_make_short(video_path, start, end, **kwargs):
            seen.update(video_path=video_path, start=start, end=end, **kwargs)
            out = Path(kwargs["out_dir"])
            out.mkdir(parents=True, exist_ok=True)
            (out / "x.mp4").write_bytes(b"js")
            return {"mp4": str(out / "x.mp4"), "usd": 0.11, "seconds": 56.5, "segs_total_generated": 55.0,
                    "usage": []}

        with patch("moment_retrieval.used_ranges.for_video", return_value=[]):
            record = jsdraw_pipeline.make_for_clip(clip, model="sonnet", highlight_run_id=run_id,
                                                   make_short=fake_make_short)
        self.assertEqual(clip.read_bytes(), b"js")
        self.assertEqual(json.loads(clip.with_suffix(".metadata.json").read_text(encoding="utf-8"))["title"],
                         "借金ルート")
        self.assertEqual((seen["start"], seen["end"]), (475.0, 565.0))
        self.assertEqual(seen["model"], "sonnet")
        self.assertEqual(seen["title"], "借金ルート")
        self.assertEqual(seen["story"], "免許代を親に借りる流れ")
        self.assertEqual(seen["video_id"], video_id)
        self.assertEqual(Path(seen["video_path"]), source)
        self.assertEqual(record["usd"], 0.11)
        self.assertFalse(clip.with_name(f".{clip.stem}.jsdraw").exists())
        self.assertTrue(clip.with_suffix(".jsdraw.json").is_file())

    def test_section_file_is_used_with_its_offset(self):
        clip, _video_id, run_id, _source = self._clip()
        section = self.root / "sec.mp4"
        section.write_bytes(b"s")
        seen = {}

        def fake_make_short(video_path, start, end, **kwargs):
            seen.update(video_path=video_path, offset=kwargs["media_offset"])
            raise RuntimeError("stop here")

        with patch("moment_retrieval.used_ranges.for_video", return_value=[]), \
                self.assertRaises(RuntimeError):
            jsdraw_pipeline.make_for_clip(clip, model="fable", highlight_run_id=run_id, section=(section, 470.0),
                                          make_short=fake_make_short)
        self.assertEqual((Path(seen["video_path"]), seen["offset"]), (section, 470.0))
        self.assertEqual(clip.read_bytes(), b"old")


class FinishStepTest(_Isolated):
    def _job(self, **kwargs):
        return auto_pipeline.AutoJob(job_id="auto_js", source="x", agent="claude", finish=True, jsdraw=True,
                                     jsdraw_model="sonnet", highlight_run_id="run_1", **kwargs)

    def test_checked_box_dispatches_to_make_short_not_the_normal_finish(self):
        from moment_retrieval import finishing

        pipeline = auto_pipeline.AutoPipeline(steps={})
        job = self._job()
        with patch.object(jsdraw_pipeline, "make_for_clip", return_value={"seconds": 56.5, "usd": 0.1}) as js, \
                patch.object(finishing, "finish_exported_clip", side_effect=AssertionError("normal finish")):
            pipeline._finish_one(job, 1, str(self.root / "a.mp4"))
        js.assert_called_once()
        self.assertEqual(js.call_args.kwargs["model"], "sonnet")
        self.assertEqual(js.call_args.kwargs["highlight_run_id"], "run_1")
        self.assertTrue(any("JS描画ショートを作りました" in line for line in job.log))

    def test_failure_falls_back_to_the_normal_finish_and_says_why(self):
        from moment_retrieval import finishing

        pipeline = auto_pipeline.AutoPipeline(steps={})
        job = self._job()
        with patch.object(jsdraw_pipeline, "make_for_clip", side_effect=RuntimeError("Batch失敗")), \
                patch.object(finishing, "finish_exported_clip",
                             return_value=finishing.FinishPlan(hook_text="引き")) as normal:
            pipeline._finish_one(job, 2, str(self.root / "b.mp4"))
        normal.assert_called_once()
        self.assertTrue(any("JS描画に失敗" in line and "Batch失敗" in line for line in job.log))

    def test_unchecked_box_keeps_the_normal_finish(self):
        from moment_retrieval import finishing

        pipeline = auto_pipeline.AutoPipeline(steps={})
        job = auto_pipeline.AutoJob(job_id="auto_n", source="x", agent="claude", finish=True)
        with patch.object(jsdraw_pipeline, "make_for_clip", side_effect=AssertionError("js")), \
                patch.object(finishing, "finish_exported_clip",
                             return_value=finishing.FinishPlan(hook_text="引き")) as normal:
            pipeline._finish_one(job, 1, str(self.root / "c.mp4"))
        normal.assert_called_once()


class SubmitTest(_Isolated):
    def test_no_key_stops_the_job_at_start_only_on_the_api_route(self):
        pipeline = auto_pipeline.AutoPipeline(steps={})
        with patch("moment_retrieval.api_keys.resolve", return_value=("", "")), \
                patch.object(pipeline, "_ensure_worker"), patch.object(pipeline, "_save"):
            for agent in ("claude", "codex"):  # login CLIs draw on their plan: no key needed
                self.assertTrue(pipeline.submit("https://youtu.be/abcDEF12345", agent, jsdraw=True).jsdraw)
        pipeline = auto_pipeline.AutoPipeline(steps={})
        with patch("moment_retrieval.api_keys.resolve", return_value=("", "")), \
                self.assertRaises(auto_pipeline.PipelineError) as raised:
            pipeline.submit("https://youtu.be/abcDEF12345", "local", jsdraw=True)
        self.assertIn("APIキー", str(raised.exception))
        self.assertIn("設定", str(raised.exception))
        self.assertEqual(pipeline.jobs, {})

    def test_with_a_key_the_job_finishes_with_js_drawing(self):
        pipeline = auto_pipeline.AutoPipeline(steps={})
        with patch("moment_retrieval.api_keys.resolve", return_value=("k" * 30, "saved")), \
                patch.object(pipeline, "_ensure_worker"), patch.object(pipeline, "_save"):
            job = pipeline.submit("https://youtu.be/abcDEF12345", "claude", jsdraw=True, jsdraw_model="sonnet",
                                  finish=False)
        self.assertTrue(job.jsdraw and job.finish)
        self.assertEqual(job.jsdraw_model, "sonnet")
        with patch("moment_retrieval.api_keys.resolve", return_value=("k" * 30, "saved")), \
                self.assertRaises(auto_pipeline.PipelineError):
            pipeline.submit("https://youtu.be/abcDEF12345", "claude", jsdraw=True, jsdraw_model="bogus")

    def test_unchecked_box_needs_no_key(self):
        pipeline = auto_pipeline.AutoPipeline(steps={})
        with patch("moment_retrieval.api_keys.resolve", return_value=("", "")), \
                patch.object(pipeline, "_ensure_worker"), patch.object(pipeline, "_save"):
            job = pipeline.submit("https://youtu.be/abcDEF12345", "claude")
        self.assertFalse(job.jsdraw)


class SynopsisRequestTest(unittest.TestCase):
    def test_cli_prompts_ask_for_a_summary_only_when_checked(self):
        for transcript in ("", "[0:00:00] #1 x"):
            on = agent_runner.ClipRequest("v", transcript=transcript, want_summary=True).prompt()
            off = agent_runner.ClipRequest("v", transcript=transcript).prompt()
            self.assertIn("summaryに話の流れが分かる", on)
            self.assertNotIn("summary", off)

    def test_oneshot_schema_and_prompt_ask_for_a_summary_only_when_checked(self):
        schema = oneshot_selector.output_schema(True)["properties"]["clips"]["items"]
        self.assertIn("summary", schema["required"])
        self.assertIs(oneshot_selector.output_schema(False), oneshot_selector.OUTPUT_SCHEMA)
        self.assertNotIn("summary", oneshot_selector.OUTPUT_SCHEMA["properties"]["clips"]["items"]["properties"])
        self.assertIn("summary", oneshot_selector.user_prompt("x", clip_count=1, min_sec=20, max_sec=180,
                                                              want_summary=True))
        self.assertNotIn("summary", oneshot_selector.user_prompt("x", clip_count=1, min_sec=20, max_sec=180))

    def test_pipeline_passes_the_request_to_the_cli_agent(self):
        pipeline = auto_pipeline.AutoPipeline(steps={})
        requests = []
        job = auto_pipeline.AutoJob(job_id="auto_s", source="x", agent="claude", finish=True, jsdraw=True,
                                    video_id="vid_x", preload_transcript=False)
        with patch.object(pipeline, "_signals", return_value=[]), \
                patch("moment_retrieval.used_ranges.for_video", return_value=[]), \
                patch("moment_retrieval.channel_performance.prompt_section", return_value=""), \
                patch.object(agent_runner, "run_agent",
                             side_effect=lambda agent, request, **_k: requests.append(request) or ""), \
                patch.object(pipeline, "_ready_run_ids", return_value=[]), \
                patch.object(pipeline, "_collect_runs", return_value="run_x"):
            pipeline._select(job)
        self.assertTrue(requests[0].want_summary)


class RoutingTest(unittest.TestCase):
    def test_drawing_follows_the_picking_ai(self):
        self.assertEqual(jsdraw_pipeline.backend_for("claude"), "claude")
        self.assertEqual(jsdraw_pipeline.backend_for("codex"), "codex")
        self.assertEqual(jsdraw_pipeline.backend_for(agent_runner.API_AGENT), "api")
        self.assertEqual(jsdraw_pipeline.backend_for("local"), "api")

    def test_note_and_availability(self):
        with patch("moment_retrieval.api_keys.resolve", return_value=("", "")):
            self.assertIn("Claude Codeのログイン枠", jsdraw_pipeline.note_for("claude"))
            self.assertIn("Codexのログイン枠", jsdraw_pipeline.note_for("codex"))
            self.assertEqual(jsdraw_pipeline.note_for("local"), jsdraw_pipeline.LOCAL_NO_KEY_NOTE)
            self.assertFalse(jsdraw_pipeline.available_for("local"))
            self.assertTrue(jsdraw_pipeline.available_for("claude"))
        with patch("moment_retrieval.api_keys.resolve", return_value=("k" * 30, "saved")):
            self.assertIn("APIで描きます", jsdraw_pipeline.note_for("local"))
            self.assertIn("APIで描きます", jsdraw_pipeline.note_for(agent_runner.API_AGENT))
            self.assertTrue(jsdraw_pipeline.available_for("local"))

    def test_finish_step_passes_the_route_and_codex_model(self):
        pipeline = auto_pipeline.AutoPipeline(steps={})
        for agent, backend, codex_model in (("claude", "claude", ""), ("codex", "codex", "gpt-6-luna"),
                                            (agent_runner.API_AGENT, "api", "")):
            job = auto_pipeline.AutoJob(job_id=f"auto_{agent}", source="x", agent=agent,
                                        model="gpt-6-luna" if agent == "codex" else "", finish=True, jsdraw=True)
            with patch.object(jsdraw_pipeline, "make_for_clip",
                              return_value={"seconds": 55.0, "usd": 0.0, "usd_equivalent": 0.4,
                                            "backend": backend}) as js:
                pipeline._finish_one(job, 1, "a.mp4")
            self.assertEqual(js.call_args.kwargs["backend"], backend)
            self.assertEqual(js.call_args.kwargs["codex_model"], codex_model)
            if backend != "api":
                self.assertTrue(any("ログインの枠" in line for line in job.log))

    def test_js_drawing_runs_two_clips_at_a_time(self):
        from moment_retrieval import relisten

        pipeline = auto_pipeline.AutoPipeline(steps={})
        job = auto_pipeline.AutoJob(job_id="auto_w", source="x", agent="claude", finish=True, jsdraw=True,
                                    outputs=["a.mp4", "b.mp4", "c.mp4"])
        with patch.object(pipeline, "_finish_one"), patch.object(relisten, "release_model"):
            pipeline._finish(job)
        self.assertTrue(any("2本ずつ並行" in line for line in job.log))


class _Done:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


class CliDrawingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        (self.work / "thumbs").mkdir()
        self.image = self.work / "thumbs" / "s2060.jpg"
        self.image.write_bytes(b"jpg")
        self.parts = [("text", "作るもの" + "あ" * 40000), ("text", "2060秒"), ("image", self.image)]

    def test_claude_gets_the_prompt_on_stdin_with_read_only_tools_and_json_is_parsed(self):
        seen = {}

        def run(command, **kwargs):
            seen["command"], seen["kwargs"] = command, kwargs
            return _Done(json.dumps({
                "is_error": False, "result": "説明\n```html\n<canvas></canvas>\n```", "num_turns": 4,
                "duration_ms": 120000, "total_cost_usd": 0.42,
                "usage": {"input_tokens": 10, "output_tokens": 9000, "cache_creation_input_tokens": 30000,
                          "cache_read_input_tokens": 50000}}))

        with patch.object(agent_runner, "find_executable", return_value="claude"), \
                patch("moment_retrieval.api_keys.agent_env", return_value={"PATH": "x"}) as env:
            text, usage = jsdraw_short._ask_claude("claude-fable-5-2", self.parts, self.work, lambda _m: None,
                                                   "生成", run=run)
        command = seen["command"]
        self.assertIn("<canvas></canvas>", text)
        self.assertEqual(command[command.index("--model") + 1], "claude-fable-5-2")
        self.assertEqual(command[command.index("--allowedTools") + 1], "Read")
        self.assertEqual(command[command.index("--add-dir") + 1], str(self.work))
        self.assertEqual(command[command.index("--effort") + 1], "medium")
        self.assertEqual(command[command.index("--output-format") + 1], "json")
        self.assertLess(len(" ".join(command)), 8000)  # the 40k-character prompt is not on the command line
        self.assertIn("あああ", seen["kwargs"]["input"])
        self.assertIn("［画像: thumbs/s2060.jpg］", seen["kwargs"]["input"])
        self.assertEqual(seen["kwargs"]["cwd"], str(self.work))
        env.assert_called_once_with("claude")  # the opt-in key rule applies
        self.assertEqual(usage["num_turns"], 4)
        self.assertEqual(usage["usd"], 0.0)
        self.assertEqual(usage["usd_equivalent"], 0.42)
        self.assertEqual(usage["cache_read_input_tokens"], 50000)

    def test_claude_error_is_raised(self):
        def run(*_a, **_k):
            return _Done(json.dumps({"is_error": True, "result": "rate limit"}), returncode=1)

        with patch.object(agent_runner, "find_executable", return_value="claude"), \
                self.assertRaises(jsdraw_short.JsDrawError) as raised:
            jsdraw_short._ask_claude("claude-sonnet-5-5", self.parts, self.work, lambda _m: None, "生成", run=run)
        self.assertIn("rate limit", str(raised.exception))

    def test_codex_attaches_images_and_reads_the_answer_file(self):
        seen = {}

        def run(command, **kwargs):
            seen["command"], seen["kwargs"] = command, kwargs
            Path(command[command.index("-o") + 1]).write_text("```html\n<x/>\n```", encoding="utf-8")
            return _Done("tokens used\n12,345\n")

        with patch.object(agent_runner, "find_executable", return_value="codex"):
            text, usage = jsdraw_short._ask_codex("gpt-6-luna", self.parts, self.work, lambda _m: None, "生成",
                                                  run=run)
        command = seen["command"]
        self.assertEqual(text, "```html\n<x/>\n```")
        self.assertIn(f"--image={self.image}", command)
        self.assertEqual(command[-1], "-")
        self.assertEqual(command[command.index("-m") + 1], "gpt-6-luna")
        self.assertIn("［添付画像 1］", seen["kwargs"]["input"])
        self.assertLess(len(" ".join(command)), 8000)
        self.assertEqual(usage["tokens_used"], 12345)

    def test_make_short_routes_both_requests_through_the_chosen_cli(self):
        calls = []

        def fake_ask(backend, model, parts, workdir, progress, label, client=None):
            calls.append((backend, model, label))
            if label == "生成":
                return "```html\n<canvas></canvas>\n```", {"usd": 0.0, "usd_equivalent": 0.3, "stop_reason": ""}
            return "直しなし", {"usd": 0.0, "usd_equivalent": 0.05}

        work = self.work / "data"
        stem_dir = work / "jsdraw" / "src_100_190"
        (stem_dir / "thumbs").mkdir(parents=True)
        (stem_dir / "meta.json").write_text(json.dumps({"nface": 10, "off": 92.0}), encoding="utf-8")
        (stem_dir / "transcript.json").write_text("[]", encoding="utf-8")
        with patch.object(config, "DATA_DIR", work), patch.object(jsdraw_short, "_ask", side_effect=fake_ask), \
                patch.object(jsdraw_short, "_client", side_effect=AssertionError("no API client")), \
                patch.object(jsdraw_short, "_preview", return_value=([], 55.0, "", 53.0)), \
                patch.object(jsdraw_short, "render", return_value=55.0):
            result = jsdraw_short.make_short("src.mp4", 100.0, 190.0, model="fable", backend="claude",
                                             out_dir=self.work / "out", progress=lambda _m: None)
        self.assertEqual(calls, [("claude", "claude-fable-5-2", "生成"), ("claude", "claude-sonnet-5-5", "直し")])
        self.assertEqual(result["usd"], 0.0)
        self.assertEqual(result["usd_equivalent"], 0.35)


if __name__ == "__main__":
    unittest.main()
