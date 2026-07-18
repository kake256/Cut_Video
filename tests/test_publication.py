import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from moment_retrieval import config, db
from moment_retrieval.publication import (
    LeaseManager, PublicationError, cleanup_orphan_generations,
    publish_current_generation, release_snapshot, resolve_snapshot,
)


class PublicationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [
            patch.object(config, "DB_PATH", self.root / "library.db"),
            patch.object(config, "TEXT_INDEX_PATH", self.root / "text.index"),
            patch.object(config, "SEARCH_GENERATIONS_DIR", self.root / "generations"),
            patch.object(config, "EMBED_VECTOR_DIM", 2),
        ]
        for item in self.patches:
            item.start()
        self.conn = db.get_conn()
        db.init_db(self.conn)
        source = self.root / "source.mp4"
        source.write_bytes(b"synthetic")
        db.insert_video(self.conn, "vid_" + "1" * 32, str(source), 10.0)
        segment = SimpleNamespace(start=1.0, end=2.0, text="alpha", words=[])
        db.insert_segment(self.conn, "vid_" + "1" * 32, segment)
        db.mark_asr_complete(self.conn, "vid_" + "1" * 32)
        db.insert_chunk(self.conn, "vid_" + "1" * 32, segment)
        self.conn.commit()
        config.TEXT_INDEX_PATH.write_bytes(b"synthetic-faiss")

    def tearDown(self):
        self.conn.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_publish_is_immutable_and_snapshot_is_leased(self):
        expected = self.conn.execute("SELECT current_publication_id FROM library_state").fetchone()[0]
        published = publish_current_generation(self.conn, expected)
        self.assertTrue((config.search_generations_dir() / published.generation_id / "manifest.json").exists())
        snapshot = resolve_snapshot(self.conn)
        self.assertEqual(snapshot.publication_id, published.publication_id)
        self.assertEqual(len(snapshot.members), 1)
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM publication_leases WHERE lease_id = ?", (snapshot.lease_id,)
        ).fetchone())
        release_snapshot(self.conn, snapshot)

    def test_compare_and_swap_rejects_stale_publication(self):
        with self.assertRaises(PublicationError):
            publish_current_generation(self.conn, "pub_stale")

    def test_stale_writer_and_orphan_cleanup(self):
        now = time.time()
        self.conn.execute(
            "INSERT INTO job_records VALUES('old','search_publish','token',1,'running',?,?,NULL)",
            (now - 20, now - 10),
        )
        self.conn.commit()
        self.assertEqual(LeaseManager(self.conn).cleanup_stale(now), 1)
        orphan = config.search_generations_dir() / "gen_orphan"
        orphan.mkdir(parents=True)
        old = now - 7200
        import os
        os.utime(orphan, (old, old))
        self.assertEqual(cleanup_orphan_generations(self.conn, grace_sec=1), [orphan])


if __name__ == "__main__":
    unittest.main()
