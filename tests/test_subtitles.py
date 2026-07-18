import unittest

from moment_retrieval.edit_domain import EditPlan, TimeRange, make_effective_export_plan
from moment_retrieval.subtitles import (
    SubtitleValidationError, map_subtitles, validate_srt_text,
)
from moment_retrieval.transcript_types import (
    TimestampGranularity, TranscriptSegment, TranscriptWord,
)


class SubtitleTest(unittest.TestCase):
    def test_word_cues_map_across_cut_without_joining_ranges(self):
        plan = EditPlan.create(40_000, 10_000, 40_000, (TimeRange(20_000, 30_000),))
        segment = TranscriptSegment(1, 15_000, 35_000, "前中後", (
            TranscriptWord("前", 15_000, 16_000),
            TranscriptWord("中", 25_000, 26_000),
            TranscriptWord("後", 34_000, 35_000),
        ), TimestampGranularity.WORD)
        result = map_subtitles([segment], make_effective_export_plan(plan))
        self.assertEqual([(cue.start_ms, cue.end_ms, cue.text) for cue in result.cues], [
            (5_000, 6_000, "前"), (14_000, 15_000, "後"),
        ])
        self.assertTrue(result.warnings)

    def test_partial_segment_fallback_is_omitted(self):
        plan = EditPlan.create(10_000, 0, 10_000, (TimeRange(4_000, 6_000),))
        segment = TranscriptSegment(
            2, 3_000, 7_000, "fallback", (), TimestampGranularity.SEGMENT,
        )
        result = map_subtitles([segment], make_effective_export_plan(plan))
        self.assertEqual(result.cues, ())
        self.assertIn("partially cut", result.warnings[0])

    def test_padding_uses_artifact_timeline(self):
        plan = EditPlan.create(50_000, 10_000, 40_000)
        effective = make_effective_export_plan(plan, 2_000, 3_000)
        segment = TranscriptSegment(
            3, 8_000, 9_000, "pad", (), TimestampGranularity.SEGMENT,
        )
        result = map_subtitles([segment], effective)
        self.assertEqual((result.cues[0].start_ms, result.cues[0].end_ms), (0, 1_000))

    def test_srt_validation_uses_probed_duration_and_frame_tolerance(self):
        text = "1\n00:00:00,000 --> 00:00:01,034\ntest\n"
        self.assertEqual(validate_srt_text(text, 1_000, 34), ((0, 1_034),))
        with self.assertRaises(SubtitleValidationError):
            validate_srt_text(text, 1_000, 33)

    def test_non_empty_srt_without_timing_is_rejected(self):
        with self.assertRaises(SubtitleValidationError):
            validate_srt_text("not an srt", 1_000, 34)


if __name__ == "__main__":
    unittest.main()
