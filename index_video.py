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


# ASR途中保存の間隔 (音声秒)。この間隔ごとにDBへコミットし、失敗時は続きから再開できる
ASR_FLUSH_INTERVAL_SEC = 300.0
# 進捗表示の間隔 (実時間秒)
ASR_PROGRESS_INTERVAL_SEC = 15.0


def _extract_audio_tail(video: Path, start_sec: float) -> Path:
    """再開用に、start_sec以降の音声だけを16kHzモノラルwavに抽出する。"""
    import subprocess
    import tempfile

    tmp = Path(tempfile.gettempdir()) / f"asr_resume_{video.stem}_{int(start_sec)}.wav"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", str(start_sec), "-i", str(video),
        "-vn", "-ac", "1", "-ar", "16000", str(tmp),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not tmp.exists():
        raise IndexError_(f"再開用の音声抽出に失敗しました: {result.stderr[:200]}")
    return tmp


def _transcribe_with_progress(conn, video_id: str, video: Path, duration: float,
                              asr_model: str, device: str, compute_type: str,
                              language: str, batch_size: Optional[int] = None) -> Iterator[str]:
    """ストリーミング文字起こし。進捗をyieldし、定期的にDBへ途中保存する。

    途中失敗しても保存済み区間は再利用され、次回は続きから再開される。
    """
    import time

    from moment_retrieval.asr import transcribe_stream

    start_offset = 0.0
    audio_src: Path = video
    tmp_audio = None

    if db.get_video(conn, video_id):
        last_end = db.get_last_segment_end(conn, video_id)
        if last_end > 0:
            start_offset = last_end
            yield (f"  前回の途中結果を検出。{utils.format_timestamp(last_end)} "
                   "から文字起こしを再開します...")
            tmp_audio = _extract_audio_tail(video, last_end)
            audio_src = tmp_audio
    else:
        db.insert_video(conn, video_id, str(video.resolve()), duration)
        conn.commit()

    yield ("  モデルをロードし音声を解析中... "
           "(長い動画では文字起こし開始まで数分〜十数分かかります)")
    seg_iter, info, used_batch_size = transcribe_stream(
        audio_src, model_size=asr_model, device=device,
        compute_type=compute_type, language=language, start_offset=start_offset,
        batch_size=batch_size,
    )
    if used_batch_size > 1:
        mode = f"バッチ並列 x{used_batch_size}"
    else:
        mode = "逐次"
    auto_note = " (空きVRAMから自動決定)" if batch_size is None else ""
    yield f"  推論モード: {mode}{auto_note}"

    pending = []
    total = 0
    last_flush_end = start_offset
    last_print = time.monotonic()

    def _flush():
        nonlocal pending
        for seg in pending:
            db.insert_segment(conn, video_id, seg)
        conn.commit()
        pending = []

    try:
        for seg in seg_iter:
            pending.append(seg)
            total += 1

            if seg.end - last_flush_end >= ASR_FLUSH_INTERVAL_SEC:
                _flush()
                last_flush_end = seg.end

            now = time.monotonic()
            if now - last_print >= ASR_PROGRESS_INTERVAL_SEC:
                pct = min(100.0, seg.end / duration * 100) if duration else 0.0
                yield (f"  文字起こし中... {pct:.1f}% "
                       f"({utils.format_timestamp(seg.end)} / {utils.format_timestamp(duration)})")
                last_print = now

        _flush()
    finally:
        if tmp_audio is not None:
            tmp_audio.unlink(missing_ok=True)

    saved_total = len(db.get_segments(conn, video_id))
    if saved_total == 0:
        raise IndexError_("文字起こし結果が空でした(無音動画の可能性があります)。")

    db.mark_asr_complete(conn, video_id)
    yield f"  文字起こし完了 (セグメント数: {saved_total})"


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
    batch_size: Optional[int] = None,
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

    duration = utils.probe_duration(video)

    yield f"[1/4] 文字起こし中... (model={asr_model}, device={device})"
    if db.get_video(conn, video_id) and db.is_asr_complete(conn, video_id):
        yield "  保存済みのASR結果を再利用します"
    else:
        yield from _transcribe_with_progress(
            conn, video_id, video, duration, asr_model, device, compute_type, language,
            batch_size=batch_size,
        )

    from moment_retrieval.asr import Segment

    segments = [
        Segment(start=r["start_sec"], end=r["end_sec"], text=r["text"])
        for r in db.get_segments(conn, video_id)
    ]
    yield f"  セグメント数: {len(segments)}"

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
    parser.add_argument(
        "--batch-size", type=int, default=None,
        help="バッチ推論のバッチサイズ。1で逐次(バッチ無効)。省略時は空きVRAMから自動決定",
    )
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
            batch_size=args.batch_size,
        ):
            print(msg)
    except IndexError_ as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
