import unittest

from moment_retrieval.edit_domain import (
    EditHistory, EditPlan, EditPlanError, TimeRange, TimelineMap,
    edit_plan_from_intuitive, edit_plan_from_kept_ranges,
)


class EditDomainTest(unittest.TestCase):
    def test_adjacent_exclusions_merge_and_kept_ranges_are_canonical(self):
        plan = EditPlan.create(60_000, 0, 60_000, (
            TimeRange(10_000, 20_000), TimeRange(20_001, 30_000),
        ))
        self.assertEqual(plan.exclusions, (TimeRange(10_000, 30_000),))
        self.assertEqual(plan.kept_ranges, (TimeRange(0, 10_000), TimeRange(30_000, 60_000)))

    def test_tiny_kept_island_is_absorbed(self):
        plan = EditPlan.create(10_000, 0, 10_000, (
            TimeRange(1_000, 4_950), TimeRange(5_000, 9_000),
        ))
        self.assertEqual(plan.exclusions, (TimeRange(1_000, 9_000),))

    def test_timeline_map_is_bidirectional_across_cut(self):
        plan = EditPlan.create(40_000, 10_000, 40_000, (TimeRange(20_000, 30_000),))
        mapping = TimelineMap.from_plan(plan)
        self.assertEqual(mapping.source_to_result(15_000), 5_000)
        self.assertIsNone(mapping.source_to_result(25_000))
        self.assertEqual(mapping.source_to_result(35_000), 15_000)
        self.assertEqual(mapping.result_to_source(15_000), 35_000)

    def test_legacy_and_intuitive_adapters_share_same_semantics(self):
        legacy = edit_plan_from_kept_ranges(10.0, 40.0, [[10.0, 20.0], [30.0, 40.0]])
        intuitive = edit_plan_from_intuitive({
            "duration": 40.0, "overall_start": 10.0, "overall_end": 40.0,
            "exclusions": [{"start": 20.0, "end": 30.0}],
        })
        self.assertEqual(legacy.semantic_signature, intuitive.semantic_signature)

    def test_entire_range_cannot_be_excluded(self):
        with self.assertRaises(EditPlanError):
            EditPlan.create(10_000, 0, 10_000, (TimeRange(0, 10_000),))

    def test_history_round_trip_preserves_clean_reference(self):
        original = EditPlan.create(10_000, 0, 10_000)
        edited = original.add_exclusion(2_000, 3_000)
        history = EditHistory.create(original).apply(edited)
        self.assertTrue(history.dirty)
        self.assertEqual(history.undo_once().current, original)
        self.assertEqual(history.undo_once().redo_once().current, edited)
        self.assertFalse(history.mark_clean().dirty)


if __name__ == "__main__":
    unittest.main()
