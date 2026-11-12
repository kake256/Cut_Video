"""Caption policy for finished Shorts (the look first used for the English-gag Shorts).

All sizes are for a 1080x1920 canvas and are scaled to the actual canvas.

* Spoken captions: one short line per utterance, white with a thick black outline
  (74 px, outline 9), always centred at y=1430: just under the 16:9 picture
  (y 656-1264), clear of the channel name and title YouTube draws over the
  bottom ~350 px even with two lines.  A line wraps at about 15 full-width
  characters, preferring a space or punctuation.
* Notes: optional yellow supplementary text (46 px) on its own layer between the
  picture and the captions (centre y=1300), e.g. "※渋谷には行きません".
* Title: two lines at the top (92 px, centre y=150); the second line is the
  emphasised part and is yellow.
* Footer: "<配信者> 配信（YYYY/MM/DD）より" in small grey text at the bottom.
* Audio: the clip opens at the nearest pause, long pauses are cut out and the
  clips joined; loudness is normalised in two passes (one gain for the clip).

Global settings live in ``<data>/short_style.json``; hand-written parts for one
clip (title, notes, captions, footer) in ``<clip>.subtitle.json`` next to it.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

BASE_W, BASE_H = 1080, 1920

CAPTION_SIZE, CAPTION_OUTLINE, CAPTION_Y = 74, 9, 1430
NOTE_SIZE, NOTE_OUTLINE, NOTE_Y = 46, 5, 1300
TITLE_SIZE, TITLE_OUTLINE, TITLE_Y = 92, 8, 150
FOOTER_SIZE, FOOTER_OUTLINE, FOOTER_Y = 34, 3, 1870
LINE_HEIGHT = 1.18
CAPTION_LINE_UNITS = 28  # 14 full-width characters (74 px each) fit inside 1080 px
TITLE_LINE_UNITS = 22  # 11 full-width characters at 92 px

WHITE = "&H00FFFFFF"
YELLOW_TITLE = "&H003CDCFF"  # (255, 220, 60)
YELLOW_NOTE = "&H0050E6FF"  # (255, 230, 80)
GREY = "&H00C8C8C8"
BLACK = "&H00000000"

DEFAULT_SETTINGS = {
    "font_name": "Yu Gothic UI",
    "streamer_name": "",  # empty: taken from 【name】 in the clip title when present
    "footer": True,
    "ai_notes": True,  # let the AI add up to 2 short notes when something needs explaining
    "cut_silence": True,
    "silence_min_sec": 1.0,  # pauses at least this long are shortened
    "silence_keep_sec": 0.25,  # silence kept on each side of a cut
    "loudnorm": True,
    "snap_start": True,  # move the clip start to the nearest pause (not inside a word)
}


def settings_path() -> Path:
    from . import config

    return Path(config.DATA_DIR) / "short_style.json"


def load_settings(path: Path | None = None) -> dict:
    settings = dict(DEFAULT_SETTINGS)
    try:
        raw = json.loads(Path(path or settings_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if isinstance(raw, dict):
        for key, default in DEFAULT_SETTINGS.items():
            value = raw.get(key)
            if isinstance(default, bool) and isinstance(value, bool):
                settings[key] = value
            elif isinstance(default, float) and isinstance(value, (int, float)) and not isinstance(value, bool):
                settings[key] = float(value)
            elif isinstance(default, str) and isinstance(value, str):
                settings[key] = value.strip()
    if not settings["font_name"] or any(ch in settings["font_name"] for ch in ",\r\n"):
        settings["font_name"] = DEFAULT_SETTINGS["font_name"]
    settings["silence_min_sec"] = min(10.0, max(0.3, settings["silence_min_sec"]))
    settings["silence_keep_sec"] = min(1.0, max(0.05, settings["silence_keep_sec"]))
    return settings


@dataclass
class ClipOverrides:
    """Hand-written parts for one clip.  Times are seconds from the clip's start."""

    title: list[tuple[str, bool]] | None = None  # (text, emphasised)
    footer: str | None = None
    notes: list[tuple[float, float, str]] | None = None
    captions: list[tuple[float, float, str]] | None = None


def _timed(items) -> list[tuple[float, float, str]]:
    out = []
    for item in items if isinstance(items, list) else []:
        try:
            if isinstance(item, dict):
                start, end, text = float(item["start"]), float(item["end"]), str(item["text"])
            else:
                start, end, text = float(item[0]), float(item[1]), str(item[2])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        text = " ".join(text.split())
        if text and end > start >= 0:
            out.append((start, end, text))
    return sorted(out)


def load_overrides(clip: Path) -> ClipOverrides:
    """Read ``<clip>.subtitle.json``; a missing or broken file means no overrides."""
    try:
        raw = json.loads(Path(clip).with_suffix(".subtitle.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ClipOverrides()
    if not isinstance(raw, dict):
        return ClipOverrides()
    overrides = ClipOverrides()
    if isinstance(raw.get("title"), list):
        lines = []
        for index, item in enumerate(raw["title"][:2]):
            if isinstance(item, str):
                text, emphasis = item, index == 1
            elif isinstance(item, dict):
                text, emphasis = str(item.get("text") or ""), bool(item.get("emphasis"))
            else:
                continue
            if text.strip():
                lines.append((" ".join(text.split()), emphasis))
        overrides.title = lines or None
    if isinstance(raw.get("footer"), str):
        overrides.footer = " ".join(raw["footer"].split())
    if "notes" in raw:
        overrides.notes = _timed(raw.get("notes"))
    if "captions" in raw:
        overrides.captions = _timed(raw.get("captions")) or None
    return overrides


def display_units(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in {"W", "F", "A"} else 1 for ch in text)


def clean_caption(text: str) -> str:
    """Spoken-style line: no full stops, phrases separated by spaces."""
    text = str(text or "").replace("。", " ").replace("　", " ")
    return " ".join(text.split()).strip(" 、")


_BREAKS = "、，,！？!?…"
_NO_LINE_START = "」』）)］]、。，,！？!?…ーっゃゅょ"


def _kind(ch: str) -> str:
    code = ord(ch)
    if 0x30A0 <= code <= 0x30FF:
        return "katakana"
    if 0x4E00 <= code <= 0x9FFF or ch in "々〆":
        return "kanji"
    if 0x3040 <= code <= 0x309F:
        return "hiragana"
    return "other"


def _balanced_cut(line: str, units: int) -> int | None:
    """Where to break a line that fits in two: near the middle, at a natural boundary."""
    best, best_score = None, None
    total = display_units(line)
    for cut in range(1, len(line)):
        left, right = line[:cut], line[cut:]
        a, b = line[cut - 1], line[cut]
        if display_units(left) > units + 4 or display_units(right) > units + 4 or b in _NO_LINE_START:
            continue
        score = -abs(display_units(left) - total / 2) / 4
        score -= max(0, display_units(left) - units) + max(0, display_units(right) - units)
        if a in _BREAKS or a in "」』）)":
            score += 3
        elif _kind(a) == "hiragana" and _kind(b) in {"katakana", "kanji"}:
            score += 2
        elif _kind(a) == _kind(b) and _kind(a) in {"katakana", "kanji", "other"}:
            score -= 3
        elif _kind(a) == "kanji" and _kind(b) == "hiragana":
            score -= 2
        if best_score is None or score > best_score:
            best, best_score = cut, score
    return best


def _hard_wrap(line: str, units: int) -> list[str]:
    if units < display_units(line) <= units * 2:
        cut = _balanced_cut(line, units)
        if cut:
            return [line[:cut].strip(), line[cut:].strip()]
    out = []
    while display_units(line) > units:
        used, end = 0, 0
        for index, ch in enumerate(line):
            used += display_units(ch)
            if used > units:
                break
            end = index + 1
        end = max(1, end)
        soft = max((i + 1 for i, ch in enumerate(line[:end]) if ch in _BREAKS), default=0)
        cut = soft if soft >= end // 2 else end
        # Kinsoku: closing brackets and punctuation stay with the line before (drawn slightly narrower).
        extra = 0
        while cut < len(line) and line[cut] in _NO_LINE_START and extra < 2:
            cut, extra = cut + 1, extra + 1
        out.append(line[:cut].strip())
        line = line[cut:].strip()
    if line:
        out.append(line)
    return out


def wrap_caption(text: str, units: int = CAPTION_LINE_UNITS) -> list[str]:
    """Wrap at spaces first (greedy), then at punctuation, then by width."""
    text = clean_caption(text)
    if not text:
        return []
    if display_units(text) <= units:
        return [text]
    lines, current = [], ""
    for part in text.split(" "):
        candidate = f"{current} {part}".strip()
        if current and display_units(candidate) > units:
            lines.append(current)
            current = part
        else:
            current = candidate
    lines.append(current)
    wrapped = []
    for line in lines:
        wrapped.extend(_hard_wrap(line, units))
    return wrapped


_BRACKET_NAME = re.compile(r"【([^】]{1,30})】")


def streamer_from_title(title: str) -> str:
    match = _BRACKET_NAME.search(str(title or ""))
    return match.group(1).strip() if match else ""


def title_lines_from_text(text: str) -> list[tuple[str, bool]]:
    """Fallback title from a plain text (e.g. the clip's posting title): 2 lines, last one yellow."""
    text = _BRACKET_NAME.sub("", str(text or ""))
    text = re.sub(r"[#＃]\S+", "", text)
    text = " ".join(text.split())
    if not text:
        return []
    lines = wrap_caption(text, TITLE_LINE_UNITS)
    if len(lines) > 2:
        second = lines[1]
        while display_units(second + "…") > TITLE_LINE_UNITS:
            second = second[:-1]
        lines = [lines[0], second + "…"]
    if len(lines) == 1:
        return [(lines[0], True)]
    return [(lines[0], False), (lines[1], True)]


def footer_text(streamer: str, upload_date: str) -> str:
    digits = re.sub(r"\D", "", str(upload_date or ""))
    date = f"{digits[:4]}/{digits[4:6]}/{digits[6:8]}" if len(digits) >= 8 else ""
    streamer = " ".join(str(streamer or "").split())
    if streamer and date:
        return f"{streamer} 配信（{date}）より"
    if streamer:
        return f"{streamer} 配信より"
    if date:
        return f"{date} の配信より"
    return ""


# ---------- silence cutting ----------

def keep_segments(duration: float, speech: list[tuple[float, float]], *, min_gap: float = 1.0,
                  keep: float = 0.25, tail: float = 0.5) -> list[tuple[float, float]]:
    """Parts of the clip to keep so that pauses of ``min_gap`` s or more become short.

    ``speech`` should be every interval where something is said (voice detector
    regions and Whisper word times together), so shouting the detector misses is
    never cut.  Returns ``[(0, duration)]`` when there is nothing to cut.
    """
    spans = sorted((max(0.0, a), min(duration, b)) for a, b in speech if b > 0 and a < duration and b > a)
    if not spans:
        return [(0.0, duration)]
    merged = [list(spans[0])]
    for a, b in spans[1:]:
        if a - merged[-1][1] < min_gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    segments = []
    for index, (a, b) in enumerate(merged):
        start = 0.0 if index == 0 and a < min_gap else max(0.0, a - keep)
        end = duration if index == len(merged) - 1 and duration - b < min_gap else min(duration, b + (
            tail if index == len(merged) - 1 else keep))
        if segments and start <= segments[-1][1]:
            segments[-1] = (segments[-1][0], end)
        else:
            segments.append((start, end))
    return [(round(a, 3), round(b, 3)) for a, b in segments if b - a > 0.05]


class TimeMap:
    """Clip time -> output time after the kept segments are joined."""

    def __init__(self, segments: list[tuple[float, float]]):
        self.segments = segments
        self.offsets = []
        acc = 0.0
        for a, b in segments:
            self.offsets.append(acc)
            acc += b - a
        self.duration = acc

    def start(self, t: float) -> float | None:
        """Output time for a start; inside a cut it moves to the next kept part."""
        for (a, b), off in zip(self.segments, self.offsets):
            if t < a:
                return off
            if t <= b:
                return off + t - a
        return None

    def end(self, t: float) -> float | None:
        """Output time for an end; inside a cut it moves back to the previous kept part's end."""
        result = None
        for (a, b), off in zip(self.segments, self.offsets):
            if t < a:
                return result
            if t <= b:
                return off + t - a
            result = off + b - a
        return result

    def span(self, start: float, end: float, minimum: float = 0.2) -> tuple[float, float] | None:
        s, e = self.start(start), self.end(end)
        if s is None or e is None or e - s < minimum:
            return None
        return round(s, 3), round(e, 3)


# ---------- ASS ----------

@dataclass
class Layout:
    width: int
    height: int
    font: str = DEFAULT_SETTINGS["font_name"]
    sx: float = field(init=False)
    sy: float = field(init=False)

    def __post_init__(self):
        self.sx, self.sy = self.width / BASE_W, self.height / BASE_H

    def px(self, value: float) -> int:
        return max(1, round(value * self.sy))

    def pos(self, y: float) -> str:
        return rf"\an5\pos({round(self.width / 2)},{round(y * self.sy)})"


def ass_styles(layout: Layout) -> list[str]:
    def style(name, size, colour, outline):
        return (f"Style: {name},{layout.font},{layout.px(size)},{colour},&H000000FF,{BLACK},&H00000000,"
                f"-1,0,0,0,100,100,0,0,1,{layout.px(outline)},0,5,0,0,0,1")
    return [
        style("Default", CAPTION_SIZE, WHITE, CAPTION_OUTLINE),
        style("Note", NOTE_SIZE, YELLOW_NOTE, NOTE_OUTLINE),
        style("Title", TITLE_SIZE, WHITE, TITLE_OUTLINE),
        style("Footer", FOOTER_SIZE, GREY, FOOTER_OUTLINE),
    ]


def ass_header(layout: Layout) -> list[str]:
    return [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {layout.width}", f"PlayResY: {layout.height}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        *ass_styles(layout),
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]


def title_events(layout: Layout, lines: list[tuple[str, bool]], duration_text: str) -> list[str]:
    from .short_video import _ass_text

    lines = lines[:2]
    step = TITLE_SIZE * LINE_HEIGHT
    first = TITLE_Y - step * (len(lines) - 1) / 2
    events = []
    for index, (text, emphasis) in enumerate(lines):
        colour = r"\c&H3CDCFF&" if emphasis else ""
        events.append(f"Dialogue: 2,0:00:00.00,{duration_text},Title,,0,0,0,,"
                      f"{{{layout.pos(first + step * index)}{colour}}}{_ass_text(text)}")
    return events
