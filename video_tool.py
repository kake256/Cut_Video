#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from moment_retrieval import config, db
from moment_retrieval.application import DOCUMENTS
from moment_retrieval.contracts import ContractError, failure, success
from moment_retrieval.edit_domain import EditPlan, TimeRange
from moment_retrieval.embedder import TextEmbedder
from moment_retrieval.save_service import save_document
from moment_retrieval.search_service import (
    SearchService,
    retrieve_semantic_hits,
    semantic_error_code,
)
from moment_retrieval.subtitles import map_subtitles
from moment_retrieval.transcript_types import parse_segment
from moment_retrieval.vector_index import VectorIndex


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _search(args) -> dict:
    conn = db.get_conn()
    db.init_db(conn)
    service = None
    try:
        def semantic(query, public_id, limit, threshold):
            return retrieve_semantic_hits(
                conn,
                service.publication_snapshot,
                query,
                public_id,
                limit,
                threshold,
                legacy_index_path=config.TEXT_INDEX_PATH,
                generations_dir=config.search_generations_dir(),
                encode_query=lambda value: TextEmbedder().encode([value]),
                index_loader=VectorIndex.load,
            )

        service = SearchService(conn, semantic)
        text_hits, semantic_hits = service.search(
            args.query, public_video_id=args.video_id,
            text_limit=args.text_limit, semantic_limit=args.semantic_limit,
            min_score=args.min_score,
        )
        serialize = lambda hit: {
            "hit_id": hit.hit_id, "public_video_id": hit.public_video_id,
            "kind": hit.kind, "evidence": [hit.evidence.start_ms, hit.evidence.end_ms],
            "suggested_range": [hit.suggested_start_ms, hit.suggested_end_ms],
            "text": hit.text, "semantic_score": hit.semantic_score,
        }
        warnings = []
        if service.semantic_error:
            warnings.append(semantic_error_code(service.semantic_error))
        return success("search", {
            "text_hits": [serialize(hit) for hit in text_hits],
            "semantic_hits": [serialize(hit) for hit in semantic_hits],
            "publication_id": service.publication_snapshot.publication_id
            if service.publication_snapshot else None,
        }, warnings)
    finally:
        conn.close()


def _load_plan(path: Path) -> EditPlan:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return EditPlan.create(
        int(payload["source_duration_ms"]), int(payload["overall"][0]), int(payload["overall"][1]),
        [TimeRange(int(item[0]), int(item[1])) for item in payload.get("exclusions", [])],
    )


def _clip(args) -> dict:
    plan = _load_plan(args.plan)
    conn = db.get_conn()
    db.init_db(conn)
    try:
        video = db.get_video(conn, args.video_id)
        if not video:
            raise ValueError("video not found")
        public_id = video["public_video_id"]
        document = DOCUMENTS.open(public_id, video.get("source_generation") or "unknown", plan)
        subtitle_text = None
        warnings = []
        if args.srt:
            rows = db.get_segments(conn, public_id)
            segments = []
            for row in rows:
                try:
                    segments.append(parse_segment(row, plan.source_duration_ms))
                except ValueError as exc:
                    warnings.append(str(exc))
            from moment_retrieval.edit_domain import make_effective_export_plan
            subtitle_result = map_subtitles(segments, make_effective_export_plan(plan))
            subtitle_text = subtitle_result.to_srt()
            warnings.extend(subtitle_result.warnings)
        result = save_document(
            document.document_id, Path(video["path"]), args.output, args.precise,
            subtitle_text=subtitle_text, warnings=warnings,
        )
        return success("clip", {
            "public_video_id": public_id,
            "source_generation": video.get("source_generation"),
            "plan_hash": plan.semantic_signature,
            "video": str(result.video_path),
            "srt": str(result.subtitle_path) if result.subtitle_path else None,
            "manifest": str(result.manifest_path),
            "commit_id": result.commit_id,
        }, warnings)
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="動画シーン検索・切り抜き CLI v1")
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--video-id")
    search.add_argument("--text-limit", type=int, default=20)
    search.add_argument("--semantic-limit", type=int, default=5)
    search.add_argument("--min-score", type=float, default=config.MIN_SCORE)
    clip = commands.add_parser("clip")
    clip.add_argument("--video-id", required=True)
    clip.add_argument("--plan", type=Path, required=True)
    clip.add_argument("--output", type=Path, required=True)
    clip.add_argument("--precise", action="store_true")
    clip.add_argument("--srt", action="store_true")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = _search(args) if args.command == "search" else _clip(args)
        _emit(payload)
        return 0
    except Exception as exc:
        _emit(failure(args.command, ContractError(type(exc).__name__.upper(), str(exc))))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
