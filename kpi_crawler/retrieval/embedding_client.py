"""Local embedding runtime client.

Model: Qwen3-Embedding-4B, served locally by Ollama (a GGUF/llama.cpp runtime)
on this machine's GPU. No API key, no external network call at query time —
Ollama is a local daemon on localhost, the model is already pulled to local
disk, and inference runs on the local GPU.

Why Ollama and not a Python embedding library directly: the naive route
(`transformers`/`sentence-transformers` loading the official bf16 safetensors)
needs ~8 GB of VRAM for weights alone, which does not fit this machine's GPU
(8 GB total, ~6.6 GB free). Ollama already had `qwen3-embedding:4b` available
as a Q4_K_M GGUF quantization (~4.4 GB resident, confirmed 100% GPU-resident),
which is the same model, correctly runnable on this hardware — a runtime
change, not a model substitution.
"""

from dataclasses import dataclass
import hashlib
import json
import math
import urllib.error
import urllib.request

from ..errors import EmbeddingError

DEFAULT_ENDPOINT = "http://localhost:11434/api/embed"
DEFAULT_MODEL = "qwen3-embedding:4b"

# Fixed for the entire experiment (Step 3 / Step 12): no per-representation,
# per-source, or per-query variation. Qwen3-Embedding models are not
# instruction-tuned the way some retrieval embedders are for the *document*
# side; no instruction prefix is used for either evidence or KPI text.
EMBEDDING_INSTRUCTION: str | None = None


@dataclass(frozen=True)
class EmbeddingConfig:
    model_identifier: str = DEFAULT_MODEL
    endpoint: str = DEFAULT_ENDPOINT
    expected_dimension: int = 2560
    normalize: bool = True
    instruction: str | None = EMBEDDING_INSTRUCTION
    timeout_seconds: float = 180.0
    batch_size: int = 64


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0.0:
        return vector
    return [component / norm for component in vector]


def checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class EmbeddingClient:
    """Thin client over Ollama's local `/api/embed` endpoint."""

    def __init__(self, config: EmbeddingConfig = EmbeddingConfig()):
        self._config = config

    @property
    def config(self) -> EmbeddingConfig:
        return self._config

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, applying the fixed instruction/normalization config."""
        if not texts:
            return []
        inputs = [f"{self._config.instruction}{text}" if self._config.instruction else text for text in texts]
        payload = json.dumps({"model": self._config.model_identifier, "input": inputs}).encode("utf-8")
        request = urllib.request.Request(
            self._config.endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._config.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise EmbeddingError(
                f"could not reach local embedding runtime at {self._config.endpoint}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise EmbeddingError(f"local embedding runtime returned invalid JSON: {exc}") from exc

        embeddings = body.get("embeddings")
        if not embeddings or len(embeddings) != len(texts):
            raise EmbeddingError(
                f"local embedding runtime returned {len(embeddings) if embeddings else 0} "
                f"embeddings for {len(texts)} inputs"
            )
        for vector in embeddings:
            if len(vector) != self._config.expected_dimension:
                raise EmbeddingError(
                    f"embedding dimension mismatch: expected {self._config.expected_dimension}, "
                    f"got {len(vector)} from model {self._config.model_identifier!r}"
                )
        if self._config.normalize:
            embeddings = [_l2_normalize(vector) for vector in embeddings]
        return embeddings

    def embed_all(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts in fixed-size batches, preserving order."""
        results: list[list[float]] = []
        batch_size = self._config.batch_size
        for start in range(0, len(texts), batch_size):
            results.extend(self.embed_batch(texts[start : start + batch_size]))
        return results
