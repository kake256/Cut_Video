import io
import json
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import numpy as np

from moment_retrieval import config, db
from moment_retrieval.publication import (
    LeaseManager,
    build_vector_index_draft,
    publish_current_generation,
)
from moment_retrieval.share import (
    PACKAGE_FORMAT,
    PACKAGE_SCHEMA_VERSION,
    ShareError,
    export_index,
    import_index,
    relink_video,
)
from moment_retrieval.vector_index import VectorIndex


@contextmanager
def configured_store(root: Path):
    data_dir = root / "synthetic-data"
    with (
        patch.object(config, "DATA_DIR", data_dir),
        patch.object(config, "DB_PATH", data_dir / "index.db"),
        patch.object(config, "TEXT_INDEX_PATH", data_dir / "text.index"),
        patch.object(config, "EMBED_VECTOR_DIM", 2),
    ):
        yield


def vector_bytes(vectors: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    np.save(buffer, vectors, allow_pickle=False)
    return buffer.getvalue()


class SharePackageTest(unittest.TestCase):
    def _seed_source(self, root: Path) -> tuple[str, str]:
        source_id = ".." + "/path-derived-private-clip"
        source_path = "C:" + "\\Users\\" + "private-account\\recording.mp4"
        conn = db.get_conn()
        db.init_db(conn)
        try:
            db.insert_video(conn, source_id, source_path, 42.0)
            conn.execute(
                "INSERT INTO asr_segments "
                "(video_id, start_sec, end_sec, text, words_json) VALUES (?, ?, ?, ?, ?)",
                (
                    source_id,
                    1.0,
                    2.0,
                    "synthetic alpha transcript",
                    json.dumps(
                        [
                            {
                                "word": "alpha",
                                "start": 1.0,
                                "end": 1.5,
                                "source_locator": "synthetic-metadata-to-remove",
                            }
                        ]
                    ),
                ),
            )
            cursor = conn.execute(
                "INSERT INTO text_chunks (video_id, start_sec, end_sec, text) "
                "VALUES (?, ?, ?, ?)",
                (source_id, 1.0, 3.0, "synthetic alpha chunk"),
            )
            chunk_id = int(cursor.lastrowid)
            conn.commit()
        finally:
            conn.close()

        index = VectorIndex(2)
        index.add(
            np.asarray([chunk_id], dtype="int64"),
            np.asarray([[0.6, 0.8]], dtype="float32"),
        )
        index.save(config.TEXT_INDEX_PATH)
        return source_id, source_path

    def _export_fixture(self, source_root: Path, export_root: Path):
        with configured_store(source_root):
            source_id, source_path = self._seed_source(source_root)
            archive = export_index(
                source_id, export_root, confirm_sensitive=True
            )
        return archive, source_id, source_path

    @staticmethod
    def _redacted_manifest(*, embedding=None, chunks=None, segments=None):
        return {
            "format": PACKAGE_FORMAT,
            "schema_version": PACKAGE_SCHEMA_VERSION,
            "video": {"duration": 5.0},
            "embedding": embedding
            or {
                "model": config.EMBED_MODEL_NAME,
                "dtype": "float32",
                "dimension": 2,
                "normalized": True,
            },
            "segments": [] if segments is None else segments,
            "chunks": (
                [{"start_sec": 0.0, "end_sec": 1.0, "text": "synthetic"}]
                if chunks is None
                else chunks
            ),
        }

    @staticmethod
    def _write_package(path: Path, manifest: dict, vectors_raw: bytes) -> Path:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
            package.writestr("manifest.json", json.dumps(manifest))
            package.writestr("vectors.npy", vectors_raw)
        return path

    def test_export_requires_explicit_sensitive_content_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with configured_store(root):
                source_id, _ = self._seed_source(root)
                with self.assertRaisesRegex(ShareError, "確認"):
                    export_index(source_id, root / "out")

    def test_export_keeps_one_snapshot_when_force_publication_commits_mid_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with configured_store(root):
                source = root / "synthetic-source.mp4"
                source.write_bytes(b"stable synthetic source")
                public_id = db.new_public_video_id()
                old_segment = type(
                    "Segment",
                    (),
                    {
                        "start": 1.0,
                        "end": 2.0,
                        "text": "old transcript",
                        "words": [],
                    },
                )()
                new_segment = type(
                    "Segment",
                    (),
                    {
                        "start": 3.0,
                        "end": 4.0,
                        "text": "new transcript",
                        "words": [],
                    },
                )()

                conn = db.get_conn()
                db.init_db(conn)
                try:
                    db.insert_video(conn, public_id, str(source), 10.0)
                    db.insert_segment(conn, public_id, old_segment)
                    db.mark_asr_complete(conn, public_id)
                    old_chunk_id = db.insert_chunk(conn, public_id, old_segment)
                    conn.commit()
                    old_index = VectorIndex(2)
                    old_index.add(
                        np.asarray([old_chunk_id], dtype="int64"),
                        np.asarray([[1.0, 0.0]], dtype="float32"),
                    )
                    old_index.save(config.TEXT_INDEX_PATH)
                    old_publication = publish_current_generation(conn, None)

                    draft_revision = db.begin_transcript_revision(
                        conn,
                        public_id,
                        asr_config={"model": "synthetic-force"},
                        reuse_draft=False,
                    )
                    db.insert_segment(
                        conn,
                        public_id,
                        new_segment,
                        transcript_revision=draft_revision,
                    )
                    new_chunk_id = db.insert_chunk(
                        conn,
                        public_id,
                        new_segment,
                        transcript_revision=draft_revision,
                    )
                    db.complete_transcript_revision(conn, draft_revision)
                    draft_path, _ = build_vector_index_draft(
                        conn,
                        {public_id: draft_revision},
                        {
                            new_chunk_id: np.asarray(
                                [0.0, 1.0], dtype="float32"
                            )
                        },
                        2,
                    )
                finally:
                    conn.close()

                original_get_active = db.get_active_transcript_revision
                revision_reads = 0
                force_committed = False

                def publish_force_on_second_revision_read(export_conn, identifier):
                    nonlocal revision_reads, force_committed
                    revision_reads += 1
                    if revision_reads == 2:
                        force_committed = True
                        writer = db.get_conn()
                        try:
                            publish_current_generation(
                                writer,
                                old_publication.publication_id,
                                transcript_updates={public_id: draft_revision},
                                vector_draft_path=draft_path,
                            )
                        finally:
                            writer.close()
                    return original_get_active(export_conn, identifier)

                try:
                    with patch.object(
                        db,
                        "get_active_transcript_revision",
                        side_effect=publish_force_on_second_revision_read,
                    ):
                        archive = export_index(
                            public_id,
                            root / "exports",
                            confirm_sensitive=True,
                        )
                finally:
                    draft_path.unlink(missing_ok=True)

                self.assertTrue(force_committed)
                with zipfile.ZipFile(archive) as package:
                    manifest = json.loads(package.read("manifest.json"))
                    vectors = np.load(
                        io.BytesIO(package.read("vectors.npy")),
                        allow_pickle=False,
                    )
                self.assertEqual(
                    [segment["text"] for segment in manifest["segments"]],
                    ["old transcript"],
                )
                self.assertEqual(
                    [chunk["text"] for chunk in manifest["chunks"]],
                    ["old transcript"],
                )
                np.testing.assert_allclose(
                    vectors,
                    np.asarray([[1.0, 0.0]], dtype="float32"),
                )

    def test_export_is_anonymous_and_uses_safe_archive_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_root = root / "out"
            archive, source_id, source_path = self._export_fixture(
                root / "source", export_root
            )

            self.assertEqual(archive.parent.resolve(), export_root.resolve())
            self.assertRegex(archive.name, r"^shared-index-[0-9a-f]{12}\.vindex\.zip$")
            self.assertNotIn(source_id, archive.name)
            with zipfile.ZipFile(archive, "r") as package:
                self.assertEqual(
                    set(package.namelist()), {"manifest.json", "vectors.npy"}
                )
                manifest_raw = package.read("manifest.json")
                manifest = json.loads(manifest_raw)

            self.assertEqual(manifest["format"], PACKAGE_FORMAT)
            self.assertEqual(manifest["schema_version"], PACKAGE_SCHEMA_VERSION)
            self.assertTrue(manifest["embedding"]["normalized"])
            self.assertFalse(manifest["privacy"]["source_path_included"])
            self.assertNotIn("path", manifest["video"])
            self.assertNotIn("video_id", manifest["video"])
            self.assertNotIn("segment_id", manifest["segments"][0])
            self.assertNotIn("chunk_id", manifest["chunks"][0])
            self.assertEqual(
                json.loads(manifest["segments"][0]["words_json"]),
                [{"word": "alpha", "start": 1.0, "end": 1.5}],
            )
            self.assertNotIn(source_path.encode(), manifest_raw)
            self.assertNotIn(source_id.encode(), manifest_raw)
            self.assertNotIn(b"synthetic-metadata-to-remove", manifest_raw)

    def test_import_preserves_canonical_public_id_and_unlinked_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, source_id, source_path = self._export_fixture(
                root / "source", root / "packages"
            )
            with configured_store(root / "destination"):
                messages = list(import_index(archive))
                conn = db.get_conn()
                try:
                    videos = db.list_videos(conn)
                    segments = conn.execute("SELECT * FROM asr_segments").fetchall()
                    chunks = conn.execute("SELECT * FROM text_chunks").fetchall()
                finally:
                    conn.close()

                self.assertEqual(len(videos), 1)
                imported = videos[0]
                self.assertRegex(imported["video_id"], r"^vid_[0-9a-f]{32}$")
                self.assertNotEqual(imported["video_id"], source_id)
                self.assertEqual(
                    imported["path"],
                    f"video/__unlinked__/{imported['video_id']}.mp4",
                )
                self.assertEqual(len(segments), 1)
                self.assertEqual(len(chunks), 1)
                self.assertTrue(config.TEXT_INDEX_PATH.exists())
                joined = "\n".join(messages)
                self.assertNotIn(source_id, joined)
                self.assertNotIn(source_path, joined)
                self.assertIn("再関連付け", joined)

    def test_relink_rejects_duration_mismatch_and_accepts_matching_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "matching.mp4"
            source.write_bytes(b"synthetic source")
            with configured_store(root):
                conn = db.get_conn()
                db.init_db(conn)
                public_id = db.new_public_video_id()
                db.insert_video(conn, public_id, "video/__unlinked__/missing.mp4", 42.0)
                conn.execute(
                    "UPDATE videos SET source_state = 'missing' WHERE public_video_id = ?",
                    (public_id,),
                )
                conn.commit()
                conn.close()
                with patch("moment_retrieval.utils.probe_duration", return_value=41.0):
                    with self.assertRaises(ShareError):
                        relink_video(public_id, source)
                with patch("moment_retrieval.utils.probe_duration", return_value=42.0):
                    linked = relink_video(public_id, source)
                self.assertEqual(linked["source_state"], "available")
                self.assertEqual(Path(linked["path"]), source.resolve())

    def test_relink_preserves_source_and_transcript_revision_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, _, _ = self._export_fixture(
                root / "source", root / "packages"
            )
            local_source = root / "matching.mp4"
            local_source.write_bytes(b"synthetic matching source")
            with configured_store(root / "destination"):
                list(import_index(archive))
                conn = db.get_conn()
                try:
                    video = db.list_videos(conn)[0]
                    public_id = video["public_video_id"]
                    old_generation = video["source_generation"]
                    old_revision = db.get_active_transcript_revision(
                        conn, public_id
                    )
                    old_publication = conn.execute(
                        "SELECT current_publication_id FROM library_state"
                    ).fetchone()[0]
                finally:
                    conn.close()

                with patch(
                    "moment_retrieval.utils.probe_duration", return_value=42.0
                ):
                    linked = relink_video(public_id, local_source)

                self.assertEqual(linked["source_generation"], old_generation)
                conn = db.get_conn()
                try:
                    self.assertEqual(
                        db.get_active_transcript_revision(conn, public_id),
                        old_revision,
                    )
                    self.assertEqual(
                        conn.execute(
                            "SELECT source_generation FROM transcript_revisions "
                            "WHERE transcript_revision = ?",
                            (old_revision,),
                        ).fetchone()[0],
                        old_generation,
                    )
                    source_row = conn.execute(
                        "SELECT locator, status FROM sources "
                        "WHERE source_generation = ?",
                        (old_generation,),
                    ).fetchone()
                    self.assertEqual(Path(source_row[0]), local_source.resolve())
                    self.assertEqual(source_row[1], "available")
                    self.assertEqual(
                        conn.execute(
                            "SELECT current_publication_id FROM library_state"
                        ).fetchone()[0],
                        old_publication,
                    )
                finally:
                    conn.close()

    def test_legacy_import_discards_source_path_and_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "legacy.vindex.zip"
            legacy_id = ".." + "/legacy-private-id"
            legacy_path = "C:" + "\\Users\\" + "legacy-account\\private.mp4"
            manifest = {
                "video": {
                    "video_id": legacy_id,
                    "path": legacy_path,
                    "duration": 10.0,
                },
                "segments": [
                    {
                        "segment_id": 99,
                        "video_id": legacy_id,
                        "start_sec": 0.0,
                        "end_sec": 1.0,
                        "text": "synthetic legacy transcript",
                        "words_json": None,
                    }
                ],
                "chunks": [
                    {
                        "chunk_id": 100,
                        "video_id": legacy_id,
                        "start_sec": 0.0,
                        "end_sec": 2.0,
                        "text": "synthetic legacy chunk",
                    }
                ],
            }
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
                package.writestr("manifest.json", json.dumps(manifest))
                package.writestr(
                    "vectors.npy",
                    vector_bytes(np.asarray([[1.0, 0.0]], dtype="float32")),
                )

            with configured_store(root / "destination"):
                messages = list(import_index(archive))
                conn = db.get_conn()
                try:
                    imported = db.list_videos(conn)[0]
                finally:
                    conn.close()

            self.assertNotEqual(imported["video_id"], legacy_id)
            self.assertNotEqual(imported["path"], legacy_path)
            joined = "\n".join(messages)
            self.assertNotIn(legacy_id, joined)
            self.assertNotIn(legacy_path, joined)
            self.assertIn("旧形式", joined)

    def test_import_rejects_extra_entries_and_invalid_vector_dtype(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = {
                "format": PACKAGE_FORMAT,
                "schema_version": PACKAGE_SCHEMA_VERSION,
                "video": {"duration": 5.0},
                "segments": [],
                "chunks": [
                    {"start_sec": 0.0, "end_sec": 1.0, "text": "synthetic"}
                ],
            }

            extra_archive = root / "extra.vindex.zip"
            with zipfile.ZipFile(extra_archive, "w") as package:
                package.writestr("manifest.json", json.dumps(manifest))
                package.writestr(
                    "vectors.npy",
                    vector_bytes(np.asarray([[1.0]], dtype="float32")),
                )
                package.writestr("unexpected.txt", "synthetic")

            dtype_archive = root / "dtype.vindex.zip"
            with zipfile.ZipFile(dtype_archive, "w") as package:
                package.writestr("manifest.json", json.dumps(manifest))
                package.writestr(
                    "vectors.npy",
                    vector_bytes(np.asarray([[1]], dtype="int64")),
                )

            with configured_store(root / "destination"):
                with self.assertRaisesRegex(ShareError, "許可"):
                    list(import_index(extra_archive))
                with self.assertRaisesRegex(ShareError, "float32"):
                    list(import_index(dtype_archive))

    def test_import_rejects_malformed_word_timestamp_items(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "invalid-words.vindex.zip"
            manifest = {
                "format": PACKAGE_FORMAT,
                "schema_version": PACKAGE_SCHEMA_VERSION,
                "video": {"duration": 5.0},
                "segments": [
                    {
                        "start_sec": 0.0,
                        "end_sec": 1.0,
                        "text": "synthetic",
                        "words_json": "[1]",
                    }
                ],
                "chunks": [],
            }
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("manifest.json", json.dumps(manifest))
                package.writestr(
                    "vectors.npy",
                    vector_bytes(np.zeros((0, 0), dtype="float32")),
                )

            with configured_store(root / "destination"):
                with self.assertRaisesRegex(ShareError, "単語時刻"):
                    list(import_index(archive))

    def test_import_validates_embedding_contract_and_unit_norms(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid_embedding = {
                "model": config.EMBED_MODEL_NAME,
                "dtype": "float32",
                "dimension": 2,
                "normalized": True,
            }
            cases = (
                (
                    "model",
                    {**valid_embedding, "model": "synthetic-incompatible-model"},
                    np.asarray([[1.0, 0.0]], dtype="float32"),
                    "model",
                ),
                (
                    "dimension",
                    {**valid_embedding, "dimension": 3},
                    np.asarray([[1.0, 0.0]], dtype="float32"),
                    "次元",
                ),
                (
                    "normalized-flag",
                    {**valid_embedding, "normalized": False},
                    np.asarray([[1.0, 0.0]], dtype="float32"),
                    "形式",
                ),
                (
                    "large-norm",
                    valid_embedding,
                    np.asarray([[100.0, 0.0]], dtype="float32"),
                    "単位長",
                ),
                (
                    "zero-norm",
                    valid_embedding,
                    np.asarray([[0.0, 0.0]], dtype="float32"),
                    "単位長",
                ),
            )
            for name, embedding, vectors, message in cases:
                with self.subTest(name=name):
                    archive = self._write_package(
                        root / f"{name}.vindex.zip",
                        self._redacted_manifest(embedding=embedding),
                        vector_bytes(vectors),
                    )
                    with configured_store(root / f"destination-{name}"):
                        with self.assertRaisesRegex(ShareError, message):
                            list(import_index(archive))

    def test_import_rejects_oversized_npy_header_before_payload_allocation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            header = io.BytesIO()
            np.lib.format.write_array_header_1_0(
                header,
                {"descr": "<f4", "fortran_order": False, "shape": (1, 1_000_000_000)},
            )
            archive = self._write_package(
                root / "oversized-header.vindex.zip",
                self._redacted_manifest(),
                header.getvalue(),
            )
            with configured_store(root / "destination"):
                with self.assertRaisesRegex(ShareError, "次元"):
                    list(import_index(archive))

    def test_import_dimension_must_match_the_configured_local_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = self._write_package(
                root / "wrong-local-dimension.vindex.zip",
                self._redacted_manifest(),
                vector_bytes(np.asarray([[1.0, 0.0]], dtype="float32")),
            )
            with configured_store(root / "destination"):
                with patch.object(config, "EMBED_VECTOR_DIM", 1024):
                    with self.assertRaisesRegex(ShareError, "ローカルembedding model"):
                        list(import_index(archive))

    def test_partial_schema_cannot_downgrade_to_legacy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._redacted_manifest()
            manifest.pop("schema_version")
            archive = self._write_package(
                root / "partial-schema.vindex.zip",
                manifest,
                vector_bytes(np.asarray([[1.0, 0.0]], dtype="float32")),
            )
            with configured_store(root / "destination"):
                with self.assertRaisesRegex(ShareError, "不完全"):
                    list(import_index(archive))

    def test_export_enforces_the_same_vector_size_limit_as_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with configured_store(root / "source"):
                source_id, _ = self._seed_source(root)
                with patch("moment_retrieval.share._MAX_VECTORS_BYTES", 64):
                    with self.assertRaisesRegex(ShareError, "出力上限"):
                        export_index(
                            source_id,
                            root / "packages",
                            confirm_sensitive=True,
                        )

    def test_closing_progress_generator_rolls_back_unpublished_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, _, _ = self._export_fixture(
                root / "source", root / "packages"
            )
            with configured_store(root / "destination"):
                progress = import_index(archive)
                for _ in range(4):
                    next(progress)
                progress.close()
                conn = db.get_conn()
                try:
                    self.assertEqual(db.list_videos(conn), [])
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM text_chunks").fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        conn.execute(
                            "SELECT COUNT(*) FROM job_records WHERE state = 'running'"
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    conn.close()
                self.assertFalse(config.TEXT_INDEX_PATH.exists())

    def test_import_preserves_existing_faiss_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, _, _ = self._export_fixture(
                root / "source", root / "packages"
            )
            with configured_store(root / "destination"):
                conn = db.get_conn()
                db.init_db(conn)
                try:
                    db.insert_video(conn, "synthetic-existing", "synthetic.mp4", 5.0)
                    segment = type(
                        "Segment", (),
                        {"start": 0.0, "end": 1.0, "text": "synthetic existing", "words": []},
                    )()
                    db.insert_segment(conn, "synthetic-existing", segment)
                    db.mark_asr_complete(conn, "synthetic-existing")
                    existing_chunk_id = db.insert_chunk(
                        conn, "synthetic-existing", segment
                    )
                    conn.commit()
                    existing_index = VectorIndex(2)
                    existing_index.add(
                        np.asarray([existing_chunk_id], dtype="int64"),
                        np.asarray([[0.0, 1.0]], dtype="float32"),
                    )
                    existing_index.save(config.TEXT_INDEX_PATH)
                    publish_current_generation(conn, None)
                    # The compatibility file is deliberately stale.  Import
                    # must preserve the active vector from the immutable
                    # publication generation, not from this mutable file.
                    VectorIndex(2).save(config.TEXT_INDEX_PATH)
                finally:
                    conn.close()

                list(import_index(archive))

                conn = db.get_conn()
                try:
                    chunk_ids = [row[0] for row in conn.execute(
                        "SELECT chunk_id FROM text_chunks ORDER BY chunk_id"
                    ).fetchall()]
                finally:
                    conn.close()
                loaded = VectorIndex.load(config.TEXT_INDEX_PATH, 2)
                self.assertEqual(int(loaded.index.ntotal), 2)
                self.assertEqual(len(chunk_ids), 2)
                np.testing.assert_allclose(
                    loaded.index.reconstruct(existing_chunk_id),
                    np.asarray([0.0, 1.0], dtype="float32"),
                )

    def test_faiss_publish_failure_compensates_database_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, _, _ = self._export_fixture(
                root / "source", root / "packages"
            )
            with configured_store(root / "destination"):
                with patch(
                    "moment_retrieval.publication._install_staged_generation",
                    side_effect=OSError("synthetic publish failure"),
                ):
                    with self.assertRaisesRegex(ShareError, "publication公開"):
                        list(import_index(archive))
                conn = db.get_conn()
                try:
                    self.assertEqual(db.list_videos(conn), [])
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM asr_segments").fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM text_chunks").fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        conn.execute(
                            "SELECT COUNT(*) FROM transcript_revisions"
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    conn.close()

    def test_faiss_publish_failure_uses_fresh_connection_if_primary_compensation_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, _, _ = self._export_fixture(
                root / "source", root / "packages"
            )
            with configured_store(root / "destination"):
                with (
                    patch(
                        "moment_retrieval.publication._install_staged_generation",
                        side_effect=OSError("synthetic publish failure"),
                    ),
                    patch(
                        "moment_retrieval.share.db.delete_video",
                        side_effect=OSError("synthetic primary compensation failure"),
                    ),
                ):
                    with self.assertRaisesRegex(ShareError, "publication公開"):
                        list(import_index(archive))
                conn = db.get_conn()
                try:
                    self.assertEqual(db.list_videos(conn), [])
                finally:
                    conn.close()

    def test_compatibility_index_failure_after_cas_keeps_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, _, _ = self._export_fixture(
                root / "source", root / "packages"
            )
            with configured_store(root / "destination"):
                with patch(
                    "moment_retrieval.share._install_compatibility_index",
                    side_effect=OSError("synthetic compatibility failure"),
                ):
                    messages = list(import_index(archive))
                conn = db.get_conn()
                try:
                    current = conn.execute(
                        "SELECT current_publication_id FROM library_state"
                    ).fetchone()[0]
                    self.assertIsNotNone(current)
                    self.assertEqual(len(db.list_videos(conn)), 1)
                    job_states = [
                        row[0] for row in conn.execute(
                            "SELECT state FROM job_records"
                        ).fetchall()
                    ]
                    self.assertEqual(job_states, ["complete"])
                finally:
                    conn.close()
                self.assertIn("互換インデックス", "\n".join(messages))

    def test_first_post_publish_progress_is_outside_writer_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, _, _ = self._export_fixture(
                root / "source", root / "packages"
            )
            with configured_store(root / "destination"):
                progress = import_index(archive)
                for _ in range(4):
                    next(progress)
                post_publish_message = next(progress)
                self.assertIn("FAISS", post_publish_message)
                conn = db.get_conn()
                try:
                    self.assertEqual(
                        conn.execute(
                            "SELECT COUNT(*) FROM job_records WHERE state = 'running'"
                        ).fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        conn.execute(
                            "SELECT COUNT(*) FROM job_records WHERE state = 'complete'"
                        ).fetchone()[0],
                        1,
                    )
                    self.assertIsNotNone(
                        conn.execute(
                            "SELECT current_publication_id FROM library_state"
                        ).fetchone()[0]
                    )
                finally:
                    conn.close()
                progress.close()

    def test_import_rejects_a_concurrent_library_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, _, _ = self._export_fixture(
                root / "source", root / "packages"
            )
            with configured_store(root / "destination"):
                blocker = db.get_conn()
                db.init_db(blocker)
                try:
                    with LeaseManager(blocker).writer():
                        with self.assertRaisesRegex(ShareError, "別のライブラリ更新"):
                            list(import_index(archive))
                finally:
                    blocker.close()

    def test_relink_rejects_a_concurrent_library_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "synthetic-source.mp4"
            source.write_bytes(b"synthetic")
            with configured_store(root):
                blocker = db.get_conn()
                db.init_db(blocker)
                public_id = db.new_public_video_id()
                db.insert_video(
                    blocker, public_id, "video/__unlinked__/synthetic.mp4", 5.0
                )
                blocker.commit()
                try:
                    with LeaseManager(blocker).writer():
                        with (
                            patch(
                                "moment_retrieval.utils.probe_duration",
                                return_value=5.0,
                            ),
                            self.assertRaisesRegex(ShareError, "別のライブラリ更新"),
                        ):
                            relink_video(public_id, source)
                finally:
                    blocker.close()


if __name__ == "__main__":
    unittest.main()
