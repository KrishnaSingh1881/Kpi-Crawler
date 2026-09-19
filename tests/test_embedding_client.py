import json
import math
import unittest
from unittest.mock import MagicMock, patch

from kpi_crawler.errors import EmbeddingError
from kpi_crawler.retrieval.embedding_client import EmbeddingClient, EmbeddingConfig, checksum


def _fake_response(body: dict) -> MagicMock:
    response = MagicMock()
    response.read.return_value = json.dumps(body).encode("utf-8")
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


class EmbeddingClientTests(unittest.TestCase):
    def test_embed_batch_returns_normalized_vectors_of_expected_dimension(self):
        config = EmbeddingConfig(expected_dimension=4, normalize=True, batch_size=8)
        client = EmbeddingClient(config)
        raw = [3.0, 4.0, 0.0, 0.0]  # norm = 5
        with patch("urllib.request.urlopen", return_value=_fake_response({"embeddings": [raw]})):
            vectors = client.embed_batch(["hello"])
        self.assertEqual(len(vectors), 1)
        self.assertEqual(len(vectors[0]), 4)
        norm = math.sqrt(sum(v * v for v in vectors[0]))
        self.assertAlmostEqual(norm, 1.0, places=6)
        self.assertAlmostEqual(vectors[0][0], 0.6, places=6)
        self.assertAlmostEqual(vectors[0][1], 0.8, places=6)

    def test_embed_batch_without_normalization_keeps_raw_values(self):
        config = EmbeddingConfig(expected_dimension=3, normalize=False)
        client = EmbeddingClient(config)
        with patch("urllib.request.urlopen", return_value=_fake_response({"embeddings": [[1.0, 2.0, 3.0]]})):
            vectors = client.embed_batch(["hello"])
        self.assertEqual(vectors, [[1.0, 2.0, 3.0]])

    def test_dimension_mismatch_raises_embedding_error(self):
        config = EmbeddingConfig(expected_dimension=10)
        client = EmbeddingClient(config)
        with patch("urllib.request.urlopen", return_value=_fake_response({"embeddings": [[1.0, 2.0]]})):
            with self.assertRaises(EmbeddingError):
                client.embed_batch(["hello"])

    def test_count_mismatch_raises_embedding_error(self):
        config = EmbeddingConfig(expected_dimension=2)
        client = EmbeddingClient(config)
        with patch("urllib.request.urlopen", return_value=_fake_response({"embeddings": [[1.0, 2.0]]})):
            with self.assertRaises(EmbeddingError):
                client.embed_batch(["hello", "world"])

    def test_empty_batch_makes_no_request(self):
        client = EmbeddingClient(EmbeddingConfig())
        with patch("urllib.request.urlopen") as mock_urlopen:
            result = client.embed_batch([])
        self.assertEqual(result, [])
        mock_urlopen.assert_not_called()

    def test_embed_all_preserves_order_across_batches(self):
        config = EmbeddingConfig(expected_dimension=1, normalize=False, batch_size=2)
        client = EmbeddingClient(config)
        texts = ["a", "b", "c", "d", "e"]

        def fake_urlopen(request, timeout):
            body = json.loads(request.data.decode("utf-8"))
            embeddings = [[float(ord(t[0]))] for t in body["input"]]
            return _fake_response({"embeddings": embeddings})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            vectors = client.embed_all(texts)
        self.assertEqual(vectors, [[float(ord(t))] for t in texts])

    def test_unreachable_endpoint_raises_embedding_error(self):
        import urllib.error

        client = EmbeddingClient(EmbeddingConfig())
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            with self.assertRaises(EmbeddingError):
                client.embed_batch(["hello"])

    def test_checksum_is_deterministic(self):
        self.assertEqual(checksum("same text"), checksum("same text"))
        self.assertNotEqual(checksum("same text"), checksum("different text"))


if __name__ == "__main__":
    unittest.main()
