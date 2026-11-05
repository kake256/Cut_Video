"""Clear guidance when Codex / Claude Code is installed but signed out (no real CLI is run)."""
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from moment_retrieval import agent_runner


class _Process:
    def __init__(self, output: str, code: int):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(output)
        self._code = code

    def wait(self, timeout=None):
        return self._code


class RunAgentLoginTest(unittest.TestCase):
    def test_expired_claude_login_is_reported_as_a_login_problem(self):
        process = _Process("Failed to authenticate: OAuth session expired and could not be refreshed\n", 1)
        with patch.object(agent_runner, "find_executable", return_value="claude"):
            with self.assertRaisesRegex(agent_runner.AgentError, "ログインが切れています"):
                agent_runner.run_agent("claude", agent_runner.ClipRequest("vid_x"), runner=lambda *a, **k: process)

    def test_other_failures_keep_the_generic_message(self):
        process = _Process("something else broke\n", 2)
        with patch.object(agent_runner, "find_executable", return_value="claude"):
            with self.assertRaisesRegex(agent_runner.AgentError, "異常終了"):
                agent_runner.run_agent("claude", agent_runner.ClipRequest("vid_x"), runner=lambda *a, **k: process)


class SubmitReadinessTest(unittest.TestCase):
    def _submit(self, agent):
        import auto_publish_app

        pipeline = SimpleNamespace(submit=Mock(return_value=SimpleNamespace(job_id="auto_x")))
        with patch.object(auto_publish_app, "_auto_pipeline", return_value=pipeline), \
                patch.object(auto_publish_app, "auto_jobs_view", return_value=("jobs", None, None)), \
                patch.object(auto_publish_app.gr, "Info"):
            auto_publish_app.auto_submit("url", "https://youtu.be/abcDEF12345", [], agent, "", 1, "",
                                         20, 60, "blur", False)
        return pipeline

    def test_signed_out_claude_is_stopped_before_a_job_is_queued(self):
        import auto_publish_app

        signed_out = {"installed": True, "ready": False, "detail": "未ログイン"}
        with patch.object(auto_publish_app.ai_setup, "claude_status", return_value=signed_out), \
                patch.object(auto_publish_app, "_auto_pipeline") as pipeline:
            with self.assertRaisesRegex(auto_publish_app.gr.Error, "ログインしていません"):
                self._submit("claude")
            pipeline.assert_not_called()

    def test_ready_claude_and_local_ai_are_queued(self):
        import auto_publish_app

        ready = {"installed": True, "ready": True, "detail": "ログイン済み"}
        with patch.object(auto_publish_app.ai_setup, "claude_status", return_value=ready):
            self.assertEqual(self._submit("claude").submit.call_count, 1)
        with patch.object(auto_publish_app.ai_setup, "claude_status", side_effect=AssertionError("not checked")):
            self.assertEqual(self._submit("local").submit.call_count, 1)


if __name__ == "__main__":
    unittest.main()
