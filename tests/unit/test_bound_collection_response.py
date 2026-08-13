"""Unit tests for ``bound_collection_response`` and ``maybe_bound``.

The earlier cautious review flagged a suspected non-deterministic
tiebreak in the auto-pick heuristic when two top-level lists have equal
length. These tests verify:

1. The tiebreak IS deterministic — ``max((len, key_name))`` falls back
   to the alphabetically-largest key_name, which is stable across any
   input order.
2. The longer list always wins regardless of dict insertion order.
3. Edge cases (empty data, no-list dicts, scalars) pass through
   untouched.
4. ``maybe_bound`` respects ``HPE_MCP_BOUND_LISTS`` — off = raw
   passthrough, on = wrap.

Also covers the wrap-shape contract: ``{"items": [...],
"_pagination": {offset, limit, total, truncated, list_key?}}``.
"""

from __future__ import annotations

import random

import pytest

from hpe_networking_mcp.mcp_servers.shared import (
    _BOUND_LISTS_FLAG,
    bound_collection_response,
    maybe_bound,
)


# ---------------------------------------------------------------------------
# bound_collection_response: list inputs
# ---------------------------------------------------------------------------


class TestListInput:
    def test_wraps_list_with_items_and_pagination(self):
        r = bound_collection_response([1, 2, 3, 4, 5], limit=3)
        assert r["items"] == [1, 2, 3]
        assert r["_pagination"] == {
            "offset": 0,
            "limit": 3,
            "total": 5,
            "truncated": True,
        }

    def test_offset_slices_correctly(self):
        r = bound_collection_response([1, 2, 3, 4, 5], limit=2, offset=2)
        assert r["items"] == [3, 4]
        assert r["_pagination"]["offset"] == 2
        assert r["_pagination"]["truncated"] is True

    def test_full_result_not_truncated(self):
        r = bound_collection_response([1, 2], limit=5)
        assert r["items"] == [1, 2]
        assert r["_pagination"]["truncated"] is False

    def test_negative_offset_clamped_to_zero(self):
        r = bound_collection_response([1, 2, 3], limit=2, offset=-5)
        assert r["items"] == [1, 2]
        assert r["_pagination"]["offset"] == 0


# ---------------------------------------------------------------------------
# bound_collection_response: dict inputs (the tiebreak concern)
# ---------------------------------------------------------------------------


class TestDictInputAutoPick:
    def test_longer_list_wins_regardless_of_insertion_order(self):
        """Shuffle insertion order 50 times — longer list always wins."""
        for trial in range(50):
            pairs = [("short", [1]), ("long", [1, 2, 3])]
            random.shuffle(pairs)
            d = dict(pairs)
            r = bound_collection_response(d, limit=10)
            assert r["_pagination"]["list_key"] == "long", (
                f"trial {trial}: input order {list(d.keys())}, chose "
                f"{r['_pagination']['list_key']}"
            )

    def test_tie_broken_by_alphabetical_key(self):
        """Equal-length lists: the alphabetically-largest key wins (stable)."""
        for trial in range(100):
            pairs = [
                ("alpha", [1, 2, 3]),
                ("bravo", [4, 5, 6]),
                ("charlie", [7, 8, 9]),
            ]
            random.shuffle(pairs)
            d = dict(pairs)
            r = bound_collection_response(d, limit=10)
            assert r["_pagination"]["list_key"] == "charlie", (
                f"trial {trial}: input {list(d.keys())}, chose "
                f"{r['_pagination']['list_key']}"
            )

    def test_explicit_list_key_overrides_auto_pick(self):
        """When caller passes list_key, auto-pick is skipped."""
        d = {"a": [1, 2, 3], "b": [10, 20, 30, 40]}
        r = bound_collection_response(d, limit=2, list_key="a")
        assert r["_pagination"]["list_key"] == "a"
        assert r["a"] == [1, 2]
        # The other list passes through unchanged.
        assert r["b"] == [10, 20, 30, 40]

    def test_non_list_value_at_list_key_returns_unchanged(self):
        """If the caller-supplied list_key points at a non-list, pass through."""
        d = {"a": "not a list", "b": [1, 2, 3]}
        r = bound_collection_response(d, limit=10, list_key="a")
        assert r == d

    def test_dict_without_any_lists_returns_unchanged(self):
        d = {"a": 1, "b": "two", "c": {"nested": "dict"}}
        r = bound_collection_response(d, limit=10)
        assert r == d

    def test_pagination_metadata_survives_preserved_other_fields(self):
        """Sibling fields (non-list, or the non-chosen list) must survive intact."""
        d = {
            "items": [1, 2, 3, 4, 5],
            "siblings": ["keep", "me"],
            "count": 5,
            "name": "page1",
        }
        r = bound_collection_response(d, limit=2)
        assert r["items"] == [1, 2]
        assert r["count"] == 5
        assert r["name"] == "page1"
        # "siblings" was the shorter list, shouldn't get chosen.
        assert r["_pagination"]["list_key"] == "items"
        assert r["siblings"] == ["keep", "me"]

    def test_existing_pagination_key_stripped(self):
        """If input already had a ``_pagination`` key, it's removed so we
        don't end up with stale metadata on top of fresh metadata."""
        d = {"items": [1, 2, 3], "_pagination": {"stale": True}}
        r = bound_collection_response(d, limit=2)
        assert r["_pagination"] != {"stale": True}
        assert r["_pagination"]["total"] == 3


# ---------------------------------------------------------------------------
# bound_collection_response: scalar/unknown passthrough
# ---------------------------------------------------------------------------


class TestPassthrough:
    def test_int_passes_through(self):
        assert bound_collection_response(42, limit=10) == 42

    def test_string_passes_through(self):
        assert bound_collection_response("hello", limit=10) == "hello"

    def test_none_passes_through(self):
        assert bound_collection_response(None, limit=10) is None


# ---------------------------------------------------------------------------
# maybe_bound — flag-gated wrapper
# ---------------------------------------------------------------------------


class TestMaybeBound:
    def test_off_by_default_returns_raw(self, monkeypatch):
        monkeypatch.delenv(_BOUND_LISTS_FLAG, raising=False)
        data = [1, 2, 3]
        assert maybe_bound(data, limit=2) is data  # identity, not just equal

    def test_off_accepts_various_falsy_flag_values(self, monkeypatch):
        for val in ("0", "false", "no", "off", ""):
            monkeypatch.setenv(_BOUND_LISTS_FLAG, val)
            data = [1, 2, 3]
            assert maybe_bound(data, limit=2) is data

    def test_on_wraps(self, monkeypatch):
        monkeypatch.setenv(_BOUND_LISTS_FLAG, "1")
        r = maybe_bound([1, 2, 3], limit=2)
        assert r["items"] == [1, 2]
        assert r["_pagination"]["total"] == 3

    def test_on_accepts_various_truthy_flag_values(self, monkeypatch):
        for val in ("1", "true", "TRUE", "yes", "True"):
            monkeypatch.setenv(_BOUND_LISTS_FLAG, val)
            r = maybe_bound([1, 2, 3], limit=10)
            assert "items" in r

    def test_on_respects_list_key(self, monkeypatch):
        monkeypatch.setenv(_BOUND_LISTS_FLAG, "1")
        d = {"a": [1, 2, 3], "b": ["x"]}
        r = maybe_bound(d, limit=10, list_key="a")
        assert r["_pagination"]["list_key"] == "a"
