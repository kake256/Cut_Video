from pathlib import Path
from typing import Tuple

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

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self.index, str(path))

    @classmethod
    def load(cls, path: Path, dim: int) -> "VectorIndex":
        obj = cls(dim)
        obj.index = faiss.read_index(str(path))
        return obj
