#!/usr/bin/env python
"""動画を文字起こしし、検索用インデックス(SQLite + FAISS)を構築するCLI。

使い方:
    python index_video.py --video input.mp4
    python index_video.py --video input.mp4 --force   # 再インデックス

ASR結果は完了時点でDBに保存されるため、後段(埋め込み等)で失敗しても
再実行時に文字起こしをやり直さずに再開できる。
"""
import argparse
import sys
from pathlib import Path
from typing import Iterator, Optional

import numpy as np

from moment_retrieval import config, db, utils
from moment_retrieval.chunker import build_chunks
from moment_retrieval.embedder import TextEmbedder
from moment_retrieval.vector_index import VectorIndex


class IndexError_(Exception):
    """インデックス構築の失敗(GUI側で表示するためのメッセージ付き)。"""


def _load_or_transcribe(conn, video_id: str, video: Path, asr_model: str,
                        device: str, compute_type: str, language: str):
    from moment_retrieval.asr import Segment, transcribe

    if db.get_video(conn, video_id):
        saved = db.get_segments(conn, video_id)
        if saved:
            return [
                Segment(start=r["start_sec"], end=r["end_sec"], text=r["text"])
                for r in saved
            ], True

    segments, info = transcribe(
        video, model_size=asr_model, device=device,
        compute_type=compute_type, language=language,
    )
    if not segments:
        raise IndexError_("文字起こし結果が空でした(無音動画の可能性があります)。")

    if not db.get_video(conn, video_id):
        duration = utils.probe_duration(video)
        db.insert_video(conn, video_id, str(video.resolve()), duration)
    for seg in segments:
        db.insert_segment(conn, video_id, seg)
    conn.commit()
    return segments, False


def run_indexing(
    video: Path,
    video_id: Optional[str] = None,
    force: bool = False,
    language: str = "ja",
    chunk_sec: float = None,
    overlap_sec: float = None,
    asr_model: str = None,
    device: str = None,
    compute_type: str = None,
) -> Iterator[str]:
    """インデックス構築パイプライン。進捗メッセージをyieldする。

    失敗時はIndexError_を送出する。
    """
    chunk_sec = chunk_sec if chunk_sec is not None else config.CHUNK_SEC
    overlap_sec = overlap_sec if overlap_sec is not None else config.OVERLAP_SEC
    asr_model = asr_model or config.ASR_MODEL_SIZE
    device = device or config.ASR_DEVICE
    compute_type = compute_type or config.ASR_COMPUTE_TYPE

    if not video.exists():
        raise IndexError_(f"動画が見つかりません: {video}")

    video_id = video_id or utils.make_video_id(video)

    conn = db.get_conn()
    db.init_db(conn)

    existing_chunk_ids = db.get_chunk_ids(conn, video_id)
    if existing_chunk_ids and not force:
        raise IndexError_(
            f"video_id '{video_id}' は既にインデックス済みです。"
            "作り直す場合は「再インデックス」を指定してください。"
        )

    if force and db.get_video(conn, video_id):
        if config.TEXT_INDEX_PATH.exists() and existing_chunk_ids:
            vindex = VectorIndex.load(config.TEXT_INDEX_PATH, 1)
            vindex.remove(np.array(existing_chunk_ids, dtype="int64"))
            vindex.save(config.TEXT_INDEX_PATH)
        db.delete_video(conn, video_id)
        yield f"既存インデックスを削除しました: {video_id}"

    yield f"[1/4] 文字起こし中... (model={asr_model}, device={device})"
    segments, reused = _load_or_transcribe(
        conn, video_id, video, asr_model, device, compute_type, language
    )
    if reused:
        yield f"  保存済みのASR結果を再利用 ({len(segments)} セグメント)"
    else:
        yield f"  セグメント数: {len(segments)} (DBに保存済み)"

    yield "[2/4] 検索用チャンクを生成中..."
    chunks = build_chunks(segments, target_sec=chunk_sec, overlap_sec=overlap_sec)
    yield f"  チャンク数: {len(chunks)}"

    yield "[3/4] テキスト埋め込みを計算中... (BGE-M3)"
    embedder = TextEmbedder()
    vectors = embedder.encode([c.text for c in chunks])

    yield "[4/4] DB / FAISSインデックスへ登録中..."
    if config.TEXT_INDEX_PATH.exists():
        vindex = VectorIndex.load(config.TEXT_INDEX_PATH, vectors.shape[1])
    else:
        vindex = VectorIndex(vectors.shape[1])

    chunk_ids = [db.insert_chunk(conn, video_id, chunk) for chunk in chunks]
    vindex.add(np.array(chunk_ids, dtype="int64"), vectors)
    vindex.save(config.TEXT_INDEX_PATH)

    conn.commit()
    conn.close()
    yield f"完了: video_id='{video_id}' / チャンク {len(chunks)} 件を登録しました。"


def main():
    parser = argparse.ArgumentParser(description="動画を文字起こしして検索インデックスを作成する")
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--video-id", default=None, help="省略時はファイル名から自動生成")
    parser.add_argument("--force", action="store_true", help="既存のインデックスを削除して作り直す")
    parser.add_argument("--language", default="ja")
    parser.add_argument("--chunk-sec", type=float, default=config.CHUNK_SEC)
    parser.add_argument("--overlap-sec", type=float, default=config.OVERLAP_SEC)
    parser.add_argument("--asr-model", default=config.ASR_MODEL_SIZE)
    parser.add_argument("--device", default=config.ASR_DEVICE)
    parser.add_argument("--compute-type", default=config.ASR_COMPUTE_TYPE)
    args = parser.parse_args()

    try:
        for msg in run_indexing(
            video=args.video,
            video_id=args.video_id,
            force=args.force,
            language=args.language,
            chunk_sec=args.chunk_sec,
            overlap_sec=args.overlap_sec,
            asr_model=args.asr_model,
            device=args.device,
            compute_type=args.compute_type,
        ):
            print(msg)
    except IndexError_ as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
