import os
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
