import sqlite3
import unittest

import numpy as np

from moment_retrieval.search import (
    MATCH_SEMANTIC,
    MATCH_TEXT,
    normalize_kana_search_text,
    normalize_search_text,
    search_chunks,
    search_semantic_chunks,
)
from moment_retrieval.vector_index import VectorIndex


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE text_chunks ("
            "chunk_id INTEGER PRIMARY KEY, video_id TEXT, start_sec REAL, "
            "end_sec REAL, text TEXT, transcript_revision TEXT)"
        )
        self.index = VectorIndex(2)

    def tearDown(self):
        self.conn.close()

    def add_chunk(self, chunk_id, video_id, text, vector, revision="revision-1"):
        self.conn.execute(
            "INSERT INTO text_chunks VALUES (?, ?, ?, ?, ?, ?)",
            (chunk_id, video_id, float(chunk_id), float(chunk_id + 1), text, revision),
        )
        self.index.add(
            np.asarray([chunk_id], dtype="int64"),
            np.asarray([vector], dtype="float32"),
        )

    def test_normalize_search_text(self):
        self.assertEqual(normalize_search_text("  ＭＥＩＲＥＩ\n Test  "), "meirei test")

    def test_normalize_kana_search_text(self):
        self.assertEqual(
            normalize_kana_search_text("ワンチャン"),
            normalize_kana_search_text("ワンちゃん"),
        )

    def test_direct_text_match_precedes_kana_variant(self):
        self.add_chunk(1, "video-a", "犬のワンちゃん", [0.40, 0.90])
        self.add_chunk(2, "video-a", "ワンチャンある", [0.90, 0.10])

        results = search_chunks(
            self.conn,
            self.index,
            "ワンちゃん",
            np.asarray([[1.0, 0.0]], dtype="float32"),
            top_k=2,
            min_score=0.95,
            video_id="video-a",
        )

        self.assertEqual([result["chunk_id"] for result in results], [1, 2])
        self.assertTrue(all(result["match_type"] == MATCH_TEXT for result in results))

    def test_kana_variant_only_fills_remaining_slots(self):
        self.add_chunk(1, "video-a", "犬のワンちゃん", [0.40, 0.90])
        self.add_chunk(2, "video-a", "ワンチャンある", [0.90, 0.10])

        results = search_chunks(
            self.conn,
            self.index,
            "ワンちゃん",
            np.asarray([[1.0, 0.0]], dtype="float32"),
            top_k=1,
            min_score=0.95,
            video_id="video-a",
        )

        self.assertEqual([result["chunk_id"] for result in results], [1])

    def test_text_match_bypasses_semantic_threshold(self):
        self.add_chunk(1, "video-a", "こいつが命令したっていう説ある？", [0.50, 0.866])
        self.add_chunk(2, "video-a", "別の関連する場面", [0.80, 0.60])

        results = search_chunks(
            self.conn,
            self.index,
            "命令",
            np.asarray([[1.0, 0.0]], dtype="float32"),
            top_k=2,
            min_score=0.55,
            video_id="video-a",
        )

        self.assertEqual(results[0]["chunk_id"], 1)
        self.assertEqual(results[0]["match_type"], MATCH_TEXT)
        self.assertLess(results[0]["score"], 0.55)
        self.assertEqual(results[1]["match_type"], MATCH_SEMANTIC)

    def test_semantic_ranking_is_limited_to_selected_video(self):
        self.add_chunk(1, "video-a", "対象動画の候補", [0.60, 0.80])
        for chunk_id in range(10, 25):
            self.add_chunk(chunk_id, "video-b", f"別動画 {chunk_id}", [0.99, 0.01])

        results = search_chunks(
            self.conn,
            self.index,
            "関連する話",
            np.asarray([[1.0, 0.0]], dtype="float32"),
            top_k=1,
            min_score=0.55,
            video_id="video-a",
        )

        self.assertEqual([result["chunk_id"] for result in results], [1])
        self.assertEqual(results[0]["match_type"], MATCH_SEMANTIC)

    def test_pure_semantic_quota_is_not_consumed_by_text_matches(self):
        self.add_chunk(1, "video-a", "needle appears here", [0.0, 1.0])
        self.add_chunk(2, "video-a", "semantic winner", [1.0, 0.0])

        results = search_semantic_chunks(
            self.conn,
            self.index,
            "needle",
            np.asarray([[1.0, 0.0]], dtype="float32"),
            top_k=1,
            min_score=0.55,
            video_id="video-a",
        )

        self.assertEqual([result["chunk_id"] for result in results], [2])
        self.assertEqual(results[0]["match_type"], MATCH_SEMANTIC)

    def test_pure_semantic_ranking_excludes_unpublished_revisions(self):
        self.add_chunk(1, "video-a", "stale winner", [1.0, 0.0], "revision-old")
        self.add_chunk(2, "video-a", "published candidate", [0.8, 0.2], "revision-new")

        results = search_semantic_chunks(
            self.conn,
            self.index,
            "semantic query",
            np.asarray([[1.0, 0.0]], dtype="float32"),
            top_k=1,
            min_score=0.55,
            allowed_revisions=frozenset({"revision-new"}),
        )

        self.assertEqual([result["chunk_id"] for result in results], [2])

    def test_text_and_semantic_duplicate_is_returned_once(self):
        self.add_chunk(1, "video-a", "命令について話す", [0.90, 0.10])

        results = search_chunks(
            self.conn,
            self.index,
            "命令",
            np.asarray([[1.0, 0.0]], dtype="float32"),
            top_k=5,
            min_score=0.55,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["match_type"], MATCH_TEXT)

    def test_range_filters_chunks_outside_window(self):
        self.add_chunk(1, "video-a", "範囲内の候補", [0.90, 0.10])
        self.add_chunk(2, "video-a", "範囲外の候補", [0.90, 0.10])

        results = search_chunks(
            self.conn,
            self.index,
            "候補",
            np.asarray([[1.0, 0.0]], dtype="float32"),
            top_k=5,
            min_score=0.95,
            video_id="video-a",
            start_sec=0.0,
            end_sec=1.5,
        )

        self.assertEqual([result["chunk_id"] for result in results], [1])

    def test_range_ignored_without_video_id(self):
        self.add_chunk(1, "video-a", "候補テキスト", [0.90, 0.10])

        results = search_chunks(
            self.conn,
            self.index,
            "候補",
            np.asarray([[1.0, 0.0]], dtype="float32"),
            top_k=5,
            min_score=0.95,
            start_sec=100.0,
            end_sec=200.0,
        )

        self.assertEqual([result["chunk_id"] for result in results], [1])


if __name__ == "__main__":
    unittest.main()
