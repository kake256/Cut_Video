"""Post a stalled Short again, spaced out, with the old video made private (never deleted).

Most clips stuck at a handful of views were never shown by YouTube because they
went up in one large batch, so a later, spaced-out second post gets a fresh trial.
The user picks the clips from a list; each one then waits in a queue and goes up
alone, at least ``interval_hours`` after the previous one:

1. The clip is linked (or copied) into ``reposts/`` beside it with its metadata,
   so the upload's own duplicate guards (receipt, folder log) see a new file.
2. If the old title is still among the channel's newest uploads, the upload's
   same-title guard would take the old video for this one, so the item waits.
3. The copy is uploaded privately, the old video is made private, and the copy is
   published.  If publishing fails, the old video is made public again.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

from . import channel_performance, config, made_clips, youtube_upload

DEFAULT_MAX_VIEWS = 30
DEFAULT_MIN_AGE_DAYS = 3
DEFAULT_INTERVAL_HOURS = 3.0
TITLE_BUSY_RETRY_SEC = 6 * 3600
WORKER_POLL_SEC = 300
STATE_LABELS = {"queued": "待ち", "running": "投稿中", "done": "再投稿済み", "failed": "失敗", "cancelled": "取り消し"}

_lock = threading.Lock()


def queue_path() -> Path:
    return config.CACHE_ROOT / "repost_queue.json"


def load_queue() -> list[dict]:
    try:
        items = json.loads(queue_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _save_queue(items: list[dict]) -> None:
    target = queue_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, target)


def candidates(*, max_views: int = DEFAULT_MAX_VIEWS, min_age_days: float = DEFAULT_MIN_AGE_DAYS,
               data: dict | None = None, clips: list | None = None, now: float | None = None) -> list[dict]:
    """Public Shorts old enough and still under ``max_views``, whose clip file is still here."""
    now = time.time() if now is None else now
    data = channel_performance.load(now=now) if data is None else data
    by_video = {str(clip.receipt["video_id"]): clip
                for clip in (made_clips.list_clips() if clips is None else clips) if clip.receipt}
    taken = {item["video_id"] for item in load_queue() if item.get("state") != "cancelled"}
    result = []
    for video in (data or {}).get("videos") or []:
        clip = by_video.get(video["id"])
        if (clip is None or video["id"] in taken or video.get("privacy") != "public"
                or not 0 < video.get("seconds", 0) <= channel_performance.MAX_SHORT_SEC
                or video.get("views", 0) >= max_views
                or now - video.get("published_at", now) < min_age_days * 86400):
            continue
        result.append({"video_id": video["id"], "title": video["title"], "views": video["views"],
                       "published_at": video["published_at"], "path": str(clip.path)})
    result.sort(key=lambda item: item["published_at"])
    return result


def enqueue(picked: list[dict], *, interval_hours: float = DEFAULT_INTERVAL_HOURS,
            now: float | None = None) -> list[dict]:
    """Queue the picked candidates, each one ``interval_hours`` after the last queued one."""
    now = time.time() if now is None else now
    with _lock:
        items = load_queue()
        taken = {item["video_id"] for item in items if item.get("state") != "cancelled"}
        pending = [float(item["not_before"]) for item in items if item.get("state") in {"queued", "running"}]
        slot = max([now] + [at + interval_hours * 3600 for at in pending])
        added = []
        for candidate in picked:
            if candidate["video_id"] in taken:
                continue
            item = {"video_id": candidate["video_id"], "title": candidate["title"], "path": candidate["path"],
                    "views": candidate.get("views", 0), "state": "queued", "not_before": slot,
                    "interval_hours": interval_hours, "queued_at": now, "message": ""}
            items.append(item)
            added.append(item)
            taken.add(candidate["video_id"])
            slot += interval_hours * 3600
        _save_queue(items)
    return added


def cancel_waiting() -> int:
    with _lock:
        items = load_queue()
        count = 0
        for item in items:
            if item.get("state") == "queued":
                item.update(state="cancelled", message="取り消しました")
                count += 1
        _save_queue(items)
    return count


def repost_copy(path: Path) -> Path:
    """The clip under ``reposts/``, hard-linked when possible, with its posting metadata."""
    target = path.parent / "reposts" / path.name
    target.parent.mkdir(exist_ok=True)
    if not target.exists():
        try:
            os.link(path, target)
        except OSError:
            shutil.copy2(path, target)
    sidecar = path.with_suffix(".metadata.json")
    if sidecar.is_file():
        shutil.copy2(sidecar, target.with_suffix(".metadata.json"))
    return target


def _update(video_id: str, **fields) -> None:
    with _lock:
        items = load_queue()
        for item in items:
            if item["video_id"] == video_id and item.get("state") != "cancelled":
                item.update(fields)
        _save_queue(items)


def run_due(*, now: float | None = None, service_factory: Callable | None = None,
            log: Callable[[str], None] = lambda message: None) -> dict | None:
    """Repost the next due item, at most one per call so the spacing holds; returns it."""
    now = time.time() if now is None else now
    with _lock:
        items = load_queue()
        due = next((item for item in sorted(items, key=lambda item: item["not_before"])
                    if item.get("state") == "queued" and item["not_before"] <= now), None)
        if due is None:
            return None
        if any(item.get("state") == "running" for item in items):
            return None
        # Slots that passed while the app was closed must not go up back to back.
        last = max([float(item.get("finished_at") or 0) for item in items
                    if item.get("state") in {"done", "failed"}] + [0.0])
        if now - last < float(due.get("interval_hours") or DEFAULT_INTERVAL_HOURS) * 3600:
            return None
        due["state"] = "running"
        _save_queue(items)
    old = due["video_id"]
    try:
        copy = repost_copy(Path(due["path"]))
        title = youtube_upload.metadata_for(copy).title
        service = youtube_upload._service(service_factory, interactive=False)
        if not youtube_upload.receipt_path(copy).is_file() and youtube_upload._existing_upload(service, title):
            _update(old, state="queued", not_before=now + TITLE_BUSY_RETRY_SEC,
                    message="同じタイトルの動画がまだ新しい投稿の中にあるため後回しにしました")
            return {**due, "state": "queued"}
        receipt = None
        for message, result in youtube_upload.upload_private(
                copy, privacy="private", service_factory=lambda: service, interactive=False):
            if message:
                log(message)
            receipt = result or receipt
        if not receipt:
            raise youtube_upload.UploadError("アップロードの結果が返りませんでした。")
        youtube_upload.set_privacy(old, "private", service_factory=lambda: service)
        try:
            youtube_upload.set_privacy(receipt["video_id"], "public", service_factory=lambda: service)
        except Exception:
            youtube_upload.set_privacy(old, "public", service_factory=lambda: service)
            raise
    except Exception as exc:
        message = str(exc) if isinstance(exc, youtube_upload.UploadError) else type(exc).__name__
        _update(old, state="failed", message=message, finished_at=time.time())
        log(f"再投稿に失敗しました: {due['title']}（{message}）")
        return {**due, "state": "failed", "message": message}
    _update(old, state="done", new_video_id=receipt["video_id"], finished_at=time.time(),
            message=f"https://www.youtube.com/watch?v={receipt['video_id']}")
    log(f"再投稿しました: {due['title']} → https://www.youtube.com/watch?v={receipt['video_id']}")
    return {**due, "state": "done", "new_video_id": receipt["video_id"]}


def recover_interrupted() -> None:
    """An item left "running" by a closed app goes back to waiting; the upload resumes from its session."""
    with _lock:
        items = load_queue()
        for item in items:
            if item.get("state") == "running":
                item["state"] = "queued"
        _save_queue(items)


def start_worker(*, poll_sec: float = WORKER_POLL_SEC) -> threading.Thread:
    """Repost due items in the background while the app is open."""
    recover_interrupted()

    def loop():
        while True:
            try:
                run_due()
            except Exception:
                pass
            time.sleep(poll_sec)

    thread = threading.Thread(target=loop, name="repost-worker", daemon=True)
    thread.start()
    return thread
