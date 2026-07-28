import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


# app.py builds its Gradio UI at import time.  Keep those reads isolated when
# this module is run on its own; the full suite may already have imported app
# through test_app_clip_plan with an equivalent isolated directory.
_JOB_TEST_DATA = tempfile.TemporaryDirectory(prefix="cut_index_job_data_")
unittest.addModuleCleanup(_JOB_TEST_DATA.cleanup)
_ORIGINAL_DATA_DIR = os.environ.get("CUT_VIDEO_DATA_DIR")
os.environ["CUT_VIDEO_DATA_DIR"] = _JOB_TEST_DATA.name


def _restore_data_dir_environment():
    if _ORIGINAL_DATA_DIR is None:
        os.environ.pop("CUT_VIDEO_DATA_DIR", None)
    else:
        os.environ["CUT_VIDEO_DATA_DIR"] = _ORIGINAL_DATA_DIR


unittest.addModuleCleanup(_restore_data_dir_environment)

import gradio as gr

import app as app_module
from moment_retrieval.downloader import DownloadError


class _QueueStdout:
    def __init__(self):
        self._lines = queue.Queue()

    def push(self, line: str):
        self._lines.put(line if line.endswith("\n") else line + "\n")

    def finish(self):
        self._lines.put("")

    def readline(self):
        return self._lines.get(timeout=5)


class _ControllableProcess:
    def __init__(self, pid=43210):
        self.pid = pid
        self.stdout = _QueueStdout()
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        deadline = time.monotonic() + (timeout if timeout is not None else 5)
        while self.returncode is None and time.monotonic() < deadline:
            time.sleep(0.005)
        if self.returncode is None:
            raise subprocess.TimeoutExpired("synthetic-index", timeout)
        return self.returncode


class IndexJobControlCharacterizationTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="cut_index_job_")
        self.addCleanup(self._temp.cleanup)
        self.pidfile = Path(self._temp.name) / "index_job.pid"
        self.old_lock = app_module._index_lock
        self.old_state = app_module._index_state
        app_module._index_lock = threading.Lock()
        app_module._index_state = {"proc": None, "stopped": False}
        self.addCleanup(self._restore_app_state)

    def _restore_app_state(self):
        app_module._index_lock = self.old_lock
        app_module._index_state = self.old_state

    def _assert_index_lock_released(self):
        self.assertTrue(app_module._index_lock.acquire(blocking=False))
        app_module._index_lock.release()

    def test_normal_completion_removes_pidfile_and_releases_lock(self):
        proc = _ControllableProcess()
        proc.stdout.push("[1/4] synthetic indexing")
        proc.stdout.finish()
        proc.returncode = 0

        with (
            patch.object(app_module, "INDEX_JOB_PIDFILE", self.pidfile),
            patch("subprocess.Popen", return_value=proc) as popen,
            patch.object(app_module, "list_video_choices", return_value=["synthetic"]),
            patch.object(app_module.gr, "Info"),
        ):
            outputs = list(
                app_module.do_index(
                    str(Path(self._temp.name) / "synthetic.mp4"),
                    "tiny",
                    False,
                    batch_infer=False,
                )
            )

        command = popen.call_args.args[0]
        self.assertEqual(command[:2], [sys.executable, "index_video.py"])
        self.assertIn("--batch-size", command)
        self.assertIn("[1/4] synthetic indexing", outputs[-1][0])
        self.assertFalse(self.pidfile.exists())
        self.assertIsNone(app_module._index_state["proc"])
        self._assert_index_lock_released()

    def test_optional_local_llm_analysis_is_forwarded_to_index_subprocess(self):
        proc = _ControllableProcess()
        proc.stdout.finish()
        proc.returncode = 0

        with (
            patch.object(app_module, "INDEX_JOB_PIDFILE", self.pidfile),
            patch("subprocess.Popen", return_value=proc) as popen,
            patch.object(app_module, "list_video_choices", return_value=[]),
            patch.object(app_module.gr, "Info"),
        ):
            list(
                app_module.do_index(
                    str(Path(self._temp.name) / "synthetic.mp4"),
                    "tiny",
                    False,
                    llm_analysis=True,
                    llm_model="synthetic-local-model",
                )
            )

        command = popen.call_args.args[0]
        self.assertIn("--llm-analysis", command)
        self.assertEqual(
            command[command.index("--llm-model") + 1],
            "synthetic-local-model",
        )

    def test_registered_index_process_can_be_stopped_and_reports_resume(self):
        proc = _ControllableProcess()
        proc.stdout.push("  文字起こし中... 10.0%")
        outputs = []
        errors = []

        def fake_run(command, **kwargs):
            if command and command[0] == "taskkill":
                proc.returncode = 1
                proc.stdout.finish()
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        def consume():
            try:
                outputs.extend(
                    app_module.do_index(
                        str(Path(self._temp.name) / "synthetic.mp4"),
                        "tiny",
                        False,
                    )
                )
            except BaseException as exc:  # surfaced in the main test thread
                errors.append(exc)

        with (
            patch.object(app_module, "INDEX_JOB_PIDFILE", self.pidfile),
            patch("subprocess.Popen", return_value=proc),
            patch("subprocess.run", side_effect=fake_run) as run,
            patch.object(app_module.gr, "Info"),
        ):
            worker = threading.Thread(target=consume, daemon=True)
            worker.start()
            deadline = time.monotonic() + 3
            while app_module._index_state["proc"] is not proc:
                if time.monotonic() >= deadline:
                    self.fail("synthetic index process was not registered")
                time.sleep(0.01)

            self.assertTrue(self.pidfile.exists())
            app_module.stop_indexing()
            worker.join(timeout=3)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        taskkill = run.call_args.args[0]
        self.assertEqual(
            taskkill,
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
        )
        self.assertTrue(app_module._index_state["stopped"])
        self.assertTrue(any("処理を停止しました" in item[0] for item in outputs))
        self.assertFalse(self.pidfile.exists())
        self.assertIsNone(app_module._index_state["proc"])
        self._assert_index_lock_released()

    def test_stop_without_a_live_index_process_is_a_validation_error(self):
        with self.assertRaises(gr.Error):
            app_module.stop_indexing()

    def test_existing_llm_analysis_uses_shared_stop_and_lock(self):
        proc = _ControllableProcess(pid=43211)
        proc.stdout.push("synthetic local analysis")
        outputs = []
        errors = []

        def fake_run(command, **kwargs):
            if command and command[0] == "taskkill":
                proc.returncode = 1
                proc.stdout.finish()
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        def consume():
            try:
                outputs.extend(
                    app_module.do_existing_llm_analysis(
                        "synthetic video choice",
                        "synthetic-local-model",
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        with (
            patch.object(app_module, "INDEX_JOB_PIDFILE", self.pidfile),
            patch.object(app_module, "parse_video_choice", return_value="vid_synthetic"),
            patch("subprocess.Popen", return_value=proc) as popen,
            patch("subprocess.run", side_effect=fake_run) as run,
            patch.object(app_module.gr, "Info"),
        ):
            worker = threading.Thread(target=consume, daemon=True)
            worker.start()
            deadline = time.monotonic() + 3
            while app_module._index_state["proc"] is not proc:
                if time.monotonic() >= deadline:
                    self.fail("synthetic LLM process was not registered")
                time.sleep(0.01)

            self.assertTrue(self.pidfile.exists())
            app_module.stop_indexing()
            worker.join(timeout=3)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(
            popen.call_args.args[0][:2],
            [sys.executable, "analyze_transcript.py"],
        )
        self.assertEqual(
            run.call_args.args[0],
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
        )
        self.assertTrue(any("LLM解析を停止しました" in item[0] for item in outputs))
        self.assertFalse(self.pidfile.exists())
        self.assertIsNone(app_module._index_state["proc"])
        self._assert_index_lock_released()

    def test_url_download_phase_has_no_registered_index_process_yet(self):
        """Temporary migration characterization, not the desired stop contract.

        URL download currently happens before the index Popen is registered.
        A future cancellable downloader should replace this observation with a
        cancellation/partial-file cleanup assertion.
        """

        entered_download = threading.Event()
        release_download = threading.Event()
        outputs = []
        errors = []

        def controlled_download(_url):
            entered_download.set()
            yield "synthetic download started", None
            release_download.wait(timeout=3)
            raise DownloadError("synthetic download interruption")

        def consume():
            try:
                outputs.extend(
                    app_module.do_index(
                        "https://example.invalid/synthetic",
                        "tiny",
                        False,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        with (
            patch.object(app_module, "INDEX_JOB_PIDFILE", self.pidfile),
            patch.object(app_module, "download_video", side_effect=controlled_download),
        ):
            worker = threading.Thread(target=consume, daemon=True)
            worker.start()
            self.assertTrue(entered_download.wait(timeout=3))
            self.assertIsNone(app_module._index_state["proc"])
            try:
                with self.assertRaises(gr.Error):
                    app_module.stop_indexing()
            finally:
                release_download.set()
            worker.join(timeout=3)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(any("エラー" in item[0] for item in outputs))
        self.assertIsNone(app_module._index_state["proc"])
        self._assert_index_lock_released()


if __name__ == "__main__":
    unittest.main()
