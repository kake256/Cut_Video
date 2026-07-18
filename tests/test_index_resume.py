import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import index_video
from moment_retrieval import db


def _segment(start: float, end: float, text: str):
    return SimpleNamespace(start=start, end=end, text=text, words=[])


class _InterruptedSegments:
    def __init__(self, segments, message="synthetic interruption"):
        self._segments = list(segments)
        self._message = message

    def __iter__(self):
        yield from self._segments
        raise RuntimeError(self._message)


class IndexResumeCharacterizationTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="cut_index_resume_")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.conn = sqlite3.connect(self.root / "index.db")
        self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn)
        self.addCleanup(self.conn.close)
        self.video = self.root / "synthetic-source.mp4"
        self.video.write_bytes(b"synthetic source marker")

    def _transcribe(self, transcribe_stream, *, flush_interval):
        with (
            patch(
                "moment_retrieval.asr.transcribe_stream",
                side_effect=transcribe_stream,
            ),
            patch.object(
                index_video,
                "ASR_FLUSH_INTERVAL_SEC",
                flush_interval,
            ),
        ):
            return list(
                index_video._transcribe_with_progress(
                    self.conn,
                    "synthetic-video",
                    self.video,
                    10.0,
                    "tiny",
                    "cpu",
                    "int8",
                    "ja",
                    batch_size=1,
                )
            )

    def test_interrupted_transcription_resumes_from_last_committed_end(self):
        first_segments = _InterruptedSegments(
            [
                _segment(0.0, 1.0, "alpha"),
                _segment(1.0, 2.1, "beta"),
            ]
        )

        def first_stream(*args, **kwargs):
            self.assertEqual(kwargs["start_offset"], 0.0)
            return iter(first_segments), object(), 1

        with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
            self._transcribe(first_stream, flush_interval=2.0)

        self.assertEqual(db.get_last_segment_end(self.conn, "synthetic-video"), 2.1)
        self.assertEqual(len(db.get_segments(self.conn, "synthetic-video")), 2)
        self.assertFalse(db.is_asr_complete(self.conn, "synthetic-video"))

        tail = self.root / "synthetic-tail.wav"
        tail.write_bytes(b"synthetic tail")
        resumed_call = {}

        def resumed_stream(audio_path, **kwargs):
            resumed_call["audio_path"] = Path(audio_path)
            resumed_call["start_offset"] = kwargs["start_offset"]
            return iter([_segment(2.1, 3.2, "gamma")]), object(), 1

        with patch.object(index_video, "_extract_audio_tail", return_value=tail) as extract:
            messages = self._transcribe(resumed_stream, flush_interval=2.0)

        extract.assert_called_once_with(self.video, 2.1)
        self.assertEqual(resumed_call["audio_path"], tail)
        self.assertEqual(resumed_call["start_offset"], 2.1)
        self.assertFalse(tail.exists())
        self.assertEqual(
            [row["text"] for row in db.get_segments(self.conn, "synthetic-video")],
            ["alpha", "beta", "gamma"],
        )
        self.assertTrue(db.is_asr_complete(self.conn, "synthetic-video"))
        self.assertTrue(any("途中結果を検出" in message for message in messages))
        self.assertTrue(any("文字起こし完了" in message for message in messages))

    def test_interruption_before_first_flush_restarts_from_zero(self):
        def interrupted_stream(*args, **kwargs):
            self.assertEqual(kwargs["start_offset"], 0.0)
            return iter(_InterruptedSegments([_segment(0.0, 1.0, "alpha")])), object(), 1

        with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
            self._transcribe(interrupted_stream, flush_interval=10.0)

        self.assertIsNotNone(db.get_video(self.conn, "synthetic-video"))
        self.assertEqual(db.get_last_segment_end(self.conn, "synthetic-video"), 0.0)
        self.assertEqual(db.get_segments(self.conn, "synthetic-video"), [])
        self.assertFalse(db.is_asr_complete(self.conn, "synthetic-video"))

        resumed_offsets = []

        def restart_stream(*args, **kwargs):
            resumed_offsets.append(kwargs["start_offset"])
            return iter([_segment(0.0, 1.0, "alpha")]), object(), 1

        with patch.object(index_video, "_extract_audio_tail") as extract:
            self._transcribe(restart_stream, flush_interval=10.0)

        extract.assert_not_called()
        self.assertEqual(resumed_offsets, [0.0])
        self.assertEqual(len(db.get_segments(self.conn, "synthetic-video")), 1)
        self.assertTrue(db.is_asr_complete(self.conn, "synthetic-video"))


if __name__ == "__main__":
    unittest.main()
