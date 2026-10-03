import hashlib
import json
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
        self.assertIn("画面サイズ", radio_labels)
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
            "public_video_id": "vid_0123456789abcdef0123456789abcdef",
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
            self.assertEqual(
                {Path(path).parent for path in saved},
                {(
                    Path(temporary) / "synthetic_source__vid_0123456789ab"
                ).resolve()},
            )
            self.assertFalse(list(Path(temporary).rglob("*.partial.mp4")))

    def test_highlight_batch_keeps_successes_after_one_candidate_fails(self):
        video = {
            "path": "synthetic.mp4",
            "display_name": "synthetic.mp4",
            "duration": 120.0,
        }
        candidates = [
            {
                "highlight_candidate_id": "candidate-fail",
                "start_sec": 10.0,
                "end_sec": 20.0,
                "export_title": "失敗候補",
            },
            {
                "highlight_candidate_id": "candidate-ok",
                "start_sec": 30.0,
                "end_sec": 40.0,
                "export_title": "成功候補",
            },
        ]
        job = app.EXPORT_JOBS.create()
        with tempfile.TemporaryDirectory() as temporary:
            def fake_cut(_source, start, _end, output, **_kwargs):
                if start == 10.0:
                    raise RuntimeError("synthetic private failure detail")
                Path(output).write_bytes(b"synthetic-video")

            with (
                patch.object(
                    app, "_highlight_export_context", return_value=(video, candidates),
                ),
                patch.object(app, "cut_clip", side_effect=fake_cut),
            ):
                updates = list(app.export_highlight_candidates(
                    "vid_synthetic", "candidate-fail", "all", temporary, True,
                    export_job_id=job.job_id,
                ))

            self.assertEqual(len(updates[-1][1]), 1)
            self.assertIn("成功 1件 / 失敗 1件", updates[-1][0])
            self.assertNotIn("private failure detail", updates[-1][0])
            job_state = app.EXPORT_JOBS.get(job.job_id)
            self.assertEqual(job_state.stage, app.ExportStage.FAILED)
            self.assertEqual(job_state.error_code, "BATCH_PARTIAL_FAILURE")
            self.assertFalse(list(Path(temporary).rglob("*.partial.mp4")))
            self.assertFalse(list(Path(temporary).rglob("*.cut-video-claim")))

    def test_highlight_export_can_write_reviewable_metadata_sidecar(self):
        video = {
            "path": "private-source.mp4",
            "display_name": "synthetic_source.mp4",
            "duration": 120.0,
        }
        candidate = {
            "highlight_candidate_id": "candidate-1",
            "start_sec": 10.0,
            "end_sec": 20.0,
            "title": "候補タイトル",
            "export_title": "章タイトル",
            "summary": "候補の要約",
            "tags": ["配信", "要点"],
            "transcript": "private transcript",
        }

        def fake_cut(_source, _start, _end, output, **_kwargs):
            Path(output).write_bytes(b"video")

        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(app, "_highlight_export_context", return_value=(video, [candidate])),
                patch.object(app, "cut_clip", side_effect=fake_cut),
            ):
                outputs = list(app.export_highlight_candidates(
                    "vid_synthetic", "candidate-1", "selected", temporary, True,
                    write_metadata=True,
                    metadata_title="編集タイトル",
                    metadata_description="編集した説明",
                    metadata_tags="Tag, 別タグ",
                ))
            video_path = Path(outputs[-1][1][0])
            sidecar = video_path.with_suffix(".metadata.json")
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            raw = json.dumps(payload, ensure_ascii=False)
            self.assertEqual(payload["title"], "編集タイトル")
            self.assertEqual(payload["tags"], ["Tag", "別タグ"])
            self.assertTrue(payload["requires_review"])
            self.assertNotIn("private transcript", raw)
            self.assertNotIn(video["path"], raw)
            self.assertEqual(sidecar.parent, video_path.parent)
            self.assertNotEqual(video_path.parent, Path(temporary).resolve())

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

    def test_highlight_export_can_burn_captions_at_source_aspect_ratio(self):
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
                Path(output).write_bytes(b"captioned-source-video")

            with (
                patch.object(
                    app, "_highlight_export_context", return_value=(video, [candidate]),
                ),
                patch.object(
                    app, "_highlight_short_captions", return_value=((object(),), []),
                ) as caption_mapper,
                patch.object(
                    app, "render_captioned_source_clip", side_effect=fake_render,
                ) as renderer,
                patch.object(app, "cut_clip") as ordinary_cutter,
            ):
                outputs = list(app.export_highlight_candidates(
                    "vid_synthetic", "candidate-1", "selected", temporary, True,
                    "standard", "blur", "1080x1920", True,
                ))

            self.assertEqual(
                [Path(path).name for path in outputs[-1][1]],
                ["synthetic_要点_字幕付き.mp4"],
            )
            caption_mapper.assert_called_once_with(video, candidate)
            renderer.assert_called_once()
            ordinary_cutter.assert_not_called()

    def test_highlight_output_controls_disable_irrelevant_settings(self):
        layout, resolution, precise = app.highlight_export_options_update(
            "standard", False,
        )
        self.assertFalse(layout["interactive"])
        self.assertFalse(resolution["interactive"])
        self.assertTrue(precise["interactive"])

        layout, resolution, precise = app.highlight_export_options_update(
            "short", True,
        )
        self.assertTrue(layout["interactive"])
        self.assertTrue(resolution["interactive"])
        self.assertFalse(precise["interactive"])

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

    def test_highlight_video_output_directories_use_distinct_public_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = {
                "path": r"C:\private\first.mp4",
                "display_name": "My Video.mp4",
                "public_video_id": "vid_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            }
            second = {
                **first,
                "public_video_id": "vid_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            }

            first_dir = app._highlight_video_output_directory(root, first)
            second_dir = app._highlight_video_output_directory(root, second)

        self.assertEqual(first_dir.name, "My Video__vid_aaaaaaaaaaaa")
        self.assertEqual(second_dir.name, "My Video__vid_bbbbbbbbbbbb")
        self.assertNotEqual(first_dir, second_dir)

    def test_highlight_video_output_directory_fallback_is_deterministic_and_private(self):
        private_path = r"C:\private\sensitive\source.mp4"
        video = {
            "path": private_path,
            "display_name": "Readable source.mp4",
        }
        expected_id = "vid_" + hashlib.sha256(
            private_path.encode("utf-8")
        ).hexdigest()[:12]

        first = app._highlight_video_output_directory(Path("root"), video)
        second = app._highlight_video_output_directory(Path("root"), video)

        self.assertEqual(first, second)
        self.assertEqual(first.name, f"Readable source__{expected_id}")
        self.assertNotIn(private_path, str(first))
        self.assertNotIn("sensitive", first.name)

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

    def test_saved_highlight_location_selects_one_file_or_opens_batch_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.mp4"
            second = root / "second.mp4"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            with patch.object(app.subprocess, "Popen") as popen:
                one_status = app.open_saved_highlight_location([str(first)], str(root))
                batch_status = app.open_saved_highlight_location(
                    [{"path": str(first)}, {"path": str(second)}], str(root)
                )

        self.assertIn("選択", one_status)
        self.assertIn("2件", batch_status)
        self.assertTrue(popen.call_args_list[0].args[0][1].startswith("/select,"))
        self.assertEqual(Path(popen.call_args_list[1].args[0][1]), root.resolve())

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
            patch.object(app, "_latest_highlight_view", return_value=("none", [])),
        ):
            outputs = app.load_summary_highlight_workspace("vid_synthetic")

        self.assertIn("保存済み要約がない", outputs[1])
        self.assertEqual(outputs[3]["value"], "")
        self.assertFalse(outputs[4]["interactive"])

    def test_saved_candidates_without_summary_stay_selectable(self):
        with (
            patch.object(app, "format_latest_llm_analysis", return_value="no summary"),
            patch.object(app, "_has_ready_llm_analysis", return_value=False),
            patch.object(
                app, "_latest_highlight_view",
                return_value=("codex candidates", [("candidate", "candidate-1")]),
            ),
        ):
            outputs = app.load_summary_highlight_workspace("vid_synthetic")

        self.assertEqual(outputs[2], "codex candidates")
        self.assertEqual(outputs[3]["value"], "candidate-1")
        self.assertIn("プレビュー・保存できます", outputs[1])
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

    def test_highlight_caption_editor_loads_relative_asr_rows_without_persisting(self):
        video = {
            "public_video_id": "vid_synthetic",
            "duration": 120.0,
            "_highlight_transcript_revision": "revision-1",
        }
        candidate = {
            "highlight_candidate_id": "candidate-1",
            "start_sec": 10.0,
            "end_sec": 20.0,
        }
        cue = app.SubtitleCue(250, 1_500, "編集できる字幕", 9)
        with (
            patch.object(
                app, "_highlight_export_context", return_value=(video, [candidate]),
            ),
            patch.object(
                app, "_highlight_short_captions", return_value=((cue,), []),
            ) as captions,
        ):
            rows, state, status = app.load_highlight_caption_editor(
                "vid_synthetic", "candidate-1",
            )

        self.assertEqual(rows, [[0.25, 1.5, "編集できる字幕"]])
        self.assertEqual(state["video_id"], "vid_synthetic")
        self.assertEqual(state["candidate_id"], "candidate-1")
        self.assertIn("1 行", status)
        captions.assert_called_once_with(video, candidate)

    def test_highlight_caption_rows_reject_invalid_order_overlap_and_bounds(self):
        accepted = app._validate_highlight_caption_rows(
            [[0, 1, "一行目"], [1, 2, "二行目"]], 2.0,
        )
        self.assertEqual([(cue.start_ms, cue.end_ms) for cue in accepted], [(0, 1000), (1000, 2000)])
        for rows in (
            [[0, 1, "" ]],
            [[0, 1.2, "一行目"], [1.1, 1.8, "二行目"]],
            [[0, 2.1, "範囲外"]],
        ):
            with self.assertRaises(ValueError):
                app._validate_highlight_caption_rows(rows, 2.0)

    def test_highlight_caption_preview_passes_edited_cues_to_source_renderer(self):
        video = {
            "public_video_id": "vid_synthetic",
            "path": "synthetic.mp4",
            "duration": 120.0,
        }
        candidate = {
            "highlight_candidate_id": "candidate-1",
            "start_sec": 10.0,
            "end_sec": 20.0,
        }
        state = app._highlight_caption_editor_state("vid_synthetic", candidate)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def cached_preview(_output, renderer):
                output = root / "caption-preview.mp4"
                renderer(output)
                return str(output)

            def fake_renderer(_source, _start, _end, output, **_kwargs):
                Path(output).write_bytes(b"preview")

            with (
                patch.object(
                    app, "_highlight_export_context", return_value=(video, [candidate]),
                ),
                patch.object(app, "_create_cached_preview", side_effect=cached_preview),
                patch.object(
                    app, "render_captioned_source_clip", side_effect=fake_renderer,
                ) as renderer,
            ):
                update, status = app.preview_highlight_caption_editor(
                    "vid_synthetic", "candidate-1", [[0, 1, "編集字幕"]], state,
                )

        self.assertIn("1 行", status)
        self.assertTrue(str(update["value"]).endswith("caption-preview.mp4"))
        cue = renderer.call_args.kwargs["captions"][0]
        self.assertEqual((cue.start_ms, cue.end_ms, cue.text), (0, 1000, "編集字幕"))

    def test_output_preview_uses_the_selected_format_and_caption_combination(self):
        video = {
            "public_video_id": "vid_synthetic",
            "path": "synthetic.mp4",
            "duration": 120.0,
        }
        candidate = {
            "highlight_candidate_id": "candidate-1",
            "start_sec": 10.0,
            "end_sec": 20.0,
        }
        automatic_cue = app.SubtitleCue(0, 1_000, "自動字幕", 1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def cached_preview(_output, renderer):
                output = root / "output-preview.mp4"
                renderer(output)
                return str(output)

            def fake_renderer(_source, _start, _end, output, **_kwargs):
                Path(output).write_bytes(b"preview")

            with (
                patch.object(app, "_highlight_export_context", return_value=(video, [candidate])),
                patch.object(app, "_create_cached_preview", side_effect=cached_preview),
                patch.object(app, "_highlight_short_captions", return_value=((automatic_cue,), [])) as automatic,
                patch.object(app, "cut_clip", side_effect=fake_renderer) as cutter,
                patch.object(app, "render_captioned_source_clip", side_effect=fake_renderer) as source_renderer,
                patch.object(app, "render_short_clip", side_effect=fake_renderer) as short_renderer,
            ):
                for export_format, burn_captions in (
                    ("standard", False), ("standard", True),
                    ("short", False), ("short", True),
                ):
                    _update, _detail, status = app.preview_highlight_output(
                        "vid_synthetic", "candidate-1", [], {}, export_format,
                        "blur", "720x1280", burn_captions,
                    )
                    self.assertIn("字幕なし" if not burn_captions else "自動字幕", status)

            self.assertEqual(cutter.call_count, 1)
            self.assertEqual(source_renderer.call_count, 1)
            self.assertEqual(short_renderer.call_count, 2)
            self.assertEqual(automatic.call_count, 2)
            self.assertFalse(short_renderer.call_args_list[0].kwargs["options"].burn_captions)
            self.assertTrue(short_renderer.call_args_list[1].kwargs["options"].burn_captions)

    def test_output_preview_prefers_matching_edits_and_cache_key_includes_caption_switch(self):
        video = {
            "public_video_id": "vid_synthetic",
            "path": "synthetic.mp4",
            "duration": 120.0,
        }
        candidate = {
            "highlight_candidate_id": "candidate-1",
            "start_sec": 10.0,
            "end_sec": 20.0,
        }
        state = app._highlight_caption_editor_state("vid_synthetic", candidate)
        cue = app.SubtitleCue(0, 1_000, "編集字幕", 1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def cached_preview(_output, renderer):
                output = root / "edited-preview.mp4"
                renderer(output)
                return str(output)

            def fake_renderer(_source, _start, _end, output, **_kwargs):
                Path(output).write_bytes(b"preview")

            with (
                patch.object(app, "_highlight_export_context", return_value=(video, [candidate])),
                patch.object(app, "_create_cached_preview", side_effect=cached_preview),
                patch.object(app, "render_captioned_source_clip", side_effect=fake_renderer) as renderer,
                patch.object(app, "_highlight_short_captions") as automatic,
            ):
                _update, _detail, status = app.preview_highlight_output(
                    "vid_synthetic", "candidate-1", [[0, 1, "編集字幕"]], state,
                    "standard", "blur", "1080x1920", True,
                )

        self.assertIn("編集字幕 1 行", status)
        automatic.assert_not_called()
        self.assertEqual(renderer.call_args.kwargs["captions"], (cue,))
        captioned = app._highlight_caption_preview_path(
            video, candidate, (cue,), export_format="standard", short_layout="blur",
            short_resolution="1080x1920", burn_captions=True,
        )
        without_captions = app._highlight_caption_preview_path(
            video, candidate, (), export_format="standard", short_layout="blur",
            short_resolution="1080x1920", burn_captions=False,
        )
        self.assertNotEqual(captioned, without_captions)

    def test_output_controls_are_single_shared_components_for_editor_preview_and_save(self):
        components = app.demo.config.get("components", [])
        by_label = {}
        for component in components:
            label = (component.get("props") or {}).get("label")
            if label:
                by_label.setdefault(label, []).append(component["id"])
        format_ids = by_label["画面サイズ"]
        caption_ids = by_label["字幕を動画へ焼き込む"]
        self.assertEqual(len(format_ids), 1)
        self.assertEqual(len(caption_ids), 1)
        preview_event = next(
            event for event in app.demo.config["dependencies"]
            if event.get("api_name") == "preview_intuitive_output"
        )
        save_event = next(
            event for event in app.demo.config["dependencies"]
            if event.get("api_name") == "save_intuitive_editor"
        )
        self.assertIn(format_ids[0], preview_event["inputs"])
        self.assertIn(format_ids[0], save_event["inputs"])
        self.assertIn(caption_ids[0], preview_event["inputs"])
        self.assertIn(caption_ids[0], save_event["inputs"])

    def test_highlight_edit_success_switches_to_main_editor_tab(self):
        load_event = next(
            event for event in app.demo.config["dependencies"]
            if event.get("api_name") == "load_highlight_candidate_into_editor"
        )
        navigation_event = next(
            event for event in app.demo.config["dependencies"]
            if event.get("trigger_after") == load_event["id"]
            and event.get("trigger_only_on_success")
        )
        self.assertIn("検索・編集・切り抜き", navigation_event.get("js") or "")
        self.assertIn("[role=\"tab\"]", navigation_event.get("js") or "")

    def test_intuitive_caption_edits_fall_back_after_plan_change(self):
        state = {
            "video_id": "vid_synthetic",
            "duration": 30.0,
            "overall_start": 0.0,
            "overall_end": 10.0,
            "exclusions": [],
        }
        edited = app.SubtitleCue(0, 1_000, "編集字幕", 1)
        automatic = app.SubtitleCue(0, 800, "自動字幕", 2)
        with patch.object(app, "_intuitive_active_transcript_revision", return_value="rev-1"):
            editor_state = app._intuitive_caption_editor_state(state, "rev-1")
            changed = {**state, "overall_end": 12.0}
            with patch.object(
                app, "_intuitive_auto_captions", return_value=((automatic,), [], "rev-1"),
            ) as auto:
                cues, kind, _warnings = app._resolve_intuitive_output_captions(
                    changed, [[0, 1, "編集字幕"]], editor_state, burn_captions=True,
                )
        self.assertEqual(cues, (automatic,))
        self.assertIn("自動字幕", kind)
        auto.assert_called_once_with(changed)

    def test_intuitive_auto_captions_are_split_before_the_editor(self):
        state = {
            "video_id": "vid_synthetic",
            "duration": 30.0,
            "overall_start": 0.0,
            "overall_end": 10.0,
            "exclusions": [],
        }
        long_cue = app.SubtitleCue(
            0, 5_000, "あいうえおかきくけこさしすせそたちつてと", 1,
        )
        with (
            patch.object(
                app, "_intuitive_active_transcript_revision", return_value="rev-1",
            ),
            patch.object(app.db, "get_conn", return_value=_Connection()),
            patch.object(app.db, "get_segments_in_range", return_value=[{}]),
            patch.object(app, "parse_segment", return_value=object()),
            patch.object(
                app, "map_subtitles",
                return_value=SimpleNamespace(cues=(long_cue,), warnings=()),
            ),
        ):
            captions, warnings, revision = app._intuitive_auto_captions(state)
        self.assertEqual(revision, "rev-1")
        self.assertEqual(warnings, [])
        self.assertGreater(len(captions), 1)
        self.assertTrue(all(len(cue.text) <= 10 for cue in captions))

    def test_intuitive_output_preview_key_changes_for_profile_and_caption(self):
        state = {
            "video_id": "vid_synthetic",
            "video_path": "synthetic.mp4",
            "duration": 30.0,
            "overall_start": 0.0,
            "overall_end": 10.0,
            "exclusions": [],
        }
        first = app._intuitive_output_preview_path(
            state, (), output_format="standard", short_layout="blur",
            short_resolution="1080x1920", burn_captions=False,
        )
        second = app._intuitive_output_preview_path(
            state, (app.SubtitleCue(0, 500, "字幕", 1),), output_format="short",
            short_layout="crop", short_resolution="720x1280", burn_captions=True,
        )
        self.assertNotEqual(first, second)

    def test_intuitive_output_profile_resolves_canvas_and_caption_style(self):
        source = app._intuitive_output_profile(
            "standard", "blur", "1080x1920", True, "boxed", "top",
        )
        portrait = app._intuitive_output_profile(
            "short", "crop", "720x1280", True, "large", "center",
        )
        square = app._intuitive_output_profile(
            "square", "blur", "1080x1920", False,
        )
        self.assertEqual(source.canvas_mode, "source")
        self.assertEqual(source.caption.preset, "boxed")
        self.assertEqual(source.caption.position, "top")
        self.assertEqual(portrait.canvas_mode, "portrait_crop")
        self.assertEqual((portrait.width, portrait.height), (720, 1280))
        self.assertEqual(portrait.caption.preset, "large")
        self.assertEqual(portrait.caption.position, "center")
        self.assertEqual(square.canvas_mode, "square_fit")
        self.assertEqual((square.width, square.height), (1080, 1080))

    def test_intuitive_audio_profile_keeps_bgm_path_ephemeral(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "private-source.mp4"
            source.write_bytes(b"source")
            bgm = root / "private music.wav"
            bgm.write_bytes(b"bgm")
            with patch.object(app, "probe_audio_stream", return_value=True):
                audio, warnings, resolved = app._resolve_intuitive_audio_profile(
                    source, True, bgm, -25, 0.5, 1.0,
                )
        manifest = audio.to_manifest()
        self.assertEqual(warnings, [])
        self.assertEqual(resolved, bgm.resolve())
        self.assertEqual(manifest["bgm_name"], bgm.name)
        self.assertTrue(manifest["normalization_applied"])
        self.assertNotIn(str(root), json.dumps(manifest, ensure_ascii=False))

    def test_intuitive_audio_profile_warns_when_source_has_no_audio(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "silent.mp4"
            source.write_bytes(b"source")
            with patch.object(app, "probe_audio_stream", return_value=False):
                audio, warnings, _bgm = app._resolve_intuitive_audio_profile(
                    source, True,
                )
        self.assertFalse(audio.normalization_applied)
        self.assertEqual(warnings, ["AUDIO_NORMALIZE_SKIPPED_NO_SOURCE_AUDIO"])

    def test_intuitive_output_preview_key_changes_for_caption_style(self):
        state = {
            "video_id": "vid_synthetic",
            "video_path": "synthetic.mp4",
            "duration": 30.0,
            "overall_start": 0.0,
            "overall_end": 10.0,
            "exclusions": [],
        }
        standard = app._intuitive_output_preview_path(
            state, (app.SubtitleCue(0, 500, "字幕", 1),),
            output_format="short", short_layout="blur",
            short_resolution="720x1280", burn_captions=True,
            caption_preset="standard", caption_position="bottom",
        )
        boxed = app._intuitive_output_preview_path(
            state, (app.SubtitleCue(0, 500, "字幕", 1),),
            output_format="short", short_layout="blur",
            short_resolution="720x1280", burn_captions=True,
            caption_preset="boxed", caption_position="top",
        )
        self.assertNotEqual(standard, boxed)

    def test_intuitive_output_font_preflight_does_not_expose_caption_text(self):
        profile = app._intuitive_output_profile(
            "short", "blur", "720x1280", True,
        )
        cue = app.SubtitleCue(0, 500, "private synthetic caption", 1)
        with patch.object(
            app,
            "validate_font_glyphs",
            return_value=SimpleNamespace(warning="FONT_CHECK_UNAVAILABLE"),
        ) as validate:
            warnings = app._validate_intuitive_output_font(profile, (cue,))
        self.assertEqual(warnings, ["FONT_CHECK_UNAVAILABLE"])
        self.assertEqual(validate.call_args.args[1], cue.text)

    def test_intuitive_export_job_can_be_started_and_cancelled(self):
        job_id, status, _button = app.start_intuitive_export_job()
        self.assertIn("保存待ち", status)
        cancelled_status, _button = app.cancel_intuitive_export_job(job_id)
        self.assertIn("停止を要求", cancelled_status)
        state = app.EXPORT_JOBS.get(job_id)
        self.assertTrue(state.cancel_requested)

    def test_intuitive_export_job_wrapper_reports_completion(self):
        job = app.EXPORT_JOBS.create()
        state = {
            "video_id": "vid_synthetic",
            "video_path": "missing-synthetic.mp4",
            "duration": 30.0,
            "overall_start": 0.0,
            "overall_end": 10.0,
            "exclusions": [],
        }
        with (
            patch.object(app, "on_save", return_value="saved.mp4"),
            patch.object(app, "render_intuitive_toolbar", return_value="toolbar"),
        ):
            updates = list(app.run_intuitive_export_job(
                state, True, "clips", "sample.mp4", False,
                "standard", "blur", "1080x1920", False,
                [], {}, "standard", "bottom", job.job_id,
            ))
        self.assertEqual(updates[-1][0], "saved.mp4")
        self.assertIn("保存完了", updates[-1][3])
        self.assertEqual(
            app.EXPORT_JOBS.get(job.job_id).stage,
            app.ExportStage.COMPLETED,
        )

    def test_intuitive_output_preview_key_changes_with_caption_renderer(self):
        state = {
            "video_id": "vid_synthetic",
            "video_path": "synthetic.mp4",
            "duration": 30.0,
            "overall_start": 0.0,
            "overall_end": 10.0,
            "exclusions": [],
        }
        with patch.object(app, "CAPTION_PREVIEW_RENDER_VERSION", "old"):
            old = app._intuitive_output_preview_path(
                state, (app.SubtitleCue(0, 500, "字幕", 1),),
                output_format="short", short_layout="blur",
                short_resolution="720x1280", burn_captions=True,
            )
        with patch.object(app, "CAPTION_PREVIEW_RENDER_VERSION", "new"):
            new = app._intuitive_output_preview_path(
                state, (app.SubtitleCue(0, 500, "字幕", 1),),
                output_format="short", short_layout="blur",
                short_resolution="720x1280", burn_captions=True,
            )
        self.assertNotEqual(old, new)

    def test_intuitive_caption_form_edits_one_selected_row(self):
        rows = [[0.0, 1.0, "最初"], [1.0, 2.0, "次"]]
        selected, text, start, end, _status = app.move_intuitive_caption_form(
            rows, 0, 1,
        )
        self.assertEqual((selected, text, start, end), (1, "次", 1.0, 2.0))
        state = {
            "video_id": "vid_synthetic",
            "duration": 30.0,
            "overall_start": 0.0,
            "overall_end": 10.0,
            "exclusions": [],
        }
        updated, status = app.apply_intuitive_caption_form(
            rows, 1, "修正後", 1.1, 2.2, state,
        )
        self.assertEqual(updated[1], [1.1, 2.2, "修正後"])
        self.assertIn("変更を反映", status)

    def test_intuitive_caption_form_rejects_overlap(self):
        rows = [[0.0, 1.0, "最初"], [1.0, 2.0, "次"]]
        state = {
            "video_id": "vid_synthetic",
            "duration": 30.0,
            "overall_start": 0.0,
            "overall_end": 10.0,
            "exclusions": [],
        }
        with self.assertRaises(Exception):
            app.apply_intuitive_caption_form(
                rows, 1, "重複", 0.5, 2.0, state,
            )

    def test_highlight_export_uses_edits_only_for_matching_selected_candidate(self):
        video = {
            "public_video_id": "vid_synthetic",
            "path": "synthetic.mp4",
            "display_name": "synthetic.mp4",
            "duration": 120.0,
        }
        candidate = {
            "highlight_candidate_id": "candidate-1",
            "start_sec": 10.0,
            "end_sec": 20.0,
            "export_title": "見どころ",
        }
        state = app._highlight_caption_editor_state("vid_synthetic", candidate)
        with tempfile.TemporaryDirectory() as temporary:
            def fake_renderer(_source, _start, _end, output, **_kwargs):
                Path(output).write_bytes(b"captioned")

            with (
                patch.object(
                    app, "_highlight_export_context", return_value=(video, [candidate]),
                ),
                patch.object(app, "render_captioned_source_clip", side_effect=fake_renderer) as renderer,
                patch.object(app, "_highlight_short_captions") as automatic,
            ):
                outputs = list(app.export_highlight_candidates(
                    "vid_synthetic", "candidate-1", "selected", temporary, True,
                    "standard", "blur", "1080x1920", True,
                    [[0, 1, "編集字幕"]], state,
                ))

            self.assertTrue(outputs[-1][1])
            automatic.assert_not_called()
            cue = renderer.call_args.kwargs["captions"][0]
            self.assertEqual(cue.text, "編集字幕")

        wrong_state = app._highlight_caption_editor_state("vid_synthetic", {
            **candidate, "highlight_candidate_id": "candidate-other",
        })
        automatic_cue = app.SubtitleCue(0, 1_000, "自動字幕", 1)
        with tempfile.TemporaryDirectory() as temporary:
            def fake_automatic_renderer(_source, _start, _end, output, **_kwargs):
                Path(output).write_bytes(b"automatic-captioned")

            with (
                patch.object(
                    app, "_highlight_export_context", return_value=(video, [candidate]),
                ),
                patch.object(
                    app, "_highlight_short_captions",
                    return_value=((automatic_cue,), []),
                ) as automatic,
                patch.object(
                    app, "render_captioned_source_clip",
                    side_effect=fake_automatic_renderer,
                ) as renderer,
            ):
                outputs = list(app.export_highlight_candidates(
                    "vid_synthetic", "candidate-1", "selected", temporary, True,
                    "standard", "blur", "1080x1920", True,
                    [[0, 1, "編集字幕"]], wrong_state,
                ))

        self.assertTrue(outputs[-1][1])
        automatic.assert_called_once_with(video, candidate)
        self.assertEqual(renderer.call_args.kwargs["captions"], (automatic_cue,))

    def test_highlight_export_without_editor_state_keeps_automatic_captions(self):
        video = {
            "public_video_id": "vid_synthetic",
            "path": "synthetic.mp4",
            "display_name": "synthetic.mp4",
            "duration": 120.0,
        }
        candidate = {
            "highlight_candidate_id": "candidate-1",
            "start_sec": 10.0,
            "end_sec": 20.0,
            "export_title": "見どころ",
        }
        automatic_cue = app.SubtitleCue(0, 1_000, "自動字幕", 1)
        with tempfile.TemporaryDirectory() as temporary:
            def fake_renderer(_source, _start, _end, output, **_kwargs):
                Path(output).write_bytes(b"captioned")

            with (
                patch.object(
                    app, "_highlight_export_context", return_value=(video, [candidate]),
                ),
                patch.object(
                    app, "_highlight_short_captions",
                    return_value=((automatic_cue,), []),
                ) as automatic,
                patch.object(
                    app, "render_captioned_source_clip", side_effect=fake_renderer,
                ) as renderer,
            ):
                outputs = list(app.export_highlight_candidates(
                    "vid_synthetic", "candidate-1", "selected", temporary, True,
                    "standard", "blur", "1080x1920", True, [], {},
                ))

        self.assertTrue(outputs[-1][1])
        automatic.assert_called_once_with(video, candidate)
        self.assertEqual(renderer.call_args.kwargs["captions"], (automatic_cue,))

    def test_matching_caption_editor_rejects_empty_rows(self):
        video = {"public_video_id": "vid_synthetic"}
        candidate = {
            "highlight_candidate_id": "candidate-1",
            "start_sec": 10.0,
            "end_sec": 20.0,
        }
        state = app._highlight_caption_editor_state("vid_synthetic", candidate)
        with self.assertRaisesRegex(ValueError, "編集字幕が空"):
            app._edited_highlight_captions_or_none(video, candidate, [], state)


if __name__ == "__main__":
    unittest.main()
