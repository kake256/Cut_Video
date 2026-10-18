#!/usr/bin/env python
"""Generate local-only highlight candidates from a ready transcript analysis."""
from __future__ import annotations

import argparse
import sys

from moment_retrieval import config, db, excitement
from moment_retrieval.highlight_analysis import HighlightAnalysisError, run_highlight_analysis
from moment_retrieval.llm_analysis import OllamaProvider


def generate_active_highlights(video_id: str, *, model: str, count: int = 6,
                               min_duration: float = 20.0, max_duration: float = 180.0,
                               use_excitement: bool = True, fetch_chat: bool = True, log=print) -> dict:
    conn = db.get_conn()
    try:
        db.init_db(conn)
        revision = db.get_active_transcript_revision(conn, video_id)
        if revision is None:
            raise HighlightAnalysisError("先にLLM解析を実行してください（有効な文字起こしがありません）")
        analysis = db.get_latest_ready_analysis_run(conn, video_id, revision)
        if analysis is None:
            raise HighlightAnalysisError("先にLLM解析を実行してください（ready状態の解析結果がありません）")
        profile = None
        if use_excitement:
            if fetch_chat:
                excitement.fetch_chat_once(conn, video_id, log=log)
            profile = excitement.ensure_profile(conn, video_id, log=log)
            if profile and log:
                log("盛り上がりの数値をAIに渡します（使う情報: "
                    + "・".join({"audio": "音量", "chat": "コメント"}[name] for name in profile["sources"]) + "）")
        endpoint = config.LLM_ANALYSIS_ENDPOINT.rstrip("/")
        if not endpoint.endswith("/api/generate"):
            endpoint += "/api/generate"
        return run_highlight_analysis(
            conn, video_id, revision, analysis["analysis_run_id"],
            OllamaProvider(endpoint=endpoint, timeout_sec=config.LLM_ANALYSIS_TIMEOUT_SEC,
                           context_length=config.LLM_ANALYSIS_CONTEXT_LENGTH), model,
            requested_count=count, min_duration_sec=min_duration, max_duration_sec=max_duration,
            excitement=profile,
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
    parser.add_argument("--no-excitement", action="store_true",
                        help="音量・コメントの盛り上がりをAIに渡さない")
    parser.add_argument("--no-chat-fetch", action="store_true",
                        help="コメント履歴を新しく取得しない（保存済みだけ使う）")
    args = parser.parse_args()
    try:
        result = generate_active_highlights(args.video_id, model=args.model, count=args.count,
                                            min_duration=args.min_duration, max_duration=args.max_duration,
                                            use_excitement=not args.no_excitement,
                                            fetch_chat=not args.no_chat_fetch)
    except (HighlightAnalysisError, ValueError) as exc:
        print(f"見どころ候補の生成に失敗しました: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(f"見どころ候補を生成しました: candidates={result['candidate_count']} requested={result['requested_count']}")


if __name__ == "__main__":
    main()
