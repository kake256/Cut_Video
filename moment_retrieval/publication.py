from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import config, db


class PublicationError(RuntimeError):
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


class LeaseManager:
    def __init__(self, conn: sqlite3.Connection, timeout_sec: float = 120.0):
        self.conn = conn
        self.timeout_sec = timeout_sec
        self.boot_token = secrets.token_hex(16)

    def cleanup_stale(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        cursor = self.conn.execute(
            "DELETE FROM job_records WHERE expires_at < ?", (now,)
        )
        self.conn.execute("DELETE FROM publication_leases WHERE expires_at < ?", (now,))
        self.conn.commit()
        return int(cursor.rowcount)

    @contextmanager
    def writer(self, kind: str = "search_publish") -> Iterator[str]:
        self.cleanup_stale()
        now = time.time()
        job_id = f"job_{uuid.uuid4().hex}"
        owner = f"{os.getpid()}:{self.boot_token}"
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            active = self.conn.execute(
                "SELECT job_id FROM job_records WHERE kind = ? AND state = 'running' "
                "AND expires_at >= ? LIMIT 1", (kind, now)
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
            yield job_id
            self.conn.execute(
                "UPDATE job_records SET state = 'complete', heartbeat = ?, expires_at = ? WHERE job_id = ?",
                (time.time(), time.time() + self.timeout_sec, job_id),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            self.conn.execute(
                "UPDATE job_records SET state = 'failed', heartbeat = ?, expires_at = ? WHERE job_id = ?",
                (time.time(), time.time() + self.timeout_sec, job_id),
            )
            self.conn.commit()
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
            raise PublicationError("no active search publication")
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


def _manifest_for_current(conn: sqlite3.Connection, generation_id: str) -> dict:
    members = conn.execute(
        "SELECT a.public_video_id, a.source_generation, a.transcript_revision "
        "FROM active_transcripts a ORDER BY a.public_video_id"
    ).fetchall()
    chunks = conn.execute(
        "SELECT chunk_id, v.public_video_id, c.transcript_revision "
        "FROM text_chunks c JOIN videos v ON v.video_id = c.video_id ORDER BY chunk_id"
    ).fetchall()
    return {
        "schema_version": 1,
        "generation_id": generation_id,
        "embedding": {"model": config.EMBED_MODEL_NAME, "dimension": config.EMBED_VECTOR_DIM,
                      "normalized": True},
        "chunking": {"seconds": config.CHUNK_SEC, "overlap_seconds": config.OVERLAP_SEC},
        "members": [list(row) for row in members],
        "chunks": [list(row) for row in chunks],
    }


def _file_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_chunk_snapshot(conn: sqlite3.Connection, target: Path) -> None:
    snapshot = sqlite3.connect(target)
    try:
        snapshot.execute(
            "CREATE TABLE chunks(chunk_id INTEGER PRIMARY KEY, public_video_id TEXT NOT NULL, "
            "transcript_revision TEXT NOT NULL, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, text TEXT)"
        )
        rows = conn.execute(
            "SELECT c.chunk_id, v.public_video_id, c.transcript_revision, "
            "CAST(round(c.start_sec * 1000) AS INTEGER), CAST(round(c.end_sec * 1000) AS INTEGER), c.text "
            "FROM text_chunks c JOIN videos v ON v.video_id = c.video_id "
            "JOIN active_transcripts a ON a.public_video_id = v.public_video_id "
            "AND a.transcript_revision = c.transcript_revision ORDER BY c.chunk_id"
        ).fetchall()
        snapshot.executemany("INSERT INTO chunks VALUES(?, ?, ?, ?, ?, ?)", rows)
        snapshot.commit()
    finally:
        snapshot.close()


def publish_current_generation(
    conn: sqlite3.Connection, expected_publication_id: str | None = None
) -> PublicationSnapshot:
    """Publish current compatibility DB/FAISS as an immutable full generation."""
    with LeaseManager(conn).writer():
        state = conn.execute(
            "SELECT current_publication_id FROM library_state WHERE singleton = 1"
        ).fetchone()
        current = str(state[0]) if state and state[0] else None
        if expected_publication_id is not None and current != expected_publication_id:
            raise PublicationError("publication compare-and-swap failed")
        generation_id = f"gen_{uuid.uuid4().hex}"
        publication_id = f"pub_{uuid.uuid4().hex}"
        root = config.search_generations_dir()
        root.mkdir(parents=True, exist_ok=True)
        staging = root / f".{generation_id}.staging"
        final = root / generation_id
        staging.mkdir()
        try:
            manifest = _manifest_for_current(conn, generation_id)
            _write_chunk_snapshot(conn, staging / "chunks.sqlite")
            if config.TEXT_INDEX_PATH.exists():
                shutil.copy2(config.TEXT_INDEX_PATH, staging / "vectors.faiss")
            manifest["artifacts"] = {
                "chunks.sqlite": _file_checksum(staging / "chunks.sqlite"),
                "vectors.faiss": _file_checksum(staging / "vectors.faiss")
                if (staging / "vectors.faiss").exists() else None,
            }
            manifest_raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            checksum = hashlib.sha256(manifest_raw).hexdigest()
            (staging / "manifest.json").write_bytes(manifest_raw)
            os.replace(staging, final)
            conn.execute("BEGIN IMMEDIATE")
            latest = conn.execute(
                "SELECT current_publication_id FROM library_state WHERE singleton = 1"
            ).fetchone()
            if expected_publication_id is not None and str(latest[0]) != expected_publication_id:
                raise PublicationError("publication compare-and-swap failed")
            conn.execute(
                "INSERT INTO search_generations(generation_id, manifest_checksum, status, directory) "
                "VALUES(?, ?, 'READY', ?)", (generation_id, checksum, str(final))
            )
            conn.execute(
                "INSERT INTO search_publications(publication_id, generation_id, manifest_checksum) "
                "VALUES(?, ?, ?)", (publication_id, generation_id, checksum)
            )
            conn.execute(
                "INSERT INTO search_publication_members(publication_id, public_video_id, source_generation, "
                "transcript_revision, semantic_covered) SELECT ?, public_video_id, source_generation, "
                "transcript_revision, 1 FROM active_transcripts",
                (publication_id,),
            )
            conn.execute(
                "UPDATE library_state SET current_publication_id = ? WHERE singleton = 1",
                (publication_id,),
            )
            conn.execute(
                "UPDATE text_chunks SET generation_id = ? WHERE generation_id IS NULL",
                (generation_id,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise
    return resolve_snapshot(conn, acquire_lease=False)


def publish_text_snapshot(
    conn: sqlite3.Connection, expected_publication_id: str | None
) -> PublicationSnapshot:
    """Atomically expose completed transcripts while semantic generation is pending."""
    publication_id = f"pub_{uuid.uuid4().hex}"
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT current_publication_id FROM library_state WHERE singleton = 1"
        ).fetchone()
        current = str(row[0]) if row and row[0] else None
        if current != expected_publication_id:
            raise PublicationError("publication compare-and-swap failed")
        conn.execute(
            "INSERT INTO search_publications(publication_id, generation_id) VALUES(?, NULL)",
            (publication_id,),
        )
        conn.execute(
            "INSERT INTO search_publication_members(publication_id, public_video_id, source_generation, "
            "transcript_revision, semantic_covered) SELECT ?, public_video_id, source_generation, "
            "transcript_revision, 0 FROM active_transcripts", (publication_id,)
        )
        conn.execute(
            "UPDATE library_state SET current_publication_id = ? WHERE singleton = 1",
            (publication_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return resolve_snapshot(conn, acquire_lease=False)


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
