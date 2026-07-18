import sqlite3
import tempfile
import unittest
from pathlib import Path

from moment_retrieval import db
from moment_retrieval.publication import PublicationSnapshot, SnapshotMember
from moment_retrieval.search_service import (
    EvidenceSpan,
    SearchHit,
    SearchService,
    SEMANTIC_KIND,
    SEMANTIC_PENDING,
    SemanticPendingError,
    SemanticUnavailableError,
    TextNormalizer,
    resolve_semantic_scope,
    semantic_error_code,
)


class SearchServiceTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn)
        db.insert_video(self.conn, "legacy-a", "X:/movie.mp4", 30.0)
        self.public_id = db.public_video_id(self.conn, "legacy-a")
        self.conn.execute("UPDATE videos SET asr_complete = 1 WHERE video_id = 'legacy-a'")
        self.conn.execute(
            "INSERT INTO asr_segments(video_id,start_sec,end_sec,text,words_json) VALUES(?,?,?,?,?)",
            ("legacy-a", 1.0, 3.0, "それは命令したんじゃね。ワンちゃんです", "[]"),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_text_match_needs_neither_faiss_nor_embedding_model(self):
        service = SearchService(self.conn)
        text, semantic = service.search("命令", public_video_id=self.public_id)
        self.assertEqual(len(text), 1)
        self.assertEqual(semantic, [])
        self.assertEqual(text[0].public_video_id, self.public_id)
        self.assertIsNone(text[0].semantic_score)

    def test_kana_and_cjk_whitespace_variants_match(self):
        service = SearchService(self.conn)
        self.assertEqual(len(service.text_search("ワンチャン")), 1)
        self.conn.execute(
            "INSERT INTO asr_segments(video_id,start_sec,end_sec,text,words_json) VALUES(?,?,?,?,?)",
            ("legacy-a", 4.0, 5.0, "壊 し た", "[]"),
        )
        self.assertEqual(len(service.text_search("壊した")), 1)

    def test_normalizer_projects_match_back_to_source_offsets(self):
        normalized = TextNormalizer().normalize("  ＡＢ 命令  ")
        start = normalized.text.index("命令")
        self.assertEqual("  ＡＢ 命令  "[normalized.source_offsets[start]], "命")

    def test_text_match_can_cross_nearby_segment_boundary(self):
        self.conn.execute("DELETE FROM asr_segments")
        self.conn.executemany(
            "INSERT INTO asr_segments(video_id,start_sec,end_sec,text,words_json) VALUES(?,?,?,?,?)",
            [
                ("legacy-a", 1.0, 2.0, "これは壊", "[]"),
                ("legacy-a", 2.2, 3.0, "したんじゃね", "[]"),
            ],
        )
        hit = SearchService(self.conn).text_search("壊した")[0]
        self.assertEqual((hit.evidence.start_ms, hit.evidence.end_ms), (1000, 3000))
        self.assertEqual((hit.suggested_start_ms, hit.suggested_end_ms), (1000, 3000))

    def test_semantic_results_are_deterministic_and_nms_limited(self):
        def retrieve(*_args):
            return [
                SearchHit("b", self.public_id, SEMANTIC_KIND, EvidenceSpan(1000, 3000), 1000, 3000, "b", semantic_score=.9),
                SearchHit("a", self.public_id, SEMANTIC_KIND, EvidenceSpan(1500, 2500), 1500, 2500, "a", semantic_score=.9),
                SearchHit("c", self.public_id, SEMANTIC_KIND, EvidenceSpan(5000, 6000), 5000, 6000, "c", semantic_score=.8),
            ]
        _, hits = SearchService(self.conn, retrieve).search("unmatched", semantic_limit=5)
        self.assertEqual([hit.hit_id for hit in hits], ["b", "c"])

    def test_semantic_failure_does_not_hide_text_matches(self):
        def unavailable(*_args):
            raise RuntimeError("model unavailable")
        service = SearchService(self.conn, unavailable)
        text, semantic = service.search("命令")
        self.assertEqual(len(text), 1)
        self.assertEqual(semantic, [])
        self.assertIsInstance(service.semantic_error, RuntimeError)

    def test_empty_semantic_coverage_is_reported_as_pending(self):
        snapshot = PublicationSnapshot(
            "publication-1",
            "legacy_current",
            None,
            (SnapshotMember(self.public_id, "source-1", "revision-1", False),),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            legacy_index = Path(temporary_directory) / "text.index"
            legacy_index.touch()
            with self.assertRaises(SemanticPendingError) as raised:
                resolve_semantic_scope(
                    snapshot,
                    self.public_id,
                    legacy_index_path=legacy_index,
                    generations_dir=Path(temporary_directory) / "generations",
                )

        self.assertEqual(semantic_error_code(raised.exception), SEMANTIC_PENDING)

    def test_semantic_scope_keeps_the_exact_covered_revision_set(self):
        snapshot = PublicationSnapshot(
            "publication-1",
            "legacy_current",
            None,
            (SnapshotMember(self.public_id, "source-1", "revision-1", True),),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            legacy_index = Path(temporary_directory) / "text.index"
            legacy_index.touch()
            scope = resolve_semantic_scope(
                snapshot,
                self.public_id,
                legacy_index_path=legacy_index,
                generations_dir=Path(temporary_directory) / "generations",
            )

        self.assertEqual(scope.allowed_revisions, frozenset({"revision-1"}))

    def test_unknown_publication_member_is_semantic_unavailable(self):
        snapshot = PublicationSnapshot("publication-1", "legacy_current", None, ())
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(SemanticUnavailableError):
                resolve_semantic_scope(
                    snapshot,
                    "missing-video",
                    legacy_index_path=Path(temporary_directory) / "text.index",
                    generations_dir=Path(temporary_directory) / "generations",
                )


if __name__ == "__main__":
    unittest.main()
