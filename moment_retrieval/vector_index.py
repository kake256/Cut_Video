from pathlib import Path
from typing import Iterable, Tuple

import faiss
import numpy as np


class VectorIndex:
    """chunk_id(int) <-> 埋め込みベクトルを扱うFAISSラッパー(内積=正規化済みコサイン類似度)。"""

    def __init__(self, dim: int):
        self.dim = dim
        self.index = faiss.IndexIDMap2(faiss.IndexFlatIP(dim))

    def add(self, ids: np.ndarray, vectors: np.ndarray) -> None:
        self.index.add_with_ids(vectors.astype("float32"), ids.astype("int64"))

    def remove(self, ids: np.ndarray) -> None:
        self.index.remove_ids(ids.astype("int64"))

    def search(self, query_vec: np.ndarray, top_k: int = 10) -> Tuple[np.ndarray, np.ndarray]:
        scores, ids = self.index.search(query_vec.astype("float32"), top_k)
        return scores[0], ids[0]

    def score_ids(
        self, query_vec: np.ndarray, ids: Iterable[int]
    ) -> Tuple[np.ndarray, np.ndarray]:
        """指定IDだけをクエリとの類似度順に並べる。

        IndexIDMap2は検索時のIDフィルターを単純には指定できないため、対象動画の
        ベクトルを復元して内積を計算する。動画単位の数百〜数千チャンクを対象に
        する用途では十分軽量で、動画別インデックスも不要になる。
        """
        valid_ids = []
        vectors = []
        for chunk_id in ids:
            chunk_id = int(chunk_id)
            try:
                vector = self.index.reconstruct(chunk_id)
            except RuntimeError:
                # DBとFAISSに不整合があるIDは検索候補から除外する。
                continue
            valid_ids.append(chunk_id)
            vectors.append(vector)

        if not vectors:
            return np.empty(0, dtype="float32"), np.empty(0, dtype="int64")

        matrix = np.asarray(vectors, dtype="float32")
        query = np.asarray(query_vec, dtype="float32")[0]
        scores = matrix @ query
        order = np.argsort(-scores)
        return scores[order], np.asarray(valid_ids, dtype="int64")[order]

    def search_ids(
        self, query_vec: np.ndarray, ids: Iterable[int], top_k: int = 10
    ) -> Tuple[np.ndarray, np.ndarray]:
        """指定ID集合の中から上位top_k件を検索する。"""
        scores, ranked_ids = self.score_ids(query_vec, ids)
        top_k = max(0, int(top_k))
        return scores[:top_k], ranked_ids[:top_k]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self.index, str(path))

    @classmethod
    def load(cls, path: Path, dim: int) -> "VectorIndex":
        obj = cls(dim)
        obj.index = faiss.read_index(str(path))
        return obj
