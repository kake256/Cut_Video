"""Finishing pass: plan validation, word-timed captions, sound-effect placement (no ffmpeg run)."""
import json
import unittest

from moment_retrieval import finishing
from moment_retrieval.finishing import Caption, FinishPlan, Word


class PlanTest(unittest.TestCase):
    def test_plan_is_clamped_and_effects_are_spaced(self):
        plan = finishing.validate_plan({
            "hook_text": "とても長い引きのタイトルはここで切れるはず", "caption_preset": "huge",
            "caption_position": "top", "trim_start": 9, "trim_end": -1,
            "sound_effects": [{"at": 5, "type": "pop"}, {"at": 5.5, "type": "ding"},
                              {"at": 12, "type": "laser"}, {"at": 20, "type": "thud"},
                              {"at": 59.9, "type": "rise"}],
        }, 60.0)
        self.assertEqual(len(plan.hook_text), 14)
        self.assertTrue(plan.hook_text.endswith("…"))
        self.assertEqual((plan.caption_preset, plan.caption_position), ("large", "bottom"))
        self.assertEqual((plan.trim_start, plan.trim_end), (3.0, 0.0))
        self.assertEqual(plan.sound_effects, [(2.0, "pop"), (17.0, "thud")])
        self.assertEqual(finishing.validate_plan("not a dict", 30).sound_effects, [])

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


class RenderPlanTest(unittest.TestCase):
    def test_effect_line_pops_and_audio_is_mixed_with_a_limiter(self):
        plan = FinishPlan(hook_text="引き", sound_effects=[(1.0, "thud")])
        ass = finishing.build_ass([Caption(0.5, 1.5, "キレた"), Caption(2.0, 3.0, "落ち着く")], plan, 1080, 1920, 4.0)
        self.assertIn(r"\fscx118", ass.split("キレた")[0].splitlines()[-1])
        self.assertNotIn(r"\fscx118", [line for line in ass.splitlines() if "落ち着く" in line][0])
        self.assertIn("Hook,,0,0,0,,引き", ass)
        graph = finishing.build_filter(1080, 1920, plan.sound_effects)
        self.assertIn("adelay=1000|1000", graph)
        self.assertIn("alimiter", graph)
        self.assertNotIn("zoompan", graph)
        self.assertIn("[0:a]anull[aout]", finishing.build_filter(1080, 1920, []))


if __name__ == "__main__":
    unittest.main()
