"""Unit tests for src/hpe_networking_mcp/pipeline/clients/source_state.py."""

from __future__ import annotations

from pathlib import Path

from hpe_networking_mcp.pipeline.clients.source_state import SourceStateStore


def test_record_and_get_roundtrip(tmp_path: Path):
    store = SourceStateStore(tmp_path / "state.sqlite")

    store.record_checked(
        "https://example.com/a", "src", etag="e1", last_modified="lm1",
        content_hash="h1", changed=True,
    )
    row = store.get("https://example.com/a")

    assert row["source"] == "src"
    assert row["etag"] == "e1"
    assert row["content_hash"] == "h1"
    assert row["last_changed_at"] is not None
    assert row["last_checked_at"] is not None


def test_unknown_url_returns_none(tmp_path: Path):
    store = SourceStateStore(tmp_path / "state.sqlite")
    assert store.get("https://example.com/never-seen") is None


def test_last_changed_at_persists_across_unchanged_check(tmp_path: Path):
    store = SourceStateStore(tmp_path / "state.sqlite")

    store.record_checked("https://example.com/a", "src", content_hash="h1", changed=True)
    first = store.get("https://example.com/a")

    store.record_checked("https://example.com/a", "src", content_hash="h1", changed=False)
    second = store.get("https://example.com/a")

    assert second["last_changed_at"] == first["last_changed_at"]
    assert second["last_checked_at"] >= first["last_checked_at"]


def test_last_changed_at_updates_when_changed_true(tmp_path: Path):
    store = SourceStateStore(tmp_path / "state.sqlite")

    store.record_checked("https://example.com/a", "src", content_hash="h1", changed=True)
    first = store.get("https://example.com/a")

    store.record_checked("https://example.com/a", "src", content_hash="h2", changed=True)
    second = store.get("https://example.com/a")

    assert second["content_hash"] == "h2"
    assert second["last_changed_at"] != first["last_changed_at"]


def test_known_url_count_and_get_all_for_source(tmp_path: Path):
    store = SourceStateStore(tmp_path / "state.sqlite")
    store.record_checked("https://example.com/a", "src1", content_hash="h1", changed=True)
    store.record_checked("https://example.com/b", "src1", content_hash="h2", changed=True)
    store.record_checked("https://example.com/c", "src2", content_hash="h3", changed=True)

    assert store.known_url_count("src1") == 2
    assert store.known_url_count("src2") == 1
    assert store.known_url_count("src3") == 0
    assert {r["url"] for r in store.get_all_for_source("src1")} == {
        "https://example.com/a", "https://example.com/b",
    }


def test_changed_urls_for_source_filters_by_timestamp(tmp_path: Path):
    store = SourceStateStore(tmp_path / "state.sqlite")
    store.record_checked("https://example.com/a", "src1", content_hash="h1", changed=True)

    changed = store.changed_urls_for_source("src1", since_iso="2000-01-01T00:00:00+00:00")
    assert changed == ["https://example.com/a"]

    changed_future = store.changed_urls_for_source("src1", since_iso="2999-01-01T00:00:00+00:00")
    assert changed_future == []
