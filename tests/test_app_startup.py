import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


# app.py constructs the Gradio UI while it is imported.  Keep standalone runs
# of this module away from the user's database and runtime files.  Every test
# also patches APP_PIDFILE explicitly because another test module may already
# have imported app with its own isolated data directory.
_STARTUP_TEST_DATA = tempfile.TemporaryDirectory(prefix="cut_app_startup_data_")
unittest.addModuleCleanup(_STARTUP_TEST_DATA.cleanup)
_ORIGINAL_DATA_DIR = os.environ.get("CUT_VIDEO_DATA_DIR")
os.environ["CUT_VIDEO_DATA_DIR"] = _STARTUP_TEST_DATA.name


def _restore_data_dir_environment():
    if _ORIGINAL_DATA_DIR is None:
        os.environ.pop("CUT_VIDEO_DATA_DIR", None)
    else:
        os.environ["CUT_VIDEO_DATA_DIR"] = _ORIGINAL_DATA_DIR


unittest.addModuleCleanup(_restore_data_dir_environment)

import app as app_module


class _HTTPResponse:
    def __init__(self, payload, *, status=200):
        self._payload = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self):
        return self._payload

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class AppStartupControlTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="cut_app_startup_")
        self.addCleanup(self._temp.cleanup)
        self.pidfile = Path(self._temp.name) / "runtime" / "app.pid"

    def _record_pidfile(
        self,
        *,
        pid=43210,
        port=17860,
        app_path=None,
    ):
        self.pidfile.parent.mkdir(parents=True, exist_ok=True)
        self.pidfile.write_text(
            json.dumps(
                {
                    "pid": pid,
                    "port": port,
                    "app_path": str(
                        Path(app_path or app_module.__file__).resolve()
                    ),
                }
            ),
            encoding="utf-8",
        )

    def test_runtime_pidfile_uses_cache_root(self):
        self.assertEqual(
            app_module.APP_PIDFILE,
            app_module.config.CACHE_ROOT / "app.pid",
        )

    def test_health_check_requires_the_cut_video_gradio_config(self):
        response = _HTTPResponse(
            {"title": "動画シーン検索", "mode": "blocks"}
        )
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            healthy = app_module._app_is_healthy(
                17860, timeout_sec=0.25, attempts=1
            )

        self.assertTrue(healthy)
        request = urlopen.call_args.args[0]
        url = getattr(request, "full_url", request)
        self.assertEqual(url, "http://127.0.0.1:17860/config")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 0.25)

    def test_health_check_rejects_an_unrelated_service_on_the_same_port(self):
        cases = [
            {"title": "unrelated", "mode": "blocks"},
            {"title": "動画シーン検索", "mode": "interface"},
            {"title": "動画シーン検索"},
        ]
        for payload in cases:
            with self.subTest(payload=payload), patch(
                "urllib.request.urlopen", return_value=_HTTPResponse(payload)
            ):
                self.assertFalse(
                    app_module._app_is_healthy(
                        17860, timeout_sec=0.01, attempts=1
                    )
                )

    def test_health_check_returns_false_for_timeout_and_url_errors(self):
        errors = [
            TimeoutError("synthetic timeout"),
            urllib.error.URLError("synthetic refusal"),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__), patch(
                "urllib.request.urlopen", side_effect=error
            ):
                self.assertFalse(
                    app_module._app_is_healthy(
                        17860, timeout_sec=0.01, attempts=1
                    )
                )

    def test_health_check_retries_a_transient_failure(self):
        response = _HTTPResponse(
            {"title": "動画シーン検索", "mode": "blocks"}
        )
        with (
            patch(
                "urllib.request.urlopen",
                side_effect=[urllib.error.URLError("not ready"), response],
            ) as urlopen,
            patch("time.sleep") as sleep,
        ):
            healthy = app_module._app_is_healthy(
                17860, timeout_sec=0.01, attempts=2
            )

        self.assertTrue(healthy)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()

    def test_already_running_uses_http_identity_not_an_open_tcp_port(self):
        with patch.object(
            app_module, "_app_is_healthy", return_value=False
        ) as healthy:
            self.assertFalse(app_module._already_running(port=17860))

        healthy.assert_called_once_with(17860, timeout_sec=2.0, attempts=3)

    def test_write_pidfile_records_pid_port_and_resolved_app_path(self):
        with (
            patch.object(app_module, "APP_PIDFILE", self.pidfile),
            patch("os.getpid", return_value=43210),
        ):
            app_module._write_app_pidfile(port=17860)

        payload = json.loads(self.pidfile.read_text(encoding="utf-8"))
        self.assertEqual(payload["pid"], 43210)
        self.assertEqual(payload["port"], 17860)
        self.assertEqual(
            Path(payload["app_path"]), Path(app_module.__file__).resolve()
        )
        self.assertFalse(self.pidfile.with_suffix(".pid.tmp").exists())

    def test_remove_pidfile_is_idempotent(self):
        self._record_pidfile(pid=43210)
        with (
            patch.object(app_module, "APP_PIDFILE", self.pidfile),
            patch("os.getpid", return_value=43210),
        ):
            app_module._remove_app_pidfile()
            app_module._remove_app_pidfile()

        self.assertFalse(self.pidfile.exists())

    def test_remove_pidfile_preserves_another_process_record(self):
        self._record_pidfile(pid=43210)
        with (
            patch.object(app_module, "APP_PIDFILE", self.pidfile),
            patch("os.getpid", return_value=98765),
        ):
            app_module._remove_app_pidfile()

        self.assertTrue(self.pidfile.exists())

    def test_cleanup_discards_malformed_pidfile_without_running_a_command(self):
        self.pidfile.parent.mkdir(parents=True)
        self.pidfile.write_text("{not-json", encoding="utf-8")
        with (
            patch.object(app_module, "APP_PIDFILE", self.pidfile),
            patch.object(app_module, "_port_is_open", return_value=True),
            patch.object(app_module, "_app_is_healthy", return_value=False),
            patch("subprocess.run") as run,
        ):
            cleaned = app_module._cleanup_stale_app_instance(port=17860)

        self.assertFalse(cleaned)
        self.assertFalse(self.pidfile.exists())
        run.assert_not_called()

    def test_cleanup_never_stops_a_healthy_running_app(self):
        self._record_pidfile(pid=43210)
        with (
            patch.object(app_module, "APP_PIDFILE", self.pidfile),
            patch.object(app_module, "_port_is_open", return_value=True),
            patch.object(app_module, "_app_is_healthy", return_value=True),
            patch.object(app_module, "_stale_app_process") as stale,
            patch("subprocess.run") as run,
        ):
            cleaned = app_module._cleanup_stale_app_instance(port=17860)

        self.assertFalse(cleaned)
        self.assertTrue(self.pidfile.exists())
        stale.assert_not_called()
        run.assert_not_called()

    def test_cleanup_never_stops_the_current_process(self):
        self._record_pidfile(pid=43210)
        with (
            patch.object(app_module, "APP_PIDFILE", self.pidfile),
            patch("os.getpid", return_value=43210),
            patch.object(app_module, "_port_is_open", return_value=True),
            patch.object(app_module, "_app_is_healthy", return_value=False),
            patch.object(app_module, "_stale_app_process") as stale,
            patch("subprocess.run") as run,
        ):
            cleaned = app_module._cleanup_stale_app_instance(port=17860)

        self.assertFalse(cleaned)
        self.assertFalse(self.pidfile.exists())
        stale.assert_not_called()
        run.assert_not_called()

    def test_cleanup_rejects_a_record_for_another_port_or_app(self):
        cases = [
            {"port": 17861, "app_path": app_module.__file__},
            {
                "port": 17860,
                "app_path": Path(self._temp.name) / "other" / "app.py",
            },
        ]
        for record in cases:
            with self.subTest(record=record):
                self._record_pidfile(pid=43210, **record)
                with (
                    patch.object(app_module, "APP_PIDFILE", self.pidfile),
                    patch("os.getpid", return_value=98765),
                    patch.object(app_module, "_port_is_open", return_value=True),
                    patch.object(app_module, "_app_is_healthy", return_value=False),
                    patch.object(app_module, "_stale_app_process") as stale,
                    patch("subprocess.run") as run,
                ):
                    cleaned = app_module._cleanup_stale_app_instance(port=17860)

                self.assertFalse(cleaned)
                self.assertFalse(self.pidfile.exists())
                stale.assert_not_called()
                run.assert_not_called()

    def test_process_check_rejects_port_owner_mismatch(self):
        result = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch("os.name", "nt"),
            patch("os.getpid", return_value=98765),
            patch("subprocess.run", return_value=result) as run,
        ):
            matches = app_module._stale_app_process(43210, 17860)

        self.assertFalse(matches)
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["powershell", "-NoProfile", "-Command"])
        self.assertIn("43210", command[3])
        self.assertIn("17860", command[3])

    def test_process_check_rejects_a_non_app_command_line(self):
        result = SimpleNamespace(
            returncode=0,
            stdout=r"F:\Python\python.exe F:\tools\unrelated.py",
            stderr="",
        )
        with (
            patch("os.name", "nt"),
            patch("os.getpid", return_value=98765),
            patch("subprocess.run", return_value=result),
        ):
            self.assertFalse(app_module._stale_app_process(43210, 17860))

    def test_process_check_accepts_only_an_app_command_line_owned_by_pid(self):
        result = SimpleNamespace(
            returncode=0,
            stdout=(
                r"F:\myapp\cut\venv\Scripts\python.exe "
                r"F:\myapp\cut\app.py"
            ),
            stderr="",
        )
        with (
            patch("os.name", "nt"),
            patch("os.getpid", return_value=98765),
            patch("subprocess.run", return_value=result),
        ):
            self.assertTrue(app_module._stale_app_process(43210, 17860))

    def test_cleanup_taskkills_only_a_verified_stale_app_and_waits_for_port(self):
        self._record_pidfile(pid=43210)
        taskkill_result = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch.object(app_module, "APP_PIDFILE", self.pidfile),
            patch("os.getpid", return_value=98765),
            patch.object(app_module, "_port_is_open", side_effect=[True, False]) as port_open,
            patch.object(app_module, "_app_is_healthy", return_value=False),
            patch.object(
                app_module, "_stale_app_process", return_value=True
            ) as stale,
            patch("subprocess.run", return_value=taskkill_result) as run,
            patch("time.sleep") as sleep,
        ):
            cleaned = app_module._cleanup_stale_app_instance(port=17860)

        self.assertTrue(cleaned)
        stale.assert_called_once_with(43210, 17860)
        run.assert_called_once_with(
            ["taskkill", "/PID", "43210", "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(port_open.call_count, 2)
        sleep.assert_not_called()
        self.assertFalse(self.pidfile.exists())

    def test_cleanup_does_not_taskkill_when_process_verification_fails(self):
        self._record_pidfile(pid=43210)
        with (
            patch.object(app_module, "APP_PIDFILE", self.pidfile),
            patch("os.getpid", return_value=98765),
            patch.object(app_module, "_port_is_open", return_value=True),
            patch.object(app_module, "_app_is_healthy", return_value=False),
            patch.object(app_module, "_stale_app_process", return_value=False),
            patch("subprocess.run") as run,
        ):
            cleaned = app_module._cleanup_stale_app_instance(port=17860)

        self.assertFalse(cleaned)
        run.assert_not_called()
        self.assertFalse(self.pidfile.exists())


if __name__ == "__main__":
    unittest.main()
