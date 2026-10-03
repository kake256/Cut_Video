import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from moment_retrieval.short_video import (
    ShortVideoOptions,
    ShortVideoCancelled,
    build_audio_filter,
    build_source_caption_filter,
    build_short_filter,
    captions_to_ass,
    parse_short_resolution,
    prepare_short_captions,
    render_captioned_source_clip,
    render_short_clip,
    wrap_caption_for_canvas,
)
from moment_retrieval.output_profile import (
    AudioProfile,
    CaptionProfile,
    OutputProfile,
    caption_style_for_canvas,
    validate_font_glyphs,
)
from moment_retrieval.publication import private_source_fingerprint
from moment_retrieval.subtitles import SubtitleCue


class ShortVideoUnitTests(unittest.TestCase):
    def test_legacy_short_options_round_trip_through_output_profile(self):
        legacy = ShortVideoOptions(720, 1280, "crop", False)
        profile = legacy.to_output_profile()
        self.assertEqual(profile.canvas_mode, "portrait_crop")
        self.assertEqual(profile.width, 720)
        self.assertFalse(profile.caption.enabled)
        self.assertEqual(ShortVideoOptions.from_output_profile(profile), legacy)
        self.assertEqual(
            build_short_filter(profile, include_captions=False),
            build_short_filter(legacy, include_captions=False),
        )

    def test_output_profile_keeps_source_canvas_separate_from_portrait_canvas(self):
        source = OutputProfile.source()
        self.assertEqual(source.canvas_mode, "source")
        self.assertIsNone(source.width)
        with self.assertRaises(ValueError):
            OutputProfile(canvas_mode="source", width=720, height=1280).validate()
        square = OutputProfile.square(720)
        self.assertEqual(square.canvas_mode, "square_fit")
        self.assertEqual((square.width, square.height), (720, 720))
        with self.assertRaisesRegex(ValueError, "equal dimensions"):
            replace(square, height=718).validate()

    def test_audio_profile_manifest_contains_no_local_path(self):
        profile = AudioProfile(
            normalize_source=True,
            normalization_applied=True,
            bgm_enabled=True,
            bgm_applied=True,
            bgm_name="music.mp3",
            bgm_fingerprint="a" * 64,
            bgm_gain_db=-22,
        ).validate()
        manifest = profile.to_manifest()
        self.assertEqual(manifest["bgm_name"], "music.mp3")
        self.assertNotIn("path", manifest)
        with self.assertRaisesRegex(ValueError, "basename"):
            replace(profile, bgm_name=r"private\music.mp3").validate()

    def test_audio_filter_normalizes_speech_and_mixes_bgm(self):
        profile = AudioProfile(
            normalize_source=True,
            normalization_applied=True,
            bgm_enabled=True,
            bgm_applied=True,
            bgm_name="music.wav",
            bgm_fingerprint="b" * 64,
            bgm_gain_db=-26,
            bgm_fade_in_sec=0.5,
            bgm_fade_out_sec=1.0,
        )
        graph = build_audio_filter(
            profile, duration=8.0, source_has_audio=True, bgm_has_audio=True,
        )
        self.assertIn("loudnorm=I=-16:LRA=11:TP=-1.5", graph)
        self.assertIn("volume=-26dB", graph)
        self.assertIn("afade=t=out:st=7.000:d=1.000", graph)
        self.assertIn("amix=inputs=2", graph)

    def test_caption_presets_resolve_safe_margins_and_positions(self):
        standard = caption_style_for_canvas(CaptionProfile(), 720, 1280)
        large_top = caption_style_for_canvas(
            CaptionProfile(preset="large", position="top"), 720, 1280,
        )
        boxed_center = caption_style_for_canvas(
            CaptionProfile(preset="boxed", position="center"), 720, 1280,
        )
        self.assertEqual(standard.alignment, 2)
        self.assertEqual(large_top.alignment, 8)
        self.assertGreater(large_top.font_size, standard.font_size)
        self.assertEqual(boxed_center.alignment, 5)
        self.assertEqual(boxed_center.border_style, 3)
        self.assertGreaterEqual(standard.margin_left, 40)
        self.assertGreaterEqual(standard.margin_vertical, 64)

    def test_ass_uses_preset_and_position_without_changing_safe_wrapping(self):
        output = captions_to_ass(
            [SubtitleCue(0, 1_500, "字幕テスト", 1)], 720, 1280,
            caption_profile=CaptionProfile(preset="boxed", position="top"),
        )
        style = next(line for line in output.splitlines() if line.startswith("Style:"))
        self.assertIn(",3,0,0,8,", style)
        self.assertIn("&H50000000", style)

    def test_standard_preset_keeps_the_legacy_ass_style_values(self):
        output = captions_to_ass([SubtitleCue(0, 1_000, "字幕", 1)], 720, 1280)
        style = next(line for line in output.splitlines() if line.startswith("Style:"))
        self.assertEqual(
            style,
            "Style: Default,Yu Gothic UI,52,&H00FFFFFF,&H000000FF,&H00000000,"
            "&H78000000,-1,0,0,0,100,100,0,0,1,4,2,2,47,47,134,1",
        )

    def test_font_validation_is_pure_when_an_inspector_is_supplied(self):
        class FakeInspector:
            def resolve_face(self, _font_name):
                return "Yu Gothic UI"

            def missing_glyphs(self, _font_name, _text):
                return ("□",)

        result = validate_font_glyphs("Yu Gothic UI", "字幕□", inspector=FakeInspector())
        self.assertTrue(result.font_available)
        self.assertFalse(result.glyphs_supported)
        self.assertEqual(result.missing_glyph_count, 1)
        self.assertEqual(result.warning, "FONT_FALLBACK_REQUIRED")

    def test_caption_font_name_rejects_ass_field_separator(self):
        with self.assertRaisesRegex(ValueError, "font name"):
            CaptionProfile(font_name="unsafe,font").validate()

    def test_low_resolution_caption_keeps_legacy_vertical_safe_margin(self):
        style = caption_style_for_canvas(CaptionProfile(), 640, 360)
        self.assertEqual(style.margin_vertical, 64)

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
        square = build_short_filter(
            OutputProfile.square(
                720, caption=CaptionProfile(enabled=False),
            ),
            include_captions=False,
        )
        self.assertIn("scale=720:720", square)
        self.assertIn("boxblur", square)
        self.assertIn("overlay", square)
        source = build_source_caption_filter()
        self.assertIn("setpts=PTS-STARTPTS", source)
        self.assertIn("subtitles=filename=captions.ass", source)

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

    def test_default_caption_block_is_about_ten_characters_when_timing_allows(self):
        source = SubtitleCue(0, 5_000, "あいうえおかきくけこさしすせそたちつてとなにぬ", 4)
        result = prepare_short_captions([source])
        self.assertGreater(len(result), 1)
        self.assertTrue(all(len(item.text) <= 10 for item in result))
        self.assertEqual(result[0].start_ms, 0)
        self.assertEqual(result[-1].end_ms, 5_000)

    def test_ass_escapes_control_syntax_and_uses_portrait_canvas(self):
        output = captions_to_ass(
            [SubtitleCue(0, 1_500, r"字幕{強調}\test", 1)], 720, 1280,
        )
        self.assertIn("PlayResX: 720", output)
        self.assertIn("PlayResY: 1280", output)
        self.assertIn("字幕｛強調｝＼test", output)
        self.assertNotIn(r"{強調}", output)

    def test_caption_wrap_uses_canvas_width_and_fullwidth_glyphs(self):
        text = "これは画面の横幅を超えないように自動改行される長い日本語字幕です"
        portrait = wrap_caption_for_canvas(text, 720, 52, 47, 47)
        landscape = wrap_caption_for_canvas(text, 1920, 44, 125, 125)
        self.assertIn("\n", portrait)
        self.assertGreater(len(portrait.splitlines()), len(landscape.splitlines()))
        self.assertEqual("".join(portrait.splitlines()), text)

    def test_ass_writes_explicit_safe_width_line_breaks(self):
        text = "字幕が画面の外にはみ出さないことを確認するための長い日本語字幕です"
        output = captions_to_ass([SubtitleCue(0, 1_500, text, 1)], 720, 1280)
        dialogue = next(
            line for line in output.splitlines() if line.startswith("Dialogue:")
        )
        self.assertIn(r"\N", dialogue)

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

    @patch("moment_retrieval.short_video.subprocess.run")
    @patch("moment_retrieval.short_video.subprocess.Popen")
    def test_renderer_honors_cancel_before_ffmpeg_start(self, popen, run):
        cancel = threading.Event()
        cancel.set()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "cancelled.mp4"
            with self.assertRaises(ShortVideoCancelled):
                render_short_clip(
                    Path(temporary) / "source.mp4", 0, 1, output,
                    options=ShortVideoOptions(720, 1280, "blur", False),
                    cancel_event=cancel,
                )
            self.assertFalse(output.exists())
        run.assert_not_called()
        popen.assert_not_called()

    @patch("moment_retrieval.short_video.captions_to_ass", return_value="ass")
    @patch("moment_retrieval.short_video.subprocess.run")
    def test_short_renderer_prepares_long_asr_cues_before_burn_in(
        self, run, to_ass,
    ):
        def create_output(command, **_kwargs):
            Path(command[-1]).touch()
            return subprocess.CompletedProcess(command, 0)

        run.side_effect = create_output
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "short.mp4"
            render_short_clip(
                Path(temporary) / "source.mp4", 0, 4, output,
                captions=[SubtitleCue(0, 4_000, "長い字幕本文" * 12, 1)],
                options=ShortVideoOptions(720, 1280, "blur", True),
            )
        prepared = tuple(to_ass.call_args.args[0])
        self.assertGreater(len(prepared), 1)
        self.assertEqual(prepared[0].start_ms, 0)
        self.assertEqual(prepared[-1].end_ms, 4_000)

    @patch(
        "moment_retrieval.short_video.probe_video_dimensions",
        return_value=(640, 360),
    )
    @patch("moment_retrieval.short_video.subprocess.run")
    def test_source_renderer_preserves_canvas_and_uses_relative_ass(
        self, run, probe,
    ):
        def create_output(command, **_kwargs):
            Path(command[-1]).touch()
            return subprocess.CompletedProcess(command, 0)

        run.side_effect = create_output
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "captioned.mp4"
            source = Path(temporary) / "source.mp4"
            render_captioned_source_clip(
                source, 0, 2, output,
                captions=[SubtitleCue(0, 1_000, "字幕", 1)],
                duration=2,
            )
            command = run.call_args.args[0]
            filter_value = command[command.index("-filter_complex") + 1]
            self.assertEqual(filter_value, build_source_caption_filter())
            self.assertNotIn("scale=", filter_value)
            self.assertNotIn("crop=", filter_value)
            self.assertTrue(output.exists())
            self.assertFalse(list(Path(temporary).glob("cut_video_captioned_*")))
        probe.assert_called_once_with(source.resolve())

    @patch(
        "moment_retrieval.short_video.probe_video_dimensions",
        return_value=(640, 360),
    )
    @patch("moment_retrieval.short_video.subprocess.run")
    def test_source_renderer_uses_output_profile_encoding_values(self, run, _probe):
        def create_output(command, **_kwargs):
            Path(command[-1]).touch()
            return subprocess.CompletedProcess(command, 0)

        run.side_effect = create_output
        profile = replace(
            OutputProfile.source(caption=CaptionProfile(enabled=True)),
            crf=24,
            encoding_preset="fast",
        ).validate()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "captioned.mp4"
            render_captioned_source_clip(
                Path(temporary) / "source.mp4", 0, 1, output,
                captions=[SubtitleCue(0, 800, "字幕", 1)],
                output_profile=profile,
                duration=1,
            )
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-crf") + 1], "24")
        self.assertEqual(command[command.index("-preset") + 1], "fast")

    @patch("moment_retrieval.short_video.probe_audio_stream", return_value=True)
    @patch("moment_retrieval.short_video.subprocess.run")
    def test_short_renderer_applies_normalization_from_output_profile(
        self, run, _audio_probe,
    ):
        def create_output(command, **_kwargs):
            Path(command[-1]).touch()
            return subprocess.CompletedProcess(command, 0)

        run.side_effect = create_output
        profile = OutputProfile.portrait(
            720, 1280,
            caption=CaptionProfile(enabled=False),
            audio=AudioProfile(
                normalize_source=True, normalization_applied=True,
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "normalized.mp4"
            render_short_clip(
                Path(temporary) / "source.mp4", 0, 2, output,
                options=profile, duration=2,
            )
        command = run.call_args.args[0]
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("loudnorm", graph)
        self.assertIn("[aout]", command)

    @patch("moment_retrieval.short_video.probe_audio_stream", return_value=True)
    @patch("moment_retrieval.short_video.subprocess.run")
    def test_source_renderer_uses_ephemeral_bgm_path_without_filter_interpolation(
        self, run, _audio_probe,
    ):
        def create_output(command, **_kwargs):
            Path(command[-1]).touch()
            return subprocess.CompletedProcess(command, 0)

        run.side_effect = create_output
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bgm = root / "private music.wav"
            bgm.write_bytes(b"synthetic-bgm")
            audio = AudioProfile(
                bgm_enabled=True,
                bgm_applied=True,
                bgm_name=bgm.name,
                bgm_fingerprint=private_source_fingerprint(bgm),
            )
            profile = OutputProfile.source(
                caption=CaptionProfile(enabled=False), audio=audio,
            )
            output = root / "mixed.mp4"
            render_captioned_source_clip(
                root / "source.mp4", 0, 2, output,
                captions=(), output_profile=profile, bgm_path=bgm, duration=2,
            )
        command = run.call_args.args[0]
        graph = command[command.index("-filter_complex") + 1]
        self.assertNotIn(str(bgm), graph)
        self.assertIn(str(bgm.resolve()), command)
        self.assertIn("amix=inputs=2", graph)


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

            horizontal = root / "captioned-source.mp4"
            render_captioned_source_clip(
                source, 0, 2, horizontal,
                captions=[SubtitleCue(100, 1_800, "横長字幕の確認", 1)],
                duration=2,
            )
            horizontal_probe = subprocess.run([
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "json", str(horizontal),
            ], check=True, capture_output=True, text=True)
            horizontal_payload = json.loads(horizontal_probe.stdout)
            self.assertEqual(
                (
                    horizontal_payload["streams"][0]["width"],
                    horizontal_payload["streams"][0]["height"],
                ),
                (640, 360),
            )

    def test_real_ffmpeg_normalizes_and_mixes_local_bgm(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            bgm = root / "bgm.wav"
            output = root / "mixed.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=size=320x180:rate=24:duration=1.5",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                str(source),
            ], check=True, capture_output=True)
            subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=220:duration=1.5",
                str(bgm),
            ], check=True, capture_output=True)
            profile = OutputProfile.source(
                caption=CaptionProfile(enabled=False),
                audio=AudioProfile(
                    normalize_source=True,
                    normalization_applied=True,
                    bgm_enabled=True,
                    bgm_applied=True,
                    bgm_name=bgm.name,
                    bgm_fingerprint=private_source_fingerprint(bgm),
                    bgm_gain_db=-30,
                ),
            )
            render_captioned_source_clip(
                source, 0, 1.5, output, captions=(),
                output_profile=profile, bgm_path=bgm, duration=1.5,
            )
            probe = subprocess.run([
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_type", "-of", "json", str(output),
            ], check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(probe.stdout)["streams"][0]["codec_type"], "audio")

    def test_real_ffmpeg_renders_square_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            output = root / "square.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
            ], check=True, capture_output=True)
            render_short_clip(
                source, 0, 1, output,
                options=OutputProfile.square(
                    360, caption=CaptionProfile(enabled=False),
                ),
                duration=1,
            )
            probe = subprocess.run([
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "json", str(output),
            ], check=True, capture_output=True, text=True)
            stream = json.loads(probe.stdout)["streams"][0]
            self.assertEqual((stream["width"], stream["height"]), (360, 360))


if __name__ == "__main__":
    unittest.main()
