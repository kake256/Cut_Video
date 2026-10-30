import os
import unittest
from unittest.mock import patch

from moment_retrieval import short_video, video_encoder


class VideoEncoderTest(unittest.TestCase):
    def test_cpu_is_forced_by_environment(self):
        with patch.dict(os.environ, {"CUT_VIDEO_ENCODER": "cpu"}):
            args = video_encoder.h264_args(crf=20, preset="medium")
        self.assertEqual(args[:2], ["-c:v", "libx264"])
        self.assertIn("-crf", args)

    def test_nvenc_uses_constant_quality(self):
        with patch.dict(os.environ, {"CUT_VIDEO_ENCODER": "nvenc"}):
            args = video_encoder.h264_args(crf=20, preset="medium")
        self.assertEqual(args[:2], ["-c:v", "h264_nvenc"])
        self.assertEqual(args[args.index("-cq") + 1], "28")
        self.assertNotIn("-crf", args)

    def test_auto_falls_back_to_cpu_when_the_probe_fails(self):
        with patch.dict(os.environ, {"CUT_VIDEO_ENCODER": "auto"}), \
                patch.object(video_encoder, "_nvenc_ok", None), \
                patch.object(video_encoder, "_probe_nvenc", return_value=False):
            self.assertEqual(video_encoder.h264_args(crf=20, preset="medium")[1], "libx264")


class BlurredBackgroundTest(unittest.TestCase):
    def test_blur_runs_on_a_quarter_size_copy(self):
        chain = short_video.blurred_background(1080, 1920)
        self.assertIn("scale=270:480", chain)
        self.assertTrue(chain.endswith("scale=1080:1920"))
        self.assertIn("scale=270:270", short_video.blurred_background(1080, 1080))
