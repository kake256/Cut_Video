"""The auto-publish option that picks clips with one Claude API call (mocked; no real calls)."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from moment_retrieval import agent_runner, auto_pipeline, config, db, oneshot_selector


class _FakeClient:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = json.dumps({"clips": self.answers.pop(0)}, ensure_ascii=False)
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=30000, output_tokens=2000,
                                  cache_creation_input_tokens=0, cache_read_input_tokens=0),
        )


class ApiAgentTest(unittest.TestCase):
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

    def _video(self) -> str:
        path = self.root / "video" / "a.mp4"
        path.write_bytes(b"x")
        conn = db.get_conn()
        db.init_db(conn, create_backup=False)
        storage = "vid_" + "a" * 32
        db.insert_video(conn, storage, str(path), 300.0)
        for i in range(60):
            db.insert_segment(conn, storage, SimpleNamespace(start=i * 5.0, end=i * 5.0 + 5.0,
                                                             text=f"発話{i}", words=[]))
        db.mark_asr_complete(conn, storage)
        video_id = db.public_video_id(conn, storage)
        conn.close()
        return video_id

    def test_agent_is_offered_to_the_pipeline_but_not_to_the_cli(self):
        self.assertIn(agent_runner.API_AGENT, agent_runner.PIPELINE_AGENTS)
        self.assertNotIn(agent_runner.API_AGENT, agent_runner.AGENTS)
        self.assertEqual(agent_runner.AGENTS, ("codex", "claude", "local"))

    def test_submit_without_a_key_is_a_clear_japanese_error(self):
        pipeline = auto_pipeline.AutoPipeline(steps={})
        with patch("moment_retrieval.api_keys.resolve", return_value=("", "")), \
                self.assertRaises(auto_pipeline.PipelineError) as raised:
            pipeline.submit("https://youtu.be/abcDEF12345", agent_runner.API_AGENT)
        self.assertIn("APIキー", str(raised.exception))
        self.assertIn("設定", str(raised.exception))
        self.assertEqual(pipeline.jobs, {})

    def test_client_without_a_key_raises_the_same_message(self):
        with patch("moment_retrieval.api_keys.resolve", return_value=("", "")), \
                self.assertRaises(oneshot_selector.OneshotSelectionError) as raised:
            oneshot_selector._client()
        self.assertEqual(str(raised.exception), oneshot_selector.NO_KEY_MESSAGE)

    def test_select_dispatches_to_the_api_and_saves_proposals(self):
        video_id = self._video()
        client = _FakeClient([[
            {"start": "0:00:10", "end": "0:00:50", "title": "最初の山場", "reason": "盛り上がる"},
            {"start": "0:02:00", "end": "0:02:40", "title": "二つ目", "reason": "オチがある"},
        ]])
        pipeline = auto_pipeline.AutoPipeline(steps={})
        job = auto_pipeline.AutoJob(job_id="auto_api", source="x", agent=agent_runner.API_AGENT,
                                    clip_count=2, video_id=video_id, focus="")
        pipeline.jobs[job.job_id] = job
        with patch.object(pipeline, "_signals", return_value=[]), \
                patch("moment_retrieval.channel_performance.prompt_section", return_value="過去の傾向\n"), \
                patch.object(oneshot_selector, "_client", return_value=client), \
                patch.object(agent_runner, "run_agent", side_effect=AssertionError("CLI must not run")):
            run_id = pipeline._select(job)
        self.assertEqual(len(client.calls), 1)
        prompt = client.calls[0]["messages"][0]["content"]
        self.assertIn("2件選んでください", prompt)
        self.assertIn("過去の傾向", prompt)
        conn = db.get_conn()
        try:
            run = db.get_highlight_run(conn, run_id)
            titles = [c["title"] for c in db.get_highlight_candidates(conn, run_id)]
        finally:
            conn.close()
        self.assertEqual(run["status"], "ready")
        self.assertEqual(titles, ["最初の山場", "二つ目"])
        cost = oneshot_selector.cost_usd({"input_tokens": 30000, "output_tokens": 2000})
        self.assertTrue(any("API料金" in line and f"{cost:.3f}" in line for line in job.log))

    def test_used_ranges_and_focus_reach_the_api(self):
        video_id = self._video()
        client = _FakeClient([[{"start": "0:03:00", "end": "0:03:40", "title": "キレる", "reason": "r"}]])
        pipeline = auto_pipeline.AutoPipeline(steps={})
        job = auto_pipeline.AutoJob(job_id="auto_api_f", source="x", agent=agent_runner.API_AGENT,
                                    clip_count=3, video_id=video_id, focus="キレている場面")
        used = [{"start_sec": 0.0, "end_sec": 60.0, "title": "投稿済み"}]
        with patch.object(pipeline, "_signals", return_value=[]), \
                patch("moment_retrieval.used_ranges.for_video", return_value=used), \
                patch.object(oneshot_selector, "_client", return_value=client):
            pipeline._select(job)
        prompt = client.calls[0]["messages"][0]["content"]
        self.assertIn("キレている場面", prompt)
        self.assertIn("最大3件", prompt)
        self.assertIn("「投稿済み」", prompt)

    def test_no_usable_clip_fails_the_job_without_saving(self):
        video_id = self._video()
        bad = [{"start": "0:00:00", "end": "0:04:30", "title": "長すぎ", "reason": "r"}]
        client = _FakeClient([bad, bad])
        pipeline = auto_pipeline.AutoPipeline(steps={})
        job = auto_pipeline.AutoJob(job_id="auto_api_bad", source="x", agent=agent_runner.API_AGENT,
                                    clip_count=1, video_id=video_id)
        with patch.object(pipeline, "_signals", return_value=[]), \
                patch.object(oneshot_selector, "_client", return_value=client), \
                self.assertRaises(auto_pipeline.PipelineError):
            pipeline._select(job)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(pipeline._ready_run_ids(video_id), [])

    def test_finishing_plan_uses_the_api_for_api_jobs(self):
        from moment_retrieval import finishing

        response = SimpleNamespace(content=[SimpleNamespace(type="text", text='{"hook_text": "引き"}')])
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **_k: response))
        with patch.object(oneshot_selector, "_client", return_value=client), \
                patch.object(agent_runner, "find_executable", side_effect=AssertionError("no CLI")):
            plan = finishing.request_plan(agent_runner.API_AGENT, "", "prompt")
        self.assertEqual(plan, {"hook_text": "引き"})


if __name__ == "__main__":
    unittest.main()
