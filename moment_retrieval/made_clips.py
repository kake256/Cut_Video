"""Finished clips from auto jobs (the app's own and the daily batch's), for posting later.

Jobs made with ``upload=False`` (e.g. the daily batch) leave their shorts in
``clips/highlights/<元動画名>__<ID>/``.  This module lists them from the job
records, read-only, and uploads a chosen clip with the privacy the user picks.
A ``<clip>.youtube.json`` receipt marks a clip as uploaded; it prevents a second
upload and holds the privacy/processing state shown in the GUI.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from . import config, youtube_upload


@dataclass(frozen=True)
class MadeClip:
    path: Path
    job_id: str
    job_source: str
    created_at: float
    source_name: str  # the output folder's source video name
    title: str
    batch: bool  # made without uploading (daily batch, or "アップロードしない")
    receipt: dict | None


def _jobs_dir() -> Path:
    return config.CACHE_ROOT / "auto_jobs"


def read_receipt(path: Path) -> dict | None:
    try:
        data = json.loads(youtube_upload.receipt_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("video_id") else None


def _write_receipt(path: Path, receipt: dict) -> None:
    target = youtube_upload.receipt_path(path)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def _source_name(path: Path) -> str:
    return path.parent.name.rsplit("__", 1)[0]


def list_clips() -> list[MadeClip]:
    """Clips of finished jobs whose files still exist, newest job first.

    Job files are read fresh each time, so clips the batch made after the app
    started show up on refresh.  Clips of unfinished jobs are skipped (they may
    still be being finished) unless they were already uploaded.
    """
    directory = _jobs_dir()
    if not directory.is_dir():
        return []
    jobs = []
    for job_path in directory.glob("auto_*.json"):
        if "." in job_path.stem:  # e.g. auto_x.export.json (export progress)
            continue
        try:
            job = json.loads(job_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(job, dict) and job.get("outputs"):
            jobs.append(job)
    jobs.sort(key=lambda job: float(job.get("created_at") or 0), reverse=True)
    clips, seen = [], set()
    for job in jobs:
        for output in job["outputs"]:
            path = Path(str(output))
            key = os.path.normcase(str(path))
            if key in seen or not path.is_file():
                continue
            receipt = read_receipt(path)
            if job.get("state") != "done" and not receipt:
                continue
            seen.add(key)
            clips.append(MadeClip(
                path=path, job_id=str(job.get("job_id") or ""), job_source=str(job.get("source") or ""),
                created_at=float(job.get("created_at") or 0), source_name=_source_name(path),
                title=youtube_upload.metadata_for(path).title, batch=not job.get("upload", True), receipt=receipt,
            ))
    return clips


def find_clip(path: str | Path) -> MadeClip:
    """Only clips listed from job records may be uploaded (never an arbitrary path)."""
    key = os.path.normcase(str(path))
    for clip in list_clips():
        if os.path.normcase(str(clip.path)) == key:
            return clip
    raise youtube_upload.UploadError(f"作成済みの動画の一覧に見つかりません: {Path(str(path)).name}")


def status_label(receipt: dict | None) -> str:
    if not receipt:
        return "未投稿"
    if receipt.get("missing_on_youtube"):
        return "YouTubeに見つかりません（削除済み？）"
    label = youtube_upload.PRIVACY_LABELS.get(str(receipt.get("privacy_status")), "非公開")
    upload_status = receipt.get("upload_status")
    if upload_status == "uploaded":
        label += "・処理中"
    elif upload_status in {"rejected", "failed"}:
        label += f"・YouTubeが拒否（{receipt.get('rejection_reason') or upload_status}）"
    return label


def upload(path: str | Path, privacy: str, *, service_factory: Callable | None = None
           ) -> Iterator[tuple[str, dict | None]]:
    """Upload one clip; "public" uploads privately first and then publishes it.

    An existing receipt skips the clip, so pressing the button twice never posts twice.
    """
    if privacy not in youtube_upload.PRIVACY_CHOICES:
        raise youtube_upload.UploadError("公開設定を選んでください。")
    clip = find_clip(path)
    if clip.receipt:
        yield f"アップロード済みのため省略しました（{status_label(clip.receipt)}）: {clip.title}", clip.receipt
        return
    if service_factory is None and (youtube_upload.setup_problems() or not youtube_upload.token_path().is_file()):
        raise youtube_upload.UploadError("YouTubeアカウントが未連携です。「設定」タブで連携してください。")
    first = "private" if privacy == "public" else privacy
    receipt = None
    for message, result in youtube_upload.upload_private(
            clip.path, privacy=first, service_factory=service_factory, interactive=False):
        yield message, None
        receipt = result or receipt
    if not receipt:
        return
    from .used_ranges import record_from_clip

    try:  # the posted range counts as used for later clipping
        record_from_clip(clip.path, receipt.get("title", ""))
    except Exception:
        pass
    if privacy == "public":
        try:
            receipt = change_privacy(clip.path, "public", service_factory=service_factory)
        except youtube_upload.UploadError as exc:
            yield f"非公開でアップロードしましたが、公開にできませんでした: {exc}", receipt
            return
        yield f"公開しました: {receipt['watch_url']}", receipt
        return
    yield "", receipt


def change_privacy(path: str | Path, privacy: str, *, service_factory: Callable | None = None) -> dict:
    clip = find_clip(path)
    receipt = dict(clip.receipt or {})
    if not receipt:
        raise youtube_upload.UploadError(f"まだアップロードしていません: {clip.title}")
    status = youtube_upload.set_privacy(receipt["video_id"], privacy, service_factory=service_factory)
    receipt.update(privacy_status=status, missing_on_youtube=False)
    _write_receipt(clip.path, receipt)
    return receipt


def refresh_statuses(paths: list[str | Path] | None = None, *, service_factory: Callable | None = None) -> int:
    """Ask YouTube for the current privacy/processing state of uploaded clips; returns how many."""
    clips = [clip for clip in list_clips() if clip.receipt]
    if paths:
        wanted = {os.path.normcase(str(path)) for path in paths}
        clips = [clip for clip in clips if os.path.normcase(str(clip.path)) in wanted]
    if not clips:
        return 0
    statuses = youtube_upload.video_statuses([clip.receipt["video_id"] for clip in clips],
                                             service_factory=service_factory)
    now = time.time()
    for clip in clips:
        receipt = dict(clip.receipt)
        found = statuses.get(str(receipt["video_id"]))
        if found is None:
            receipt["missing_on_youtube"] = True
        else:
            receipt.update(found, missing_on_youtube=False)
        receipt["checked_at"] = now
        _write_receipt(clip.path, receipt)
    return len(clips)
