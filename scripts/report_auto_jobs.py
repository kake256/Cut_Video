#!/usr/bin/env python
"""Summarise auto-publish jobs: time per step and fallbacks, to find what to speed up next.

Reads the job records in data/auto_jobs (read-only).  Jobs started before step
timings were recorded show only their total time.

    venv\\Scripts\\python.exe scripts\\report_auto_jobs.py --days 1
    venv\\Scripts\\python.exe scripts\\report_auto_jobs.py --json auto_9889278aed36 auto_6c1ea8281d66
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from moment_retrieval import config  # noqa: E402

STEP_ORDER = ("metadata", "download", "index", "select", "select.signals", "await_video", "export",
              "finish", "upload")
STEP_LABELS = {"metadata": "動画情報", "download": "取り込み", "index": "文字起こし", "select": "候補選び",
               "select.signals": "（うち盛り上がり解析）", "await_video": "動画DL待ち", "export": "範囲記録/書き出し",
               "finish": "仕上げ", "upload": "投稿"}
# Log lines that mean a step fell back to a weaker or slower path.
FALLBACKS = {
    "聞き直し失敗→文字起こしの字幕": "音声の聞き直しに失敗",
    "仕上げ計画なし": "仕上げの計画を取得できなかった",
    "発話区間なし": "発話区間を検出できなかった",
    "仕上げ省略": "の仕上げを省略しました",
    "動画を作れず": "の動画を作れませんでした",
    "波形解析なし": "音声の波形を解析できなかった",
    "コメント取得失敗": "コメント履歴を取得できなかった",
    "候補の分割保存": "回に分けて保存",
}


def load_jobs(job_ids: list[str], days: float | None) -> list[dict]:
    directory = Path(config.CACHE_ROOT) / "auto_jobs"
    paths = [directory / f"{job_id}.json" for job_id in job_ids] if job_ids else \
        sorted(path for path in directory.glob("auto_*.json") if not path.name.endswith(".export.json"))
    jobs = []
    for path in paths:
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        job["_ended_at"] = path.stat().st_mtime
        if days is None or job.get("created_at", 0) >= time.time() - days * 86400:
            jobs.append(job)
    return sorted(jobs, key=lambda job: job.get("created_at", 0))


def video_minutes(video_id: str) -> float | None:
    if not video_id:
        return None
    try:
        conn = sqlite3.connect(f"file:{Path(config.DB_PATH)}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT duration FROM videos WHERE video_id = ? OR public_video_id = ?",
                               (video_id, video_id)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return round(float(row[0]) / 60, 1) if row and row[0] else None


def summarise(job: dict) -> dict:
    steps: dict[str, float] = {}
    for item in job.get("timings") or []:
        steps[item["step"]] = steps.get(item["step"], 0.0) + float(item["seconds"])
    log = job.get("log") or []
    timings = job.get("timings") or []
    ended = max((item["started_at"] + item["seconds"] for item in timings), default=job["_ended_at"])
    return {
        "job_id": job.get("job_id"),
        "state": job.get("state"),
        "agent": "/".join(part for part in (job.get("agent"), job.get("model")) if part),
        "clips": len(job.get("outputs") or []),
        "video_min": video_minutes(job.get("video_id", "")),
        # The record is touched again by a later "publish" click, so prefer the last step's end.
        "total_min": round((ended - job.get("created_at", ended)) / 60, 1),
        "steps_min": {name: round(seconds / 60, 1) for name, seconds in steps.items()},
        "fallbacks": {label: sum(pattern in line for line in log)
                      for label, pattern in FALLBACKS.items() if any(pattern in line for line in log)},
        "failed_at": job.get("step") if job.get("state") == "failed" else "",
    }


def print_report(rows: list[dict]) -> None:
    if not rows:
        print("該当するジョブがありません。")
        return
    for row in rows:
        size = f"{row['video_min']:g}分の動画" if row["video_min"] else "動画の長さ不明"
        print(f"■ {row['job_id']}  {row['state']}  {row['agent']}  {size} → {row['clips']}本  合計{row['total_min']}分"
              + (f"  （{row['failed_at']}で失敗）" if row["failed_at"] else ""))
        if row["steps_min"]:
            parts = [f"{STEP_LABELS.get(name, name)} {row['steps_min'][name]}分"
                     for name in STEP_ORDER if name in row["steps_min"]]
            print("   " + " / ".join(parts))
        else:
            print("   工程別の時刻は記録されていません（記録を始める前のジョブ）")
        if row["fallbacks"]:
            print("   フォールバック: " + "、".join(f"{label}×{count}" for label, count in row["fallbacks"].items()))
    timed = [row for row in rows if row["steps_min"] and row["state"] == "done"]
    if len(timed) > 1:
        print("\n■ 完了ジョブの平均（分）")
        for name in STEP_ORDER:
            values = [row["steps_min"][name] for row in timed if name in row["steps_min"]]
            if values:
                print(f"   {STEP_LABELS.get(name, name)}: {sum(values) / len(values):.1f}")
        clips = sum(row["clips"] for row in timed)
        if clips:
            print(f"   1本あたりの仕上げ: {sum(row['steps_min'].get('finish', 0) for row in timed) / clips:.2f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("job_ids", nargs="*", help="対象のジョブID（省略時はすべて）")
    parser.add_argument("--days", type=float, help="直近この日数に始まったジョブだけ")
    parser.add_argument("--json", action="store_true", help="JSONで出力する")
    args = parser.parse_args()
    rows = [summarise(job) for job in load_jobs(args.job_ids, args.days)]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print_report(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
