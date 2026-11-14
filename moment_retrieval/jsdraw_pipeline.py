"""Auto-publish: make each picked clip as a JS-drawn Short (jsdraw_short) instead of the normal finish.

The picked clip is 20-180 s; ``jsdraw_short.make_short`` takes a 50-300 s stretch of the
stream and condenses it to a 50-58 s Short.  Short clips are widened around their middle
to about ``TARGET_SEC`` (edges on segment boundaries, never past the stream's ends or into
another posted range); longer clips are passed as they are.

The result replaces the clip file under the same name, so its ``.metadata.json`` (title,
description, source range) stays with it and upload / posted-range records work unchanged.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Callable, Sequence

from . import db

TARGET_SEC = 90.0
MIN_SEC = 50.0  # make_short's lower limit
MAX_SEC = 300.0  # make_short's upper limit
PREPARE_PAD_SEC = 8.0  # make_short reads this much around the range (audio cut points, face thumbnails)

Log = Callable[[str], None]


class JsDrawPipelineError(RuntimeError):
    pass


def has_key() -> bool:
    from .api_keys import resolve

    return bool(resolve("anthropic")[0])


NO_KEY_MESSAGE = ("この設定ではJS描画を Claude API で行うため、Claude（Anthropic）のAPIキーが必要です。"
                  "「設定」タブのAPIキー欄に保存してから、もう一度開始してください。")
LOCAL_NO_KEY_NOTE = "ローカルAIでは描けないため、JS描画にはAPIキーが必要です（設定タブで保存すると使えます）"


def backend_for(agent: str) -> str:
    """The JS drawing follows the highlight-picking AI: Claude Code / Codex draw on their login,
    the API option uses the API, and the local AI (which cannot draw) falls back to the API."""
    return {"claude": "claude", "codex": "codex"}.get(agent, "api")


def note_for(agent: str) -> str:
    """One line under the checkbox saying how the Short will be drawn."""
    backend = backend_for(agent)
    if backend == "claude":
        return "Claude Codeのログイン枠で描きます（APIの料金はかかりません）"
    if backend == "codex":
        return "Codexのログイン枠で、見どころと同じCodexのモデルで描きます（描画モデルの選択は使いません）"
    if agent == "local" and not has_key():
        return LOCAL_NO_KEY_NOTE
    return "Claude APIで描きます（1本の目安：Fable 約0.5／Opus 約0.25／Sonnet 約0.1ドル）"


def available_for(agent: str) -> bool:
    return backend_for(agent) != "api" or has_key()


def widen_range(start: float, end: float, segments: Sequence[dict], *, stream_end: float,
                used: Sequence[dict] = (), target: float = TARGET_SEC) -> tuple[float, float]:
    """The source range to hand to make_short for the clip [start, end].

    Clips of ``target`` s or longer are returned unchanged.  Shorter ones grow evenly on both
    sides (what one side cannot take goes to the other), within [0, stream_end] and without
    reaching into a used range that lies before / after the clip; the new edges are moved
    inward to the nearest segment start / end.  Ranges that overlap the clip itself (its own
    record, written at export) do not limit it.
    """
    start, end = float(start), float(end)
    if end - start >= target:
        return start, end
    left_limit, right_limit = 0.0, float(stream_end)
    for item in used:
        a, b = float(item["start_sec"]), float(item["end_sec"])
        if b <= start + 1e-6:
            left_limit = max(left_limit, b)
        elif a >= end - 1e-6:
            right_limit = min(right_limit, a)
    need = target - (end - start)
    room_left, room_right = start - left_limit, right_limit - end
    grow_left = min(room_left, need / 2)
    grow_right = min(room_right, need - grow_left)
    grow_left = min(room_left, need - grow_right)
    new_start, new_end = start - grow_left, end + grow_right
    # Snap inward to segment edges, so the range never passes the limits and starts on an utterance.
    starts = sorted(float(s["start_sec"]) for s in segments if new_start - 1e-6 <= float(s["start_sec"]) <= start)
    ends = sorted(float(s["end_sec"]) for s in segments if end <= float(s["end_sec"]) <= new_end + 1e-6)
    new_start = starts[0] if starts else start
    new_end = ends[-1] if ends else end
    return round(new_start, 3), round(new_end, 3)


def _video_and_segments(video_id: str) -> tuple[dict, list[dict]]:
    conn = db.get_conn()
    try:
        db.init_db(conn)
        video = db.get_video(conn, video_id)
        revision = db.get_active_transcript_revision(conn, video_id)
        segments = db.get_segments(conn, video_id, transcript_revision=revision) if revision else []
    finally:
        conn.close()
    if not video:
        raise JsDrawPipelineError("元の動画がライブラリに見つかりません")
    return dict(video), [dict(row) for row in segments]


def source_range(clip: Path) -> tuple[str, float, float, dict]:
    meta = json.loads(Path(clip).with_suffix(".metadata.json").read_text(encoding="utf-8"))
    source = meta["source_range"]
    return str(source["video_id"]), float(source["start_sec"]), float(source["end_sec"]), meta


def range_for_clip(clip: Path) -> tuple[float, float]:
    """The widened source range for an exported clip (also used to download just that section)."""
    from .used_ranges import for_video

    video_id, start, end, _meta = source_range(clip)
    video, segments = _video_and_segments(video_id)
    stream_end = float(video.get("duration") or 0.0) or max((float(s["end_sec"]) for s in segments), default=end)
    return widen_range(start, end, segments, stream_end=stream_end, used=for_video(video_id))


def candidate_for(clip: Path, highlight_run_id: str = "") -> dict | None:
    """The highlight proposal an exported clip was cut from (matched by its source range)."""
    if not highlight_run_id:
        return None
    _video_id, start, end, _meta = source_range(clip)
    conn = db.get_conn()
    try:
        candidates = db.get_highlight_candidates(conn, highlight_run_id)
    finally:
        conn.close()
    best = min(candidates, default=None,
               key=lambda c: abs(float(c["start_sec"]) - start) + abs(float(c["end_sec"]) - end))
    if best is not None and abs(float(best["start_sec"]) - start) + abs(float(best["end_sec"]) - end) < 5.0:
        return best
    return None


def title_and_story(clip: Path, highlight_run_id: str = "") -> tuple[str, str]:
    """The proposal's title, and its synopsis (summary) or the reason it was picked."""
    _video_id, _start, _end, meta = source_range(clip)
    title, story = str(meta.get("title") or ""), ""
    best = candidate_for(clip, highlight_run_id)
    if best is not None:
        title = str(best["title"] or title)
        story = str(best["summary"] or "").strip() or str(best["reason"] or "").strip()
    return title, story


def make_for_clip(clip: Path, *, model: str, highlight_run_id: str = "", section=None,
                  log: Log | None = None, make_short: Callable | None = None,
                  backend: str = "api", codex_model: str = "", suitability: dict | None = None) -> dict:
    """Replace the exported clip with a JS-drawn Short.  Raises on any failure (caller falls back).

    ``suitability`` ({"js_score", "js_reason"} from the picking AI) is written into
    ``<clip>.jsdraw.json`` so it can be compared with how the Short did later.
    """
    from . import jsdraw_short

    make_short = make_short or jsdraw_short.make_short
    clip = Path(clip)
    video_id, _start, _end, _meta = source_range(clip)
    start, end = range_for_clip(clip)
    if end - start < MIN_SEC:
        raise JsDrawPipelineError(f"前後に広げても{end - start:.0f}秒しか取れません（{MIN_SEC:g}秒以上が必要）")
    end = min(end, start + MAX_SEC)
    video, _segments = _video_and_segments(video_id)
    if section is not None:
        media, offset = str(section[0]), float(section[1])
    else:
        media, offset = str(video.get("path") or ""), 0.0
    if not media or not Path(media).is_file():
        raise JsDrawPipelineError("元動画が見つかりません")
    title, story = title_and_story(clip, highlight_run_id)
    work = clip.with_name(f".{clip.stem}.jsdraw")
    try:
        result = make_short(media, start, end, model=model, title=title, story=story, out_dir=work,
                            progress=log or (lambda _message: None), video_id=video_id, media_offset=offset,
                            backend=backend, codex_model=codex_model)
        os.replace(result["mp4"], clip)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    record = {"backend": backend, "model": codex_model if backend == "codex" else model,
              "range": [start, end], "title": title, "story": story,
              "usd": result.get("usd"), "usd_equivalent": result.get("usd_equivalent"),
              "seconds": result.get("seconds"),
              "segs_total_generated": result.get("segs_total_generated"), "usage": result.get("usage"),
              "js_score": (suitability or {}).get("js_score"), "js_reason": (suitability or {}).get("js_reason") or ""}
    clip.with_suffix(".jsdraw.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record
