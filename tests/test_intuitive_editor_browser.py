"""Browser E2E for the intuitive editor using synthetic, isolated data only."""

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class IntuitiveEditorBrowserTests(unittest.TestCase):
    """Run only with Playwright plus a locally installed Microsoft Edge."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if shutil.which("ffmpeg") is None:
            raise unittest.SkipTest("browser E2E skipped: ffmpeg is not installed")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise unittest.SkipTest(
                "browser E2E skipped: install requirements-dev.txt (Playwright missing)"
            ) from exc

        cls._temp = tempfile.TemporaryDirectory(prefix="cut_video_browser_e2e_")
        cls.addClassCleanup(cls._temp.cleanup)
        cls.root = Path(cls._temp.name)
        cls.data_dir = cls.root / "isolated-data"
        cls.data_dir.mkdir()
        cls.video_path = cls.root / "synthetic_fixture.mp4"

        ffmpeg = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=blue:s=640x360:r=25:d=3",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=3",
                "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", str(cls.video_path),
            ],
            capture_output=True,
            timeout=30,
        )
        if ffmpeg.returncode != 0 or not cls.video_path.exists():
            raise unittest.SkipTest(
                "browser E2E skipped: ffmpeg cannot create the synthetic H.264 fixture"
            )

        cls._create_isolated_database()
        cls._playwright = sync_playwright().start()
        cls.addClassCleanup(cls._playwright.stop)
        try:
            cls.browser = cls._playwright.chromium.launch(
                channel="msedge", headless=True
            )
        except Exception as exc:
            raise unittest.SkipTest(
                "browser E2E skipped: Microsoft Edge is not available to Playwright"
            ) from exc
        cls.addClassCleanup(cls.browser.close)

        cls.port = _free_local_port()
        env = os.environ.copy()
        env.update({
            "CUT_VIDEO_DATA_DIR": str(cls.data_dir),
            "CUT_VIDEO_E2E_PORT": str(cls.port),
            "CUT_VIDEO_E2E_ROOT": str(cls.root),
            "GRADIO_ANALYTICS_ENABLED": "False",
            "PYTHONIOENCODING": "utf-8",
        })
        repo_root = Path(__file__).resolve().parents[1]
        launch_code = (
            "import os, app; "
            "app._enable_crash_log(); "
            "app._initialize_preview_cache(); "
            "app.demo.launch(server_name='127.0.0.1', "
            "server_port=int(os.environ['CUT_VIDEO_E2E_PORT']), "
            "inbrowser=False, css=app._APP_CSS, "
            "allowed_paths=[os.environ['CUT_VIDEO_E2E_ROOT']])"
        )
        cls._server_log = open(cls.root / "server.log", "w", encoding="utf-8")
        cls.addClassCleanup(cls._server_log.close)
        cls.server = subprocess.Popen(
            [sys.executable, "-c", launch_code],
            cwd=repo_root,
            env=env,
            stdout=cls._server_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        cls.addClassCleanup(cls._stop_server)
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls._wait_for_server()

    @classmethod
    def _create_isolated_database(cls):
        conn = sqlite3.connect(cls.data_dir / "index.db")
        try:
            conn.executescript(
                """
                CREATE TABLE videos (
                    video_id TEXT PRIMARY KEY, path TEXT NOT NULL,
                    duration REAL, created_at TEXT DEFAULT (datetime('now')),
                    asr_complete INTEGER DEFAULT 0
                );
                CREATE TABLE asr_segments (
                    segment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL, start_sec REAL NOT NULL,
                    end_sec REAL NOT NULL, text TEXT, words_json TEXT
                );
                CREATE TABLE text_chunks (
                    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL, start_sec REAL NOT NULL,
                    end_sec REAL NOT NULL, text TEXT
                );
                """
            )
            words = [
                {"word": "alpha", "start": 0.20, "end": 0.55},
                {"word": "beta", "start": 0.70, "end": 1.05},
                {"word": "gamma", "start": 1.25, "end": 1.65},
                {"word": "delta", "start": 1.90, "end": 2.35},
            ]
            conn.execute(
                "INSERT INTO videos(video_id, path, duration, asr_complete) "
                "VALUES (?, ?, ?, 1)",
                ("synthetic-video", str(cls.video_path), 3.0),
            )
            conn.execute(
                "INSERT INTO asr_segments(video_id, start_sec, end_sec, text, words_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "synthetic-video", 0.2, 2.35,
                    "alpha beta gamma delta", json.dumps(words),
                ),
            )
            conn.execute(
                "INSERT INTO text_chunks(video_id, start_sec, end_sec, text) "
                "VALUES (?, ?, ?, ?)",
                ("synthetic-video", 0.2, 2.35, "alpha beta gamma delta"),
            )
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def _wait_for_server(cls):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if cls.server.poll() is not None:
                raise RuntimeError("synthetic browser E2E server exited during startup")
            try:
                with urllib.request.urlopen(cls.base_url, timeout=1) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("synthetic browser E2E server did not become ready")

    @classmethod
    def _stop_server(cls):
        if getattr(cls, "server", None) is None or cls.server.poll() is not None:
            return
        cls.server.terminate()
        try:
            cls.server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.server.kill()
            cls.server.wait(timeout=5)

    def test_intuitive_editor_browser_workflow(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(30_000)
        dialogs = []

        def dismiss_dialog(dialog):
            dialogs.append(dialog.message)
            dialog.dismiss()

        page.on("dialog", dismiss_dialog)
        page.goto(self.base_url, wait_until="domcontentloaded")
        page.get_by_role("tab", name="直感編集（試作）").click()

        card = page.locator(
            "#intuitive-video-card-grid .intuitive-video-card"
        ).first
        card.wait_for(state="visible")
        card.click()
        root = page.locator("#intuitive-toolbox [data-intuitive-root]")
        root.wait_for(state="visible")
        search_accordion = page.get_by_text(
            "検索して候補区間から編集を始める", exact=True
        )
        search_accordion.click()
        search_target_input = page.locator("#intuitive-search-target input")
        search_target_input.wait_for(state="visible")
        self.assertIn("synthetic_fixture.mp4", search_target_input.input_value())
        search_accordion.click()
        page.locator("#intuitive-transcript-words").get_by_text(
            "alpha", exact=True
        ).wait_for(state="visible")

        # A temporary Gradio rerender can remove the hidden command bridge.
        # The queued command must stay at the head and be submitted once after
        # the exact nodes return.
        bridge_revision = int(root.get_attribute("data-revision"))
        page.evaluate(
            """() => {
              const field = document.querySelector(
                '#intuitive-command-json textarea, #intuitive-command-json input'
              );
              const button = document.querySelector(
                '#intuitive-command-submit button, button#intuitive-command-submit, '
                + '#intuitive-command-submit'
              );
              window.__e2eBridgeClicks = 0;
              button.addEventListener('click', () => window.__e2eBridgeClicks++);
              window.__e2eDetachedBridge = {
                field, fieldParent: field.parentNode,
                button, buttonParent: button.parentNode
              };
              field.remove();
              button.remove();
            }"""
        )
        page.locator(
            "#intuitive-toolbox [data-intuitive-tool='overall_end']"
        ).click()
        page.wait_for_timeout(250)
        self.assertEqual(
            int(root.get_attribute("data-revision")), bridge_revision
        )
        page.evaluate(
            """() => {
              const saved = window.__e2eDetachedBridge;
              saved.fieldParent.appendChild(saved.field);
              saved.buttonParent.appendChild(saved.button);
            }"""
        )
        page.wait_for_function(
            "([selector, revision]) => "
            "Number(document.querySelector(selector).dataset.revision) > revision",
            arg=["#intuitive-toolbox [data-intuitive-root]", bridge_revision],
        )
        self.assertEqual(page.evaluate("window.__e2eBridgeClicks"), 1)

        # The recovery bridge performs a read-only canonical redraw through a
        # separate callback. It must preserve both revision and the most recent
        # command acknowledgement while echoing its one-shot sync token.
        sync_revision = int(root.get_attribute("data-revision"))
        synced_command_id = root.get_attribute("data-last-command-id")
        sync_token = "edge-e2e-sync-token"
        page.evaluate(
            """(token) => {
              const field = document.querySelector(
                '#intuitive-sync-token textarea, #intuitive-sync-token input'
              );
              const button = document.querySelector(
                '#intuitive-sync-submit button, button#intuitive-sync-submit, '
                + '#intuitive-sync-submit'
              );
              const prototype = field instanceof HTMLTextAreaElement
                ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
              Object.getOwnPropertyDescriptor(prototype, 'value').set.call(field, token);
              field.dispatchEvent(new InputEvent('input', {
                bubbles: true, composed: true, inputType: 'insertText', data: null
              }));
              button.click();
            }""",
            sync_token,
        )
        page.wait_for_function(
            "token => document.querySelector("
            "'#intuitive-sync-ack [data-intuitive-sync-token]')"
            ".dataset.intuitiveSyncToken === token",
            arg=sync_token,
        )
        self.assertEqual(int(root.get_attribute("data-revision")), sync_revision)
        self.assertEqual(
            root.get_attribute("data-last-command-id"), synced_command_id
        )

        # A validation-error acknowledgement unlocks the bridge, but must not
        # run commands queued behind the rejected edit.
        arm_revision = int(root.get_attribute("data-revision"))
        page.locator(
            "#intuitive-toolbox [data-intuitive-tool='exclude_end']"
        ).click()
        page.wait_for_function(
            "([selector, revision]) => { const root = document.querySelector(selector); "
            "return Number(root.dataset.revision) > revision "
            "&& root.dataset.activeTool === 'exclude_end' "
            "&& root.dataset.lastCommandStatus === 'success'; }",
            arg=["#intuitive-toolbox [data-intuitive-root]", arm_revision],
        )
        rejected_revision = int(root.get_attribute("data-revision"))
        page.evaluate(
            """() => {
              document.querySelector(
                "#intuitive-transcript-words .intuitive-word[data-start='0.200']"
              ).click();
              document.querySelector(
                "#intuitive-toolbox [data-intuitive-tool='overall_start']"
              ).click();
            }"""
        )
        page.wait_for_function(
            "([selector, revision]) => { const root = document.querySelector(selector); "
            "return Number(root.dataset.revision) > revision "
            "&& root.dataset.lastCommandStatus === 'validation_error'; }",
            arg=["#intuitive-toolbox [data-intuitive-root]", rejected_revision],
        )
        failed_revision = int(root.get_attribute("data-revision"))
        page.wait_for_timeout(300)
        self.assertEqual(int(root.get_attribute("data-revision")), failed_revision)
        self.assertEqual(root.get_attribute("data-active-tool"), "exclude_end")
        page.locator("#intuitive-command-wait-notice").get_by_text(
            "後続の操作を破棄しました", exact=False
        ).wait_for(state="visible")

        page.locator(
            "#intuitive-toolbox [data-intuitive-tool='overall_start']"
        ).click()
        page.locator(
            "#intuitive-transcript-words .intuitive-word[data-start='0.200']"
        ).click()
        page.wait_for_function(
            "document.querySelector('#intuitive-toolbox [data-intuitive-root]')"
            ".dataset.editDirty === 'true'"
        )

        # Toolbar history is routed through the same command-id FIFO.
        undo_revision = int(root.get_attribute("data-revision"))
        page.locator("[data-intuitive-history='undo']").click()
        page.wait_for_function(
            "([selector, revision]) => { const root = document.querySelector(selector); "
            "return Number(root.dataset.revision) > revision "
            "&& root.dataset.editDirty === 'false' && root.dataset.canRedo === 'true'; }",
            arg=["#intuitive-toolbox [data-intuitive-root]", undo_revision],
        )
        redo_revision = int(root.get_attribute("data-revision"))
        page.locator("[data-intuitive-history='redo']").click()
        page.wait_for_function(
            "([selector, revision]) => { const root = document.querySelector(selector); "
            "return Number(root.dataset.revision) > revision "
            "&& root.dataset.editDirty === 'true' && root.dataset.canUndo === 'true'; }",
            arg=["#intuitive-toolbox [data-intuitive-root]", redo_revision],
        )

        # Transcript uses one roving Tab stop; Enter/Space activate the same
        # existing click path, while ArrowRight only moves focus.
        first_word = page.locator(
            "#intuitive-transcript-words .intuitive-word[tabindex='0']"
        ).first
        first_word.focus()
        word_revision = int(root.get_attribute("data-revision"))
        first_word.press("Enter")
        page.wait_for_function(
            "([selector, revision]) => Number(document.querySelector(selector)"
            ".dataset.revision) > revision",
            arg=["#intuitive-toolbox [data-intuitive-root]", word_revision],
        )
        page.locator(
            "#intuitive-transcript-words .intuitive-word[tabindex='0']"
        ).first.focus()
        page.keyboard.press("ArrowRight")
        self.assertEqual(
            page.evaluate("document.activeElement.dataset.start"), "0.700"
        )
        space_revision = int(root.get_attribute("data-revision"))
        page.keyboard.press("Space")
        page.wait_for_function(
            "([selector, revision]) => Number(document.querySelector(selector)"
            ".dataset.revision) > revision",
            arg=["#intuitive-toolbox [data-intuitive-root]", space_revision],
        )

        # Zoom boundary Arrow sends exactly one canonical set_boundary command.
        toggle_revision = int(root.get_attribute("data-revision"))
        page.locator("[data-intuitive-toggle-edit-mode]").click()
        page.wait_for_function(
            "([selector, revision]) => { const root = document.querySelector(selector); "
            "return Number(root.dataset.revision) > revision "
            "&& root.dataset.timelineEditMode === 'true'; }",
            arg=["#intuitive-toolbox [data-intuitive-root]", toggle_revision],
        )
        end_handle = page.locator(
            "[data-intuitive-zoom] [data-boundary-kind='overall_end'][role='slider']"
        )
        before_end = float(end_handle.get_attribute("data-boundary-time"))
        handle_revision = int(root.get_attribute("data-revision"))
        end_handle.focus()
        end_handle.press("ArrowLeft")
        page.wait_for_function(
            "([selector, revision]) => Number(document.querySelector(selector)"
            ".dataset.revision) > revision",
            arg=["#intuitive-toolbox [data-intuitive-root]", handle_revision],
        )
        end_handle = page.locator(
            "[data-intuitive-zoom] [data-boundary-kind='overall_end'][role='slider']"
        )
        self.assertAlmostEqual(
            float(end_handle.get_attribute("data-boundary-time")), before_end - 1.0,
            delta=0.01,
        )

        viewport_revision = int(root.get_attribute("data-revision"))
        viewport_move = page.locator(
            "[data-intuitive-overview] [data-viewport-drag='move'][role='slider']"
        )
        viewport_move.focus()
        viewport_move.press("ArrowLeft")
        page.wait_for_function(
            "([selector, revision]) => Number(document.querySelector(selector)"
            ".dataset.revision) > revision",
            arg=["#intuitive-toolbox [data-intuitive-root]", viewport_revision],
        )
        revision = root.get_attribute("data-revision")

        page.locator(
            "button#intuitive-reselect-video, #intuitive-reselect-video button"
        ).click()
        card.wait_for(state="visible")
        before_dialogs = len(dialogs)
        card.click()
        page.wait_for_timeout(250)
        self.assertEqual(len(dialogs), before_dialogs + 1)
        self.assertEqual(root.get_attribute("data-revision"), revision)
        self.assertEqual(root.get_attribute("data-edit-dirty"), "true")

        before_dialogs = len(dialogs)
        page.locator(
            "button#intuitive-load-video, #intuitive-load-video button"
        ).click()
        page.wait_for_timeout(250)
        self.assertEqual(len(dialogs), before_dialogs + 1)
        self.assertEqual(root.get_attribute("data-revision"), revision)

        page.get_by_text(
            "検索して候補区間から編集を始める", exact=True
        ).click()
        page.locator("#intuitive-search-results").wait_for(state="visible")
        before_dialogs = len(dialogs)
        canceled = page.evaluate(
            """() => {
              const host = document.querySelector('#intuitive-search-results');
              const row = document.createElement('div');
              row.className = 'virtual-row';
              const cell = document.createElement('div');
              cell.className = 'body-cell';
              row.appendChild(cell);
              host.appendChild(row);
              const allowed = cell.dispatchEvent(new MouseEvent('mousedown', {
                bubbles: true, cancelable: true, button: 0
              }));
              row.remove();
              return !allowed;
            }"""
        )
        self.assertTrue(canceled)
        self.assertEqual(len(dialogs), before_dialogs + 1)
        self.assertEqual(root.get_attribute("data-revision"), revision)

        before_dialogs = len(dialogs)
        ime_allowed = page.evaluate(
            """() => {
              const host = document.querySelector('#intuitive-search-query');
              const probe = document.createElement('input');
              host.appendChild(probe);
              const event = new KeyboardEvent('keydown', {
                key: 'Enter', keyCode: 229, isComposing: true,
                bubbles: true, cancelable: true
              });
              const allowed = probe.dispatchEvent(event);
              probe.remove();
              return allowed;
            }"""
        )
        self.assertTrue(ime_allowed)
        self.assertEqual(len(dialogs), before_dialogs)

        page.wait_for_function(
            """() => {
              const video = document.querySelector('#intuitive-preview-video video');
              return video && video.readyState >= 1 && Number.isFinite(video.duration);
            }"""
        )

        def seek_and_left(seconds):
            return page.evaluate(
                """async (seconds) => {
                  const video = document.querySelector('#intuitive-preview-video video');
                  video.currentTime = seconds;
                  video.dispatchEvent(new Event('seeking', {bubbles: true}));
                  await new Promise(requestAnimationFrame);
                  await new Promise(requestAnimationFrame);
                  return parseFloat(document.querySelector(
                    '[data-intuitive-zoom] .intuitive-playhead'
                  ).style.left);
                }""",
                seconds,
            )

        first_left = seek_and_left(0.4)
        second_left = seek_and_left(2.2)
        self.assertGreater(second_left, first_left + 20)
        self.assertEqual(root.get_attribute("data-preview-mode"), "source")

        # Any server-rendering edit command must carry the browser's current
        # source playhead so the canonical redraw does not jump backwards.
        redraw_revision = int(root.get_attribute("data-revision"))
        page.locator(
            "#intuitive-toolbox [data-intuitive-tool='overall_end']"
        ).click()
        page.wait_for_function(
            "([selector, revision]) => "
            "Number(document.querySelector(selector).dataset.revision) > revision",
            arg=["#intuitive-toolbox [data-intuitive-root]", redraw_revision],
        )
        redrawn_left = page.evaluate(
            "parseFloat(document.querySelector("
            "'[data-intuitive-zoom] .intuitive-playhead').style.left)"
        )
        self.assertAlmostEqual(redrawn_left, second_left, delta=5.0)


if __name__ == "__main__":
    unittest.main()
