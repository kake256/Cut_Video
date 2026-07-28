import sys
import unittest
from unittest.mock import patch

import generate_highlights
from moment_retrieval.highlight_analysis import HighlightAnalysisError


class GenerateHighlightsCliTest(unittest.TestCase):
    def test_cli_exits_nonzero_for_missing_ready_analysis_without_leaking_source_data(self):
        with patch.object(generate_highlights, "generate_active_highlights", side_effect=HighlightAnalysisError("先にLLM解析を実行してください")), \
             patch.object(sys, "argv", ["generate_highlights.py", "--video-id", "vid_test"]):
            with self.assertRaises(SystemExit) as raised:
                generate_highlights.main()
        self.assertEqual(raised.exception.code, 1)

    def test_cli_passes_duration_validation_to_core(self):
        with self.assertRaisesRegex(ValueError, "requested_count"):
            # Core validation is intentionally before provider/network work.
            from moment_retrieval.highlight_analysis import run_highlight_analysis
            run_highlight_analysis(None, "v", "r", "a", None, "m", requested_count=2)
