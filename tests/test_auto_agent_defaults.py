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
