"""聞き直し(relisten)・確定字幕: GPU/ffmpegなしで検証する。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
import cut_mcp
from moment_retrieval import agent_runner, db, finishing, relisten
from moment_retrieval.mcp_library import LibraryToolError, LibraryTools


class RelistenRangeTest(unittest.TestCase):
    def _run(self, start, end, raw):
        with patch.object(relisten, "_extract_audio") as extract, \
                patch.object(relisten, "_transcribe_words", return_value=raw):
            return relisten.retranscribe_range(Path("x.mp4"), start, end), extract

    def test_words_are_shifted_to_absolute_time(self):
        words, extract = self._run(100.0, 110.0, [
            {"start": 0.5, "end": 1.0, "word": " あ "}, {"start": 2.0, "end": 12.0, "word": "い"},
            {"start": 3.0, "end": 3.2, "word": " "}, {"start": 11.0, "end": 12.0, "word": "範囲外"}])
        self.assertEqual(words, [{"start": 100.5, "end": 101.0, "word": "あ"},
                                 {"start": 102.0, "end": 110.0, "word": "い"}])
        extract.assert_called_once()

    def test_invalid_ranges_are_rejected(self):
        for start, end in ((5, 5), (6, 5), (-1, 3), (0, 601)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                relisten.retranscribe_range(Path("x.mp4"), start, end)


class ConfirmedCaptionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "index.db"
        self.media = Path(self.tmp.name) / "synthetic.mp4"
        self.media.write_bytes(b"x")
        conn = self.connect()
        db.init_db(conn, create_backup=False)
        db.insert_video(conn, "vid_" + "d" * 32, str(self.media), 120)
        for index, text in enumerate(["あ", "い", "う"]):
            db.insert_segment(conn, "vid_" + "d" * 32, SimpleNamespace(
                start=float(index * 10), end=float(index * 10 + 9), text=text, words=[]))
        self.revision = db.mark_asr_complete(conn, "vid_" + "d" * 32)
        self.vid = db.public_video_id(conn, "vid_" + "d" * 32)
        conn.close()
        self.tools = LibraryTools(self.connect)

    def tearDown(self):
        self.tmp.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _cue(self, start, end, text):
        return {"start_ms": start, "end_ms": end, "text": text}

    def test_set_captions_round_trip_with_tolerance(self):
        self.tools.set_captions(self.vid, 1000, 11000, [self._cue(0, 500, " こん  にちは "), self._cue(500, 900, "あ")])
        conn = self.connect()
        found = db.find_confirmed_captions(conn, self.vid, self.revision, 1030, 10980)
        self.assertEqual(found[0]["text"], "こん にちは")
        self.assertIsNone(db.find_confirmed_captions(conn, self.vid, self.revision, 1200, 11000))
        self.assertIsNone(db.find_confirmed_captions(conn, self.vid, "other", 1000, 11000))
        conn.close()

    def test_set_captions_validation(self):
        bad = [
            [self._cue(0, 500, "あ" * 11)],
            [self._cue(0, 500, "  ")],
            [self._cue(0, 500, "あ"), self._cue(400, 800, "い")],
            [self._cue(600, 800, "あ"), self._cue(0, 500, "い")],
            [self._cue(0, 10001, "あ")],
            [self._cue(300, 300, "あ")],
            [self._cue(0, 1, "あ")] * 301,
            [],
        ]
        for captions in bad:
            with self.subTest(captions=captions[:1]), self.assertRaises(LibraryToolError):
                self.tools.set_captions(self.vid, 0, 10000, captions)

    def test_relisten_tool_returns_relative_ms_and_needs_consent(self):
        server = cut_mcp.CaptionTools(library=self.tools)
        args = {"video_id": self.vid, "start_ms": 10000, "end_ms": 20000}
        with self.assertRaises(cut_mcp.ToolError) as ctx:
            server.call("cut_relisten_range", {**args, "allow_transcript_transfer": False})
        self.assertEqual(ctx.exception.code, "PRIVACY_CONFIRMATION_REQUIRED")
        raw = [{"start": 10.5, "end": 11.0, "word": "やった"}]
        with patch("moment_retrieval.mcp_library.relisten.retranscribe_range", return_value=raw) as call:
            result = server.call("cut_relisten_range", {**args, "allow_transcript_transfer": True})
        self.assertEqual(call.call_args.args[1:], (10.0, 20.0))
        self.assertEqual(result["untrusted_source_data"], [{"start_ms": 500, "end_ms": 1000, "word": "やった"}])
        self.assertIn("cut_set_captions", result["notice"])
        with self.assertRaises(cut_mcp.ToolError) as ctx:
            server.call("cut_relisten_range", {**args, "end_ms": 700000, "allow_transcript_transfer": True})
        self.assertEqual(ctx.exception.code, "VALIDATION_ERROR")

    def test_set_captions_through_mcp_and_tool_listing(self):
        server = cut_mcp.CaptionTools(library=self.tools)
        names = {tool["name"] for tool in cut_mcp.TOOLS}
        self.assertLessEqual({"cut_relisten_range", "cut_set_captions"}, names)
        result = server.call("cut_set_captions", {
            "video_id": self.vid, "start_ms": 0, "end_ms": 9000, "captions": [self._cue(0, 800, "やった！")]})
        self.assertEqual(result["saved_caption_count"], 1)
        with self.assertRaises(cut_mcp.ToolError):
            server.call("cut_set_captions", {
                "video_id": self.vid, "start_ms": 0, "end_ms": 9000, "captions": [self._cue(0, 800, "あ" * 11)]})

    def test_export_helper_converts_confirmed_captions(self):
        conn = self.connect()
        db.save_confirmed_captions(conn, self.vid, self.revision, 1000, 11000,
                                   [self._cue(0, 500, "あ"), self._cue(9000, 12000, "い")])
        conn.close()
        video = {"public_video_id": self.vid, "_highlight_transcript_revision": self.revision}
        with patch.object(app.db, "get_conn", self.connect):
            cues = app._confirmed_highlight_captions(video, {"start_sec": 1.0, "end_sec": 11.0})
            none = app._confirmed_highlight_captions(video, {"start_sec": 2.0, "end_sec": 11.0})
        self.assertEqual([(c.start_ms, c.end_ms, c.text) for c in cues], [(0, 500, "あ"), (9000, 10000, "い")])
        self.assertEqual(none, ())

    def test_agent_prompt_mentions_relisten_steps(self):
        prompt = agent_runner.ClipRequest(video_id="vid_x").prompt()
        self.assertIn("cut_relisten_range", prompt)
        self.assertIn("cut_set_captions", prompt)
        self.assertLess(prompt.index("cut_propose_clips"), prompt.index("cut_relisten_range"))
        self.assertIn("cut_set_captions", agent_runner.CLAUDE_TOOLS)

    def test_finishing_uses_relisten_and_falls_back(self):
        segments = [{"start_sec": 10.0, "end_sec": 12.0, "text": "あい",
                     "words_json": '[{"word":"あい","start":10.5,"end":11.0}]'}]
        raw = [{"start": 10.2, "end": 10.6, "word": "や"}, {"start": 10.6, "end": 11.0, "word": "った"}]
        with patch("moment_retrieval.relisten.retranscribe_range", return_value=raw):
            words, source = finishing.words_for_clip(self.media, segments, 10.0, 20.0)
        self.assertEqual(source, "relisten")
        self.assertEqual([w.text for w in words], ["や", "った"])
        self.assertAlmostEqual(words[0].start, 0.2)
        logs = []
        with patch("moment_retrieval.relisten.retranscribe_range", side_effect=RuntimeError("no gpu")):
            words, source = finishing.words_for_clip(self.media, segments, 10.0, 20.0, logs.append)
        self.assertEqual(source, "transcript")
        self.assertEqual([w.text for w in words], ["あい"])
        self.assertTrue(logs)


if __name__ == "__main__":
    unittest.main()


class RelistenHallucinationFilterTest(unittest.TestCase):
    def test_noise_like_segments_are_dropped_and_speech_is_kept(self):
        from types import SimpleNamespace

        from moment_retrieval import relisten

        def segment(text, no_speech=0.2, logprob=-0.4, ratio=1.3):
            return SimpleNamespace(text=text, no_speech_prob=no_speech, avg_logprob=logprob, compression_ratio=ratio)

        self.assertTrue(relisten._keep_segment(segment("もうキモいお前")))
        self.assertFalse(relisten._keep_segment(segment("ええええええ", no_speech=0.38, logprob=-1.36)))
        self.assertFalse(relisten._keep_segment(segment("ご視聴ありがとうございました")))
        self.assertFalse(relisten._keep_segment(segment("あ" * 40, ratio=3.0)))
        self.assertTrue(relisten._keep_segment(segment("話があって", no_speech=0.92, logprob=-0.19)))
