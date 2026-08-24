"""Unit tests for the Prometheus text exposition of the metrics registry.

Covers:
- render_prometheus maps the snapshot honestly: outcomes and capabilities
  render as two separate marginal counter families (the registry never
  stores a joint capability x outcome count).
- Exclusive per-bin latency buckets convert to cumulative `le` counts,
  with latency_over_max folded into le="+Inf"; the invariant
  sum(le counts) == latency_count holds.
- Rate-limit aggregate and registry gauges render.
- The /metrics route negotiates format: Prometheus text by default, JSON
  via ?format=json or a JSON-only Accept header, and the disabled
  response stays JSON regardless of requested format.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from hpe_networking_mcp.mcp_servers._middleware.metrics import (
    MetricsRegistry,
    get_default_registry,
    render_prometheus,
)
from hpe_networking_mcp.mcp_servers.shared import _register_metrics_route

Samples = dict[tuple[str, str], float]


def _parse_text(text: str) -> Samples:
    """Parse `metric{labels} value` lines into {(name, labels): value}."""
    samples: Samples = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        metric, _, raw = line.rpartition(" ")
        if "{" in metric:
            name, _, labels = metric.partition("{")
            labels = labels.rstrip("}")
        else:
            name, labels = metric, ""
        samples[(name, labels)] = float(raw)
    return samples


def _labels(**kwargs: str) -> str:
    return ",".join(f'{k}="{v}"' for k, v in kwargs.items())


def _registry_with_calls() -> MetricsRegistry:
    registry = MetricsRegistry()
    registry.record_call(
        tool="list_devices",
        backend="central",
        capability="read",
        outcome="success",
        duration_ms=5.0,  # bin le=10
        truncated=False,
    )
    registry.record_call(
        tool="list_devices",
        backend="central",
        capability="read",
        outcome="success",
        duration_ms=30.0,  # bin le=50
        truncated=True,
    )
    registry.record_call(
        tool="apply_config",
        backend="central",
        capability="write",
        outcome="blocked",
        duration_ms=9000.0,  # over max
        truncated=False,
    )
    registry.record_rate_limit_wait(0.25)
    return registry


class TestRenderPrometheus:
    def test_outcome_and_capability_are_separate_marginal_families(self):
        samples = _parse_text(render_prometheus(_registry_with_calls().snapshot()))
        ld = _labels(tool="list_devices", backend="central")
        ac = _labels(tool="apply_config", backend="central")

        assert samples[("hpe_mcp_tool_calls_total", f"{ld},outcome=\"success\"")] == 2
        assert samples[("hpe_mcp_tool_calls_total", f"{ac},outcome=\"blocked\"")] == 1
        cap_metric = "hpe_mcp_tool_capability_calls_total"
        assert samples[(cap_metric, f"{ld},capability=\"read\"")] == 2
        assert samples[(cap_metric, f"{ac},capability=\"write\"")] == 1

    def test_latency_buckets_are_cumulative_and_sum_to_count(self):
        samples = _parse_text(render_prometheus(_registry_with_calls().snapshot()))
        bucket = "hpe_mcp_tool_call_latency_ms_bucket"
        ld = _labels(tool="list_devices", backend="central")
        ac = _labels(tool="apply_config", backend="central")

        for tool in ("list_devices", "apply_config"):
            le_values = [
                v
                for (name, labels), v in samples.items()
                if name == bucket and f'tool="{tool}"' in labels
            ]
            assert len(le_values) == 10  # 9 fixed edges + +Inf
            assert le_values == sorted(le_values), "le counts must be cumulative"

        # list_devices: 5ms -> le=10, 30ms -> le=50; le=10 counts 1, le=25
        # still 1, le=50 reaches 2.
        assert samples[(bucket, f'{ld},le="10"')] == 1
        assert samples[(bucket, f'{ld},le="25"')] == 1
        assert samples[(bucket, f'{ld},le="50"')] == 2
        # apply_config: 9000ms over max -> only +Inf holds it.
        assert samples[(bucket, f'{ac},le="5000"')] == 0
        assert samples[(bucket, f'{ac},le="+Inf"')] == 1
        # sum(le counts) == latency_count per series.
        assert samples[(bucket, f'{ld},le="+Inf"')] == 2
        assert samples[("hpe_mcp_tool_call_latency_ms_count", ld)] == 2
        assert samples[("hpe_mcp_tool_call_latency_ms_sum", ac)] == 9000.0

    def test_truncated_rate_limit_and_gauges_render(self):
        samples = _parse_text(render_prometheus(_registry_with_calls().snapshot()))
        ld = _labels(tool="list_devices", backend="central")

        assert samples[("hpe_mcp_tool_call_truncated_total", ld)] == 1
        assert samples[("hpe_mcp_rate_limit_waits_total", "")] == 1
        assert samples[("hpe_mcp_rate_limit_wait_ms_total", "")] == 250.0
        assert samples[("hpe_mcp_rate_limit_wait_max_ms", "")] == 250.0
        assert samples[("hpe_mcp_metrics_series", "")] == 2
        assert samples[("hpe_mcp_metrics_series_cap", "")] == 512
        assert ("hpe_mcp_metrics_uptime_seconds", "") in samples

    def test_empty_registry_still_renders_gauges(self):
        text = render_prometheus(MetricsRegistry().snapshot())
        samples = _parse_text(text)

        assert samples[("hpe_mcp_metrics_series", "")] == 0
        assert not any(name == "hpe_mcp_tool_calls_total" for name, _ in samples)
        assert "# TYPE hpe_mcp_tool_call_latency_ms histogram" in text

    def test_output_terminates_with_newline_and_has_help_type(self):
        text = render_prometheus(_registry_with_calls().snapshot())
        assert text.endswith("\n")
        assert "# HELP hpe_mcp_tool_calls_total " in text


class _FakeMcp:
    def __init__(self):
        self.handlers = {}

    def custom_route(self, path, methods, name=None, include_in_schema=True):
        def decorator(fn):
            self.handlers[path] = fn
            return fn

        return decorator


def _request(query: dict[str, str] | None = None, accept: str = ""):
    return SimpleNamespace(query_params=query or {}, headers={"accept": accept} if accept else {})


class TestMetricsRouteNegotiation:
    @pytest.fixture()
    def handler(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_METRICS", "1")
        get_default_registry().reset()
        mcp = _FakeMcp()
        _register_metrics_route(mcp)
        yield mcp.handlers["/metrics"]
        get_default_registry().reset()

    def test_default_is_prometheus_text(self, handler):
        registry = get_default_registry()
        registry.record_call(
            tool="t",
            backend="b",
            capability="read",
            outcome="success",
            duration_ms=1.0,
            truncated=False,
        )
        response = asyncio.run(handler(_request()))
        assert "text/plain" in response.media_type
        body = response.body.decode()
        assert 'hpe_mcp_tool_calls_total{tool="t",backend="b",outcome="success"} 1' in body

    def test_format_json_param_returns_snapshot(self, handler):
        response = asyncio.run(handler(_request(query={"format": "json"})))
        payload = json.loads(response.body)
        assert payload["enabled"] is True
        assert payload["schema_version"] == 1

    def test_json_only_accept_returns_json(self, handler):
        response = asyncio.run(handler(_request(accept="application/json")))
        payload = json.loads(response.body)
        assert payload["enabled"] is True

    def test_prometheus_accept_returns_text(self, handler):
        accept = "application/openmetrics-text; version=1.0.0, text/plain; version=0.0.4"
        response = asyncio.run(handler(_request(accept=accept)))
        assert "text/plain" in response.media_type

    def test_wildcard_accept_prefers_scrapeable_text(self, handler):
        response = asyncio.run(handler(_request(accept="application/json, */*")))
        assert "text/plain" in response.media_type

    def test_disabled_response_is_json_regardless_of_format(self, handler, monkeypatch):
        monkeypatch.delenv("HPE_MCP_METRICS", raising=False)
        for request in (_request(), _request(accept="text/plain")):
            response = asyncio.run(handler(request))
            payload = json.loads(response.body)
            assert payload["enabled"] is False
            assert "HPE_MCP_METRICS=1" in payload["hint"]
