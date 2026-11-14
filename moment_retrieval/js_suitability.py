"""How well each picked clip suits a JS-drawn Short, and which clips get one.

When the JS-drawn Short is on, the picking AI (Claude Code / Codex through
``cut_propose_clips``, or the one-call API selector) also returns ``js_score``
(0-100) and a short ``js_reason`` for every clip, in the same answer: no extra
AI call.  The score is stored in the proposal's metadata; finishing gives the top
``jsdraw_count`` clips to JS drawing and the rest the normal finish.
"""
from __future__ import annotations

from typing import Sequence

MIN_SCORE, MAX_SCORE = 0, 100
REASON_MAX_CHARS = 120
DEFAULT_COUNT = 3

# One sentence appended to the picking prompt only when the box is on.
PROMPT_RULE = (
    "各候補に js_score（JS描画ショート＝図やアニメで描き直す動画への向き具合、0〜100の整数）と、"
    f"その理由 js_reason（{REASON_MAX_CHARS}文字以内）を付ける。"
    "数字・時間・金額が出てくる、具体的な物が出てくる、手順や段階的な変化がある、"
    "図で見せられるはっきりしたオチがある場面は高く、"
    "表情（顔カメラ）の反応やゲーム画面そのものが面白さの中心の場面、とりとめのない雑談は低くする。"
)


def clean(item: dict) -> dict:
    """``{"js_score", "js_reason"}`` from an AI answer, or ``{}`` when there is no usable score."""
    raw = item.get("js_score") if isinstance(item, dict) else None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return {}
    score = int(round(max(MIN_SCORE, min(MAX_SCORE, float(raw)))))
    reason = str(item.get("js_reason") or "").strip()[:REASON_MAX_CHARS]
    return {"js_score": score, "js_reason": reason}


def score_of(candidate: dict | None) -> int | None:
    """The stored score of a highlight candidate (``metadata.js_score``), None for old proposals."""
    if not candidate:
        return None
    metadata = candidate.get("metadata") or {}
    value = metadata.get("js_score") if isinstance(metadata, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def pick(scores: Sequence[int | None], count: int) -> list[int]:
    """Indexes (proposal order) of the clips to draw with JS: the top ``count`` by score.

    Ties keep proposal order.  Clips without a score rank after every scored one, in
    proposal order, so with no scores at all the first ``count`` clips are taken.
    ``count`` <= 0 means every clip (jobs saved before this setting existed).
    """
    order = sorted(range(len(scores)),
                   key=lambda i: (scores[i] is None, -(scores[i] or 0), i))
    if count <= 0:
        return sorted(order)
    return sorted(order[:count])
