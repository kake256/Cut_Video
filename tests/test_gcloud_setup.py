"""gcloud-assisted Google Cloud setup with a fake gcloud (no account changes)."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from moment_retrieval import config, gcloud_setup


class _FakeGcloud:
    def __init__(self, *, account="me@example.com", fail_on=None, stderr=""):
        self.calls = []
        self.account = account
        self.fail_on = fail_on
        self.stderr = stderr

    def __call__(self, command, **kwargs):
        args = command[1:]
        self.calls.append(args)
        if self.fail_on and args[:2] == self.fail_on:
            return SimpleNamespace(returncode=1, stdout="", stderr=self.stderr)
        if args[:2] == ["auth", "list"]:
            accounts = [{"account": self.account, "status": "ACTIVE"}] if self.account else []
            return SimpleNamespace(returncode=0, stdout=json.dumps(accounts), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class GcloudSetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(config, "LIBRARY_ROOT", Path(self.tmp.name)),
            patch.object(gcloud_setup, "find_gcloud", return_value="gcloud"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_creates_project_once_and_always_enables_youtube_api(self):
        fake = _FakeGcloud()
        first = gcloud_setup.create_project_and_enable_api(runner=fake)
        self.assertTrue(first["created"])
        self.assertRegex(first["project_id"], r"^cut-youtube-[0-9a-f]{6}$")
        self.assertIn(["projects", "create", first["project_id"], "--name=CUT-YouTube", "--quiet"], fake.calls)
        self.assertIn(["services", "enable", "youtube.googleapis.com",
                       f"--project={first['project_id']}", "--quiet"], fake.calls)
        second = gcloud_setup.create_project_and_enable_api(runner=fake)
        self.assertFalse(second["created"])
        self.assertEqual(second["project_id"], first["project_id"])
        self.assertEqual(sum(1 for call in fake.calls if call[:2] == ["projects", "create"]), 1)

    def test_requires_login_and_explains_terms_of_service(self):
        with self.assertRaises(gcloud_setup.SetupError):
            gcloud_setup.create_project_and_enable_api(runner=_FakeGcloud(account=None))
        fake = _FakeGcloud(fail_on=["projects", "create"], stderr="ERROR: Callers must accept Terms of Service")
        with self.assertRaises(gcloud_setup.SetupError) as ctx:
            gcloud_setup.create_project_and_enable_api(runner=fake)
        self.assertIn("利用規約", str(ctx.exception))
        self.assertIsNone(gcloud_setup.saved_project())

    def test_console_pages_target_the_saved_project(self):
        opened = []
        pages = gcloud_setup.open_console_pages("cut-youtube-abc123", opener=opened.append)
        self.assertEqual(len(pages), 3)
        self.assertTrue(all("project=cut-youtube-abc123" in url for url in opened))
        self.assertIn("clients/create", opened[-1])

    def test_copy_code_login_opens_link_and_sends_the_code(self):
        import io

        class _Process:
            def __init__(self):
                self.stdout = io.StringIO(
                    "Go to the following link in your browser:\n\n"
                    "    https://accounts.google.com/o/oauth2/auth?a=1&b=2\n\n"
                )
                self.stdin = io.StringIO()
                self.sent = ""
                self.returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.sent = self.stdin.getvalue()
                self.returncode = 0
                return 0

            def kill(self):
                self.returncode = -9

        process = _Process()
        process.stdin.close = lambda: None
        opened = []
        url = gcloud_setup.start_login(spawn=lambda *a, **k: process, opener=opened.append, wait_sec=2)
        self.assertEqual(url, "https://accounts.google.com/o/oauth2/auth?a=1&b=2")
        self.assertEqual(opened, [url])
        with self.assertRaises(gcloud_setup.SetupError):
            gcloud_setup.finish_login("has space")
        account = gcloud_setup.finish_login("4/abcCODE", runner=_FakeGcloud(account="you@example.com"))
        self.assertEqual(process.sent, "4/abcCODE\n")
        self.assertEqual(account, "you@example.com")
        with self.assertRaises(gcloud_setup.SetupError):
            gcloud_setup.finish_login("4/again")

    def test_missing_gcloud_is_reported(self):
        with patch.object(gcloud_setup, "find_gcloud", return_value=None):
            with self.assertRaises(gcloud_setup.SetupError):
                gcloud_setup.active_account()


if __name__ == "__main__":
    unittest.main()
