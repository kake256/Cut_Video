"""Static contract tests for the optional Windows Ollama bootstrap.

The setup path must remain opt-in: these tests intentionally do not download
Ollama or a model.
"""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class OllamaSetupScriptTests(unittest.TestCase):
    def setUp(self):
        self.start_bat = (ROOT / "start.bat").read_text(encoding="utf-8")
        self.setup_bat = (ROOT / "setup_ollama.bat").read_text(encoding="utf-8")
        self.setup_ps1 = (ROOT / "scripts" / "setup_ollama.ps1").read_text(
            encoding="utf-8"
        )

    def test_normal_startup_does_not_run_the_ollama_setup(self):
        self.assertIn('"--setup-ollama"', self.start_bat)
        self.assertIn("setup_ollama.bat", self.start_bat)
        self.assertIn("Optional only", self.start_bat)

    def test_setup_entrypoint_is_opt_in_and_passes_arguments(self):
        self.assertIn("scripts\\setup_ollama.ps1", self.setup_bat)
        self.assertIn("%*", self.setup_bat)

    def test_bootstrap_uses_only_official_download_and_loopback_api(self):
        self.assertIn("https://ollama.com/download/OllamaSetup.exe", self.setup_ps1)
        self.assertIn("http://127.0.0.1:11434/api/tags", self.setup_ps1)
        self.assertIn("Get-AuthenticodeSignature", self.setup_ps1)

    def test_bootstrap_keeps_a_new_managed_install_outside_the_repository(self):
        self.assertIn('Split-Path -Parent $repoRoot', self.setup_ps1)
        self.assertIn('Join-Path $workspaceRoot "dependencies"', self.setup_ps1)
        self.assertIn('SetEnvironmentVariable("OLLAMA_MODELS", $managedModelsDir, "User")', self.setup_ps1)
        self.assertIn('"/DIR=$managedInstallDir"', self.setup_ps1)
        self.assertIn('"/VERYSILENT"', self.setup_ps1)
        self.assertIn('"/SUPPRESSMSGBOXES"', self.setup_ps1)
        self.assertIn('"/NORESTART"', self.setup_ps1)
        self.assertIn("$installer.WaitForExit()", self.setup_ps1)

    def test_model_pull_is_skippable_and_existing_model_is_reused(self):
        self.assertIn("[switch]$SkipModelPull", self.setup_ps1)
        self.assertIn("& $ollamaExe show $Model", self.setup_ps1)
        self.assertIn("& $ollamaExe pull $Model", self.setup_ps1)


if __name__ == "__main__":
    unittest.main()
