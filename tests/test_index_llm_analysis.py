import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import index_video


class OptionalLlmIndexStageTest(unittest.TestCase):
    def _torch_stub(self):
        return SimpleNamespace(
            cuda=SimpleNamespace(
                is_available=lambda: False,
                empty_cache=lambda: None,
            )
        )

    def test_success_reports_derived_tag_and_chapter_counts(self):
        with (
            patch.dict(sys.modules, {"torch": self._torch_stub()}),
            patch(
                "moment_retrieval.llm_analysis.run_transcript_analysis",
                return_value={"tags": ["a", "b"], "chapters": [{}, {}]},
            ) as analyze,
        ):
            messages = list(
                index_video._run_optional_llm_analysis(
                    object(), "synthetic-video", "synthetic-revision", "local-model"
                )
            )

        self.assertIn("ローカルLLM", messages[0])
        self.assertIn("タグ2件 / 章2件", messages[-1])
        self.assertEqual(
            analyze.call_args.args[1:3],
            ("synthetic-video", "synthetic-revision"),
        )
        self.assertEqual(analyze.call_args.args[4], "local-model")

    def test_failure_is_a_warning_and_does_not_raise(self):
        with (
            patch.dict(sys.modules, {"torch": self._torch_stub()}),
            patch(
                "moment_retrieval.llm_analysis.run_transcript_analysis",
                side_effect=RuntimeError("synthetic offline"),
            ),
        ):
            messages = list(
                index_video._run_optional_llm_analysis(
                    object(), "synthetic-video", "synthetic-revision", "local-model"
                )
            )

        self.assertIn("LLM解析に失敗", messages[-1])
        self.assertIn("動画・文字起こし・検索は利用できます", messages[-1])


if __name__ == "__main__":
    unittest.main()
