import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cut_clip import cut_clips, validate_clip_ranges


class ValidateClipRangesTests(unittest.TestCase):
    def test_accepts_sorted_non_overlapping_ranges(self):
        self.assertEqual(
            validate_clip_ranges([(0, 10), (20.5, 30)], duration=60),
            [(0.0, 10.0), (20.5, 30.0)],
        )

    def test_rejects_empty_ranges(self):
        with self.assertRaisesRegex(ValueError, "1つ以上"):
            validate_clip_ranges([])

    def test_rejects_invalid_range_order_and_overlap(self):
        invalid_cases = [
            [(-1, 2)],
            [(2, 2)],
            [(3, 2)],
            [(10, 20), (5, 8)],
            [(0, 10), (9, 20)],
        ]
        for ranges in invalid_cases:
            with self.subTest(ranges=ranges), self.assertRaises(ValueError):
                validate_clip_ranges(ranges)

    def test_rejects_range_beyond_duration(self):
        with self.assertRaisesRegex(ValueError, "動画の長さ"):
            validate_clip_ranges([(0, 10.1)], duration=10)


class CutClipsTests(unittest.TestCase):
    @patch("cut_clip.cut_clip")
    def test_single_range_uses_existing_cut_clip(self, mock_cut_clip):
        output = Path("out.mp4")
        mock_cut_clip.return_value = output

        result = cut_clips(
            Path("input.mp4"), [(10, 20)], output, precise=True, duration=100, pad=1.5
        )

        self.assertEqual(result, output)
        mock_cut_clip.assert_called_once_with(
            Path("input.mp4"),
            8.5,
            21.5,
            output,
            pad=0.0,
            precise=True,
            duration=100,
        )

    @patch("cut_clip.subprocess.run")
    @patch("cut_clip.cut_clip")
    def test_multiple_ranges_cut_and_concat(self, mock_cut_clip, mock_run):
        def create_segment(*args, **kwargs):
            output_path = args[3]
            output_path.touch()
            return output_path

        mock_cut_clip.side_effect = create_segment

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "result.mp4"

            def create_joined_file(cmd, **kwargs):
                Path(cmd[-1]).touch()
                return subprocess.CompletedProcess(cmd, 0)

            mock_run.side_effect = create_joined_file
            result = cut_clips(
                Path("input.mp4"),
                [(0, 25), (35, 60)],
                output,
                precise=False,
                duration=60,
            )

            self.assertEqual(result, output)
            self.assertTrue(output.exists())
            self.assertEqual(mock_cut_clip.call_count, 2)
            first_call, second_call = mock_cut_clip.call_args_list
            self.assertEqual(first_call.args[1:3], (0.0, 25.0))
            self.assertEqual(second_call.args[1:3], (35.0, 60.0))
            concat_cmd = mock_run.call_args.args[0]
            self.assertEqual(concat_cmd[1:7], ["-y", "-f", "concat", "-safe", "0", "-i"])
            self.assertEqual(concat_cmd[-3:-1], ["-c", "copy"])
            self.assertFalse(any(Path(temp_dir).glob("cut_video_*")))

    @patch("cut_clip.subprocess.run")
    @patch("cut_clip.cut_clip")
    def test_temporary_files_are_removed_when_concat_fails(self, mock_cut_clip, mock_run):
        def create_segment(*args, **kwargs):
            args[3].touch()
            return args[3]

        mock_cut_clip.side_effect = create_segment
        mock_run.side_effect = subprocess.CalledProcessError(1, ["ffmpeg"])

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "result.mp4"
            with self.assertRaises(subprocess.CalledProcessError):
                cut_clips(Path("input.mp4"), [(0, 5), (10, 15)], output)
            self.assertFalse(output.exists())
            self.assertFalse(any(Path(temp_dir).glob("cut_video_*")))


if __name__ == "__main__":
    unittest.main()
