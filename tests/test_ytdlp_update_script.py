"""Keep the opt-in yt-dlp update path aligned with the dependency contract."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class YtDlpUpdateScriptTests(unittest.TestCase):
    def setUp(self):
        self.requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.updater = (ROOT / "update_ytdlp.bat").read_text(encoding="utf-8")
        self.start_bat = (ROOT / "start.bat").read_text(encoding="utf-8")

    def test_requirement_uses_current_default_extra(self):
        self.assertIn("yt-dlp[default]>=2026.8.19", self.requirements)

    def test_updater_uses_venv_and_propagates_failures(self):
        self.assertIn('venv\\Scripts\\python.exe" -m pip install --upgrade', self.updater)
        self.assertIn('"yt-dlp[default]>=2026.8.19"', self.updater)
        self.assertIn('set "UPDATE_EXIT=%ERRORLEVEL%"', self.updater)
        self.assertIn('exit /b %UPDATE_EXIT%', self.updater)
        self.assertIn("yt_dlp.version.__version__", self.updater)

    def test_normal_start_does_not_update_ytdlp(self):
        self.assertNotIn("update_ytdlp", self.start_bat.lower())
        self.assertNotIn("yt-dlp", self.start_bat.lower())


if __name__ == "__main__":
    unittest.main()
