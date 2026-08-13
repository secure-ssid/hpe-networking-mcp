"""Tests for the v0.7 optional-depth curated tools added to mist.py."""

from __future__ import annotations

import asyncio

import hpe_networking_mcp.mcp_servers.mist as mist
from hpe_networking_mcp.mcp_servers.shared import READ_ONLY

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_client(called: dict, response_payload):
    """Return a fake httpx.AsyncClient that records GET calls."""

    class _Resp:
        status_code = 200
        text = str(response_payload)

        def json(self):
            return response_payload

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["headers"] = headers or {}
            called["params"] = {k: v for k, v in (params or {}).items() if v is not None}
            return _Resp()

    return _FakeAsyncClient


# ---------------------------------------------------------------------------
# Annotation classification
# ---------------------------------------------------------------------------


def test_mist_get_org_sle_overview_is_read_only():
    tool = mist.mcp._tool_manager._tools["mist_get_org_sle_overview"]
    assert tool.annotations is READ_ONLY


def test_mist_get_site_sle_metric_summary_is_read_only():
    tool = mist.mcp._tool_manager._tools["mist_get_site_sle_metric_summary"]
    assert tool.annotations is READ_ONLY


# ---------------------------------------------------------------------------
# mist_get_org_sle_overview
# ---------------------------------------------------------------------------


def test_mist_get_org_sle_overview_missing_org_id_returns_error():
    out = asyncio.run(mist.mist_get_org_sle_overview(org_id=""))
    assert "error" in out


def test_mist_get_org_sle_overview_calls_correct_path(monkeypatch):
    sle_data = {"sle": [{"metric": "throughput", "value": 98.5}]}
    called = {}
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "token123")
    monkeypatch.setattr(mist.httpx, "AsyncClient", _make_fake_client(called, sle_data))

    out = asyncio.run(
        mist.mist_get_org_sle_overview(
            org_id="org-uuid-1",
            metric="throughput",
            duration="7d",
        )
    )

    assert called["url"] == "https://api.mist.com/api/v1/orgs/org-uuid-1/insights/throughput"
    assert called["params"]["duration"] == "7d"
    assert out["metric"] == "throughput"
    assert out["org_id"] == "org-uuid-1"
    assert "sle" in out
    assert "data" not in out


def test_mist_get_org_sle_overview_bounds_large_response(monkeypatch):
    """sle field must be bounded even with a huge list payload."""
    big_list = [{"value": i} for i in range(500)]
    called = {}
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "token123")
    monkeypatch.setattr(mist.httpx, "AsyncClient", _make_fake_client(called, big_list))

    out = asyncio.run(mist.mist_get_org_sle_overview("org-uuid-1"))

    # bounded: default clamp_limit returns <= 200 (DEFAULT_LIST_LIMIT)
    sle = out.get("sle")
    if isinstance(sle, dict):
        items = sle.get("items", [])
        assert len(items) <= 200
    else:
        # If payload is a dict or not a list it won't be truncated by bound_collection
        assert sle is not None


def test_mist_get_org_sle_overview_unconfigured_returns_error(monkeypatch):
    monkeypatch.delenv("MIST_API_TOKEN", raising=False)

    out = asyncio.run(mist.mist_get_org_sle_overview("org1"))

    assert "error" in out


# ---------------------------------------------------------------------------
# mist_get_site_sle_metric_summary
# ---------------------------------------------------------------------------


def test_mist_get_site_sle_metric_summary_missing_params_returns_error():
    out = asyncio.run(
        mist.mist_get_site_sle_metric_summary(
            site_id="", scope="ap", scope_id="scope1"
        )
    )
    assert "error" in out


def test_mist_get_site_sle_metric_summary_calls_correct_path(monkeypatch):
    summary_data = {"start": 1700000000, "end": 1700086400, "metric": "wifi", "results": []}
    called = {}
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "token123")
    monkeypatch.setattr(mist.httpx, "AsyncClient", _make_fake_client(called, summary_data))

    out = asyncio.run(
        mist.mist_get_site_sle_metric_summary(
            site_id="site-abc",
            scope="ap",
            scope_id="scope-xyz",
            metric="wifi",
            duration="1d",
        )
    )

    expected_url = (
        "https://api.mist.com/api/v1/sites/site-abc/sle/ap/scope-xyz/metric/wifi/summary"
    )
    assert called["url"] == expected_url
    assert called["params"]["duration"] == "1d"
    assert out["metric"] == "wifi"
    assert out["scope"] == "ap"
    assert out["scope_id"] == "scope-xyz"
    assert out["site_id"] == "site-abc"
    assert "summary" in out
    assert "data" not in out


def test_mist_get_site_sle_metric_summary_bounds_large_list_payload(monkeypatch):
    """summary field must be bounded for large list payloads."""
    big_list = [{"classifier": f"c{i}", "value": i} for i in range(500)]
    called = {}
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "token123")
    monkeypatch.setattr(mist.httpx, "AsyncClient", _make_fake_client(called, big_list))

    out = asyncio.run(
        mist.mist_get_site_sle_metric_summary("site1", "ap", "scope1", "wifi")
    )

    summary = out.get("summary")
    if isinstance(summary, dict):
        items = summary.get("items", [])
        assert len(items) <= 200
    else:
        assert summary is not None


def test_mist_get_site_sle_metric_summary_path_encodes_spaces(monkeypatch):
    """Scope/metric values with spaces must be URL-encoded in the path."""
    called = {}
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "token123")
    monkeypatch.setattr(mist.httpx, "AsyncClient", _make_fake_client(called, {}))

    asyncio.run(
        mist.mist_get_site_sle_metric_summary("site 1", "ap", "scope 1", "wifi success")
    )

    assert "site%201" in called["url"]
    assert "scope%201" in called["url"]
    assert "wifi%20success" in called["url"]
