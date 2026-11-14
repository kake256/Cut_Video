import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class AgentDefaultsTest(unittest.TestCase):
    def setUp(self):
        import auto_publish_app
        from moment_retrieval import config

        self.app = auto_publish_app
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = patch.object(config, "LIBRARY_ROOT", Path(self.tmp.name))
        root.start()
        self.addCleanup(root.stop)
        self.path = Path(self.tmp.name) / "auto_publish_ui.json"

    def test_default_is_codex_with_its_model(self):
        agent, model, effort = self.app.apply_agent_settings()
        self.assertEqual(agent["value"], "codex")
        self.assertEqual(model["value"], self.app.agent_runner.DEFAULT_CODEX_MODEL)
        self.assertTrue(effort["visible"])

    def test_last_choice_is_restored(self):
        self.app.remember_agent("claude", "sonnet")
        agent, model, effort = self.app.apply_agent_settings()
        self.assertEqual((agent["value"], model["value"]), ("claude", "sonnet"))
        self.assertFalse(effort["visible"])

    def test_saving_length_settings_keeps_the_ai_and_vice_versa(self):
        self.app.remember_agent("claude", "opus")
        self.app.save_ui_settings("max", 15, 45, "crop", False, "unlisted")
        self.assertEqual(self.app._saved_agent(), ("claude", "opus"))
        self.app.remember_agent("codex", "gpt-6-luna")
        self.assertEqual(self.app.apply_ui_settings(), ("max", 15.0, 45.0, "crop", False, "unlisted"))

    def test_bad_saved_values_fall_back(self):
        self.path.write_text(json.dumps({"agent": "evil", "model": "x"}), encoding="utf-8")
        self.assertEqual(self.app._saved_agent(), ("codex", self.app.agent_runner.DEFAULT_CODEX_MODEL))
        self.path.write_text(json.dumps({"agent": "claude", "model": "bad model; rm"}), encoding="utf-8")
        self.assertEqual(self.app._saved_agent(), ("claude", ""))
        self.app.remember_agent("claude", "bad model; rm")  # ignored
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["model"], "bad model; rm")

    def test_removed_local_model_stays_selected(self):
        self.app.remember_agent("local", "gone:7b")
        with patch.object(self.app.ai_setup, "ollama_models", return_value=["qwen3:8b"]):
            _agent, model, _effort = self.app.apply_agent_settings()
        self.assertEqual(model["value"], "gone:7b")
        self.assertIn(("gone:7b", "gone:7b"), model["choices"])


if __name__ == "__main__":
    unittest.main()


class ApiAgentChoiceTest(unittest.TestCase):
    def setUp(self):
        import auto_publish_app
        from moment_retrieval import config

        self.app = auto_publish_app
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = patch.object(config, "LIBRARY_ROOT", Path(self.tmp.name))
        root.start()
        self.addCleanup(root.stop)

    def test_api_choice_is_remembered_with_its_fixed_model(self):
        api = self.app.agent_runner.API_AGENT
        self.app.remember_agent(api, "")
        agent, model, effort = self.app.apply_agent_settings()
        self.assertEqual((agent["value"], model["value"]), (api, ""))
        self.assertFalse(effort["visible"])

    def test_api_choice_without_a_key_stops_before_queueing(self):
        import gradio as gr

        with patch("moment_retrieval.api_keys.resolve", return_value=("", "")), \
                self.assertRaises(gr.Error) as raised:
            self.app._require_agent_ready(self.app.agent_runner.API_AGENT)
        self.assertIn("APIキー", str(raised.exception))
        with patch("moment_retrieval.api_keys.resolve", return_value=("x" * 30, "saved")):
            self.app._require_agent_ready(self.app.agent_runner.API_AGENT)


class JsDrawSettingTest(unittest.TestCase):
    def setUp(self):
        import auto_publish_app
        from moment_retrieval import config

        self.app = auto_publish_app
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = patch.object(config, "LIBRARY_ROOT", Path(self.tmp.name))
        root.start()
        self.addCleanup(root.stop)

    def test_default_is_off_with_fable_hidden(self):
        box, model, _note = self.app.apply_jsdraw_settings()
        self.assertFalse(box["value"])
        self.assertEqual(model["value"], "fable")
        self.assertFalse(model["visible"])

    def test_choice_is_remembered_and_keeps_other_settings(self):
        self.app.remember_agent("claude", "opus")
        self.app.remember_jsdraw(True, "opus")
        self.assertEqual(self.app.apply_jsdraw_settings()[1]["value"], "opus")
        self.app.remember_jsdraw(True, "sonnet")
        box, model, note = self.app.apply_jsdraw_settings()
        self.assertTrue(box["value"])
        self.assertEqual(model["value"], "sonnet")
        self.assertTrue(model["visible"])
        self.assertIn("Claude Codeのログイン枠", note["value"])
        self.assertEqual(self.app._saved_agent(), ("claude", "opus"))
        self.app.remember_jsdraw(False, "bogus")
        box, model, _note = self.app.apply_jsdraw_settings()
        self.assertFalse(box["value"])
        self.assertEqual(model["value"], "fable")

    def test_model_choice_shows_only_when_checked(self):
        self.assertTrue(self.app.on_jsdraw_change(True, "claude")[0]["visible"])
        self.assertFalse(self.app.on_jsdraw_change(False, "claude")[0]["visible"])
        radio, note = self.app.on_jsdraw_change(True, "codex")  # Codex draws with its own model
        self.assertFalse(radio["visible"])
        self.assertIn("Codexのログイン枠", note["value"])

    def test_note_follows_the_ai_and_local_without_key_is_unavailable(self):
        with patch("moment_retrieval.api_keys.resolve", return_value=("", "")):
            box, radio, note = self.app.jsdraw_controls("local", True)
            self.assertFalse(box["interactive"])
            self.assertFalse(box["value"])
            self.assertTrue(note["visible"])
            self.assertIn("APIキーが必要", note["value"])
            box, _radio, note = self.app.jsdraw_controls(self.app.agent_runner.API_AGENT, True)
            self.assertFalse(box["interactive"])
            box, _radio, note = self.app.jsdraw_controls("claude", True)
            self.assertTrue(box["interactive"] and box["value"])
        with patch("moment_retrieval.api_keys.resolve", return_value=("k" * 30, "saved")):
            _box, _radio, note = self.app.jsdraw_controls(self.app.agent_runner.API_AGENT, True)
            self.assertIn("Opus 約0.25", note["value"])

    def test_checked_box_without_a_key_stops_before_queueing(self):
        import gradio as gr

        with patch("moment_retrieval.api_keys.resolve", return_value=("", "")):
            self.app._require_jsdraw_ready(False)
            self.app._require_jsdraw_ready(True, "claude")  # login CLI: no key needed
            with self.assertRaises(gr.Error) as raised:
                self.app._require_jsdraw_ready(True, "local")
        self.assertIn("APIキー", str(raised.exception))


class ClaudeKeyOptionTest(unittest.TestCase):
    def setUp(self):
        import auto_publish_app
        from moment_retrieval import config

        self.app = auto_publish_app
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = patch.object(config, "DATA_DIR", Path(self.tmp.name))
        data.start()
        self.addCleanup(data.stop)

    def test_checkbox_is_off_by_default_and_persists(self):
        api_keys = self.app.api_keys
        self.assertFalse(api_keys.claude_cli_uses_key())
        with patch.object(self.app.gr, "Info"), patch.object(self.app, "ai_setup_view", return_value=""):
            self.app.save_claude_uses_key(True)
            self.assertTrue(api_keys.claude_cli_uses_key())
            self.app.save_claude_uses_key(False)
        self.assertFalse(api_keys.claude_cli_uses_key())
