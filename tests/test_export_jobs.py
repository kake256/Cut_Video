import unittest

from moment_retrieval.export_jobs import (
    ExportJobRegistry,
    ExportStage,
)


class ExportJobRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = ExportJobRegistry()
        self.job = self.registry.create(job_id="export_test")

    def test_successful_lifecycle_has_monotonic_progress(self):
        validating = self.registry.transition("export_test", ExportStage.VALIDATING)
        joining = self.registry.transition("export_test", ExportStage.JOINING)
        rendering = self.registry.transition("export_test", ExportStage.RENDERING)
        completed = self.registry.complete("export_test")

        self.assertLess(validating.progress, joining.progress)
        self.assertLess(joining.progress, rendering.progress)
        self.assertEqual(completed.stage, ExportStage.COMPLETED)
        self.assertEqual(completed.progress, 100)
        self.assertTrue(completed.terminal)

    def test_progress_cannot_move_backwards(self):
        self.registry.transition("export_test", ExportStage.RENDERING, progress=70)
        state = self.registry.transition("export_test", ExportStage.PROBING, progress=60)
        self.assertEqual(state.progress, 70)

    def test_cancel_request_is_separate_from_worker_acknowledgement(self):
        requested = self.registry.request_cancel("export_test")
        self.assertTrue(requested.cancel_requested)
        self.assertFalse(requested.terminal)
        self.assertTrue(self.registry.cancel_event("export_test").is_set())

        cancelled = self.registry.acknowledge_cancel("export_test")
        self.assertEqual(cancelled.stage, ExportStage.CANCELLED)
        self.assertEqual(cancelled.error_code, "EXPORT_CANCELLED")
        self.assertTrue(cancelled.terminal)

    def test_failure_records_stage_and_structured_code(self):
        self.registry.transition("export_test", ExportStage.PROBING)
        failed = self.registry.fail(
            "export_test", "artifact_probe_failed", message="safe summary",
        )
        self.assertEqual(failed.stage, ExportStage.FAILED)
        self.assertEqual(failed.failed_stage, ExportStage.PROBING)
        self.assertEqual(failed.error_code, "ARTIFACT_PROBE_FAILED")
        self.assertEqual(failed.message, "safe summary")

    def test_terminal_state_is_not_overwritten_by_late_completion(self):
        failed = self.registry.fail("export_test", "FFMPEG_FAILED")
        completed = self.registry.complete("export_test")
        self.assertEqual(completed, failed)

    def test_duplicate_and_missing_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            self.registry.create(job_id="export_test")
        with self.assertRaises(KeyError):
            self.registry.cancel_event("missing")

    def test_remove_discards_state_and_cancel_event(self):
        self.registry.remove("export_test")
        self.assertIsNone(self.registry.get("export_test"))
        with self.assertRaises(KeyError):
            self.registry.cancel_event("export_test")


if __name__ == "__main__":
    unittest.main()
