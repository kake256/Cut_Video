import tempfile
import unittest
from pathlib import Path

from moment_retrieval.ui_experiment import (
    SCENARIOS, UI_VARIANTS, UIExperimentRecorder, compare_ui_runs,
)


class UIExperimentTest(unittest.TestCase):
    def test_anonymous_runs_and_adoption_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = UIExperimentRecorder(Path(directory) / "metrics.jsonl")
            for scenario in SCENARIOS:
                for variant in UI_VARIANTS:
                    for cold in (False, True):
                        for _ in range(10):
                            recorder.record(
                                scenario, variant, cold,
                                800 if variant == "candidate" else 1000,
                                5, 0, True,
                            )
            raw = recorder.path.read_text(encoding="utf-8")
            self.assertNotIn("query", raw)
            self.assertNotIn("video_id", raw)
            report = compare_ui_runs(recorder.read())
            self.assertTrue(report["ready"])
            self.assertTrue(report["adopt_candidate"])

    def test_insufficient_runs_never_adopt(self):
        self.assertFalse(compare_ui_runs([])["adopt_candidate"])


if __name__ == "__main__":
    unittest.main()
