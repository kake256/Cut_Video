"""Local 9:16 short-video rendering with optional burned-in ASR captions."""
from __future__ import annotations

import math
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .subtitles import SubtitleCue


class ShortVideoError(RuntimeError):
    pass


@dataclass(frozen=True)
class ShortVideoOptions:
    width: int = 1080
    height: int = 1920
    layout: str = "blur"
    burn_captions: bool = True

    def validate(self) -> "ShortVideoOptions":
        if self.layout not in {"blur", "crop"}:
            raise ValueError("short-video layout must be 'blur' or 'crop'")
        if self.width <= 0 or self.height <= 0 or self.width >= self.height:
            raise ValueError("short-video resolution must be a positive portrait size")
        if self.width % 2 or self.height % 2:
            raise ValueError("short-video resolution must use even dimensions")
        return self


def parse_short_resolution(value: str) -> tuple[int, int]:
    normalized = str(value or "").lower().replace(" ", "")
    supported = {
        "1080x1920": (1080, 1920),
        "720x1280": (720, 1280),
    }
    try:
        return supported[normalized]
    except KeyError as exc:
        raise ValueError("ショート動画の解像度は1080x1920または720x1280です") from exc


def _normalize_caption_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _split_caption_text(value: str, max_chars: int) -> list[str]:
    text = _normalize_caption_text(value)
    if not text:
        return []
    max_chars = max(4, int(max_chars))
    parts: list[str] = []
    remaining = text
    punctuation = "。！？!?、，, "
    while len(remaining) > max_chars:
        window = remaining[: max_chars + 1]
        split_at = max((window.rfind(mark) for mark in punctuation), default=-1)
        if split_at < max_chars // 2:
            split_at = max_chars
        else:
            split_at += 1
        parts.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        parts.append(remaining)
    return [part for part in parts if part]


def prepare_short_captions(
    cues: Iterable[SubtitleCue], *, max_chars: int = 18, minimum_part_ms: int = 500,
) -> tuple[SubtitleCue, ...]:
    """Split long ASR cues into phone-readable caption blocks.

    Timing remains inside the already-mapped output cue. No transcript text is
    logged or persisted by this function.
    """
    prepared: list[SubtitleCue] = []
    for cue in cues:
        duration = int(cue.end_ms) - int(cue.start_ms)
        if duration <= 0:
            continue
        parts = _split_caption_text(cue.text, max_chars)
        if not parts:
            continue
        max_parts = max(1, duration // max(1, int(minimum_part_ms)))
        if len(parts) > max_parts:
            text = _normalize_caption_text(cue.text)
            chunk_size = max(1, math.ceil(len(text) / max_parts))
            parts = [
                text[index:index + chunk_size].strip()
                for index in range(0, len(text), chunk_size)
            ]
            parts = [part for part in parts if part]
        weights = [max(1, len(re.sub(r"\s", "", part))) for part in parts]
        total_weight = sum(weights)
        guaranteed_ms = min(int(minimum_part_ms), duration // len(parts))
        distributable_ms = duration - guaranteed_ms * len(parts)
        consumed = 0
        for index, (part, weight) in enumerate(zip(parts, weights)):
            part_start = (
                cue.start_ms
                + guaranteed_ms * index
                + round(distributable_ms * consumed / total_weight)
            )
            consumed += weight
            part_end = (
                cue.end_ms
                if index == len(parts) - 1
                else (
                    cue.start_ms
                    + guaranteed_ms * (index + 1)
                    + round(distributable_ms * consumed / total_weight)
                )
            )
            if part_end > part_start:
                prepared.append(SubtitleCue(
                    part_start, part_end, part, cue.source_segment_id,
                ))
    return tuple(prepared)


def _ass_time(value_ms: int) -> str:
    centiseconds = max(0, round(int(value_ms) / 10))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{fraction:02d}"


def _ass_text(value: str) -> str:
    return (
        str(value or "")
        .replace("\\", "＼")
        .replace("{", "｛")
        .replace("}", "｝")
        .replace("\r\n", "\\N")
        .replace("\r", "\\N")
        .replace("\n", "\\N")
    )


def captions_to_ass(
    cues: Iterable[SubtitleCue], width: int, height: int,
) -> str:
    font_size = max(32, round(height * 0.041))
    outline = max(2, round(height * 0.003))
    margin_v = max(64, round(height * 0.105))
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Yu Gothic UI,"
        f"{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H78000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline},2,2,70,70,{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text",
    ]
    for cue in cues:
        if cue.end_ms <= cue.start_ms or not str(cue.text or "").strip():
            continue
        lines.append(
            f"Dialogue: 0,{_ass_time(cue.start_ms)},{_ass_time(cue.end_ms)},"
            f"Default,,0,0,0,,{_ass_text(cue.text)}"
        )
    return "\n".join(lines) + "\n"


def build_short_filter(options: ShortVideoOptions, *, include_captions: bool) -> str:
    options.validate()
    width, height = options.width, options.height
    captions = ",subtitles=filename=captions.ass" if include_captions else ""
    if options.layout == "crop":
        return (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1{captions}[vout]"
        )
    return (
        "[0:v]split=2[background_source][foreground_source];"
        f"[background_source]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=24:2[background];"
        f"[foreground_source]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        "setsar=1[foreground];"
        "[background][foreground]overlay=(W-w)/2:(H-h)/2,setsar=1"
        f"{captions}[vout]"
    )


def render_short_clip(
    video_path: Path,
    start: float,
    end: float,
    output_path: Path,
    *,
    captions: Iterable[SubtitleCue] = (),
    options: ShortVideoOptions = ShortVideoOptions(),
    duration: float | None = None,
    timeout_sec: float | None = None,
) -> Path:
    """Render one source interval as a portrait MP4.

    Captions are written only to a temporary ASS file next to the staging
    output. Keeping the filter filename relative avoids Windows drive-letter
    escaping in libass.
    """
    options.validate()
    start_value, end_value = float(start), float(end)
    if not math.isfinite(start_value) or not math.isfinite(end_value):
        raise ValueError("ショート動画の開始・終了時刻は有限値で指定してください")
    if start_value < 0 or end_value <= start_value:
        raise ValueError("ショート動画の終了時刻は開始時刻より後にしてください")
    if duration is not None and end_value > float(duration) + 0.001:
        raise ValueError("ショート動画の終了時刻が元動画の長さを超えています")

    video_path = Path(video_path).resolve()
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prepared = tuple(captions) if options.burn_captions else ()
    with tempfile.TemporaryDirectory(
        prefix="cut_video_short_", dir=str(output_path.parent),
    ) as temporary_name:
        temporary = Path(temporary_name)
        if prepared:
            (temporary / "captions.ass").write_text(
                captions_to_ass(prepared, options.width, options.height),
                encoding="utf-8",
            )
        command = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", f"{start_value:.3f}",
            "-i", str(video_path),
            "-t", f"{end_value - start_value:.3f}",
            "-filter_complex", build_short_filter(
                options, include_captions=bool(prepared),
            ),
            "-map", "[vout]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(output_path),
        ]
        try:
            subprocess.run(
                command, cwd=temporary, check=True, capture_output=True,
                timeout=timeout_sec,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            output_path.unlink(missing_ok=True)
            raise ShortVideoError("ショート動画の生成に失敗しました") from exc
    return output_path
