import unittest

import gradio as gr

from app import _clip_plan_ranges, exclude_clip_range, reset_clip_plan


class ClipPlanTest(unittest.TestCase):
    def test_middle_exclusion_splits_selection(self):
        plan, _, _ = reset_clip_plan(0.0, 60.0)

        plan, table, summary = exclude_clip_range(0.0, 60.0, 20.0, 30.0, plan)

        self.assertEqual(plan["ranges"], [[0.0, 20.0], [30.0, 60.0]])
        self.assertEqual([row[3] for row in table], [20.0, 30.0])
        self.assertIn("50.0秒", summary)

    def test_multiple_exclusions_create_multiple_kept_windows(self):
        plan, _, _ = reset_clip_plan(0.0, 60.0)
        plan, _, _ = exclude_clip_range(0.0, 60.0, 10.0, 20.0, plan)

        plan, _, _ = exclude_clip_range(0.0, 60.0, 40.0, 45.0, plan)

        self.assertEqual(plan["ranges"], [[0.0, 10.0], [20.0, 40.0], [45.0, 60.0]])

    def test_changed_outer_selection_discards_stale_plan(self):
        plan, _, _ = reset_clip_plan(0.0, 60.0)
        plan, _, _ = exclude_clip_range(0.0, 60.0, 20.0, 30.0, plan)

        self.assertEqual(_clip_plan_ranges(5.0, 55.0, plan), [[5.0, 55.0]])

    def test_cannot_exclude_entire_selection(self):
        plan, _, _ = reset_clip_plan(0.0, 60.0)

        with self.assertRaises(gr.Error):
            exclude_clip_range(0.0, 60.0, 0.0, 60.0, plan)


if __name__ == "__main__":
    unittest.main()
