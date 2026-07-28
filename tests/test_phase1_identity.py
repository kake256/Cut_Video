import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moment_retrieval import db


class Phase1IdentityTest(unittest.TestCase):
    def test_legacy_schema_migrates_without_rewriting_content_and_creates_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE videos(video_id TEXT PRIMARY KEY, path TEXT NOT NULL, duration REAL)")
            conn.execute("CREATE TABLE asr_segments(segment_id INTEGER PRIMARY KEY, video_id TEXT, start_sec REAL, end_sec REAL, text TEXT, words_json TEXT)")
            conn.execute("CREATE TABLE text_chunks(chunk_id INTEGER PRIMARY KEY, video_id TEXT, start_sec REAL, end_sec REAL, text TEXT)")
            conn.execute("INSERT INTO videos VALUES ('legacy-path-derived', 'X:/private/name.mp4', 60.0)")
            conn.execute("INSERT INTO asr_segments VALUES (1, 'legacy-path-derived', 1.0, 2.0, '命令した', '[]')")
            conn.commit()
            conn.row_factory = sqlite3.Row

            backup = db.init_db(conn)
            migrated = db.get_video(conn, "legacy-path-derived")
            public_id = migrated["public_video_id"]
            self.assertTrue(public_id.startswith("vid_"))
            self.assertNotIn("private", public_id)
            self.assertEqual(db.get_video(conn, public_id)["video_id"], "legacy-path-derived")
            self.assertEqual(db.get_segments(conn, public_id)[0]["text"], "命令した")
            self.assertTrue(backup and backup.exists())
            self.assertNotIn("video_id", db.get_public_video(conn, public_id))
            self.assertNotIn("legacy", repr(db.get_public_video(conn, public_id)))
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0],
                db.SCHEMA_VERSION,
            )
            indexes = {
                row[1] for row in conn.execute("PRAGMA index_list(asr_segments)")
            }
            self.assertIn("idx_segments_revision_range", indexes)
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT segment_id FROM asr_segments "
                "WHERE video_id = ? AND transcript_revision = ? "
                "AND end_sec > ? AND start_sec < ? ORDER BY start_sec, segment_id",
                (
                    "legacy-path-derived",
                    db.get_active_transcript_revision(conn, public_id),
                    0.0,
                    60.0,
                ),
            ).fetchall()
            self.assertIn(
                "idx_segments_revision_range",
                " ".join(str(row[3]) for row in plan),
            )
            conn.close()

    def test_public_id_is_stable_when_source_path_changes(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        db.insert_video(conn, "old-alias", "D:/old/movie.mp4", 10.0)
        public_id = db.public_video_id(conn, "old-alias")
        conn.execute("UPDATE videos SET path = ? WHERE video_id = ?", ("F:/new/movie.mp4", "old-alias"))
        self.assertEqual(db.public_video_id(conn, "old-alias"), public_id)
        conn.close()

    def test_failed_migration_rolls_back_schema(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE videos(video_id TEXT PRIMARY KEY, path TEXT NOT NULL, duration REAL)")
        conn.commit()
        with patch("moment_retrieval.db._migrate_legacy_schema", side_effect=RuntimeError("synthetic")):
            with self.assertRaises(RuntimeError):
                db.init_db(conn, create_backup=False)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
        self.assertNotIn("public_video_id", columns)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
        conn.close()

    def test_source_generation_keeps_private_expected_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic.mp4"
            source.write_bytes(b"synthetic-v1")
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            db.init_db(conn)
            db.insert_video(conn, "synthetic", str(source), 10.0)
            video = db.get_video(conn, "synthetic")
            fingerprint = db.resolve_source_fingerprint(
                conn, video["public_video_id"], video["source_generation"],
            )
            self.assertTrue(fingerprint)
            source.write_bytes(b"synthetic-v2")
            self.assertEqual(
                db.resolve_source_fingerprint(
                    conn, video["public_video_id"], video["source_generation"],
                ),
                fingerprint,
            )
            conn.close()

    def test_null_legacy_fingerprint_is_backfilled_only_from_recorded_locator(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic.mp4"
            source.write_bytes(b"legacy-source")
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            db.init_db(conn)
            db.insert_video(conn, "legacy", str(source), 10.0)
            video = db.get_video(conn, "legacy")
            conn.execute(
                "UPDATE sources SET private_fingerprint = NULL WHERE source_generation = ?",
                (video["source_generation"],),
            )
            migrated = db.resolve_source_fingerprint(
                conn, video["public_video_id"], video["source_generation"],
            )
            stored = conn.execute(
                "SELECT private_fingerprint FROM sources WHERE source_generation = ?",
                (video["source_generation"],),
            ).fetchone()[0]
            self.assertTrue(migrated)
            self.assertEqual(stored, migrated)
            conn.close()

    def test_missing_legacy_locator_keeps_source_identity_unknown(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        db.insert_video(conn, "missing", "X:/missing/synthetic.mp4", 10.0)
        video = db.get_video(conn, "missing")
        self.assertIsNone(db.resolve_source_fingerprint(
            conn, video["public_video_id"], video["source_generation"],
        ))
        conn.close()


if __name__ == "__main__":
    unittest.main()
