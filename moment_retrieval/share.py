"""インデックスのエクスポート/インポート(他PCへの共有機能)。

エクスポート: 指定video_idのvideos行・asr_segments・text_chunksとその
FAISSベクトルをまとめてzip(<video_id>.vindex.zip)にする。
インポート: そのzipを読み込み、DB(SQLite)とFAISSインデックスへ登録する。
再文字起こしなしで検索対象に追加できる。
"""
import json
import zipfile
from pathlib import Path
from typing import Iterator

import numpy as np

from . import config, db
from .vector_index import VectorIndex


class ShareError(Exception):
    """エクスポート/インポートの失敗(GUI表示用メッセージ付き)。"""


def export_index(video_id: str, out_dir: Path = Path("exports")) -> Path:
    """指定video_idのインデックスをzipファイルにエクスポートする。

    戻り値: 作成したzipファイルのパス
    """
    conn = db.get_conn()
    db.init_db(conn)
    try:
        video = db.get_video(conn, video_id)
        if not video:
            raise ShareError(f"video_id '{video_id}' が見つかりません。")

        segments = db.get_segments(conn, video_id)

        chunk_rows = conn.execute(
            "SELECT chunk_id, video_id, start_sec, end_sec, text FROM text_chunks "
            "WHERE video_id = ? ORDER BY chunk_id",
            (video_id,),
        ).fetchall()
        chunks = [dict(r) for r in chunk_rows]
        chunk_ids = [c["chunk_id"] for c in chunks]

        if chunk_ids:
            if not config.TEXT_INDEX_PATH.exists():
                raise ShareError("FAISSインデックスが見つかりません。")
            vindex = VectorIndex.load(config.TEXT_INDEX_PATH, 1)
            vectors = np.stack(
                [vindex.index.reconstruct(int(cid)) for cid in chunk_ids]
            ).astype("float32")
        else:
            vectors = np.zeros((0, 0), dtype="float32")

        manifest = {
            "video": video,
            "segments": segments,
            "chunks": chunks,
        }

        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{video_id}.vindex.zip"

        vectors_bytes_path = out_dir / f"_tmp_{video_id}_vectors.npy"
        np.save(vectors_bytes_path, vectors)
        try:
            with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                )
                zf.write(vectors_bytes_path, "vectors.npy")
        finally:
            vectors_bytes_path.unlink(missing_ok=True)

        return out_path
    finally:
        conn.close()


def import_index(zip_path: Path) -> Iterator[str]:
    """zipファイルからインデックスをインポートする。進捗メッセージをyieldする。"""
    zip_path = Path(zip_path)
    if not zip_path.exists():
        raise ShareError(f"ファイルが見つかりません: {zip_path}")

    with zipfile.ZipFile(zip_path, "r") as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        import io

        vectors = np.load(io.BytesIO(zf.read("vectors.npy")))

    video = manifest["video"]
    segments = manifest["segments"]
    chunks = manifest["chunks"]
    video_id = video["video_id"]

    conn = db.get_conn()
    db.init_db(conn)
    try:
        if db.get_video(conn, video_id):
            raise ShareError(f"video_id '{video_id}' は既にインポート済みです。")

        yield f"インポート開始: video_id='{video_id}'"

        db.insert_video(conn, video_id, video["path"], video["duration"])
        db.mark_asr_complete(conn, video_id)
        conn.commit()
        yield "  動画情報を登録しました"

        for seg in segments:
            conn.execute(
                "INSERT INTO asr_segments (video_id, start_sec, end_sec, text, words_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (video_id, seg["start_sec"], seg["end_sec"], seg["text"], seg.get("words_json")),
            )
        conn.commit()
        yield f"  文字起こしセグメントを登録しました ({len(segments)} 件)"

        new_chunk_ids = []
        for chunk in chunks:
            cur = conn.execute(
                "INSERT INTO text_chunks (video_id, start_sec, end_sec, text) VALUES (?, ?, ?, ?)",
                (video_id, chunk["start_sec"], chunk["end_sec"], chunk["text"]),
            )
            new_chunk_ids.append(cur.lastrowid)
        conn.commit()
        yield f"  検索用チャンクを登録しました ({len(chunks)} 件)"

        if new_chunk_ids:
            if vectors.ndim != 2 or vectors.shape[0] != len(new_chunk_ids):
                raise ShareError("manifestとベクトルの件数が一致しません。")
            if config.TEXT_INDEX_PATH.exists():
                vindex = VectorIndex.load(config.TEXT_INDEX_PATH, vectors.shape[1])
            else:
                vindex = VectorIndex(vectors.shape[1])
            vindex.add(np.array(new_chunk_ids, dtype="int64"), vectors)
            vindex.save(config.TEXT_INDEX_PATH)
            yield "  FAISSインデックスへ登録しました"

        conn.commit()

        video_file = Path(video["path"])
        if not video_file.exists():
            yield (
                "警告: 動画ファイルが見つかりません。"
                "検索は可能ですが、プレビュー・切り抜きには動画ファイルを "
                f"video/ に置いて下さい ({video['path']})。"
            )

        yield f"完了: video_id='{video_id}' のインポートが完了しました。"
    finally:
        conn.close()
