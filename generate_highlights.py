#!/usr/bin/env python
"""Generate local-only highlight candidates from a ready transcript analysis."""
from __future__ import annotations

import argparse
import sys

from moment_retrieval import config, db
from moment_retrieval.highlight_analysis import HighlightAnalysisError, run_highlight_analysis
from moment_retrieval.llm_analysis import OllamaProvider


def generate_active_highlights(video_id: str, *, model: str, count: int = 6,
                               min_duration: float = 20.0, max_duration: float = 180.0) -> dict:
    conn = db.get_conn()
    try:
        db.init_db(conn)
        revision = db.get_active_transcript_revision(conn, video_id)
        if revision is None:
            raise HighlightAnalysisError("先にLLM解析を実行してください（有効な文字起こしがありません）")
        analysis = db.get_latest_ready_analysis_run(conn, video_id, revision)
        if analysis is None:
            raise HighlightAnalysisError("先にLLM解析を実行してください（ready状態の解析結果がありません）")
        endpoint = config.LLM_ANALYSIS_ENDPOINT.rstrip("/")
        if not endpoint.endswith("/api/generate"):
            endpoint += "/api/generate"
        return run_highlight_analysis(
            conn, video_id, revision, analysis["analysis_run_id"],
            OllamaProvider(endpoint=endpoint, timeout_sec=config.LLM_ANALYSIS_TIMEOUT_SEC,
                           context_length=config.LLM_ANALYSIS_CONTEXT_LENGTH), model,
            requested_count=count, min_duration_sec=min_duration, max_duration_sec=max_duration,
        )
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="ローカルLLMで見どころ候補を生成します")
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--model", default=config.LLM_HIGHLIGHT_MODEL)
    parser.add_argument("--count", type=int, default=config.LLM_HIGHLIGHT_COUNT)
    parser.add_argument("--min-duration", type=float, default=config.LLM_HIGHLIGHT_MIN_DURATION_SEC)
    parser.add_argument("--max-duration", type=float, default=config.LLM_HIGHLIGHT_MAX_DURATION_SEC)
    args = parser.parse_args()
    try:
        result = generate_active_highlights(args.video_id, model=args.model, count=args.count,
                                            min_duration=args.min_duration, max_duration=args.max_duration)
    except (HighlightAnalysisError, ValueError) as exc:
        print(f"見どころ候補の生成に失敗しました: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(f"見どころ候補を生成しました: candidates={result['candidate_count']} requested={result['requested_count']}")


if __name__ == "__main__":
    main()
