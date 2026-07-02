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
    args = parser.parse_args()

    if not config.TEXT_INDEX_PATH.exists():
        print("インデックスが見つかりません。先に index_video.py を実行してください。", file=sys.stderr)
        sys.exit(1)

    conn = db.get_conn()
    db.init_db(conn)

    embedder = TextEmbedder()
    query_vec = embedder.encode([args.query])
    dim = query_vec.shape[1]

    vindex = VectorIndex.load(config.TEXT_INDEX_PATH, dim)

    # video_idで絞り込む場合、フィルタで捨てられる分を見込んで多めに取得する
    fetch_k = args.top_k * 5 if args.video_id else args.top_k
    scores, ids = vindex.search(query_vec, top_k=max(fetch_k, args.top_k))

    valid_ids = [int(cid) for cid in ids if cid != -1]
    chunks = db.get_chunks_by_ids(conn, valid_ids)

    results = []
    for cid, score in zip(ids, scores):
        cid = int(cid)
        if cid == -1:
            continue
        chunk = chunks.get(cid)
        if not chunk:
            continue
        if args.video_id and chunk["video_id"] != args.video_id:
            continue
        if float(score) < args.min_score:
            continue
        results.append((float(score), chunk))
        if len(results) >= args.top_k:
            break

    if not results:
        print(f"該当するシーンが見つかりませんでした。(類似度 {args.min_score} 以上の候補なし)")
        return

    print(f"クエリ: {args.query!r}")
    print("-" * 60)
    for rank, (score, chunk) in enumerate(results, start=1):
        start_ts = utils.format_timestamp(chunk["start_sec"])
        end_ts = utils.format_timestamp(chunk["end_sec"])
        print(f"{rank}. [{chunk['video_id']}] {start_ts} - {end_ts} (score={score:.3f})")
        preview = chunk["text"][:80] + ("..." if len(chunk["text"]) > 80 else "")
        print(f"   {preview}")

    if args.cut:
        from cut_clip import cut_clip
        from moment_retrieval.refine import expand_to_speech_boundary

        for rank, (score, chunk) in enumerate(results[: args.cut_top_n], start=1):
            video = db.get_video(conn, chunk["video_id"])
            if not video:
                continue

            start, end = chunk["start_sec"], chunk["end_sec"]
            if not args.no_expand:
                start, end = expand_to_speech_boundary(
                    conn, chunk["video_id"], start, end, gap_sec=args.gap
                )
                print(
                    f"境界拡張: {utils.format_timestamp(chunk['start_sec'])}-"
                    f"{utils.format_timestamp(chunk['end_sec'])} → "
                    f"{utils.format_timestamp(start)}-{utils.format_timestamp(end)}"
                )

            args.output_dir.mkdir(parents=True, exist_ok=True)
            out_path = (
                args.output_dir / f"{chunk['video_id']}_{rank}_{int(start)}.mp4"
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
