"""Finishing pass: plan validation, word-timed captions, sound-effect placement (no ffmpeg run)."""
import json
import unittest

from moment_retrieval import finishing, short_style
from moment_retrieval.finishing import Caption, FinishPlan, Word


class PlanTest(unittest.TestCase):
    def test_plan_is_clamped_and_effects_are_spaced(self):
        plan = finishing.validate_plan({
            "title_top": "外国人に道を聞かれて", "title_emphasis": "とりあえず「Yes」と答えたらこうなった",
            "trim_start": 9, "trim_end": -1,
            "sound_effects": [{"at": 5, "type": "pop"}, {"at": 5.5, "type": "ding"},
                              {"at": 12, "type": "laser"}, {"at": 20, "type": "thud"},
                              {"at": 59.9, "type": "rise"}],
            "notes": [{"start": 2, "end": 8, "text": "※渋谷には行きません"}, {"start": 9, "end": 9.2, "text": "短すぎ"},
                      {"start": 10, "end": 90, "text": "あ" * 30}, {"start": 20, "end": 30, "text": "3つ目"}],
        }, 60.0)
        self.assertEqual(plan.title_lines[0], ("外国人に道を聞かれて", False))
        self.assertTrue(plan.title_lines[1][1])
        self.assertTrue(plan.title_lines[1][0].endswith("…"))
        self.assertLessEqual(short_style.display_units(plan.title_lines[1][0]), short_style.TITLE_LINE_UNITS)
        self.assertEqual((plan.trim_start, plan.trim_end), (3.0, 0.0))
        self.assertEqual(plan.sound_effects, [(2.0, "pop"), (17.0, "thud")])
        self.assertEqual(plan.notes, [(2.0, 8.0, "※渋谷には行きません"), (10.0, 60.0, "あ" * 20)])
        self.assertEqual(finishing.validate_plan("not a dict", 30).sound_effects, [])

    def test_posting_title_becomes_a_two_line_title(self):
        plan = finishing.validate_plan({"hook_text": "【ゲラサブロウ】外国人に道を聞かれてとりあえず「Yes」 #shorts"}, 30)
        self.assertEqual([yellow for _text, yellow in plan.title_lines], [False, True])
        self.assertNotIn("【", plan.hook_text)
        self.assertNotIn("#shorts", plan.hook_text)

    def test_effects_snap_to_the_nearest_spoken_word(self):
        words = [Word(1.0, 1.3, "え"), Word(4.2, 4.6, "は"), Word(9.0, 9.4, "?")]
        plan = finishing.snap_sound_effects(FinishPlan(sound_effects=[(4.5, "pop"), (7.0, "ding")]), words)
        self.assertEqual(plan.sound_effects, [(4.2, "pop"), (7.0, "ding")])


class CaptionTest(unittest.TestCase):
    def _words(self, text, start=0.0, step=0.12, pause_after=()):
        words, t = [], start
        for index, char in enumerate(text):
            words.append(Word(t, t + step, char))
            t += step + (0.8 if index in pause_after else 0.0)
        return words

    def test_captions_start_when_spoken_and_break_at_pauses(self):
        words = self._words("答えを自力で見つけることに意味がありますからね", start=2.0, pause_after=(5,))
        captions = finishing.captions_from_words(words)
        self.assertEqual(captions[0].text, "答えを自力で")
        self.assertAlmostEqual(captions[0].start, 1.95, places=2)
        self.assertEqual("".join(c.text for c in captions), "答えを自力で見つけることに意味がありますからね")
        self.assertTrue(all(len(c.text) <= 18 for c in captions))

    def test_long_runs_are_not_cut_inside_katakana_or_kanji_words(self):
        captions = finishing.captions_from_words(self._words("それでニュースの数字をもう一回見ろって言ってるんだよ"))
        joined = [c.text for c in captions]
        self.assertEqual("".join(joined), "それでニュースの数字をもう一回見ろって言ってるんだよ")
        for left, right in zip(joined, joined[1:]):
            self.assertFalse(finishing._joins_one_word(left, right), (left, right))

    def test_tiny_leftovers_are_merged(self):
        words = self._words("ほんとでちゅか", pause_after=(3,)) + [Word(2.0, 2.1, "で")]
        texts = [c.text for c in finishing.captions_from_words(words)]
        self.assertNotIn("で", texts)

    def test_words_come_from_whisper_json_relative_to_the_clip(self):
        segments = [{"start_sec": 10, "end_sec": 12, "text": "あい",
                     "words_json": json.dumps([{"word": "あ", "start": 10.0, "end": 10.5},
                                               {"word": "い", "start": 10.5, "end": 11.0}])},
                    {"start_sec": 12, "end_sec": 13, "text": "うえ", "words_json": None}]
        words = finishing.words_from_segments(segments, 10.2, 13.0)
        self.assertEqual([(round(w.start, 2), w.text) for w in words], [(0.0, "あ"), (0.3, "い"), (1.8, "うえ")])


class AiCaptionTest(unittest.TestCase):
    def _words(self, text, step=0.2):
        return [Word(i * step, i * step + step, ch) for i, ch in enumerate(text)]

    def test_ai_lines_get_whisper_times_even_after_corrections(self):
        words = self._words("でんちねでんちのつかいみちはなに")  # Whisper heard it in kana
        lines = ["でんちね。", "でんちの使い道は何？"]
        captions = finishing.captions_from_ai_lines(words, lines)
        self.assertEqual([c.text for c in captions], lines)
        self.assertAlmostEqual(captions[0].start, 0.0, places=2)
        self.assertAlmostEqual(captions[1].start, 4 * 0.2 - 0.05, places=2)
        self.assertLessEqual(captions[0].end, captions[1].start)

    def test_lines_unrelated_to_the_speech_are_rejected(self):
        words = self._words("きょうはいいてんきですね")
        self.assertIsNone(finishing.captions_from_ai_lines(words, ["まったく違う内容をでっち上げた字幕"]))

    def test_plan_keeps_caption_lines_trimmed(self):
        plan = finishing.validate_plan({"captions": ["  電池ね。 ", "", "あ" * 40]}, 30)
        self.assertEqual(plan.caption_lines, ["電池ね。", "あ" * 30])


class SpeechSnapTest(unittest.TestCase):
    def test_lines_start_at_the_voice_and_end_shortly_after_it(self):
        captions = [Caption(0.5, 3.5, "早すぎる"), Caption(5.0, 6.0, "ちょうど")]
        regions = [(0.9, 2.0), (5.0, 6.1)]
        snapped = finishing.snap_captions_to_speech(captions, regions)
        self.assertAlmostEqual(snapped[0].start, 0.87, places=2)
        self.assertAlmostEqual(snapped[0].end, 2.25, places=2)
        self.assertEqual((snapped[1].start, snapped[1].end), (5.0, 6.0))
        self.assertEqual(finishing.snap_captions_to_speech(captions, []), captions)

    def test_a_line_shown_late_moves_back_to_where_the_voice_started(self):
        snapped = finishing.snap_captions_to_speech([Caption(2.0, 3.0, "遅い")], [(1.4, 3.2)])
        self.assertAlmostEqual(snapped[0].start, 1.37, places=2)

    def test_a_line_placed_just_before_the_voice_waits_for_it(self):
        snapped = finishing.snap_captions_to_speech([Caption(2.0, 3.0, "早い")], [(2.2, 4.0)])
        self.assertAlmostEqual(snapped[0].start, 2.17, places=2)

    def test_whisper_time_is_kept_when_the_detector_missed_the_voice(self):
        # Shouting over game audio: the detector only picks the voice up later.
        snapped = finishing.snap_captions_to_speech([Caption(37.5, 38.5, "崖の2フレーム")], [(38.6, 41.2)])
        self.assertAlmostEqual(snapped[0].start, 37.5, places=2)

    def test_a_short_line_is_not_kept_up_for_long(self):
        snapped = finishing.snap_captions_to_speech([Caption(1.0, 27.0, "すばらしい！")], [(1.0, 1.5), (26.5, 27.2)])
        self.assertLessEqual(snapped[0].end - snapped[0].start, 3.0)


class RenderPlanTest(unittest.TestCase):
    def test_effect_line_pops_and_audio_is_mixed_with_a_limiter(self):
        plan = FinishPlan(hook_text="引き", sound_effects=[(1.0, "thud")], notes=[(0.5, 3.0, "※補足")],
                          footer="ゲラサブロウ 配信（2026/07/16）より")
        ass = finishing.build_ass([Caption(0.5, 1.5, "キレた"), Caption(2.0, 3.0, "落ち着く")], plan, 1080, 1920, 4.0)
        self.assertIn(r"\fscx118", ass.split("キレた")[0].splitlines()[-1])
        self.assertNotIn(r"\fscx118", [line for line in ass.splitlines() if "落ち着く" in line][0])
        self.assertIn(r"Title,,0,0,0,,{\an5\pos(540,580)\c&H3CDCFF&}引き", ass)
        self.assertIn(r"Note,,0,0,0,,{\an5\pos(540,1300)}※補足", ass)
        self.assertIn(r"Footer,,0,0,0,,{\an5\pos(540,1870)}ゲラサブロウ", ass)
        self.assertIn(r"{\an5\pos(540,1430)}落ち着く", ass)
        self.assertIn("Style: Default,Yu Gothic UI,74,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,"
                      "100,100,0,0,1,9,0,5", ass)
        graph = finishing.build_filter(1080, 1920, plan.sound_effects)
        self.assertIn("adelay=1000|1000", graph)
        # The limiter must neither turn the mix back up to 0 dBFS nor delay it against the picture.
        self.assertIn("alimiter=limit=0.95:level=disabled:latency=enabled", graph)
        self.assertIn("loudnorm=I=-16:TP=-1.5:LRA=11", graph)
        self.assertNotIn("zoompan", graph)
        self.assertNotIn("loudnorm", finishing.build_filter(1080, 1920, [], loudnorm=False))
        # Audio times are renumbered from the sample count last (loudnorm's own times can be broken).
        self.assertIn("latency=enabled,asetpts=N/SR/TB[aout]", graph)
        for loudness in (True, False, {"input_i": -30.0, "input_tp": -20.0, "input_lra": 6.0,
                                       "input_thresh": -40.0, "target_offset": 0.3}):
            self.assertIn("channel_layouts=stereo,asetpts=N/SR/TB[aout]",
                          finishing.build_filter(1080, 1920, [], loudnorm=loudness))

    def test_two_line_title_sits_just_above_the_picture(self):
        plan = FinishPlan(title_lines=[("上の行", False), ("強調の行", True)])
        ass = finishing.build_ass([], plan, 1080, 1920, 4.0)
        self.assertIn(r"{\an5\pos(540,471)}上の行", ass)
        self.assertIn(r"{\an5\pos(540,580)\c&H3CDCFF&}強調の行", ass)
        # Bottom of the second line (centre + half size + outline) stays above a 16:9 picture (y=656).
        self.assertLess(580 + short_style.TITLE_SIZE / 2 + short_style.TITLE_OUTLINE, 656)

    def test_kept_segments_are_joined_in_the_graph(self):
        graph = finishing.build_filter(1080, 1920, [], segments=[(0.0, 2.0), (3.5, 6.0)])
        self.assertIn("[vs1]trim=start=3.500:end=6.000", graph)
        self.assertIn("concat=n=2:v=1:a=1[cv][ca]", graph)
        self.assertIn("[cv]split=2[bg_src][fg_src]", graph)

    def test_two_pass_loudnorm_is_linear_and_keeps_the_true_peak(self):
        quiet = {"input_i": -30.0, "input_tp": -20.0, "input_lra": 6.0, "input_thresh": -40.0, "target_offset": 0.3}
        level = finishing.loudnorm_filter(quiet)
        self.assertTrue(level.startswith("loudnorm=I=-16:TP=-1.5:LRA=11:measured_I=-30.00:measured_TP=-20.00"))
        self.assertIn(":linear=true", level)
        # +14 dB would push a -3 dBTP peak far over -1.5: limit it first, LRA never below the clip's own.
        peaky = dict(quiet, input_tp=-3.0, input_lra=14.2)
        level = finishing.loudnorm_filter(peaky)
        self.assertTrue(level.startswith("alimiter=limit=0.1"))
        self.assertIn("LRA=14.3:", level)
        self.assertIn("measured_TP=-16.00", level)  # -1.5 - 14 - 0.5
        graph = finishing.build_filter(1080, 1920, [], loudnorm=quiet)
        self.assertIn("measured_I=-30.00", graph)

    def test_audio_only_join_for_the_measuring_pass(self):
        parts, label = finishing.audio_join_filter([(0.0, 2.0), (3.5, 6.0)])
        self.assertEqual("[ca]", label)
        self.assertEqual("[a0][a1]concat=n=2:v=0:a=1[ca]", parts[-1])
        self.assertEqual(([], "[0:a]"), finishing.audio_join_filter(None))

    def test_clip_start_moves_to_the_nearest_pause(self):
        import numpy as np

        rate = 16000
        rng = np.random.default_rng(0)

        def clip(*spans, seconds=1.6):
            x = rng.normal(0, 0.0005, int(seconds * rate))  # room noise
            for a, b in spans:
                x[int(a * rate):int(b * rate)] += 0.3 * np.sin(np.arange(int((b - a) * rate)) * 0.2)
            return x

        zero = rate  # the start is 1.0 s into the samples
        # A word that began 0.3 s before the start: open just before it.
        shift = finishing.quiet_start_shift(clip((0.7, 1.5)), rate, zero)
        self.assertTrue(-0.35 <= shift <= -0.29, shift)
        # Only the last 0.15 s of the previous sentence: skip it.
        shift = finishing.quiet_start_shift(clip((0.2, 1.15), (1.45, 1.6)), rate, zero)
        self.assertTrue(0.15 <= shift <= 0.2, shift)
        # Already in a pause: unchanged.
        self.assertEqual(0.0, finishing.quiet_start_shift(clip((1.2, 1.5)), rate, zero))
        self.assertEqual(0.0, finishing.quiet_start_shift(np.zeros(10), rate, 0))

    def test_moved_start_moves_effects_notes_and_captions_with_it(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        plan = FinishPlan(hook_text="引き", sound_effects=[(2.0, "pop")], notes=[(1.0, 3.0, "※補足")])
        words = [Word(0.1, 0.5, "最初"), Word(2.0, 2.4, "ポン")]
        calls = []
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(finishing, "speech_start_shift", return_value=-0.5), \
                patch.object(finishing, "measure_loudness", return_value=None), \
                patch.object(finishing.av_check, "verify_audio_timing"), \
                patch.object(finishing.subprocess, "run",
                             side_effect=lambda command, cwd, **_: calls.append(
                                 (command, Path(cwd, "finish.ass").read_text(encoding="utf-8")))):
            result = finishing.render(Path(tmp, "src.mp4"), 10.0, 20.0, words, plan, Path(tmp, "out.mp4"),
                                      settings={"cut_silence": False})
        command, ass = calls[0]
        self.assertEqual("9.500", command[command.index("-ss") + 1])  # opens 0.5 s earlier
        self.assertEqual(10.5, result.duration)
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("adelay=2500|2500", graph)  # the effect stays on its word
        self.assertIn("Dialogue: 1,0:00:01.50,0:00:03.50,Note", ass)
        self.assertIn("Dialogue: 0,0:00:00.55,", ass)  # the first caption starts with the first word

    def test_captions_keep_a_short_breath_as_a_space(self):
        words = [Word(0.0, 0.3, "嘘"), Word(0.3, 0.6, "だろ"), Word(0.9, 1.2, "嘘か"), Word(1.2, 1.5, "これ。")]
        self.assertEqual([c.text for c in finishing.captions_from_words(words)], ["嘘だろ 嘘かこれ"])


class StyleTest(unittest.TestCase):
    def test_wrap_prefers_spaces_and_stays_inside_the_canvas(self):
        self.assertEqual(short_style.wrap_caption("クロール 背泳ぎ 平泳ぎ バタフライ 全部できる"),
                         ["クロール 背泳ぎ 平泳ぎ", "バタフライ 全部できる"])
        lines = short_style.wrap_caption("「どうやったら渋谷に行くの？」って聞かれそうじゃん")
        self.assertTrue(all(short_style.display_units(line) <= 32 for line in lines))
        self.assertFalse(lines[1].startswith("」"))
        self.assertEqual("".join(lines), "「どうやったら渋谷に行くの？」って聞かれそうじゃん")
        self.assertEqual(short_style.wrap_caption("待って待って。"), ["待って待って"])
        self.assertEqual(short_style.wrap_caption("はいどうもゲラサブローでございます"),
                         ["はいどうも", "ゲラサブローでございます"])

    def test_pauses_are_cut_but_speech_is_kept(self):
        segments = short_style.keep_segments(20.0, [(0.2, 3.0), (3.3, 5.0), (9.0, 12.0), (12.4, 14.0)])
        self.assertEqual(segments, [(0.0, 5.25), (8.75, 14.5)])
        timeline = short_style.TimeMap(segments)
        self.assertAlmostEqual(timeline.duration, 11.0)
        self.assertEqual(timeline.span(9.0, 10.0), (5.5, 6.5))
        self.assertEqual(timeline.span(6.0, 8.0), None)  # a line inside a cut disappears
        self.assertEqual(timeline.span(4.0, 7.0), (4.0, 5.25))
        self.assertEqual(short_style.keep_segments(10.0, []), [(0.0, 10.0)])

    def test_footer_and_overrides(self):
        import json
        import tempfile
        from pathlib import Path

        self.assertEqual(short_style.footer_text("ゲラサブロウ", "20260716"), "ゲラサブロウ 配信（2026/07/16）より")
        self.assertEqual(short_style.footer_text("", ""), "")
        self.assertEqual(short_style.streamer_from_title("【ゲラサブロウ】道を聞かれて"), "ゲラサブロウ")
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp, "a.mp4")
            clip.with_suffix(".subtitle.json").write_text(json.dumps({
                "title": ["butterfly が", "読めない男"],
                "notes": [{"start": 1, "end": 4, "text": "※チャット「バターフライ」"}],
                "captions": [[0.0, 1.0, "待って待って"], [1.0, 0.5, "壊れた行"]],
            }), encoding="utf-8")
            overrides = short_style.load_overrides(clip)
            self.assertEqual(overrides.title, [("butterfly が", False), ("読めない男", True)])
            self.assertEqual(overrides.notes, [(1.0, 4.0, "※チャット「バターフライ」")])
            self.assertEqual(overrides.captions, [(0.0, 1.0, "待って待って")])
            self.assertIsNone(overrides.footer)
            self.assertEqual(short_style.load_overrides(Path(tmp, "none.mp4")).title, None)

    def test_settings_fall_back_to_defaults(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "s.json")
            path.write_text('{"streamer_name": "ゲラサブロウ", "cut_silence": "yes", "silence_min_sec": 0.01}',
                            encoding="utf-8")
            settings = short_style.load_settings(path)
        self.assertEqual(settings["streamer_name"], "ゲラサブロウ")
        self.assertTrue(settings["cut_silence"])
        self.assertEqual(settings["silence_min_sec"], 0.3)


if __name__ == "__main__":
    unittest.main()
