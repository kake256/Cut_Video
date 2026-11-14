"""Claude Code / Codex get the whole condensed transcript in the first prompt (via stdin)."""
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from moment_retrieval import agent_runner, auto_pipeline, config, db, oneshot_selector


def _segments(count=6, step=5.0, first_id=100):
    return [{"segment_id": first_id + i, "start_sec": i * step, "end_sec": i * step + step, "text": f"発話{i}"}
            for i in range(count)]


class _Process:
    """Popen stand-in that records what was written to stdin."""

    def __init__(self):
        self.stdin = io.StringIO()
        self.stdin.close = lambda: None  # keep the text readable after run_agent closes it
        self.stdout = io.StringIO("保存しました\n")

    def wait(self, timeout=None):
        return 0


class PromptTest(unittest.TestCase):
    def test_id_transcript_lines_carry_the_segment_ids_of_each_block(self):
        text = oneshot_selector.id_transcript_text(_segments())
        lines = text.splitlines()
        self.assertEqual(lines[0], "[0:00:00] #100-101 発話0 発話1")
        self.assertEqual(lines[1], "[0:00:10] #102-103 発話2 発話3")
        single = oneshot_selector.id_transcript_text(
            [{"segment_id": 7, "start_sec": 0.0, "end_sec": 30.0, "text": "長い"}])
        self.assertEqual(single, "[0:00:00] #7 長い")

    def test_preloaded_prompt_has_the_transcript_and_says_not_to_page(self):
        used = ({"start_sec": 60.0, "end_sec": 120.0, "title": "済"},)
        prompt = agent_runner.ClipRequest(
            "vid_x", 3, used_ranges=used, hints="盛り上がり: 0:10:00\n",
            transcript="[0:00:00] #1-2 こんにちは", transcript_revision="rev_9", confirm_captions=False,
        ).prompt()
        self.assertIn("<transcript>\n[0:00:00] #1-2 こんにちは\n</transcript>", prompt)
        self.assertIn("cut_read_transcript でページをめくって読み直さない", prompt)
        self.assertIn("cut_search_transcript", prompt)
        self.assertIn("cut_propose_clips", prompt)
        self.assertIn("transcript_revision=rev_9", prompt)
        self.assertIn("「済」", prompt)
        self.assertIn("盛り上がり: 0:10:00", prompt)
        self.assertIn("3件選ぶ", prompt)
        self.assertNotIn("cut_relisten_range", prompt)

    def test_without_a_transcript_the_old_paging_prompt_is_kept(self):
        prompt = agent_runner.ClipRequest("vid_x", 3).prompt()
        self.assertIn("全ページ読む", prompt)
        self.assertNotIn("<transcript>", prompt)

    def test_long_prompt_goes_through_stdin_not_the_command_line(self):
        transcript = "\n".join(f"[0:{i // 60:02d}:{i % 60:02d}] #{i} " + "あ" * 80 for i in range(600))
        request = agent_runner.ClipRequest("vid_x", transcript=transcript, transcript_revision="r")
        self.assertGreater(len(request.prompt()), 40_000)  # beyond Windows' ~32k command-line limit
        seen = {}
        process = _Process()

        def runner(command, **kwargs):
            seen["command"], seen["kwargs"] = command, kwargs
            return process

        for agent in ("claude", "codex"):
            with self.subTest(agent=agent), patch.object(agent_runner, "find_executable", return_value=agent):
                process.stdin = io.StringIO()
                process.stdin.close = lambda: None
                process.stdout = io.StringIO("ok\n")
                agent_runner.run_agent(agent, request, runner=runner)
                self.assertEqual(process.stdin.getvalue(), request.prompt())
                self.assertLess(len(" ".join(seen["command"])), 8000)
                self.assertNotIn("あああ", " ".join(seen["command"]))


class PipelineTest(unittest.TestCase):
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
        db.insert_video(conn, storage, str(path), 60.0)
        for i in range(12):
            db.insert_segment(conn, storage, SimpleNamespace(start=i * 5.0, end=i * 5.0 + 5.0,
                                                             text=f"発話{i}", words=[]))
        db.mark_asr_complete(conn, storage)
        video_id = db.public_video_id(conn, storage)
        conn.close()
        return video_id

    def _select(self, job):
        requests = []
        pipeline = auto_pipeline.AutoPipeline(steps={})
        with patch.object(pipeline, "_signals", return_value=[]), \
                patch("moment_retrieval.channel_performance.prompt_section", return_value=""), \
                patch.object(agent_runner, "run_agent",
                             side_effect=lambda agent, request, **_k: requests.append(request) or ""), \
                patch.object(pipeline, "_collect_runs", return_value="run_x"):
            pipeline._select(job)
        return requests[0], job

    def test_setting_on_puts_the_ids_and_revision_into_the_request(self):
        video_id = self._video()
        request, job = self._select(auto_pipeline.AutoJob(job_id="auto_pre", source="x", agent="claude",
                                                          video_id=video_id))
        conn = db.get_conn()
        try:
            revision = db.get_active_transcript_revision(conn, video_id)
            ids = [int(row["segment_id"]) for row in db.get_segments(conn, video_id, transcript_revision=revision)]
        finally:
            conn.close()
        self.assertEqual(request.transcript_revision, revision)
        self.assertTrue(request.transcript.startswith(f"[0:00:00] #{ids[0]}-{ids[1]} 発話0 発話1"))
        self.assertTrue(any("最初に渡します" in line for line in job.log))

    def test_setting_off_keeps_the_paging_request(self):
        video_id = self._video()
        request, _job = self._select(auto_pipeline.AutoJob(job_id="auto_nopre", source="x", agent="claude",
                                                           video_id=video_id, preload_transcript=False))
        self.assertEqual(request.transcript, "")
        self.assertIn("全ページ読む", request.prompt())

    def test_setting_defaults_on_and_is_kept_by_submit(self):
        self.assertTrue(auto_pipeline.AutoJob(job_id="j", source="x", agent="claude").preload_transcript)
        pipeline = auto_pipeline.AutoPipeline(steps={})
        with patch.object(pipeline, "_ensure_worker"), patch.object(pipeline, "_save"):
            job = pipeline.submit("https://youtu.be/abcDEF12345", "claude", preload_transcript=False)
        self.assertFalse(job.preload_transcript)
        # jobs saved before this setting existed lack the field: the default applies
        restored = auto_pipeline.AutoJob(**{"job_id": "old", "source": "x", "agent": "codex"})
        self.assertTrue(restored.preload_transcript)


if __name__ == "__main__":
    unittest.main()
