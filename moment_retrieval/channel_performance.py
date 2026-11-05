"""How the channel's posted clips did, as a hint for choosing the next ones.

View counts of the linked channel's uploads come from the YouTube Data API
(the read-only scope the app already holds) and are joined to the auto-job
records that posted them.  Raw views mostly say whether YouTube showed a batch
to anyone at all: whole batches posted together stay near zero whatever their
content.  So each clip is compared with the clips posted in the same batch, and
only batches that reached viewers count.  The selection prompt then gets the
clips that did best and worst within their batch, as titles with view counts.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import config

CACHE_NAME = "channel_performance.json"
REFRESH_AFTER_SEC = 24 * 3600
MIN_AGE_DAYS = 3  # Shorts views settle after a few days; newer clips are not judged yet
MAX_AGE_DAYS = 120  # older posts were titled and cut by hand, in another style
MAX_SHORT_SEC = 180
BATCH_GAP_SEC = 120
DEAD_BATCH_MEDIAN = 30  # a batch whose median is below this was barely shown to anyone
MIN_SCORED = 12  # fewer judged clips than this says too little to steer the choice
EXAMPLES = 6
MIN_VIEWS_FOR_KEPT = 100  # below this the share kept past the opening is mostly noise


def cache_path() -> Path:
    return config.CACHE_ROOT / CACHE_NAME


def enabled() -> bool:
    return os.environ.get("CUT_PERFORMANCE_HINTS", "1") != "0"


def _seconds(duration: str) -> int:
    match = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration or "")
    if not match:
        return 0
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def fetch_videos(service) -> list[dict]:
    """Title, length, privacy and counts of every upload on the linked channel."""
    channels = service.channels().list(part="contentDetails", mine=True).execute().get("items") or []
    if not channels:
        return []
    playlist = channels[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    ids: list[str] = []
    page = None
    while True:
        response = service.playlistItems().list(
            part="contentDetails", playlistId=playlist, maxResults=50, pageToken=page).execute()
        ids += [item["contentDetails"]["videoId"] for item in response.get("items") or []]
        page = response.get("nextPageToken")
        if not page:
            break
    videos = []
    for start in range(0, len(ids), 50):
        response = service.videos().list(
            part="snippet,statistics,contentDetails,status", id=",".join(ids[start:start + 50])).execute()
        for item in response.get("items") or []:
            stats = item.get("statistics") or {}
            published = (item.get("snippet") or {}).get("publishedAt") or ""
            videos.append({
                "id": item["id"],
                "title": (item.get("snippet") or {}).get("title") or "",
                "published_at": datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp()
                if published else 0.0,
                "seconds": _seconds((item.get("contentDetails") or {}).get("duration") or ""),
                "privacy": (item.get("status") or {}).get("privacyStatus") or "",
                "views": int(stats.get("viewCount") or 0),
                "likes": int(stats.get("likeCount") or 0),
                "comments": int(stats.get("commentCount") or 0),
            })
    return videos


def job_batches() -> dict[str, dict]:
    """video_id -> the auto job that posted it (its batch) and the source video."""
    result: dict[str, dict] = {}
    for path in (config.CACHE_ROOT / "auto_jobs").glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for upload in job.get("uploads") or []:
            if isinstance(upload, dict) and upload.get("video_id"):
                result[str(upload["video_id"])] = {"batch": str(job.get("job_id") or path.stem),
                                                   "source": str(job.get("source_video_key") or "")}
    return result


def _analytics_service():
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    from . import youtube_upload

    http = AuthorizedHttp(youtube_upload.load_credentials(interactive=False), http=youtube_upload._transport())
    return build("youtubeAnalytics", "v2", http=http, cache_discovery=False)


def fetch_retention(service, *, now: float | None = None, days: int = MAX_AGE_DAYS + 30) -> dict[str, dict]:
    """video_id -> share of views kept past the first seconds, and the average share watched.

    For Shorts ``views`` counts every start, swipes included, while ``engagedViews``
    counts only views past the first few seconds, so their ratio is how often the
    opening held the viewer.
    """
    now = time.time() if now is None else now
    end = datetime.fromtimestamp(now).date()
    start = datetime.fromtimestamp(now - days * 86400).date()
    result: dict[str, dict] = {}
    index = 1
    while True:
        response = service.reports().query(
            ids="channel==MINE", startDate=start.isoformat(), endDate=end.isoformat(),
            metrics="views,engagedViews,averageViewPercentage", dimensions="video",
            sort="-views", maxResults=200, startIndex=index).execute()
        rows = response.get("rows") or []
        for video_id, views, engaged, percentage in rows:
            if views:
                result[str(video_id)] = {"kept": round(engaged / views, 3), "watched": round(percentage / 100, 3)}
        if len(rows) < 200:
            return result
        index += len(rows)


def refresh(*, service_factory: Callable | None = None, analytics_factory: Callable | None = None,
            now: float | None = None) -> dict:
    """Fetch the counts again and save them with each video's batch."""
    from . import youtube_upload

    videos = fetch_videos(youtube_upload._service(service_factory, interactive=False))
    try:
        retention = fetch_retention((analytics_factory or _analytics_service)(), now=now)
    except Exception:  # Analytics API off or not allowed: views alone still work
        retention = {}
    batches = job_batches()
    for video in videos:
        video.update(retention.get(video["id"]) or {})
        joined = batches.get(video["id"])
        # Clips posted outside an auto job (by hand from the app) are grouped by posting hour.
        video["batch"] = joined["batch"] if joined else f"hour:{int(video['published_at'] // 3600)}"
        video["source"] = joined["source"] if joined else ""
    data = {"fetched_at": time.time() if now is None else now, "videos": videos}
    target = cache_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, target)
    return data


def load(*, max_age_sec: float = REFRESH_AFTER_SEC, refresh_fn: Callable[[], dict] | None = None,
         now: float | None = None) -> dict | None:
    """The saved counts, fetched again when older than a day; the old copy when that fails."""
    now = time.time() if now is None else now
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if data and now - float(data.get("fetched_at") or 0) < max_age_sec:
        return data
    try:
        return (refresh_fn or refresh)()
    except Exception:  # offline, not linked, quota: choose without the hint rather than fail the job
        return data


def scored(videos: list[dict], *, now: float | None = None) -> list[dict]:
    """Public Shorts old enough to judge, each with views relative to its batch's median."""
    now = time.time() if now is None else now
    ready = [video for video in videos
             if video.get("privacy") == "public" and 0 < video.get("seconds", 0) <= MAX_SHORT_SEC
             and MIN_AGE_DAYS * 86400 <= now - video.get("published_at", 0) <= MAX_AGE_DAYS * 86400]
    jobs: dict[str, list[dict]] = {}
    for video in ready:
        jobs.setdefault(video.get("batch") or video["id"], []).append(video)
    # Clips of one job posted minutes apart fared as separate batches (the later few all near
    # zero), so a gap in posting time starts a new batch.
    groups: list[list[dict]] = []
    for members in jobs.values():
        members.sort(key=lambda video: video.get("published_at", 0))
        groups.append([members[0]])
        for previous, video in zip(members, members[1:]):
            if video.get("published_at", 0) - previous.get("published_at", 0) > BATCH_GAP_SEC:
                groups.append([])
            groups[-1].append(video)
    result = []
    for members in groups:
        median = statistics.median(video["views"] for video in members)
        if len(members) < 2 or median < DEAD_BATCH_MEDIAN:
            continue  # one clip has nothing to compare with; a dead batch says nothing about content
        result += [{**video, "relative": video["views"] / median} for video in members]
    return result


def _unique_titles(videos: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique = []
    for video in videos:
        if video["title"] not in seen:
            seen.add(video["title"])
            unique.append(video)
    return unique


def examples(videos: list[dict], *, count: int = EXAMPLES, now: float | None = None) -> tuple[list, list]:
    judged = _unique_titles(sorted(scored(videos, now=now), key=lambda video: -video["relative"]))
    if len(judged) < max(MIN_SCORED, count * 2):
        return [], []
    return judged[:count], judged[-count:][::-1]


def prompt_section(*, data: dict | None = None, now: float | None = None) -> str:
    """Lines for the selection prompt; empty when off or when there is too little to go on."""
    if not enabled():
        return ""
    data = load(now=now) if data is None else data
    good, poor = examples((data or {}).get("videos") or [], now=now)
    if not good:
        return ""

    def lines(videos: list[dict]) -> str:
        return "".join(
            f"- {video['title']}（{video['views']}回"
            + (f"、冒頭で離れなかった人{video['kept']:.0%}" if _kept_known(video) else "") + "）\n"
            for video in videos)

    return (
        "参考: このチャンネルで同時に投稿した切り抜きのうち、よく見られたもののタイトル:\n" + lines(good)
        + "同時に投稿した中であまり見られなかったもの:\n" + lines(poor)
        + "両者の題材や切り口の違いを踏まえ、よく見られた側に近い場面を優先する。"
        "あまり見られなかった側に近い場面は、他に良い場面がないときだけ選ぶ。タイトルを真似る必要はない。\n"
        + _opening_line(scored((data or {}).get("videos") or [], now=now))
    )


def _kept_known(video: dict) -> bool:
    return "kept" in video and video.get("views", 0) >= MIN_VIEWS_FOR_KEPT


def _opening_line(judged: list[dict]) -> str:
    """How often the opening loses viewers here, when the channel has enough retention data."""
    kept = [video["kept"] for video in judged if _kept_known(video)]
    if len(kept) < MIN_SCORED:
        return ""
    return (f"このチャンネルでは、見始めた人の{1 - statistics.median(kept):.0%}が最初の数秒で離れている。"
            "よく見られた切り抜きほど冒頭で離れる人が少ない。開始位置は、最初の1〜2秒で何が起きているか"
            "分かる発言や出来事の直前にし、前置きや相づちから始めない。\n")
