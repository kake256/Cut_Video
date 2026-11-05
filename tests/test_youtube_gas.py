"""GAS version of the comment auto-reply, with a fake Apps Script API (no network, no OAuth)."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moment_retrieval import config, youtube_auto_reply as reply, youtube_gas as gas

ROOT = Path(__file__).resolve().parents[1]


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class _ScriptService:
    """Mimics service.projects() / .versions() / .deployments() of the Apps Script API."""

    def __init__(self, *, fail=None):
        self.calls = []
        self.files = None
        self.fail = fail or {}

    def projects(self):
        return self

    def versions(self):
        return _Sub(self, "versions")

    def deployments(self):
        return _Sub(self, "deployments")

    def _answer(self, name, value):
        self.calls.append(name)
        return _Exec(self.fail.get(name, value))

    def create(self, body):
        return self._answer("create", {"scriptId": "script123"})

    def get(self, scriptId):
        return self._answer("get", {"scriptId": scriptId, "creator": {"email": "owner@example.com"}})

    def updateContent(self, scriptId, body):
        self.files = {item["name"]: item for item in body["files"]}
        return self._answer("updateContent", {})


class _Sub:
    def __init__(self, parent, kind):
        self.parent = parent
        self.kind = kind

    def create(self, scriptId, body):
        if self.kind == "versions":
            count = sum(1 for c in self.parent.calls if c == "versions.create") + 1
            return self.parent._answer("versions.create", {"versionNumber": count})
        return self.parent._answer("deployments.create", {
            "deploymentId": "dep1",
            "entryPoints": [{"entryPointType": "WEB_APP", "webApp": {"url": "https://script.google.com/macros/s/dep1/exec"}}],
        })

    def update(self, scriptId, deploymentId, body):
        self.parent.updated_version = body["deploymentConfig"]["versionNumber"]
        return self.parent._answer("deployments.update", {
            "deploymentId": deploymentId,
            "entryPoints": [{"webApp": {"url": "https://script.google.com/macros/s/dep1/exec"}}],
        })


class GasDeployTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(config, "LIBRARY_ROOT", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        reply.token_path().write_text(json.dumps({
            "token": "access", "refresh_token": "brand-refresh", "client_id": "cid.apps.googleusercontent.com",
            "client_secret": "csecret", "token_uri": "https://oauth2.googleapis.com/token",
        }), encoding="utf-8")

    def settings(self, **kwargs):
        return reply.save_settings(kwargs.pop("enabled", True), kwargs.pop("templates", ["ありがとう！\n2行目"]),
                                   3, 30, **kwargs)

    def test_first_deploy_creates_project_writes_files_and_web_app(self):
        service = _ScriptService()
        state = gas.deploy(self.settings(run_on="gas"), service_factory=lambda: service)
        self.assertEqual(service.calls,
                         ["create", "get", "updateContent", "versions.create", "deployments.create"])
        self.assertEqual(state["script_id"], "script123")
        self.assertEqual(state["web_url"], "https://script.google.com/macros/s/dep1/exec")
        self.assertEqual(state["editor_url"], "https://script.google.com/d/script123/edit")
        # Opened as the owner, so a browser signed in to several accounts still works.
        self.assertEqual(state["owner_email"], "owner@example.com")
        self.assertEqual(state["setup_url"],
                         "https://script.google.com/d/script123/edit?authuser=owner%40example.com")
        self.assertEqual(gas.load_state()["deployment_id"], "dep1")
        manifest = json.loads(service.files["appsscript"]["source"])
        # YouTube is called with the channel's own login, so GAS needs only outbound requests.
        self.assertIn("https://www.googleapis.com/auth/script.external_request", manifest["oauthScopes"])
        self.assertNotIn("dependencies", manifest)
        self.assertEqual(manifest["webapp"]["access"], "MYSELF")
        secret = service.files["Secret"]["source"]
        creds = json.loads(secret[secret.index("{"):secret.rindex("}") + 1])
        self.assertEqual(creds, {"client_id": "cid.apps.googleusercontent.com", "client_secret": "csecret",
                                 "refresh_token": "brand-refresh", "token_uri": "https://oauth2.googleapis.com/token"})
        self.assertNotIn("access", creds.values())  # short-lived access token is not copied
        self.assertIn("function autoReply()", service.files["Code"]["source"])
        source = service.files["Config"]["source"]
        values = json.loads(source[source.index("{"):source.rindex("}") + 1])
        self.assertEqual(values["templates"], ["ありがとう！\n2行目"])
        self.assertTrue(values["enabled"])
        self.assertEqual(values["daily_limit"], 3)
        self.assertEqual(values["min_gap_minutes"], reply.MIN_GAP_MINUTES)

    def test_redeploy_reuses_project_and_sync_only_rewrites_files(self):
        service = _ScriptService()
        gas.deploy(self.settings(run_on="gas"), service_factory=lambda: service)
        gas.deploy(reply.load_settings(), service_factory=lambda: service)
        self.assertEqual(service.calls.count("create"), 1)
        self.assertEqual(service.calls.count("get"), 1)  # the owner is looked up once
        self.assertEqual(service.updated_version, 2)
        service.calls.clear()
        self.assertTrue(gas.sync(reply.add_template("追加"), service_factory=lambda: service))
        self.assertEqual(service.calls, ["updateContent"])  # triggers run the latest code; no new version
        self.assertIn("追加", service.files["Config"]["source"])

    def test_config_is_disabled_unless_gas_is_the_one_replying(self):
        source = gas.config_source(self.settings(run_on="pc"))
        self.assertIn('"enabled": false', source)
        self.assertFalse(gas.sync(reply.load_settings(), service_factory=_ScriptService))  # no project yet

    def test_channel_login_is_required_before_creating_anything(self):
        reply.token_path().unlink()
        service = _ScriptService()
        with self.assertRaises(gas.GasError) as ctx:
            gas.deploy(self.settings(run_on="gas"), service_factory=lambda: service)
        self.assertIn("返信用に連携", str(ctx.exception))
        self.assertEqual(service.calls, [])

    def test_project_id_is_kept_when_a_later_step_fails(self):
        service = _ScriptService(fail={"updateContent": RuntimeError("boom")})
        with self.assertRaises(gas.GasError):
            gas.deploy(self.settings(run_on="gas"), service_factory=lambda: service)
        self.assertEqual(gas.load_state()["script_id"], "script123")
        service.fail = {}
        gas.deploy(reply.load_settings(), service_factory=lambda: service)
        self.assertEqual(service.calls.count("create"), 1)

    def test_errors_point_to_the_page_the_user_must_open(self):
        disabled = RuntimeError("User has not enabled the Apps Script API. Enable it by visiting "
                                "https://script.google.com/home/usersettings then retry.")
        with self.assertRaises(gas.GasError) as ctx:
            gas.deploy(self.settings(), service_factory=lambda: _ScriptService(fail={"create": disabled}))
        self.assertEqual(ctx.exception.url, gas.USER_SETTINGS_URL)
        not_enabled = RuntimeError(
            "Apps Script API has not been used in project 123456 before or it is disabled. Enable it by visiting "
            "https://console.developers.google.com/apis/api/script.googleapis.com/overview?project=123456 then retry.")
        with self.assertRaises(gas.GasError) as ctx:
            gas.deploy(self.settings(), service_factory=lambda: _ScriptService(fail={"create": not_enabled}))
        self.assertEqual(ctx.exception.url,
                         "https://console.developers.google.com/apis/api/script.googleapis.com/overview?project=123456")


class GasModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(config, "LIBRARY_ROOT", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pc_does_not_reply_while_gas_does_and_the_choice_is_kept(self):
        reply.save_settings(True, ["x"], 3, 30, run_on="gas")
        reply.add_template("y")
        self.assertEqual(reply.load_settings().run_on, "gas")
        self.assertEqual(reply.Settings().run_on, "pc")

        def must_not_be_called():
            raise AssertionError("PC must not call YouTube in GAS mode")

        result = reply.run_once(manual=True, service_factory=must_not_be_called)
        self.assertEqual(result.replied, 0)
        self.assertIn("GAS", result.message)
        calls = []
        reply.token_path().write_text("{}", encoding="utf-8")
        reply.BackgroundReplier(runner=lambda: calls.append(1) or reply.RunResult(0, "")).tick()
        self.assertEqual(calls, [])

    def test_tab_switches_modes_and_explains_failures(self):
        import auto_publish_app

        reply.save_settings(True, ["x"], 3, 30)
        state = {"script_id": "s", "web_url": "https://script.google.com/macros/s/d/exec",
                 "editor_url": "https://script.google.com/d/s/edit",
                 "setup_url": "https://accounts.google.com/AccountChooser?Email=o%40x&continue=e"}
        with patch.object(auto_publish_app.youtube_upload, "setup_problems", return_value=[]), \
                patch.object(auto_publish_app.youtube_gas, "deploy", return_value=state), \
                patch.object(auto_publish_app.webbrowser, "open") as opened:
            message = auto_publish_app.reply_gas_enable()
        opened.assert_called_once_with(state["setup_url"])
        self.assertIn("実行", message)
        self.assertEqual(reply.load_settings().run_on, "gas")
        gas._save_state(state)
        self.assertIn("GAS", auto_publish_app.reply_status())

        error = gas.GasError("Apps Script APIがオフ", url=gas.USER_SETTINGS_URL)
        with patch.object(auto_publish_app.youtube_gas, "sync") as synced:
            auto_publish_app.reply_gas_stop()
        self.assertFalse(synced.call_args.args[0].enabled and synced.call_args.args[0].run_on == "gas")
        self.assertEqual(reply.load_settings().run_on, "pc")
        with patch.object(auto_publish_app.youtube_upload, "setup_problems", return_value=[]), \
                patch.object(auto_publish_app.youtube_gas, "deploy", side_effect=error), \
                patch.object(auto_publish_app.webbrowser, "open") as opened:
            message = auto_publish_app.reply_gas_enable()
        opened.assert_called_once_with(gas.USER_SETTINGS_URL)
        self.assertIn("オフ", message)
        self.assertEqual(reply.load_settings().run_on, "pc")  # unchanged after a failure


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
class GasScriptLogicTest(unittest.TestCase):
    def test_reply_rules_in_the_apps_script_code(self):
        result = subprocess.run(["node", str(ROOT / "tests" / "gas" / "code_test.js")],
                                capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertTrue(result.stdout.startswith("OK"), result.stdout)


if __name__ == "__main__":
    unittest.main()
