from __future__ import annotations

from collections.abc import Sequence
from dataclasses import FrozenInstanceError

import pytest

from hpe_networking_mcp.pipeline.clients.retrieval_contract import (
    MAX_HIT_TEXT_CHARS,
    MAX_TOP_K,
    BackendIndexIdentity,
    RetrievalBackend,
    RetrievalHit,
    RetrievalOptions,
)


def test_options_normalize_and_bound_top_k_and_sources():
    options = RetrievalOptions.normalize(
        top_k=MAX_TOP_K + 10,
        source_filter=["Developer_Docs", "developer_docs", "tech_docs"],
    )

    assert options.top_k == MAX_TOP_K
    assert options.source_filter == ("developer_docs", "tech_docs")


@pytest.mark.parametrize("top_k", [0, -5])
def test_options_clamp_non_positive_top_k(top_k):
    assert RetrievalOptions(top_k=top_k).top_k == 1


@pytest.mark.parametrize("source_filter", ["bad source", ["ok", "bad}"], [1]])
def test_options_reject_invalid_source_filters(source_filter):
    with pytest.raises(ValueError, match="invalid source filter"):
        RetrievalOptions.normalize(source_filter=source_filter)


def test_hit_is_bounded_and_has_stable_wire_shape():
    hit = RetrievalHit(
        text="x" * (MAX_HIT_TEXT_CHARS + 1),
        source="developer_docs",
        doc_type="developer-docs",
        file_path="guide.md",
        chunk_index=2,
        score=0.987654,
        source_url="https://example.test/guide",
    )

    payload = hit.as_dict()
    assert len(hit.text) == MAX_HIT_TEXT_CHARS
    assert payload["score"] == 0.9877
    assert set(payload) == {
        "text",
        "source",
        "doc_type",
        "file_path",
        "chunk_index",
        "score",
        "source_url",
    }


def test_hit_rejects_invalid_score_and_chunk_index():
    kwargs = {
        "text": "text",
        "source": "docs",
        "doc_type": "docs",
        "file_path": "doc.md",
    }
    with pytest.raises(ValueError, match="finite"):
        RetrievalHit(**kwargs, chunk_index=0, score=float("nan"))
    with pytest.raises(ValueError, match="non-negative"):
        RetrievalHit(**kwargs, chunk_index=-1, score=1.0)


def test_identity_exposes_backend_and_index_names():
    identity = BackendIndexIdentity(
        backend="lancedb",
        index="docs",
        index_version="2026-08",
        embedding_model="nomic-embed-text-v1.5",
        embedding_dimensions=768,
    )

    assert identity.backend_name == "lancedb"
    assert identity.index_name == "docs"
    assert identity.index_version == "2026-08"


def test_protocol_is_implementable_without_inheritance():
    class FakeBackend:
        identity = BackendIndexIdentity("fake", "docs")

        def retrieve(
            self,
            query: str,
            options: RetrievalOptions | None = None,
        ) -> Sequence[RetrievalHit]:
            return [
                RetrievalHit(
                    text=query,
                    source="docs",
                    doc_type="docs",
                    file_path="query.md",
                    chunk_index=0,
                    score=1.0,
                )
            ]

    backend: RetrievalBackend = FakeBackend()
    assert backend.retrieve("hello")[0].text == "hello"


def test_contract_values_are_immutable():
    options = RetrievalOptions()
    with pytest.raises(FrozenInstanceError):
        options.top_k = 2

