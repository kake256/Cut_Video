#!/usr/bin/env python
"""Run optional local LLM analysis for an already indexed transcript."""
from __future__ import annotations

import argparse
import sys

from moment_retrieval import config, db
from moment_retrieval.llm_analysis import (
    OllamaProvider,
    TranscriptAnalysisError,
    run_transcript_analysis,
)


def analyze_active_transcript(
    video_id: str,
    *,
    model: str,
    endpoint: str | None = None,
) -> dict:
    conn = db.get_conn()
    try:
        db.init_db(conn)
        revision = db.get_active_transcript_revision(conn, video_id)
        if revision is None:
            raise TranscriptAnalysisError(
                "この動画には解析可能な有効文字起こしがありません。"
            )
        provider_endpoint = (endpoint or config.LLM_ANALYSIS_ENDPOINT).rstrip("/")
        if not provider_endpoint.endswith("/api/generate"):
            provider_endpoint += "/api/generate"
        return run_transcript_analysis(
            conn,
            video_id,
            revision,
            OllamaProvider(
                endpoint=provider_endpoint,
                timeout_sec=config.LLM_ANALYSIS_TIMEOUT_SEC,
                context_length=config.LLM_ANALYSIS_CONTEXT_LENGTH,
            ),
            model,
            max_window_chars=config.LLM_ANALYSIS_MAX_WINDOW_CHARS,
        )
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="既存の文字起こしをローカルOllamaで要約・タグ・章に解析する"
    )
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--model", default=config.LLM_ANALYSIS_MODEL)
    parser.add_argument(
        "--endpoint",
        default=config.LLM_ANALYSIS_ENDPOINT,
        help="ローカルOllama URL（loopbackのみ）",
    )
    args = parser.parse_args()

    try:
        result = analyze_active_transcript(
            args.video_id,
            model=args.model,
            endpoint=args.endpoint,
        )
    except (TranscriptAnalysisError, ValueError) as exc:
        print(f"LLM解析に失敗しました: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(
        "LLM解析完了: "
        f"analysis_run_id={result['analysis_run_id']} "
        f"tags={len(result['tags'])} chapters={len(result['chapters'])} "
        f"coverage={result.get('segment_coverage_ratio', 0.0) * 100:.1f}%"
    )


if __name__ == "__main__":
    main()
