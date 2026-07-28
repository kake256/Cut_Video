"""Browser E2E for the intuitive editor using synthetic, isolated data only."""

import json
import math
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
import statistics
from pathlib import Path

import numpy as np

from moment_retrieval.vector_index import VectorIndex


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
        if shutil.which("ffprobe") is None:
            raise unittest.SkipTest("browser E2E skipped: ffprobe is not installed")
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
        cls._create_isolated_vector_index()
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
            "import os, numpy as np, app; "
            "app._embedder = type('SyntheticE2EEmbedder', (), {"
            "'encode': lambda self, texts: np.tile("
            "np.asarray([[1.0, 0.0]], dtype='float32'), (len(texts), 1))"
            "})(); "
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
                    "synthetic-video", 0.0, 2.8,
                    "alpha beta gamma delta", json.dumps(words),
                ),
            )
            cursor = conn.execute(
                "INSERT INTO text_chunks(video_id, start_sec, end_sec, text) "
                "VALUES (?, ?, ?, ?)",
                ("synthetic-video", 0.2, 2.35, "alpha beta gamma delta"),
            )
            cls.chunk_id = int(cursor.lastrowid)
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def _create_isolated_vector_index(cls):
        index = VectorIndex(2)
        index.add(
            np.asarray([cls.chunk_id], dtype="int64"),
            np.asarray([[1.0, 0.0]], dtype="float32"),
        )
        index.save(cls.data_dir / "text.index")

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
        context = self.browser.new_context(viewport={"width": 1440, "height": 900})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(30_000)
        dialogs = []

        def dismiss_dialog(dialog):
            dialogs.append(dialog.message)
            dialog.dismiss()

        page.on("dialog", dismiss_dialog)
        page.goto(self.base_url, wait_until="domcontentloaded")
        main_tab = page.get_by_role("tab", name="検索・編集・切り抜き")
        main_tab.wait_for(state="visible")
        self.assertEqual(main_tab.get_attribute("aria-selected"), "true")

        card = page.locator(
            "#intuitive-video-card-grid .intuitive-video-card"
        ).first
        card.wait_for(state="visible")
        card.click()
        root = page.locator("#intuitive-toolbox [data-intuitive-root]")
        root.wait_for(state="visible")
        page.get_by_text("2. 文字クエリー検索", exact=True).wait_for(
            state="visible"
        )
        page.get_by_text("3. 文字起こし編集", exact=True).wait_for(
            state="visible"
        )
        self.assertEqual(
            root.get_attribute("data-transcript-start"),
            root.get_attribute("data-viewport-start"),
        )
        self.assertEqual(
            root.get_attribute("data-transcript-end"),
            root.get_attribute("data-viewport-end"),
        )
        search_target_input = page.locator("#intuitive-search-target input")
        search_target_input.wait_for(state="visible")
        self.assertIn("synthetic_fixture.mp4", search_target_input.input_value())

        # Exercise the real search callback without loading a model or touching
        # a user's index.  The server owns a two-dimensional synthetic FAISS
        # index and a deterministic in-process embedder for this test only.
        loaded_nonce = root.get_attribute("data-nonce")
        page.locator(
            "#intuitive-search-query textarea, #intuitive-search-query input"
        ).fill("alpha")
        page.locator(
            "button#intuitive-search-button, #intuitive-search-button button"
        ).click()
        search_results = page.locator("#intuitive-search-results")
        search_results.wait_for(state="visible")
        page.wait_for_function(
            "() => document.querySelector('#intuitive-search-results')"
            ".innerText.includes('alpha beta gamma delta')"
        )
        search_result_text = search_results.inner_text()
        self.assertIn("alpha beta gamma delta", search_result_text)
        self.assertEqual(root.get_attribute("data-nonce"), loaded_nonce)
        # Search and editor mutations use separate lanes. Results are inert
        # until the user explicitly selects a row.
        text_marker = page.locator(
            "[data-intuitive-search-marker-layer] "
            ".intuitive-search-marker[data-marker-kind='text']"
        ).first
        semantic_marker = page.locator(
            "[data-intuitive-search-marker-layer] "
            ".intuitive-search-marker[data-marker-kind='semantic']"
        ).first
        text_marker.wait_for(state="visible")
        semantic_marker.wait_for(state="visible")
        self.assertTrue(
            page.locator("[data-intuitive-search-marker-legend]").is_visible()
        )
        self.assertEqual(text_marker.get_attribute("aria-current"), "false")

        # Reproduce the transport race explicitly: after a newer query has
        # rendered, force the older request projection to arrive in the DOM.
        # The bounded rejected-ID set must clear it instead of reactivating it.
        projection_source = page.locator(
            "#intuitive-search-marker-projection "
            "[data-intuitive-search-marker-projection]"
        )
        old_projection = projection_source.get_attribute("data-search-markers")
        old_request_id = json.loads(old_projection)["request_id"]
        page.locator(
            "#intuitive-search-query textarea, #intuitive-search-query input"
        ).fill("beta")
        page.locator(
            "button#intuitive-search-button, #intuitive-search-button button"
        ).click()
        page.wait_for_function(
            "([selector, oldRequestId]) => { const source = "
            "document.querySelector(selector); if (!source) return false; "
            "try { return JSON.parse(source.dataset.searchMarkers).request_id "
            "!== oldRequestId; } catch (_) { return false; } }",
            arg=[
                "#intuitive-search-marker-projection "
                "[data-intuitive-search-marker-projection]",
                old_request_id,
            ],
        )
        semantic_marker.wait_for(state="visible")
        new_projection = projection_source.get_attribute("data-search-markers")
        page.evaluate(
            "([selector, projection]) => { document.querySelector(selector)"
            ".dataset.searchMarkers = projection; }",
            arg=[
                "#intuitive-search-marker-projection "
                "[data-intuitive-search-marker-projection]",
                old_projection,
            ],
        )
        page.wait_for_function(
            "() => document.querySelectorAll("
            "'[data-intuitive-search-marker-layer] .intuitive-search-marker'"
            ").length === 0"
        )
        self.assertEqual(root.get_attribute("data-nonce"), loaded_nonce)
        page.evaluate(
            "([selector, projection]) => { document.querySelector(selector)"
            ".dataset.searchMarkers = projection; }",
            arg=[
                "#intuitive-search-marker-projection "
                "[data-intuitive-search-marker-projection]",
                new_projection,
            ],
        )
        text_marker.wait_for(state="visible")
        semantic_marker.wait_for(state="visible")
        # Marker activation resolves its stable hit ID through the exact same
        # server selection helper as a Dataframe row.
        text_marker.click()
        page.wait_for_function(
            "([selector, nonce]) => { const root = document.querySelector(selector); "
            "return root && root.dataset.nonce !== nonce "
            "&& root.dataset.editDirty === 'false'; }",
            arg=["#intuitive-toolbox [data-intuitive-root]", loaded_nonce],
        )
        self.assertIn("● 1", search_results.inner_text())
        page.wait_for_function(
            "() => { const marker = document.querySelector("
            "'[data-intuitive-search-marker-layer] .intuitive-search-marker'); "
            "return marker && marker.getAttribute('aria-current') === 'true'; }"
        )
        # Simulate a late table repaint that restores the server's unselected
        # ○ prefix. The request-scoped selected hit must project ● back without
        # reopening or mutating the editor document.
        page.evaluate(
            """() => {
              const cell = document.querySelector(
                '#intuitive-search-results .body-cell, '
                + '#intuitive-search-results tbody td'
              );
              if (cell) {
                const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
                let node = walker.nextNode();
                while (node) {
                  if (/●\s*1/.test(node.nodeValue || '')) {
                    node.nodeValue = (node.nodeValue || '').replace('●', '○');
                    break;
                  }
                  node = walker.nextNode();
                }
              }
              document.dispatchEvent(new CustomEvent('cut-video:sync-search-markers'));
            }"""
        )
        page.wait_for_function(
            "() => document.querySelector('#intuitive-search-results')"
            ".innerText.includes('● 1')"
        )
        self.assertIn("synthetic_fixture.mp4", search_target_input.input_value())
        self.assertEqual(root.get_attribute("data-edit-dirty"), "false")
        self.assertEqual(root.get_attribute("data-can-undo"), "false")
        self.assertEqual(root.get_attribute("data-can-redo"), "false")
        overall_timeline_tab = page.get_by_role(
            "tab", name="① 全体を決める", exact=True
        )
        detail_timeline_tab = page.get_by_role(
            "tab", name="② 詳細編集（任意）", exact=True
        )
        self.assertEqual(overall_timeline_tab.get_attribute("aria-selected"), "true")
        detail_timeline_tab.click()
        self.assertEqual(detail_timeline_tab.get_attribute("aria-selected"), "true")
        zoom = page.locator("[data-intuitive-zoom]")
        self.assertAlmostEqual(
            float(zoom.get_attribute("data-overall-start")), 0.0, delta=0.001
        )
        self.assertAlmostEqual(
            float(zoom.get_attribute("data-overall-end")), 2.8, delta=0.001
        )
        page.locator("#intuitive-transcript-words").get_by_text(
            "alpha", exact=True
        ).wait_for(state="visible")
        preview_panel = page.locator("#intuitive-preview-video")
        mode_header = page.locator("#intuitive-header")
        self.assertIn("Source timeline", preview_panel.inner_text())
        self.assertTrue(
            mode_header.get_by_role(
                "button", name="編集結果を確認", exact=True
            ).is_visible()
        )
        self.assertFalse(
            mode_header.get_by_role(
                "button", name="元動画へ戻る", exact=True
            ).is_visible()
        )
        page.wait_for_function(
            """() => {
              const zoom = document.querySelector('#intuitive-zoom-timeline');
              const bars = Array.from(document.querySelectorAll('#intuitive-save-bar'));
              const save = bars.find((node) => getComputedStyle(node).position === 'fixed'
                && node.getClientRects().length);
              return zoom && save
                && zoom.getBoundingClientRect().bottom
                  <= save.getBoundingClientRect().top - 8;
            }"""
        )
        layout = page.evaluate("""() => {
          const selectors = ['#intuitive-header', '#intuitive-workspace-row',
            '#intuitive-preview-panel', '#intuitive-search-panel',
            '#intuitive-search-panel .intuitive-panel-heading',
            '#intuitive-search-panel .intuitive-search-primary',
            '#intuitive-search-panel .intuitive-search-options',
            '#intuitive-search-results',
            '#intuitive-search-results .table-wrap',
            '#intuitive-transcript-panel', '#intuitive-toolbox',
            '#intuitive-transcript-words', '#intuitive-edit-controls-row',
            '#intuitive-boundary-controls', '#intuitive-exclusion-panel',
            '#intuitive-overview-timeline', '#intuitive-zoom-timeline',
            '#intuitive-save-bar'];
          return Object.fromEntries(selectors.map((selector) => {
            const nodes = Array.from(document.querySelectorAll(selector));
            const node = nodes.find((item) => item.getClientRects().length
              && getComputedStyle(item).display !== 'none');
            if (!node) return [selector, null];
            const r = node.getBoundingClientRect();
            return [selector, {x: Math.round(r.x), y: Math.round(r.y),
              w: Math.round(r.width), h: Math.round(r.height),
              bottom: Math.round(r.bottom)}];
          }));
        }""")
        self.assertGreaterEqual(layout["#intuitive-preview-panel"]["w"], 480)
        self.assertGreaterEqual(layout["#intuitive-search-panel"]["w"], 400)
        self.assertGreaterEqual(layout["#intuitive-transcript-panel"]["w"], 280)
        self.assertLess(
            layout["#intuitive-preview-panel"]["x"],
            layout["#intuitive-search-panel"]["x"],
        )
        self.assertLess(
            layout["#intuitive-search-panel"]["x"],
            layout["#intuitive-transcript-panel"]["x"],
        )
        self.assertGreaterEqual(
            layout["#intuitive-search-results .table-wrap"]["h"], 140
        )
        self.assertLessEqual(
            layout["#intuitive-zoom-timeline"]["bottom"],
            layout["#intuitive-save-bar"]["y"] - 8,
        )
        self.assertLessEqual(layout["#intuitive-save-bar"]["bottom"], 900)

        def run_editor_command(action):
            revision = int(root.get_attribute("data-revision"))
            action()
            page.wait_for_function(
                "([selector, revision]) => { const root = document.querySelector(selector); "
                "return Number(root.dataset.revision) > revision "
                "&& root.dataset.lastCommandStatus === 'success'; }",
                arg=["#intuitive-toolbox [data-intuitive-root]", revision],
            )

        def choose_tool(tool):
            run_editor_command(
                lambda: page.locator(
                    f"#intuitive-toolbox [data-intuitive-tool='{tool}']"
                ).click()
            )

        def choose_word(start):
            run_editor_command(
                lambda: page.locator(
                    "#intuitive-transcript-words "
                    f".intuitive-word[data-start='{start:.3f}']"
                ).click()
            )

        # The overview tab exposes the same canonical boundary adjustment
        # commands as detail editing, without restoring the removed guide.
        overall_timeline_tab.click()
        self.assertEqual(
            page.locator("#intuitive-overall-range-guide").count(), 0
        )
        overall_step = page.locator(
            "[data-intuitive-overall-step][value='0.1']"
        )
        overall_step.check()
        overall_start_button = page.locator(
            "[data-intuitive-select-overall-boundary='overall_start']"
        )
        overall_end_button = page.locator(
            "[data-intuitive-select-overall-boundary='overall_end']"
        )
        adjust_before = page.locator("[data-intuitive-overall-adjust='-1']")
        adjust_after = page.locator("[data-intuitive-overall-adjust='1']")

        run_editor_command(overall_start_button.click)
        page.wait_for_function(
            "(selector) => document.querySelector(selector)"
            ".getAttribute('aria-pressed') === 'true'",
            arg="[data-intuitive-select-overall-boundary='overall_start']",
        )
        self.assertEqual(overall_start_button.get_attribute("aria-pressed"), "true")
        run_editor_command(adjust_after.click)
        self.assertAlmostEqual(
            float(zoom.get_attribute("data-overall-start")), 0.1, delta=0.001
        )
        run_editor_command(adjust_before.click)
        self.assertAlmostEqual(
            float(zoom.get_attribute("data-overall-start")), 0.0, delta=0.001
        )

        run_editor_command(overall_end_button.click)
        page.wait_for_function(
            "(selector) => document.querySelector(selector)"
            ".getAttribute('aria-pressed') === 'true'",
            arg="[data-intuitive-select-overall-boundary='overall_end']",
        )
        self.assertEqual(overall_end_button.get_attribute("aria-pressed"), "true")
        run_editor_command(adjust_before.click)
        self.assertAlmostEqual(
            float(zoom.get_attribute("data-overall-end")), 2.7, delta=0.001
        )
        run_editor_command(adjust_after.click)
        self.assertAlmostEqual(
            float(zoom.get_attribute("data-overall-end")), 2.8, delta=0.001
        )
        detail_timeline_tab.click()
        self.assertEqual(detail_timeline_tab.get_attribute("aria-selected"), "true")

        # Ordinary boundary/cut commands must retain the transcript DOM.  The
        # toolbar carries a compact canonical projection and JavaScript only
        # updates word decoration classes instead of replacing all words.
        transcript_dom_token = "persist-through-boundary-command"
        page.locator("#intuitive-transcript-words").evaluate(
            "(node, token) => node.dataset.domToken = token",
            transcript_dom_token,
        )

        # Two overlapping cut gestures must collapse into one canonical range.
        # Each completed gesture is a single history entry even though its
        # start and end are chosen by separate browser commands.
        choose_tool("exclude_start")
        choose_word(0.70)
        self.assertEqual(root.get_attribute("data-active-tool"), "exclude_end")
        self.assertEqual(
            page.locator("#intuitive-transcript-words").get_attribute(
                "data-dom-token"
            ),
            transcript_dom_token,
        )
        page.wait_for_function(
            '''() => document.querySelector(
              "#intuitive-transcript-words .intuitive-word[data-start='0.700']"
            ).classList.contains("marks-pending-cut")'''
        )
        self.assertIn(
            "marks-pending-cut",
            page.locator(
                "#intuitive-transcript-words "
                ".intuitive-word[data-start='0.700']"
            ).get_attribute("class"),
        )
        choose_word(1.25)
        first_cut = page.locator(
            "[data-intuitive-zoom] .intuitive-cut-zone"
        )
        self.assertEqual(first_cut.count(), 1)
        self.assertAlmostEqual(
            float(first_cut.locator("[data-boundary-kind='exclusion_start']")
                  .get_attribute("data-boundary-time")),
            0.70,
            delta=0.001,
        )
        self.assertAlmostEqual(
            float(first_cut.locator("[data-boundary-kind='exclusion_end']")
                  .get_attribute("data-boundary-time")),
            1.65,
            delta=0.001,
        )
        page.wait_for_function(
            '''() => document.querySelector(
              "#intuitive-transcript-words .intuitive-word[data-start='0.700']"
            ).classList.contains("is-excluded-word")'''
        )
        self.assertIn(
            "is-excluded-word",
            page.locator(
                "#intuitive-transcript-words "
                ".intuitive-word[data-start='0.700']"
            ).get_attribute("class"),
        )

        choose_tool("exclude_start")
        choose_word(1.25)
        choose_word(1.90)
        merged_cut = page.locator(
            "[data-intuitive-zoom] .intuitive-cut-zone"
        )
        self.assertEqual(merged_cut.count(), 1)
        self.assertAlmostEqual(
            float(merged_cut.locator("[data-boundary-kind='exclusion_start']")
                  .get_attribute("data-boundary-time")),
            0.70,
            delta=0.001,
        )
        self.assertAlmostEqual(
            float(merged_cut.locator("[data-boundary-kind='exclusion_end']")
                  .get_attribute("data-boundary-time")),
            2.35,
            delta=0.001,
        )
        self.assertIn(
            "途中カット一覧（1箇所）",
            page.locator("#intuitive-exclusion-list").inner_text(),
        )

        run_editor_command(
            lambda: page.locator("[data-intuitive-history='undo']").click()
        )
        undone_cut = page.locator("[data-intuitive-zoom] .intuitive-cut-zone")
        self.assertEqual(undone_cut.count(), 1)
        self.assertAlmostEqual(
            float(undone_cut.locator("[data-boundary-kind='exclusion_end']")
                  .get_attribute("data-boundary-time")),
            1.65,
            delta=0.001,
        )
        run_editor_command(
            lambda: page.locator("[data-intuitive-history='redo']").click()
        )
        redone_cut = page.locator("[data-intuitive-zoom] .intuitive-cut-zone")
        self.assertEqual(redone_cut.count(), 1)
        self.assertAlmostEqual(
            float(redone_cut.locator("[data-boundary-kind='exclusion_end']")
                  .get_attribute("data-boundary-time")),
            2.35,
            delta=0.001,
        )

        # Synchronize a non-zero source playhead through the regular command
        # bridge, then verify result preview and source restoration preserve it.
        page.wait_for_function(
            "() => { const video = document.querySelector("
            "'#intuitive-preview-video video'); return video "
            "&& video.readyState >= 1 && Number.isFinite(video.duration); }"
        )
        page.evaluate(
            """async () => {
              const video = document.querySelector('#intuitive-preview-video video');
              video.currentTime = 2.6;
              video.dispatchEvent(new Event('seeking', {bubbles: true}));
              await new Promise(requestAnimationFrame);
              await new Promise(requestAnimationFrame);
            }"""
        )
        choose_tool("overall_end")
        source_playhead_left = float(page.locator(
            "[data-intuitive-zoom] .intuitive-playhead"
        ).evaluate("node => parseFloat(node.style.left)"))
        source_video_src = page.locator(
            "#intuitive-preview-video video"
        ).get_attribute("src")

        page.locator(
            "button#intuitive-preview-result, #intuitive-preview-result button"
        ).click()
        page.wait_for_function(
            "() => { const root = document.querySelector("
            "'#intuitive-toolbox [data-intuitive-root]'); const video = "
            "document.querySelector('#intuitive-preview-video video'); "
            "return root.dataset.previewMode === 'result' && video "
            "&& video.readyState >= 1 && Number.isFinite(video.duration); }"
        )
        self.assertIn("Result timeline", preview_panel.inner_text())
        self.assertFalse(
            mode_header.get_by_role(
                "button", name="編集結果を確認", exact=True
            ).is_visible()
        )
        self.assertTrue(
            mode_header.get_by_role(
                "button", name="元動画へ戻る", exact=True
            ).is_visible()
        )
        self.assertNotEqual(
            page.locator("#intuitive-preview-video video").get_attribute("src"),
            source_video_src,
        )
        result_duration = float(
            page.locator("#intuitive-preview-video video").evaluate(
                "video => video.duration"
            )
        )
        self.assertAlmostEqual(result_duration, 1.15, delta=0.35)
        page.locator(
            "button#intuitive-return-source, #intuitive-return-source button"
        ).click()
        page.wait_for_function(
            "() => document.querySelector("
            "'#intuitive-toolbox [data-intuitive-root]').dataset.previewMode === 'source'"
        )
        self.assertIn("Source timeline", preview_panel.inner_text())
        self.assertTrue(
            mode_header.get_by_role(
                "button", name="編集結果を確認", exact=True
            ).is_visible()
        )
        self.assertFalse(
            mode_header.get_by_role(
                "button", name="元動画へ戻る", exact=True
            ).is_visible()
        )
        restored_playhead_left = float(page.locator(
            "[data-intuitive-zoom] .intuitive-playhead"
        ).evaluate("node => parseFloat(node.style.left)"))
        # Result currentTime=0 maps through TimelineMap to the first kept
        # Source boundary, rather than reusing the stale pre-preview playhead.
        self.assertAlmostEqual(restored_playhead_left, 0.0, delta=0.5)

        # Finish the full user path with a real ffmpeg save into the isolated
        # E2E root.  Save establishes the current plan as the clean baseline.
        save_dir = self.root / "saved-clips"
        save_bar = page.locator("#intuitive-save-bar:visible").first
        save_bar.wait_for(state="visible")
        out_dir_input = save_bar.locator(
            "#intuitive-out-dir textarea, #intuitive-out-dir input"
        )
        filename_input = save_bar.locator(
            "#intuitive-filename textarea, #intuitive-filename input"
        )
        saved_path_input = save_bar.locator(
            "#intuitive-saved-path textarea, #intuitive-saved-path input"
        )
        out_dir_input.fill(str(save_dir))
        filename_input.fill("phase0_browser_e2e")
        save_bar.get_by_text("SRT字幕も保存", exact=True).click()
        save_bar.get_by_role("button", name="保存", exact=True).click()
        page.wait_for_function(
            "() => { const input = Array.from(document.querySelectorAll("
            "'#intuitive-saved-path textarea, #intuitive-saved-path input'))"
            ".find(node => node.offsetParent !== null); "
            "return input && input.value.endsWith('.mp4'); }",
        )
        page.wait_for_function(
            "() => document.querySelector("
            "'#intuitive-toolbox [data-intuitive-root]').dataset.editDirty === 'false'"
        )
        saved_path = Path(saved_path_input.input_value())
        self.assertEqual(saved_path, (save_dir / "phase0_browser_e2e.mp4").resolve())
        self.assertTrue(saved_path.is_file())
        self.assertGreater(saved_path.stat().st_size, 0)
        self.assertTrue(saved_path.with_suffix(".srt").is_file())
        self.assertTrue(saved_path.with_suffix(".mp4.manifest.json").is_file())
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(saved_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertAlmostEqual(float(probe.stdout.strip()), 1.15, delta=0.35)

        # Saving changes only the clean reference. Existing history remains
        # usable, and redoing the saved semantic plan becomes clean again.
        run_editor_command(
            lambda: page.locator("[data-intuitive-history='undo']").click()
        )
        self.assertEqual(root.get_attribute("data-edit-dirty"), "true")
        run_editor_command(
            lambda: page.locator("[data-intuitive-history='redo']").click()
        )
        self.assertEqual(root.get_attribute("data-edit-dirty"), "false")

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

        # Switching timeline views is presentation-only: the same canonical
        # state and history remain active while the overview is manipulated.
        state_before_tab_switch = {
            name: root.get_attribute(name)
            for name in (
                "data-active-tool", "data-has-selected-boundary",
                "data-can-undo", "data-can-redo", "data-edit-dirty",
            )
        }
        revision_before_tab_switch = int(root.get_attribute("data-revision"))
        overall_timeline_tab.click()
        self.assertEqual(
            int(root.get_attribute("data-revision")), revision_before_tab_switch
        )
        for name, value in state_before_tab_switch.items():
            self.assertEqual(root.get_attribute(name), value)

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
        detail_timeline_tab.click()
        revision = root.get_attribute("data-revision")

        # A search refresh is transient and must not warn about losing a dirty
        # edit. Only selecting a result/marker replaces the edit document.
        before_dialogs = len(dialogs)
        page.locator(
            "button#intuitive-search-button, #intuitive-search-button button"
        ).click()
        page.wait_for_timeout(250)
        self.assertEqual(len(dialogs), before_dialogs)
        self.assertEqual(root.get_attribute("data-revision"), revision)

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

        # Labels and legends are informative only.  A click outside the actual
        # zoom track must neither seek nor enqueue an edit command.
        page.wait_for_timeout(350)
        passive_revision = int(root.get_attribute("data-revision"))
        passive_left = float(page.locator(
            "[data-intuitive-zoom] .intuitive-playhead"
        ).evaluate("node => parseFloat(node.style.left)"))
        page.locator(
            "[data-intuitive-zoom] .intuitive-timeline-scale"
        ).click()
        page.locator(
            "[data-intuitive-zoom] .intuitive-timeline-legend"
        ).click()
        page.wait_for_timeout(150)
        self.assertEqual(int(root.get_attribute("data-revision")), passive_revision)
        self.assertAlmostEqual(
            float(page.locator(
                "[data-intuitive-zoom] .intuitive-playhead"
            ).evaluate("node => parseFloat(node.style.left)")),
            passive_left,
            delta=0.01,
        )

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

        # Global keyboard handlers and the fixed save bar belong only to the
        # visible intuitive tab.  Visiting another tab must not undo the
        # hidden document.
        hidden_revision = int(root.get_attribute("data-revision"))
        hidden_dirty = root.get_attribute("data-edit-dirty")
        page.get_by_role("tab", name="動画保存").click()
        self.assertEqual(page.locator("#intuitive-save-bar:visible").count(), 0)
        page.keyboard.press("Control+Z")
        page.wait_for_timeout(150)
        page.get_by_role("tab", name="検索・編集・切り抜き").click()
        self.assertEqual(int(root.get_attribute("data-revision")), hidden_revision)
        self.assertEqual(root.get_attribute("data-edit-dirty"), hidden_dirty)

        self.assertIsNone(self.server.poll())
        server_log = (self.root / "server.log").read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertNotIn("Traceback (most recent call last)", server_log)
        self.assertNotIn("access violation", server_log.casefold())
        leftovers = [
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file()
            and (
                path.name.lower().endswith((".tmp", ".tmp.mp4", ".part"))
                or path.name.lower() == "concat.txt"
            )
        ]
        self.assertEqual(leftovers, [])

    @unittest.skipUnless(
        os.environ.get("CUT_VIDEO_RUN_BROWSER_PERF") == "1",
        "600-second browser performance measurement is opt-in",
    )
    def test_transcript_projection_600_second_performance(self):
        """Measure dense synthetic transcript DOM render and production projection."""
        previous_data_dir = os.environ.get("CUT_VIDEO_DATA_DIR")
        os.environ["CUT_VIDEO_DATA_DIR"] = str(self.data_dir)
        try:
            import app as app_module
        finally:
            if previous_data_dir is None:
                os.environ.pop("CUT_VIDEO_DATA_DIR", None)
            else:
                os.environ["CUT_VIDEO_DATA_DIR"] = previous_data_dir

        segments = []
        for second in range(600):
            words = []
            for index in range(5):
                start = second + 0.04 + index * 0.18
                words.append({
                    "word": f"w{second:03d}_{index}",
                    "start": start,
                    "end": start + 0.14,
                })
            segments.append({
                "start_sec": float(second),
                "end_sec": second + 0.95,
                "text": " ".join(word["word"] for word in words),
                "words_json": json.dumps(words, separators=(",", ":")),
            })

        state_a = {
            "overall_start": 20.0,
            "overall_end": 580.0,
            "viewport_start": 0.0,
            "viewport_end": 600.0,
            "exclusions": [
                {"id": "cut-a", "start": 80.0, "end": 90.0},
                {"id": "cut-b", "start": 200.0, "end": 215.0},
                {"id": "cut-c", "start": 330.0, "end": 345.0},
                {"id": "cut-d", "start": 500.0, "end": 510.0},
            ],
            "selected_word": {"start": 321.04, "end": 321.18},
            "pending_cut_start": 420.22,
        }
        state_b = {
            **state_a,
            "overall_start": 25.0,
            "overall_end": 575.0,
            "selected_word": {"start": 322.22, "end": 322.36},
            "pending_cut_start": 421.40,
        }
        transcript_html = app_module.render_intuitive_transcript(segments, state_a)
        projections = [
            json.loads(app_module._intuitive_transcript_projection(state_a)),
            json.loads(app_module._intuitive_transcript_projection(state_b)),
        ]

        context = self.browser.new_context(viewport={"width": 1440, "height": 900})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(30_000)
        page.goto(self.base_url, wait_until="domcontentloaded")
        page.locator("#intuitive-transcript-words").wait_for(state="visible")
        page.wait_for_function(
            "() => typeof globalThis.__cutVideoDiagnostics"
            ".applyTranscriptProjection === 'function'"
        )
        browser_metrics = page.evaluate(
            """async ({html, projections, warmups, samples}) => {
              const host = document.getElementById('intuitive-transcript-words');
              const renderOnce = () => {
                host.replaceChildren();
                const started = performance.now();
                host.innerHTML = html;
                void host.scrollHeight;
                return performance.now() - started;
              };
              await new Promise(requestAnimationFrame);
              const coldRenderMs = renderOnce();
              for (let index = 0; index < warmups; index += 1) renderOnce();
              const renderSamplesMs = [];
              for (let index = 0; index < samples; index += 1) {
                renderSamplesMs.push(renderOnce());
              }
              for (let index = 0; index < warmups; index += 1) {
                globalThis.__cutVideoDiagnostics.applyTranscriptProjection(
                  projections[index % projections.length]
                );
                void host.offsetHeight;
              }
              const projectionSamplesMs = [];
              for (let index = 0; index < samples; index += 1) {
                const started = performance.now();
                globalThis.__cutVideoDiagnostics.applyTranscriptProjection(
                  projections[index % projections.length]
                );
                void host.offsetHeight;
                projectionSamplesMs.push(performance.now() - started);
              }
              return {
                coldRenderMs,
                renderSamplesMs,
                projectionSamplesMs,
                wordCount: host.querySelectorAll('.intuitive-word').length,
                userAgent: navigator.userAgent
              };
            }""",
            arg={
                "html": transcript_html,
                "projections": projections,
                "warmups": 5,
                "samples": 20,
            },
        )

        def percentile_95(values):
            ordered = sorted(float(value) for value in values)
            return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]

        metrics = {
            "duration_sec": 600,
            "word_count": browser_metrics["wordCount"],
            "html_utf8_bytes": len(transcript_html.encode("utf-8")),
            "cold_render_ms": round(float(browser_metrics["coldRenderMs"]), 3),
            "warm_render_median_ms": round(
                statistics.median(browser_metrics["renderSamplesMs"]), 3
            ),
            "warm_render_p95_ms": round(
                percentile_95(browser_metrics["renderSamplesMs"]), 3
            ),
            "projection_median_ms": round(
                statistics.median(browser_metrics["projectionSamplesMs"]), 3
            ),
            "projection_p95_ms": round(
                percentile_95(browser_metrics["projectionSamplesMs"]), 3
            ),
            "render_samples_ms": [
                round(float(value), 3) for value in browser_metrics["renderSamplesMs"]
            ],
            "projection_samples_ms": [
                round(float(value), 3)
                for value in browser_metrics["projectionSamplesMs"]
            ],
            "user_agent": browser_metrics["userAgent"],
        }
        self.assertEqual(metrics["word_count"], 3000)
        self.assertEqual(len(metrics["render_samples_ms"]), 20)
        self.assertEqual(len(metrics["projection_samples_ms"]), 20)
        self.assertTrue(all(value >= 0 for value in metrics["render_samples_ms"]))
        self.assertTrue(all(value >= 0 for value in metrics["projection_samples_ms"]))
        print("TRANSCRIPT_PERF_JSON=" + json.dumps(metrics, ensure_ascii=False))

    def test_responsive_layout_has_no_horizontal_overflow(self):
        for width, height in ((1024, 768), (760, 900)):
            with self.subTest(viewport=(width, height)):
                context = self.browser.new_context(
                    viewport={"width": width, "height": height}
                )
                try:
                    page = context.new_page()
                    page.set_default_timeout(30_000)
                    page.goto(self.base_url, wait_until="domcontentloaded")
                    page.get_by_role("tab", name="検索・編集・切り抜き").click()
                    page.locator(
                        "#intuitive-video-card-grid .intuitive-video-card"
                    ).first.click()
                    page.locator(
                        "#intuitive-toolbox [data-intuitive-root]"
                    ).wait_for(state="visible")
                    page.get_by_role(
                        "tab", name="② 詳細編集（任意）", exact=True
                    ).click()
                    page.wait_for_timeout(250)

                    layout = page.evaluate("""() => {
                      const visibleRect = (selector) => {
                        const node = Array.from(document.querySelectorAll(selector))
                          .find((item) => item.getClientRects().length
                            && getComputedStyle(item).display !== 'none');
                        if (!node) return null;
                        const r = node.getBoundingClientRect();
                        return {x: r.x, y: r.y, right: r.right,
                          bottom: r.bottom, width: r.width};
                      };
                      const documentElement = document.documentElement;
                      return {
                        clientWidth: documentElement.clientWidth,
                        scrollWidth: Math.max(documentElement.scrollWidth,
                          document.body ? document.body.scrollWidth : 0),
                        header: visibleRect('#intuitive-header'),
                        mode: visibleRect('#intuitive-mode-row'),
                        workspace: visibleRect('#intuitive-workspace-row'),
                        preview: visibleRect('#intuitive-preview-panel'),
                        transcript: visibleRect('#intuitive-transcript-panel'),
                        search: visibleRect('#intuitive-search-panel'),
                        boundary: visibleRect('#intuitive-boundary-controls'),
                        timelineTools: visibleRect('.intuitive-timeline-toolbox'),
                        save: visibleRect('#intuitive-save-bar')
                      };
                    }""")
                    self.assertLessEqual(
                        layout["scrollWidth"], layout["clientWidth"] + 2
                    )
                    for name in (
                        "header", "mode", "workspace", "preview", "transcript",
                        "search", "boundary", "timelineTools", "save",
                    ):
                        rect = layout[name]
                        self.assertIsNotNone(rect, name)
                        self.assertGreaterEqual(rect["x"], -2, name)
                        self.assertLessEqual(
                            rect["right"], layout["clientWidth"] + 2, name
                        )

                    if width == 1024:
                        self.assertAlmostEqual(
                            layout["preview"]["y"], layout["search"]["y"],
                            delta=3,
                        )
                        self.assertGreaterEqual(
                            layout["transcript"]["y"],
                            max(layout["preview"]["bottom"],
                                layout["search"]["bottom"]) - 2,
                        )
                    else:
                        self.assertGreaterEqual(
                            layout["search"]["y"],
                            layout["preview"]["bottom"] - 2,
                        )
                        self.assertGreaterEqual(
                            layout["transcript"]["y"],
                            layout["search"]["bottom"] - 2,
                        )
                finally:
                    context.close()


if __name__ == "__main__":
    unittest.main()
