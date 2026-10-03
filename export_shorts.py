#!/usr/bin/env python
"""Export every candidate of one highlight run as 9:16 Short videos.

Run as a separate process (the MCP sidecar starts it) so ffmpeg work never
blocks an MCP call.  Progress is written atomically to ``--status-file`` as
JSON: ``state`` is ``running``, ``done`` or ``failed``.  Nothing is uploaded.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _write_status(path: Path, **fields) -> None:
    fields["updated_at"] = time.time()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(fields, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def export_run(video_id: str, highlight_run_id: str, *, layout: str, burn_captions: bool,
               status_file: Path) -> tuple[list[str], str]:
    import app
    from moment_retrieval import config, db

    conn = db.get_conn()
    try:
        db.init_db(conn)
        revision = db.get_active_transcript_revision(conn, video_id)
        latest = db.get_latest_ready_highlight_run(conn, video_id, revision) if revision else None
    finally:
        conn.close()
    # The UI export always uses the latest ready run; refuse a stale request.
    if latest is None or latest["highlight_run_id"] != highlight_run_id:
        raise RuntimeError("指定した候補は最新ではありません。候補を提案し直してください。")
    output_root = str(config.ARTIFACT_ROOT / "highlights")
    log_text, files = "", []
    for log_text, files in app.export_highlight_candidates(
        video_id, "", "all", output_root, True, "short", layout, "1080x1920",
        burn_captions, None, None, True,
    ):
        _write_status(status_file, state="running", log=log_text, outputs=list(files))
    return [str(item) for item in files], log_text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--highlight-run-id", required=True)
    parser.add_argument("--layout", choices=["blur", "crop"], default="blur")
    parser.add_argument("--no-captions", action="store_true")
    parser.add_argument("--status-file", required=True, type=Path)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    _write_status(args.status_file, state="running", log="書き出しを準備しています。", outputs=[])
    try:
        outputs, log_text = export_run(
            args.video_id, args.highlight_run_id, layout=args.layout,
            burn_captions=not args.no_captions, status_file=args.status_file,
        )
    except Exception as exc:  # report every failure through the status file
        _write_status(args.status_file, state="failed", log=f"{type(exc).__name__}: {exc}", outputs=[])
        raise SystemExit(1)
    _write_status(args.status_file, state="done", log=log_text, outputs=outputs)


if __name__ == "__main__":
    main()
