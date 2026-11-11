"""Source ranges already used for posted clips, so new runs pick different scenes.

A range is recorded when a clip is uploaded (from the posting-metadata JSON
written next to the mp4) or imported from a share zip.  The AI is told to
avoid these ranges, and proposals that still overlap them are rejected.
When the shared ledger is set up (``shared_ledger``), ranges other people
exported or uploaded from the same public video are avoided the same way.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import db, shared_ledger

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


def combined(conn, video_id: str) -> list[dict]:
    """This PC's used ranges plus the ranges other people put in the shared ledger."""
    used = db.list_used_clip_ranges(conn, video_id)
    if shared_ledger.enabled():
        url = shared_ledger.origin_for_video(conn, video_id)
        if url:
            used = sorted([*used, *shared_ledger.ranges(url)], key=lambda item: float(item["start_sec"]))
    return used


def for_video(video_id: str) -> list[dict]:
    conn = db.get_conn()
    try:
        db.init_db(conn)
        return combined(conn, video_id)
    finally:
        conn.close()


def _sidecar_range(clip_path: Path) -> tuple[dict, str, float, float] | None:
    sidecar = Path(clip_path).with_suffix(".metadata.json")
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        source = data["source_range"]
        video_id = str(source["video_id"])
        start, end = float(source["start_sec"]), float(source["end_sec"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not video_id:
        return None
    return data, video_id, start, end


def _to_ledger(conn, kind: str, video_id: str, start: float, end: float, title: str) -> bool:
    if not shared_ledger.enabled():
        return False
    url = shared_ledger.origin_for_video(conn, video_id)
    return bool(url) and shared_ledger.record(kind, url, start, end, title)


def record_from_clip(clip_path: Path, title: str = "") -> bool:
    """Record the source range stored in a clip's posting-metadata JSON after upload."""
    found = _sidecar_range(clip_path)
    if found is None:
        return False
    data, video_id, start, end = found
    title = title or str(data.get("title") or "")
    conn = db.get_conn()
    try:
        db.init_db(conn)
        added = db.add_used_clip_range(conn, video_id, start, end, title)
        _to_ledger(conn, "uploaded", video_id, start, end, title)
        return added
    finally:
        conn.close()


def record_made(clip_path: Path) -> bool:
    """Tell the shared ledger that this clip was exported (this PC can still pick it again)."""
    found = _sidecar_range(clip_path)
    if found is None or not shared_ledger.enabled():
        return False
    data, video_id, start, end = found
    conn = db.get_conn()
    try:
        db.init_db(conn)
        return _to_ledger(conn, "made", video_id, start, end, str(data.get("title") or ""))
    finally:
        conn.close()


def backfill_from_jobs(jobs: list) -> int:
    """Record clips uploaded before ranges were tracked, using each job's highlight run.

    Matches an upload to its candidate by title; safe to run repeatedly (duplicates are ignored).
    """
    added = 0
    conn = db.get_conn()
    try:
        db.init_db(conn)
        for job in jobs:
            if not (job.uploads and job.video_id and job.highlight_run_id):
                continue
            by_title = {str(c["title"]): c for c in db.get_highlight_candidates(conn, job.highlight_run_id)}
            for upload in job.uploads:
                candidate = by_title.get(str(upload.get("title") or ""))
                if candidate and db.add_used_clip_range(
                    conn, job.video_id, candidate["start_sec"], candidate["end_sec"], candidate["title"],
                ):
                    added += 1
    finally:
        conn.close()
    return added


def clash_reason(item: dict) -> str:
    """Why a proposal was rejected, for the AI's feedback."""
    title = item.get("title") or "無題"
    if item.get("origin") == "ledger":
        return f"{item.get('by')}さんが切り抜いた範囲（{title}）と重なっています"
    return f"投稿済みの範囲（{title}）と重なっています"


def prompt_lines(used: list[dict]) -> str:
    """Human-readable list for AI prompts (times only plus the clip title)."""
    def clock(seconds: float) -> str:
        seconds = int(seconds)
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"

    return "\n".join(
        f"- {clock(item['start_sec'])}〜{clock(item['end_sec'])}「{item.get('title') or ''}」"
        + (f"（{item.get('by')}さんが作成）" if item.get("origin") == "ledger" else "")
        for item in used
    )


def send_past_uploads_to_ledger() -> dict:
    """Send the ranges this PC already uploaded (before the ledger was set up) to the ledger.

    Only this PC's own uploads (origin 'upload') are sent, not ranges imported from other
    people's share files; videos without a public origin URL cannot be matched and are counted."""
    if not shared_ledger.enabled():
        return {"sent": 0, "no_url": 0, "delivered": False, "enabled": False}
    conn = db.get_conn()
    try:
        db.init_db(conn)
        rows = conn.execute(
            "SELECT public_video_id, start_sec, end_sec, title FROM used_clip_ranges "
            "WHERE origin = 'upload' ORDER BY public_video_id, start_sec"
        ).fetchall()
        urls: dict[str, str | None] = {}
        items, no_url = [], 0
        for public_id, start, end, title in rows:
            if public_id not in urls:
                found = conn.execute(
                    "SELECT video_id FROM videos WHERE public_video_id = ? OR video_id = ? LIMIT 1",
                    (public_id, public_id),
                ).fetchone()
                urls[public_id] = shared_ledger.origin_for_video(conn, found[0]) if found else None
            if not urls[public_id]:
                no_url += 1
                continue
            items.append(("uploaded", urls[public_id], start, end, title or ""))
    finally:
        conn.close()
    sent, delivered = shared_ledger.record_many(items)
    return {"sent": sent, "no_url": no_url, "delivered": delivered, "enabled": True}
