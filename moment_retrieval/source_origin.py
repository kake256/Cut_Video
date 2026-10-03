"""Public YouTube origin of a source video, used to share and auto-relink.

Only canonical ``https://www.youtube.com/watch?v=<id>`` URLs are ever stored
or shared, so a package can never smuggle an arbitrary URL or local path.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .youtube_captions import CaptionError, normalize_youtube_url

# Files saved by downloader.download_video: YYYYMMDD[_HHMMSS]_<YouTube ID>.
_DOWNLOAD_NAME = re.compile(r"^\d{8}(?:_\d{6})?_([A-Za-z0-9_-]{11})$")


def canonical_youtube_url(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        return normalize_youtube_url(url)
    except CaptionError:
        return None


def _path_key(path: str | Path) -> str:
    return str(Path(path).resolve())


def record_download(conn: sqlite3.Connection, path: str | Path, url: str) -> str | None:
    """Remember where a downloaded file came from; non-YouTube URLs are ignored."""
    canonical = canonical_youtube_url(url)
    if canonical is None:
        return None
    conn.execute(
        "INSERT INTO downloaded_sources(path, origin_url) VALUES(?, ?) "
        "ON CONFLICT(path) DO UPDATE SET origin_url = excluded.origin_url",
        (_path_key(path), canonical),
    )
    conn.commit()
    return canonical


def origin_url_for_video(conn: sqlite3.Connection, video: dict) -> str | None:
    """Return the canonical origin URL known for a library video, if any."""
    public_id = video.get("public_video_id")
    if public_id:
        row = conn.execute(
            "SELECT origin_url FROM shared_source_origins WHERE public_video_id = ?",
            (public_id,),
        ).fetchone()
        if row:
            return canonical_youtube_url(row[0])
    path = video.get("path")
    if not path:
        return None
    row = conn.execute(
        "SELECT origin_url FROM downloaded_sources WHERE path = ?", (_path_key(path),)
    ).fetchone()
    if row:
        return canonical_youtube_url(row[0])
    # Files downloaded before origins were recorded still carry the ID in their name.
    match = _DOWNLOAD_NAME.fullmatch(Path(path).stem)
    if match:
        return canonical_youtube_url(f"https://www.youtube.com/watch?v={match.group(1)}")
    return None


def remember_shared_origin(
    conn: sqlite3.Connection, public_video_id: str, url: str, *, commit: bool = True
) -> str | None:
    canonical = canonical_youtube_url(url)
    if canonical is None:
        return None
    conn.execute(
        "INSERT INTO shared_source_origins(public_video_id, origin_url) VALUES(?, ?) "
        "ON CONFLICT(public_video_id) DO UPDATE SET origin_url = excluded.origin_url",
        (public_video_id, canonical),
    )
    if commit:
        conn.commit()
    return canonical


def unlinked_videos_for_origin(conn: sqlite3.Connection, url: str) -> list[str]:
    """Imported videos with this origin that still wait for a local source."""
    canonical = canonical_youtube_url(url)
    if canonical is None:
        return []
    rows = conn.execute(
        "SELECT v.public_video_id FROM shared_source_origins o "
        "JOIN videos v ON v.public_video_id = o.public_video_id "
        "WHERE o.origin_url = ? AND v.source_state != 'available' ORDER BY v.created_at",
        (canonical,),
    ).fetchall()
    return [str(row[0]) for row in rows]
