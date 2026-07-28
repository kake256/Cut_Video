#!/usr/bin/env python
"""動画を文字起こしし、検索用インデックス(SQLite + FAISS)を構築するCLI。

使い方:
    python index_video.py --video input.mp4
    python index_video.py --video input.mp4 --force   # 再インデックス

ASR結果は完了時点でDBに保存されるため、後段(埋め込み等)で失敗しても
再実行時に文字起こしをやり直さずに再開できる。
"""
import argparse
import gc
import os
import sys
from pathlib import Path
from typing import Iterator, Optional

import numpy as np

from moment_retrieval import config, db, utils
from moment_retrieval.chunker import build_chunks
from moment_retrieval.embedder import TextEmbedder


class IndexError_(Exception):
    """インデックス構築の失敗(GUI側で表示するためのメッセージ付き)。"""


# ASR途中保存の間隔 (音声秒)。この間隔ごとにDBへコミットし、失敗時は続きから再開できる
ASR_FLUSH_INTERVAL_SEC = 300.0
# 進捗表示の間隔 (実時間秒)
ASR_PROGRESS_INTERVAL_SEC = 15.0


def _install_compatibility_index(draft_path: Path) -> None:
    os.replace(draft_path, config.TEXT_INDEX_PATH)


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
                              language: str, batch_size: Optional[int] = None,
                              finalize_asr: bool = True,
                              transcript_revision: str | None = None) -> Iterator[str]:
    """ストリーミング文字起こし。進捗をyieldし、定期的にDBへ途中保存する。

    途中失敗しても保存済み区間は再利用され、次回は続きから再開される。
    """
    import time

    from moment_retrieval.asr import transcribe_stream

    start_offset = 0.0
    audio_src: Path = video
    tmp_audio = None

    if db.get_video(conn, video_id):
        last_end = db.get_last_segment_end(
            conn, video_id, transcript_revision=transcript_revision
        )
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
            db.insert_segment(
                conn,
                video_id,
                seg,
                transcript_revision=transcript_revision,
            )
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

    saved_total = len(
        db.get_segments(
            conn, video_id, transcript_revision=transcript_revision
        )
    )
    if saved_total == 0:
        raise IndexError_("文字起こし結果が空でした(無音動画の可能性があります)。")

    if finalize_asr:
        if transcript_revision is None:
            db.mark_asr_complete(conn, video_id)
        else:
            db.complete_transcript_revision(conn, transcript_revision)
    yield f"  文字起こし完了 (セグメント数: {saved_total})"


def _build_verified_index_draft(
    conn,
    video_id: str,
    transcript_revision: str,
    chunks,
    vectors: np.ndarray,
    transcript_updates: dict[str, str] | None = None,
) -> tuple[Path, list[int]]:
    """Build an exact prospective-generation FAISS draft without publishing it."""
    if len(chunks) != len(vectors) or len(chunks) == 0:
        raise IndexError_("検索用チャンクまたは埋め込み結果が空です。")
    db.delete_chunks_for_revision(conn, video_id, transcript_revision)
    chunk_ids = [
        db.insert_chunk(
            conn,
            video_id,
            chunk,
            transcript_revision=transcript_revision,
        )
        for chunk in chunks
    ]
    conn.commit()
    from moment_retrieval.publication import build_vector_index_draft

    replacements = {
        int(chunk_id): np.asarray(vector, dtype="float32")
        for chunk_id, vector in zip(chunk_ids, vectors)
    }
    draft_path, _expected_ids = build_vector_index_draft(
        conn,
        transcript_updates,
        replacements,
        int(vectors.shape[1]),
    )
    return draft_path, chunk_ids


def _run_optional_llm_analysis(
    conn,
    video_id: str,
    revision: str,
    model: str,
) -> Iterator[str]:
    """Run retryable derived analysis without changing indexing success."""
    yield (
        "[任意] ローカルLLMで要約・タグ・章を生成中... "
        f"(provider=Ollama, model={model})"
    )
    try:
        # Release Python references and CUDA cache left by embedding before
        # the separate Ollama server loads its model.
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except (ImportError, RuntimeError):
            pass
        from moment_retrieval.llm_analysis import (
            OllamaProvider,
            run_transcript_analysis,
        )

        endpoint = config.LLM_ANALYSIS_ENDPOINT.rstrip("/")
        if not endpoint.endswith("/api/generate"):
            endpoint += "/api/generate"
        result = run_transcript_analysis(
            conn,
            video_id,
            revision,
            OllamaProvider(
                endpoint=endpoint,
                timeout_sec=config.LLM_ANALYSIS_TIMEOUT_SEC,
                context_length=config.LLM_ANALYSIS_CONTEXT_LENGTH,
            ),
            model,
            max_window_chars=config.LLM_ANALYSIS_MAX_WINDOW_CHARS,
        )
        yield (
            "  LLM解析完了: "
            f"タグ{len(result['tags'])}件 / 章{len(result['chapters'])}件 / "
            f"文字起こし網羅率{result.get('segment_coverage_ratio', 0.0) * 100:.1f}%"
        )
    except Exception as exc:
        # Analysis is derived and retryable. Search publication and immutable
        # ASR remain successful even if provider/configuration is unavailable.
        yield (
            "  注意: LLM解析に失敗しました。動画・文字起こし・検索は利用できます。"
            f" 後から再実行できます: {exc}"
        )


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
    llm_analysis: bool = False,
    llm_model: str | None = None,
) -> Iterator[str]:
    """Build a draft revision and switch the current publication only after CAS."""
    chunk_sec = chunk_sec if chunk_sec is not None else config.CHUNK_SEC
    overlap_sec = overlap_sec if overlap_sec is not None else config.OVERLAP_SEC
    asr_model = asr_model or config.ASR_MODEL_SIZE
    device = device or config.ASR_DEVICE
    compute_type = compute_type or config.ASR_COMPUTE_TYPE
    video = Path(video)
    if not video.exists():
        raise IndexError_(f"動画が見つかりません: {video}")

    from moment_retrieval.publication import (
        LeaseManager,
        PublicationError,
        private_source_fingerprint,
        publish_current_generation,
    )

    conn = db.get_conn()
    draft_index_path: Path | None = None
    post_messages: list[str] = []
    try:
        with LeaseManager(conn).writer() as writer_lease:
            db.init_db(conn)
            expected_row = conn.execute(
                "SELECT current_publication_id FROM library_state WHERE singleton = 1"
            ).fetchone()
            expected_publication = (
                str(expected_row[0]) if expected_row and expected_row[0] else None
            )
            source_fingerprint = private_source_fingerprint(video)
            resolved_path = str(video.resolve())

            if video_id is None:
                existing_at_path = db.find_video_by_path(conn, resolved_path)
                video_id = (
                    existing_at_path["video_id"]
                    if existing_at_path else db.new_public_video_id()
                )
            existing_video = db.get_video(conn, video_id)
            if existing_video and Path(existing_video["path"]).resolve() != video.resolve():
                raise IndexError_(
                    "既存video IDを別の元動画で再インデックスできません。"
                    "先に再関連付けを行ってください。"
                )
            if existing_video is None:
                db.insert_video(conn, video_id, resolved_path, utils.probe_duration(video))
                conn.commit()
                existing_video = db.get_video(conn, video_id)
            else:
                stored_fingerprint = conn.execute(
                    "SELECT private_fingerprint FROM sources "
                    "WHERE source_generation = ?",
                    (existing_video["source_generation"],),
                ).fetchone()
                expected_fingerprint = (
                    str(stored_fingerprint[0])
                    if stored_fingerprint and stored_fingerprint[0]
                    else None
                )
                if (
                    expected_fingerprint is not None
                    and expected_fingerprint != source_fingerprint
                ):
                    raise IndexError_(
                        "元動画の内容が登録時と異なります。既存source generationを"
                        "上書きせず、再関連付けまたは新しい動画として登録してください。"
                    )

            active_revision = db.get_active_transcript_revision(conn, video_id)
            active_chunk_ids = (
                db.get_chunk_ids(
                    conn, video_id, transcript_revision=active_revision
                )
                if active_revision else []
            )
            if active_chunk_ids and not force:
                raise IndexError_(
                    f"video_id '{video_id}' は既にインデックス済みです。"
                    "作り直す場合は「再インデックス」を指定してください。"
                )

            duration = float(existing_video.get("duration") or utils.probe_duration(video))
            transcript_updates: dict[str, str] = {}
            public_video_id = str(existing_video["public_video_id"])
            if force or active_revision is None:
                revision = db.begin_transcript_revision(
                    conn,
                    video_id,
                    asr_config={
                        "model": asr_model,
                        "language": language,
                        "device": device,
                        "compute_type": compute_type,
                        "batch_size": batch_size,
                    },
                )
                revision_status = db.transcript_revision_status(conn, revision)
                draft_segments = db.get_segments(
                    conn, video_id, transcript_revision=revision
                )
                yield f"[1/4] 文字起こし中... (model={asr_model}, device={device})"
                if revision_status == "TEXT_READY" and draft_segments:
                    yield "  完成済みの未公開ASR draftを再利用します"
                else:
                    yield from _transcribe_with_progress(
                        conn,
                        video_id,
                        video,
                        duration,
                        asr_model,
                        device,
                        compute_type,
                        language,
                        batch_size=batch_size,
                        finalize_asr=False,
                        transcript_revision=revision,
                    )
                    if private_source_fingerprint(video) != source_fingerprint:
                        raise IndexError_(
                            "文字起こし中に元動画が変更されたため、結果を公開しませんでした。"
                        )
                    db.complete_transcript_revision(conn, revision)
                transcript_updates[public_video_id] = revision
            else:
                revision = active_revision
                yield f"[1/4] 文字起こし中... (model={asr_model}, device={device})"
                yield "  保存済みのASR結果を再利用します"

            from moment_retrieval.asr import Segment

            segments = [
                Segment(start=row["start_sec"], end=row["end_sec"], text=row["text"])
                for row in db.get_segments(
                    conn, video_id, transcript_revision=revision
                )
            ]
            yield f"  セグメント数: {len(segments)}"
            yield "[2/4] 検索用チャンクを生成中..."
            chunks = build_chunks(
                segments, target_sec=chunk_sec, overlap_sec=overlap_sec
            )
            yield f"  チャンク数: {len(chunks)}"

            yield "[3/4] テキスト埋め込みを計算中... (BGE-M3)"
            vectors = TextEmbedder().encode([chunk.text for chunk in chunks])
            if private_source_fingerprint(video) != source_fingerprint:
                raise IndexError_(
                    "埋め込み中に元動画が変更されたため、検索世代を公開しませんでした。"
                )

            yield "[4/4] DB / FAISSインデックスへ登録中..."
            draft_index_path, chunk_ids = _build_verified_index_draft(
                conn,
                video_id,
                revision,
                chunks,
                vectors,
                transcript_updates,
            )
            if private_source_fingerprint(video) != source_fingerprint:
                raise IndexError_(
                    "検索世代の公開直前に元動画が変更されたため、"
                    "結果を公開しませんでした。"
                )
            writer_lease.assert_owned()
            try:
                snapshot = publish_current_generation(
                    conn,
                    expected_publication,
                    transcript_updates=transcript_updates,
                    vector_draft_path=draft_index_path,
                    source_fingerprints={
                        str(existing_video["source_generation"]): source_fingerprint
                    },
                    writer_lease=writer_lease,
                )
            except PublicationError as exc:
                raise IndexError_(f"検索世代の公開に失敗しました: {exc}") from exc
            compatibility_warning = None
            try:
                _install_compatibility_index(draft_index_path)
                draft_index_path = None
            except OSError:
                compatibility_warning = (
                    "  注意: 互換インデックスの更新に失敗しましたが、"
                    "公開済み検索世代は利用できます。"
                )
            post_messages.append(
                f"  検索世代を公開しました: {snapshot.generation_id}"
            )
            if compatibility_warning:
                post_messages.append(compatibility_warning)
            post_messages.append(
                f"完了: video_id='{video_id}' / "
                f"チャンク {len(chunk_ids)} 件を登録しました。"
            )
        yield from post_messages
        if llm_analysis:
            selected_model = (llm_model or config.LLM_ANALYSIS_MODEL).strip()
            if not selected_model:
                yield "  注意: LLM解析をスキップしました（Ollamaモデル名が空です）。"
            else:
                yield from _run_optional_llm_analysis(
                    conn, video_id, revision, selected_model
                )
    except PublicationError as exc:
        raise IndexError_(f"別のライブラリ更新処理が実行中です: {exc}") from exc
    finally:
        if draft_index_path is not None:
            draft_index_path.unlink(missing_ok=True)
        conn.close()


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
    parser.add_argument(
        "--llm-analysis",
        action="store_true",
        help="公開後の文字起こしをローカルOllamaで要約・タグ・章に解析する",
    )
    parser.add_argument(
        "--llm-model",
        default=config.LLM_ANALYSIS_MODEL,
        help="LLM解析に使うローカルOllamaモデル名",
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
            llm_analysis=args.llm_analysis,
            llm_model=args.llm_model,
        ):
            print(msg)
    except IndexError_ as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
