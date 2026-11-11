"""Publish uploaded clips one at a time, spaced out, instead of all at once.

Clips made public together in one batch tend to stay at a handful of views, so
the publish buttons put the checked clips in a queue and a background worker
publishes them one by one, at least ``interval_hours`` apart:

* The first one is due now (or ``interval_hours`` after the last one published or
  still waiting), each next one ``interval_hours`` later.
* The queue is a JSON file under ``config.CACHE_ROOT`` and only moves while the app
  is open.  On start, an item left "running" goes back to waiting, and items whose
  time passed while the app was closed are spaced out again from now, so at most
  one goes public right away.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable

from . import config, made_clips, youtube_upload

DEFAULT_INTERVAL_HOURS = 3.0
WORKER_POLL_SEC = 60
STATE_LABELS = {"queued": "公開待ち", "running": "公開中", "done": "公開済み", "failed": "失敗", "cancelled": "取り消し"}
PENDING = {"queued", "running"}

_lock = threading.Lock()
_wake = threading.Event()


def queue_path() -> Path:
    return config.CACHE_ROOT / "publish_queue.json"


def _key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


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


def _last_published(items: list[dict]) -> float | None:
    times = [float(item["finished_at"]) for item in items if item.get("state") == "done" and item.get("finished_at")]
    return max(times) if times else None


def _respace(items: list[dict], now: float) -> None:
    """Waiting items keep their order; none earlier than now or than ``interval`` after the one before."""
    previous = _last_published(items)
    if any(item.get("state") == "running" for item in items):
        previous = now  # the running one goes public about now
    for item in sorted((item for item in items if item.get("state") == "queued"),
                       key=lambda item: float(item["not_before"])):
        gap = float(item.get("interval_hours") or DEFAULT_INTERVAL_HOURS) * 3600
        slot = max(float(item["not_before"]), now)
        if previous is not None:
            slot = max(slot, previous + gap)
        item["not_before"] = slot
        previous = slot


def enqueue(paths, *, interval_hours: float = DEFAULT_INTERVAL_HOURS, now: float | None = None,
            clips: list | None = None) -> tuple[list[dict], list[tuple[str, str]]]:
    """Queue uploaded, not yet public clips; returns (added items, [(name, reason) skipped])."""
    now = time.time() if now is None else now
    interval_hours = float(interval_hours) if interval_hours and float(interval_hours) > 0 else DEFAULT_INTERVAL_HOURS
    by_key = {_key(clip.path): clip for clip in (made_clips.list_clips() if clips is None else clips)}
    added, skipped = [], []
    with _lock:
        items = load_queue()
        taken = {_key(item["path"]) for item in items if item.get("state") in PENDING}
        slots = [float(item["not_before"]) for item in items if item.get("state") in PENDING]
        last = _last_published(items)
        if last is not None:
            slots.append(last)
        slot = max([now] + [at + interval_hours * 3600 for at in slots])
        for path in paths or []:
            if not path:
                continue
            key, name = _key(path), Path(str(path)).name
            clip = by_key.get(key)
            if key in taken:
                skipped.append((name, "すでに公開待ちです"))
                continue
            if clip is None:
                skipped.append((name, "作成済みの動画の一覧に見つかりません"))
                continue
            if not clip.receipt:
                skipped.append((clip.title, "まだアップロードしていません"))
                continue
            if clip.receipt.get("privacy_status") == "public":
                skipped.append((clip.title, "すでに公開です"))
                continue
            item = {"path": str(clip.path), "title": clip.title, "video_id": str(clip.receipt["video_id"]),
                    "state": "queued", "not_before": slot, "interval_hours": interval_hours,
                    "queued_at": now, "message": ""}
            items.append(item)
            added.append(item)
            taken.add(key)
            slot += interval_hours * 3600
        if added:
            _save_queue(items)
    if added:
        _wake.set()
    return added, skipped


def cancel_waiting() -> int:
    """Cancel every item still waiting (one being published right now finishes)."""
    with _lock:
        items = load_queue()
        count = 0
        for item in items:
            if item.get("state") == "queued":
                item.update(state="cancelled", message="取り消しました", finished_at=time.time())
                count += 1
        if count:
            _save_queue(items)
    return count


def pending_by_path() -> dict[str, dict]:
    """Waiting/publishing items keyed like ``_key`` (for labels in the clip list)."""
    return {_key(item["path"]): item for item in load_queue() if item.get("state") in PENDING}


def latest_by_path() -> dict[str, dict]:
    """Each clip's queue item to show: the waiting/publishing one, else the last finished one."""
    latest: dict[str, dict] = {}
    for item in load_queue():
        key = _key(item["path"])
        current = latest.get(key)
        if current is not None and current.get("state") in PENDING:
            continue
        if (item.get("state") in PENDING or current is None
                or float(item.get("finished_at") or 0) >= float(current.get("finished_at") or 0)):
            latest[key] = item
    return latest


def run_due(*, now: float | None = None, change_privacy: Callable | None = None,
            on_published: Callable[[dict], None] | None = None,
            log: Callable[[str], None] = lambda message: None) -> dict | None:
    """Publish the next due item, at most one per call; returns it with its new state."""
    now = time.time() if now is None else now
    change_privacy = change_privacy or made_clips.change_privacy
    with _lock:
        items = load_queue()
        if any(item.get("state") == "running" for item in items):
            return None
        due = next((item for item in sorted(items, key=lambda item: float(item["not_before"]))
                    if item.get("state") == "queued" and float(item["not_before"]) <= now), None)
        if due is None:
            return None
        due["state"] = "running"
        due["started_at"] = now
        _save_queue(items)
    try:
        receipt = change_privacy(due["path"], "public")
    except Exception as exc:
        message = str(exc) if isinstance(exc, youtube_upload.UploadError) else f"{type(exc).__name__}: {exc}"[:200]
        _finish(due, state="failed", message=message, finished_at=time.time())
        log(f"公開できませんでした: {due['title']}（{message}）")
        return {**due, "state": "failed", "message": message}
    finished = time.time()
    url = receipt.get("watch_url") or f"https://www.youtube.com/watch?v={receipt.get('video_id', due['video_id'])}"
    _finish(due, state="done", message=url, finished_at=finished)
    if on_published is not None:
        try:
            on_published(receipt)
        except Exception:
            pass
    log(f"公開しました: {due['title']} → {url}")
    return {**due, "state": "done", "message": url, "finished_at": finished}


def _finish(due: dict, **fields) -> None:
    with _lock:
        items = load_queue()
        for item in items:
            if item.get("state") == "running" and _key(item["path"]) == _key(due["path"]):
                item.update(fields)
        if fields.get("state") == "done":
            _respace(items, float(fields["finished_at"]))
        _save_queue(items)


def recover_interrupted(*, now: float | None = None) -> None:
    """On app start: an item left "running" waits again, and overdue items are spaced out from now."""
    now = time.time() if now is None else now
    with _lock:
        items = load_queue()
        if not items:
            return
        for item in items:
            if item.get("state") == "running":
                item["state"] = "queued"
                item["not_before"] = min(float(item["not_before"]), now)
        _respace(items, now)
        _save_queue(items)


def summary(*, now: float | None = None, recent: int = 5) -> dict:
    """Waiting count, next publish time, and the latest finished/failed items (newest first)."""
    now = time.time() if now is None else now
    items = load_queue()
    pending = sorted((item for item in items if item.get("state") in PENDING), key=lambda item: float(item["not_before"]))
    finished = sorted((item for item in items if item.get("state") in {"done", "failed"}),
                      key=lambda item: float(item.get("finished_at") or 0), reverse=True)
    return {"remaining": len(pending), "pending": pending,
            "next_at": float(pending[0]["not_before"]) if pending else None,
            "recent": finished[:recent]}


def start_worker(*, on_published: Callable[[dict], None] | None = None,
                 poll_sec: float = WORKER_POLL_SEC) -> threading.Thread:
    """Publish due items in the background while the app is open."""
    recover_interrupted()

    def loop():
        while True:
            try:
                run_due(on_published=on_published)
            except Exception:
                pass
            _wake.wait(poll_sec)
            _wake.clear()

    thread = threading.Thread(target=loop, name="publish-worker", daemon=True)
    thread.start()
    return thread
