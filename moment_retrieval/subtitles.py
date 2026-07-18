from __future__ import annotations

import re
from dataclasses import dataclass

from .edit_domain import EffectiveExportPlan, TimeRange
from .transcript_types import TimestampGranularity, TranscriptSegment, TranscriptWord


class SubtitleValidationError(ValueError):
    pass


_SRT_TIMING = re.compile(
    r"^\s*(\d+):([0-5]\d):([0-5]\d),(\d{3})\s*-->\s*"
    r"(\d+):([0-5]\d):([0-5]\d),(\d{3})\s*$"
)


@dataclass(frozen=True)
class SubtitleCue:
    start_ms: int
    end_ms: int
    text: str
    source_segment_id: int


@dataclass(frozen=True)
class SubtitleResult:
    cues: tuple[SubtitleCue, ...]
    warnings: tuple[str, ...]

    def to_srt(self) -> str:
        blocks = []
        for index, cue in enumerate(self.cues, 1):
            blocks.append(
                f"{index}\n{format_srt_time(cue.start_ms)} --> {format_srt_time(cue.end_ms)}\n"
                f"{cue.text.strip()}\n"
            )
        return "\n".join(blocks)


def format_srt_time(value_ms: int) -> str:
    value_ms = max(0, int(value_ms))
    hours, remainder = divmod(value_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def _srt_groups_to_ms(groups: tuple[str, ...]) -> int:
    hours, minutes, seconds, milliseconds = (int(value) for value in groups)
    return hours * 3_600_000 + minutes * 60_000 + seconds * 1000 + milliseconds


def validate_srt_text(
    text: str, output_duration_ms: int, tolerance_ms: int,
) -> tuple[tuple[int, int], ...]:
    """Validate generated SRT timing against the probed output artifact.

    Cue text is intentionally not returned or logged.  An empty SRT is valid
    when every transcript cue was removed by the edit plan.
    """
    duration = int(output_duration_ms)
    tolerance = max(0, int(tolerance_ms))
    if duration < 0:
        raise SubtitleValidationError("output duration must not be negative")
    if not text.strip():
        return ()
    timings: list[tuple[int, int]] = []
    for line in text.splitlines():
        if "-->" not in line:
            continue
        match = _SRT_TIMING.fullmatch(line)
        if match is None:
            raise SubtitleValidationError("invalid SRT timing line")
        start_ms = _srt_groups_to_ms(match.groups()[:4])
        end_ms = _srt_groups_to_ms(match.groups()[4:])
        if not 0 <= start_ms < end_ms:
            raise SubtitleValidationError("SRT cue must have a positive duration")
        if end_ms > duration + tolerance:
            raise SubtitleValidationError("SRT cue exceeds the probed output duration")
        if timings and start_ms < timings[-1][0]:
            raise SubtitleValidationError("SRT cues are not in timeline order")
        timings.append((start_ms, end_ms))
    if not timings:
        raise SubtitleValidationError("non-empty SRT has no timing cues")
    return tuple(timings)


def _containing_range(ranges: tuple[TimeRange, ...], start: int, end: int) -> TimeRange | None:
    return next((item for item in ranges if item.start_ms <= start and end <= item.end_ms), None)


def map_subtitles(
    segments: list[TranscriptSegment], effective: EffectiveExportPlan,
    output_duration_ms: int | None = None, tolerance_ms: int = 50,
) -> SubtitleResult:
    kept = effective.plan.kept_ranges
    mapping = effective.timeline_map
    cues: list[SubtitleCue] = []
    warnings: list[str] = []
    for segment in segments:
        if segment.granularity == TimestampGranularity.SEGMENT:
            if not _containing_range(kept, segment.start_ms, segment.end_ms):
                warnings.append(f"segment {segment.segment_id}: partially cut fallback timing omitted")
                continue
            mapped_start = mapping.source_boundary_to_result(segment.start_ms, edge="start")
            mapped_end = mapping.source_boundary_to_result(segment.end_ms, edge="end")
            if mapped_start is not None and mapped_end is not None:
                cues.append(SubtitleCue(mapped_start, mapped_end, segment.text, segment.segment_id))
            continue

        group: list[TranscriptWord] = []
        group_range: TimeRange | None = None

        def flush() -> None:
            nonlocal group, group_range
            if not group:
                return
            mapped_start = mapping.source_boundary_to_result(group[0].start_ms, edge="start")
            mapped_end = mapping.source_boundary_to_result(group[-1].end_ms, edge="end")
            if mapped_start is not None and mapped_end is not None and mapped_end > mapped_start:
                cues.append(SubtitleCue(
                    mapped_start, mapped_end, "".join(word.text for word in group), segment.segment_id
                ))
            group = []
            group_range = None

        for word in segment.words:
            containing = _containing_range(kept, word.start_ms, word.end_ms)
            if not containing:
                flush()
                warnings.append(f"segment {segment.segment_id}: partially cut word omitted")
                continue
            if group_range is not None and containing != group_range:
                flush()
            group_range = containing
            group.append(word)
        flush()

    cues.sort(key=lambda cue: (cue.start_ms, cue.end_ms, cue.source_segment_id))
    prior_end = -1
    accepted: list[SubtitleCue] = []
    limit = output_duration_ms if output_duration_ms is not None else mapping.result_duration_ms
    for cue in cues:
        if not (0 <= cue.start_ms < cue.end_ms <= limit + tolerance_ms):
            warnings.append(f"segment {cue.source_segment_id}: cue outside output duration omitted")
            continue
        if cue.start_ms < prior_end:
            warnings.append(f"segment {cue.source_segment_id}: overlapping cue retained")
        accepted.append(cue)
        prior_end = max(prior_end, cue.end_ms)
    return SubtitleResult(tuple(accepted), tuple(warnings))
