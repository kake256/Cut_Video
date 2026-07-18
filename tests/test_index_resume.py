import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import index_video
import numpy as np

from moment_retrieval import config, db
from moment_retrieval.publication import (
    LeaseManager,
    PublicationError,
    publish_current_generation,
)
from moment_retrieval.vector_index import VectorIndex


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


class ForceReindexPublicationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cut_force_draft_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / "synthetic-source.mp4"
        self.video.write_bytes(b"stable synthetic source")
        self.public_id = "vid_" + "2" * 32
        self.patches = [
            patch.object(config, "DB_PATH", self.root / "index.db"),
            patch.object(config, "TEXT_INDEX_PATH", self.root / "text.index"),
            patch.object(config, "SEARCH_GENERATIONS_DIR", self.root / "generations"),
            patch.object(config, "EMBED_VECTOR_DIM", 2),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

        conn = db.get_conn()
        db.init_db(conn)
        db.insert_video(conn, self.public_id, str(self.video.resolve()), 10.0)
        old_segment = _segment(0.0, 1.0, "old published transcript")
        db.insert_segment(conn, self.public_id, old_segment)
        db.mark_asr_complete(conn, self.public_id)
        old_chunk_id = db.insert_chunk(conn, self.public_id, old_segment)
        conn.commit()
        index = VectorIndex(2)
        index.add(
            np.asarray([old_chunk_id], dtype="int64"),
            np.asarray([[0.0, 1.0]], dtype="float32"),
        )
        index.save(config.TEXT_INDEX_PATH)
        published = publish_current_generation(conn, None)
        self.old_publication = published.publication_id
        self.old_revision = db.get_active_transcript_revision(conn, self.public_id)
        self.old_chunk_id = old_chunk_id
        self.old_index_bytes = config.TEXT_INDEX_PATH.read_bytes()
        conn.close()

    def test_force_failure_keeps_old_publication_and_rows_active(self):
        new_segment = _segment(2.0, 3.0, "new draft transcript")
        new_chunk = SimpleNamespace(start=2.0, end=3.0, text="new draft chunk")

        def transcribe_draft(conn, video_id, *_args, transcript_revision=None, **_kwargs):
            db.insert_segment(
                conn,
                video_id,
                new_segment,
                transcript_revision=transcript_revision,
            )
            conn.commit()
            yield "  synthetic draft ASR complete"

        conn = db.get_conn()
        conn.execute(
            "UPDATE sources SET private_fingerprint = NULL WHERE public_video_id = ?",
            (self.public_id,),
        )
        conn.commit()
        conn.close()

        with (
            patch.object(index_video, "_transcribe_with_progress", transcribe_draft),
            patch.object(index_video, "build_chunks", return_value=[new_chunk]),
            patch.object(index_video, "TextEmbedder") as embedder,
            patch(
                "moment_retrieval.publication.publish_current_generation",
                side_effect=PublicationError("synthetic CAS failure"),
            ),
        ):
            embedder.return_value.encode.return_value = np.asarray(
                [[1.0, 0.0]], dtype="float32"
            )
            with self.assertRaisesRegex(index_video.IndexError_, "公開に失敗"):
                list(
                    index_video.run_indexing(
                        self.video,
                        video_id=self.public_id,
                        force=True,
                        asr_model="synthetic",
                        device="cpu",
                        compute_type="int8",
                        batch_size=1,
                    )
                )

        conn = db.get_conn()
        try:
            current = conn.execute(
                "SELECT current_publication_id FROM library_state"
            ).fetchone()[0]
            self.assertEqual(current, self.old_publication)
            self.assertEqual(
                db.get_active_transcript_revision(conn, self.public_id),
                self.old_revision,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM text_chunks WHERE chunk_id = ?",
                    (self.old_chunk_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM asr_segments WHERE transcript_revision = ?",
                    (self.old_revision,),
                ).fetchone()[0],
                1,
            )
            self.assertIsNone(
                conn.execute(
                    "SELECT private_fingerprint FROM sources WHERE public_video_id = ?",
                    (self.public_id,),
                ).fetchone()[0]
            )
        finally:
            conn.close()
        self.assertEqual(config.TEXT_INDEX_PATH.read_bytes(), self.old_index_bytes)
        loaded = VectorIndex.load(config.TEXT_INDEX_PATH, 2)
        np.testing.assert_allclose(
            loaded.index.reconstruct(self.old_chunk_id),
            np.asarray([0.0, 1.0], dtype="float32"),
        )

    def test_changed_source_is_rejected_before_creating_a_revision(self):
        conn = db.get_conn()
        try:
            before_revisions = conn.execute(
                "SELECT COUNT(*) FROM transcript_revisions"
            ).fetchone()[0]
            stored_fingerprint = conn.execute(
                "SELECT private_fingerprint FROM sources WHERE public_video_id = ?",
                (self.public_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.video.write_bytes(b"different source bytes")

        with self.assertRaisesRegex(index_video.IndexError_, "内容が登録時と異なります"):
            list(
                index_video.run_indexing(
                    self.video,
                    video_id=self.public_id,
                    force=True,
                )
            )

        conn = db.get_conn()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM transcript_revisions").fetchone()[0],
                before_revisions,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT private_fingerprint FROM sources WHERE public_video_id = ?",
                    (self.public_id,),
                ).fetchone()[0],
                stored_fingerprint,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT current_publication_id FROM library_state"
                ).fetchone()[0],
                self.old_publication,
            )
        finally:
            conn.close()
        self.assertEqual(config.TEXT_INDEX_PATH.read_bytes(), self.old_index_bytes)

    def test_successful_force_generation_contains_only_prospective_chunk_ids(self):
        new_segment = _segment(2.0, 3.0, "new draft transcript")
        new_chunk = SimpleNamespace(start=2.0, end=3.0, text="new draft chunk")

        def transcribe_draft(conn, video_id, *_args, transcript_revision=None, **_kwargs):
            db.insert_segment(
                conn,
                video_id,
                new_segment,
                transcript_revision=transcript_revision,
            )
            conn.commit()
            yield "  synthetic draft ASR complete"

        with (
            patch.object(index_video, "_transcribe_with_progress", transcribe_draft),
            patch.object(index_video, "build_chunks", return_value=[new_chunk]),
            patch.object(index_video, "TextEmbedder") as embedder,
        ):
            embedder.return_value.encode.return_value = np.asarray(
                [[1.0, 0.0]], dtype="float32"
            )
            progress = index_video.run_indexing(
                self.video,
                video_id=self.public_id,
                force=True,
                asr_model="synthetic",
                device="cpu",
                compute_type="int8",
                batch_size=1,
            )
            messages = []
            for message in progress:
                messages.append(message)
                if "検索世代を公開" in message:
                    conn = db.get_conn()
                    try:
                        self.assertEqual(
                            conn.execute(
                                "SELECT COUNT(*) FROM job_records WHERE state = 'running'"
                            ).fetchone()[0],
                            0,
                        )
                    finally:
                        conn.close()

        self.assertTrue(any("完了:" in message for message in messages))
        conn = db.get_conn()
        try:
            active_revision = db.get_active_transcript_revision(
                conn, self.public_id
            )
            expected_ids = db.get_chunk_ids(
                conn,
                self.public_id,
                transcript_revision=active_revision,
            )
            generation_id = conn.execute(
                "SELECT p.generation_id FROM library_state s "
                "JOIN search_publications p "
                "ON p.publication_id = s.current_publication_id"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(len(expected_ids), 1)
        generation = VectorIndex.load(
            config.search_generations_dir() / generation_id / "vectors.faiss", 2
        )
        self.assertEqual(int(generation.index.ntotal), 1)
        np.testing.assert_allclose(
            generation.index.reconstruct(expected_ids[0]),
            np.asarray([1.0, 0.0], dtype="float32"),
        )
        with self.assertRaises(RuntimeError):
            generation.index.reconstruct(self.old_chunk_id)

    def test_source_change_during_draft_build_prevents_publication(self):
        new_segment = _segment(2.0, 3.0, "new draft transcript")
        new_chunk = SimpleNamespace(start=2.0, end=3.0, text="new draft chunk")
        original_builder = index_video._build_verified_index_draft

        def transcribe_draft(conn, video_id, *_args, transcript_revision=None, **_kwargs):
            db.insert_segment(
                conn,
                video_id,
                new_segment,
                transcript_revision=transcript_revision,
            )
            conn.commit()
            yield "  synthetic draft ASR complete"

        def build_then_change_source(*args, **kwargs):
            result = original_builder(*args, **kwargs)
            self.video.write_bytes(b"changed while vector draft was being built")
            return result

        with (
            patch.object(index_video, "_transcribe_with_progress", transcribe_draft),
            patch.object(index_video, "build_chunks", return_value=[new_chunk]),
            patch.object(index_video, "TextEmbedder") as embedder,
            patch.object(
                index_video,
                "_build_verified_index_draft",
                side_effect=build_then_change_source,
            ),
        ):
            embedder.return_value.encode.return_value = np.asarray(
                [[1.0, 0.0]], dtype="float32"
            )
            with self.assertRaisesRegex(index_video.IndexError_, "公開直前"):
                list(
                    index_video.run_indexing(
                        self.video,
                        video_id=self.public_id,
                        force=True,
                        asr_model="synthetic",
                        device="cpu",
                        compute_type="int8",
                        batch_size=1,
                    )
                )

        conn = db.get_conn()
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT current_publication_id FROM library_state"
                ).fetchone()[0],
                self.old_publication,
            )
            self.assertEqual(
                db.get_active_transcript_revision(conn, self.public_id),
                self.old_revision,
            )
        finally:
            conn.close()
        self.assertEqual(config.TEXT_INDEX_PATH.read_bytes(), self.old_index_bytes)

    def test_indexing_rejects_a_concurrent_library_writer(self):
        blocker = db.get_conn()
        try:
            with LeaseManager(blocker).writer():
                with self.assertRaisesRegex(index_video.IndexError_, "別のライブラリ更新"):
                    list(
                        index_video.run_indexing(
                            self.video,
                            video_id=self.public_id,
                            force=True,
                        )
                    )
        finally:
            blocker.close()


if __name__ == "__main__":
    unittest.main()
