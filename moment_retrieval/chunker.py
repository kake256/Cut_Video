from dataclasses import dataclass
from typing import List

from .asr import Segment


@dataclass
class Chunk:
    start: float
    end: float
    text: str


def build_chunks(
    segments: List[Segment], target_sec: float = 15.0, overlap_sec: float = 5.0
) -> List[Chunk]:
    """ASRセグメント(文単位)をtarget_sec程度の検索用チャンクにまとめる。

    セグメント境界でしか区切らないため、切り出し時の境界ずれが起きにくい。
    overlap_secにより隣接チャンクの先頭を重ねて、境界付近のクエリでも
    ヒットしやすくする。
    """
    if not segments:
        return []

    n = len(segments)
    chunks: List[Chunk] = []
    start_idx = 0

    while start_idx < n:
        cur_start = segments[start_idx].start
        end_idx = start_idx
        cur_end = segments[start_idx].end
        texts = []

        while end_idx < n and (segments[end_idx].end - cur_start) < target_sec:
            texts.append(segments[end_idx].text)
            cur_end = segments[end_idx].end
            end_idx += 1

        if end_idx == start_idx:
            # 1セグメント自体がtarget_secを超える場合でも最低1つは含める
            texts.append(segments[start_idx].text)
            cur_end = segments[start_idx].end
            end_idx += 1

        chunks.append(Chunk(start=cur_start, end=cur_end, text=" ".join(texts)))

        if end_idx >= n:
            break

        next_start_time = cur_end - overlap_sec
        new_start_idx = end_idx
        for idx in range(start_idx, end_idx):
            if segments[idx].start >= next_start_time:
                new_start_idx = idx
                break
        if new_start_idx <= start_idx:
            new_start_idx = start_idx + 1

        start_idx = new_start_idx

    return chunks
