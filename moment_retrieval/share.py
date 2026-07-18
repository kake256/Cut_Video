"""インデックス共有パッケージの安全なエクスポート/インポート。

Phase 0の暫定形式では、送信元PCの絶対パス、path由来の旧video_id、
DB内部IDをパッケージへ含めない。全文文字起こし等の機密情報を含むため、
エクスポート時は呼び出し側から明示的な確認を要求する。
"""

from __future__ import annotations

import io
import hashlib
import json
import math
import os
import tempfile
import uuid
import zipfile
import zlib
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from . import config, db
from .vector_index import VectorIndex


class ShareError(Exception):
    """エクスポート/インポートの失敗(GUI表示用メッセージ付き)。"""


PACKAGE_FORMAT = "cut-video-index"
PACKAGE_SCHEMA_VERSION = 2
LEGACY_REDACTED_SCHEMA_VERSION = "phase0-redacted-1"
_ALLOWED_ENTRIES = frozenset({"manifest.json", "vectors.npy"})
_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_MAX_VECTORS_BYTES = 128 * 1024 * 1024
_MAX_PACKAGE_BYTES = _MAX_MANIFEST_BYTES + _MAX_VECTORS_BYTES
_MAX_ZIP_OVERHEAD_BYTES = 1024 * 1024
_MAX_ITEMS = 500_000
_MAX_TEXT_LENGTH = 2_000_000
_MAX_VECTOR_DIM = 8192
_MAX_DURATION_SEC = 31 * 24 * 60 * 60


def _opaque_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _safe_float(value: Any, field: str, *, maximum: float = _MAX_DURATION_SEC) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ShareError(f"共有データの{field}が不正です。") from exc
    if not math.isfinite(number) or number < 0 or number > maximum:
        raise ShareError(f"共有データの{field}が許容範囲外です。")
    return number


def _safe_text(value: Any, field: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or len(value) > _MAX_TEXT_LENGTH:
        raise ShareError(f"共有データの{field}が不正です。")
    return value


def _sanitize_words_json(value: Any) -> str | None:
    """単語時刻を必要fieldだけへ正規化し、未知metadataを破棄する。"""
    raw = _safe_text(value, "segments.words_json", allow_none=True)
    if raw is None:
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ShareError("共有データの単語時刻情報が不正です。") from exc
    if not isinstance(decoded, list) or len(decoded) > _MAX_ITEMS:
        raise ShareError("共有データの単語時刻情報が不正です。")

    clean_words: list[dict[str, Any]] = []
    for item in decoded:
        if not isinstance(item, dict):
            raise ShareError("共有データの単語時刻情報が不正です。")
        start = _safe_float(item.get("start"), "word.start")
        end = _safe_float(item.get("end"), "word.end")
        if end < start:
            raise ShareError("共有データの単語時刻が逆転しています。")
        clean_words.append(
            {
                "word": _safe_text(item.get("word", ""), "word.text"),
                "start": start,
                "end": end,
            }
        )
    return json.dumps(clean_words, ensure_ascii=False, separators=(",", ":"))


def _sanitize_ranges(items: Any, kind: str) -> list[dict[str, Any]]:
    if not isinstance(items, list) or len(items) > _MAX_ITEMS:
        raise ShareError(f"共有データの{kind}件数が不正です。")

    sanitized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ShareError(f"共有データの{kind}形式が不正です。")
        start = _safe_float(item.get("start_sec"), f"{kind}.start_sec")
        end = _safe_float(item.get("end_sec"), f"{kind}.end_sec")
        if end < start:
            raise ShareError(f"共有データの{kind}時刻が逆転しています。")
        clean = {
            "start_sec": start,
            "end_sec": end,
            "text": _safe_text(item.get("text", ""), f"{kind}.text"),
        }
        if kind == "segments":
            clean["words_json"] = _sanitize_words_json(item.get("words_json"))
        sanitized.append(clean)
    return sanitized


def _validate_npy_header(raw: bytes, chunk_count: int) -> tuple[int, int]:
    """配列を確保する前に.npy headerと実payload長を検証する。"""
    stream = io.BytesIO(raw)
    try:
        version = np.lib.format.read_magic(stream)
        if version == (1, 0):
            shape, fortran_order, dtype = np.lib.format.read_array_header_1_0(
                stream, max_header_size=10_000
            )
        elif version == (2, 0):
            shape, fortran_order, dtype = np.lib.format.read_array_header_2_0(
                stream, max_header_size=10_000
            )
        else:
            raise ShareError("未対応のベクトル配列形式です。")
    except ShareError:
        raise
    except (OSError, ValueError, EOFError, TypeError) as exc:
        raise ShareError("共有データのベクトルheaderが不正です。") from exc

    if fortran_order or np.dtype(dtype) != np.dtype("float32"):
        raise ShareError("共有データのベクトル型はC-order float32である必要があります。")
    if (
        not isinstance(shape, tuple)
        or len(shape) != 2
        or any(not isinstance(value, int) for value in shape)
        or shape[0] != chunk_count
    ):
        raise ShareError("文字チャンクとベクトルの件数が一致しません。")
    rows, dimension = shape
    if rows:
        if dimension <= 0 or dimension > _MAX_VECTOR_DIM:
            raise ShareError("共有データのベクトル次元が不正です。")
    elif dimension != 0:
        raise ShareError("空の共有データに不要なベクトルが含まれています。")

    payload_offset = stream.tell()
    expected_size = payload_offset + rows * dimension * np.dtype("float32").itemsize
    if expected_size != len(raw) or expected_size > _MAX_VECTORS_BYTES:
        raise ShareError("共有データのベクトルpayload長が不正です。")
    return rows, dimension


def _normalize_unit_vectors(vectors: np.ndarray) -> np.ndarray:
    if not np.isfinite(vectors).all():
        raise ShareError("共有データのベクトルに不正な値が含まれています。")
    if vectors.shape[0] == 0:
        return np.ascontiguousarray(vectors, dtype="float32")
    normalized = np.asarray(vectors, dtype="float32", order="C")
    with np.errstate(over="ignore", invalid="ignore"):
        norms = np.linalg.norm(normalized, axis=1)
    if (
        not np.isfinite(norms).all()
        or np.any(norms <= 0.0)
        or not np.allclose(norms, 1.0, rtol=1e-3, atol=1e-4)
    ):
        raise ShareError("共有データのベクトルが単位長へ正規化されていません。")
    normalized /= norms[:, None]
    return normalized


def _validate_vectors(raw: bytes, chunk_count: int) -> np.ndarray:
    _validate_npy_header(raw, chunk_count)
    try:
        vectors = np.load(io.BytesIO(raw), allow_pickle=False)
    except (OSError, ValueError, EOFError, TypeError, MemoryError) as exc:
        raise ShareError("共有データのベクトルを読み込めません。") from exc
    return _normalize_unit_vectors(vectors)


def _validate_embedding_metadata(
    manifest: dict[str, Any], vectors: np.ndarray, *, legacy: bool
) -> None:
    if vectors.shape[0] and int(vectors.shape[1]) != int(config.EMBED_VECTOR_DIM):
        raise ShareError("共有ベクトルの次元がローカルembedding modelと一致しません。")
    if legacy:
        return
    embedding = manifest.get("embedding")
    if not isinstance(embedding, dict):
        raise ShareError("共有データのembedding情報がありません。")
    if embedding.get("model") != config.EMBED_MODEL_NAME:
        raise ShareError("共有データのembedding modelに互換性がありません。")
    if embedding.get("dtype") != "float32" or embedding.get("normalized") is not True:
        raise ShareError("共有データのembedding形式に互換性がありません。")
    dimension = embedding.get("dimension")
    if (
        isinstance(dimension, bool)
        or not isinstance(dimension, int)
        or dimension != int(vectors.shape[1])
    ):
        raise ShareError("共有データのembedding次元が一致しません。")


def _read_package(zip_path: Path) -> tuple[dict[str, Any], np.ndarray]:
    try:
        if zip_path.stat().st_size > _MAX_PACKAGE_BYTES + _MAX_ZIP_OVERHEAD_BYTES:
            raise ShareError("共有zipのファイルサイズが上限を超えています。")
        with zipfile.ZipFile(zip_path, "r") as zf:
            infos = zf.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or set(names) != _ALLOWED_ENTRIES:
                raise ShareError("共有zipの内容が許可された構成ではありません。")
            if any(info.is_dir() for info in infos):
                raise ShareError("共有zipに不正なディレクトリが含まれています。")
            sizes = {info.filename: info.file_size for info in infos}
            compressed_sizes = {info.filename: info.compress_size for info in infos}
            if (
                sizes["manifest.json"] > _MAX_MANIFEST_BYTES
                or sizes["vectors.npy"] > _MAX_VECTORS_BYTES
                or sum(sizes.values()) > _MAX_PACKAGE_BYTES
                or compressed_sizes["manifest.json"] > _MAX_MANIFEST_BYTES
                or compressed_sizes["vectors.npy"] > _MAX_VECTORS_BYTES
                or sum(compressed_sizes.values()) > _MAX_PACKAGE_BYTES
            ):
                raise ShareError("共有zipの展開サイズが上限を超えています。")
            manifest_raw = zf.read("manifest.json")
            vectors_raw = zf.read("vectors.npy")
            if (
                len(manifest_raw) != sizes["manifest.json"]
                or len(vectors_raw) != sizes["vectors.npy"]
            ):
                raise ShareError("共有zipのentryサイズが一致しません。")
    except ShareError:
        raise
    except (
        OSError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        KeyError,
        RuntimeError,
        MemoryError,
        zlib.error,
    ) as exc:
        raise ShareError("共有zipを読み込めません。") from exc

    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ShareError("共有zipのmanifestが不正です。") from exc
    if not isinstance(manifest, dict):
        raise ShareError("共有zipのmanifestが不正です。")

    # 旧v1はformat/schema_versionの両方がない既知shapeだけを読み込む。
    # 片方だけ欠けた現行風packageをlegacyへdowngradeしない。
    has_format = "format" in manifest
    has_schema_version = "schema_version" in manifest
    is_legacy = not has_format and not has_schema_version
    if has_format != has_schema_version:
        raise ShareError("共有zipの形式情報が不完全です。")
    if not is_legacy and (
        manifest.get("format") != PACKAGE_FORMAT
        or manifest.get("schema_version") not in {PACKAGE_SCHEMA_VERSION, LEGACY_REDACTED_SCHEMA_VERSION}
    ):
        raise ShareError("未対応の共有zip形式です。")

    video = manifest.get("video")
    if not isinstance(video, dict):
        raise ShareError("共有データの動画情報が不正です。")
    if is_legacy and (
        not isinstance(video.get("video_id"), str)
        or not isinstance(video.get("path"), str)
    ):
        raise ShareError("旧形式の共有データshapeが不正です。")
    duration = _safe_float(video.get("duration"), "duration")
    public_id = video.get("public_video_id")
    if public_id is not None and (
        not isinstance(public_id, str) or not public_id.startswith(db.PUBLIC_ID_PREFIX)
    ):
        raise ShareError("共有データのpublic video IDが不正です。")
    content_digest = video.get("content_digest")
    if content_digest is not None and (
        not isinstance(content_digest, str) or len(content_digest) != 64
        or any(char not in "0123456789abcdef" for char in content_digest)
    ):
        raise ShareError("共有データのcontent digestが不正です。")
    segments = _sanitize_ranges(manifest.get("segments"), "segments")
    chunks = _sanitize_ranges(manifest.get("chunks"), "chunks")
    vectors = _validate_vectors(vectors_raw, len(chunks))
    _validate_embedding_metadata(manifest, vectors, legacy=is_legacy)
    return {
        "duration": duration,
        "segments": segments,
        "chunks": chunks,
        "legacy": is_legacy,
        "public_video_id": video.get("public_video_id"),
        "content_digest": video.get("content_digest"),
    }, vectors


def export_index(
    video_id: str,
    out_dir: Path = Path("exports"),
    *,
    confirm_sensitive: bool = False,
) -> Path:
    """指定動画の匿名化インデックスをzipへ出力する。

    パッケージには全文文字起こし、単語時刻、検索チャンク、埋め込みが
    含まれる。送信元パスや旧video_idは含めない。
    """
    if not confirm_sensitive:
        raise ShareError(
            "全文文字起こし等を含むため、内容を確認してからエクスポートしてください。"
        )

    conn = db.get_conn()
    db.init_db(conn)
    try:
        video = db.get_video(conn, video_id)
        if not video:
            raise ShareError("選択した動画のインデックスが見つかりません。")

        raw_segments = db.get_segments(conn, video_id)
        storage_id = video["video_id"]
        chunk_rows = conn.execute(
            "SELECT chunk_id, start_sec, end_sec, text FROM text_chunks "
            "WHERE video_id = ? ORDER BY chunk_id",
            (storage_id,),
        ).fetchall()
        raw_chunks = [dict(row) for row in chunk_rows]
        chunk_ids = [int(chunk["chunk_id"]) for chunk in raw_chunks]

        segments = _sanitize_ranges(raw_segments, "segments")
        chunks = _sanitize_ranges(raw_chunks, "chunks")
        duration = _safe_float(video.get("duration"), "duration")

        if chunk_ids:
            if not config.TEXT_INDEX_PATH.exists():
                raise ShareError("FAISSインデックスが見つかりません。")
            vindex = VectorIndex.load(config.TEXT_INDEX_PATH, 1)
            try:
                vectors = np.stack(
                    [vindex.index.reconstruct(chunk_id) for chunk_id in chunk_ids]
                ).astype("float32")
            except (RuntimeError, ValueError) as exc:
                raise ShareError("DBとFAISSインデックスが一致していません。") from exc
            vectors = _normalize_unit_vectors(vectors)
            if int(vectors.shape[1]) != int(config.EMBED_VECTOR_DIM):
                raise ShareError("FAISSの次元が設定中のembedding modelと一致しません。")
        else:
            vectors = np.zeros((0, 0), dtype="float32")

        package_id = _opaque_id("package")
        public_id = video.get("public_video_id") or db.public_video_id(conn, storage_id)
        content_payload = json.dumps(
            {"duration": duration, "segments": segments, "chunks": chunks},
            ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")
        content_digest = hashlib.sha256(content_payload + vectors.tobytes()).hexdigest()
        manifest = {
            "format": PACKAGE_FORMAT,
            "schema_version": PACKAGE_SCHEMA_VERSION,
            "package": {"id": package_id},
            "privacy": {
                "contains_transcript": True,
                "contains_word_timestamps": True,
                "contains_embeddings": True,
                "source_path_included": False,
                "source_name_included": False,
            },
            "video": {
                "public_video_id": public_id,
                "display_name": "共有動画",
                "duration": duration,
                "content_digest": content_digest,
            },
            "embedding": {
                "model": config.EMBED_MODEL_NAME,
                "revision": "configured",
                "dtype": "float32",
                "dimension": int(vectors.shape[1]) if vectors.ndim == 2 else 0,
                "normalized": True,
            },
            "chunking": {
                "seconds": config.CHUNK_SEC,
                "overlap_seconds": config.OVERLAP_SEC,
            },
            "package_source_token": _opaque_id("source"),
            "segments": segments,
            "chunks": chunks,
        }

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"shared-index-{uuid.uuid4().hex[:12]}.vindex.zip"
        vectors_buffer = io.BytesIO()
        np.save(vectors_buffer, vectors, allow_pickle=False)
        vectors_raw = vectors_buffer.getvalue()
        manifest_raw = json.dumps(
            manifest, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if (
            len(manifest_raw) > _MAX_MANIFEST_BYTES
            or len(vectors_raw) > _MAX_VECTORS_BYTES
            or len(manifest_raw) + len(vectors_raw) > _MAX_PACKAGE_BYTES
        ):
            raise ShareError("共有データが安全な出力上限を超えています。")

        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=".vindex.zip.tmp", dir=out_dir, delete=False
            ) as temporary:
                temp_path = Path(temporary.name)
            with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("manifest.json", manifest_raw)
                zf.writestr("vectors.npy", vectors_raw)
            if temp_path.stat().st_size > _MAX_PACKAGE_BYTES + _MAX_ZIP_OVERHEAD_BYTES:
                raise ShareError("共有zipのファイルサイズが上限を超えています。")
            os.replace(temp_path, out_path)
            temp_path = None
        except OSError as exc:
            raise ShareError("共有zipを安全に保存できませんでした。") from exc
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

        return out_path
    finally:
        conn.close()


def _compensate_imported_video(conn, video_id: str) -> None:
    """FAISS publish失敗時に、commit済みの新規行を可能な限り除去する。"""
    try:
        db.delete_video(conn, video_id)
        return
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

        recovery_conn = None
        try:
            recovery_conn = db.get_conn()
            recovery_conn.execute("DELETE FROM text_chunks WHERE video_id = ?", (video_id,))
            recovery_conn.execute("DELETE FROM asr_segments WHERE video_id = ?", (video_id,))
            recovery_conn.execute("DELETE FROM videos WHERE video_id = ?", (video_id,))
            recovery_conn.commit()
            return
        except Exception as recovery_error:
            if recovery_conn is not None:
                try:
                    recovery_conn.rollback()
                except Exception:
                    pass
            raise ShareError(
                "検索インデックス公開後のDB補償にも失敗しました。"
                "再起動前にインデックス整合性を確認してください。"
            ) from recovery_error
        finally:
            if recovery_conn is not None:
                recovery_conn.close()


def import_index(zip_path: Path) -> Iterator[str]:
    """共有zipを検証してインポートする。進捗メッセージをyieldする。"""
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise ShareError("インポートする共有zipが見つかりません。")

    package, vectors = _read_package(zip_path)
    requested_public_id = package.get("public_video_id")
    video_id = (
        requested_public_id
        if isinstance(requested_public_id, str) and requested_public_id.startswith(db.PUBLIC_ID_PREFIX)
        else db.new_public_video_id()
    )
    placeholder_path = (Path("video") / "__unlinked__" / f"{video_id}.mp4").as_posix()

    conn = db.get_conn()
    db.init_db(conn)
    publication_row = conn.execute(
        "SELECT current_publication_id FROM library_state WHERE singleton = 1"
    ).fetchone()
    expected_publication = str(publication_row[0]) if publication_row and publication_row[0] else None
    temp_index_path: Path | None = None
    db_committed = False
    try:
        yield "インポートを開始しました（送信元のパスと旧IDは使用しません）"
        existing = db.get_video(conn, video_id)
        if existing:
            if package.get("content_digest") and existing.get("content_digest") == package["content_digest"]:
                yield "  同一内容の動画は登録済みです"
                return
            raise ShareError("同じpublic video IDに異なる内容が登録されています。")
        conn.execute("BEGIN")
        db.insert_video(conn, video_id, placeholder_path, package["duration"])
        conn.execute(
            "UPDATE videos SET source_state = 'missing', content_digest = ? "
            "WHERE video_id = ?",
            (package.get("content_digest"), video_id),
        )
        yield "  匿名化した動画情報を登録しました"

        for segment in package["segments"]:
            conn.execute(
                "INSERT INTO asr_segments "
                "(video_id, start_sec, end_sec, text, words_json) VALUES (?, ?, ?, ?, ?)",
                (
                    video_id,
                    segment["start_sec"],
                    segment["end_sec"],
                    segment["text"],
                    segment.get("words_json"),
                ),
            )
        yield f"  文字起こしセグメントを登録しました ({len(package['segments'])} 件)"

        revision = db.mark_asr_complete(conn, video_id, commit=False)

        new_chunk_ids: list[int] = []
        for chunk in package["chunks"]:
            cursor = conn.execute(
                "INSERT INTO text_chunks (video_id, start_sec, end_sec, text, transcript_revision) "
                "VALUES (?, ?, ?, ?, ?)",
                (video_id, chunk["start_sec"], chunk["end_sec"], chunk["text"], revision),
            )
            new_chunk_ids.append(int(cursor.lastrowid))
        yield f"  検索用チャンクを登録しました ({len(new_chunk_ids)} 件)"

        if new_chunk_ids:
            index_path = Path(config.TEXT_INDEX_PATH)
            index_path.parent.mkdir(parents=True, exist_ok=True)
            if index_path.exists():
                vindex = VectorIndex.load(index_path, int(vectors.shape[1]))
                actual_dim = int(getattr(vindex.index, "d", vectors.shape[1]))
                if actual_dim != int(vectors.shape[1]):
                    raise ShareError("既存インデックスと共有ベクトルの次元が一致しません。")
            else:
                vindex = VectorIndex(int(vectors.shape[1]))
            previous_count = int(vindex.index.ntotal)
            vindex.add(np.asarray(new_chunk_ids, dtype="int64"), vectors)
            with tempfile.NamedTemporaryFile(
                suffix=".index.tmp", dir=index_path.parent, delete=False
            ) as temporary:
                temp_index_path = Path(temporary.name)
            vindex.save(temp_index_path)
            verified = VectorIndex.load(temp_index_path, int(vectors.shape[1]))
            if (
                int(getattr(verified.index, "d", -1)) != int(vectors.shape[1])
                or int(verified.index.ntotal) != previous_count + len(new_chunk_ids)
            ):
                raise ShareError("一時検索インデックスの検証に失敗しました。")
            for chunk_id, expected in zip(new_chunk_ids, vectors):
                try:
                    actual = verified.index.reconstruct(int(chunk_id))
                except RuntimeError as exc:
                    raise ShareError("一時検索インデックスのID検証に失敗しました。") from exc
                if not np.allclose(actual, expected, rtol=1e-5, atol=1e-6):
                    raise ShareError("一時検索インデックスのvector検証に失敗しました。")

        conn.commit()
        db_committed = True

        if temp_index_path is not None:
            try:
                os.replace(temp_index_path, config.TEXT_INDEX_PATH)
                temp_index_path = None
            except OSError as exc:
                # Phase 2の世代publish導入までの補償処理。DBだけが残る状態を避ける。
                _compensate_imported_video(conn, video_id)
                db_committed = False
                raise ShareError("検索インデックスを安全に公開できませんでした。") from exc
            yield "  FAISSインデックスへ登録しました"

        try:
            from .publication import publish_current_generation, publish_text_snapshot
            if new_chunk_ids:
                publish_current_generation(conn, expected_publication)
            else:
                publish_text_snapshot(conn, expected_publication)
        except Exception as exc:
            _compensate_imported_video(conn, video_id)
            db_committed = False
            raise ShareError("共有インデックスのpublication公開に失敗しました。") from exc

        if package["legacy"]:
            yield "  旧形式を安全に変換し、送信元のパスと旧IDを破棄しました"
        yield "警告: 元動画は未接続です。検索はできますが、プレビュー・保存には再関連付けが必要です。"
        yield "インポートが完了しました。"
    except ShareError:
        if not db_committed:
            conn.rollback()
        raise
    except Exception as exc:
        if not db_committed:
            conn.rollback()
        raise ShareError("共有インデックスの登録に失敗しました。") from exc
    finally:
        if temp_index_path is not None:
            temp_index_path.unlink(missing_ok=True)
        conn.close()


def relink_video(public_video_id: str, source_path: Path, *, duration_tolerance: float = 0.25) -> dict:
    """Attach an unlinked shared transcript to a local source after validation."""
    from . import utils
    from .publication import private_source_fingerprint

    source_path = Path(source_path)
    if not source_path.is_file():
        raise ShareError("再関連付けする元動画が見つかりません。")
    actual_duration = float(utils.probe_duration(source_path))
    conn = db.get_conn()
    db.init_db(conn)
    try:
        publication_row = conn.execute(
            "SELECT current_publication_id FROM library_state WHERE singleton = 1"
        ).fetchone()
        expected_publication = str(publication_row[0]) if publication_row and publication_row[0] else None
        video = db.get_video(conn, public_video_id)
        if not video:
            raise ShareError("再関連付け対象の動画が見つかりません。")
        expected_duration = float(video.get("duration") or 0.0)
        if abs(actual_duration - expected_duration) > duration_tolerance:
            raise ShareError("動画の長さが共有インデックスと一致しないため、関連付けを拒否しました。")
        fingerprint = private_source_fingerprint(source_path)
        old_generation = video.get("source_generation")
        new_generation = f"src_{uuid.uuid4().hex}"
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO sources(source_generation, public_video_id, locator, private_fingerprint, status) "
            "VALUES(?, ?, ?, ?, 'available')",
            (new_generation, public_video_id, str(source_path.resolve()), fingerprint),
        )
        conn.execute(
            "UPDATE videos SET path = ?, source_generation = ?, source_state = 'available' "
            "WHERE public_video_id = ?",
            (str(source_path.resolve()), new_generation, public_video_id),
        )
        conn.execute(
            "UPDATE active_transcripts SET source_generation = ? WHERE public_video_id = ?",
            (new_generation, public_video_id),
        )
        conn.execute(
            "UPDATE transcript_revisions SET source_generation = ? WHERE source_generation = ?",
            (new_generation, old_generation),
        )
        conn.commit()
        from .publication import publish_text_snapshot
        publish_text_snapshot(conn, expected_publication)
        return db.get_public_video(conn, public_video_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
