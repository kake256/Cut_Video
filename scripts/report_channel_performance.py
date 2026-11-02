#!/usr/bin/env python
"""Show how posted clips did within their batch, and the hint the selection prompt gets.

Fetches the linked channel's view counts (read-only) and saves them to
data/channel_performance.json, which auto jobs reuse for a day.

    venv\\Scripts\\python.exe scripts\\report_channel_performance.py
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from moment_retrieval import channel_performance  # noqa: E402


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    data = channel_performance.refresh()
    videos = data["videos"]
    batches: dict[str, list[dict]] = {}
    for video in videos:
        if video["privacy"] == "public" and 0 < video["seconds"] <= channel_performance.MAX_SHORT_SEC:
            batches.setdefault(video["batch"], []).append(video)
    print(f"動画 {len(videos)} 本（公開ショート {sum(len(group) for group in batches.values())} 本）")
    print("\n投稿のまとまりごとの再生数（中央値が低いまとまりは、内容より表示されなかったことを疑う）:")
    for batch, group in sorted(batches.items(), key=lambda item: min(video["published_at"] for video in item[1])):
        views = [video["views"] for video in group]
        print(f"  {batch:<28} {len(group):>3}本  中央値 {statistics.median(views):>7.0f}  最大 {max(views):>6}")
    section = channel_performance.prompt_section(data=data)
    print("\n選定プロンプトに足す文:\n" + (section or "（判断できる投稿がまだ少ないので足しません）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
