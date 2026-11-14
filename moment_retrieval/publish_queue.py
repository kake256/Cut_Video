"""Publish uploaded clips one at a time, spaced out, instead of all at once.

Clips made public together in one batch tend to stay at a handful of views, so
the publish buttons put the checked clips in a queue and a background worker
publishes them one by one, at least ``interval_hours`` apart:

* The first one is due now (or ``interval_hours`` after the last one published or
  still waiting), each next one ``interval_hours`` later.
* Every item whose time is more than ``MIN_LEAD_SEC`` away is registered on YouTube as
  a publish time (private + ``status.publishAt``, see ``schedule_waiting``), so YouTube
  publishes it even while this app or the PC is off.  Such items are "scheduled".
* Only the one due right away (or one YouTube would not schedule) stays "queued" and
  is published by the app's background worker.  The queue file under
  ``config.CACHE_ROOT`` keeps the plan for spacing, labels and cancelling.  On start,
  an item left "running" goes back to waiting, and queued items whose time passed
  while the app was closed are spaced out again from now.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable

from . import config, made_clips, youtube_upload

DEFAULT_INTERVAL_HOURS = 1.0
WORKER_POLL_SEC = 60
# A publish time closer than this is published by the app right away instead of scheduled.
MIN_LEAD_SEC = 10 * 60
# How long after a scheduled time to wait for YouTube to report the clip public.
SETTLE_GRACE_SEC = 5 * 60
SETTLE_GIVE_UP_SEC = 2 * 3600
STATE_LABELS = {"queued": "公開待ち", "running": "公開中", "scheduled": "公開予約", "done": "公開済み",
                "failed": "失敗", "cancelled": "取り消し"}
PENDING = {"queued", "running", "scheduled"}

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
    """Waiting items keep their order; none earlier than now or than ``interval`` after the one before.

    Items already scheduled on YouTube keep their time; queued ones go after them."""
    previous = _last_published(items)
    scheduled = [float(item["not_before"]) for item in items if item.get("state") == "scheduled"]
    if scheduled:
        previous = max([previous or 0.0] + scheduled)
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
            if made_clips.scheduled_publish_at(clip.receipt) is not None:
                skipped.append((clip.title, "すでにYouTubeで公開予約されています"))
                continue
            item = {"path": str(clip.path), "title": clip.title, "video_id": str(clip.receipt["video_id"]),
                    "state": "queued", "not_before": slot, "interval_hours": interval_hours,
                    "previous_privacy": str(clip.receipt.get("privacy_status") or "private"),
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


def _error_text(exc: Exception) -> str:
    return str(exc) if isinstance(exc, youtube_upload.UploadError) else f"{type(exc).__name__}: {exc}"[:200]


def schedule_waiting(*, now: float | None = None, paths=None, schedule: Callable | None = None,
                     log: Callable[[str], None] = lambda message: None) -> tuple[list[dict], list[dict]]:
    """Register waiting items on YouTube as publish times, so they go public with the app closed.

    Only "queued" items more than ``MIN_LEAD_SEC`` ahead are registered (``paths`` narrows
    it to those clips); the one due right away is left to the app.  An item YouTube will
    not schedule stays queued with the reason.  Returns (scheduled, still queued).
    """
    now = time.time() if now is None else now
    schedule = schedule or made_clips.schedule_publish
    wanted = None if paths is None else {_key(path) for path in paths}
    done, kept = [], []
    with _lock:  # held across the YouTube calls so the worker never publishes one mid-way
        items = load_queue()
        clips = None
        for item in sorted((item for item in items if item.get("state") == "queued"),
                           key=lambda item: float(item["not_before"])):
            if wanted is not None and _key(item["path"]) not in wanted:
                continue
            if float(item["not_before"]) <= now + MIN_LEAD_SEC:
                continue
            previous = item.get("previous_privacy")
            if not previous:  # queued before this was recorded
                if clips is None:
                    clips = {_key(clip.path): clip for clip in made_clips.list_clips()}
                clip = clips.get(_key(item["path"]))
                previous = (clip.receipt or {}).get("privacy_status") if clip and clip.receipt else ""
            try:
                receipt = schedule(item["path"], float(item["not_before"]))
            except Exception as exc:
                item["message"] = f"YouTubeに予約できませんでした（アプリを開いている間に公開します）: {_error_text(exc)}"
                kept.append(item)
                log(f"公開予約できませんでした: {item['title']}（{_error_text(exc)}）")
                continue
            item.update(state="scheduled", publish_at=str((receipt or {}).get("publish_at") or ""),
                        previous_privacy=previous or "private", scheduled_at=now, message="")
            done.append(item)
        if done or kept:
            _save_queue(items)
    return done, kept


def settle_scheduled(*, now: float | None = None, refresh: Callable | None = None,
                     find: Callable | None = None, on_published: Callable[[dict], None] | None = None) -> list[dict]:
    """Mark scheduled items done once YouTube reports them public (checked a little after their time)."""
    now = time.time() if now is None else now
    refresh = refresh or made_clips.refresh_statuses
    find = find or made_clips.find_clip
    due = [item for item in load_queue()
           if item.get("state") == "scheduled" and float(item["not_before"]) + SETTLE_GRACE_SEC <= now]
    if not due:
        return []
    try:
        refresh([item["path"] for item in due])
    except Exception:
        return []  # offline or not connected: try again on the next round
    settled = []
    for item in due:
        try:
            receipt = find(item["path"]).receipt or {}
        except Exception:
            receipt = {}
        if receipt.get("privacy_status") == "public":
            url = f"https://www.youtube.com/watch?v={receipt.get('video_id', item['video_id'])}"
            settled.append((item, {"state": "done", "message": url, "finished_at": float(item["not_before"])}, receipt))
        elif receipt.get("missing_on_youtube"):
            settled.append((item, {"state": "failed", "message": "YouTubeに見つかりません（削除済み？）",
                                   "finished_at": now}, None))
        elif float(item["not_before"]) + SETTLE_GIVE_UP_SEC <= now:
            settled.append((item, {"state": "failed", "message": "予約時刻を過ぎても公開になっていません（Studioで確認してください）",
                                   "finished_at": now}, None))
    if not settled:
        return []
    with _lock:
        items = load_queue()
        for item, fields, _receipt in settled:
            for current in items:
                if current.get("state") == "scheduled" and _key(current["path"]) == _key(item["path"]):
                    current.update(fields)
        _save_queue(items)
    for _item, fields, receipt in settled:
        if receipt is not None and on_published is not None:
            try:
                on_published(receipt)
            except Exception:
                pass
    return [{**item, **fields} for item, fields, _receipt in settled]


def cancel_waiting(*, now: float | None = None, unschedule: Callable | None = None) -> tuple[int, list[tuple[str, str]]]:
    """Cancel every item still waiting (one being published right now finishes).

    Items scheduled on YouTube get their publish time removed (back to the privacy they had
    before); returns (cancelled count, [(title, reason)] that could not be cancelled)."""
    now = time.time() if now is None else now
    unschedule = unschedule or made_clips.change_privacy
    failed = []
    with _lock:
        items = load_queue()
        count = 0
        for item in items:
            if item.get("state") == "scheduled":
                if float(item["not_before"]) <= now:
                    continue  # already public on YouTube (or about to be)
                try:
                    unschedule(item["path"], item.get("previous_privacy") or "private")
                except Exception as exc:
                    failed.append((item["title"], _error_text(exc)))
                    continue
            elif item.get("state") != "queued":
                continue
            item.update(state="cancelled", message="取り消しました", finished_at=now)
            count += 1
        if count:
            _save_queue(items)
    return count, failed


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
    """Publish due queued items and record scheduled ones that went public, while the app is open."""
    recover_interrupted()

    def loop():
        while True:
            try:
                settle_scheduled(on_published=on_published)
            except Exception:
                pass
            try:
                run_due(on_published=on_published)
            except Exception:
                pass
            _wake.wait(poll_sec)
            _wake.clear()

    thread = threading.Thread(target=loop, name="publish-worker", daemon=True)
    thread.start()
    return thread
