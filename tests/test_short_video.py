import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moment_retrieval.short_video import (
    ShortVideoOptions,
    build_short_filter,
    captions_to_ass,
    parse_short_resolution,
    prepare_short_captions,
    render_short_clip,
)
from moment_retrieval.subtitles import SubtitleCue


class ShortVideoUnitTests(unittest.TestCase):
    def test_supported_resolutions_are_portrait(self):
        self.assertEqual(parse_short_resolution("1080x1920"), (1080, 1920))
        self.assertEqual(parse_short_resolution("720X1280"), (720, 1280))
        with self.assertRaises(ValueError):
            parse_short_resolution("1920x1080")

    def test_filter_keeps_full_frame_or_crops_explicitly(self):
        blur = build_short_filter(
            ShortVideoOptions(720, 1280, "blur", True), include_captions=True,
        )
        self.assertIn("boxblur", blur)
        self.assertIn("overlay", blur)
        self.assertIn("subtitles=filename=captions.ass", blur)

        crop = build_short_filter(
            ShortVideoOptions(720, 1280, "crop", False), include_captions=False,
        )
        self.assertIn("crop=720:1280", crop)
        self.assertNotIn("subtitles", crop)

    def test_long_caption_is_split_without_leaving_source_timing(self):
        source = SubtitleCue(
            1_000, 5_000,
            "これはショート動画で読みやすく表示するための長い字幕テキストです。",
            7,
        )
        result = prepare_short_captions([source], max_chars=12)
        self.assertGreater(len(result), 1)
        self.assertEqual(result[0].start_ms, 1_000)
        self.assertEqual(result[-1].end_ms, 5_000)
        self.assertTrue(all(a.end_ms == b.start_ms for a, b in zip(result, result[1:])))
        self.assertTrue(all(item.source_segment_id == 7 for item in result))

    def test_every_split_caption_gets_the_minimum_reading_time(self):
        source = SubtitleCue(0, 1_000, "a" * 18 + " b", 9)
        result = prepare_short_captions(
            [source], max_chars=18, minimum_part_ms=500,
        )
        self.assertEqual(len(result), 2)
        self.assertTrue(
            all(item.end_ms - item.start_ms >= 500 for item in result)
        )

    def test_ass_escapes_control_syntax_and_uses_portrait_canvas(self):
        output = captions_to_ass(
            [SubtitleCue(0, 1_500, r"字幕{強調}\test", 1)], 720, 1280,
        )
        self.assertIn("PlayResX: 720", output)
        self.assertIn("PlayResY: 1280", output)
        self.assertIn("字幕｛強調｝＼test", output)
        self.assertNotIn(r"{強調}", output)

    @patch("moment_retrieval.short_video.subprocess.run")
    def test_renderer_uses_relative_caption_path_on_windows(self, run):
        def create_output(command, **_kwargs):
            Path(command[-1]).touch()
            return subprocess.CompletedProcess(command, 0)

        run.side_effect = create_output
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "short.mp4"
            render_short_clip(
                Path(temporary) / "source.mp4", 10, 20, output,
                captions=[SubtitleCue(0, 1_000, "字幕", 1)],
                options=ShortVideoOptions(720, 1280, "blur", True),
            )
            command = run.call_args.args[0]
            filter_value = command[command.index("-filter_complex") + 1]
            self.assertIn("subtitles=filename=captions.ass", filter_value)
            self.assertIsNotNone(run.call_args.kwargs["cwd"])
            self.assertTrue(output.exists())
            self.assertFalse(list(Path(temporary).glob("cut_video_short_*")))


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "ffmpeg and ffprobe are required",
)
class ShortVideoIntegrationTests(unittest.TestCase):
    def test_real_ffmpeg_renders_vertical_captioned_video(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            output = root / "short.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=2",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                str(source),
            ], check=True, capture_output=True)
            render_short_clip(
                source, 0, 2, output,
                captions=[SubtitleCue(100, 1_800, "自動字幕の確認", 1)],
                options=ShortVideoOptions(720, 1280, "blur", True),
                duration=2,
            )
            probe = subprocess.run([
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "json", str(output),
            ], check=True, capture_output=True, text=True)
            payload = json.loads(probe.stdout)
            self.assertEqual(
                (payload["streams"][0]["width"], payload["streams"][0]["height"]),
                (720, 1280),
            )
            self.assertAlmostEqual(float(payload["format"]["duration"]), 2.0, delta=0.15)


if __name__ == "__main__":
    unittest.main()
