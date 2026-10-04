#!/usr/bin/env python
"""Try the finishing pass on an existing job's clips without touching the originals.

    venv\\Scripts\\python.exe finish_clips.py [--job auto_xxx] [--agent codex] [--model gpt-6-luna]

Writes ``<name>_finished.mp4`` (+ ``.finish.json`` with the plan) next to each clip.
The transcript lines of each clip are sent to the selected AI.  Nothing is uploaded.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from moment_retrieval import agent_runner, config, finishing


def latest_job_with_outputs() -> dict:
    jobs = []
    for path in (config.CACHE_ROOT / "auto_jobs").glob("auto_*.json"):
        if ".export" in path.name:
            continue
        job = json.loads(path.read_text(encoding="utf-8"))
        if job.get("outputs"):
            jobs.append(job)
    if not jobs:
        raise SystemExit("書き出し済みのジョブがありません。")
    return max(jobs, key=lambda job: job["created_at"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job")
    parser.add_argument("--agent", choices=agent_runner.AGENTS, default="codex")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    model = args.model or (agent_runner.DEFAULT_CODEX_MODEL if args.agent == "codex" else "")
    job = (json.loads((config.CACHE_ROOT / "auto_jobs" / f"{args.job}.json").read_text(encoding="utf-8"))
           if args.job else latest_job_with_outputs())
    print(f"ジョブ {job['job_id']}（{len(job['outputs'])}本）を仕上げます。")
    for original in map(Path, job["outputs"]):
        copy = original.with_name(f"{original.stem}_finished.mp4")
        shutil.copyfile(original, copy)
        shutil.copyfile(original.with_suffix(".metadata.json"), copy.with_suffix(".metadata.json"))
        plan = finishing.finish_exported_clip(copy, agent=args.agent, model=model, focus=job.get("focus", ""),
                                              log=print)
        effects = ", ".join(f"{at:g}s {kind}" for at, kind in plan.sound_effects) or "なし"
        print(f"- {copy.name}: 引き「{plan.hook_text}」/ 字幕 {plan.caption_preset} / 効果音 {effects}"
              f" / 詰め {plan.trim_start:g}s・{plan.trim_end:g}s")


if __name__ == "__main__":
    main()
