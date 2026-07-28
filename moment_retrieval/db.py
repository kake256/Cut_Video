from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Iterable, Optional

from . import config

SCHEMA_VERSION = 6
PUBLIC_ID_PREFIX = "vid_"

_JOURNAL_MODES = {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}
_SYNCHRONOUS_MODES = {"OFF", "NORMAL", "FULL", "EXTRA"}

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

-- LLM outputs are derived data.  The immutable ASR rows above remain the
-- canonical source for timing and search, even when an analysis is rerun.
CREATE TABLE IF NOT EXISTS analysis_runs (
    analysis_run_id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES videos(video_id),
    transcript_revision TEXT NOT NULL REFERENCES transcript_revisions(transcript_revision),
    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'ready', 'failed')),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    summary TEXT,
    tags_json TEXT,
    result_json TEXT,
    error_message TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_revision
ON analysis_runs(video_id, transcript_revision, status, created_at DESC);

CREATE TABLE IF NOT EXISTS analysis_chapters (
    chapter_id TEXT PRIMARY KEY,
    analysis_run_id TEXT NOT NULL REFERENCES analysis_runs(analysis_run_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    start_segment_id INTEGER NOT NULL,
    end_segment_id INTEGER NOT NULL,
    start_sec REAL NOT NULL,
    end_sec REAL NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    tags_json TEXT,
    UNIQUE(analysis_run_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_analysis_chapters_run
ON analysis_chapters(analysis_run_id, ordinal);
"""


def new_public_video_id() -> str:
    """Return an opaque stable ID which contains no path or filename material."""
    return f"{PUBLIC_ID_PREFIX}{uuid.uuid4().hex}"


def _fingerprint_available_source(path: str | Path) -> str | None:
    """Return the private local fingerprint without exposing source material.

    The import is deliberately local because ``publication`` also imports this
    module.  Missing or concurrently replaced legacy sources remain unknown and
    are not silently rebound to a generation.
    """
    source = Path(path)
    if not source.is_file():
        return None
    try:
        from .publication import private_source_fingerprint
        return private_source_fingerprint(source)
    except OSError:
        return None


def configure_connection(conn: sqlite3.Connection) -> None:
    """Apply the process-wide SQLite concurrency and integrity policy."""
    busy_timeout = max(0, int(config.SQLITE_BUSY_TIMEOUT_MS))
    journal_mode = str(config.SQLITE_JOURNAL_MODE).upper()
    synchronous = str(config.SQLITE_SYNCHRONOUS).upper()
    if journal_mode not in _JOURNAL_MODES:
        raise ValueError(f"unsupported SQLite journal mode: {journal_mode}")
    if synchronous not in _SYNCHRONOUS_MODES:
        raise ValueError(f"unsupported SQLite synchronous mode: {synchronous}")
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout}")
    conn.execute("PRAGMA foreign_keys = ON")
    if not conn.in_transaction:
        conn.execute(f"PRAGMA journal_mode = {journal_mode}")
        conn.execute(f"PRAGMA synchronous = {synchronous}")


def ensure_writer_lease_schema(conn: sqlite3.Connection) -> None:
    """Create only the coordination table needed before full schema bootstrap."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS job_records ("
        "job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner_token TEXT NOT NULL, "
        "owner_pid INTEGER NOT NULL, state TEXT NOT NULL, heartbeat REAL NOT NULL, "
        "expires_at REAL NOT NULL, payload_json TEXT)"
    )


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
        stored_fingerprint = conn.execute(
            "SELECT private_fingerprint FROM sources "
            "WHERE source_generation = ? AND public_video_id = ?",
            (source_generation, public_id),
        ).fetchone()
        if (
            source_status == "available"
            and stored_fingerprint is not None
            and not stored_fingerprint[0]
        ):
            fingerprint = _fingerprint_available_source(row[2])
            if fingerprint is not None:
                conn.execute(
                    "UPDATE sources SET private_fingerprint = COALESCE(private_fingerprint, ?) "
                    "WHERE source_generation = ? AND public_video_id = ?",
                    (fingerprint, source_generation, public_id),
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
    configure_connection(conn)
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {current} is newer than supported "
            f"version {SCHEMA_VERSION}"
        )
    if current == SCHEMA_VERSION:
        # Current-schema callers are overwhelmingly read paths.  Avoid taking
        # a write lock and rerunning legacy backfills on every callback.
        return None
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_segments_revision_range "
            "ON asr_segments(video_id, transcript_revision, start_sec)"
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return backup


def get_conn() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        config.DB_PATH,
        timeout=max(0.0, float(config.SQLITE_BUSY_TIMEOUT_MS) / 1000.0),
    )
    conn.row_factory = sqlite3.Row
    configure_connection(conn)
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


def resolve_source_fingerprint(
    conn: sqlite3.Connection,
    public_video_id: str,
    source_generation: str,
    *,
    migrate_legacy: bool = True,
) -> str | None:
    """Resolve the private fingerprint bound to one immutable source generation.

    Legacy rows may have a null fingerprint.  When their recorded locator still
    exists, the first resolution computes and stores the local fingerprint.  A
    missing locator or an unknown generation stays unresolved so preview/save
    callers can block and ask for an explicit relink.  Callers own the database
    transaction and must commit if they want a legacy backfill persisted.
    """
    row = conn.execute(
        "SELECT locator, private_fingerprint, status FROM sources "
        "WHERE public_video_id = ? AND source_generation = ?",
        (public_video_id, source_generation),
    ).fetchone()
    if row is None:
        return None
    fingerprint = row[1]
    if fingerprint:
        return str(fingerprint)
    if not migrate_legacy or str(row[2] or "") != "available" or not row[0]:
        return None
    fingerprint = _fingerprint_available_source(str(row[0]))
    if fingerprint is None:
        return None
    conn.execute(
        "UPDATE sources SET private_fingerprint = ? "
        "WHERE public_video_id = ? AND source_generation = ? "
        "AND private_fingerprint IS NULL",
        (fingerprint, public_video_id, source_generation),
    )
    current = conn.execute(
        "SELECT private_fingerprint FROM sources "
        "WHERE public_video_id = ? AND source_generation = ?",
        (public_video_id, source_generation),
    ).fetchone()
    return str(current[0]) if current and current[0] else None


def insert_video(conn: sqlite3.Connection, video_id: str, path: str, duration: float) -> None:
    public_id = video_id if video_id.startswith(PUBLIC_ID_PREFIX) else new_public_video_id()
    source_generation = f"src_{uuid.uuid4().hex}"
    source_status = "available" if Path(path).exists() else "missing"
    source_fingerprint = (
        _fingerprint_available_source(path) if source_status == "available" else None
    )
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
        "INSERT INTO sources(source_generation, public_video_id, locator, private_fingerprint, status) "
        "VALUES(?, ?, ?, ?, ?)",
        (source_generation, public_id, path, source_fingerprint, source_status),
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


def begin_transcript_revision(
    conn: sqlite3.Connection,
    video_id: str,
    *,
    asr_config: dict | None = None,
    reuse_draft: bool = True,
    commit: bool = True,
) -> str:
    """Create or resume an unpublished transcript revision for one source."""
    storage_id = _storage_id(conn, video_id) or video_id
    video = conn.execute(
        "SELECT public_video_id, source_generation FROM videos WHERE video_id = ?",
        (storage_id,),
    ).fetchone()
    if not video:
        raise ValueError("video does not exist")
    config_json = json.dumps(
        asr_config or {"model": "unknown", "language": "unknown"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if reuse_draft:
        existing = conn.execute(
            "SELECT r.transcript_revision FROM transcript_revisions r "
            "LEFT JOIN active_transcripts a "
            "ON a.transcript_revision = r.transcript_revision "
            "WHERE r.source_generation = ? AND r.asr_config_json = ? "
            "AND r.status IN ('BUILDING', 'TEXT_READY') "
            "AND a.transcript_revision IS NULL ORDER BY r.rowid DESC LIMIT 1",
            (video[1], config_json),
        ).fetchone()
        if existing:
            return str(existing[0])
    revision = f"tr_{uuid.uuid4().hex}"
    conn.execute(
        "INSERT INTO transcript_revisions"
        "(transcript_revision, source_generation, status, asr_config_json) "
        "VALUES(?, ?, 'BUILDING', ?)",
        (revision, video[1], config_json),
    )
    if commit:
        conn.commit()
    return revision


def complete_transcript_revision(
    conn: sqlite3.Connection, transcript_revision: str, *, commit: bool = True
) -> None:
    cursor = conn.execute(
        "UPDATE transcript_revisions SET status = 'TEXT_READY' "
        "WHERE transcript_revision = ? AND status IN ('BUILDING', 'TEXT_READY')",
        (transcript_revision,),
    )
    if cursor.rowcount != 1:
        raise ValueError("transcript revision is not an active draft")
    if commit:
        conn.commit()


def transcript_revision_status(
    conn: sqlite3.Connection, transcript_revision: str
) -> str | None:
    row = conn.execute(
        "SELECT status FROM transcript_revisions WHERE transcript_revision = ?",
        (transcript_revision,),
    ).fetchone()
    return str(row[0]) if row else None


def get_active_transcript_revision(
    conn: sqlite3.Connection, video_id: str
) -> str | None:
    storage_id = _storage_id(conn, video_id) or video_id
    row = conn.execute(
        "SELECT a.transcript_revision FROM active_transcripts a JOIN videos v "
        "ON v.public_video_id = a.public_video_id WHERE v.video_id = ?",
        (storage_id,),
    ).fetchone()
    return str(row[0]) if row else None


def activate_transcript_revision(
    conn: sqlite3.Connection, public_video_id: str, transcript_revision: str
) -> str:
    row = conn.execute(
        "SELECT r.source_generation FROM transcript_revisions r JOIN videos v "
        "ON v.source_generation = r.source_generation "
        "WHERE v.public_video_id = ? AND r.transcript_revision = ?",
        (public_video_id, transcript_revision),
    ).fetchone()
    if not row:
        raise ValueError("transcript revision does not belong to the video source")
    source_generation = str(row[0])
    previous = conn.execute(
        "SELECT transcript_revision FROM active_transcripts WHERE public_video_id = ?",
        (public_video_id,),
    ).fetchone()
    if previous and str(previous[0]) != transcript_revision:
        conn.execute(
            "UPDATE transcript_revisions SET status = 'SUPERSEDED' "
            "WHERE transcript_revision = ?",
            (previous[0],),
        )
    conn.execute(
        "INSERT INTO active_transcripts(public_video_id, source_generation, transcript_revision) "
        "VALUES(?, ?, ?) ON CONFLICT(public_video_id) DO UPDATE SET "
        "source_generation=excluded.source_generation, "
        "transcript_revision=excluded.transcript_revision",
        (public_video_id, source_generation, transcript_revision),
    )
    conn.execute(
        "UPDATE transcript_revisions SET status = 'ACTIVE' WHERE transcript_revision = ?",
        (transcript_revision,),
    )
    conn.execute(
        "UPDATE videos SET asr_complete = 1 WHERE public_video_id = ?",
        (public_video_id,),
    )
    return source_generation


def insert_segment(
    conn: sqlite3.Connection,
    video_id: str,
    segment,
    *,
    transcript_revision: str | None = None,
) -> None:
    storage_id = _storage_id(conn, video_id) or video_id
    words_json = json.dumps(
        [{"word": w.word, "start": w.start, "end": w.end} for w in segment.words],
        ensure_ascii=False,
    )
    conn.execute(
        "INSERT INTO asr_segments "
        "(video_id, start_sec, end_sec, text, words_json, transcript_revision) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            storage_id,
            segment.start,
            segment.end,
            segment.text,
            words_json,
            transcript_revision,
        ),
    )


def insert_chunk(
    conn: sqlite3.Connection,
    video_id: str,
    chunk,
    *,
    transcript_revision: str | None = None,
) -> int:
    storage_id = _storage_id(conn, video_id) or video_id
    revision = transcript_revision
    if revision is None:
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


def get_last_segment_end(
    conn: sqlite3.Connection,
    video_id: str,
    *,
    transcript_revision: str | None = None,
) -> float:
    storage_id = _storage_id(conn, video_id) or video_id
    effective_revision = (
        transcript_revision
        if transcript_revision is not None
        else get_active_transcript_revision(conn, storage_id)
    )
    if effective_revision is None:
        row = conn.execute(
            "SELECT MAX(end_sec) AS m FROM asr_segments WHERE video_id = ?",
            (storage_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT MAX(end_sec) AS m FROM asr_segments "
            "WHERE video_id = ? AND transcript_revision = ?",
            (storage_id, effective_revision),
        ).fetchone()
    return float(row["m"]) if row and row["m"] is not None else 0.0


def get_segments(
    conn: sqlite3.Connection,
    video_id: str,
    *,
    transcript_revision: str | None = None,
) -> list[dict]:
    storage_id = _storage_id(conn, video_id) or video_id
    effective_revision = (
        transcript_revision
        if transcript_revision is not None
        else get_active_transcript_revision(conn, storage_id)
    )
    if effective_revision is None:
        rows = conn.execute(
            "SELECT * FROM asr_segments WHERE video_id = ? ORDER BY start_sec",
            (storage_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM asr_segments WHERE video_id = ? "
            "AND transcript_revision = ? ORDER BY start_sec",
            (storage_id, effective_revision),
        ).fetchall()
    return [dict(r) for r in rows]


ANALYSIS_STATUSES = frozenset({"pending", "running", "ready", "failed"})


def create_analysis_run(
    conn: sqlite3.Connection,
    video_id: str,
    transcript_revision: str,
    *,
    provider: str,
    model: str,
    prompt_version: str,
    commit: bool = True,
) -> str:
    """Create a new, rerunnable derived LLM analysis record.

    This function never mutates ``asr_segments`` or ``transcript_revisions``.
    ``video_id`` accepts either the public or storage identifier, but persists
    the storage identifier so the foreign-key relationship remains valid.
    """
    storage_id = _storage_id(conn, video_id) or video_id
    valid = conn.execute(
        "SELECT 1 FROM transcript_revisions r JOIN videos v "
        "ON v.source_generation = r.source_generation "
        "WHERE v.video_id = ? AND r.transcript_revision = ? LIMIT 1",
        (storage_id, transcript_revision),
    ).fetchone()
    if valid is None:
        raise ValueError("transcript revision does not belong to this video")
    run_id = f"analysis_{uuid.uuid4().hex}"
    conn.execute(
        "INSERT INTO analysis_runs(analysis_run_id, video_id, transcript_revision, status, "
        "provider, model, prompt_version) VALUES(?, ?, ?, 'pending', ?, ?, ?)",
        (run_id, storage_id, transcript_revision, provider, model, prompt_version),
    )
    if commit:
        conn.commit()
    return run_id


def update_analysis_run(
    conn: sqlite3.Connection,
    analysis_run_id: str,
    *,
    status: str,
    summary: str | None = None,
    tags: list[str] | None = None,
    result: dict | None = None,
    error_message: str | None = None,
    commit: bool = True,
) -> None:
    """Transition an analysis run and persist only its derived result fields."""
    if status not in ANALYSIS_STATUSES:
        raise ValueError(f"invalid analysis status: {status}")
    cursor = conn.execute(
        "UPDATE analysis_runs SET status = ?, summary = ?, tags_json = ?, result_json = ?, "
        "error_message = ?, updated_at = datetime('now') WHERE analysis_run_id = ?",
        (
            status,
            summary,
            json.dumps(tags, ensure_ascii=False) if tags is not None else None,
            json.dumps(result, ensure_ascii=False, sort_keys=True) if result is not None else None,
            error_message,
            analysis_run_id,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("analysis run does not exist")
    if commit:
        conn.commit()


def replace_analysis_chapters(
    conn: sqlite3.Connection,
    analysis_run_id: str,
    chapters: Iterable[dict],
    *,
    commit: bool = True,
) -> None:
    """Replace chapters for one run; callers supply already validated timings."""
    conn.execute("DELETE FROM analysis_chapters WHERE analysis_run_id = ?", (analysis_run_id,))
    rows = []
    for ordinal, chapter in enumerate(chapters):
        rows.append((
            f"chapter_{uuid.uuid4().hex}", analysis_run_id, ordinal,
            int(chapter["start_segment_id"]), int(chapter["end_segment_id"]),
            float(chapter["start_sec"]), float(chapter["end_sec"]),
            str(chapter["title"]), chapter.get("summary"),
            json.dumps(chapter.get("tags", []), ensure_ascii=False),
        ))
    conn.executemany(
        "INSERT INTO analysis_chapters(chapter_id, analysis_run_id, ordinal, start_segment_id, "
        "end_segment_id, start_sec, end_sec, title, summary, tags_json) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows,
    )
    if commit:
        conn.commit()


def get_analysis_run(conn: sqlite3.Connection, analysis_run_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM analysis_runs WHERE analysis_run_id = ?", (analysis_run_id,)
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    for field in ("tags_json", "result_json"):
        if result.get(field):
            result[field[:-5]] = json.loads(result[field])
    return result


def get_analysis_chapters(conn: sqlite3.Connection, analysis_run_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM analysis_chapters WHERE analysis_run_id = ? ORDER BY ordinal",
        (analysis_run_id,),
    ).fetchall()
    chapters = []
    for row in rows:
        result = dict(row)
        result["tags"] = json.loads(result.pop("tags_json") or "[]")
        chapters.append(result)
    return chapters


def list_analysis_runs(
    conn: sqlite3.Connection, video_id: str, transcript_revision: str
) -> list[dict]:
    storage_id = _storage_id(conn, video_id) or video_id
    rows = conn.execute(
        "SELECT * FROM analysis_runs WHERE video_id = ? AND transcript_revision = ? "
        "ORDER BY created_at DESC, rowid DESC",
        (storage_id, transcript_revision),
    ).fetchall()
    return [get_analysis_run(conn, str(row["analysis_run_id"])) for row in rows]


def get_segments_in_range(
    conn: sqlite3.Connection,
    video_id: str,
    start_sec: float,
    end_sec: float,
    *,
    transcript_revision: str | None = None,
) -> list[dict]:
    """Return only the active transcript rows overlapping one source range."""
    storage_id = _storage_id(conn, video_id) or video_id
    effective_revision = (
        transcript_revision
        if transcript_revision is not None
        else get_active_transcript_revision(conn, storage_id)
    )
    params: tuple = (storage_id, float(start_sec), float(end_sec))
    revision_clause = ""
    if effective_revision is not None:
        revision_clause = " AND transcript_revision = ?"
        params = (*params, effective_revision)
    rows = conn.execute(
        "SELECT segment_id, video_id, start_sec, end_sec, text, words_json, "
        "transcript_revision FROM asr_segments "
        "WHERE video_id = ? AND end_sec > ? AND start_sec < ?"
        f"{revision_clause} ORDER BY start_sec, segment_id",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def get_first_text_segment(
    conn: sqlite3.Connection,
    video_id: str,
    *,
    transcript_revision: str | None = None,
) -> dict | None:
    """Return the first non-empty row from the active transcript revision."""
    storage_id = _storage_id(conn, video_id) or video_id
    effective_revision = (
        transcript_revision
        if transcript_revision is not None
        else get_active_transcript_revision(conn, storage_id)
    )
    params: tuple = (storage_id,)
    revision_clause = ""
    if effective_revision is not None:
        revision_clause = " AND transcript_revision = ?"
        params = (*params, effective_revision)
    row = conn.execute(
        "SELECT segment_id, video_id, start_sec, end_sec, text, words_json, "
        "transcript_revision FROM asr_segments WHERE video_id = ? "
        "AND trim(COALESCE(text, '')) <> ''"
        f"{revision_clause} ORDER BY start_sec, segment_id LIMIT 1",
        params,
    ).fetchone()
    return dict(row) if row else None


def get_indexed_video_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT v.public_video_id FROM text_chunks c "
        "JOIN videos v ON v.video_id = c.video_id "
        "JOIN active_transcripts a ON a.public_video_id = v.public_video_id "
        "AND a.transcript_revision = c.transcript_revision"
    ).fetchall()
    return {str(r[0]) for r in rows}


def get_chunk_ids(
    conn: sqlite3.Connection,
    video_id: str,
    *,
    transcript_revision: str | None = None,
) -> list[int]:
    storage_id = _storage_id(conn, video_id) or video_id
    effective_revision = (
        transcript_revision
        if transcript_revision is not None
        else get_active_transcript_revision(conn, storage_id)
    )
    if effective_revision is None:
        rows = conn.execute(
            "SELECT chunk_id FROM text_chunks WHERE video_id = ?", (storage_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT chunk_id FROM text_chunks WHERE video_id = ? "
            "AND transcript_revision = ?",
            (storage_id, effective_revision),
        ).fetchall()
    return [int(r["chunk_id"]) for r in rows]


def delete_chunks_for_revision(
    conn: sqlite3.Connection, video_id: str, transcript_revision: str
) -> None:
    storage_id = _storage_id(conn, video_id) or video_id
    conn.execute(
        "DELETE FROM text_chunks WHERE video_id = ? AND transcript_revision = ?",
        (storage_id, transcript_revision),
    )


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
