from __future__ import annotations

from dataclasses import dataclass

from .edit_domain import EffectiveExportPlan, TimeRange
from .transcript_types import TimestampGranularity, TranscriptSegment, TranscriptWord


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
