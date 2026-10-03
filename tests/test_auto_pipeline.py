"""Auto clip -> private upload -> one-click publish, with every external step faked."""
import json
import tempfile
import threading
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


if __name__ == "__main__":
    unittest.main()
