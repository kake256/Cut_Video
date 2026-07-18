from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import threading
import tempfile
import time
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Mapping

import numpy as np

from . import config, db
from .vector_index import VectorIndex


class PublicationError(RuntimeError):
    pass


class NoActivePublicationError(PublicationError):
    """The library has not published its first immutable search snapshot yet."""

    pass


def private_source_fingerprint(path: Path, sample_size: int = 64 * 1024) -> str:
    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
    with path.open("rb") as source:
        digest.update(source.read(sample_size))
        if stat.st_size > sample_size:
            source.seek(max(0, stat.st_size - sample_size))
            digest.update(source.read(sample_size))
    return digest.hexdigest()


@dataclass(frozen=True)
class SnapshotMember:
    public_video_id: str
    source_generation: str
    transcript_revision: str
    semantic_covered: bool


@dataclass(frozen=True)
class PublicationSnapshot:
    publication_id: str
    generation_id: str | None
    manifest_checksum: str | None
    members: tuple[SnapshotMember, ...]
    lease_id: str | None = None


@dataclass
class WriterLease:
    manager: "LeaseManager"
    job_id: str
    kind: str
    owner_token: str
    publication_committed: bool = False
    _lost: threading.Event = field(default_factory=threading.Event, repr=False)

    def heartbeat(self) -> None:
        if self.manager.conn.in_transaction:
            raise PublicationError(
                "manual heartbeat cannot run inside a caller transaction"
            )
        self.manager._heartbeat_on(self.manager.conn, self)

    def assert_owned(self) -> None:
        if self._lost.is_set():
            raise PublicationError("writer lease was lost")
        row = self.manager.conn.execute(
            "SELECT owner_token, state, expires_at FROM job_records WHERE job_id = ?",
            (self.job_id,),
        ).fetchone()
        if (
            not row
            or str(row[0]) != self.owner_token
            or str(row[1]) != "running"
            or float(row[2]) < time.time()
        ):
            self._lost.set()
            raise PublicationError("writer lease was lost")


class LeaseManager:
    def __init__(
        self,
        conn: sqlite3.Connection,
        timeout_sec: float | None = None,
        heartbeat_sec: float | None = None,
    ):
        self.conn = conn
        self.timeout_sec = max(
            1.0,
            float(
                config.WRITER_LEASE_TIMEOUT_SEC
                if timeout_sec is None else timeout_sec
            ),
        )
        configured_heartbeat = float(
            config.WRITER_HEARTBEAT_SEC
            if heartbeat_sec is None else heartbeat_sec
        )
        self.heartbeat_sec = max(
            0.1, min(configured_heartbeat, self.timeout_sec / 3)
        )
        self.boot_token = secrets.token_hex(16)

    @staticmethod
    def _database_path(conn: sqlite3.Connection) -> Path | None:
        for _, name, filename in conn.execute("PRAGMA database_list"):
            if name == "main" and filename:
                return Path(str(filename))
        return None

    def cleanup_stale(self, now: float | None = None) -> int:
        if self.conn.in_transaction:
            raise PublicationError("lease cleanup requires a clean connection")
        now = time.time() if now is None else now
        cursor = self.conn.execute(
            "DELETE FROM job_records WHERE expires_at < ?", (now,)
        )
        self.conn.execute("DELETE FROM publication_leases WHERE expires_at < ?", (now,))
        self.conn.commit()
        return int(cursor.rowcount)

    def _heartbeat_on(
        self, conn: sqlite3.Connection, lease: WriterLease
    ) -> None:
        now = time.time()
        cursor = conn.execute(
            "UPDATE job_records SET heartbeat = ?, expires_at = ? "
            "WHERE job_id = ? AND owner_token = ? AND state = 'running' "
            "AND expires_at >= ?",
            (
                now,
                now + self.timeout_sec,
                lease.job_id,
                lease.owner_token,
                now,
            ),
        )
        conn.commit()
        if cursor.rowcount != 1:
            lease._lost.set()
            raise PublicationError("writer lease was lost")

    def _heartbeat_loop(
        self,
        lease: WriterLease,
        stop_event: threading.Event,
        database_path: Path,
    ) -> None:
        heartbeat_conn = sqlite3.connect(
            database_path,
            timeout=max(0.0, float(config.SQLITE_BUSY_TIMEOUT_MS) / 1000.0),
        )
        try:
            heartbeat_conn.execute(
                f"PRAGMA busy_timeout = {max(0, int(config.SQLITE_BUSY_TIMEOUT_MS))}"
            )
            heartbeat_conn.execute("PRAGMA foreign_keys = ON")
            while not stop_event.wait(self.heartbeat_sec):
                try:
                    self._heartbeat_on(heartbeat_conn, lease)
                except PublicationError:
                    return
                except sqlite3.Error:
                    # A transient writer lock is retried before the generous
                    # lease timeout; ownership is checked again before publish.
                    continue
        finally:
            heartbeat_conn.close()

    @contextmanager
    def writer(self, kind: str = "library_write") -> Iterator[WriterLease]:
        if self.conn.in_transaction:
            raise PublicationError(
                "writer lease acquisition requires a clean connection"
            )
        now = time.time()
        job_id = f"job_{uuid.uuid4().hex}"
        owner = f"{os.getpid()}:{self.boot_token}"
        lease = WriterLease(self, job_id, kind, owner)
        stop_event = threading.Event()
        heartbeat_thread: threading.Thread | None = None
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            db.ensure_writer_lease_schema(self.conn)
            self.conn.execute("DELETE FROM job_records WHERE expires_at < ?", (now,))
            has_reader_leases = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'publication_leases'"
            ).fetchone()
            if has_reader_leases:
                self.conn.execute(
                    "DELETE FROM publication_leases WHERE expires_at < ?", (now,)
                )
            active = self.conn.execute(
                "SELECT job_id FROM job_records WHERE state = 'running' "
                "AND expires_at >= ? LIMIT 1", (now,)
            ).fetchone()
            if active:
                self.conn.rollback()
                raise PublicationError("another publication writer is active")
            self.conn.execute(
                "INSERT INTO job_records(job_id, kind, owner_token, owner_pid, state, heartbeat, expires_at) "
                "VALUES(?, ?, ?, ?, 'running', ?, ?)",
                (job_id, kind, owner, os.getpid(), now, now + self.timeout_sec),
            )
            self.conn.commit()
            database_path = self._database_path(self.conn)
            if database_path is not None:
                heartbeat_thread = threading.Thread(
                    target=self._heartbeat_loop,
                    args=(lease, stop_event, database_path),
                    name=f"cut-video-writer-heartbeat-{job_id[-8:]}",
                    daemon=True,
                )
                heartbeat_thread.start()
            yield lease
            stop_event.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=max(1.0, self.heartbeat_sec * 2))
            if self.conn.in_transaction:
                self.conn.rollback()
                raise PublicationError(
                    "writer scope ended with an open caller transaction"
                )
            if lease.publication_committed:
                completed_at = time.time()
                try:
                    self.conn.execute(
                        "UPDATE job_records SET state = 'complete', heartbeat = ?, expires_at = ? "
                        "WHERE job_id = ? AND owner_token = ?",
                        (
                            completed_at,
                            completed_at + self.timeout_sec,
                            job_id,
                            owner,
                        ),
                    )
                    self.conn.commit()
                except sqlite3.Error:
                    # The publication is already durable and cannot be rolled
                    # back.  Job bookkeeping is best-effort from this point.
                    self.conn.rollback()
                return
            lease.assert_owned()
            now = time.time()
            cursor = self.conn.execute(
                "UPDATE job_records SET state = 'complete', heartbeat = ?, expires_at = ? "
                "WHERE job_id = ? AND owner_token = ? AND state = 'running' "
                "AND expires_at >= ?",
                (now, now + self.timeout_sec, job_id, owner, now),
            )
            if cursor.rowcount != 1:
                lease._lost.set()
                raise PublicationError("writer lease was lost")
            self.conn.commit()
        except BaseException:
            stop_event.set()
            if heartbeat_thread is not None and heartbeat_thread.is_alive():
                heartbeat_thread.join(timeout=max(1.0, self.heartbeat_sec * 2))
            self.conn.rollback()
            try:
                failed_at = time.time()
                self.conn.execute(
                    "UPDATE job_records SET state = 'failed', heartbeat = ?, expires_at = ? "
                    "WHERE job_id = ? AND owner_token = ? AND state = 'running'",
                    (failed_at, failed_at + self.timeout_sec, job_id, owner),
                )
                self.conn.commit()
            except sqlite3.Error:
                self.conn.rollback()
            raise

    def acquire_reader(self, publication_id: str, generation_id: str | None) -> str:
        lease_id = f"lease_{uuid.uuid4().hex}"
        now = time.time()
        self.conn.execute(
            "INSERT INTO publication_leases(lease_id, publication_id, generation_id, owner_token, heartbeat, expires_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (lease_id, publication_id, generation_id, self.boot_token, now, now + self.timeout_sec),
        )
        self.conn.commit()
        return lease_id

    def release_reader(self, lease_id: str) -> None:
        self.conn.execute("DELETE FROM publication_leases WHERE lease_id = ?", (lease_id,))
        self.conn.commit()


def resolve_snapshot(conn: sqlite3.Connection, *, acquire_lease: bool = True) -> PublicationSnapshot:
    conn.execute("BEGIN")
    try:
        state = conn.execute(
            "SELECT current_publication_id FROM library_state WHERE singleton = 1"
        ).fetchone()
        if not state or not state[0]:
            raise NoActivePublicationError("no active search publication")
        publication_id = str(state[0])
        publication = conn.execute(
            "SELECT generation_id, manifest_checksum FROM search_publications WHERE publication_id = ?",
            (publication_id,),
        ).fetchone()
        rows = conn.execute(
            "SELECT public_video_id, source_generation, transcript_revision, semantic_covered "
            "FROM search_publication_members WHERE publication_id = ? ORDER BY public_video_id",
            (publication_id,),
        ).fetchall()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    generation_id = str(publication[0]) if publication and publication[0] else None
    checksum = str(publication[1]) if publication and publication[1] else None
    lease_id = LeaseManager(conn).acquire_reader(publication_id, generation_id) if acquire_lease else None
    return PublicationSnapshot(
        publication_id, generation_id, checksum,
        tuple(SnapshotMember(str(r[0]), str(r[1]), str(r[2]), bool(r[3])) for r in rows),
        lease_id,
    )


def release_snapshot(conn: sqlite3.Connection, snapshot: PublicationSnapshot) -> None:
    if snapshot.lease_id:
        LeaseManager(conn).release_reader(snapshot.lease_id)


def _prospective_members(
    conn: sqlite3.Connection,
    transcript_updates: Mapping[str, str] | None = None,
) -> tuple[tuple[str, str, str], ...]:
    members = {
        str(row[0]): (str(row[0]), str(row[1]), str(row[2]))
        for row in conn.execute(
            "SELECT public_video_id, source_generation, transcript_revision "
            "FROM active_transcripts"
        ).fetchall()
    }
    for public_video_id, transcript_revision in (transcript_updates or {}).items():
        row = conn.execute(
            "SELECT v.public_video_id, r.source_generation, r.transcript_revision "
            "FROM transcript_revisions r JOIN videos v "
            "ON v.source_generation = r.source_generation "
            "WHERE v.public_video_id = ? AND r.transcript_revision = ?",
            (public_video_id, transcript_revision),
        ).fetchone()
        if not row:
            raise PublicationError(
                "draft transcript revision does not belong to the selected video"
            )
        if db.transcript_revision_status(conn, transcript_revision) not in {
            "TEXT_READY", "ACTIVE"
        }:
            raise PublicationError("draft transcript revision is not complete")
        members[str(public_video_id)] = (
            str(row[0]), str(row[1]), str(row[2])
        )
    return tuple(members[key] for key in sorted(members))


def _chunk_rows_for_members(
    conn: sqlite3.Connection,
    members: tuple[tuple[str, str, str], ...],
) -> list[sqlite3.Row]:
    revisions = {public_id: revision for public_id, _, revision in members}
    rows = conn.execute(
        "SELECT c.chunk_id, v.public_video_id, c.transcript_revision, "
        "c.start_sec, c.end_sec, c.text FROM text_chunks c "
        "JOIN videos v ON v.video_id = c.video_id ORDER BY c.chunk_id"
    ).fetchall()
    return [
        row for row in rows
        if revisions.get(str(row[1])) == str(row[2])
    ]


def prospective_chunk_ids(
    conn: sqlite3.Connection,
    transcript_updates: Mapping[str, str] | None = None,
) -> tuple[int, ...]:
    members = _prospective_members(conn, transcript_updates)
    return tuple(int(row[0]) for row in _chunk_rows_for_members(conn, members))


def _current_vector_index_path(conn: sqlite3.Connection) -> Path | None:
    row = conn.execute(
        "SELECT p.generation_id FROM library_state s "
        "LEFT JOIN search_publications p "
        "ON p.publication_id = s.current_publication_id "
        "WHERE s.singleton = 1"
    ).fetchone()
    generation_id = str(row[0]) if row and row[0] else None
    if generation_id and generation_id != "legacy_current":
        immutable = config.search_generations_dir() / generation_id / "vectors.faiss"
        if not immutable.exists():
            raise PublicationError("current immutable vector generation is missing")
        return immutable
    compatibility = Path(config.TEXT_INDEX_PATH)
    return compatibility if compatibility.exists() else None


def verify_vector_index_exact(
    path: Path,
    expected_ids: Iterator[int] | tuple[int, ...] | list[int],
    dimension: int,
) -> None:
    expected = tuple(int(chunk_id) for chunk_id in expected_ids)
    try:
        index = VectorIndex.load(Path(path), int(dimension))
    except Exception as exc:
        raise PublicationError("vector draft cannot be loaded") from exc
    if int(getattr(index.index, "d", -1)) != int(dimension):
        raise PublicationError("vector draft dimension does not match")
    if int(index.index.ntotal) != len(expected):
        raise PublicationError("vector draft ID count does not match publication chunks")
    for chunk_id in expected:
        try:
            index.index.reconstruct(chunk_id)
        except RuntimeError as exc:
            raise PublicationError(
                "vector draft is missing a publication chunk ID"
            ) from exc


def build_vector_index_draft(
    conn: sqlite3.Connection,
    transcript_updates: Mapping[str, str] | None,
    replacement_vectors: Mapping[int, np.ndarray],
    dimension: int,
) -> tuple[Path, tuple[int, ...]]:
    """Build an exact-ID vector draft from the immutable expected generation."""
    expected_ids = prospective_chunk_ids(conn, transcript_updates)
    expected_set = set(expected_ids)
    replacements = {
        int(chunk_id): np.asarray(vector, dtype="float32").reshape(-1)
        for chunk_id, vector in replacement_vectors.items()
    }
    unexpected = set(replacements) - expected_set
    if unexpected:
        raise PublicationError("replacement vectors contain unpublished chunk IDs")
    base_ids = [chunk_id for chunk_id in expected_ids if chunk_id not in replacements]
    base_index = None
    if base_ids:
        base_path = _current_vector_index_path(conn)
        if base_path is None:
            raise PublicationError("base vector generation is unavailable")
        try:
            base_index = VectorIndex.load(base_path, int(dimension))
        except Exception as exc:
            raise PublicationError(
                "immutable base vector generation cannot be loaded"
            ) from exc
        if int(getattr(base_index.index, "d", -1)) != int(dimension):
            raise PublicationError("base vector dimension does not match")

    vectors = []
    for chunk_id in expected_ids:
        vector = replacements.get(chunk_id)
        if vector is None:
            try:
                vector = np.asarray(
                    base_index.index.reconstruct(chunk_id), dtype="float32"
                )
            except RuntimeError as exc:
                raise PublicationError(
                    "immutable base generation is missing an active chunk ID"
                ) from exc
        if vector.shape != (int(dimension),):
            raise PublicationError("replacement vector dimension does not match")
        vectors.append(vector)

    draft = VectorIndex(int(dimension))
    if expected_ids:
        draft.add(
            np.asarray(expected_ids, dtype="int64"),
            np.asarray(vectors, dtype="float32"),
        )
    target_dir = Path(config.TEXT_INDEX_PATH).parent
    target_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        suffix=".generation.faiss.tmp", dir=target_dir, delete=False
    ) as temporary:
        draft_path = Path(temporary.name)
    try:
        draft.save(draft_path)
        verify_vector_index_exact(draft_path, expected_ids, int(dimension))
        return draft_path, expected_ids
    except Exception:
        draft_path.unlink(missing_ok=True)
        raise


def _manifest_for_current(
    conn: sqlite3.Connection,
    generation_id: str,
    members: tuple[tuple[str, str, str], ...],
) -> dict:
    chunks = [list(row[:3]) for row in _chunk_rows_for_members(conn, members)]
    return {
        "schema_version": 1,
        "generation_id": generation_id,
        "embedding": {"model": config.EMBED_MODEL_NAME, "dimension": config.EMBED_VECTOR_DIM,
                      "normalized": True},
        "chunking": {"seconds": config.CHUNK_SEC, "overlap_seconds": config.OVERLAP_SEC},
        "members": [list(row) for row in members],
        "chunks": chunks,
    }


def _file_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _install_staged_generation(staging: Path, final: Path) -> None:
    os.replace(staging, final)


def _write_chunk_snapshot(
    conn: sqlite3.Connection,
    target: Path,
    members: tuple[tuple[str, str, str], ...],
) -> None:
    snapshot = sqlite3.connect(target)
    try:
        snapshot.execute(
            "CREATE TABLE chunks(chunk_id INTEGER PRIMARY KEY, public_video_id TEXT NOT NULL, "
            "transcript_revision TEXT NOT NULL, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, text TEXT)"
        )
        rows = [
            (
                int(row[0]),
                str(row[1]),
                str(row[2]),
                round(float(row[3]) * 1000),
                round(float(row[4]) * 1000),
                row[5],
            )
            for row in _chunk_rows_for_members(conn, members)
        ]
        snapshot.executemany("INSERT INTO chunks VALUES(?, ?, ?, ?, ?, ?)", rows)
        snapshot.commit()
    finally:
        snapshot.close()


_EXPECTED_UNSET = object()


def _assert_writer_owned_in_transaction(
    conn: sqlite3.Connection, lease: WriterLease
) -> None:
    """Revalidate the writer in the same transaction as the publication CAS."""
    row = conn.execute(
        "SELECT owner_token, state, expires_at FROM job_records WHERE job_id = ?",
        (lease.job_id,),
    ).fetchone()
    if (
        not row
        or str(row[0]) != lease.owner_token
        or str(row[1]) != "running"
        or float(row[2]) < time.time()
    ):
        lease._lost.set()
        raise PublicationError("writer lease was lost")


def publish_current_generation(
    conn: sqlite3.Connection,
    expected_publication_id: str | None | object = _EXPECTED_UNSET,
    *,
    transcript_updates: Mapping[str, str] | None = None,
    vector_draft_path: Path | None = None,
    source_fingerprints: Mapping[str, str] | None = None,
    writer_lease: WriterLease | None = None,
) -> PublicationSnapshot:
    """Publish a verified vector draft and atomically activate transcript drafts."""
    writer_scope = (
        nullcontext(writer_lease)
        if writer_lease is not None
        else LeaseManager(conn).writer()
    )
    committed_snapshot: PublicationSnapshot | None = None
    with writer_scope as lease:
        lease.assert_owned()
        state = conn.execute(
            "SELECT current_publication_id FROM library_state WHERE singleton = 1"
        ).fetchone()
        current = str(state[0]) if state and state[0] else None
        if (
            expected_publication_id is not _EXPECTED_UNSET
            and current != expected_publication_id
        ):
            raise PublicationError("publication compare-and-swap failed")
        members = _prospective_members(conn, transcript_updates)
        chunk_rows = _chunk_rows_for_members(conn, members)
        expected_ids = tuple(int(row[0]) for row in chunk_rows)
        covered_revisions = {str(row[2]) for row in chunk_rows}
        vector_source = (
            Path(vector_draft_path)
            if vector_draft_path is not None
            else _current_vector_index_path(conn)
        )
        if vector_source is None and expected_ids:
            raise PublicationError("vector generation is unavailable")
        if vector_source is not None:
            verify_vector_index_exact(
                vector_source, expected_ids, int(config.EMBED_VECTOR_DIM)
            )
        generation_id = f"gen_{uuid.uuid4().hex}"
        publication_id = f"pub_{uuid.uuid4().hex}"
        root = config.search_generations_dir()
        root.mkdir(parents=True, exist_ok=True)
        staging = root / f".{generation_id}.staging"
        final = root / generation_id
        staging.mkdir()
        try:
            manifest = _manifest_for_current(conn, generation_id, members)
            _write_chunk_snapshot(conn, staging / "chunks.sqlite", members)
            if vector_source is not None:
                shutil.copy2(vector_source, staging / "vectors.faiss")
                verify_vector_index_exact(
                    staging / "vectors.faiss",
                    expected_ids,
                    int(config.EMBED_VECTOR_DIM),
                )
            manifest["artifacts"] = {
                "chunks.sqlite": _file_checksum(staging / "chunks.sqlite"),
                "vectors.faiss": _file_checksum(staging / "vectors.faiss")
                if (staging / "vectors.faiss").exists() else None,
            }
            manifest_raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            checksum = hashlib.sha256(manifest_raw).hexdigest()
            (staging / "manifest.json").write_bytes(manifest_raw)
            lease.assert_owned()
            _install_staged_generation(staging, final)
            conn.execute("BEGIN IMMEDIATE")
            _assert_writer_owned_in_transaction(conn, lease)
            latest = conn.execute(
                "SELECT current_publication_id FROM library_state WHERE singleton = 1"
            ).fetchone()
            latest_publication = str(latest[0]) if latest and latest[0] else None
            if (
                expected_publication_id is not _EXPECTED_UNSET
                and latest_publication != expected_publication_id
            ):
                raise PublicationError("publication compare-and-swap failed")
            for public_video_id, transcript_revision in (transcript_updates or {}).items():
                db.activate_transcript_revision(
                    conn, public_video_id, transcript_revision
                )
            for source_generation, fingerprint in (source_fingerprints or {}).items():
                source_update = conn.execute(
                    "UPDATE sources SET private_fingerprint = ?, status = 'available' "
                    "WHERE source_generation = ? AND "
                    "(private_fingerprint IS NULL OR private_fingerprint = ?)",
                    (fingerprint, source_generation, fingerprint),
                )
                if source_update.rowcount != 1:
                    raise PublicationError(
                        "source generation fingerprint changed before publication"
                    )
            conn.execute(
                "INSERT INTO search_generations(generation_id, manifest_checksum, status, directory) "
                "VALUES(?, ?, 'READY', ?)", (generation_id, checksum, str(final))
            )
            conn.execute(
                "INSERT INTO search_publications(publication_id, generation_id, manifest_checksum) "
                "VALUES(?, ?, ?)", (publication_id, generation_id, checksum)
            )
            member_rows = [
                (
                    publication_id,
                    *member,
                    int(member[2] in covered_revisions),
                )
                for member in members
            ]
            conn.executemany(
                "INSERT INTO search_publication_members"
                "(publication_id, public_video_id, source_generation, "
                "transcript_revision, semantic_covered) VALUES(?, ?, ?, ?, ?)",
                member_rows,
            )
            conn.execute(
                "UPDATE library_state SET current_publication_id = ? WHERE singleton = 1",
                (publication_id,),
            )
            revisions = [member[2] for member in members]
            if revisions:
                conn.execute(
                    "UPDATE text_chunks SET generation_id = ? WHERE transcript_revision IN ("
                    + ",".join("?" for _ in revisions)
                    + ")",
                    (generation_id, *revisions),
                )
            committed_snapshot = PublicationSnapshot(
                publication_id,
                generation_id,
                checksum,
                tuple(
                    SnapshotMember(
                        member[0],
                        member[1],
                        member[2],
                        member[2] in covered_revisions,
                    )
                    for member in members
                ),
            )
            conn.commit()
            lease.publication_committed = True
        except Exception:
            conn.rollback()
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if final.exists():
                shutil.rmtree(final, ignore_errors=True)
            raise
    assert committed_snapshot is not None
    return committed_snapshot


def publish_text_snapshot(
    conn: sqlite3.Connection,
    expected_publication_id: str | None,
    *,
    transcript_updates: Mapping[str, str] | None = None,
    writer_lease: WriterLease | None = None,
    transaction_open: bool = False,
) -> PublicationSnapshot:
    """Atomically expose completed transcripts while semantic generation is pending."""
    writer_scope = (
        nullcontext(writer_lease)
        if writer_lease is not None
        else LeaseManager(conn).writer()
    )
    snapshot: PublicationSnapshot | None = None
    with writer_scope as lease:
        lease.assert_owned()
        publication_id = f"pub_{uuid.uuid4().hex}"
        manages_transaction = not transaction_open
        if transaction_open and not conn.in_transaction:
            raise PublicationError("text publication requires an open transaction")
        if manages_transaction:
            conn.execute("BEGIN IMMEDIATE")
        try:
            _assert_writer_owned_in_transaction(conn, lease)
            row = conn.execute(
                "SELECT current_publication_id FROM library_state WHERE singleton = 1"
            ).fetchone()
            current = str(row[0]) if row and row[0] else None
            if current != expected_publication_id:
                raise PublicationError("publication compare-and-swap failed")
            members = _prospective_members(conn, transcript_updates)
            for public_video_id, transcript_revision in (transcript_updates or {}).items():
                db.activate_transcript_revision(
                    conn, public_video_id, transcript_revision
                )
            conn.execute(
                "INSERT INTO search_publications(publication_id, generation_id) VALUES(?, NULL)",
                (publication_id,),
            )
            conn.executemany(
                "INSERT INTO search_publication_members"
                "(publication_id, public_video_id, source_generation, "
                "transcript_revision, semantic_covered) VALUES(?, ?, ?, ?, 0)",
                [(publication_id, *member) for member in members],
            )
            conn.execute(
                "UPDATE library_state SET current_publication_id = ? WHERE singleton = 1",
                (publication_id,),
            )
            snapshot = PublicationSnapshot(
                publication_id,
                None,
                None,
                tuple(SnapshotMember(*member, False) for member in members),
            )
            if manages_transaction:
                conn.commit()
                lease.publication_committed = True
        except Exception:
            if manages_transaction:
                conn.rollback()
            raise
    assert snapshot is not None
    return snapshot


def cleanup_orphan_generations(conn: sqlite3.Connection, grace_sec: float = 3600.0) -> list[Path]:
    root = config.search_generations_dir()
    if not root.exists():
        return []
    referenced = {str(row[0]) for row in conn.execute(
        "SELECT generation_id FROM search_publications WHERE generation_id IS NOT NULL"
    )}
    leased = {str(row[0]) for row in conn.execute(
        "SELECT generation_id FROM publication_leases WHERE expires_at >= ? AND generation_id IS NOT NULL",
        (time.time(),),
    )}
    removed = []
    for child in root.iterdir():
        generation_id = child.name.removeprefix(".").removesuffix(".staging")
        if generation_id in referenced or generation_id in leased:
            continue
        if time.time() - child.stat().st_mtime < grace_sec:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
        removed.append(child)
    return removed
