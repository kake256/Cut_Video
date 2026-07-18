from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import Enum


class TimestampGranularity(str, Enum):
    WORD = "word"
    SEGMENT = "segment"


@dataclass(frozen=True)
class TranscriptRevisionRef:
    public_video_id: str
    source_generation: str
    transcript_revision: str


@dataclass(frozen=True)
class TranscriptWord:
    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class TranscriptSegment:
    segment_id: int
    start_ms: int
    end_ms: int
    text: str
    words: tuple[TranscriptWord, ...]
    granularity: TimestampGranularity


def _milliseconds(value: object) -> int:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("timestamp is not finite")
    return round(number * 1000)


def parse_segment(row: dict, source_duration_ms: int) -> TranscriptSegment:
    start_ms = _milliseconds(row["start_sec"])
    end_ms = _milliseconds(row["end_sec"])
    if not 0 <= start_ms < end_ms <= source_duration_ms:
        raise ValueError("segment is outside the source duration")
    raw = row.get("words_json")
    words: list[TranscriptWord] = []
    valid = True
    try:
        units = json.loads(raw) if raw else []
        if not isinstance(units, list) or not units:
            valid = False
        previous_start = previous_end = -1
        for unit in units if isinstance(units, list) else []:
            text = str(unit.get("word") or "") if isinstance(unit, dict) else ""
            if not text.strip():
                continue
            word_start = _milliseconds(unit.get("start"))
            word_end = _milliseconds(unit.get("end"))
            if not (start_ms <= word_start < word_end <= end_ms):
                valid = False
                break
            if word_start < previous_start or word_end < previous_end:
                valid = False
                break
            words.append(TranscriptWord(text, word_start, word_end))
            previous_start, previous_end = word_start, word_end
        if not words:
            valid = False
    except (TypeError, ValueError, json.JSONDecodeError):
        valid = False
    return TranscriptSegment(
        int(row["segment_id"]), start_ms, end_ms, str(row.get("text") or ""),
        tuple(words) if valid else (),
        TimestampGranularity.WORD if valid else TimestampGranularity.SEGMENT,
    )
