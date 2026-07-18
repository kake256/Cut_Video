from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Iterable, Optional

from . import config

SCHEMA_VERSION = 4
PUBLIC_ID_PREFIX = "vid_"

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    duration REAL,
    created_at TEXT DEFAULT (datetime('now')),
    asr_complete INTEGER NOT NULL DEFAULT 0,
    public_video_id TEXT,
    display_name TEXT,
    source_generation TEXT,
    content_digest TEXT,
    source_state TEXT NOT NULL DEFAULT 'available'
);

CREATE TABLE IF NOT EXISTS legacy_video_aliases (
    alias TEXT PRIMARY KEY,
    public_video_id TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS asr_segments (
    segment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL REFERENCES videos(video_id),
    start_sec REAL NOT NULL,
    end_sec REAL NOT NULL,
    text TEXT,
    words_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_segments_video ON asr_segments(video_id);

CREATE TABLE IF NOT EXISTS text_chunks (
    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL REFERENCES videos(video_id),
    start_sec REAL NOT NULL,
    end_sec REAL NOT NULL,
    text TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_video ON text_chunks(video_id);

CREATE TABLE IF NOT EXISTS sources (
    source_generation TEXT PRIMARY KEY,
    public_video_id TEXT NOT NULL,
    locator TEXT,
    private_fingerprint TEXT,
    status TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sources_video ON sources(public_video_id);

CREATE TABLE IF NOT EXISTS transcript_revisions (
    transcript_revision TEXT PRIMARY KEY,
    source_generation TEXT NOT NULL,
    status TEXT NOT NULL,
    asr_config_json TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS active_transcripts (
    public_video_id TEXT PRIMARY KEY,
    source_generation TEXT NOT NULL,
    transcript_revision TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_generations (
    generation_id TEXT PRIMARY KEY,
    manifest_checksum TEXT,
    status TEXT NOT NULL,
    directory TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS search_publications (
    publication_id TEXT PRIMARY KEY,
    generation_id TEXT,
    manifest_checksum TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS search_publication_members (
    publication_id TEXT NOT NULL,
    public_video_id TEXT NOT NULL,
    source_generation TEXT NOT NULL,
    transcript_revision TEXT NOT NULL,
    semantic_covered INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(publication_id, public_video_id)
);

CREATE TABLE IF NOT EXISTS library_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    current_publication_id TEXT
);

CREATE TABLE IF NOT EXISTS job_records (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    owner_token TEXT NOT NULL,
    owner_pid INTEGER NOT NULL,
    state TEXT NOT NULL,
    heartbeat REAL NOT NULL,
    expires_at REAL NOT NULL,
    payload_json TEXT
);

CREATE TABLE IF NOT EXISTS publication_leases (
    lease_id TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL,
    generation_id TEXT,
    owner_token TEXT NOT NULL,
    heartbeat REAL NOT NULL,
    expires_at REAL NOT NULL
);
"""


def new_public_video_id() -> str:
    """Return an opaque stable ID which contains no path or filename material."""
    return f"{PUBLIC_ID_PREFIX}{uuid.uuid4().hex}"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _database_path(conn: sqlite3.Connection) -> Path | None:
    for _, name, filename in conn.execute("PRAGMA database_list"):
        if name == "main" and filename:
            return Path(filename)
    return None


def _backup_before_migration(conn: sqlite3.Connection, from_version: int) -> Path | None:
    source = _database_path(conn)
    if source is None or not source.exists() or source.stat().st_size == 0:
        return None
    backup_dir = source.parent / "migration-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{source.name}.v{from_version}.bak"
    temporary = target.with_suffix(target.suffix + ".tmp")
    backup = sqlite3.connect(temporary)
    try:
        conn.backup(backup)
        backup.commit()
    finally:
        backup.close()
    os.replace(temporary, target)
    return target


def _migrate_legacy_schema(conn: sqlite3.Connection) -> None:
    columns = _columns(conn, "videos")
    additions = {
        "asr_complete": "INTEGER NOT NULL DEFAULT 0",
        "public_video_id": "TEXT",
        "display_name": "TEXT",
        "source_generation": "TEXT",
        "content_digest": "TEXT",
        "source_state": "TEXT NOT NULL DEFAULT 'available'",
    }
    for name, declaration in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE videos ADD COLUMN {name} {declaration}")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS legacy_video_aliases ("
        "alias TEXT PRIMARY KEY, public_video_id TEXT NOT NULL UNIQUE)"
    )
    rows = conn.execute(
        "SELECT video_id, path, public_video_id, display_name FROM videos"
    ).fetchall()
    for row in rows:
        public_id = row[2] or new_public_video_id()
        display_name = row[3] or Path(row[1]).name
        conn.execute(
            "UPDATE videos SET public_video_id = ?, display_name = ? "
            "WHERE video_id = ?",
            (public_id, display_name, row[0]),
        )
        conn.execute(
            "INSERT OR REPLACE INTO legacy_video_aliases(alias, public_video_id) "
            "VALUES (?, ?)",
            (row[0], public_id),
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_public_id "
        "ON videos(public_video_id)"
    )
    conn.execute(
        "UPDATE videos SET asr_complete = 1 WHERE video_id IN "
        "(SELECT DISTINCT video_id FROM text_chunks)"
    )
    segment_columns = _columns(conn, "asr_segments")
    if "transcript_revision" not in segment_columns:
        conn.execute("ALTER TABLE asr_segments ADD COLUMN transcript_revision TEXT")
    chunk_columns = _columns(conn, "text_chunks")
    if "transcript_revision" not in chunk_columns:
        conn.execute("ALTER TABLE text_chunks ADD COLUMN transcript_revision TEXT")
    if "generation_id" not in chunk_columns:
        conn.execute("ALTER TABLE text_chunks ADD COLUMN generation_id TEXT")
    conn.execute("INSERT OR IGNORE INTO library_state(singleton, current_publication_id) VALUES(1, NULL)")
    _backfill_publication_metadata(conn)


def _backfill_publication_metadata(conn: sqlite3.Connection) -> None:
    """Describe legacy rows as one immutable logical publication without re-ASR."""
    active_rows = []
    for row in conn.execute(
        "SELECT video_id, public_video_id, path, asr_complete, source_generation "
        "FROM videos"
    ).fetchall():
        public_id = str(row[1])
        source_generation = str(row[4] or f"src_{uuid.uuid4().hex}")
        source_status = "available" if Path(row[2]).exists() else "missing"
        conn.execute(
            "UPDATE videos SET source_generation = ?, source_state = ? WHERE video_id = ?",
            (source_generation, source_status, row[0]),
        )
        conn.execute(
            "INSERT OR IGNORE INTO sources(source_generation, public_video_id, locator, status) "
            "VALUES(?, ?, ?, ?)",
            (source_generation, public_id, row[2], source_status),
        )
        if not row[3]:
            continue
        revision = f"tr_{uuid.uuid4().hex}"
        existing = conn.execute(
            "SELECT transcript_revision FROM active_transcripts WHERE public_video_id = ?",
            (public_id,),
        ).fetchone()
        if existing:
            revision = str(existing[0])
        else:
            conn.execute(
                "INSERT INTO transcript_revisions(transcript_revision, source_generation, status, asr_config_json) "
                "VALUES(?, ?, 'TEXT_READY', ?)",
                (revision, source_generation, json.dumps({"model": "unknown", "language": "unknown"})),
            )
            conn.execute(
                "INSERT INTO active_transcripts(public_video_id, source_generation, transcript_revision) "
                "VALUES(?, ?, ?)",
                (public_id, source_generation, revision),
            )
            conn.execute(
                "UPDATE asr_segments SET transcript_revision = ? "
                "WHERE video_id = ? AND transcript_revision IS NULL",
                (revision, row[0]),
            )
            conn.execute(
                "UPDATE text_chunks SET transcript_revision = ? "
                "WHERE video_id = ? AND transcript_revision IS NULL",
                (revision, row[0]),
            )
        semantic = bool(conn.execute(
            "SELECT 1 FROM text_chunks WHERE video_id = ? LIMIT 1", (row[0],)
        ).fetchone())
        active_rows.append((public_id, source_generation, revision, semantic))
    current = conn.execute(
        "SELECT current_publication_id FROM library_state WHERE singleton = 1"
    ).fetchone()
    if active_rows and (not current or current[0] is None):
        publication_id = f"pub_{uuid.uuid4().hex}"
        generation_id = "legacy_current" if any(row[3] for row in active_rows) else None
        conn.execute(
            "INSERT INTO search_publications(publication_id, generation_id) VALUES(?, ?)",
            (publication_id, generation_id),
        )
        conn.executemany(
            "INSERT INTO search_publication_members(publication_id, public_video_id, "
            "source_generation, transcript_revision, semantic_covered) VALUES(?, ?, ?, ?, ?)",
            [(publication_id, *row) for row in active_rows],
        )
        conn.execute(
            "UPDATE library_state SET current_publication_id = ? WHERE singleton = 1",
            (publication_id,),
        )


def init_db(conn: sqlite3.Connection, *, create_backup: bool = True) -> Path | None:
    """Create/migrate the compatibility schema without rebuilding ASR or FAISS.

    Legacy ``videos.video_id`` remains the storage foreign key.  It is isolated
    behind repository resolution while all public DTOs use ``public_video_id``.
    A file database is backed up once before an old schema is changed.
    """
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    had_videos = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='videos'"
        ).fetchone()
    )
    backup = None
    if had_videos and current < SCHEMA_VERSION and create_backup:
        backup = _backup_before_migration(conn, current)
    try:
        conn.execute("BEGIN IMMEDIATE")
        # sqlite3.executescript() performs an implicit commit; execute each
        # simple DDL statement explicitly so migration rollback is real.
        for statement in SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        _migrate_legacy_schema(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return backup


def get_conn() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _storage_id(conn: sqlite3.Connection, identifier: str) -> str | None:
    row = conn.execute(
        "SELECT video_id FROM videos WHERE video_id = ? OR public_video_id = ?",
        (identifier, identifier),
    ).fetchone()
    if row:
        return str(row[0])
    row = conn.execute(
        "SELECT v.video_id FROM legacy_video_aliases a JOIN videos v "
        "ON v.public_video_id = a.public_video_id WHERE a.alias = ?",
        (identifier,),
    ).fetchone()
    return str(row[0]) if row else None


def storage_video_id(conn: sqlite3.Connection, identifier: str) -> str | None:
    """Repository-only compatibility lookup; never expose this in a public DTO."""
    try:
        return _storage_id(conn, identifier)
    except (AttributeError, sqlite3.DatabaseError):
        # Test/adapter connections may intentionally implement only execute+fetchall.
        return identifier


def public_video_id(conn: sqlite3.Connection, identifier: str) -> str | None:
    storage_id = _storage_id(conn, identifier)
    if storage_id is None:
        return None
    row = conn.execute(
        "SELECT public_video_id FROM videos WHERE video_id = ?", (storage_id,)
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def insert_video(conn: sqlite3.Connection, video_id: str, path: str, duration: float) -> None:
    public_id = video_id if video_id.startswith(PUBLIC_ID_PREFIX) else new_public_video_id()
    source_generation = f"src_{uuid.uuid4().hex}"
    source_status = "available" if Path(path).exists() else "missing"
    conn.execute(
        "INSERT INTO videos (video_id, path, duration, public_video_id, display_name, "
        "source_generation, source_state) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (video_id, path, duration, public_id, Path(path).name, source_generation, source_status),
    )
    conn.execute(
        "INSERT INTO legacy_video_aliases(alias, public_video_id) VALUES (?, ?)",
        (video_id, public_id),
    )
    conn.execute(
        "INSERT INTO sources(source_generation, public_video_id, locator, status) VALUES(?, ?, ?, ?)",
        (source_generation, public_id, path, source_status),
    )


def get_video(conn: sqlite3.Connection, video_id: str) -> Optional[dict]:
    storage_id = _storage_id(conn, video_id)
    if storage_id is None:
        return None
    row = conn.execute("SELECT * FROM videos WHERE video_id = ?", (storage_id,)).fetchone()
    return dict(row) if row else None


def _public_record(row: sqlite3.Row | dict) -> dict:
    value = dict(row)
    return {
        "public_video_id": value["public_video_id"],
        "display_name": value.get("display_name") or Path(value["path"]).name,
        "path": value["path"],
        "duration": value.get("duration"),
        "asr_complete": bool(value.get("asr_complete")),
        "source_generation": value.get("source_generation"),
        "content_digest": value.get("content_digest"),
        "source_state": value.get("source_state") or "available",
    }


def get_public_video(conn: sqlite3.Connection, identifier: str) -> Optional[dict]:
    row = get_video(conn, identifier)
    return _public_record(row) if row else None


def list_videos(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM videos ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def list_public_videos(conn: sqlite3.Connection) -> list[dict]:
    return [_public_record(row) for row in conn.execute(
        "SELECT * FROM videos ORDER BY created_at"
    ).fetchall()]


def find_video_by_path(conn: sqlite3.Connection, path: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM videos WHERE path = ?", (str(path),)).fetchone()
    return dict(row) if row else None


def insert_segment(conn: sqlite3.Connection, video_id: str, segment) -> None:
    storage_id = _storage_id(conn, video_id) or video_id
    words_json = json.dumps(
        [{"word": w.word, "start": w.start, "end": w.end} for w in segment.words],
        ensure_ascii=False,
    )
    conn.execute(
        "INSERT INTO asr_segments (video_id, start_sec, end_sec, text, words_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (storage_id, segment.start, segment.end, segment.text, words_json),
    )


def insert_chunk(conn: sqlite3.Connection, video_id: str, chunk) -> int:
    storage_id = _storage_id(conn, video_id) or video_id
    revision_row = conn.execute(
        "SELECT a.transcript_revision FROM active_transcripts a JOIN videos v "
        "ON v.public_video_id = a.public_video_id WHERE v.video_id = ?", (storage_id,)
    ).fetchone()
    revision = str(revision_row[0]) if revision_row else None
    cur = conn.execute(
        "INSERT INTO text_chunks (video_id, start_sec, end_sec, text, transcript_revision) "
        "VALUES (?, ?, ?, ?, ?)",
        (storage_id, chunk.start, chunk.end, chunk.text, revision),
    )
    return int(cur.lastrowid)


def get_chunks_by_ids(conn: sqlite3.Connection, chunk_ids: Iterable[int]) -> dict[int, dict]:
    chunk_ids = list(chunk_ids)
    if not chunk_ids:
        return {}
    placeholders = ",".join("?" for _ in chunk_ids)
    rows = conn.execute(
        f"SELECT * FROM text_chunks WHERE chunk_id IN ({placeholders})", chunk_ids
    ).fetchall()
    return {row["chunk_id"]: dict(row) for row in rows}


def mark_asr_complete(
    conn: sqlite3.Connection, video_id: str, *, commit: bool = True
) -> str:
    storage_id = _storage_id(conn, video_id) or video_id
    video = conn.execute(
        "SELECT public_video_id, source_generation FROM videos WHERE video_id = ?", (storage_id,)
    ).fetchone()
    if not video:
        raise ValueError("video does not exist")
    revision = f"tr_{uuid.uuid4().hex}"
    conn.execute("UPDATE videos SET asr_complete = 1 WHERE video_id = ?", (storage_id,))
    conn.execute(
        "INSERT INTO transcript_revisions(transcript_revision, source_generation, status, asr_config_json) "
        "VALUES(?, ?, 'TEXT_READY', ?)",
        (revision, video[1], json.dumps({"model": "unknown", "language": "unknown"})),
    )
    conn.execute(
        "UPDATE asr_segments SET transcript_revision = ? WHERE video_id = ? AND transcript_revision IS NULL",
        (revision, storage_id),
    )
    conn.execute(
        "INSERT INTO active_transcripts(public_video_id, source_generation, transcript_revision) "
        "VALUES(?, ?, ?) ON CONFLICT(public_video_id) DO UPDATE SET "
        "source_generation=excluded.source_generation, transcript_revision=excluded.transcript_revision",
        (video[0], video[1], revision),
    )
    if commit:
        conn.commit()
    return revision


def is_asr_complete(conn: sqlite3.Connection, video_id: str) -> bool:
    storage_id = _storage_id(conn, video_id) or video_id
    row = conn.execute("SELECT asr_complete FROM videos WHERE video_id = ?", (storage_id,)).fetchone()
    return bool(row and row["asr_complete"])


def get_last_segment_end(conn: sqlite3.Connection, video_id: str) -> float:
    storage_id = _storage_id(conn, video_id) or video_id
    row = conn.execute(
        "SELECT MAX(end_sec) AS m FROM asr_segments WHERE video_id = ?", (storage_id,)
    ).fetchone()
    return float(row["m"]) if row and row["m"] is not None else 0.0


def get_segments(conn: sqlite3.Connection, video_id: str) -> list[dict]:
    storage_id = _storage_id(conn, video_id) or video_id
    rows = conn.execute(
        "SELECT * FROM asr_segments WHERE video_id = ? ORDER BY start_sec", (storage_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_indexed_video_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT v.public_video_id FROM text_chunks c "
        "JOIN videos v ON v.video_id = c.video_id"
    ).fetchall()
    return {str(r[0]) for r in rows}


def get_chunk_ids(conn: sqlite3.Connection, video_id: str) -> list[int]:
    storage_id = _storage_id(conn, video_id) or video_id
    rows = conn.execute("SELECT chunk_id FROM text_chunks WHERE video_id = ?", (storage_id,)).fetchall()
    return [int(r["chunk_id"]) for r in rows]


def delete_video(conn: sqlite3.Connection, video_id: str) -> None:
    storage_id = _storage_id(conn, video_id)
    if storage_id is None:
        return
    public_id = public_video_id(conn, storage_id)
    conn.execute("DELETE FROM text_chunks WHERE video_id = ?", (storage_id,))
    conn.execute("DELETE FROM asr_segments WHERE video_id = ?", (storage_id,))
    conn.execute("DELETE FROM legacy_video_aliases WHERE public_video_id = ?", (public_id,))
    conn.execute("DELETE FROM active_transcripts WHERE public_video_id = ?", (public_id,))
    conn.execute("DELETE FROM sources WHERE public_video_id = ?", (public_id,))
    conn.execute("DELETE FROM videos WHERE video_id = ?", (storage_id,))
    conn.commit()
