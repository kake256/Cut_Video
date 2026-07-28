import sqlite3
from typing import Tuple

from . import config
from . import db


def expand_to_speech_boundary(
    conn: sqlite3.Connection,
    video_id: str,
    start_sec: float,
    end_sec: float,
    gap_sec: float = None,
    back_max: float = None,
    fwd_max: float = None,
) -> Tuple[float, float]:
    """ヒット区間をASRセグメント列に沿って「話の切れ目」まで拡張する。

    隣接セグメントとの間隔がgap_sec未満なら同じ話が続いているとみなして取り込み、
    gap_sec以上空いたところ(または最大延長量に達したところ)で止める。
    チャンク境界で話が途中で切れる問題への対処。
    """
    gap_sec = gap_sec if gap_sec is not None else config.BOUNDARY_GAP_SEC
    back_max = back_max if back_max is not None else config.EXTEND_BACK_MAX_SEC
    fwd_max = fwd_max if fwd_max is not None else config.EXTEND_FWD_MAX_SEC

    rows = db.get_segments_in_range(
        conn,
        video_id,
        max(0.0, start_sec - back_max - gap_sec),
        end_sec + fwd_max + gap_sec,
    )
    if not rows:
        return start_sec, end_sec

    segs = [(r["start_sec"], r["end_sec"]) for r in rows]

    # ヒット区間に重なるセグメントの範囲を特定
    first = last = None
    for i, (s, e) in enumerate(segs):
        if e > start_sec and s < end_sec:
            if first is None:
                first = i
            last = i
    if first is None:
        return start_sec, end_sec

    new_start = segs[first][0]
    new_end = segs[last][1]

    # 後ろ方向: 話が続いている限り延長 (「急に終わる」対策)
    i = last
    while i + 1 < len(segs):
        next_s, next_e = segs[i + 1]
        if next_s - segs[i][1] >= gap_sec:
            break
        if next_e - segs[last][1] > fwd_max:
            break
        new_end = next_e
        i += 1

    # 前方向: 文の頭が欠けないように少しだけ遡る
    i = first
    while i - 1 >= 0:
        prev_s, prev_e = segs[i - 1]
        if segs[i][0] - prev_e >= gap_sec:
            break
        if segs[first][0] - prev_s > back_max:
            break
        new_start = prev_s
        i -= 1

    return new_start, new_end
