"""Regression tests for invoke_read_tool_batch hardening and the
dispatch-level rate gate.

Reproduced defects:

- The batch charged the shared token bucket once for the whole outer MCP call,
  so 25 real backend requests drew one token from a limiter that exists to
  respect a 10 req/s account-wide cap.
- ``calls`` was an untyped ``list[dict[str, Any]]``: the published schema said
  nothing about the shape or its bounds.
- Two entries could share an ``id``, making results impossible to correlate by
  the very field supplied to correlate them.
- Each item claimed the *whole* single-call response budget, so one large read
  could crowd out every other result before the whole-response shrink ran.
- The whole-response shrink could still finish over budget when shrinking the
  ``ok`` items was not enough.
- Metrics labels and audit targets resolved to ``router``/``unknown``, making
  every batched backend call invisible to observability.

No network access.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import ValidationError

import hpe_networking_mcp.mcp_servers.tool_router as router
from hpe_networking_mcp.mcp_servers._middleware.rate_limit import RateLimitMiddleware
from hpe_networking_mcp.mcp_servers.shared import DESTRUCTIVE, READ_ONLY

pytestmark = pytest.mark.skipif(
    not hasattr(router, "invoke_read_tool_batch"),
    reason="invoke_read_tool_batch is not registered in minimal router mode",
)


def _backend(payload_size: int = 3) -> MCPServer:
    srv = MCPServer("central-monitoring")

    @srv.tool(annotations=READ_ONLY)
    def echo(value: str = "x") -> dict[str, Any]:
        return {"value": value}

    @srv.tool(annotations=READ_ONLY)
    def big_list(count: int = 5) -> dict[str, Any]:
        return {"items": [{"i": i, "pad": "y" * 200} for i in range(count)]}

    @srv.tool(annotations=DESTRUCTIVE)
    def wipe() -> dict[str, Any]:
        return {"wiped": True}

    @srv.tool(annotations=READ_ONLY)
    def explode() -> dict[str, Any]:
        raise RuntimeError("backend boom")

    return srv


@pytest.fixture
def wired(monkeypatch):
    backend = _backend()
    monkeypatch.setattr(router, "_BACKENDS", {"central-monitoring": "fake.mod"})
    monkeypatch.setattr(router, "_tool_index", {})
    monkeypatch.setattr(router, "_tool_servers", {})
    monkeypatch.setattr(router, "_tool_backend_names", {})
    monkeypatch.setattr(router, "_backend_load_errors", {})
    monkeypatch.setattr(
        router.importlib, "import_module", lambda path: SimpleNamespace(mcp=backend)
    )
    monkeypatch.setattr(router, "_dispatch_rate_gate", None)
    return backend


def _batch(calls):
    return asyncio.run(router.invoke_read_tool_batch(None, calls))


# ---------------------------------------------------------------------------
# Typed, bounded input schema
# ---------------------------------------------------------------------------


class TestBatchInputSchema:
    def test_published_schema_describes_the_call_object(self):
        tool = router.mcp._tool_manager._tools["invoke_read_tool_batch"]
        schema = tool.parameters
        text = json.dumps(schema)

        assert "calls" in schema["properties"]
        # Not an opaque object: the entry's fields are published.
        for field in ("name", "arguments", "id", "cursor"):
            assert f'"{field}"' in text

    def test_model_bounds_name_and_id_lengths(self):
        model = router.BatchCall

        assert model.model_fields["name"].metadata
        with pytest.raises(ValidationError):
            model(name="x" * (router.MAX_BATCH_TOOL_NAME_CHARS + 1))
        with pytest.raises(ValidationError):
            model(name="echo", id="i" * (router.MAX_BATCH_CALL_ID_CHARS + 1))

    def test_model_rejects_unknown_fields(self):
        with pytest.raises(ValidationError):
            router.BatchCall(name="echo", surprise=1)

    def test_model_instances_are_accepted_at_runtime(self, wired):
        out = _batch([router.BatchCall(name="echo", arguments={"value": "hi"}, id="a")])

        assert out["ok"] is True
        assert out["results"][0]["result"]["value"] == "hi"

    def test_plain_dicts_are_still_accepted(self, wired):
        out = _batch([{"name": "echo", "arguments": {"value": "hi"}}])

        assert out["ok"] is True
        assert out["results"][0]["id"] == "0"

    def test_oversized_arguments_reject_only_that_entry(self, wired):
        huge = {"value": "z" * (router.MAX_BATCH_ARGUMENTS_BYTES + 10)}
        out = _batch([{"name": "echo", "id": "big", "arguments": huge},
                      {"name": "echo", "id": "ok"}])

        assert out["results"][0]["status"] == "invalid_call"
        assert out["results"][1]["status"] == "ok"
        # The argument value itself is never echoed back.
        assert "zzz" not in json.dumps(out)

    def test_batch_entry_cap_is_enforced(self, wired):
        out = _batch([{"name": "echo"} for _ in range(router.MAX_BATCH_CALLS + 1)])

        assert out["ok"] is False
        assert "exceeding" in out["error"]

    def test_empty_batch_is_rejected(self, wired):
        assert _batch([])["ok"] is False


# ---------------------------------------------------------------------------
# Duplicate ids
# ---------------------------------------------------------------------------


class TestDuplicateIds:
    def test_duplicate_explicit_ids_reject_the_whole_batch(self, wired):
        out = _batch(
            [
                {"name": "echo", "id": "same"},
                {"name": "echo", "id": "other"},
                {"name": "echo", "id": "same"},
            ]
        )

        assert out["ok"] is False
        assert "duplicate call id" in out["error"]
        assert "same" in out["error"]
        assert "results" not in out

    def test_duplicate_check_runs_before_any_dispatch(self, wired, monkeypatch):
        called = {"n": 0}

        async def _spy(*args, **kwargs):
            called["n"] += 1
            return {}

        monkeypatch.setattr(router, "_dispatch_read_tool", _spy)
        _batch([{"name": "echo", "id": "d"}, {"name": "echo", "id": "d"}])

        assert called["n"] == 0

    def test_default_index_ids_are_never_duplicates(self, wired):
        out = _batch([{"name": "echo"}, {"name": "echo"}, {"name": "echo"}])

        assert out["ok"] is True
        assert [item["id"] for item in out["results"]] == ["0", "1", "2"]

    def test_model_instances_are_duplicate_checked_too(self, wired):
        out = _batch(
            [router.BatchCall(name="echo", id="dup"), router.BatchCall(name="echo", id="dup")]
        )

        assert out["ok"] is False
        assert "duplicate call id" in out["error"]


# ---------------------------------------------------------------------------
# Per-item + whole-response byte budget
# ---------------------------------------------------------------------------


class TestBatchBudgets:
    def test_per_item_budget_is_threaded_into_dispatch(self, wired, monkeypatch):
        seen: list[int | None] = []
        original = router._dispatch_read_tool

        async def _spy(ctx, name, arguments=None, cursor=None, **kwargs):
            seen.append(kwargs.get("max_bytes"))
            return await original(ctx, name, arguments, cursor, **kwargs)

        monkeypatch.setattr(router, "_dispatch_read_tool", _spy)
        monkeypatch.setenv(router._BATCH_RESPONSE_BUDGET_BYTES_ENV, "100000")

        _batch([{"name": "echo"}, {"name": "echo"}, {"name": "echo"}, {"name": "echo"}])

        assert seen == [25000, 25000, 25000, 25000]

    def test_per_item_budget_never_drops_below_the_router_floor(self, wired, monkeypatch):
        seen: list[int | None] = []
        original = router._dispatch_read_tool

        async def _spy(ctx, name, arguments=None, cursor=None, **kwargs):
            seen.append(kwargs.get("max_bytes"))
            return await original(ctx, name, arguments, cursor, **kwargs)

        monkeypatch.setattr(router, "_dispatch_read_tool", _spy)
        monkeypatch.setenv(router._BATCH_RESPONSE_BUDGET_BYTES_ENV, "2048")

        _batch([{"name": "echo"} for _ in range(20)])

        assert all(value == router._RESPONSE_BUDGET_MIN_BYTES for value in seen)

    def test_whole_response_is_strictly_within_budget(self, wired, monkeypatch):
        monkeypatch.setenv(router._BATCH_RESPONSE_BUDGET_BYTES_ENV, "2048")

        out = _batch([{"name": "big_list", "arguments": {"count": 40}} for _ in range(6)])

        size = len(json.dumps(out, ensure_ascii=False, default=str).encode("utf-8"))
        assert size <= 4096, size
        assert out["truncated"] is True

    def test_truncation_marks_shrunk_items_without_dropping_them(self, wired, monkeypatch):
        monkeypatch.setenv(router._BATCH_RESPONSE_BUDGET_BYTES_ENV, "2048")

        out = _batch([{"name": "big_list", "arguments": {"count": 40}} for _ in range(5)])

        assert out["counts"]["total"] == 5
        assert len(out["results"]) == 5
        assert any(item.get("result_truncated") for item in out["results"])

    def test_failures_keep_their_identity_when_over_budget(self, wired, monkeypatch):
        monkeypatch.setenv(router._BATCH_RESPONSE_BUDGET_BYTES_ENV, "2048")

        out = _batch(
            [
                {"name": "big_list", "arguments": {"count": 40}, "id": "big"},
                {"name": "wipe", "id": "blocked-one"},
            ]
        )

        blocked = [item for item in out["results"] if item["id"] == "blocked-one"]
        assert blocked and blocked[0]["status"] == "blocked"
        assert out["failed_ids"] == ["blocked-one"]

    def test_within_budget_responses_are_not_marked_truncated(self, wired):
        out = _batch([{"name": "echo"}])

        assert out["truncated"] is False
        assert "result_truncated" not in out["results"][0]


# ---------------------------------------------------------------------------
# Failure containment
# ---------------------------------------------------------------------------


class TestBatchFailureContainment:
    def test_backend_exception_becomes_one_error_item(self, wired):
        out = _batch([{"name": "explode", "id": "boom"}, {"name": "echo", "id": "fine"}])

        assert out["ok"] is False
        assert out["results"][0]["status"] == "error"
        assert out["results"][1]["status"] == "ok"
        assert out["counts"] == {"total": 2, "succeeded": 1, "failed": 1}

    def test_router_level_exception_is_contained(self, wired, monkeypatch):
        async def _boom(*args, **kwargs):
            raise ValueError("router exploded")

        monkeypatch.setattr(router, "_dispatch_read_tool", _boom)

        out = _batch([{"name": "echo", "id": "a"}, {"name": "echo", "id": "b"}])

        assert out["ok"] is False
        assert [item["status"] for item in out["results"]] == ["error", "error"]
        assert "router exploded" in out["results"][0]["error"]

    def test_write_tool_is_blocked_and_never_dispatched(self, wired):
        out = _batch([{"name": "wipe"}])

        assert out["results"][0]["status"] == "blocked"
        assert out["ok"] is False

    def test_unknown_tool_is_isolated(self, wired):
        out = _batch([{"name": "nope"}, {"name": "echo"}])

        assert out["results"][0]["status"] == "unknown_tool"
        assert out["results"][1]["status"] == "ok"

    def test_items_report_their_resolved_backend_server(self, wired):
        out = _batch([{"name": "echo"}])

        assert out["results"][0]["server"] == "central-monitoring"


# ---------------------------------------------------------------------------
# Rate limiting: one token per backend call
# ---------------------------------------------------------------------------


class TestDispatchRateGate:
    def test_gate_is_awaited_once_per_backend_call(self, wired, monkeypatch):
        calls = {"n": 0}

        async def _gate():
            calls["n"] += 1

        monkeypatch.setattr(router, "_dispatch_rate_gate", _gate)

        _batch([{"name": "echo"} for _ in range(5)])

        assert calls["n"] == 5

    def test_single_dispatch_charges_exactly_one_token(self, wired, monkeypatch):
        calls = {"n": 0}

        async def _gate():
            calls["n"] += 1

        monkeypatch.setattr(router, "_dispatch_rate_gate", _gate)

        asyncio.run(router._dispatch_read_tool(None, "echo", {}))

        assert calls["n"] == 1

    def test_locally_rejected_calls_do_not_charge(self, wired, monkeypatch):
        calls = {"n": 0}

        async def _gate():
            calls["n"] += 1

        monkeypatch.setattr(router, "_dispatch_rate_gate", _gate)

        _batch([{"name": "nope"}, {"name": "wipe"}, {"name": "echo"}])

        assert calls["n"] == 1

    def test_unset_gate_is_a_no_op(self, wired, monkeypatch):
        monkeypatch.setattr(router, "_dispatch_rate_gate", None)

        assert _batch([{"name": "echo"}])["ok"] is True

    def test_broken_gate_never_blocks_dispatch(self, wired, monkeypatch):
        async def _gate():
            raise RuntimeError("limiter exploded")

        monkeypatch.setattr(router, "_dispatch_rate_gate", _gate)

        assert _batch([{"name": "echo"}])["ok"] is True

    def test_set_dispatch_rate_gate_round_trips(self, monkeypatch):
        monkeypatch.setattr(router, "_dispatch_rate_gate", None)

        async def _gate():
            return None

        router.set_dispatch_rate_gate(_gate)
        assert router._dispatch_rate_gate is _gate
        router.set_dispatch_rate_gate(None)
        assert router._dispatch_rate_gate is None


class TestRateLimiterPublicAcquire:
    def test_acquire_is_public_and_consumes_a_token(self):
        limiter = RateLimitMiddleware(rate=100.0, burst=3)

        asyncio.run(limiter.acquire())

        assert limiter._tokens < 3

    def test_private_alias_still_resolves(self):
        limiter = RateLimitMiddleware(rate=100.0)

        assert limiter._acquire.__func__ is limiter.acquire.__func__

    def test_exempt_names_are_not_charged(self):
        limiter = RateLimitMiddleware(
            rate=100.0, burst=5, exempt_names={"invoke_read_tool_batch"}
        )

        asyncio.run(limiter.before_call("invoke_read_tool_batch", {}))

        assert limiter._tokens == 5

    def test_non_exempt_names_are_charged(self):
        limiter = RateLimitMiddleware(rate=100.0, burst=5, exempt_names={"other"})

        asyncio.run(limiter.before_call("find_tool", {}))

        assert limiter._tokens < 5

    def test_default_charges_everything(self):
        limiter = RateLimitMiddleware(rate=100.0, burst=5)

        asyncio.run(limiter.before_call("invoke_read_tool_batch", {}))

        assert limiter._tokens < 5

    def test_router_exempts_its_dispatching_tools(self):
        assert router._DISPATCHING_ROUTER_TOOLS == {
            "invoke_tool",
            "invoke_read_tool",
            "invoke_read_tool_batch",
        }


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


class TestBatchObservability:
    def test_single_target_batch_resolves_the_real_tool(self, wired):
        router._load_all_backends()
        args = {"calls": [{"name": "echo"}, {"name": "echo"}]}

        tool, backend, capability = router._router_call_labels(
            "invoke_read_tool_batch", args
        )

        assert (tool, backend, capability) == ("echo", "central-monitoring", "read")

    def test_multi_target_batch_uses_a_bounded_constant_label(self, wired):
        router._load_all_backends()
        args = {"calls": [{"name": "echo"}, {"name": "big_list"}]}

        tool, backend, capability = router._router_call_labels(
            "invoke_read_tool_batch", args
        )

        assert tool == router.BATCH_MULTI_LABEL
        assert backend == "central-monitoring"
        assert capability == "read"

    def test_labels_are_never_unknown_for_a_batch(self, wired):
        router._load_all_backends()

        for args in ({"calls": []}, {"calls": [{"name": "nope"}]}, {}):
            tool, backend, capability = router._router_call_labels(
                "invoke_read_tool_batch", args
            )
            assert capability != "unknown"
            assert tool

    def test_audit_target_resolves_for_a_batch(self, wired):
        router._load_all_backends()

        target = router._router_call_target(
            "invoke_read_tool_batch", {"calls": [{"name": "echo"}]}
        )

        assert target == "echo"

    def test_audit_target_is_bounded_for_multi_target_batches(self, wired):
        router._load_all_backends()

        target = router._router_call_target(
            "invoke_read_tool_batch", {"calls": [{"name": "echo"}, {"name": "big_list"}]}
        )

        assert target == router.BATCH_MULTI_LABEL

    def test_audit_middleware_accepts_the_batch_as_a_dispatching_tool(self):
        from hpe_networking_mcp.mcp_servers._middleware.audit_log import _DISPATCHING_TOOL_NAMES

        assert "invoke_read_tool_batch" in _DISPATCHING_TOOL_NAMES
        assert _DISPATCHING_TOOL_NAMES == router._DISPATCHING_ROUTER_TOOLS

    def test_label_resolution_never_reads_argument_values(self, wired):
        router._load_all_backends()
        args = {"calls": [{"name": "echo", "arguments": {"value": "SECRET-TOKEN"}}]}

        labels = router._router_call_labels("invoke_read_tool_batch", args)

        assert "SECRET-TOKEN" not in json.dumps(labels)

    def test_label_resolution_is_bounded_for_oversized_requests(self, wired):
        router._load_all_backends()
        args = {"calls": [{"name": "echo"} for _ in range(500)]}

        targets = router._batch_call_targets(args)

        assert len(targets) <= router.MAX_BATCH_CALLS_LABEL_CAP


# ---------------------------------------------------------------------------
# Strict-budget shrink stages
# ---------------------------------------------------------------------------


def _bulky_items(count: int, *, status: str = "error") -> list[dict[str, Any]]:
    return [
        {
            "index": i,
            "id": "i" * 90,
            "tool": "a_very_long_backend_tool_name_" * 3,
            "server": "central-monitoring",
            "status": status,
            "error": "e" * 400,
        }
        for i in range(count)
    ]


class TestStrictBudgetShrinkStages:
    def test_within_budget_is_returned_untouched(self):
        items = [{"index": 0, "id": "a", "tool": "echo", "server": "s", "status": "ok",
                  "result": {"v": 1}}]

        shrunk, truncated = router._shrink_batch_items_for_budget(items, byte_budget=100_000)

        assert shrunk == items
        assert truncated is False

    def test_stage_one_shrinks_ok_results_last_first(self):
        items = [
            {"index": i, "id": str(i), "tool": "big_list", "server": "central-monitoring",
             "status": "ok", "result": {"items": ["x" * 500]}}
            for i in range(4)
        ]

        shrunk, truncated = router._shrink_batch_items_for_budget(items, byte_budget=1600)

        assert truncated is True
        assert shrunk[-1]["result_truncated"] is True
        assert shrunk[-1]["result"] is None
        # The earliest call keeps its payload for as long as the budget allows.
        assert "result" in shrunk[0]

    def test_stage_two_squeezes_error_text(self):
        shrunk, truncated = router._shrink_batch_items_for_budget(
            _bulky_items(25), byte_budget=1024
        )

        assert truncated is True
        assert all(len(item["error"]) <= 80 for item in shrunk)
        assert all(item["error_truncated"] is True for item in shrunk)

    def test_failures_never_lose_their_identity(self):
        shrunk, _ = router._shrink_batch_items_for_budget(_bulky_items(25), byte_budget=1024)

        assert len(shrunk) == 25
        for index, item in enumerate(shrunk):
            assert item["index"] == index
            assert item["status"] == "error"
            assert item["id"]

    def test_overflow_envelope_is_strictly_bounded(self):
        items = _bulky_items(25)

        envelope = router._batch_overflow_envelope(
            items, {"total": 25, "succeeded": 0, "failed": 25}, ["a"], [0], 1024
        )

        size = len(json.dumps(envelope, ensure_ascii=False, default=str).encode("utf-8"))
        assert size <= 1024, size
        assert envelope["ok"] is False
        assert envelope["truncated"] is True
        assert envelope["counts"]["failed"] == 25
        assert envelope["results_omitted"] > 0

    def test_overflow_envelope_keeps_a_status_skeleton(self):
        items = _bulky_items(4)

        envelope = router._batch_overflow_envelope(
            items, {"total": 4, "succeeded": 0, "failed": 4}, [], [], 100_000
        )

        assert [item["status"] for item in envelope["results"]] == ["error"] * 4
        assert envelope["results_omitted"] == 0
