import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


KEY_A = "sk-ant-api03-" + "a" * 40 + "WXYZ"
KEY_O = "sk-proj-" + "b" * 40 + "QRST"
ENV_NAMES = ("ANTHROPIC_API_KEY", "CODEX_API_KEY", "OPENAI_API_KEY")


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ApiKeysTest(unittest.TestCase):
    def setUp(self):
        # Imported here, not at module level: config fixes DATA_DIR on first import, and other
        # test modules set CUT_VIDEO_DATA_DIR before importing it.
        global agent_runner, ai_setup, api_keys, config
        from moment_retrieval import agent_runner, ai_setup, api_keys, config

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data_dir = patch.object(config, "DATA_DIR", Path(self.tmp.name))
        data_dir.start()
        self.addCleanup(data_dir.stop)
        clean = {k: v for k, v in os.environ.items() if k not in ENV_NAMES}
        env = patch.dict(os.environ, clean, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_save_resolve_delete(self):
        self.assertEqual(api_keys.resolve("anthropic"), ("", ""))
        api_keys.save_key("anthropic", f"  {KEY_A}\n")
        self.assertEqual(api_keys.resolve("anthropic"), (KEY_A, "saved"))
        stored = json.loads((Path(self.tmp.name) / "api_keys.json").read_text(encoding="utf-8"))
        self.assertEqual(stored, {"anthropic": KEY_A})
        self.assertIn("保存済み", api_keys.describe("anthropic"))
        self.assertNotIn(KEY_A, api_keys.describe("anthropic"))
        self.assertTrue(api_keys.delete_key("anthropic"))
        self.assertFalse(api_keys.delete_key("anthropic"))
        self.assertEqual(api_keys.resolve("anthropic"), ("", ""))

    def test_rejects_malformed_key(self):
        for bad in ("", "short", "sk-ant has spaces in it 1234567890"):
            with self.assertRaises(api_keys.ApiKeyError):
                api_keys.save_key("anthropic", bad)
        self.assertFalse((Path(self.tmp.name) / "api_keys.json").exists())

    def test_saved_key_wins_over_environment(self):
        os.environ["OPENAI_API_KEY"] = "sk-env-" + "c" * 30
        self.assertEqual(api_keys.resolve("openai")[1], "OPENAI_API_KEY")
        api_keys.save_key("openai", KEY_O)
        self.assertEqual(api_keys.resolve("openai"), (KEY_O, "saved"))

    def test_agent_env_injects_only_for_that_agent(self):
        self.assertNotIn("ANTHROPIC_API_KEY", api_keys.agent_env("claude"))
        api_keys.save_key("anthropic", KEY_A)
        api_keys.save_key("openai", KEY_O)
        api_keys.set_claude_cli_uses_key(True)
        claude = api_keys.agent_env("claude", base={})
        codex = api_keys.agent_env("codex", base={})
        self.assertEqual(claude, {"ANTHROPIC_API_KEY": KEY_A})
        self.assertEqual(codex, {"CODEX_API_KEY": KEY_O, "OPENAI_API_KEY": KEY_O})
        self.assertEqual(api_keys.agent_env("local", base={}), {})

    def test_mcp_server_env_never_carries_the_key(self):
        api_keys.save_key("anthropic", KEY_A)
        os.environ["ANTHROPIC_API_KEY"] = KEY_A
        self.assertNotIn(KEY_A, json.dumps(agent_runner._mcp_server()))

    def test_claude_code_uses_its_login_unless_opted_in(self):
        api_keys.save_key("anthropic", KEY_A)
        api_keys.save_key("openai", KEY_O)
        self.assertFalse(api_keys.claude_cli_uses_key())  # off by default
        inherited = {"ANTHROPIC_API_KEY": "sk-env-" + "e" * 30, "ANTHROPIC_AUTH_TOKEN": "t" * 30, "PATH": "x"}
        self.assertEqual(api_keys.agent_env("claude", base=inherited), {"PATH": "x"})
        self.assertFalse(api_keys.has_key("claude"))
        # Codex is unchanged, and the app's own API features still find the key.
        self.assertEqual(api_keys.agent_env("codex", base={})["CODEX_API_KEY"], KEY_O)
        self.assertEqual(api_keys.resolve("anthropic"), (KEY_A, "saved"))
        api_keys.set_claude_cli_uses_key(True)
        self.assertTrue(api_keys.claude_cli_uses_key())
        self.assertEqual(api_keys.agent_env("claude", base={"PATH": "x"})["ANTHROPIC_API_KEY"], KEY_A)
        self.assertTrue(api_keys.has_key("claude"))
        api_keys.set_claude_cli_uses_key(False)
        self.assertNotIn("ANTHROPIC_API_KEY", api_keys.agent_env("claude", base={}))
        self.assertNotIn(KEY_A, (Path(self.tmp.name) / api_keys.OPTIONS_FILE).read_text(encoding="utf-8"))

    def test_run_agent_keeps_the_key_from_the_cli_by_default(self):
        api_keys.save_key("anthropic", KEY_A)
        seen = {}
        process = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO("ok\n"), wait=lambda timeout=None: 0)

        def runner(command, **kwargs):
            seen.update(kwargs)
            return process

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": KEY_A}), \
                patch.object(agent_runner, "find_executable", return_value="claude"):
            agent_runner.run_agent("claude", agent_runner.ClipRequest("vid_x"), runner=runner)
        self.assertNotIn("ANTHROPIC_API_KEY", seen["env"])

    def test_finishing_plan_keeps_the_key_from_the_cli_by_default(self):
        from moment_retrieval import finishing

        api_keys.save_key("anthropic", KEY_A)
        seen = {}

        def run(command, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(stdout='{"hook_text": "x"}')

        with patch.object(agent_runner, "find_executable", return_value="claude"), \
                patch.object(finishing.subprocess, "run", side_effect=run):
            finishing.request_plan("claude", "", "prompt")
        self.assertNotIn("ANTHROPIC_API_KEY", seen["env"])

    def test_run_agent_passes_key_to_cli(self):
        api_keys.save_key("anthropic", KEY_A)
        api_keys.set_claude_cli_uses_key(True)
        seen = {}
        process = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO("ok\n"), wait=lambda timeout=None: 0)

        def runner(command, **kwargs):
            seen.update(kwargs)
            return process

        with patch.object(agent_runner, "find_executable", return_value="claude"):
            agent_runner.run_agent("claude", agent_runner.ClipRequest("vid_x"), runner=runner)
        self.assertEqual(seen["env"]["ANTHROPIC_API_KEY"], KEY_A)

    def test_status_is_ready_with_key_even_when_signed_out(self):
        api_keys.save_key("anthropic", KEY_A)
        api_keys.set_claude_cli_uses_key(True)
        with patch.object(agent_runner, "find_executable", return_value="claude"):
            status = ai_setup.claude_status(runner=lambda *a, **k: SimpleNamespace(stdout='{"loggedIn": false}'))
        self.assertTrue(status["ready"])
        self.assertEqual(status["detail"], "APIキーで利用")

    def test_connection_test(self):
        requests = []

        def ok(request, timeout):
            requests.append(request)
            return _Response()

        good, message = api_keys.test_key("anthropic", KEY_A, opener=ok)
        self.assertTrue(good)
        self.assertNotIn(KEY_A, message)
        self.assertEqual(requests[0].get_header("X-api-key"), KEY_A)

        def unauthorized(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

        good, message = api_keys.test_key("openai", KEY_O, opener=unauthorized)
        self.assertFalse(good)
        self.assertIn("401", message)

        good, message = api_keys.test_key("openai", "", opener=ok)
        self.assertFalse(good)
        self.assertIn("ありません", message)


if __name__ == "__main__":
    unittest.main()
