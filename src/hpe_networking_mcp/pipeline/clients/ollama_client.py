import os
import time

import httpx

OLLAMA_URL = "http://localhost:11434"
EMBEDDING_MODEL = "nomic-embed-text"
# nomic-embed-text context window; truncate to stay safe
_MAX_CHARS = 6000

# nomic-embed-text task prefixes. The current 40,900-doc corpus was embedded
# WITHOUT prefixes, so prefixing queries only would mismatch the stored vectors
# and degrade retrieval. Gate both sides behind one flag so they flip together
# with a full re-ingest. Default off to match the existing corpus.
_NOMIC_PREFIXES = os.getenv("HPE_MCP_NOMIC_PREFIXES", "").lower() in ("1", "true", "yes")
_DOC_PREFIX = "search_document: "
_QUERY_PREFIX = "search_query: "
# Batch size for the native /api/embed endpoint.
_EMBED_BATCH = 64


class OllamaClient:
    def __init__(self, url: str = OLLAMA_URL, model: str = EMBEDDING_MODEL):
        self.url = url
        self.model = model
        self._client = httpx.Client(timeout=60.0)

    def embed(self, text: str) -> list[float]:
        text = text[:_MAX_CHARS]
        for attempt in range(3):
            try:
                resp = self._client.post(
                    f"{self.url}/api/embeddings",
                    json={"model": self.model, "prompt": text},
                )
                resp.raise_for_status()
                return resp.json()["embedding"]
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 500 and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise RuntimeError("Ollama embed failed after 3 attempts")

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed many texts via the native /api/embed batch endpoint.

        POSTs {"model":..., "input":[...]} in slices of ~64 and reads back
        {"embeddings": [...]}. Falls back to the sequential legacy path
        (self.embed per text) on HTTP error so ingest never hard-stops.
        """
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _EMBED_BATCH):
            slice_texts = [t[:_MAX_CHARS] for t in texts[start : start + _EMBED_BATCH]]
            try:
                resp = self._client.post(
                    f"{self.url}/api/embed",
                    json={"model": self.model, "input": slice_texts},
                )
                resp.raise_for_status()
                vectors.extend(resp.json()["embeddings"])
            except httpx.HTTPError:
                vectors.extend(self.embed(t) for t in slice_texts)
        return vectors

    def embed_document(self, texts: list[str]) -> list[list[float]]:
        """Embed passages for indexing.

        When HPE_MCP_NOMIC_PREFIXES is set, prepends nomic's
        "search_document: " task prefix. This MUST flip together with
        embed_query() and a full re-ingest — the live corpus was embedded
        without prefixes, so a one-sided change degrades retrieval.
        """
        if _NOMIC_PREFIXES:
            texts = [_DOC_PREFIX + t for t in texts]
        return self.embed_batch(texts)

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string.

        When HPE_MCP_NOMIC_PREFIXES is set, prepends nomic's
        "search_query: " task prefix. Must stay in lockstep with
        embed_document() and a full re-ingest (see embed_document docstring).
        """
        if _NOMIC_PREFIXES:
            text = _QUERY_PREFIX + text
        return self.embed(text)

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
