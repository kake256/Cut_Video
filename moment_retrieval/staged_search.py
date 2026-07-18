"""Request ordering primitives for progressively rendered search results."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Iterator

from .search_service import (
    SearchHit,
    SearchService,
    SearchSnapshotChangedError,
    semantic_error_code,
)


class SearchRequestRegistry:
    """Track the newest search request independently for each browser session.

    Gradio can finish an older embedding request after a newer text search has
    already rendered.  A late result must therefore prove that it is still the
    newest request before it may update the UI.
    """

    def __init__(self, ttl_sec: float = 30 * 60) -> None:
        self.ttl_sec = max(1.0, float(ttl_sec))
        self._lock = threading.Lock()
        self._requests: dict[str, tuple[str, float]] = {}

    def begin(self, session_id: str | None) -> str:
        key = self._session_key(session_id)
        request_id = uuid.uuid4().hex
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            self._requests[key] = (request_id, now)
        return request_id

    def is_current(self, session_id: str | None, request_id: str) -> bool:
        key = self._session_key(session_id)
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            current = self._requests.get(key)
            return bool(current and current[0] == request_id)

    @staticmethod
    def _session_key(session_id: str | None) -> str:
        return str(session_id or "__anonymous__")

    def _prune(self, now: float) -> None:
        expired = [
            key for key, (_request_id, started) in self._requests.items()
            if now - started > self.ttl_sec
        ]
        for key in expired:
            self._requests.pop(key, None)


@dataclass(frozen=True)
class SearchStage:
    """One immutable progressive-search response."""

    request_id: str
    text_hits: tuple[SearchHit, ...]
    semantic_hits: tuple[SearchHit, ...] = ()
    semantic_status: str | None = None
    complete: bool = False
    publication_changed: bool = False

    @property
    def hits(self) -> tuple[SearchHit, ...]:
        return (*self.text_hits, *self.semantic_hits)


class StagedSearchCoordinator:
    """Run text and semantic stages without retaining a DB lease across yield."""

    def __init__(
        self,
        registry: SearchRequestRegistry | None = None,
        semantic_lock: threading.Lock | None = None,
    ) -> None:
        self.registry = registry or SearchRequestRegistry()
        self.semantic_lock = semantic_lock or threading.Lock()

    def search(
        self,
        query: str,
        *,
        session_id: str | None,
        connection_factory: Callable[[], object],
        service_factory: Callable[[object], SearchService],
        public_video_id: str | None,
        text_limit: int,
        semantic_limit: int,
        min_score: float,
    ) -> Iterator[SearchStage]:
        request_id = self.registry.begin(session_id)
        connection = connection_factory()
        try:
            service = service_factory(connection)
            text_hits, publication_id = service.search_text_stage(
                query,
                public_video_id=public_video_id,
                limit=text_limit,
            )
        finally:
            connection.close()

        text_stage = SearchStage(request_id, tuple(text_hits))
        if not self.registry.is_current(session_id, request_id):
            return
        yield text_stage

        if not self.registry.is_current(session_id, request_id):
            return
        connection = connection_factory()
        try:
            service = service_factory(connection)
            try:
                with self.semantic_lock:
                    if not self.registry.is_current(session_id, request_id):
                        return
                    semantic_hits = service.search_semantic_stage(
                        query,
                        expected_publication_id=publication_id,
                        public_video_id=public_video_id,
                        limit=semantic_limit,
                        min_score=min_score,
                    )
            except SearchSnapshotChangedError:
                final_stage = SearchStage(
                    request_id,
                    tuple(text_hits),
                    complete=True,
                    publication_changed=True,
                )
            else:
                final_stage = SearchStage(
                    request_id,
                    tuple(text_hits),
                    tuple(semantic_hits),
                    semantic_status=semantic_error_code(service.semantic_error),
                    complete=True,
                )
        finally:
            connection.close()

        if self.registry.is_current(session_id, request_id):
            yield final_stage
