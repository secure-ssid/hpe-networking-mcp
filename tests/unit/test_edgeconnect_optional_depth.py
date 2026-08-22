"""Tests for the v0.7 optional-depth curated tools added to edgeconnect.py."""

from __future__ import annotations

import asyncio

import hpe_networking_mcp.mcp_servers.edgeconnect as edgeconnect
from hpe_networking_mcp.mcp_servers.shared import IDEMPOTENT_WRITE, READ_ONLY

# ---------------------------------------------------------------------------
# Helpers (mirror pattern from test_edgeconnect_generated.py)
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, payload=None, *, content=b"{}", content_type="application/json"):
        self.status_code = 200
        self._payload = payload
        self.content = content
        self.text = content.decode(errors="replace")
        self.headers = {"content-type": content_type}

    def json(self):
        if self._payload is None:
            raise ValueError
        return self._payload


def _fake_http(monkeypatch, captured, response):
    class Client:
        def __init__(self, timeout=None, **_ignored):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, **kwargs):
            captured.update(method=method, url=url, kwargs=kwargs)
            return response

        async def get(self, url, **kwargs):
            return await self.request("GET", url, **kwargs)

    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", Client)


def _configure(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.delenv("EDGECONNECT_AUTH_HEADER", raising=False)
    monkeypatch.delenv("EDGECONNECT_ALLOW_LEGACY_API", raising=False)


# ---------------------------------------------------------------------------
# Annotation classification
# ---------------------------------------------------------------------------


def test_edgeconnect_acknowledge_alarm_is_idempotent_write():
    tool = edgeconnect.mcp._tool_manager._tools["edgeconnect_acknowledge_alarm"]
    assert tool.annotations is IDEMPOTENT_WRITE


def test_edgeconnect_clear_alarm_is_idempotent_write():
    tool = edgeconnect.mcp._tool_manager._tools["edgeconnect_clear_alarm"]
    assert tool.annotations is IDEMPOTENT_WRITE


def test_edgeconnect_alarm_summary_is_read_only():
    tool = edgeconnect.mcp._tool_manager._tools["edgeconnect_alarm_summary"]
    assert tool.annotations is READ_ONLY


def test_edgeconnect_list_flows_is_read_only():
    tool = edgeconnect.mcp._tool_manager._tools["edgeconnect_list_flows"]
    assert tool.annotations is READ_ONLY


def test_edgeconnect_get_flow_stats_is_read_only():
    tool = edgeconnect.mcp._tool_manager._tools["edgeconnect_get_flow_stats"]
    assert tool.annotations is READ_ONLY


# ---------------------------------------------------------------------------
# edgeconnect_acknowledge_alarm
# ---------------------------------------------------------------------------


def test_edgeconnect_acknowledge_alarm_dry_run_preview(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(edgeconnect.edgeconnect_acknowledge_alarm("alarm-42"))

    assert out["dry_run"] is True
    assert out["path"] == "/alarm/acknowledgement/gms"
    assert out["body"]["ids"] == ["alarm-42"]
    assert out["body"]["acknowledge"] is True
    assert "execute_hint" in out


def test_edgeconnect_acknowledge_alarm_unacknowledge(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(
        edgeconnect.edgeconnect_acknowledge_alarm("alarm-42", acknowledge=False)
    )

    assert out["dry_run"] is True
    assert out["body"]["acknowledge"] is False


def test_edgeconnect_acknowledge_alarm_blocked_read_only(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")

    out = asyncio.run(edgeconnect.edgeconnect_acknowledge_alarm("alarm-42"))

    assert out["status"] == "blocked"


def test_edgeconnect_acknowledge_alarm_requires_confirm(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(
        edgeconnect.edgeconnect_acknowledge_alarm("alarm-42", dry_run=False, confirm=False)
    )

    assert out["dry_run"] is True
    assert "confirm=True" in out["error"]


def test_edgeconnect_acknowledge_alarm_executes_with_confirm(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    captured = {}
    _fake_http(monkeypatch, captured, _Response({"acknowledged": True}))

    out = asyncio.run(
        edgeconnect.edgeconnect_acknowledge_alarm(
            "alarm-42", dry_run=False, confirm=True
        )
    )

    assert out["status_code"] == 200
    assert captured["method"] == "POST"
    assert "/alarm/acknowledgement/gms" in captured["url"]
    assert captured["kwargs"]["json"]["ids"] == ["alarm-42"]


# ---------------------------------------------------------------------------
# edgeconnect_clear_alarm
# ---------------------------------------------------------------------------


def test_edgeconnect_clear_alarm_dry_run_preview(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(edgeconnect.edgeconnect_clear_alarm("alarm-99"))

    assert out["dry_run"] is True
    assert out["path"] == "/alarm/clearance/gms"
    assert out["body"]["ids"] == ["alarm-99"]
    assert "execute_hint" in out


def test_edgeconnect_clear_alarm_blocked_read_only(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")

    out = asyncio.run(edgeconnect.edgeconnect_clear_alarm("alarm-99"))

    assert out["status"] == "blocked"


# ---------------------------------------------------------------------------
# edgeconnect_alarm_summary
# ---------------------------------------------------------------------------


def test_edgeconnect_alarm_summary_calls_correct_path(monkeypatch):
    _configure(monkeypatch)
    captured = {}
    _fake_http(
        monkeypatch,
        captured,
        _Response({"critical": 3, "major": 5, "minor": 12}),
    )

    out = asyncio.run(edgeconnect.edgeconnect_alarm_summary())

    assert "/alarm/summary" in captured["url"]
    assert "source" in captured["kwargs"]["params"]
    assert out["alarm_summary"]["critical"] == 3
    assert "data" not in out


def test_edgeconnect_alarm_summary_passes_type_filter(monkeypatch):
    _configure(monkeypatch)
    captured = {}
    _fake_http(monkeypatch, captured, _Response({"gms_total": 2}))

    asyncio.run(edgeconnect.edgeconnect_alarm_summary(alarm_type="gms"))

    assert captured["kwargs"]["params"]["type"] == "gms"


def test_edgeconnect_alarm_summary_no_type_omits_filter(monkeypatch):
    _configure(monkeypatch)
    captured = {}
    _fake_http(monkeypatch, captured, _Response({"total": 10}))

    asyncio.run(edgeconnect.edgeconnect_alarm_summary())

    assert "type" not in captured["kwargs"]["params"]


# ---------------------------------------------------------------------------
# edgeconnect_list_flows
# ---------------------------------------------------------------------------


def test_edgeconnect_list_flows_requires_ne_pk(monkeypatch):
    _configure(monkeypatch)
    captured = {}
    flows = [{"flowId": f"f{i}", "srcIp": "10.0.0.1"} for i in range(100)]
    _fake_http(monkeypatch, captured, _Response(flows))

    asyncio.run(edgeconnect.edgeconnect_list_flows("0.NE", limit=10))

    assert "/flow" in captured["url"]
    assert captured["kwargs"]["params"]["nePk"] == "0.NE"
    assert captured["kwargs"]["params"]["maxFlows"] == 10000


def test_edgeconnect_list_flows_bounds_large_payload(monkeypatch):
    _configure(monkeypatch)
    big = [{"flowId": f"f{i}"} for i in range(200)]
    captured = {}
    _fake_http(monkeypatch, captured, _Response(big))

    out = asyncio.run(edgeconnect.edgeconnect_list_flows("0.NE", limit=20))

    flows = out["flows"]
    if isinstance(flows, dict):
        assert len(flows["items"]) <= 20
    else:
        assert len(flows) <= 20


def test_edgeconnect_list_flows_passes_optional_filters(monkeypatch):
    _configure(monkeypatch)
    captured = {}
    _fake_http(monkeypatch, captured, _Response([]))

    asyncio.run(
        edgeconnect.edgeconnect_list_flows(
            "0.NE", ip1="10.1.1.1", ip2="10.2.2.2", uptime="last1hr"
        )
    )

    params = captured["kwargs"]["params"]
    assert params["ip1"] == "10.1.1.1"
    assert params["ip2"] == "10.2.2.2"
    assert params["uptime"] == "last1hr"


# ---------------------------------------------------------------------------
# edgeconnect_get_flow_stats
# ---------------------------------------------------------------------------


def test_edgeconnect_get_flow_stats_rejects_invalid_granularity():
    out = asyncio.run(
        edgeconnect.edgeconnect_get_flow_stats(
            granularity="second", start_time=0, end_time=1
        )
    )
    assert "error" in out
    assert "granularity" in out["error"]


def test_edgeconnect_get_flow_stats_calls_correct_path(monkeypatch):
    _configure(monkeypatch)
    captured = {}
    _fake_http(monkeypatch, captured, _Response({"stats": [{"ts": 1700000000, "bytes": 1024}]}))

    out = asyncio.run(
        edgeconnect.edgeconnect_get_flow_stats(
            granularity="hour",
            start_time=1700000000000,
            end_time=1700086400000,
            appliance_id="0.NE",
        )
    )

    assert "/stats/aggregate/flow" in captured["url"]
    params = captured["kwargs"]["params"]
    assert params["granularity"] == "hour"
    assert params["startTime"] == 1700000000000
    assert params["endTime"] == 1700086400000
    assert params["nePk"] == "0.NE"
    assert out["flow_stats"]["stats"][0]["bytes"] == 1024
    assert "data" not in out


def test_edgeconnect_get_flow_stats_without_appliance_id_omits_ne_pk(monkeypatch):
    _configure(monkeypatch)
    captured = {}
    _fake_http(monkeypatch, captured, _Response({}))

    asyncio.run(
        edgeconnect.edgeconnect_get_flow_stats(
            granularity="day",
            start_time=0,
            end_time=86400000,
        )
    )

    assert "nePk" not in captured["kwargs"]["params"]
