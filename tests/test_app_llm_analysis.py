import os
import tempfile
import unittest
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
