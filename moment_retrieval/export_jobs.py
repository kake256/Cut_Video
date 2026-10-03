from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, replace
from enum import Enum


class ExportStage(str, Enum):
    QUEUED = "queued"
    VALIDATING = "validating"
    JOINING = "joining"
    RENDERING = "rendering"
    PROBING = "probing"
    PUBLISHING = "publishing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL_STAGES = {
    ExportStage.COMPLETED,
    ExportStage.FAILED,
    ExportStage.CANCELLED,
}

_STAGE_PROGRESS = {
    ExportStage.QUEUED: 0,
    ExportStage.VALIDATING: 5,
    ExportStage.JOINING: 15,
    ExportStage.RENDERING: 55,
    ExportStage.PROBING: 85,
    ExportStage.PUBLISHING: 95,
    ExportStage.COMPLETED: 100,
    ExportStage.FAILED: 0,
    ExportStage.CANCELLED: 0,
}


@dataclass(frozen=True)
class ExportJobSnapshot:
    job_id: str
    stage: ExportStage = ExportStage.QUEUED
    progress: int = 0
    cancel_requested: bool = False
    failed_stage: ExportStage | None = None
    error_code: str | None = None
    message: str = ""

    @property
    def terminal(self) -> bool:
        return self.stage in _TERMINAL_STAGES


class ExportJobRegistry:
    """Thread-safe, content-free state for local export jobs.

    Video names, paths and transcript text deliberately do not belong in this
    state.  UI adapters can map the structured stage/error code to localized
    text without exposing private source details.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, ExportJobSnapshot] = {}
        self._cancel_events: dict[str, threading.Event] = {}

    def create(self, *, job_id: str | None = None) -> ExportJobSnapshot:
        identifier = str(job_id or f"export_{uuid.uuid4().hex}")
        if not identifier or len(identifier) > 96:
            raise ValueError("export job ID is invalid")
        with self._lock:
            if identifier in self._states:
                raise ValueError("export job ID already exists")
            snapshot = ExportJobSnapshot(identifier)
            self._states[identifier] = snapshot
            self._cancel_events[identifier] = threading.Event()
            return snapshot

    def get(self, job_id: str) -> ExportJobSnapshot | None:
        with self._lock:
            return self._states.get(str(job_id))

    def cancel_event(self, job_id: str) -> threading.Event:
        with self._lock:
            try:
                return self._cancel_events[str(job_id)]
            except KeyError as exc:
                raise KeyError("export job does not exist") from exc

    def transition(
        self,
        job_id: str,
        stage: ExportStage | str,
        *,
        progress: int | None = None,
        message: str = "",
    ) -> ExportJobSnapshot:
        next_stage = ExportStage(stage)
        if next_stage in _TERMINAL_STAGES:
            raise ValueError("use complete, fail or acknowledge_cancel for a terminal stage")
        with self._lock:
            current = self._require(job_id)
            if current.terminal:
                return current
            next_progress = _STAGE_PROGRESS[next_stage] if progress is None else int(progress)
            next_progress = max(current.progress, min(99, max(0, next_progress)))
            updated = replace(
                current,
                stage=next_stage,
                progress=next_progress,
                message=_safe_message(message),
            )
            self._states[current.job_id] = updated
            return updated

    def request_cancel(self, job_id: str) -> ExportJobSnapshot:
        with self._lock:
            current = self._require(job_id)
            if current.terminal:
                return current
            self._cancel_events[current.job_id].set()
            updated = replace(current, cancel_requested=True)
            self._states[current.job_id] = updated
            return updated

    def acknowledge_cancel(self, job_id: str) -> ExportJobSnapshot:
        with self._lock:
            current = self._require(job_id)
            if current.terminal:
                return current
            updated = replace(
                current,
                stage=ExportStage.CANCELLED,
                failed_stage=current.stage,
                error_code="EXPORT_CANCELLED",
                message="",
            )
            self._states[current.job_id] = updated
            return updated

    def fail(
        self, job_id: str, error_code: str, *, message: str = "",
    ) -> ExportJobSnapshot:
        code = str(error_code or "EXPORT_FAILED").strip().upper()[:80]
        with self._lock:
            current = self._require(job_id)
            if current.terminal:
                return current
            updated = replace(
                current,
                stage=ExportStage.FAILED,
                failed_stage=current.stage,
                error_code=code,
                message=_safe_message(message),
            )
            self._states[current.job_id] = updated
            return updated

    def complete(self, job_id: str) -> ExportJobSnapshot:
        with self._lock:
            current = self._require(job_id)
            if current.terminal:
                return current
            updated = replace(
                current,
                stage=ExportStage.COMPLETED,
                progress=100,
                error_code=None,
                failed_stage=None,
                message="",
            )
            self._states[current.job_id] = updated
            return updated

    def remove(self, job_id: str) -> None:
        with self._lock:
            self._states.pop(str(job_id), None)
            self._cancel_events.pop(str(job_id), None)

    def _require(self, job_id: str) -> ExportJobSnapshot:
        try:
            return self._states[str(job_id)]
        except KeyError as exc:
            raise KeyError("export job does not exist") from exc


def _safe_message(value: object) -> str:
    # Messages must already be content-free at call sites.  Length limiting
    # prevents an adapter from accidentally retaining a large ffmpeg stderr or
    # transcript fragment in long-lived UI state.
    return str(value or "").strip()[:500]


EXPORT_JOBS = ExportJobRegistry()
