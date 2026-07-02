from typing import List, Optional

import numpy as np

from . import config


class TextEmbedder:
    """BGE-M3によるテキスト埋め込み。FlagEmbeddingパッケージが必要。"""

    def __init__(
        self,
        model_name: Optional[str] = None,
        use_fp16: bool = True,
        device: Optional[str] = None,
    ):
        from FlagEmbedding import BGEM3FlagModel

        model_name = model_name or config.EMBED_MODEL_NAME
        self.model = BGEM3FlagModel(model_name, use_fp16=use_fp16, device=device)

    def encode(self, texts: List[str], batch_size: int = 12) -> np.ndarray:
        output = self.model.encode(texts, batch_size=batch_size, max_length=512)
        vectors = np.asarray(output["dense_vecs"], dtype="float32")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vectors / norms
