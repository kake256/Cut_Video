from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable

from cut_clip import cut_clips

from . import db
from .application import ApplicationError, DOCUMENTS, DocumentRepository, SaveTicket
from .edit_domain import EffectiveExportPlan, make_effective_export_plan, ms_to_seconds
from .publication import private_source_fingerprint
from .subtitles import SubtitleValidationError, validate_srt_text


class SaveError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProbedArtifact:
    duration_ms: int
    tolerance_ms: int


def _positive_fraction(value: object) -> Fraction | None:
    try:
        parsed = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None
    return parsed if parsed > 0 else None


def probe_staged_video(path: Path) -> ProbedArtifact:
    """Probe the staged artifact and derive a one-frame/container tolerance."""
    command = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries",
        "format=duration:stream=duration,avg_frame_rate,r_frame_rate,time_base",
        "-of", "json", str(path),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, check=True,
        )
        payload = json.loads(completed.stdout)
        streams = payload.get("streams") or []
        stream = streams[0] if streams else {}
        # Validate the video timeline used by preview/SRT.  Container duration
        # can be longer because of AAC encoder priming even when video frames
        # exactly match the plan, so it is a fallback rather than the primary.
        raw_duration = stream.get("duration") or (payload.get("format") or {}).get("duration")
        duration_seconds = float(raw_duration)
        if not math.isfinite(duration_seconds) or duration_seconds <= 0:
            raise ValueError("invalid duration")
    except (
        OSError, TypeError, ValueError, KeyError, AttributeError,
        json.JSONDecodeError, subprocess.SubprocessError,
    ) as exc:
        raise SaveError("ARTIFACT_PROBE_FAILED: staged video could not be verified") from exc

    frame_rate = (
        _positive_fraction(stream.get("avg_frame_rate"))
        or _positive_fraction(stream.get("r_frame_rate"))
    )
    time_base = _positive_fraction(stream.get("time_base"))
    timing_units_ms: list[float] = []
    if frame_rate is not None:
        timing_units_ms.append(float(Fraction(1000, 1) / frame_rate))
    if time_base is not None:
        timing_units_ms.append(float(time_base * 1000))
    # ffprobe metadata without timing fields is unusual but valid.  Keep the
    # fallback explicit instead of pretending the planned duration is measured.
    tolerance_ms = math.ceil(max(timing_units_ms)) if timing_units_ms else 50
    return ProbedArtifact(round(duration_seconds * 1000), max(1, tolerance_ms))


def _read_source_fingerprint(path: Path) -> str:
    try:
        return private_source_fingerprint(path)
    except OSError as exc:
        raise SaveError("SOURCE_CHANGED: source is missing or unreadable") from exc


def _verify_source(path: Path, expected_fingerprint: str | None) -> None:
    if not expected_fingerprint:
        raise SaveError(
            "SOURCE_IDENTITY_UNKNOWN: expected source fingerprint is unavailable; "
            "relink or reopen the source"
        )
    if _read_source_fingerprint(path) != expected_fingerprint:
        raise SaveError("SOURCE_CHANGED: source no longer matches the open document")


def _resolve_expected_source_fingerprint(
    public_video_id: str, source_generation: str,
) -> str | None:
    conn = db.get_conn()
    try:
        db.init_db(conn)
        fingerprint = db.resolve_source_fingerprint(
            conn, public_video_id, source_generation, migrate_legacy=True,
        )
        conn.commit()
        return fingerprint
    finally:
        conn.close()


@dataclass(frozen=True)
class SaveResult:
    commit_id: str
    video_path: Path
    manifest_path: Path
    subtitle_path: Path | None
    ticket: SaveTicket


class ArtifactTransaction:
    def __init__(
        self, output_path: Path, source_path: Path, effective_plan: EffectiveExportPlan,
        precise: bool, expected_source_fingerprint: str,
        cancel_event: threading.Event | None = None,
        cutter: Callable = cut_clips,
        probe: Callable[[Path], ProbedArtifact] = probe_staged_video,
    ):
        self.output_path = Path(output_path)
        self.source_path = Path(source_path)
        self.effective_plan = effective_plan
        self.precise = precise
        self.expected_source_fingerprint = expected_source_fingerprint
        self.cancel_event = cancel_event or threading.Event()
        self.cutter = cutter
        self.probe = probe

    def execute(
        self, ticket: SaveTicket, subtitle_text: str | None = None,
        warnings: list[str] | None = None,
    ) -> SaveResult:
        _verify_source(self.source_path, self.expected_source_fingerprint)
        if subtitle_text is not None and not self.precise:
            raise SaveError("SRT_REQUIRES_PRECISE: subtitle export requires precise encoding")
        if self.output_path.exists():
            raise SaveError(f"output already exists: {self.output_path}")
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        commit_id = f"artifact_{uuid.uuid4().hex}"
        staging = Path(tempfile.mkdtemp(
            prefix=f".cut-video-{ticket.sequence}-{commit_id}-",
            dir=self.output_path.parent,
        ))
        staged_video = staging / self.output_path.name
        staged_srt = staging / f"{self.output_path.stem}.srt"
        staged_manifest = staging / f"{self.output_path.name}.manifest.json"
        journal = staging / "publish-journal.json"
        claim = self.output_path.with_name(f".{self.output_path.name}.cut-video-claim")
        try:
            journal.write_text(json.dumps({
                "schema_version": 2,
                "output_path": str(self.output_path.resolve()),
                "subtitle_path": str(self.output_path.with_suffix(".srt").resolve()),
                "manifest_path": str(self.output_path.with_suffix(
                    self.output_path.suffix + ".manifest.json"
                ).resolve()),
                "claim_path": str(claim.resolve()),
                "commit_id": commit_id,
            }), encoding="utf-8")
            try:
                with claim.open("x", encoding="utf-8", newline="") as handle:
                    json.dump({
                        "schema_version": 1,
                        "commit_id": commit_id,
                        "output_name": self.output_path.name,
                    }, handle)
            except FileExistsError as exc:
                raise SaveError(f"output is already being saved: {self.output_path}") from exc
            if self.output_path.exists():
                raise SaveError(f"output already exists: {self.output_path}")
            if self.cancel_event.is_set():
                raise SaveError("save was cancelled")
            ranges = [
                [ms_to_seconds(item.start_ms), ms_to_seconds(item.end_ms)]
                for item in self.effective_plan.plan.kept_ranges
            ]
            self.cutter(
                self.source_path, ranges, staged_video, precise=self.precise,
                duration=ms_to_seconds(self.effective_plan.plan.source_duration_ms),
                pad=0.0, cancel_event=self.cancel_event,
            )
            if self.cancel_event.is_set():
                raise SaveError("save was cancelled")
            probed = self.probe(staged_video)
            if probed.duration_ms <= 0 or probed.tolerance_ms < 0:
                raise SaveError("ARTIFACT_PROBE_FAILED: staged video timing is invalid")
            planned_duration_ms = self.effective_plan.timeline_map.result_duration_ms
            duration_delta_ms = probed.duration_ms - planned_duration_ms
            # Each independently encoded kept range can round its final frame.
            # Scale only the whole-artifact duration check; individual SRT cues
            # still use the single probed frame/container tolerance below.
            duration_tolerance_ms = (
                probed.tolerance_ms
                * max(1, len(self.effective_plan.plan.kept_ranges))
            )
            duration_matches_plan = abs(duration_delta_ms) <= duration_tolerance_ms
            if self.precise and not duration_matches_plan:
                raise SaveError(
                    "ARTIFACT_DURATION_MISMATCH: staged video duration does not match the edit plan"
                )
            manifest_warnings = list(warnings or [])
            if not self.precise and not duration_matches_plan:
                # Stream-copy mode is intentionally approximate at keyframes.
                # Preserve compatibility, but make the measured drift explicit.
                manifest_warnings.append(
                    "FAST_MODE_DURATION_DRIFT: staged video differs from the planned "
                    f"duration by {duration_delta_ms} ms"
                )
            subtitle_path = None
            if subtitle_text is not None:
                try:
                    validate_srt_text(
                        subtitle_text, probed.duration_ms, probed.tolerance_ms,
                    )
                except SubtitleValidationError as exc:
                    raise SaveError(f"SUBTITLE_INVALID: {exc}") from exc
                staged_srt.write_text(subtitle_text, encoding="utf-8")
                subtitle_path = self.output_path.with_suffix(".srt")
            manifest = {
                "schema_version": 1,
                "commit_id": commit_id,
                "document_id": ticket.document_id,
                "source_generation": ticket.source_generation,
                "save_sequence": ticket.sequence,
                "plan_hash": ticket.plan_hash,
                "video": self.output_path.name,
                "subtitle": subtitle_path.name if subtitle_path else None,
                "precise": self.precise,
                "planned_duration_ms": planned_duration_ms,
                "probed_duration_ms": probed.duration_ms,
                "duration_tolerance_ms": duration_tolerance_ms,
                "subtitle_tolerance_ms": probed.tolerance_ms,
                "duration_delta_ms": duration_delta_ms,
                "duration_matches_plan": duration_matches_plan,
                "warnings": manifest_warnings,
            }
            staged_manifest.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if self.output_path.exists():
                raise SaveError(f"output already exists: {self.output_path}")
            # Keep this as the final operation before the first public replace.
            # The manifest remains the commit marker for crash recovery.
            _verify_source(self.source_path, self.expected_source_fingerprint)
            os.replace(staged_video, self.output_path)
            if subtitle_path:
                os.replace(staged_srt, subtitle_path)
            manifest_path = self.output_path.with_suffix(self.output_path.suffix + ".manifest.json")
            os.replace(staged_manifest, manifest_path)
            return SaveResult(commit_id, self.output_path, manifest_path, subtitle_path, ticket)
        except Exception:
            # A manifest is the commit marker. Files without it are rollback candidates.
            manifest_path = self.output_path.with_suffix(self.output_path.suffix + ".manifest.json")
            if not manifest_path.exists():
                self.output_path.unlink(missing_ok=True)
                self.output_path.with_suffix(".srt").unlink(missing_ok=True)
            raise
        finally:
            _remove_matching_claim(claim, commit_id, self.output_path.name)
            shutil.rmtree(staging, ignore_errors=True)


def save_document(
    document_id: str, source_path: Path, output_path: Path, precise: bool,
    pad_before_ms: int = 0, pad_after_ms: int = 0,
    subtitle_text: str | None = None, warnings: list[str] | None = None,
    cancel_event: threading.Event | None = None,
    documents: DocumentRepository = DOCUMENTS,
    cutter: Callable = cut_clips,
    probe: Callable[[Path], ProbedArtifact] = probe_staged_video,
    source_fingerprint_resolver: Callable[[str, str], str | None] = (
        _resolve_expected_source_fingerprint
    ),
) -> SaveResult:
    document = documents.get(document_id)
    if not document or document.closed:
        raise ApplicationError("document is closed or missing")
    expected_fingerprint = document.expected_source_fingerprint
    if expected_fingerprint is None:
        expected_fingerprint = source_fingerprint_resolver(
            document.public_video_id, document.source_generation,
        )
    # Validate before creating a SaveTicket, staging directory, or output claim.
    _verify_source(Path(source_path), expected_fingerprint)
    ticket = documents.begin_save(document_id, expected_fingerprint)
    recover_artifact_transactions(Path(output_path).parent)
    effective = make_effective_export_plan(ticket.snapshot, pad_before_ms, pad_after_ms)
    effective_precise = bool(precise or subtitle_text is not None)
    transaction = ArtifactTransaction(
        output_path, source_path, effective, effective_precise,
        expected_fingerprint, cancel_event, cutter, probe,
    )
    result = transaction.execute(ticket, subtitle_text, warnings)
    documents.complete_save(ticket, result.commit_id)
    return result


def recover_artifact_transactions(output_root: Path) -> list[Path]:
    """Rollback crash-left staging jobs unless their commit manifest exists."""
    output_root = Path(output_root).resolve()
    recovered = []
    if not output_root.exists():
        return recovered
    for staging in output_root.glob(".cut-video-*"):
        if not staging.is_dir():
            continue
        journal = staging / "publish-journal.json"
        try:
            payload = json.loads(journal.read_text(encoding="utf-8"))
            paths = _validated_recovery_paths(output_root, staging.resolve(), payload)
            if paths is not None:
                output, subtitle, manifest, claim, commit_id = paths
                if _claim_matches(claim, commit_id, output.name):
                    if not manifest.exists():
                        output.unlink(missing_ok=True)
                        subtitle.unlink(missing_ok=True)
                    _remove_matching_claim(claim, commit_id, output.name)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            pass
        shutil.rmtree(staging, ignore_errors=True)
        recovered.append(staging)
    return recovered


def _is_direct_child(path: Path, parent: Path) -> bool:
    try:
        return path.resolve().parent == parent.resolve()
    except OSError:
        return False


def _valid_commit_id(value: object) -> str | None:
    text = str(value or "")
    prefix = "artifact_"
    suffix = text[len(prefix):] if text.startswith(prefix) else ""
    if len(suffix) != 32 or any(ch not in "0123456789abcdef" for ch in suffix):
        return None
    return text


def _validated_recovery_paths(
    output_root: Path, staging: Path, payload: dict,
) -> tuple[Path, Path, Path, Path, str] | None:
    """Accept only journals tied to a claimed output in this directory.

    Version 1 journals did not carry a claim marker and cannot prove that an
    existing file belongs to the interrupted transaction. Their staging
    directory is removed conservatively, while published paths are untouched.
    """
    if payload.get("schema_version") != 2:
        return None
    commit_id = _valid_commit_id(payload.get("commit_id"))
    if commit_id is None or f"-{commit_id}-" not in staging.name:
        return None
    output = Path(payload["output_path"]).resolve()
    subtitle = Path(payload["subtitle_path"]).resolve()
    manifest = Path(payload["manifest_path"]).resolve()
    claim = Path(payload["claim_path"]).resolve()
    if not all(_is_direct_child(path, output_root) for path in (
        output, subtitle, manifest, claim,
    )):
        return None
    if subtitle != output.with_suffix(".srt"):
        return None
    if manifest != output.with_suffix(output.suffix + ".manifest.json"):
        return None
    if claim != output.with_name(f".{output.name}.cut-video-claim"):
        return None
    return output, subtitle, manifest, claim, commit_id


def _claim_matches(claim: Path, commit_id: str, output_name: str) -> bool:
    try:
        payload = json.loads(claim.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return (
        payload.get("schema_version") == 1
        and payload.get("commit_id") == commit_id
        and payload.get("output_name") == output_name
    )


def _remove_matching_claim(claim: Path, commit_id: str, output_name: str) -> None:
    if _claim_matches(claim, commit_id, output_name):
        claim.unlink(missing_ok=True)
