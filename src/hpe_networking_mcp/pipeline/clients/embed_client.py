"""In-process embeddings via fastembed (ONNX) — no Ollama server required.

Runs nomic-embed-text-v1.5 locally (same model semantics as the Ollama
`nomic-embed-text` deployment it replaces) with the task prefixes nomic
requires (R3): passages get "search_document: ", queries "search_query: ".
Unlike the Ollama path, prefixes are ALWAYS on — this client only ever talks
to the LanceDB corpus, which is ingested with prefixes from day one.

fastembed batches natively (R4), so a full re-ingest is minutes, not a 40k-call
serial loop. The ONNX model (~250 MB) downloads to the HF cache on first use.
"""

from __future__ import annotations

import os
from typing import Iterable

EMBEDDING_MODEL = "nomic-ai/nomic-embed-text-v1.5"
EMBEDDING_DIMS = 768
# nomic-embed context window; truncate to stay safe (matches the Ollama client)
_MAX_CHARS = 6000
_DOC_PREFIX = "search_document: "
_QUERY_PREFIX = "search_query: "

# Optional ONNX execution providers, comma-separated short names, e.g.
# HPE_MCP_EMBED_PROVIDERS=coreml for Apple Neural Engine acceleration
# during a local index build. fp16 EP deviation from CPU f32 is ~1e-3 cosine —
# negligible, unlike cross-implementation embedders (Ollama GGUF measured
# 0.75-0.96 agreement with rank flips; never mix those).
_PROVIDER_ALIASES = {
    "coreml": "CoreMLExecutionProvider",
    "cuda": "CUDAExecutionProvider",
    "cpu": "CPUExecutionProvider",
}


def _providers_from_env() -> list[str] | None:
    raw = os.getenv("HPE_MCP_EMBED_PROVIDERS", "").strip()
    if not raw:
        return None
    names = [_PROVIDER_ALIASES.get(p.strip().lower(), p.strip()) for p in raw.split(",")]
    if "CPUExecutionProvider" not in names:
        names.append("CPUExecutionProvider")  # always keep the fallback
    return names


class EmbedClient:
    """Drop-in replacement for OllamaClient's embed_document/embed_query."""

    def __init__(self, model: str = EMBEDDING_MODEL, providers: list[str] | None = None):
        self.model_name = model
        self.providers = providers if providers is not None else _providers_from_env()
        self._model = None  # lazy — keep MCP server startup fast

    @property
    def model(self):
        if self._model is None:
            from fastembed import TextEmbedding
            kwargs = {"providers": self.providers} if self.providers else {}
            self._model = TextEmbedding(model_name=self.model_name, **kwargs)
        return self._model

    def embed_document(self, texts: Iterable[str], batch_size: int = 64) -> list[list[float]]:
        """Embed passages for indexing, with nomic's search_document prefix."""
        prefixed = [_DOC_PREFIX + t[:_MAX_CHARS] for t in texts]
        return [v.tolist() for v in self.model.embed(prefixed, batch_size=batch_size)]

    def iter_embed_documents(
        self,
        texts: Iterable[str],
        batch_size: int = 32,
        parallel: int | None = None,
    ) -> Iterable[list[float]]:
        """Stream document embeddings for a large corpus.

        One embed() call over the whole iterable so fastembed's worker pool
        (parallel=N data-parallel ONNX sessions) spawns ONCE — calling
        embed_document per batch would respawn workers (and reload the model)
        every call. Callers in worker-spawning contexts must run under an
        `if __name__ == "__main__":` guard.
        """
        prefixed = (_DOC_PREFIX + t[:_MAX_CHARS] for t in texts)
        for v in self.model.embed(prefixed, batch_size=batch_size, parallel=parallel):
            yield v.tolist()

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string, with nomic's search_query prefix."""
        vectors = list(self.model.embed([_QUERY_PREFIX + text[:_MAX_CHARS]]))
        return vectors[0].tolist()

    def warm_up(self) -> None:
        """Load the ONNX model before serving the first user query."""
        self.embed_query("warm up local documentation retrieval")
