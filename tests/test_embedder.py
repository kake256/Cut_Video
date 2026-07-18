import unittest
from unittest.mock import patch

import numpy as np

from moment_retrieval import config
from moment_retrieval.embedder import TextEmbedder


class _SyntheticModel:
    def __init__(self, vectors):
        self.vectors = vectors

    def encode(self, texts, **_kwargs):
        return {"dense_vecs": self.vectors[: len(texts)]}


class TextEmbedderContractTest(unittest.TestCase):
    def test_encode_normalizes_the_configured_dimension(self):
        embedder = TextEmbedder.__new__(TextEmbedder)
        embedder.model = _SyntheticModel([[3.0, 4.0]])
        with patch.object(config, "EMBED_VECTOR_DIM", 2):
            vectors = embedder.encode(["synthetic"])
        np.testing.assert_allclose(
            vectors,
            np.asarray([[0.6, 0.8]], dtype="float32"),
            atol=1e-6,
        )

    def test_encode_rejects_an_unexpected_model_dimension(self):
        embedder = TextEmbedder.__new__(TextEmbedder)
        embedder.model = _SyntheticModel([[1.0, 0.0, 0.0]])
        with patch.object(config, "EMBED_VECTOR_DIM", 2):
            with self.assertRaisesRegex(ValueError, "出力次元"):
                embedder.encode(["synthetic"])


if __name__ == "__main__":
    unittest.main()
