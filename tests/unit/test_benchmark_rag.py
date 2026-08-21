"""Tests for dependency-free helpers in the local RAG benchmark."""

from __future__ import annotations

import pytest

from scripts.benchmark_rag import BenchmarkCase, _query_once, percentile, summarize


def test_percentile_interpolates_between_samples():
    assert percentile([10, 20, 30, 40], 0.5) == 25
    assert percentile([10, 20, 30, 40], 0.95) == pytest.approx(38.5)


def test_percentile_rejects_empty_or_invalid_samples():
    with pytest.raises(ValueError, match="at least one"):
        percentile([], 0.5)
    with pytest.raises(ValueError, match="between 0 and 1"):
        percentile([1], 1.1)


def test_summarize_reports_latency_statistics():
    result = summarize([1, 2, 3, 4])

    assert result == {
        "n": 4,
        "min_ms": 1.0,
        "mean_ms": 2.5,
        "p50_ms": 2.5,
        "p95_ms": 3.85,
        "max_ms": 4.0,
    }


def test_default_cases_cover_requested_query_shapes():
    from scripts.benchmark_rag import DEFAULT_CASES

    assert {case.kind for case in DEFAULT_CASES} == {
        "broad",
        "source-filtered",
        "exact-like",
    }
    assert isinstance(DEFAULT_CASES[0], BenchmarkCase)
    assert DEFAULT_CASES[1].source_filter == "developer_docs"


def test_query_once_reuses_cached_embedding_for_warm_samples():
    class Embedder:
        calls = 0

        def embed_query(self, _query):
            self.calls += 1
            return [0.1, 0.2]

    class Lance:
        calls = 0

        def hybrid_search(self, *_args, **_kwargs):
            self.calls += 1
            return []

    class Rag:
        @staticmethod
        def _boost_sources(hits, _query):
            return hits

        @staticmethod
        def _boost_model_match(hits, _query):
            return hits

        @staticmethod
        def _shape(hits, _top_k):
            return hits

    embedder = Embedder()
    lance = Lance()
    cache = {}
    case = BenchmarkCase("test", "broad", "same query")

    cold = _query_once(
        db=object(),
        embedder=embedder,
        lance_client=lance,
        rag=Rag,
        case=case,
        top_k=5,
        vector_cache=cache,
    )
    warm = _query_once(
        db=object(),
        embedder=embedder,
        lance_client=lance,
        rag=Rag,
        case=case,
        top_k=5,
        vector_cache=cache,
    )

    assert cold["embedding_cache_hit"] is False
    assert warm["embedding_cache_hit"] is True
    assert embedder.calls == 1
    assert lance.calls == 2
