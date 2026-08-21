from __future__ import annotations

import pytest

from hpe_networking_mcp.pipeline.clients.rag_cache import BoundedCache, normalize_query


def test_normalize_query_collapses_case_and_whitespace():
    assert normalize_query("  WPA3   SSID\n") == "wpa3 ssid"


def test_cache_is_bounded_lru_and_reports_stats():
    cache = BoundedCache[str, int](max_entries=2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1
    cache.put("c", 3)

    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.get("c") == 3
    stats = cache.stats()
    assert stats.size == 2
    assert stats.hits == 3
    assert stats.misses == 1
    assert stats.evictions == 1


def test_cache_rejects_non_positive_capacity():
    with pytest.raises(ValueError, match="positive"):
        BoundedCache(max_entries=0)
