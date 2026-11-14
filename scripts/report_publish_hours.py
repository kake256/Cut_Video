#!/usr/bin/env python
"""Which publish hours (JST) did better, judged on clips that went public one at a time.

Read-only: fetches the linked channel's uploads and view counts (YouTube Data API).
The channel's Shorts stop gaining views within about a day, so the current count of
a clip public for 24 hours or more stands in for its "first 24 hours".  Clips that
went public within 30 minutes of another are left out: in a batch the clips compete
and whole batches can go unseen, whatever the hour.

    venv\\Scripts\\python.exe scripts\\report_publish_hours.py [--since 2026-11-06] [--bucket 3]
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from moment_retrieval import channel_performance, youtube_upload  # noqa: E402

JST = timezone(timedelta(hours=9))
ALONE_SEC = 30 * 60
MIN_AGE_SEC = 24 * 3600
FEW = 5  # fewer clips than this in a slot: say it is too few to judge


def singles(videos: list[dict], *, since: float, now: float) -> list[dict]:
    """Public Shorts published alone since ``since`` and at least a day ago, oldest first."""
    public = sorted((video for video in videos if video["privacy"] == "public"
                     and 0 < video["seconds"] <= channel_performance.MAX_SHORT_SEC),
                    key=lambda video: video["published_at"])
    times = [video["published_at"] for video in public]
    result = []
    for index, video in enumerate(public):
        neighbours = times[max(0, index - 1):index] + times[index + 1:index + 2]
        if any(abs(video["published_at"] - other) < ALONE_SEC for other in neighbours):
            continue
        if since <= video["published_at"] <= now - MIN_AGE_SEC:
            result.append(video)
    return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2026-11-06", help="この日（日本時間）以降の公開だけを見る")
    parser.add_argument("--bucket", type=int, default=3, help="何時間ごとにまとめるか")
    args = parser.parse_args()
    since = datetime.fromisoformat(args.since).replace(tzinfo=JST).timestamp()
    now = time.time()
    videos = channel_performance.fetch_videos(youtube_upload._service(None, interactive=False))
    picked = singles(videos, since=since, now=now)
    print(f"{args.since}以降に1本ずつ公開し、公開から24時間以上たったショート: {len(picked)}本\n")
    slots: dict[int, list[dict]] = {}
    for video in picked:
        hour = datetime.fromtimestamp(video["published_at"], JST).hour
        slots.setdefault(hour // args.bucket * args.bucket, []).append(video)
    for start in range(0, 24, args.bucket):
        group = slots.get(start, [])
        label = f"{start:02d}〜{start + args.bucket:02d}時"
        if not group:
            print(f"{label}: なし")
            continue
        views = sorted(video["views"] for video in group)
        note = "（本数が少なく判断できない）" if len(group) < FEW else ""
        print(f"{label}: {len(group)}本  中央値 {statistics.median(views):.0f}回  "
              f"最小 {views[0]}  最大 {views[-1]}{note}")
        for video in group:
            at = datetime.fromtimestamp(video["published_at"], JST)
            print(f"    {at:%m/%d %H:%M}  {video['views']:>6}回  {video['title'][:40]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
