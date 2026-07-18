from __future__ import annotations

import json
import threading
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence

import numpy as np

from .publication import (
    NoActivePublicationError,
    PublicationSnapshot,
    release_snapshot,
    resolve_snapshot,
)


TEXT_KIND = "text"
SEMANTIC_KIND = "semantic"
SEMANTIC_PENDING = "SEMANTIC_PENDING"
SEMANTIC_UNAVAILABLE = "SEMANTIC_UNAVAILABLE"
SEARCH_STALE = "SEARCH_STALE"


class SemanticSearchError(RuntimeError):
    code = SEMANTIC_UNAVAILABLE


class SemanticPendingError(SemanticSearchError):
    code = SEMANTIC_PENDING


class SemanticUnavailableError(SemanticSearchError):
    code = SEMANTIC_UNAVAILABLE


class SearchSnapshotChangedError(RuntimeError):
    """Raised when a staged semantic response no longer matches its text stage."""

    code = SEARCH_STALE


@dataclass(frozen=True)
class SemanticSearchScope:
    index_path: Path
    allowed_revisions: frozenset[str] | None


def resolve_semantic_scope(
    snapshot: PublicationSnapshot | None,
    public_video_id: str | None,
    *,
    legacy_index_path: Path,
    generations_dir: Path,
) -> SemanticSearchScope:
    """Resolve one publication-consistent semantic search scope.

    ``allowed_revisions=None`` is reserved for databases that predate search
    publications.  Once a publication snapshot exists, an empty covered set is
    a state (pending/unavailable), never permission to search every revision.
    """
    legacy_index_path = Path(legacy_index_path)
    generations_dir = Path(generations_dir)
    if snapshot is None:
        if not legacy_index_path.exists():
            raise SemanticUnavailableError("semantic index is unavailable")
        return SemanticSearchScope(legacy_index_path, None)

    relevant_members = tuple(
        member for member in snapshot.members
        if public_video_id is None or member.public_video_id == public_video_id
    )
    if not relevant_members:
        raise SemanticUnavailableError("video is not present in the active publication")
    if snapshot.generation_id is None:
        raise SemanticPendingError("semantic index publication is pending")

    covered_revisions = frozenset(
        member.transcript_revision
        for member in relevant_members
        if member.semantic_covered and member.transcript_revision
    )
    if not covered_revisions:
        raise SemanticPendingError("semantic coverage is pending for the selected scope")

    if snapshot.generation_id == "legacy_current":
        index_path = legacy_index_path
    else:
        index_path = generations_dir / snapshot.generation_id / "vectors.faiss"
    if not index_path.exists():
        raise SemanticUnavailableError("semantic index artifact is unavailable")
    return SemanticSearchScope(index_path, covered_revisions)


def semantic_error_code(error: Exception | None) -> str | None:
    if error is None:
        return None
    return str(getattr(error, "code", SEMANTIC_UNAVAILABLE))


def retrieve_semantic_hits(
    conn: sqlite3.Connection,
    snapshot: PublicationSnapshot | None,
    query: str,
    public_video_id: str | None,
    limit: int,
    min_score: float,
    *,
    legacy_index_path: Path,
    generations_dir: Path,
    encode_query: Callable[[str], np.ndarray],
    index_loader: Callable[[Path, int], object],
    start_sec: float | None = None,
    end_sec: float | None = None,
) -> list[SearchHit]:
    """Adapt the publication-aware vector search to canonical ``SearchHit`` values."""
    from .search import search_semantic_chunks

    storage_id = None
    resolved_public_id = public_video_id
    if public_video_id:
        video = conn.execute(
            "SELECT video_id, COALESCE(public_video_id, video_id) AS public_video_id "
            "FROM videos WHERE video_id = ? OR public_video_id = ?",
            (public_video_id, public_video_id),
        ).fetchone()
        if video:
            storage_id = str(video[0])
            resolved_public_id = str(video[1])
        else:
            # Keep an unknown scoped ID scoped; never turn it into all-video search.
            storage_id = public_video_id

    semantic_scope = resolve_semantic_scope(
        snapshot,
        resolved_public_id,
        legacy_index_path=legacy_index_path,
        generations_dir=generations_dir,
    )
    query_vec = encode_query(query)
    vindex = index_loader(semantic_scope.index_path, int(query_vec.shape[1]))
    rows = search_semantic_chunks(
        conn,
        vindex,
        query,
        query_vec,
        top_k=int(limit),
        min_score=float(min_score),
        video_id=storage_id,
        start_sec=start_sec,
        end_sec=end_sec,
        allowed_revisions=semantic_scope.allowed_revisions,
    )
    hits = []
    for row in rows:
        identity = conn.execute(
            "SELECT COALESCE(public_video_id, video_id) FROM videos WHERE video_id = ?",
            (row["video_id"],),
        ).fetchone()
        result_public_id = str(identity[0]) if identity else str(row["video_id"])
        start_ms = round(row["start"] * 1000)
        end_ms = round(row["end"] * 1000)
        hits.append(SearchHit(
            hit_id=f"semantic:{result_public_id}:{row['chunk_id']}",
            public_video_id=result_public_id,
            kind=SEMANTIC_KIND,
            evidence=EvidenceSpan(start_ms, end_ms),
            suggested_start_ms=start_ms,
            suggested_end_ms=end_ms,
            text=row["text"],
            semantic_score=float(row["score"]),
        ))
    return hits


@dataclass(frozen=True)
class NormalizedText:
    text: str
    source_offsets: tuple[int, ...]


class TextNormalizer:
    """Versioned query/transcript normalization with source projection."""

    version = "text-normalizer-v1"

    @staticmethod
    def _kana(char: str) -> str:
        codepoint = ord(char)
        return chr(codepoint - 0x60) if 0x30A1 <= codepoint <= 0x30F6 else char

    @staticmethod
    def _is_cjk(char: str) -> bool:
        codepoint = ord(char)
        return (
            0x3040 <= codepoint <= 0x30FF
            or 0x3400 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
        )

    def normalize(self, value: str, tier: str = "base") -> NormalizedText:
        chars: list[str] = []
        offsets: list[int] = []
        previous_space = False
        for source_index, source_char in enumerate(value or ""):
            expanded = unicodedata.normalize("NFKC", source_char).casefold()
            for char in expanded:
                if tier in {"kana", "cjk_compact"}:
                    char = self._kana(char)
                if char.isspace():
                    if not previous_space and chars:
                        chars.append(" ")
                        offsets.append(source_index)
                    previous_space = True
                else:
                    chars.append(char)
                    offsets.append(source_index)
                    previous_space = False
        if chars and chars[-1] == " ":
            chars.pop()
            offsets.pop()
        if tier == "cjk_compact":
            compact_chars: list[str] = []
            compact_offsets: list[int] = []
            for index, char in enumerate(chars):
                if char == " ":
                    before = chars[index - 1] if index else ""
                    after = chars[index + 1] if index + 1 < len(chars) else ""
                    if self._is_cjk(before) and self._is_cjk(after):
                        continue
                compact_chars.append(char)
                compact_offsets.append(offsets[index])
            chars, offsets = compact_chars, compact_offsets
        return NormalizedText("".join(chars), tuple(offsets))


@dataclass(frozen=True)
class EvidenceSpan:
    start_ms: int
    end_ms: int
    segment_id: int | None = None
    char_start: int | None = None
    char_end: int | None = None


@dataclass(frozen=True)
class SearchHit:
    hit_id: str
    public_video_id: str
    kind: str
    evidence: EvidenceSpan
    suggested_start_ms: int
    suggested_end_ms: int
    text: str
    match_tier: str | None = None
    semantic_score: float | None = None
    occurrence_count: int = 1

    def to_legacy_dict(self) -> dict:
        return {
            "hit_id": self.hit_id,
            "video_id": self.public_video_id,
            "start": self.suggested_start_ms / 1000,
            "end": self.suggested_end_ms / 1000,
            "evidence_start": self.evidence.start_ms / 1000,
            "evidence_end": self.evidence.end_ms / 1000,
            "score": self.semantic_score,
            "match_type": "文字一致" if self.kind == TEXT_KIND else "意味検索",
            "text": self.text,
            "match_tier": self.match_tier,
            "occurrence_count": self.occurrence_count,
        }


@dataclass(frozen=True)
class LegacyTranscriptRef:
    public_video_id: str
    temporary_source_token: str
    legacy_revision_token: str
    lock_epoch: int


@dataclass(frozen=True)
class SearchSnapshot:
    refs: tuple[LegacyTranscriptRef, ...]
    lock_epoch: int


_LEGACY_SEARCH_LOCK = threading.RLock()


class SemanticRetriever(Protocol):
    def __call__(
        self, query: str, public_video_id: str | None, limit: int, min_score: float
    ) -> Iterable[SearchHit]: ...


def _word_evidence(row: dict, char_start: int, char_end: int) -> tuple[int, int]:
    words = []
    try:
        words = json.loads(row.get("words_json") or "[]")
    except (TypeError, ValueError):
        pass
    valid = [
        word for word in words
        if isinstance(word, dict) and word.get("word")
        and word.get("start") is not None and word.get("end") is not None
    ]
    if not valid:
        return round(float(row["start_sec"]) * 1000), round(float(row["end_sec"]) * 1000)
    cursor = 0
    selected = []
    for word in valid:
        word_start = cursor
        cursor += len(str(word["word"]))
        if cursor > char_start and word_start < char_end:
            selected.append(word)
    if not selected:
        return round(float(row["start_sec"]) * 1000), round(float(row["end_sec"]) * 1000)
    return round(float(selected[0]["start"]) * 1000), round(float(selected[-1]["end"]) * 1000)


class SearchService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        semantic_retriever: SemanticRetriever | None = None,
        normalizer: TextNormalizer | None = None,
    ) -> None:
        self.conn = conn
        self.semantic_retriever = semantic_retriever
        self.normalizer = normalizer or TextNormalizer()
        self.snapshot: SearchSnapshot | None = None
        self.publication_snapshot: PublicationSnapshot | None = None
        self.semantic_error: Exception | None = None

    def _capture_snapshot(self) -> SearchSnapshot:
        epoch = int(self.conn.execute("PRAGMA data_version").fetchone()[0])
        rows = self.conn.execute(
            "SELECT public_video_id, source_generation FROM videos "
            "WHERE asr_complete = 1 ORDER BY public_video_id"
        ).fetchall()
        refs = tuple(
            LegacyTranscriptRef(
                public_video_id=str(row[0]),
                temporary_source_token=str(row[1] or "unknown"),
                legacy_revision_token=f"legacy:{row[0]}:{epoch}",
                lock_epoch=epoch,
            )
            for row in rows if row[0]
        )
        return SearchSnapshot(refs, epoch)

    def _open_search_snapshot(self) -> str | None:
        self.semantic_error = None
        self.snapshot = self._capture_snapshot()
        try:
            self.publication_snapshot = resolve_snapshot(self.conn)
        except NoActivePublicationError:
            self.publication_snapshot = None
        return (
            self.publication_snapshot.publication_id
            if self.publication_snapshot is not None else None
        )

    def _close_search_snapshot(self) -> None:
        if self.publication_snapshot:
            release_snapshot(self.conn, self.publication_snapshot)

    def _resolve_scope(self, identifier: str | None) -> tuple[str | None, str | None]:
        if not identifier:
            return None, None
        row = self.conn.execute(
            "SELECT video_id, public_video_id FROM videos "
            "WHERE video_id = ? OR public_video_id = ?",
            (identifier, identifier),
        ).fetchone()
        if row:
            return str(row[0]), str(row[1] or row[0])
        return identifier, identifier

    def _transcript_rows(
        self, storage_id: str | None, start_ms: int | None, end_ms: int | None
    ) -> list[dict]:
        where = ["v.asr_complete = 1", "trim(COALESCE(s.text, '')) <> ''"]
        values: list[object] = []
        if storage_id:
            where.append("s.video_id = ?")
            values.append(storage_id)
        if start_ms is not None:
            where.append("s.end_sec > ?")
            values.append(start_ms / 1000)
        if end_ms is not None:
            where.append("s.start_sec < ?")
            values.append(end_ms / 1000)
        if self.publication_snapshot is not None:
            revisions = [
                member.transcript_revision
                for member in self.publication_snapshot.members
                if member.transcript_revision
            ]
            if revisions:
                where.append(
                    "s.transcript_revision IN ("
                    + ",".join("?" for _ in revisions)
                    + ")"
                )
                values.extend(revisions)
            else:
                # An immutable empty publication means exactly no searchable
                # transcript. It must never broaden into the legacy all-row
                # fallback, which is reserved for libraries with no publication.
                where.append("0 = 1")
        rows = self.conn.execute(
            "SELECT s.*, COALESCE(v.public_video_id, v.video_id) AS public_video_id "
            "FROM asr_segments s JOIN videos v ON v.video_id = s.video_id WHERE "
            + " AND ".join(where) + " ORDER BY public_video_id, s.start_sec, s.segment_id",
            values,
        ).fetchall()
        return [dict(row) for row in rows]

    def text_search(
        self,
        query: str,
        *,
        public_video_id: str | None = None,
        limit: int = 20,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[SearchHit]:
        storage_id, _ = self._resolve_scope(public_video_id)
        tiers = ("base", "kana", "cjk_compact")
        normalized_queries = {tier: self.normalizer.normalize(query, tier).text for tier in tiers}
        if not normalized_queries["base"]:
            raise ValueError("query is empty after normalization")
        hits: list[SearchHit] = []
        seen: set[tuple[str, int, int]] = set()
        rows = self._transcript_rows(storage_id, start_ms, end_ms)
        groups: list[list[dict]] = []
        for row in rows:
            if not groups:
                groups.append([row])
                continue
            previous = groups[-1][-1]
            gap_ms = round((float(row["start_sec"]) - float(previous["end_sec"])) * 1000)
            if row["public_video_id"] == previous["public_video_id"] and 0 <= gap_ms <= 2000:
                groups[-1].append(row)
            else:
                groups.append([row])
        for group in groups:
            parts = [str(row.get("text") or "") for row in group]
            source = " ".join(parts)
            boundaries = []
            cursor = 0
            for row, part in zip(group, parts):
                boundaries.append((cursor, cursor + len(part), row))
                cursor += len(part) + 1
            for tier_index, tier in enumerate(tiers):
                needle = normalized_queries[tier]
                normalized = self.normalizer.normalize(source, tier)
                offset = 0
                occurrence = 0
                while needle and (found := normalized.text.find(needle, offset)) >= 0:
                    occurrence += 1
                    source_start = normalized.source_offsets[found]
                    source_end = normalized.source_offsets[found + len(needle) - 1] + 1
                    touched = [item for item in boundaries if item[1] > source_start and item[0] < source_end]
                    first_offset, _, first_row = touched[0]
                    last_offset, _, last_row = touched[-1]
                    first_start, _ = _word_evidence(
                        first_row, max(0, source_start - first_offset),
                        min(len(str(first_row.get("text") or "")), source_end - first_offset),
                    )
                    _, last_end = _word_evidence(
                        last_row, max(0, source_start - last_offset),
                        min(len(str(last_row.get("text") or "")), source_end - last_offset),
                    )
                    evidence_start, evidence_end = first_start, last_end
                    identity = (str(first_row["public_video_id"]), evidence_start, evidence_end)
                    if identity not in seen:
                        segment_id = int(first_row["segment_id"])
                        hits.append(SearchHit(
                            hit_id=f"text:{first_row['public_video_id']}:{segment_id}:{source_start}:{tier_index}",
                            public_video_id=str(first_row["public_video_id"]),
                            kind=TEXT_KIND,
                            evidence=EvidenceSpan(
                                evidence_start, evidence_end, segment_id, source_start, source_end
                            ),
                            suggested_start_ms=round(float(first_row["start_sec"]) * 1000),
                            suggested_end_ms=round(float(last_row["end_sec"]) * 1000),
                            text=source,
                            match_tier=tier,
                            occurrence_count=occurrence,
                        ))
                        seen.add(identity)
                    offset = found + max(1, len(needle))
                if occurrence:
                    break
        tier_rank = {tier: rank for rank, tier in enumerate(tiers)}
        hits.sort(key=lambda hit: (
            tier_rank.get(hit.match_tier or "", 99),
            hit.evidence.end_ms - hit.evidence.start_ms,
            hit.public_video_id,
            hit.evidence.start_ms,
            hit.hit_id,
        ))
        return hits[:max(0, int(limit))]

    @staticmethod
    def _semantic_nms(hits: Sequence[SearchHit], limit: int) -> list[SearchHit]:
        ordered = sorted(hits, key=lambda hit: (
            -(hit.semantic_score or 0.0), hit.public_video_id,
            hit.evidence.start_ms, hit.hit_id,
        ))
        accepted: list[SearchHit] = []
        for candidate in ordered:
            duration = candidate.evidence.end_ms - candidate.evidence.start_ms
            if duration <= 0:
                continue
            overlaps = False
            for existing in accepted:
                if existing.public_video_id != candidate.public_video_id:
                    continue
                overlap = max(0, min(existing.evidence.end_ms, candidate.evidence.end_ms)
                              - max(existing.evidence.start_ms, candidate.evidence.start_ms))
                denominator = min(duration, existing.evidence.end_ms - existing.evidence.start_ms)
                if denominator > 0 and overlap / denominator >= 0.30:
                    overlaps = True
                    break
            if not overlaps:
                accepted.append(candidate)
                if len(accepted) >= limit:
                    break
        return accepted

    def search(
        self,
        query: str,
        *,
        public_video_id: str | None = None,
        text_limit: int = 20,
        semantic_limit: int = 5,
        min_score: float = 0.55,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> tuple[list[SearchHit], list[SearchHit]]:
        with _LEGACY_SEARCH_LOCK:
            self.semantic_error = None
            self.snapshot = self._capture_snapshot()
            try:
                self.publication_snapshot = resolve_snapshot(self.conn)
            except NoActivePublicationError:
                self.publication_snapshot = None
            try:
                text_hits = self.text_search(
                    query, public_video_id=public_video_id, limit=text_limit,
                    start_ms=start_ms, end_ms=end_ms,
                )
                semantic_hits: list[SearchHit] = []
                if self.semantic_retriever is not None and semantic_limit > 0:
                    try:
                        candidates = [
                            hit for hit in self.semantic_retriever(
                                query, public_video_id, semantic_limit * 4, min_score
                            ) if hit.semantic_score is not None and hit.semantic_score >= min_score
                        ]
                        semantic_hits = self._semantic_nms(candidates, semantic_limit)
                    except Exception as exc:
                        self.semantic_error = exc
                return text_hits, semantic_hits
            finally:
                if self.publication_snapshot:
                    release_snapshot(self.conn, self.publication_snapshot)

    def search_text_stage(
        self,
        query: str,
        *,
        public_video_id: str | None = None,
        limit: int = 20,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> tuple[list[SearchHit], str | None]:
        """Return fast text hits and the publication identity for a later stage."""
        with _LEGACY_SEARCH_LOCK:
            publication_id = self._open_search_snapshot()
            try:
                hits = self.text_search(
                    query,
                    public_video_id=public_video_id,
                    limit=limit,
                    start_ms=start_ms,
                    end_ms=end_ms,
                )
                return hits, publication_id
            finally:
                self._close_search_snapshot()

    def search_semantic_stage(
        self,
        query: str,
        *,
        expected_publication_id: str | None,
        public_video_id: str | None = None,
        limit: int = 5,
        min_score: float = 0.55,
    ) -> list[SearchHit]:
        """Return semantic hits only when the publication still matches stage one."""
        with _LEGACY_SEARCH_LOCK:
            publication_id = self._open_search_snapshot()
            try:
                if publication_id != expected_publication_id:
                    raise SearchSnapshotChangedError(
                        "search publication changed before semantic completion"
                    )
                if self.semantic_retriever is None or int(limit) <= 0:
                    return []
                try:
                    candidates = [
                        hit for hit in self.semantic_retriever(
                            query, public_video_id, int(limit) * 4, float(min_score)
                        )
                        if hit.semantic_score is not None
                        and hit.semantic_score >= float(min_score)
                    ]
                    return self._semantic_nms(candidates, int(limit))
                except SearchSnapshotChangedError:
                    raise
                except Exception as exc:
                    self.semantic_error = exc
                    return []
            finally:
                self._close_search_snapshot()
