from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hpe_networking_mcp.pipeline.clients import redis_client


def test_get_client_uses_env_redis_url(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://cache.internal:6380/1")

    with patch("hpe_networking_mcp.pipeline.clients.redis_client.redis.from_url") as from_url:
        redis_client.get_client()

    from_url.assert_called_once_with("redis://cache.internal:6380/1", decode_responses=False)


def test_get_client_prefers_explicit_url(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://cache.internal:6380/1")

    with patch("hpe_networking_mcp.pipeline.clients.redis_client.redis.from_url") as from_url:
        redis_client.get_client("redis://override:6379/9")

    from_url.assert_called_once_with("redis://override:6379/9", decode_responses=False)


def _fake_client_returning(score):
    """Build a fake Redis client whose ft().search() returns one doc with `score`."""
    doc = SimpleNamespace(
        text="t", source="developer_docs", doc_type="developer-docs",
        file_path="f.md", chunk_index="0", name="n", description="d",
        server="aruba", schema_json="{}", score=score,
    )
    ft = MagicMock()
    ft.search.return_value = SimpleNamespace(docs=[doc])
    client = MagicMock()
    client.ft.return_value = ft
    return client


# RediSearch COSINE distance: 0 = identical, 1 = orthogonal, 2 = opposite.
# Corrected conversion (H13): similarity = clamp(1 - distance, 0, 1).
@pytest.mark.parametrize("distance,expected", [(0.0, 1.0), (0.28, 0.72), (1.0, 0.0)])
def test_vector_search_distance_to_similarity(distance, expected):
    client = _fake_client_returning(distance)
    hits = redis_client.vector_search(client, query_vector=[0.0] * 768, top_k=1)
    assert hits[0]["score"] == pytest.approx(expected)


@pytest.mark.parametrize("distance,expected", [(0.0, 1.0), (0.28, 0.72), (1.0, 0.0)])
def test_search_tools_distance_to_similarity(distance, expected):
    client = _fake_client_returning(distance)
    hits = redis_client.search_tools(client, query_vector=[0.0] * 768, top_k=1)
    assert hits[0]["score"] == pytest.approx(expected)


def test_vector_search_similarity_clamped_above_one():
    # Distance > 1 (obtuse angle) must clamp to 0, never go negative.
    client = _fake_client_returning(1.5)
    hits = redis_client.vector_search(client, query_vector=[0.0] * 768, top_k=1)
    assert hits[0]["score"] == 0.0


def test_vector_search_negative_top_k_clamped_to_one():
    client = _fake_client_returning(0.0)
    redis_client.vector_search(client, query_vector=[0.0] * 768, top_k=-5)

    query = client.ft.return_value.search.call_args.args[0]
    assert "KNN 1" in query._query_string
    assert query._num == 1


def test_search_tools_negative_top_k_clamped_to_one():
    client = _fake_client_returning(0.0)
    redis_client.search_tools(client, query_vector=[0.0] * 768, top_k=-5)

    query = client.ft.return_value.search.call_args.args[0]
    assert "KNN 1" in query._query_string
    assert query._num == 1
