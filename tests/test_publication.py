import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from moment_retrieval import config, db
from moment_retrieval import publication as publication_module
from moment_retrieval.publication import (
    LeaseManager, PublicationError, build_vector_index_draft, cleanup_orphan_generations,
    publish_current_generation, release_snapshot, resolve_snapshot,
)
from moment_retrieval.search_service import SearchService
from moment_retrieval.vector_index import VectorIndex


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
        self.chunk_id = db.insert_chunk(
            self.conn, "vid_" + "1" * 32, segment
        )
        self.conn.commit()
        index = VectorIndex(2)
        index.add(
            np.asarray([self.chunk_id], dtype="int64"),
            np.asarray([[1.0, 0.0]], dtype="float32"),
        )
        index.save(config.TEXT_INDEX_PATH)

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

    def test_staged_search_releases_real_publication_reader_leases(self):
        expected = self.conn.execute(
            "SELECT current_publication_id FROM library_state"
        ).fetchone()[0]
        published = publish_current_generation(self.conn, expected)
        service = SearchService(self.conn)

        _hits, publication_id = service.search_text_stage("alpha")
        self.assertEqual(publication_id, published.publication_id)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM publication_leases").fetchone()[0],
            0,
        )

        service.search_semantic_stage(
            "alpha", expected_publication_id=publication_id, limit=0
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM publication_leases").fetchone()[0],
            0,
        )

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

    def test_writer_lease_conflicts_across_connections_and_heartbeats(self):
        second = db.get_conn()
        try:
            manager = LeaseManager(self.conn, timeout_sec=10, heartbeat_sec=3)
            with manager.writer() as lease:
                self.conn.execute(
                    "UPDATE job_records SET expires_at = ? WHERE job_id = ?",
                    (time.time() + 1, lease.job_id),
                )
                self.conn.commit()
                lease.heartbeat()
                renewed = self.conn.execute(
                    "SELECT expires_at FROM job_records WHERE job_id = ?",
                    (lease.job_id,),
                ).fetchone()[0]
                self.assertGreater(renewed, time.time() + 5)
                with self.assertRaisesRegex(PublicationError, "another publication writer"):
                    with LeaseManager(second).writer():
                        pass
        finally:
            second.close()

    def test_writer_lease_rejects_an_owner_token_change(self):
        with self.assertRaisesRegex(PublicationError, "lease was lost"):
            with LeaseManager(self.conn).writer() as lease:
                self.conn.execute(
                    "UPDATE job_records SET owner_token = 'other-owner' WHERE job_id = ?",
                    (lease.job_id,),
                )
                self.conn.commit()
                lease.assert_owned()

    def test_writer_lease_background_heartbeat_covers_long_work(self):
        with LeaseManager(
            self.conn, timeout_sec=1.0, heartbeat_sec=0.1
        ).writer() as lease:
            time.sleep(1.2)
            lease.assert_owned()
            expires_at = self.conn.execute(
                "SELECT expires_at FROM job_records WHERE job_id = ?",
                (lease.job_id,),
            ).fetchone()[0]
            self.assertGreater(expires_at, time.time())

    def test_draft_revision_activates_only_with_generation_cas(self):
        public_id = "vid_" + "1" * 32
        previous_revision = db.get_active_transcript_revision(self.conn, public_id)
        draft = db.begin_transcript_revision(
            self.conn,
            public_id,
            asr_config={"model": "synthetic-draft"},
            reuse_draft=False,
        )
        segment = SimpleNamespace(start=3.0, end=4.0, text="draft", words=[])
        db.insert_segment(
            self.conn, public_id, segment, transcript_revision=draft
        )
        draft_chunk_id = db.insert_chunk(
            self.conn, public_id, segment, transcript_revision=draft
        )
        db.complete_transcript_revision(self.conn, draft)
        expected = self.conn.execute(
            "SELECT current_publication_id FROM library_state"
        ).fetchone()[0]

        self.assertEqual(
            db.get_active_transcript_revision(self.conn, public_id),
            previous_revision,
        )
        draft_path, expected_ids = build_vector_index_draft(
            self.conn,
            {public_id: draft},
            {draft_chunk_id: np.asarray([0.0, 1.0], dtype="float32")},
            2,
        )
        self.assertEqual(expected_ids, (draft_chunk_id,))
        try:
            published = publish_current_generation(
                self.conn,
                expected,
                transcript_updates={public_id: draft},
                vector_draft_path=draft_path,
            )
        finally:
            draft_path.unlink(missing_ok=True)

        self.assertEqual(db.get_active_transcript_revision(self.conn, public_id), draft)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM text_chunks WHERE transcript_revision = ?",
                (previous_revision,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(published.members[0].transcript_revision, draft)
        self.assertEqual(
            [row["text"] for row in db.get_segments(self.conn, public_id)],
            ["draft"],
        )

    def test_database_connection_policy_is_enabled(self):
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute("PRAGMA busy_timeout").fetchone()[0],
            config.SQLITE_BUSY_TIMEOUT_MS,
        )
        self.assertEqual(
            str(self.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
            "wal",
        )

    def test_current_schema_init_is_read_only_fast_path(self):
        traced: list[str] = []
        before = self.conn.total_changes
        self.conn.set_trace_callback(traced.append)
        try:
            self.assertIsNone(db.init_db(self.conn))
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual(self.conn.total_changes, before)
        statements = "\n".join(traced).upper()
        self.assertNotIn("BEGIN IMMEDIATE", statements)
        self.assertNotIn("UPDATE VIDEOS", statements)
        self.assertNotIn("INSERT INTO", statements)

    def test_writer_entry_never_commits_caller_transaction(self):
        original = self.conn.execute(
            "SELECT display_name FROM videos LIMIT 1"
        ).fetchone()[0]
        self.conn.execute("UPDATE videos SET display_name = 'uncommitted'")
        with self.assertRaisesRegex(PublicationError, "clean connection"):
            with LeaseManager(self.conn).writer():
                pass
        second = db.get_conn()
        try:
            visible = second.execute(
                "SELECT display_name FROM videos LIMIT 1"
            ).fetchone()[0]
            self.assertEqual(visible, original)
        finally:
            second.close()
            self.conn.rollback()

    def test_writer_exit_rolls_back_an_open_caller_transaction(self):
        original = self.conn.execute(
            "SELECT display_name FROM videos LIMIT 1"
        ).fetchone()[0]
        with self.assertRaisesRegex(PublicationError, "open caller transaction"):
            with LeaseManager(self.conn).writer():
                self.conn.execute("UPDATE videos SET display_name = 'uncommitted'")
        second = db.get_conn()
        try:
            visible = second.execute(
                "SELECT display_name FROM videos LIMIT 1"
            ).fetchone()[0]
            self.assertEqual(visible, original)
        finally:
            second.close()

    def test_member_without_chunks_is_not_semantically_covered(self):
        self.conn.execute("DELETE FROM text_chunks")
        self.conn.commit()
        empty = VectorIndex(2)
        empty.save(config.TEXT_INDEX_PATH)
        expected = self.conn.execute(
            "SELECT current_publication_id FROM library_state"
        ).fetchone()[0]
        published = publish_current_generation(self.conn, expected)
        self.assertEqual(len(published.members), 1)
        self.assertFalse(published.members[0].semantic_covered)

    def test_publication_never_overwrites_a_different_source_fingerprint(self):
        source_row = self.conn.execute(
            "SELECT source_generation, private_fingerprint FROM sources LIMIT 1"
        ).fetchone()
        expected = self.conn.execute(
            "SELECT current_publication_id FROM library_state"
        ).fetchone()[0]
        with self.assertRaisesRegex(PublicationError, "fingerprint changed"):
            publish_current_generation(
                self.conn,
                expected,
                source_fingerprints={str(source_row[0]): "different-fingerprint"},
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT private_fingerprint FROM sources WHERE source_generation = ?",
                (source_row[0],),
            ).fetchone()[0],
            source_row[1],
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT current_publication_id FROM library_state"
            ).fetchone()[0],
            expected,
        )

    def test_publication_revalidates_writer_inside_cas_transaction(self):
        expected = self.conn.execute(
            "SELECT current_publication_id FROM library_state"
        ).fetchone()[0]
        original_install = publication_module._install_staged_generation

        with self.assertRaisesRegex(PublicationError, "lease was lost"):
            with LeaseManager(self.conn).writer() as lease:
                def install_then_steal(staging, final):
                    original_install(staging, final)
                    second = db.get_conn()
                    try:
                        second.execute(
                            "UPDATE job_records SET owner_token = 'stolen' "
                            "WHERE job_id = ?",
                            (lease.job_id,),
                        )
                        second.commit()
                    finally:
                        second.close()

                with patch.object(
                    publication_module,
                    "_install_staged_generation",
                    side_effect=install_then_steal,
                ):
                    with self.assertRaisesRegex(PublicationError, "lease was lost"):
                        publish_current_generation(
                            self.conn,
                            expected,
                            writer_lease=lease,
                        )

        self.assertEqual(
            self.conn.execute(
                "SELECT current_publication_id FROM library_state"
            ).fetchone()[0],
            expected,
        )


if __name__ == "__main__":
    unittest.main()
