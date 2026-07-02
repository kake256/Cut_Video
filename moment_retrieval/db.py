import json
import sqlite3
from typing import Iterable, Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    duration REAL,
    created_at TEXT DEFAULT (datetime('now'))
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
"""


def get_conn() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def insert_video(conn: sqlite3.Connection, video_id: str, path: str, duration: float) -> None:
    conn.execute(
        "INSERT INTO videos (video_id, path, duration) VALUES (?, ?, ?)",
        (video_id, path, duration),
    )


def get_video(conn: sqlite3.Connection, video_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    return dict(row) if row else None


def list_videos(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM videos ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def insert_segment(conn: sqlite3.Connection, video_id: str, segment) -> None:
    words_json = json.dumps(
        [{"word": w.word, "start": w.start, "end": w.end} for w in segment.words],
        ensure_ascii=False,
    )
    conn.execute(
        "INSERT INTO asr_segments (video_id, start_sec, end_sec, text, words_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (video_id, segment.start, segment.end, segment.text, words_json),
    )


def insert_chunk(conn: sqlite3.Connection, video_id: str, chunk) -> int:
    cur = conn.execute(
        "INSERT INTO text_chunks (video_id, start_sec, end_sec, text) VALUES (?, ?, ?, ?)",
        (video_id, chunk.start, chunk.end, chunk.text),
    )
    return cur.lastrowid


def get_chunks_by_ids(conn: sqlite3.Connection, chunk_ids: Iterable[int]) -> dict[int, dict]:
    chunk_ids = list(chunk_ids)
    if not chunk_ids:
        return {}
    placeholders = ",".join("?" for _ in chunk_ids)
    rows = conn.execute(
        f"SELECT * FROM text_chunks WHERE chunk_id IN ({placeholders})",
        chunk_ids,
    ).fetchall()
    return {row["chunk_id"]: dict(row) for row in rows}


def get_segments(conn: sqlite3.Connection, video_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM asr_segments WHERE video_id = ? ORDER BY start_sec",
        (video_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_chunk_ids(conn: sqlite3.Connection, video_id: str) -> list[int]:
    rows = conn.execute(
        "SELECT chunk_id FROM text_chunks WHERE video_id = ?", (video_id,)
    ).fetchall()
    return [r["chunk_id"] for r in rows]


def delete_video(conn: sqlite3.Connection, video_id: str) -> None:
    conn.execute("DELETE FROM text_chunks WHERE video_id = ?", (video_id,))
    conn.execute("DELETE FROM asr_segments WHERE video_id = ?", (video_id,))
    conn.execute("DELETE FROM videos WHERE video_id = ?", (video_id,))
    conn.commit()
