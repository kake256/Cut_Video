from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any

from .edit_domain import EditHistory, EditPlan, TimeRange


class ApplicationError(RuntimeError):
    code = "APPLICATION_ERROR"


class RevisionConflict(ApplicationError):
    code = "REVISION_CONFLICT"


@dataclass(frozen=True)
class EditorDocument:
    document_id: str
    public_video_id: str
    source_generation: str
    history: EditHistory
    revision: int = 0
    closed: bool = False
    latest_save_sequence: int = 0
    latest_completed_save_sequence: int = 0
    command_results: tuple[tuple[str, int], ...] = ()

    @property
    def current(self) -> EditPlan:
        return self.history.current


@dataclass(frozen=True)
class SaveTicket:
    document_id: str
    source_generation: str
    sequence: int
    snapshot: EditPlan
    plan_hash: str


class DocumentRepository:
    def __init__(self, command_cache_size: int = 128):
        self._documents: dict[str, EditorDocument] = {}
        self._lock = threading.RLock()
        self.command_cache_size = command_cache_size

    def open(self, public_video_id: str, source_generation: str, plan: EditPlan) -> EditorDocument:
        document = EditorDocument(
            f"doc_{uuid.uuid4().hex}", public_video_id, source_generation,
            EditHistory.create(plan),
        )
        with self._lock:
            self._documents[document.document_id] = document
        return document

    def get(self, document_id: str) -> EditorDocument | None:
        with self._lock:
            return self._documents.get(document_id)

    def close(self, document_id: str) -> None:
        with self._lock:
            document = self._documents.get(document_id)
            if document:
                self._documents[document_id] = replace(document, closed=True)

    def apply(
        self, document_id: str, command_id: str, expected_revision: int,
        command: str, payload: dict[str, Any] | None = None,
    ) -> EditorDocument:
        payload = payload or {}
        with self._lock:
            document = self._documents.get(document_id)
            if not document or document.closed:
                raise ApplicationError("document is closed or missing")
            cached = dict(document.command_results)
            if command_id in cached:
                return document
            if document.revision != expected_revision:
                raise RevisionConflict(
                    f"expected revision {expected_revision}, current {document.revision}"
                )
            history = document.history
            plan = history.current
            if command == "set_overall":
                next_plan = plan.with_overall(int(payload["start_ms"]), int(payload["end_ms"]))
                history = history.apply(next_plan)
            elif command == "add_exclusion":
                next_plan = plan.add_exclusion(int(payload["start_ms"]), int(payload["end_ms"]))
                history = history.apply(next_plan)
            elif command == "undo":
                history = history.undo_once()
            elif command == "redo":
                history = history.redo_once()
            elif command == "mark_clean":
                history = history.mark_clean()
            else:
                raise ApplicationError(f"unknown editor command: {command}")
            results = OrderedDict(document.command_results)
            results[command_id] = document.revision + 1
            while len(results) > self.command_cache_size:
                results.popitem(last=False)
            updated = replace(
                document, history=history, revision=document.revision + 1,
                command_results=tuple(results.items()),
            )
            self._documents[document_id] = updated
            return updated

    def begin_save(self, document_id: str) -> SaveTicket:
        with self._lock:
            document = self._documents.get(document_id)
            if not document or document.closed:
                raise ApplicationError("document is closed or missing")
            sequence = document.latest_save_sequence + 1
            updated = replace(document, latest_save_sequence=sequence)
            self._documents[document_id] = updated
            return SaveTicket(
                document_id, document.source_generation, sequence,
                document.current, document.current.semantic_signature,
            )

    def sync_adapter_plan(self, document_id: str, plan: EditPlan, *, clean: bool = False) -> EditorDocument:
        """Migration seam while Gradio still serializes view state client-side."""
        with self._lock:
            document = self._documents.get(document_id)
            if not document or document.closed:
                raise ApplicationError("document is closed or missing")
            history = document.history.apply(plan)
            if clean:
                history = history.mark_clean()
            updated = replace(document, history=history)
            self._documents[document_id] = updated
            return updated

    def complete_save(self, ticket: SaveTicket, artifact_commit_id: str) -> EditorDocument | None:
        if not artifact_commit_id:
            raise ApplicationError("artifact commit ID is required")
        with self._lock:
            document = self._documents.get(ticket.document_id)
            if not document or document.closed or document.source_generation != ticket.source_generation:
                return None
            if ticket.sequence < document.latest_completed_save_sequence:
                return document
            history = replace(document.history, clean_reference=ticket.snapshot)
            updated = replace(
                document, history=history,
                latest_completed_save_sequence=ticket.sequence,
            )
            self._documents[ticket.document_id] = updated
            return updated


DOCUMENTS = DocumentRepository()
