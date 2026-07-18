from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
import math
from typing import Iterable

TimeMs = int
MIN_RANGE_MS = 100
MERGE_GAP_MS = 1


class EditPlanError(ValueError):
    pass


def seconds_to_ms(value: float) -> TimeMs:
    if not math.isfinite(float(value)):
        raise EditPlanError("time must be finite")
    return int(round(float(value) * 1000))


def ms_to_seconds(value: TimeMs) -> float:
    return int(value) / 1000.0


@dataclass(frozen=True, order=True)
class TimeRange:
    start_ms: TimeMs
    end_ms: TimeMs

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


KeptRange = TimeRange


def _normalize_exclusions(
    overall: TimeRange, exclusions: Iterable[TimeRange]
) -> tuple[TimeRange, ...]:
    clipped: list[TimeRange] = []
    for item in exclusions:
        start = max(overall.start_ms, int(item.start_ms))
        end = min(overall.end_ms, int(item.end_ms))
        if end - start < MIN_RANGE_MS:
            continue
        if clipped and start <= clipped[-1].end_ms + MERGE_GAP_MS:
            clipped[-1] = TimeRange(clipped[-1].start_ms, max(clipped[-1].end_ms, end))
        else:
            clipped.append(TimeRange(start, end))

    # Absorb a tiny kept island into the adjacent exclusion.  If every frame
    # would be removed, reject the command instead of constructing an invalid plan.
    changed = True
    while changed:
        changed = False
        for index in range(len(clipped) - 1):
            gap = clipped[index + 1].start_ms - clipped[index].end_ms
            if 0 < gap < MIN_RANGE_MS:
                clipped[index:index + 2] = [
                    TimeRange(clipped[index].start_ms, clipped[index + 1].end_ms)
                ]
                changed = True
                break
    if clipped and clipped[0].start_ms - overall.start_ms < MIN_RANGE_MS:
        clipped[0] = TimeRange(overall.start_ms, clipped[0].end_ms)
    if clipped and overall.end_ms - clipped[-1].end_ms < MIN_RANGE_MS:
        clipped[-1] = TimeRange(clipped[-1].start_ms, overall.end_ms)
    if len(clipped) == 1 and clipped[0] == overall:
        raise EditPlanError("an edit plan must keep at least 100 ms")
    return tuple(clipped)


@dataclass(frozen=True)
class EditPlan:
    source_duration_ms: TimeMs
    overall: TimeRange
    exclusions: tuple[TimeRange, ...] = ()

    @classmethod
    def create(
        cls,
        source_duration_ms: TimeMs,
        overall_start_ms: TimeMs,
        overall_end_ms: TimeMs,
        exclusions: Iterable[TimeRange] = (),
    ) -> "EditPlan":
        duration = int(source_duration_ms)
        overall = TimeRange(int(overall_start_ms), int(overall_end_ms))
        if duration < MIN_RANGE_MS:
            raise EditPlanError("source duration must be at least 100 ms")
        if not (0 <= overall.start_ms < overall.end_ms <= duration):
            raise EditPlanError("overall range is outside the source")
        if overall.duration_ms < MIN_RANGE_MS:
            raise EditPlanError("overall range must be at least 100 ms")
        ordered = sorted(exclusions)
        return cls(duration, overall, _normalize_exclusions(overall, ordered))

    @property
    def kept_ranges(self) -> tuple[KeptRange, ...]:
        cursor = self.overall.start_ms
        kept: list[KeptRange] = []
        for exclusion in self.exclusions:
            if exclusion.start_ms - cursor >= MIN_RANGE_MS:
                kept.append(KeptRange(cursor, exclusion.start_ms))
            cursor = max(cursor, exclusion.end_ms)
        if self.overall.end_ms - cursor >= MIN_RANGE_MS:
            kept.append(KeptRange(cursor, self.overall.end_ms))
        if not kept:
            raise EditPlanError("an edit plan must keep at least 100 ms")
        return tuple(kept)

    @property
    def result_duration_ms(self) -> int:
        return sum(item.duration_ms for item in self.kept_ranges)

    @property
    def semantic_signature(self) -> str:
        payload = {
            "version": 1,
            "source_duration_ms": self.source_duration_ms,
            "overall": [self.overall.start_ms, self.overall.end_ms],
            "exclusions": [[item.start_ms, item.end_ms] for item in self.exclusions],
        }
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        return sha256(encoded).hexdigest()

    def with_overall(self, start_ms: TimeMs, end_ms: TimeMs) -> "EditPlan":
        return self.create(self.source_duration_ms, start_ms, end_ms, self.exclusions)

    def add_exclusion(self, start_ms: TimeMs, end_ms: TimeMs) -> "EditPlan":
        return self.create(
            self.source_duration_ms, self.overall.start_ms, self.overall.end_ms,
            (*self.exclusions, TimeRange(int(start_ms), int(end_ms))),
        )


@dataclass(frozen=True)
class TimelineMap:
    kept_ranges: tuple[KeptRange, ...]

    @classmethod
    def from_plan(cls, plan: EditPlan) -> "TimelineMap":
        return cls(plan.kept_ranges)

    @property
    def result_duration_ms(self) -> int:
        return sum(item.duration_ms for item in self.kept_ranges)

    def source_to_result(self, source_ms: TimeMs) -> TimeMs | None:
        elapsed = 0
        source_ms = int(source_ms)
        for kept in self.kept_ranges:
            if kept.start_ms <= source_ms < kept.end_ms:
                return elapsed + source_ms - kept.start_ms
            elapsed += kept.duration_ms
        # The final source boundary maps to the final result boundary.
        if self.kept_ranges and source_ms == self.kept_ranges[-1].end_ms:
            return elapsed
        return None

    def source_boundary_to_result(self, source_ms: TimeMs, *, edge: str) -> TimeMs | None:
        """Map a half-open interval boundary with explicit start/end bias."""
        if edge not in {"start", "end"}:
            raise EditPlanError("edge must be start or end")
        elapsed = 0
        source_ms = int(source_ms)
        for kept in self.kept_ranges:
            if kept.start_ms <= source_ms < kept.end_ms:
                return elapsed + source_ms - kept.start_ms
            if edge == "end" and source_ms == kept.end_ms:
                return elapsed + kept.duration_ms
            elapsed += kept.duration_ms
        return None

    def result_to_source(self, result_ms: TimeMs) -> TimeMs:
        result_ms = int(result_ms)
        if not 0 <= result_ms <= self.result_duration_ms:
            raise EditPlanError("result position is outside the artifact")
        elapsed = 0
        for kept in self.kept_ranges:
            next_elapsed = elapsed + kept.duration_ms
            if result_ms < next_elapsed:
                return kept.start_ms + result_ms - elapsed
            elapsed = next_elapsed
        return self.kept_ranges[-1].end_ms


@dataclass(frozen=True)
class EffectiveExportPlan:
    plan: EditPlan
    requested_pad_before_ms: int
    requested_pad_after_ms: int
    effective_pad_before_ms: int
    effective_pad_after_ms: int
    timeline_map: TimelineMap


def make_effective_export_plan(
    plan: EditPlan, pad_before_ms: int = 0, pad_after_ms: int = 0
) -> EffectiveExportPlan:
    requested_before = max(0, int(pad_before_ms))
    requested_after = max(0, int(pad_after_ms))
    start = max(0, plan.overall.start_ms - requested_before)
    end = min(plan.source_duration_ms, plan.overall.end_ms + requested_after)
    effective = EditPlan.create(
        plan.source_duration_ms, start, end, plan.exclusions,
    )
    return EffectiveExportPlan(
        effective, requested_before, requested_after,
        plan.overall.start_ms - start, end - plan.overall.end_ms,
        TimelineMap.from_plan(effective),
    )


@dataclass(frozen=True)
class EditHistory:
    current: EditPlan
    clean_reference: EditPlan
    undo: tuple[EditPlan, ...] = ()
    redo: tuple[EditPlan, ...] = ()

    @classmethod
    def create(cls, plan: EditPlan) -> "EditHistory":
        return cls(plan, plan)

    @property
    def dirty(self) -> bool:
        return self.current.semantic_signature != self.clean_reference.semantic_signature

    def apply(self, plan: EditPlan) -> "EditHistory":
        if plan.semantic_signature == self.current.semantic_signature:
            return self
        return EditHistory(plan, self.clean_reference, (*self.undo, self.current)[-50:], ())

    def undo_once(self) -> "EditHistory":
        if not self.undo:
            return self
        return EditHistory(
            self.undo[-1], self.clean_reference, self.undo[:-1],
            (self.current, *self.redo)[:50],
        )

    def redo_once(self) -> "EditHistory":
        if not self.redo:
            return self
        return EditHistory(
            self.redo[0], self.clean_reference,
            (*self.undo, self.current)[-50:], self.redo[1:],
        )

    def mark_clean(self) -> "EditHistory":
        return replace(self, clean_reference=self.current)


def edit_plan_from_legacy(
    start_sec: float, end_sec: float, plan: dict | None, source_duration_sec: float | None = None
) -> EditPlan:
    duration_ms = seconds_to_ms(source_duration_sec if source_duration_sec is not None else end_sec)
    exclusions = []
    for item in (plan or {}).get("exclusions", []):
        if isinstance(item, dict):
            lo, hi = item.get("start"), item.get("end")
        else:
            lo, hi = item[:2]
        exclusions.append(TimeRange(seconds_to_ms(lo), seconds_to_ms(hi)))
    return EditPlan.create(duration_ms, seconds_to_ms(start_sec), seconds_to_ms(end_sec), exclusions)


def edit_plan_from_kept_ranges(
    start_sec: float, end_sec: float, ranges: Iterable[Iterable[float]]
) -> EditPlan:
    start_ms, end_ms = seconds_to_ms(start_sec), seconds_to_ms(end_sec)
    ordered = sorted(
        TimeRange(seconds_to_ms(item[0]), seconds_to_ms(item[1])) for item in ranges
    )
    exclusions: list[TimeRange] = []
    cursor = start_ms
    for kept in ordered:
        kept_start = max(start_ms, kept.start_ms)
        kept_end = min(end_ms, kept.end_ms)
        if kept_end <= kept_start:
            continue
        if kept_start > cursor:
            exclusions.append(TimeRange(cursor, kept_start))
        cursor = max(cursor, kept_end)
    if cursor < end_ms:
        exclusions.append(TimeRange(cursor, end_ms))
    return EditPlan.create(end_ms, start_ms, end_ms, exclusions)


def edit_plan_from_intuitive(state: dict) -> EditPlan:
    exclusions = [
        TimeRange(seconds_to_ms(item["start"]), seconds_to_ms(item["end"]))
        for item in state.get("exclusions", [])
    ]
    return EditPlan.create(
        seconds_to_ms(state["duration"]), seconds_to_ms(state["overall_start"]),
        seconds_to_ms(state["overall_end"]), exclusions,
    )


def edit_plan_to_legacy(plan: EditPlan) -> dict:
    return {
        "exclusions": [
            {"start": ms_to_seconds(item.start_ms), "end": ms_to_seconds(item.end_ms)}
            for item in plan.exclusions
        ]
    }
