"""Excitement levels (0-10) per time bin, computed by the app for the highlight AI.

Inputs are the stored signals of a video (see ``signals``): the audio loudness
profile and, when the original stream had one, its chat replay.  Each signal is
compared with the stream's own baseline (a quiet talk stream and a loud game
stream are judged against themselves), then combined into an integer level so
the model never has to do arithmetic:

* audio: the loudest second in each bin, as robust z-score against all bins;
* chat: messages per bin as a multiple of the stream's usual (median) rate,
  shifted a few seconds earlier because viewers react after the moment;
* laughs: chat messages like ``w`` / ``ｗ`` / ``草`` / ``笑`` / ``lol`` per bin,
  as a multiple of the stream's average.

Without chat the level comes from audio alone.  The result is derived data in
SQLite (``excitement_profiles``); ASR rows and the search index are never touched.
"""
from __future__ import annotations

import math
import re
import statistics
from pathlib import Path
from typing import Callable, Sequence

from . import db, signals

VERSION = 2
BIN_SEC = 10.0
CHAT_LAG_SEC = 5.0  # chat reacts a few seconds after what happened on stream
ACTIVE_DB = -50.0  # bins quieter than this are silence, not part of the baseline
AUDIO_FULL_Z = 4.0  # robust z-score that counts as maximum audio excitement
CHAT_FULL_RATIO = 4.0  # chat at 4x the usual rate counts as maximum
LAUGH_FULL_RATIO = 4.0
# Chat is counted over a bin plus half of each neighbour (about 20 s), and the
# usual rate has a floor, so 3-4 messages on a quiet chat are not a "burst".
CHAT_WINDOW_BINS = 2.0
CHAT_MIN_BASELINE = 3.0  # messages per window
LAUGH_MIN_BASELINE = 1.0
CHAT_NOTE_RATIO = 1.3  # chapter lines mention chat only from this multiple up
LAUGH_NOTE_RATIO = 1.5

_LAUGH = re.compile(
    r"(?:^|[^a-zA-Zａ-ｚＡ-Ｚ])[wWｗＷ]{1,}(?:$|[^a-zA-Zａ-ｚＡ-Ｚ])|草|笑|lol|lmao|kekw|lul|omegalul|ｗ|ハハハ|ははは|ﾊﾊﾊ|ワロ|わろ|ﾜﾛ|^くさ+$",
    re.IGNORECASE,
)


def is_laugh(text: str) -> bool:
    return bool(_LAUGH.search(str(text or "")))


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _audio_bins(profile: dict, bin_sec: float) -> list[float]:
    """Loudest one-second level (dBFS) in each bin."""
    seconds = signals._per_second(profile.get("loudness_db", []), float(profile.get("loudness_step", 0.1)))
    per = max(1, int(round(bin_sec)))
    return [max(seconds[index:index + per]) for index in range(0, len(seconds), per)]


def _audio_scores(peaks: Sequence[float]) -> list[float]:
    """Robust z-scores against the stream's own active bins (silence excluded)."""
    active = [value for value in peaks if value > ACTIVE_DB]
    if len(active) < 6:
        return [0.0] * len(peaks)
    median = statistics.median(active)
    spread = 1.4826 * statistics.median(abs(value - median) for value in active)
    spread = max(spread, 1.5)  # flat audio must not turn 1 dB wiggles into excitement
    return [(value - median) / spread if value > ACTIVE_DB else 0.0 for value in peaks]


def _windowed(counts: Sequence[int]) -> list[float]:
    """Each bin plus half of each neighbour (``CHAT_WINDOW_BINS`` bins' worth)."""
    return [counts[i] + 0.5 * ((counts[i - 1] if i else 0) + (counts[i + 1] if i + 1 < len(counts) else 0))
            for i in range(len(counts))]


def compute_profile(audio: dict | None, chat: dict | None, *, duration_sec: float | None = None,
                    bin_sec: float = BIN_SEC, chat_lag_sec: float = CHAT_LAG_SEC) -> dict:
    """``{"bin_sec", "sources", "chat_baseline", "laugh_baseline", "bins": [...]}``.

    Each bin is ``{"t": start_sec, "level": 0-10, "audio_z", "chat", "laugh"}``.
    """
    peaks = _audio_bins(audio, bin_sec) if audio else []
    messages = [item for item in ((chat or {}).get("messages") or []) if isinstance(item, (list, tuple)) and item]
    offset = float((chat or {}).get("offset_sec") or 0.0)
    shifted = []
    for item in messages:
        try:
            at = float(item[0]) - offset - chat_lag_sec
        except (TypeError, ValueError):
            continue
        if at >= -chat_lag_sec:
            shifted.append((max(0.0, at), str(item[1]) if len(item) > 1 else ""))
    count = len(peaks)
    if duration_sec:
        count = max(count, int(math.ceil(float(duration_sec) / bin_sec)))
    if shifted and not peaks and not duration_sec:
        count = int(max(at for at, _t in shifted) // bin_sec) + 1
    chat_counts = [0] * count
    laugh_counts = [0] * count
    for at, text in shifted:
        index = int(at // bin_sec)
        if index < count:
            chat_counts[index] += 1
            laugh_counts[index] += is_laugh(text)
    has_chat = bool(shifted)
    audio_z = _audio_scores(peaks) + [0.0] * (count - len(peaks))
    chat_window = _windowed(chat_counts)
    laugh_window = _windowed(laugh_counts)
    chat_baseline = max(statistics.median(chat_window), CHAT_MIN_BASELINE) if has_chat and count else CHAT_MIN_BASELINE
    laugh_baseline = (max(sum(laugh_window) / count, LAUGH_MIN_BASELINE) if has_chat and count
                      else LAUGH_MIN_BASELINE)
    bins = []
    for index in range(count):
        parts = [_clamp(audio_z[index] / AUDIO_FULL_Z)] if peaks else []
        if has_chat:
            chat_ratio = chat_window[index] / chat_baseline
            laugh_ratio = laugh_window[index] / laugh_baseline
            chat_part = _clamp(math.log2(max(chat_ratio, 1.0)) / math.log2(CHAT_FULL_RATIO))
            laugh_part = _clamp(math.log2(max(laugh_ratio, 1.0)) / math.log2(LAUGH_FULL_RATIO))
            parts += [chat_part, laugh_part]
        if not parts:
            combined = 0.0
        elif len(parts) == 1:
            combined = parts[0]
        else:
            weights = (0.45, 0.35, 0.20) if peaks else (0.65, 0.35)
            weighted = sum(weight * part for weight, part in zip(weights, parts))
            # One very strong signal (a scream nobody typed about) still counts.
            combined = 0.5 * weighted + 0.5 * max(parts)
        bins.append({"t": round(index * bin_sec, 1), "level": int(round(10 * combined)),
                     "audio_z": round(audio_z[index], 2), "chat": chat_counts[index],
                     "laugh": laugh_counts[index]})
    return {"bin_sec": bin_sec, "chat_lag_sec": chat_lag_sec, "chat_window_bins": CHAT_WINDOW_BINS,
            "sources": (["audio"] if peaks else []) + (["chat"] if has_chat else []),
            "chat_baseline": chat_baseline, "laugh_baseline": laugh_baseline, "bins": bins}


def _overlapping(profile: dict, start: float, end: float) -> list[dict]:
    bin_sec = float(profile.get("bin_sec") or BIN_SEC)
    return [item for item in profile.get("bins", [])
            if float(item["t"]) < end and float(item["t"]) + bin_sec > start]


def level_between(profile: dict | None, start: float, end: float) -> int | None:
    """Highest level of the bins overlapping [start, end]; None without a profile."""
    if not profile or not profile.get("sources"):
        return None
    chosen = _overlapping(profile, float(start), max(float(end), float(start) + 0.001))
    return max((int(item["level"]) for item in chosen), default=0)


def chapter_summary(profile: dict | None, start: float, end: float) -> dict | None:
    if not profile or not profile.get("sources"):
        return None
    chosen = _overlapping(profile, float(start), float(end))
    if not chosen:
        return None
    summary = {"max_level": max(int(item["level"]) for item in chosen)}
    if "chat" in profile["sources"]:
        window = float(profile.get("chat_window_bins") or 1.0)
        per_window = window * sum(int(item["chat"]) for item in chosen) / len(chosen)
        laughs = window * sum(int(item["laugh"]) for item in chosen) / len(chosen)
        summary["chat_ratio"] = round(per_window / float(profile.get("chat_baseline") or 1.0), 1)
        summary["laugh_ratio"] = round(laughs / float(profile.get("laugh_baseline") or 1.0), 1)
    return summary


def summary_line(summary: dict | None) -> str:
    """``盛り上がり: 最大8/10, コメント2.5倍, 笑い多め`` (empty without data)."""
    if not summary:
        return ""
    parts = [f"最大{int(summary['max_level'])}/10"]
    if summary.get("chat_ratio", 0) >= CHAT_NOTE_RATIO:
        parts.append(f"コメント{summary['chat_ratio']:.1f}倍")
    if summary.get("laugh_ratio", 0) >= LAUGH_NOTE_RATIO:
        parts.append("笑い多め")
    return "盛り上がり: " + ", ".join(parts)


def _inputs(audio: dict | None, chat: dict | None) -> dict:
    messages = (chat or {}).get("messages") or []
    return {"audio": (audio or {}).get("source"),
            "chat": {"url": (chat or {}).get("url"), "count": len(messages),
                     "offset_sec": (chat or {}).get("offset_sec")} if messages else None}


def fetch_chat_once(conn, video_id: str, *, log: Callable | None = None) -> None:
    """Store the original stream's chat once, as the auto pipeline does (best effort).

    Uses the video's recorded origin URL (YouTube live chat replay / Twitch VOD).
    A stream without chat is stored as an empty record so it is not asked again;
    network trouble stores nothing so the next run retries.
    """
    from . import chat_history, source_origin

    video = db.get_video(conn, video_id)
    if video is None or signals.load_chat(str(video["video_id"])) is not None:
        return
    url = source_origin.origin_url_for_video(conn, video)
    if not url:
        return
    try:
        chat = chat_history.fetch(url, log=log)
    except chat_history.ChatUnavailable as exc:
        if log:
            log(f"コメント履歴は使いません: {exc}")
        signals.save_chat(str(video["video_id"]), {"url": url, "messages": [], "unavailable": str(exc)})
        return
    except Exception as exc:
        if log:
            log(f"コメント履歴を取得できなかったため、今回は使いません（{type(exc).__name__}）")
        return
    signals.save_chat(str(video["video_id"]), chat)
    if log:
        log(f"コメント履歴を{len(chat['messages'])}件取得しました。")


def ensure_profile(conn, video_id: str, *, source: Path | None = None, build_audio: bool = True,
                   log: Callable | None = None) -> dict | None:
    """Load or (re)compute the stored profile from the video's current signals.

    Audio is built once per source file when ``build_audio`` (best effort); chat is
    only read from the signals cache.  Returns None when no signal exists.
    """
    video = db.get_video(conn, video_id)
    if video is None:
        return None
    storage_id = str(video["video_id"])
    source = Path(source or video["path"])
    audio = None
    try:
        audio = (signals.ensure_audio(storage_id, source, log=log) if build_audio
                 else signals.load_audio(storage_id, source))
    except Exception as exc:  # the transcript alone still works
        if log:
            log(f"音声の波形を解析できなかったため、音量は使いません（{type(exc).__name__}）")
    chat = signals.load_chat(storage_id)
    if not (chat or {}).get("messages"):
        chat = None
    if audio is None and chat is None:
        return None
    inputs = _inputs(audio, chat)
    stored = db.get_excitement_profile(conn, storage_id)
    if stored and stored["version"] == VERSION and stored["inputs"] == inputs:
        return stored["profile"]
    profile = compute_profile(audio, chat, duration_sec=float(video.get("duration") or 0) or None)
    db.save_excitement_profile(conn, storage_id, version=VERSION, inputs=inputs, profile=profile)
    return profile
