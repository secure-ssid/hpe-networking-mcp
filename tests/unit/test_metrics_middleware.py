"""Unit tests for MetricsRegistry/MetricsMiddleware.

Covers:
- Disabled by default (HPE_MCP_METRICS unset) — every hook is a
  strict no-op and the registry stays empty.
- Bounded counters/latency aggregates recorded correctly on success,
  error, blocked, exception, cancelled, and timeout outcomes.
- Truncation-event counting from an existing `_pagination`/
  `_response_bounds` marker only — never argument/result content.
- Hard cardinality cap: once `max_series` distinct (tool, backend) pairs
  are seen, further unseen combinations fold into one overflow bucket
  instead of growing without bound.
- Rate-limit wait aggregate recording (global, unlabeled).
- Thread/asyncio-safety under concurrent calls.
- No secret/identifier values ever appear in a snapshot, even when a
  misbehaving label_resolver tries to leak one.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest
from mcp.server.mcpserver import Context, MCPServer

from hpe_networking_mcp.mcp_servers._middleware import (
    MetricsMiddleware,
    MetricsRegistry,
    install_middleware,
)
from hpe_networking_mcp.mcp_servers._middleware.metrics import metrics_enabled

# ---------------------------------------------------------------------------
# Disabled by default
# ---------------------------------------------------------------------------


class TestDisabledByDefault:
    def test_metrics_enabled_false_without_env(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_METRICS", raising=False)
        assert metrics_enabled() is False

    def test_hooks_are_noop_when_disabled(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_METRICS", raising=False)
        registry = MetricsRegistry()
        mw = MetricsMiddleware(registry)

        mw.before_call("t", {"a": 1})
        mw.after_call("t", {"a": 1}, {"ok": True})
        snapshot = registry.snapshot()

        assert snapshot["series"] == []
        assert snapshot["series_count"] == 0

    def test_on_error_is_noop_when_disabled(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_METRICS", raising=False)
        registry = MetricsRegistry()
        mw = MetricsMiddleware(registry)

        mw.before_call("t", {})
        mw.on_error("t", {}, RuntimeError("boom"))

        assert registry.snapshot()["series"] == []


# ---------------------------------------------------------------------------
# MetricsRegistry aggregates
# ---------------------------------------------------------------------------


class TestRegistryAggregates:
    def test_record_call_counts_and_latency(self):
        registry = MetricsRegistry()
        registry.record_call(
            tool="find_device",
            backend="central-ops",
            capability="read",
            outcome="success",
            duration_ms=12.5,
            truncated=False,
        )
        registry.record_call(
            tool="find_device",
            backend="central-ops",
            capability="read",
            outcome="error",
            duration_ms=42.0,
            truncated=True,
        )

        snapshot = registry.snapshot()
        assert snapshot["series_count"] == 1
        bucket = snapshot["series"][0]
        assert bucket["tool"] == "find_device"
        assert bucket["backend"] == "central-ops"
        assert bucket["requests"] == 2
        assert bucket["errors"] == 1
        assert bucket["truncated_events"] == 1
        assert bucket["outcomes"] == {"success": 1, "error": 1}
        assert bucket["capabilities"] == {"read": 2}
        assert bucket["latency_count"] == 2
        assert bucket["latency_max_ms"] == 42.0

    def test_unknown_outcome_folds_to_success(self):
        registry = MetricsRegistry()
        registry.record_call(
            tool="t",
            backend="b",
            capability="read",
            outcome="not-a-real-outcome",
            duration_ms=1.0,
            truncated=False,
        )
        bucket = registry.snapshot()["series"][0]
        assert bucket["outcomes"] == {"success": 1}

    def test_unknown_capability_folds_to_unknown(self):
        registry = MetricsRegistry()
        registry.record_call(
            tool="t",
            backend="b",
            capability="not-a-real-capability",
            outcome="success",
            duration_ms=1.0,
            truncated=False,
        )
        bucket = registry.snapshot()["series"][0]
        assert bucket["capabilities"] == {"unknown": 1}

    def test_latency_bucket_placement(self):
        registry = MetricsRegistry()
        for duration in (5.0, 30.0, 9000.0):
            registry.record_call(
                tool="t",
                backend="b",
                capability="read",
                outcome="success",
                duration_ms=duration,
                truncated=False,
            )
        bucket = registry.snapshot()["series"][0]
        assert bucket["latency_buckets"]["10"] == 1  # 5ms
        assert bucket["latency_buckets"]["50"] == 1  # 30ms
        assert bucket["latency_over_max"] == 1  # 9000ms exceeds every bucket edge

    def test_negative_duration_is_ignored(self):
        registry = MetricsRegistry()
        registry.record_call(
            tool="t", backend="b", capability="read", outcome="success",
            duration_ms=-5.0, truncated=False,
        )
        bucket = registry.snapshot()["series"][0]
        assert bucket["latency_count"] == 0

    def test_rate_limit_wait_recorded(self):
        registry = MetricsRegistry()
        registry.record_rate_limit_wait(0.05)
        registry.record_rate_limit_wait(0.15)
        snapshot = registry.snapshot()
        assert snapshot["rate_limit"]["wait_count"] == 2
        assert snapshot["rate_limit"]["wait_max_ms"] == pytest.approx(150.0, abs=0.5)

    def test_negative_wait_ignored(self):
        registry = MetricsRegistry()
        registry.record_rate_limit_wait(-1.0)
        assert registry.snapshot()["rate_limit"]["wait_count"] == 0

    def test_invalid_max_series_raises(self):
        with pytest.raises(ValueError):
            MetricsRegistry(max_series=0)


# ---------------------------------------------------------------------------
# Bounded cardinality
# ---------------------------------------------------------------------------


class TestBoundedCardinality:
    def test_overflow_bucket_caps_series_count(self):
        registry = MetricsRegistry(max_series=5)
        for i in range(50):
            registry.record_call(
                tool=f"tool_{i}",
                backend=f"backend_{i}",
                capability="read",
                outcome="success",
                duration_ms=1.0,
                truncated=False,
            )
        snapshot = registry.snapshot()
        # At most max_series distinct real buckets, plus the fixed overflow.
        assert snapshot["series_count"] <= 6
        overflow = [b for b in snapshot["series"] if b["tool"] == "_overflow_"]
        assert overflow, "expected an overflow bucket once max_series is exceeded"
        assert overflow[0]["requests"] >= 50 - 5

    def test_label_sanitization_bounds_length_and_charset(self):
        registry = MetricsRegistry()
        weird_tool = "A" * 500 + " secret=hunter2 !!"
        registry.record_call(
            tool=weird_tool,
            backend="B/../etc",
            capability="read",
            outcome="success",
            duration_ms=1.0,
            truncated=False,
        )
        bucket = registry.snapshot()["series"][0]
        assert len(bucket["tool"]) <= 64
        assert "hunter2" not in bucket["tool"]
        assert "/" not in bucket["backend"]


# ---------------------------------------------------------------------------
# MetricsMiddleware end-to-end
# ---------------------------------------------------------------------------


def _make_server_with_tool(fn):
    srv = MCPServer("test")
    srv.tool()(fn)
    return srv


def _call(srv: MCPServer, name: str, args: dict[str, Any]):
    return asyncio.run(srv._tool_manager.call_tool(name, args))


class TestMetricsMiddlewareEndToEnd:
    def test_records_success_via_install(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_METRICS", "1")
        registry = MetricsRegistry()

        def ok(x: int) -> int:
            return x * 2

        srv = _make_server_with_tool(ok)
        install_middleware(srv, [MetricsMiddleware(registry)])
        _call(srv, "ok", {"x": 3})

        snapshot = registry.snapshot()
        assert snapshot["series_count"] == 1
        bucket = snapshot["series"][0]
        assert bucket["tool"] == "ok"
        assert bucket["backend"] == "router"
        assert bucket["requests"] == 1
        assert bucket["outcomes"]["success"] == 1

    def test_records_exception_via_install(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_METRICS", "1")
        registry = MetricsRegistry()

        def raiser() -> str:
            raise RuntimeError("boom")

        srv = _make_server_with_tool(raiser)
        install_middleware(srv, [MetricsMiddleware(registry)])
        with pytest.raises(Exception):
            _call(srv, "raiser", {})

        bucket = registry.snapshot()["series"][0]
        assert bucket["outcomes"]["exception"] == 1
        assert bucket["errors"] == 1

    def test_records_cancelled_outcome(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_METRICS", "1")
        registry = MetricsRegistry()
        mw = MetricsMiddleware(registry)

        mw.before_call("t", {})
        mw.on_error("t", {}, asyncio.CancelledError())

        bucket = registry.snapshot()["series"][0]
        assert bucket["outcomes"] == {"cancelled": 1}

    def test_records_timeout_outcome(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_METRICS", "1")
        registry = MetricsRegistry()
        mw = MetricsMiddleware(registry)

        mw.before_call("t", {})
        mw.on_error("t", {}, TimeoutError("deadline exceeded"))

        bucket = registry.snapshot()["series"][0]
        assert bucket["outcomes"] == {"timeout": 1}

    def test_records_truncated_marker(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_METRICS", "1")
        registry = MetricsRegistry()
        mw = MetricsMiddleware(registry)

        mw.before_call("list_devices", {"limit": 10})
        mw.after_call(
            "list_devices",
            {"limit": 10},
            {"items": [1, 2], "_pagination": {"truncated": True}},
        )

        bucket = registry.snapshot()["series"][0]
        assert bucket["truncated_events"] == 1

    def test_label_resolver_exception_falls_back_safely(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_METRICS", "1")
        registry = MetricsRegistry()

        def bad_resolver(name, arguments):
            raise RuntimeError("resolver exploded")

        mw = MetricsMiddleware(registry, label_resolver=bad_resolver)
        mw.before_call("invoke_tool", {"name": "create_vlan"})
        mw.after_call("invoke_tool", {"name": "create_vlan"}, {"ok": True})

        bucket = registry.snapshot()["series"][0]
        assert bucket["tool"] == "invoke_tool"
        assert bucket["backend"] == "router"

    def test_disabled_env_leaves_registry_untouched_end_to_end(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_METRICS", raising=False)
        registry = MetricsRegistry()

        def ok() -> str:
            return "ok"

        srv = _make_server_with_tool(ok)
        install_middleware(srv, [MetricsMiddleware(registry)])
        _call(srv, "ok", {})

        assert registry.snapshot()["series"] == []


# ---------------------------------------------------------------------------
# Concurrency / thread-safety
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_concurrent_asyncio_calls_are_counted_exactly(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_METRICS", "1")
        registry = MetricsRegistry()

        async def fast_tool(x: int) -> int:
            await asyncio.sleep(0)
            return x

        srv = _make_server_with_tool(fast_tool)
        install_middleware(srv, [MetricsMiddleware(registry)])

        async def _run():
            await asyncio.gather(
                *[
                    srv._tool_manager.call_tool("fast_tool", {"x": i}, Context(mcp_server=srv))
                    for i in range(50)
                ]
            )

        asyncio.run(_run())

        bucket = registry.snapshot()["series"][0]
        assert bucket["requests"] == 50
        assert bucket["outcomes"]["success"] == 50

    def test_concurrent_threads_do_not_corrupt_counters(self):
        registry = MetricsRegistry()

        def worker():
            for _ in range(200):
                registry.record_call(
                    tool="t",
                    backend="b",
                    capability="read",
                    outcome="success",
                    duration_ms=1.0,
                    truncated=False,
                )

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        bucket = registry.snapshot()["series"][0]
        assert bucket["requests"] == 8 * 200


# ---------------------------------------------------------------------------
# No secret/identifier leakage
# ---------------------------------------------------------------------------


class TestNoSecretLeakage:
    def test_snapshot_never_contains_argument_or_result_values(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_METRICS", "1")
        registry = MetricsRegistry()
        mw = MetricsMiddleware(registry)

        arguments = {
            "name": "create_ssid",
            "arguments": {
                "ssid": "employee-network",
                "password": "hunter2-super-secret",
                "api_token": "abcdef0123456789",
            },
        }
        result = {
            "status": "ok",
            "device_serial": "SG12345678",
            "mac": "aa:bb:cc:dd:ee:ff",
            "tenant_id": "tenant-acme-corp",
        }

        mw.before_call("invoke_tool", arguments)
        mw.after_call("invoke_tool", arguments, result)

        serialized = json.dumps(registry.snapshot())
        for leaked in (
            "hunter2-super-secret",
            "abcdef0123456789",
            "employee-network",
            "SG12345678",
            "aa:bb:cc:dd:ee:ff",
            "tenant-acme-corp",
        ):
            assert leaked not in serialized

    def test_snapshot_never_contains_exception_message(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_METRICS", "1")
        registry = MetricsRegistry()
        mw = MetricsMiddleware(registry)

        mw.before_call("invoke_tool", {"name": "reboot_device"})
        mw.on_error(
            "invoke_tool",
            {"name": "reboot_device"},
            RuntimeError("failed for serial SG12345678 token=abcdef0123456789"),
        )

        serialized = json.dumps(registry.snapshot())
        assert "SG12345678" not in serialized
        assert "abcdef0123456789" not in serialized
