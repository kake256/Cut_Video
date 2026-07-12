#!/usr/bin/env python
"""自然言語クエリで動画内シーンを検索するCLI。

使い方:
    python search_video.py --query "新作ゲームについて話しているところ"
    python search_video.py --query "価格について話しているところ" --cut
"""
import argparse
import sys
from pathlib import Path

from moment_retrieval import config, db, utils
from moment_retrieval.embedder import TextEmbedder
from moment_retrieval.search import search_chunks
from moment_retrieval.vector_index import VectorIndex


def main():
    parser = argparse.ArgumentParser(description="自然言語クエリで動画内シーンを検索する")
    parser.add_argument("--query", required=True)
    parser.add_argument("--video-id", default=None, help="特定の動画に絞り込む")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--min-score",
        type=float,
        default=config.MIN_SCORE,
        help="この類似度未満の候補は捨てる(全滅なら「該当なし」と返す)",
    )
    parser.add_argument("--cut", action="store_true", help="上位結果をffmpegで切り出す")
    parser.add_argument("--cut-top-n", type=int, default=1, help="切り出す件数(--cut時)")
    parser.add_argument("--output-dir", type=Path, default=Path("clips"))
    parser.add_argument("--pad", type=float, default=config.PAD_SEC)
    parser.add_argument(
        "--precise", action="store_true", help="再エンコードしてフレーム精度で切り出す"
    )
    parser.add_argument(
        "--no-expand",
        action="store_true",
        help="切り出し時の境界拡張(話の切れ目まで延長)を無効にする",
    )
    parser.add_argument(
        "--gap",
        type=float,
        default=config.BOUNDARY_GAP_SEC,
        help="この秒数以上の無音で話の切れ目とみなす",
    )
    parser.add_argument(
        "--start-sec", type=float, default=None,
        help="この秒数以降だけを検索対象にする(--video-idと併用必須)",
    )
    parser.add_argument(
        "--end-sec", type=float, default=None,
        help="この秒数までだけを検索対象にする(--video-idと併用必須)",
    )
    args = parser.parse_args()

    if not config.TEXT_INDEX_PATH.exists():
        print("インデックスが見つかりません。先に index_video.py を実行してください。", file=sys.stderr)
        sys.exit(1)

    range_specified = args.start_sec is not None or args.end_sec is not None
    if range_specified and not args.video_id:
        print("--start-sec/--end-secは--video-idと併用してください。", file=sys.stderr)
        sys.exit(1)

    conn = db.get_conn()
    db.init_db(conn)

    start_sec, end_sec = args.start_sec, args.end_sec
    if range_specified:
        video = db.get_video(conn, args.video_id)
        if not video:
            print(f"動画が見つかりません: {args.video_id}", file=sys.stderr)
            sys.exit(1)
        # 片方だけ指定した場合は動画の先頭/末尾で補完する
        if start_sec is None:
            start_sec = 0.0
        if end_sec is None:
            end_sec = video["duration"]
        if end_sec <= start_sec:
            print("--end-secは--start-secより後の値にしてください。", file=sys.stderr)
            sys.exit(1)

    embedder = TextEmbedder()
    query_vec = embedder.encode([args.query])
    dim = query_vec.shape[1]

    vindex = VectorIndex.load(config.TEXT_INDEX_PATH, dim)

    results = search_chunks(
        conn,
        vindex,
        args.query,
        query_vec,
        top_k=args.top_k,
        min_score=args.min_score,
        video_id=args.video_id,
        start_sec=start_sec,
        end_sec=end_sec,
    )

    if not results:
        print(
            "該当するシーンが見つかりませんでした。"
            f"(文字一致なし、意味類似度 {args.min_score} 以上の候補なし)"
        )
        return

    print(f"クエリ: {args.query!r}")
    print("-" * 60)
    for rank, result in enumerate(results, start=1):
        start_ts = utils.format_timestamp(result["start"])
        end_ts = utils.format_timestamp(result["end"])
        print(
            f"{rank}. [{result['video_id']}] {start_ts} - {end_ts} "
            f"({result['match_type']}, score={result['score']:.3f})"
        )
        preview = result["text"][:80] + ("..." if len(result["text"]) > 80 else "")
        print(f"   {preview}")

    if args.cut:
        from cut_clip import cut_clip
        from moment_retrieval.refine import expand_to_speech_boundary

        for rank, result in enumerate(results[: args.cut_top_n], start=1):
            video = db.get_video(conn, result["video_id"])
            if not video:
                continue

            start, end = result["start"], result["end"]
            if not args.no_expand:
                start, end = expand_to_speech_boundary(
                    conn, result["video_id"], start, end, gap_sec=args.gap
                )
                print(
                    f"境界拡張: {utils.format_timestamp(result['start'])}-"
                    f"{utils.format_timestamp(result['end'])} → "
                    f"{utils.format_timestamp(start)}-{utils.format_timestamp(end)}"
                )

            args.output_dir.mkdir(parents=True, exist_ok=True)
            out_path = (
                args.output_dir / f"{result['video_id']}_{rank}_{int(start)}.mp4"
            )
            print(f"切り出し中: {out_path}")
            cut_clip(
                Path(video["path"]),
                start,
                end,
                out_path,
                pad=args.pad,
                precise=args.precise,
                duration=video["duration"],
            )


if __name__ == "__main__":
    main()
