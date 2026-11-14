"""Auto clip -> private upload -> one-click publish, with every external step faked."""
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from moment_retrieval import agent_runner, auto_pipeline, channel_policy, config, youtube_upload


class _Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "video").mkdir()
        self.patches = [
            patch.object(config, "LIBRARY_ROOT", root / "lib"),
            patch.object(config, "CACHE_ROOT", root / "cache"),
            patch.object(config, "SOURCE_ROOTS", (root / "video",)),
        ]
        for item in self.patches:
            item.start()
        self.root = root

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()


class ChannelPolicyTest(_Isolated):
    def test_channel_identity_from_youtube_and_twitch_metadata(self):
        yt = channel_policy.channel_from_info({"extractor_key": "Youtube", "channel_id": "UCabc", "channel": "A"})
        tw = channel_policy.channel_from_info({"extractor_key": "TwitchVod", "uploader_id": "Streamer"})
        self.assertEqual(yt.key, "youtube:UCabc")
        self.assertEqual(tw.key, "twitch:streamer")
        self.assertIsNone(channel_policy.channel_from_info({"extractor_key": "Generic"}))

    def test_video_identity_ignores_playlists(self):
        video = channel_policy.video_from_info(
            {"extractor_key": "Youtube", "channel_id": "UCabc", "id": "abcDEF12345", "title": "T"})
        self.assertEqual((video.key, video.channel_key), ("youtube:abcDEF12345", "youtube:UCabc"))
        self.assertIsNone(channel_policy.video_from_info(
            {"extractor_key": "YoutubeTab", "channel_id": "UCabc", "id": "PL1", "_type": "playlist"}))


class AgentRunnerTest(unittest.TestCase):
    def test_claude_only_gets_cut_mcp_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = agent_runner.build_command("claude", "claude", Path(tmp))
            config_json = json.loads((Path(tmp) / "cut_mcp.json").read_text(encoding="utf-8"))
        self.assertIn("--strict-mcp-config", command)
        allowed = command[command.index("--allowedTools") + 1]
        self.assertTrue(all(name.startswith("mcp__cut__") for name in allowed.split(",")))
        self.assertTrue(config_json["mcpServers"]["cut"]["args"][0].endswith("cut_mcp.py"))

    def test_codex_runs_read_only_with_prompt_on_stdin(self):
        command = agent_runner.build_command("codex", "codex.exe", Path("."))
        self.assertEqual(command[1:4], ["exec", "-s", "read-only"])
        self.assertEqual(command[-1], "-")
        self.assertIn('mcp_servers.cut_auto.default_tools_approval_mode="approve"', command)

    def test_model_and_effort_apply_to_this_run_only(self):
        codex = agent_runner.build_command("codex", "codex.exe", Path("."), model="gpt-6-luna", effort="high")
        self.assertEqual(codex[codex.index("-m") + 1], "gpt-6-luna")
        self.assertIn('model_reasoning_effort="high"', codex)
        with tempfile.TemporaryDirectory() as tmp:
            claude = agent_runner.build_command("claude", "claude", Path(tmp), model="opus")
        self.assertEqual(claude[-2:], ["--model", "opus"])
        for agent, model, effort in (("codex", "bad model", ""), ("codex", "", "extreme"), ("claude", "", "high")):
            with self.subTest(model=model, effort=effort), self.assertRaises(agent_runner.AgentError):
                agent_runner.validate_model(agent, model, effort)
        self.assertIn(agent_runner.DEFAULT_CODEX_MODEL, [slug for _name, slug in agent_runner.codex_models()])

    def test_prompt_carries_the_request(self):
        prompt = agent_runner.ClipRequest("vid_x", 4, 15, 45).prompt()
        self.assertIn("vid_x", prompt)
        self.assertIn("4件", prompt)
        self.assertIn("allow_transcript_transfer=true", prompt)


class PipelineTest(_Isolated):
    def _pipeline(self, calls, *, channel_key="twitch:alice", source_video_key="twitch-video:123456"):
        channel = channel_policy.SourceChannel(channel_key, "Alice", "https://www.twitch.tv/alice")
        extractor = "Youtube" if channel_key.startswith("youtube:") else "TwitchVod"
        info = {"extractor_key": extractor, "id": source_video_key.split(":", 1)[1],
                "channel_id": channel_key.split(":", 1)[1] if extractor == "Youtube" else "",
                "uploader_id": channel_key.split(":", 1)[1] if extractor != "Youtube" else "",
                "title": "Source"}

        def upload(job):
            job.uploads.append({"video_id": "yt1", "title": "候補", "privacy_status": "private",
                                "studio_url": "https://studio.youtube.com/video/yt1/edit"})
            calls.append("upload")

        steps = {
            "metadata": lambda job: (calls.append("metadata"), (info, channel, None))[1],
            "download": lambda job: (calls.append("download"), str(self.root / "video" / "a.mp4"))[1],
            "index": lambda job, path: (calls.append("index"), "vid_a")[1],
            "select": lambda job: (calls.append("select"), "highlight_1")[1],
            "export": lambda job: (calls.append("export"), ["a_short.mp4"])[1],
            "upload": upload,
        }
        return auto_pipeline.AutoPipeline(index_lock=threading.Lock(), steps=steps)

    def test_runs_every_step_in_order_and_persists(self):
        calls = []
        pipeline = self._pipeline(calls)
        job = auto_pipeline.AutoJob(job_id="auto_test", source="https://www.twitch.tv/videos/123456", agent="codex")
        pipeline.jobs[job.job_id] = job
        pipeline.run(job)
        self.assertEqual(job.state, "done")
        self.assertEqual(calls, ["metadata", "download", "index", "select", "export", "upload"])
        self.assertEqual(job.source_channel_key, "twitch:alice")
        self.assertEqual(job.source_video_key, "twitch-video:123456")
        saved = json.loads((config.CACHE_ROOT / "auto_jobs" / "auto_test.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["state"], "done")
        reloaded = auto_pipeline.AutoPipeline(steps={})
        self.assertEqual(reloaded.jobs["auto_test"].highlight_run_id, "highlight_1")
        steps = [item["step"] for item in saved["timings"]]
        self.assertEqual(steps, ["metadata", "download", "index", "select", "await_video", "export", "upload"])
        self.assertTrue(all(item["seconds"] >= 0 for item in saved["timings"]))

    def test_report_sums_step_times_and_counts_fallbacks(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import report_auto_jobs

        job = {"job_id": "auto_r", "state": "done", "agent": "claude", "model": "sonnet", "created_at": 100.0,
               "_ended_at": 9999.0, "outputs": ["a", "b"], "video_id": "",
               "timings": [{"step": "index", "started_at": 100.0, "seconds": 300.0},
                           {"step": "finish", "started_at": 400.0, "seconds": 120.0}],
               "log": ["  1本目:音声の聞き直しに失敗したため、文字起こしの単語時刻を使います（RuntimeError）"]}
        row = report_auto_jobs.summarise(job)
        self.assertEqual(row["steps_min"], {"index": 5.0, "finish": 2.0})
        self.assertEqual(row["total_min"], 7.0)
        self.assertEqual(row["fallbacks"], {"聞き直し失敗→文字起こしの字幕": 1})

    def test_publish_needs_only_the_click_and_happens_once(self):
        calls = []
        pipeline = self._pipeline(calls)
        job = auto_pipeline.AutoJob(job_id="auto_pub", source="https://www.twitch.tv/videos/123456", agent="codex")
        pipeline.jobs[job.job_id] = job
        pipeline.run(job)
        with patch.object(youtube_upload, "publish",
                          return_value={"video_id": "yt1", "privacy_status": "public",
                                        "watch_url": "https://www.youtube.com/watch?v=yt1"}) as publish:
            result = pipeline.publish("auto_pub", "yt1")
        publish.assert_called_once_with("yt1")
        self.assertEqual(job.uploads[0]["privacy_status"], "public")
        self.assertIn("watch?v=yt1", result["watch_url"])
        with self.assertRaises(auto_pipeline.PipelineError):
            pipeline.publish("auto_pub", "yt1")
        with self.assertRaises(auto_pipeline.PipelineError):
            pipeline.publish("auto_pub", "unknown")

    def test_failure_and_cancel_end_the_job_readably(self):
        calls = []
        pipeline = self._pipeline(calls)
        pipeline.steps["select"] = lambda job: (_ for _ in ()).throw(RuntimeError("AIが失敗"))
        job = auto_pipeline.AutoJob(job_id="auto_fail", source="x", agent="codex")
        pipeline.jobs[job.job_id] = job
        pipeline.run(job)
        self.assertEqual(job.state, "failed")
        self.assertIn("候補選び", job.log[-1])
        job2 = auto_pipeline.AutoJob(job_id="auto_stop", source="x", agent="codex")
        pipeline.jobs[job2.job_id] = job2
        pipeline.cancelled.add("auto_stop")
        pipeline.run(job2)
        self.assertEqual(job2.state, "cancelled")

    def test_sources_must_be_urls_or_files_in_the_video_folder(self):
        inside = self.root / "video" / "clip.mp4"
        inside.write_bytes(b"x")
        outside = self.root / "elsewhere.mp4"
        outside.write_bytes(b"x")
        self.assertEqual(auto_pipeline.validate_source(str(inside)), str(inside.resolve()))
        self.assertTrue(auto_pipeline.validate_source("https://www.youtube.com/watch?v=abcDEF12345"))
        for bad in ("", str(outside), str(self.root / "missing.mp4")):
            with self.subTest(bad=bad), self.assertRaises(auto_pipeline.PipelineError):
                auto_pipeline.validate_source(bad)

    def _library(self):
        from types import SimpleNamespace
        from moment_retrieval import db, source_origin

        conn_patch = patch.object(config, "DB_PATH", self.root / "lib" / "index.db")
        conn_patch.start()
        self.addCleanup(conn_patch.stop)
        local = self.root / "video" / "local.mp4"
        local.write_bytes(b"x")
        conn = db.get_conn()
        db.init_db(conn, create_backup=False)
        ids = {}
        for name, path in (("local", local), ("shared", self.root / "video" / "__unlinked__" / "s.mp4"),
                           ("untranscribed", local.with_name("raw.mp4"))):
            storage = "vid_" + name[0] * 32
            db.insert_video(conn, storage, str(path), 60.0)
            if name != "untranscribed":
                db.insert_segment(conn, storage, SimpleNamespace(start=0.0, end=5.0, text="合成", words=[]))
                db.mark_asr_complete(conn, storage)
            ids[name] = db.public_video_id(conn, storage)
        conn.execute("UPDATE videos SET source_state = 'missing' WHERE public_video_id = ?", (ids["shared"],))
        source_origin.remember_shared_origin(conn, ids["shared"], "https://youtu.be/abcDEF12345")
        conn.close()
        return ids, local

    def test_library_lists_transcribed_videos_and_maps_shared_ones_to_their_url(self):
        ids, local = self._library()
        listed = {item["video_id"]: item for item in auto_pipeline.library_videos()}
        self.assertEqual(set(listed), {ids["local"], ids["shared"]})
        self.assertEqual(auto_pipeline.validate_source("library:" + ids["local"]), "library:" + ids["local"])
        self.assertEqual(auto_pipeline.validate_source("library:" + ids["shared"]),
                         "https://www.youtube.com/watch?v=abcDEF12345")
        with self.assertRaises(auto_pipeline.PipelineError):
            auto_pipeline.validate_source("library:" + ids["untranscribed"])
        pipeline = auto_pipeline.AutoPipeline(steps={})
        job = auto_pipeline.AutoJob(job_id="auto_lib", source="library:" + ids["local"], agent="codex")
        pipeline.jobs[job.job_id] = job
        _info, _channel, path = pipeline._metadata(job)
        self.assertEqual(Path(path), local.resolve())

    def test_downloaded_url_relinks_to_an_imported_shared_index_instead_of_whisper(self):
        ids, _local = self._library()
        downloaded = self.root / "video" / "20260101_abcDEF12345.mp4"
        downloaded.write_bytes(b"x")
        pipeline = auto_pipeline.AutoPipeline(steps={})
        job = auto_pipeline.AutoJob(job_id="auto_share", source="https://youtu.be/abcDEF12345", agent="codex")
        pipeline.jobs[job.job_id] = job
        with patch("moment_retrieval.share.relink_video") as relink, \
                patch.object(pipeline, "_run_logged", side_effect=AssertionError("Whisper must not run")):
            video_id = pipeline._index(job, downloaded)
        relink.assert_called_once_with(ids["shared"], downloaded)
        self.assertEqual(video_id, ids["shared"])

    def test_finishing_runs_between_export_and_upload_only_when_requested(self):
        calls = []
        pipeline = self._pipeline(calls)
        pipeline.steps["finish"] = lambda job: calls.append("finish")
        for finish, expected in ((False, []), (True, ["finish"])):
            calls.clear()
            job = auto_pipeline.AutoJob(job_id=f"auto_fin_{finish}", source="x", agent="codex", finish=finish)
            pipeline.jobs[job.job_id] = job
            pipeline.run(job)
            self.assertEqual([c for c in calls if c == "finish"], expected)
            if finish:
                self.assertLess(calls.index("export"), calls.index("finish"))
                self.assertLess(calls.index("finish"), calls.index("upload"))

    def test_clip_ranges_are_fetched_instead_of_waiting_while_the_video_downloads(self):
        calls = []
        pipeline = self._pipeline(calls)
        pipeline.steps["finish"] = lambda job: calls.append("finish")
        pipeline.steps["await_video"] = lambda job: calls.append("await_video")
        for sections_ok, expected in ((True, ["export", "sections", "finish"]),
                                      (False, ["export", "sections", "await_video", "finish"])):
            calls.clear()
            pipeline.steps["sections"] = lambda job, ok=sections_ok: (calls.append("sections"), ok)[1]
            job = auto_pipeline.AutoJob(job_id=f"auto_sec_{sections_ok}", source="x", agent="claude", finish=True)
            pipeline.jobs[job.job_id] = job
            with patch.object(pipeline, "_video_still_downloading", return_value=True):
                pipeline.run(job)
            self.assertEqual(job.state, "done")
            self.assertEqual([c for c in calls if c in {"export", "sections", "await_video", "finish"}], expected)

    def test_section_download_maps_each_clip_to_its_file_and_offset(self):
        from moment_retrieval import downloader

        pipeline = auto_pipeline.AutoPipeline(steps={})
        outputs = []
        for index, (start, end) in enumerate(((1.0, 30.0), (100.0, 160.0))):
            clip = self.root / f"c{index}.mp4"
            clip.with_suffix(".metadata.json").write_text(
                json.dumps({"source_range": {"start_sec": start, "end_sec": end}}), encoding="utf-8")
            outputs.append(str(clip))
        job = auto_pipeline.AutoJob(job_id="auto_map", source="https://x", agent="claude", finish=True,
                                    outputs=outputs)
        pipeline.video_downloads[job.job_id] = type("D", (), {"url": "https://x", "done": False})()
        seen = {}

        def fake(url, ranges, directory, **_kwargs):
            seen["ranges"] = ranges
            return [Path(directory) / f"section_{index:02d}.mp4" for index in range(len(ranges))]

        with patch.object(downloader, "download_sections", side_effect=fake):
            self.assertTrue(pipeline._sections(job))
        self.assertEqual(seen["ranges"], [(0.0, 32.0), (98.0, 162.0)])
        files = pipeline.clip_sources[job.job_id]["files"]
        self.assertEqual(files[outputs[1]][1], 98.0)
        self.assertEqual(files[outputs[1]][0].name, "section_01.mp4")

    def test_link_only_job_downloads_and_links_without_clipping(self):
        calls = []
        pipeline = self._pipeline(calls)
        job = auto_pipeline.AutoJob(job_id="auto_link", source="https://www.twitch.tv/videos/123456",
                                    agent="codex", link_only=True)
        pipeline.jobs[job.job_id] = job
        pipeline.run(job)
        self.assertEqual(job.state, "done")
        self.assertEqual(calls, ["metadata", "download", "index"])
        self.assertIn("文字起こしが完了しました", job.log[-1])

    def test_backfill_records_clips_uploaded_before_tracking(self):
        from moment_retrieval import db, used_ranges

        ids, _local = self._library()
        conn = db.get_conn()
        try:
            revision = db.get_active_transcript_revision(conn, ids["local"])
            analysis = db.create_analysis_run(conn, ids["local"], revision, provider="t", model="t", prompt_version="v")
            db.replace_analysis_chapters(conn, analysis, [{"start_segment_id": 1, "end_segment_id": 1, "start_sec": 0,
                                                           "end_sec": 5, "title": "章", "summary": "", "tags": []}])
            db.update_analysis_run(conn, analysis, status="ready", summary="s", tags=[], result={})
            run = db.create_highlight_run(conn, ids["local"], revision, analysis, provider="t", model="t",
                                          prompt_version="v", requested_count=1, min_duration_sec=1, max_duration_sec=5)
            db.replace_highlight_candidates(conn, run, [{
                "source_chapter_ordinal": 0, "anchor_start_segment_id": 1, "anchor_end_segment_id": 1,
                "start_segment_id": 1, "end_segment_id": 1, "start_sec": 0.0, "end_sec": 5.0,
                "title": "投稿済み", "summary": "", "reason": "r", "category": "c", "tags": []}])
        finally:
            conn.close()
        job = auto_pipeline.AutoJob(job_id="auto_old_upload", source="x", agent="codex", video_id=ids["local"],
                                    highlight_run_id=run, uploads=[{"title": "投稿済み", "video_id": "yt"}])
        self.assertEqual(used_ranges.backfill_from_jobs([job]), 1)
        self.assertEqual(used_ranges.backfill_from_jobs([job]), 0)
        self.assertEqual(used_ranges.for_video(ids["local"])[0]["title"], "投稿済み")

    def test_unfinished_jobs_are_marked_failed_after_restart(self):
        directory = config.CACHE_ROOT / "auto_jobs"
        directory.mkdir(parents=True)
        job = auto_pipeline.AutoJob(job_id="auto_old", source="x", agent="codex", state="running")
        old_payload = job.__dict__.copy()
        old_payload.pop("source_video_key")
        (directory / "auto_old.json").write_text(json.dumps(old_payload), encoding="utf-8")
        pipeline = auto_pipeline.AutoPipeline(steps={})
        self.assertEqual(pipeline.jobs["auto_old"].state, "failed")
        self.assertEqual(pipeline.jobs["auto_old"].source_video_key, "")


class DeferredRenderTest(_Isolated):
    def _status(self, outputs):
        def run_logged(job, command):
            status = Path(command[command.index("--status-file") + 1])
            status.write_text(json.dumps({"state": "done", "outputs": outputs}), encoding="utf-8")
            self.commands.append(command)
            return 0
        return run_logged

    def test_export_only_records_ranges_when_finishing_follows(self):
        pipeline = auto_pipeline.AutoPipeline(steps={})
        for finish in (False, True):
            self.commands = []
            job = auto_pipeline.AutoJob(job_id=f"auto_defer_{finish}", source="x", agent="codex",
                                        finish=finish, video_id="vid_x", highlight_run_id="run_x")
            with patch.object(auto_pipeline, "_jobs_dir", return_value=self.root),                     patch.object(pipeline, "_run_logged", side_effect=self._status(["a.mp4"])):
                pipeline._export(job)
            self.assertEqual("--defer-render" in self.commands[0], finish)

    def test_a_failed_finish_still_produces_a_plain_clip(self):
        from moment_retrieval import finishing

        pipeline = auto_pipeline.AutoPipeline(steps={})
        made, lost = self.root / "made.mp4", self.root / "lost.mp4"
        job = auto_pipeline.AutoJob(job_id="auto_plain", source="x", agent="codex", finish=True,
                                    outputs=[str(made), str(lost)])

        def plain(clip, section=None):
            if clip == made:
                clip.write_bytes(b"mp4")
                return clip
            raise RuntimeError("source gone")

        with patch.object(finishing, "finish_exported_clip", side_effect=RuntimeError("ai down")),                 patch.object(finishing, "render_plain_clip", side_effect=plain):
            pipeline._finish(job)
        self.assertEqual(job.outputs, [str(made)])
        self.assertTrue(any("仕上げなしの動画" in line for line in job.log))

    def test_clips_are_finished_side_by_side_and_the_whisper_model_released(self):
        from moment_retrieval import finishing, relisten

        pipeline = auto_pipeline.AutoPipeline(steps={})
        outputs = [self.root / f"clip{index}.mp4" for index in range(4)]
        job = auto_pipeline.AutoJob(job_id="auto_par", source="x", agent="claude", finish=True,
                                    outputs=[str(path) for path in outputs])
        running, peak, lock = [0], [0], threading.Lock()

        def finish(clip, **_kwargs):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.2)
            clip.write_bytes(b"mp4")
            with lock:
                running[0] -= 1
            return finishing.FinishPlan(hook_text="引き")

        with patch.object(auto_pipeline, "FINISH_WORKERS", 3), \
                patch.object(finishing, "finish_exported_clip", side_effect=finish), \
                patch.object(relisten, "release_model") as release:
            pipeline._finish(job)
        self.assertEqual(peak[0], 3)
        self.assertEqual(job.outputs, [str(path) for path in outputs])
        release.assert_called_once()

    def test_selection_leaves_captions_to_finishing(self):
        pipeline = auto_pipeline.AutoPipeline(steps={})
        requests = []
        for finish in (False, True):
            job = auto_pipeline.AutoJob(job_id=f"auto_cap_{finish}", source="x", agent="claude",
                                        finish=finish, video_id="vid_x", preload_transcript=False)
            with patch.object(pipeline, "_signals", return_value=[]), \
                    patch("moment_retrieval.used_ranges.for_video", return_value=[]), \
                    patch.object(agent_runner, "run_agent",
                                 side_effect=lambda agent, request, **_k: requests.append(request) or ""), \
                    patch.object(pipeline, "_ready_run_ids", return_value=[]), \
                    patch.object(pipeline, "_collect_runs", return_value="run_x"):
                pipeline._select(job)
        self.assertEqual([request.confirm_captions for request in requests], [True, False])


class KillProcessTreeTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "taskkill is Windows-only")
    def test_cancel_stops_grandchildren_such_as_ffmpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "grandchild.pid"
            # Parent starts a long-running grandchild (stands in for ffmpeg) and waits.
            parent_code = (
                "import subprocess, sys, time;"
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']);"
                f"open({str(pid_file)!r}, 'w').write(str(p.pid));"
                "time.sleep(120)"
            )
            parent = subprocess.Popen([sys.executable, "-c", parent_code])
            try:
                deadline = time.time() + 20
                while not pid_file.exists() or not pid_file.read_text():
                    self.assertLess(time.time(), deadline, "grandchild did not start")
                    time.sleep(0.1)
                grandchild = int(pid_file.read_text())
                auto_pipeline.kill_process_tree(parent)
                parent.wait(timeout=10)
                deadline = time.time() + 10
                while _pid_running(grandchild) and time.time() < deadline:
                    time.sleep(0.2)
                self.assertFalse(_pid_running(grandchild))
            finally:
                if parent.poll() is None:
                    parent.kill()


def _pid_running(pid: int) -> bool:
    listing = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True
    ).stdout
    return str(pid) in listing


class UploadPreflightTest(_Isolated):
    def test_a_rejected_file_is_logged_and_the_others_still_go_up(self):
        pipeline = auto_pipeline.AutoPipeline(steps={})
        job = auto_pipeline.AutoJob(job_id="auto_pre", source="x", agent="codex", outputs=["bad.mp4", "good.mp4"])

        def upload_private(path, **_):
            if path.name == "bad.mp4":
                raise youtube_upload.UploadRejected("アップロードしませんでした: bad.mp4（音声の時刻が乱れています）")
            yield "アップロード完了", {"video_id": "yt2", "title": "良い", "privacy_status": "private"}

        with patch.object(youtube_upload, "setup_problems", return_value=[]),                 patch.object(youtube_upload, "token_path", return_value=Path(__file__)),                 patch.object(youtube_upload, "upload_private", side_effect=upload_private),                 patch("moment_retrieval.used_ranges.record_from_clip"):
            pipeline._upload(job)
        self.assertEqual(["yt2"], [item["video_id"] for item in job.uploads])
        self.assertTrue(any("bad.mp4" in line and "乱れ" in line for line in job.log))


if __name__ == "__main__":
    unittest.main()
