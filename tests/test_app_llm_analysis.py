import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


_DATA = tempfile.TemporaryDirectory(prefix="cut_app_llm_data_")
unittest.addModuleCleanup(_DATA.cleanup)
os.environ["CUT_VIDEO_DATA_DIR"] = _DATA.name

import app


class _Connection:
    def close(self):
        pass


class AppLlmAnalysisTest(unittest.TestCase):
    def test_highlight_generation_uses_mode_radio_and_inline_candidate_bridge(self):
        radio_labels = [
            (component.get("props") or {}).get("label")
            for component in app.demo.config.get("components", [])
            if component.get("type") == "radio"
        ]
        self.assertIn("候補の作り方", radio_labels)
        self.assertIn("保存対象", radio_labels)
        self.assertIn("出力形式", radio_labels)
        components_by_elem_id = {
            (component.get("props") or {}).get("elem_id"): component
            for component in app.demo.config.get("components", [])
        }
        self.assertEqual(
            components_by_elem_id["highlight-candidate-selection"]["type"],
            "textbox",
        )
        self.assertIn("highlight-candidate-inline", app._INTUITIVE_EDITOR_JS)
        self.assertIn("#highlight-candidate-selection", app._APP_CSS)

    def test_query_generation_dispatches_without_reanalyzing_summary(self):
        with (
            patch.object(
                app,
                "_create_query_highlight_run",
                return_value={"candidate_count": 2},
            ) as create_query,
            patch.object(
                app,
                "_latest_highlight_view",
                return_value=("query candidates", [("first", "candidate-1")]),
            ),
        ):
            outputs = list(app.do_highlight_generation(
                "query",
                "vid_synthetic",
                "unused-model",
                "環境改善について説明している場面",
                3,
                20.0,
                90.0,
            ))

        create_query.assert_called_once()
        self.assertEqual(len(outputs), 2)
        self.assertIn("2件", outputs[-1][0])
        self.assertEqual(outputs[-1][1], "query candidates")
        self.assertEqual(outputs[-1][2]["value"], "candidate-1")

    def test_highlight_export_can_atomically_save_all_visible_candidates(self):
        video = {
            "path": "synthetic.mp4",
            "display_name": "synthetic:source.mp4",
            "duration": 120.0,
        }
        candidates = [
            {
                "highlight_candidate_id": "candidate-1",
                "start_sec": 10.0,
                "end_sec": 20.0,
                "export_title": "導入/最初の話題",
            },
            {
                "highlight_candidate_id": "candidate-2",
                "start_sec": 30.0,
                "end_sec": 45.0,
                "export_title": "本題：詳しい説明",
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            def fake_cut(_source, _start, _end, output, **_kwargs):
                Path(output).write_bytes(b"synthetic-video")

            with (
                patch.object(
                    app,
                    "_highlight_export_context",
                    return_value=(video, candidates),
                ),
                patch.object(app, "cut_clip", side_effect=fake_cut),
            ):
                outputs = list(app.export_highlight_candidates(
                    "vid_synthetic",
                    "candidate-1",
                    "all",
                    temporary,
                    True,
                ))

            saved = outputs[-1][1]
            self.assertEqual(len(saved), 2)
            self.assertTrue(all(Path(path).is_file() for path in saved))
            self.assertEqual(
                [Path(path).name for path in saved],
                [
                    "synthetic_source_導入_最初の話題.mp4",
                    "synthetic_source_本題：詳しい説明.mp4",
                ],
            )
            self.assertFalse(list(Path(temporary).glob("*.partial.mp4")))

    def test_highlight_export_can_render_captioned_short_video(self):
        video = {
            "path": "synthetic.mp4",
            "display_name": "synthetic.mp4",
            "duration": 120.0,
            "public_video_id": "vid_synthetic",
        }
        candidate = {
            "highlight_candidate_id": "candidate-1",
            "start_sec": 10.0,
            "end_sec": 25.0,
            "export_title": "要点",
        }
        with tempfile.TemporaryDirectory() as temporary:
            def fake_render(_source, _start, _end, output, **_kwargs):
                Path(output).write_bytes(b"vertical-video")

            with (
                patch.object(
                    app, "_highlight_export_context", return_value=(video, [candidate]),
                ),
                patch.object(
                    app, "_highlight_short_captions", return_value=((object(),), []),
                ) as caption_mapper,
                patch.object(app, "render_short_clip", side_effect=fake_render) as renderer,
            ):
                outputs = list(app.export_highlight_candidates(
                    "vid_synthetic", "candidate-1", "selected", temporary, True,
                    "short", "blur", "720x1280", True,
                ))

            saved = outputs[-1][1]
            self.assertEqual(
                [Path(path).name for path in saved],
                ["synthetic_要点_short.mp4"],
            )
            caption_mapper.assert_called_once_with(video, candidate)
            options = renderer.call_args.kwargs["options"]
            self.assertEqual(
                (options.width, options.height, options.layout),
                (720, 1280, "blur"),
            )
            self.assertTrue(options.burn_captions)

    def test_highlight_publish_never_replaces_an_unexpected_existing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged = root / "staged.mp4"
            destination = root / "result.mp4"
            staged.write_bytes(b"new")
            destination.write_bytes(b"existing")

            with self.assertRaisesRegex(app.gr.Error, "同名ファイル"):
                app._publish_highlight_without_overwrite(staged, destination)

            self.assertEqual(destination.read_bytes(), b"existing")
            self.assertEqual(staged.read_bytes(), b"new")

    def test_short_captions_use_the_highlight_revision_snapshot(self):
        video = {
            "public_video_id": "vid_synthetic",
            "duration": 120.0,
            "_highlight_transcript_revision": "revision-from-highlight",
        }
        candidate = {"start_sec": 10.0, "end_sec": 20.0}
        with (
            patch.object(app.db, "get_conn", return_value=_Connection()),
            patch.object(app.db, "get_segments_in_range", return_value=[]) as rows,
            patch.object(app.db, "get_active_transcript_revision") as active_revision,
        ):
            captions, warnings = app._highlight_short_captions(video, candidate)

        self.assertEqual(captions, ())
        self.assertEqual(warnings, [])
        active_revision.assert_not_called()
        self.assertEqual(
            rows.call_args.kwargs["transcript_revision"],
            "revision-from-highlight",
        )

    def test_highlight_filename_parts_are_windows_safe(self):
        self.assertEqual(
            app._safe_highlight_filename_part(
                '章: まとめ/結論?*', fallback="見どころ", max_length=80
            ),
            "章_ まとめ_結論__",
        )
        self.assertEqual(
            app._safe_highlight_filename_part(
                "CON", fallback="動画", max_length=80
            ),
            "_CON",
        )

    def test_highlight_export_uses_source_chapter_title(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.mp4"
            source.write_bytes(b"video")
            with (
                patch.object(app, "parse_video_choice", return_value="vid_synthetic"),
                patch.object(app.db, "get_conn", return_value=_Connection()),
                patch.object(app.db, "init_db"),
                patch.object(
                    app.db,
                    "get_active_transcript_revision",
                    return_value="revision-1",
                ),
                patch.object(
                    app.db,
                    "get_latest_ready_highlight_run",
                    return_value={
                        "highlight_run_id": "highlight-1",
                        "analysis_run_id": "analysis-1",
                    },
                ),
                patch.object(
                    app.db,
                    "get_highlight_candidates",
                    return_value=[{
                        "highlight_candidate_id": "candidate-1",
                        "source_chapter_ordinal": 2,
                        "title": "candidate title",
                    }],
                ),
                patch.object(
                    app.db,
                    "get_analysis_chapters",
                    return_value=[{"ordinal": 2, "title": "chapter title"}],
                ),
                patch.object(
                    app.db,
                    "get_video",
                    return_value={"path": str(source), "display_name": "source.mp4"},
                ),
            ):
                context_video, candidates = app._highlight_export_context("synthetic")

        self.assertEqual(candidates[0]["export_title"], "chapter title")
        self.assertEqual(
            context_video["_highlight_transcript_revision"], "revision-1"
        )

    def test_llm_summary_and_highlights_have_a_dedicated_ordered_tab(self):
        tab_labels = [
            (component.get("props") or {}).get("label")
            for component in app.demo.config.get("components", [])
            if component.get("type") == "tabitem"
        ]
        self.assertIn("LLM要約・見どころ", tab_labels)
        visible_top_tabs = [
            "検索・編集・切り抜き",
            "LLM要約・見どころ",
            "動画保存",
            "インデックスの共有",
        ]
        self.assertEqual(
            sorted(visible_top_tabs, key=tab_labels.index),
            visible_top_tabs,
        )
        summary_index = tab_labels.index("① 要約を作る・確認する")
        highlight_index = tab_labels.index("② 要約から見どころを作る・切り抜く")
        self.assertLess(summary_index, highlight_index)

        components = app.demo.config.get("components", [])
        component_ids = {
            (component.get("props") or {}).get("elem_id")
            for component in components
        }
        self.assertIn("llm-summary-video-select", component_ids)
        self.assertIn("llm-highlight-video-select", component_ids)
        self.assertIn("llm-summary-video-card-grid", component_ids)
        self.assertIn("llm-highlight-video-card-grid", component_ids)
        self.assertIn("#llm-summary-video-card-command", app._INTUITIVE_EDITOR_JS)
        self.assertIn("#llm-highlight-video-card-command", app._INTUITIVE_EDITOR_JS)
        self.assertIn("is-summary-missing", app._APP_CSS)

    def test_llm_thumbnail_card_selects_stable_video_id(self):
        with patch.object(
            app,
            "build_llm_video_cards",
            return_value='<button class="intuitive-video-card is-selected">card</button>',
        ) as build_cards:
            video_id, cards = app.select_llm_video_from_card(
                '{"video_id":"vid_synthetic","request_id":"request-1"}',
                "filter",
            )

        self.assertEqual(video_id, "vid_synthetic")
        self.assertIn("is-selected", cards)
        build_cards.assert_called_once_with(
            "filter", "vid_synthetic", generate_thumbnails=False
        )

    def test_llm_cards_show_and_dim_saved_summary_state(self):
        cards = [
            {
                "video_id": "video-ready",
                "name": "ready.mp4",
                "duration": 60.0,
                "thumbnail_url": "ready.jpg",
                "asr_complete": True,
                "indexed": True,
                "summary_ready": True,
            },
            {
                "video_id": "video-missing",
                "name": "missing.mp4",
                "duration": 90.0,
                "thumbnail_url": "missing.jpg",
                "asr_complete": True,
                "indexed": True,
                "summary_ready": False,
            },
        ]

        rendered = app.render_intuitive_video_cards(cards)

        self.assertIn("is-summary-ready", rendered)
        self.assertIn("is-summary-missing", rendered)
        self.assertIn("要約済み", rendered)
        self.assertIn("未要約", rendered)

    def test_output_and_export_locations_open_without_a_shell(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "clips"
            exported = root / "exports" / "index.zip"
            exported.parent.mkdir()
            exported.write_bytes(b"zip")
            with patch.object(app.subprocess, "Popen") as popen:
                folder_status = app.open_output_folder(str(output_dir))
                export_status = app.open_exported_index_location(
                    f"保存先: {exported}"
                )
                output_created = output_dir.is_dir()

        self.assertTrue(output_created)
        self.assertIn("保存フォルダ", folder_status)
        self.assertIn("インデックス", export_status)
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(popen.call_args_list[0].args[0][0], "explorer.exe")
        self.assertTrue(
            popen.call_args_list[1].args[0][1].startswith("/select,")
        )

    def test_saved_summary_enables_highlight_generation_without_reanalysis(self):
        with (
            patch.object(
                app,
                "format_latest_llm_analysis",
                return_value="saved summary",
            ),
            patch.object(app, "_has_ready_llm_analysis", return_value=True),
            patch.object(
                app,
                "_latest_highlight_view",
                return_value=("saved candidates", [("candidate", "candidate-1")]),
            ),
        ):
            outputs = app.load_summary_highlight_workspace("vid_synthetic")

        self.assertEqual(outputs[0], "saved summary")
        self.assertIn("再要約せず", outputs[1])
        self.assertEqual(outputs[2], "saved candidates")
        self.assertEqual(outputs[3]["value"], "candidate-1")
        self.assertTrue(outputs[4]["interactive"])

    def test_video_without_saved_summary_keeps_highlight_generation_disabled(self):
        with (
            patch.object(
                app,
                "format_latest_llm_analysis",
                return_value="no summary",
            ),
            patch.object(app, "_has_ready_llm_analysis", return_value=False),
        ):
            outputs = app.load_summary_highlight_workspace("vid_synthetic")

        self.assertIn("保存済み要約がない", outputs[1])
        self.assertEqual(outputs[3]["value"], "")
        self.assertFalse(outputs[4]["interactive"])

    def test_latest_ready_analysis_is_escaped_and_time_linked(self):
        ready = {
            "analysis_run_id": "analysis-ready",
            "status": "ready",
            "summary": "<script>summary</script>",
            "tags": ["topic"],
            "model": "synthetic-model",
            "prompt_version": "transcript-analysis-v3",
            "result": {
                "window_count": 3,
                "chapter_count": 1,
                "segment_coverage_ratio": 1.0,
            },
        }
        with (
            patch.object(app, "parse_video_choice", return_value="vid_synthetic"),
            patch.object(app.db, "get_conn", return_value=_Connection()),
            patch.object(app.db, "init_db"),
            patch.object(
                app.db,
                "get_active_transcript_revision",
                return_value="revision-1",
            ),
            patch.object(app.db, "list_analysis_runs", return_value=[ready]),
            patch.object(
                app.db,
                "get_analysis_chapters",
                return_value=[{
                    "start_sec": 10.0,
                    "end_sec": 20.0,
                    "title": "chapter",
                    "summary": "summary",
                    "tags": ["topic"],
                }],
            ),
        ):
            rendered = app.format_latest_llm_analysis("synthetic")

        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;summary&lt;/script&gt;", rendered)
        self.assertIn("00:00:10", rendered)
        self.assertIn("00:00:20", rendered)
        self.assertIn("chapter", rendered)
        self.assertIn("100.0%", rendered)
        self.assertIn("transcript-analysis-v3", rendered)
        self.assertIn("synthetic-model", rendered)

    def test_latest_failure_is_shown_above_last_ready_result(self):
        failed = {
            "analysis_run_id": "analysis-failed",
            "status": "failed",
            "error_message": "offline",
        }
        ready = {
            "analysis_run_id": "analysis-ready",
            "status": "ready",
            "summary": "previous summary",
            "tags": [],
        }
        with (
            patch.object(app, "parse_video_choice", return_value="vid_synthetic"),
            patch.object(app.db, "get_conn", return_value=_Connection()),
            patch.object(app.db, "init_db"),
            patch.object(
                app.db,
                "get_active_transcript_revision",
                return_value="revision-1",
            ),
            patch.object(
                app.db,
                "list_analysis_runs",
                return_value=[failed, ready],
            ),
            patch.object(app.db, "get_analysis_chapters", return_value=[]),
        ):
            rendered = app.format_latest_llm_analysis("synthetic")

        self.assertIn("offline", rendered)
        self.assertIn("previous summary", rendered)

    def test_latest_highlights_are_escaped_and_return_stable_candidate_choices(self):
        ready = {
            "highlight_run_id": "highlight-ready",
            "status": "ready",
            "requested_count": 3,
            "result": {
                "requested_count": 3,
                "duration_min": 20.0,
                "duration_median": 25.0,
                "duration_max": 30.0,
                "overlap_suppressed_count": 1,
                "boundary_expanded_count": 2,
                "boundary_warning_count": 1,
                "below_min_duration_count": 0,
                "all_segment_linked": True,
                "invalid_segment_count": 2,
            },
        }
        candidates = [{
            "highlight_candidate_id": "candidate-safe",
            "start_sec": 10.0,
            "end_sec": 35.0,
            "title": "<script>候補</script>",
            "summary": "要点を説明している。",
            "reason": "単独で理解できるため。",
            "category": "解説",
            "tags": ["要点"],
            "boundary_warning": True,
        }]
        with (
            patch.object(app, "parse_video_choice", return_value="vid_synthetic"),
            patch.object(app.db, "get_conn", return_value=_Connection()),
            patch.object(app.db, "init_db"),
            patch.object(
                app.db, "get_active_transcript_revision", return_value="revision-1"
            ),
            patch.object(app.db, "list_highlight_runs", return_value=[ready]),
            patch.object(
                app.db, "get_highlight_candidates", return_value=candidates
            ),
        ):
            rendered, choices = app._latest_highlight_view("synthetic")

        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;候補&lt;/script&gt;", rendered)
        self.assertIn("segment根拠: 全候補で確認済み", rendered)
        self.assertIn("隔離した不正ASR segment: 2件", rendered)
        self.assertIn("重複抑制: 1件", rendered)
        self.assertIn("最小尺へ自動拡張: 2件", rendered)
        self.assertIn("境界警告: 1件", rendered)
        self.assertIn("最大尺内で前後関係を完結できない", rendered)
        self.assertIn('name="highlight-candidate-inline"', rendered)
        self.assertIn('value="candidate-safe"', rendered)
        self.assertIn("highlight-candidate-description", rendered)
        self.assertEqual(choices[0][1], "candidate-safe")

    def test_highlight_candidate_opens_as_exact_clean_edit_plan(self):
        video = {
            "video_id": "storage-video",
            "public_video_id": "vid_synthetic",
            "source_generation": "source-1",
            "path": "synthetic.mp4",
            "duration": 120.0,
        }
        candidate = {
            "highlight_candidate_id": "candidate-1",
            "start_sec": 20.0,
            "end_sec": 40.0,
        }
        with (
            patch.object(
                app,
                "_resolve_highlight_candidate",
                return_value=("vid_synthetic", video, candidate),
            ),
            patch.object(app.db, "get_conn", return_value=_Connection()),
            patch.object(app.db, "get_segments_in_range", return_value=[]),
            patch.object(app, "make_intuitive_preview", return_value="preview.mp4"),
            patch.object(
                app.DOCUMENTS,
                "open",
                return_value=SimpleNamespace(document_id="document-1"),
            ),
        ):
            outputs = app._load_highlight_candidate_editor(
                "synthetic", "candidate-1"
            )

        state = outputs[0]
        self.assertEqual((state["overall_start"], state["overall_end"]), (20.0, 40.0))
        self.assertEqual((state["viewport_start"], state["viewport_end"]), (10.0, 50.0))
        self.assertEqual(
            (
                state["baseline_plan"]["overall_start"],
                state["baseline_plan"]["overall_end"],
            ),
            (20.0, 40.0),
        )
        self.assertFalse(state["edit_dirty"])


if __name__ == "__main__":
    unittest.main()
