"""Source ranges already used for posted clips, so new runs pick different scenes.

A range is recorded when a clip is uploaded (from the posting-metadata JSON
written next to the mp4) or imported from a share zip.  The AI is told to
avoid these ranges, and proposals that still overlap them are rejected.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import db

# Same rule as candidate overlap suppression: overlapping 30% of the shorter clip.
OVERLAP_THRESHOLD = 0.30


def overlaps(start: float, end: float, used: list[dict], threshold: float = OVERLAP_THRESHOLD) -> dict | None:
    """Return the used range that a candidate overlaps too much, if any."""
    for item in used:
        shared = min(end, float(item["end_sec"])) - max(start, float(item["start_sec"]))
        shorter = min(end - start, float(item["end_sec"]) - float(item["start_sec"]))
        if shorter > 0 and shared > 0 and shared / shorter >= threshold:
            return item
    return None


def for_video(video_id: str) -> list[dict]:
    conn = db.get_conn()
    try:
        db.init_db(conn)
        return db.list_used_clip_ranges(conn, video_id)
    finally:
        conn.close()


def record_from_clip(clip_path: Path, title: str = "") -> bool:
    """Record the source range stored in a clip's posting-metadata JSON after upload."""
    sidecar = Path(clip_path).with_suffix(".metadata.json")
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        source = data["source_range"]
        video_id = str(source["video_id"])
        start, end = float(source["start_sec"]), float(source["end_sec"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if not video_id:
        return False
    conn = db.get_conn()
    try:
        db.init_db(conn)
        return db.add_used_clip_range(conn, video_id, start, end, title or str(data.get("title") or ""))
    finally:
        conn.close()


def prompt_lines(used: list[dict]) -> str:
    """Human-readable list for AI prompts (times only plus the clip title)."""
    def clock(seconds: float) -> str:
        seconds = int(seconds)
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"

    return "\n".join(
        f"- {clock(item['start_sec'])}〜{clock(item['end_sec'])}「{item.get('title') or ''}」" for item in used
    )
