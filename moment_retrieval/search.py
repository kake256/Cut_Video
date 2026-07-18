"""文字列一致と意味ベクトルを統合した検索処理。"""

from __future__ import annotations

import sqlite3
import unicodedata

import numpy as np

from .vector_index import VectorIndex

MATCH_TEXT = "文字一致"
MATCH_SEMANTIC = "意味検索"


def normalize_search_text(text: str) -> str:
    """表記揺れを抑えるため、検索語と文字起こしを同じ形式へ正規化する。"""
    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join(normalized.split())


def normalize_kana_search_text(text: str) -> str:
    """通常の正規化に加え、カタカナをひらがなへ統一する。"""
    normalized = normalize_search_text(text)
    chars = []
    for char in normalized:
        codepoint = ord(char)
        # ァ〜ヶは、対応するひらがなとの差がUnicode上で0x60。
        if 0x30A1 <= codepoint <= 0x30F6:
            char = chr(codepoint - 0x60)
        chars.append(char)
    return "".join(chars)


def normalize_cjk_compact_search_text(text: str) -> str:
    """Remove whitespace only when it separates two CJK/kana characters."""
    normalized = normalize_kana_search_text(text)

    def is_cjk(char: str) -> bool:
        codepoint = ord(char)
        return (0x3040 <= codepoint <= 0x30FF or 0x3400 <= codepoint <= 0x9FFF
                or 0xF900 <= codepoint <= 0xFAFF)

    chars = []
    for index, char in enumerate(normalized):
        if (char == " " and index and index + 1 < len(normalized)
                and is_cjk(normalized[index - 1]) and is_cjk(normalized[index + 1])):
            continue
        chars.append(char)
    return "".join(chars)


def _scope_chunks(
    conn: sqlite3.Connection,
    video_id: str | None,
    start_sec: float | None = None,
    end_sec: float | None = None,
    allowed_revisions: set[str] | None = None,
) -> list[dict]:
    """検索対象チャンクを絞り込む。start_sec/end_secは単一動画選択時のみ有効。

    チャンクには前後オーバーラップがあるため「範囲と重なる」判定にする
    (指定範囲の終了 > チャンク開始 かつ 指定範囲の開始 < チャンク終了)。
    """
    if video_id and start_sec is not None and end_sec is not None:
        rows = conn.execute(
            "SELECT * FROM text_chunks WHERE video_id = ? "
            "AND end_sec > ? AND start_sec < ? ORDER BY chunk_id",
            (video_id, start_sec, end_sec),
        ).fetchall()
    elif video_id:
        rows = conn.execute(
            "SELECT * FROM text_chunks WHERE video_id = ? ORDER BY chunk_id",
            (video_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM text_chunks ORDER BY chunk_id").fetchall()
    values = [dict(row) for row in rows]
    if allowed_revisions is not None:
        values = [row for row in values if row.get("transcript_revision") in allowed_revisions]
    return values


def _result(chunk: dict, score: float, match_type: str) -> dict:
    return {
        "chunk_id": int(chunk["chunk_id"]),
        "video_id": chunk["video_id"],
        "start": float(chunk["start_sec"]),
        "end": float(chunk["end_sec"]),
        "score": float(score),
        "match_type": match_type,
        "text": chunk["text"],
    }


def search_semantic_chunks(
    conn: sqlite3.Connection,
    vindex: VectorIndex,
    query: str,
    query_vec: np.ndarray,
    *,
    top_k: int = 5,
    min_score: float = 0.55,
    video_id: str | None = None,
    start_sec: float | None = None,
    end_sec: float | None = None,
    allowed_revisions: set[str] | frozenset[str] | None = None,
) -> list[dict]:
    """Return semantic candidates without consuming their quota with text hits.

    Publication-scoped searches rank only chunk IDs belonging to the selected
    transcript revisions.  This prevents stale vectors outside the active
    publication from occupying FAISS' global ``top_k`` results.
    """
    top_k = max(0, int(top_k))
    if top_k == 0 or not normalize_search_text(query):
        return []

    scoped_chunks = _scope_chunks(
        conn, video_id, start_sec, end_sec, allowed_revisions
    )
    chunks_by_id = {int(chunk["chunk_id"]): chunk for chunk in scoped_chunks}
    if not chunks_by_id:
        return []

    if video_id is not None or allowed_revisions is not None:
        semantic_scores, semantic_ids = vindex.search_ids(
            query_vec, chunks_by_id.keys(), top_k=top_k
        )
    else:
        semantic_scores, semantic_ids = vindex.search(query_vec, top_k=top_k)

    results = []
    for score, chunk_id in zip(semantic_scores, semantic_ids):
        chunk_id = int(chunk_id)
        if chunk_id == -1 or float(score) < float(min_score):
            continue
        chunk = chunks_by_id.get(chunk_id)
        if chunk is not None:
            results.append(_result(chunk, score, MATCH_SEMANTIC))
    return results


def search_chunks(
    conn: sqlite3.Connection,
    vindex: VectorIndex,
    query: str,
    query_vec: np.ndarray,
    *,
    top_k: int = 5,
    min_score: float = 0.55,
    video_id: str | None = None,
    start_sec: float | None = None,
    end_sec: float | None = None,
    allowed_revisions: set[str] | None = None,
) -> list[dict]:
    """正規化文字列一致を優先し、意味検索結果を続けて返す。

    文字列一致は類似度閾値の対象外。意味検索だけをmin_scoreで足切りする。
    video_id指定時は全体検索後のフィルターではなく、その動画のベクトルだけで
    順位を計算する。start_sec/end_secを指定すると、その動画内の指定範囲に
    重なるチャンクだけを対象にする(video_id未指定時は無視される)。
    """
    top_k = max(1, int(top_k))
    normalized_query = normalize_search_text(query)
    if not normalized_query:
        return []

    scoped_chunks = _scope_chunks(conn, video_id, start_sec, end_sec, allowed_revisions)
    chunks_by_id = {int(chunk["chunk_id"]): chunk for chunk in scoped_chunks}

    direct_text_ids = [
        chunk_id
        for chunk_id, chunk in chunks_by_id.items()
        if normalized_query in normalize_search_text(chunk["text"])
    ]
    direct_text_id_set = set(direct_text_ids)

    # 「ワンチャン」と「ワンちゃん」のようなかな表記ゆれは、通常一致より
    # 優先度の低い文字一致として補う。通常一致とUI上の分類は分けない。
    kana_query = normalize_kana_search_text(query)
    kana_text_ids = [
        chunk_id
        for chunk_id, chunk in chunks_by_id.items()
        if chunk_id not in direct_text_id_set
        and kana_query in normalize_kana_search_text(chunk["text"])
    ]
    cjk_query = normalize_cjk_compact_search_text(query)
    prior_ids = direct_text_id_set | set(kana_text_ids)
    cjk_text_ids = [
        chunk_id for chunk_id, chunk in chunks_by_id.items()
        if chunk_id not in prior_ids
        and cjk_query in normalize_cjk_compact_search_text(chunk["text"])
    ]

    if video_id:
        semantic_scores, semantic_ids = vindex.search_ids(
            query_vec, chunks_by_id.keys(), top_k=top_k
        )
    else:
        semantic_scores, semantic_ids = vindex.search(query_vec, top_k=top_k)

    results = []
    seen = set()

    # 正規化後の文字列一致は、意味類似度が低くても必ず候補に残す。
    for ranked_ids in (direct_text_ids, kana_text_ids, cjk_text_ids):
        for chunk_id in ranked_ids:
            chunk_id = int(chunk_id)
            chunk = chunks_by_id.get(chunk_id)
            if not chunk:
                continue
            # Text matches are deliberately independent of FAISS/model scores.
            results.append(_result(chunk, 0.0, MATCH_TEXT))
            seen.add(chunk_id)
            if len(results) >= top_k:
                return results

    # 同じチャンクが文字列一致にも出た場合は、文字列一致として1件だけ返す。
    for score, chunk_id in zip(semantic_scores, semantic_ids):
        chunk_id = int(chunk_id)
        if chunk_id == -1 or chunk_id in seen or float(score) < float(min_score):
            continue
        chunk = chunks_by_id.get(chunk_id)
        if not chunk:
            continue
        results.append(_result(chunk, score, MATCH_SEMANTIC))
        seen.add(chunk_id)
        if len(results) >= top_k:
            break

    return results
