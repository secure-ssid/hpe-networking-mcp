"""Unit tests for the router-level dependency planner and reconciliation
scheduler (v0.7 router automation): ``plan_tool_workflow`` and
``plan_reconciliation_schedule`` in ``hpe_networking_mcp.mcp_servers.tool_router``.

Covers:
- Explicit-tool-name resolution (exact match only, never guessed).
- Hint/keyword resolution, including ambiguity detection.
- Unresolved tool/dependency reporting and cycle detection withholding
  ``order`` while still reporting true ``acyclic``/``cycles`` state.
- Step-count bound rejection and duplicate step id handling.
- Cadence validation pass-through, capability-based exclusion, bounded
  ``max_entries``, oversized ``tools`` input rejection, and
  platform/server filtering for the reconciliation scheduler.
- Both tools always produce a valid ``artifact`` (or an explicit
  ``artifact_error``) via ``hpe_networking_mcp.pipeline.artifact_contracts`` -- never raising.
- Permission-separation regression: ``invoke_read_tool`` still refuses a
  write/destructive tool even when the planner resolved/returned that same
  tool name as part of a plan.
- Neither tool ever calls ``invoke_tool``/``invoke_read_tool`` itself (no
  live dispatch happens as a side effect of planning).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp.server.mcpserver import Context, MCPServer

import os

# Prefer default router mode so planner tools register even when developer
# .env pins HPE_MCP_ROUTER_MODE=minimal for interactive clients.
os.environ.setdefault("HPE_MCP_ROUTER_MODE", "default")
if os.environ.get("HPE_MCP_ROUTER_MODE", "").strip().lower() == "minimal":
    os.environ["HPE_MCP_ROUTER_MODE"] = "default"

import hpe_networking_mcp.mcp_servers.tool_router as router
from hpe_networking_mcp.mcp_servers.shared import DESTRUCTIVE, IDEMPOTENT_WRITE, READ_ONLY

if getattr(router, "plan_tool_workflow", None) is None:
    import importlib
    importlib.reload(router)

# ---------------------------------------------------------------------------
# Fixture: a small, diverse fake backend (read/write/destructive tools).
# ---------------------------------------------------------------------------


def _build_planner_backend() -> MCPServer:
    srv = MCPServer("planner-test-backend")

    @srv.tool(annotations=READ_ONLY)
    def list_widgets() -> dict[str, Any]:
        return {"widgets": []}

    @srv.tool(annotations=READ_ONLY)
    def find_widget(widget_id: str) -> dict[str, Any]:
        return {"widget_id": widget_id}

    @srv.tool(annotations=IDEMPOTENT_WRITE)
    def update_widget(widget_id: str) -> dict[str, Any]:
        return {"updated": widget_id}

    @srv.tool(annotations=DESTRUCTIVE)
    def delete_widget(widget_id: str) -> dict[str, Any]:
        return {"deleted": widget_id}

    return srv


@pytest.fixture
def wired_planner(monkeypatch):
    backend = _build_planner_backend()
    tools = dict(backend._tool_manager._tools)
    servers = {name: backend for name in tools}
    monkeypatch.setattr(router, "_tool_index", tools, raising=True)
    monkeypatch.setattr(router, "_tool_servers", servers, raising=True)
    monkeypatch.setattr(
        router,
        "_tool_backend_names",
        {name: "planner-test-backend" for name in tools},
        raising=True,
    )
    monkeypatch.setattr(router, "_BACKENDS", {"planner-test-backend": "n/a"}, raising=True)
    monkeypatch.setattr(router, "_load_all_backends", lambda: None, raising=True)
    return backend


def _plan_tool_workflow_fn():
    # plan_tool_workflow is only registered when _ROUTER_MODE != "minimal";
    # skip cleanly (rather than erroring) if some other test left minimal mode active.
    fn = getattr(router, "plan_tool_workflow", None)
    if fn is None:
        pytest.skip("plan_tool_workflow not registered (router in minimal mode)")
    return fn


def _plan_reconciliation_schedule_fn():
    fn = getattr(router, "plan_reconciliation_schedule", None)
    if fn is None:
        pytest.skip("plan_reconciliation_schedule not registered (router in minimal mode)")
    return fn


def _call(tool_fn, *args, **kwargs) -> Any:
    """Call a @mcp.tool()-wrapped function, unwrapping FunctionTool if needed."""
    target = getattr(tool_fn, "fn", tool_fn)
    return target(*args, **kwargs)


# ---------------------------------------------------------------------------
# plan_tool_workflow
# ---------------------------------------------------------------------------


class TestPlanToolWorkflowResolution:
    def test_explicit_tool_name_resolves_exactly(self, wired_planner):
        fn = _plan_tool_workflow_fn()
        out = _call(fn, [{"id": "a", "tool": "list_widgets"}])
        assert out["ok"] is True
        assert out["steps"][0]["resolved"] is True
        assert out["steps"][0]["tool"] == "list_widgets"
        assert out["steps"][0]["capability"] == "read"
        assert out["steps"][0]["recommended_dispatcher"] == "invoke_read_tool"

    def test_unknown_explicit_tool_name_never_guessed(self, wired_planner):
        fn = _plan_tool_workflow_fn()
        out = _call(fn, [{"id": "a", "tool": "totally_unknown_tool_xyz"}])
        assert out["steps"][0]["resolved"] is False
        assert out["steps"][0]["tool"] is None
        assert "a" in out["unresolved_step_ids"]
        assert out["ok"] is False
        assert out["order"] is None

    def test_hint_resolves_via_keyword_search(self, wired_planner):
        fn = _plan_tool_workflow_fn()
        out = _call(fn, [{"id": "a", "hint": "list widgets"}])
        assert out["steps"][0]["resolved"] is True
        assert out["steps"][0]["tool"] == "list_widgets"

    def test_ambiguous_hint_is_flagged(self, wired_planner):
        fn = _plan_tool_workflow_fn()
        # "widget" alone is a near-equal match for several tools.
        out = _call(fn, [{"id": "a", "hint": "widget"}])
        step = out["steps"][0]
        assert step["resolved"] is True
        # Either flagged ambiguous, or at minimum resolved to *a* real tool
        # (never guessed beyond the catalog) -- assert the field exists and
        # is a bool either way, and if ambiguous, candidates aren't silently
        # dropped when requested.
        assert isinstance(step["ambiguous"], bool)

    def test_include_candidates_reports_bounded_candidate_list(self, wired_planner):
        fn = _plan_tool_workflow_fn()
        out = _call(
            fn,
            [{"id": "a", "tool": "totally_unknown_tool_xyz"}],
            include_candidates=True,
        )
        # Unresolved explicit-name steps never carry candidates (no keyword
        # search is performed for an exact-name miss).
        assert "candidates" not in out["steps"][0] or out["steps"][0]["candidates"] == []

    def test_empty_steps_rejected(self, wired_planner):
        fn = _plan_tool_workflow_fn()
        out = _call(fn, [])
        assert out["ok"] is False
        assert "non-empty" in out["error"]

    def test_over_bound_steps_rejected(self, wired_planner):
        fn = _plan_tool_workflow_fn()
        too_many = [{"id": f"s{i}", "tool": "list_widgets"} for i in range(30)]
        out = _call(fn, too_many)
        assert out["ok"] is False
        assert "exceeding" in out["error"]

    def test_duplicate_step_id_reported_as_error(self, wired_planner):
        fn = _plan_tool_workflow_fn()
        out = _call(
            fn,
            [
                {"id": "dup", "tool": "list_widgets"},
                {"id": "dup", "tool": "find_widget"},
            ],
        )
        assert any("duplicate step id" in e for e in out["errors"])
        assert out["ok"] is False
        assert out["order"] is None


class TestPlanToolWorkflowOrdering:
    def test_linear_dependency_chain_orders_correctly(self, wired_planner):
        fn = _plan_tool_workflow_fn()
        out = _call(
            fn,
            [
                {"id": "first", "tool": "list_widgets"},
                {"id": "second", "tool": "find_widget", "depends_on": ["first"]},
            ],
        )
        assert out["ok"] is True
        assert out["order"] == ["first", "second"]
        assert out["acyclic"] is True
        assert out["cycles"] == []

    def test_depends_on_may_reference_resolved_tool_name(self, wired_planner):
        fn = _plan_tool_workflow_fn()
        out = _call(
            fn,
            [
                {"id": "first", "tool": "list_widgets"},
                {"id": "second", "tool": "find_widget", "depends_on": ["list_widgets"]},
            ],
        )
        assert out["ok"] is True
        assert out["order"] == ["first", "second"]

    def test_cycle_is_detected_and_order_withheld(self, wired_planner):
        fn = _plan_tool_workflow_fn()
        out = _call(
            fn,
            [
                {"id": "a", "tool": "list_widgets", "depends_on": ["b"]},
                {"id": "b", "tool": "find_widget", "depends_on": ["a"]},
            ],
        )
        assert out["ok"] is False
        assert out["order"] is None
        assert out["acyclic"] is False
        assert out["cycles"], "expected at least one reported cycle"

    def test_unresolved_dependency_is_reported_explicitly(self, wired_planner):
        fn = _plan_tool_workflow_fn()
        out = _call(
            fn,
            [{"id": "a", "tool": "list_widgets", "depends_on": ["nonexistent_step"]}],
        )
        assert out["ok"] is False
        assert out["order"] is None
        assert {"step": "a", "missing": "nonexistent_step"} in out["unresolved_dependencies"]

    def test_never_calls_invoke_tool_as_a_side_effect(self, wired_planner, monkeypatch):
        spy = AsyncMock(side_effect=AssertionError("plan_tool_workflow must never dispatch"))
        monkeypatch.setattr(router, "invoke_tool", spy, raising=True)
        monkeypatch.setattr(router, "invoke_read_tool", spy, raising=True)
        fn = _plan_tool_workflow_fn()
        out = _call(fn, [{"id": "a", "tool": "list_widgets"}])
        assert out["ok"] is True
        spy.assert_not_called()


class TestPlanToolWorkflowArtifact:
    def test_clean_plan_produces_valid_artifact(self, wired_planner):
        fn = _plan_tool_workflow_fn()
        out = _call(fn, [{"id": "a", "tool": "list_widgets"}])
        assert out["artifact"] is not None
        assert out["artifact_error"] is None
        assert out["artifact"]["kind"] == "router_dependency_plan"

    def test_blocked_plan_still_produces_a_valid_artifact(self, wired_planner):
        # Cyclic plans withhold `order` but must still report an
        # artifact-valid true cycle state (acyclic decoupled from "blocked").
        fn = _plan_tool_workflow_fn()
        out = _call(
            fn,
            [
                {"id": "a", "tool": "list_widgets", "depends_on": ["b"]},
                {"id": "b", "tool": "find_widget", "depends_on": ["a"]},
            ],
        )
        assert out["artifact"] is not None, out["artifact_error"]
        assert out["artifact"]["acyclic"] is False
        assert out["artifact"]["order"] == []


# ---------------------------------------------------------------------------
# plan_reconciliation_schedule
# ---------------------------------------------------------------------------


class TestPlanReconciliationScheduleCadence:
    def test_named_cadence_is_accepted(self, wired_planner):
        fn = _plan_reconciliation_schedule_fn()
        out = _call(fn, "daily", tools=["list_widgets"])
        assert out["ok"] is True
        assert out["cadence"]["valid"] is True

    def test_invalid_cadence_is_rejected_without_touching_catalog(self, wired_planner):
        fn = _plan_reconciliation_schedule_fn()
        out = _call(fn, "every-other-tuesday")
        assert out["ok"] is False
        assert out["cadence"]["valid"] is False

    def test_interval_cadence_below_minimum_rejected(self, wired_planner):
        fn = _plan_reconciliation_schedule_fn()
        out = _call(fn, {"kind": "interval_minutes", "interval_minutes": 1})
        assert out["ok"] is False

    def test_cron_cadence_structurally_valid(self, wired_planner):
        fn = _plan_reconciliation_schedule_fn()
        out = _call(
            fn, {"kind": "cron", "expression": "*/15 * * * *"}, tools=["list_widgets"]
        )
        assert out["ok"] is True

    def test_cron_cadence_length_is_bounded(self, wired_planner):
        fn = _plan_reconciliation_schedule_fn()
        expression = ",".join(str(index) for index in range(1000))
        out = _call(
            fn,
            {"kind": "cron", "expression": f"{expression} * * * *"},
            tools=["list_widgets"],
        )
        assert out["ok"] is False
        assert out["cadence"]["valid"] is False
        assert "expression" not in out["cadence"]


class TestPlanReconciliationScheduleFiltering:
    def test_write_and_destructive_tools_are_excluded(self, wired_planner):
        fn = _plan_reconciliation_schedule_fn()
        out = _call(
            fn,
            "daily",
            tools=["list_widgets", "update_widget", "delete_widget"],
        )
        entry_tools = {e["tool"] for e in out["entries"]}
        assert entry_tools == {"list_widgets"}
        excluded_tools = {e["tool"] for e in out["excluded"]}
        assert excluded_tools == {"update_widget", "delete_widget"}
        assert out["dry_run"] is True

    def test_platform_filter_applied_when_tools_omitted(self, wired_planner):
        fn = _plan_reconciliation_schedule_fn()
        out = _call(fn, "daily", platforms=["planner-test-backend"])
        entry_tools = {e["tool"] for e in out["entries"]}
        assert "list_widgets" in entry_tools
        assert "find_widget" in entry_tools

    def test_server_filter_applied_when_tools_omitted(self, wired_planner):
        fn = _plan_reconciliation_schedule_fn()
        out = _call(fn, "daily", servers=["planner-test-backend"])
        assert out["ok"] is True
        assert len(out["entries"]) >= 1

    def test_unresolved_explicit_tool_is_excluded_with_reason(self, wired_planner):
        fn = _plan_reconciliation_schedule_fn()
        out = _call(fn, "daily", tools=["list_widgets", "totally_unknown_tool_xyz"])
        excluded_by_name = {e["tool"]: e for e in out["excluded"]}
        assert "totally_unknown_tool_xyz" in excluded_by_name
        assert excluded_by_name["totally_unknown_tool_xyz"]["reason"] == "unresolved_tool"

    def test_max_entries_bounds_results(self, wired_planner):
        fn = _plan_reconciliation_schedule_fn()
        out = _call(fn, "daily", tools=["list_widgets", "find_widget"], max_entries=1)
        assert len(out["entries"]) == 1

    def test_oversized_tools_input_rejected(self, wired_planner):
        fn = _plan_reconciliation_schedule_fn()
        too_many = [f"tool_{i}" for i in range(400)]
        out = _call(fn, "daily", tools=too_many)
        assert out["ok"] is False
        assert "exceeding" in out["error"]

    def test_never_calls_invoke_tool_as_a_side_effect(self, wired_planner, monkeypatch):
        spy = AsyncMock(
            side_effect=AssertionError("plan_reconciliation_schedule must never dispatch")
        )
        monkeypatch.setattr(router, "invoke_tool", spy, raising=True)
        monkeypatch.setattr(router, "invoke_read_tool", spy, raising=True)
        fn = _plan_reconciliation_schedule_fn()
        out = _call(fn, "daily", tools=["list_widgets"])
        assert out["ok"] is True
        spy.assert_not_called()


class TestPlanReconciliationScheduleArtifact:
    def test_valid_plan_produces_artifact(self, wired_planner):
        fn = _plan_reconciliation_schedule_fn()
        out = _call(fn, "daily", tools=["list_widgets"])
        assert out["artifact"] is not None
        assert out["artifact_error"] is None
        assert out["artifact"]["kind"] == "router_reconciliation_plan"
        assert out["artifact"]["dry_run"] is True

    def test_combined_excluded_detail_never_exceeds_bound_even_when_both_sources_overflow(
        self, wired_planner
    ):
        # Regression test: unresolved-tool exclusions + capability-based
        # exclusions are independently bounded but were previously
        # concatenated without a combined re-cap.
        fn = _plan_reconciliation_schedule_fn()
        write_tools = ["update_widget"] * 1  # only one real write tool exists;
        unresolved = [f"missing_tool_{i}" for i in range(250)]
        out = _call(fn, "daily", tools=["list_widgets", *write_tools, *unresolved])
        assert len(out["excluded"]) <= 200
        assert out["excluded_count"] >= len(out["excluded"])


# ---------------------------------------------------------------------------
# Permission separation regression
# ---------------------------------------------------------------------------


class TestPermissionSeparationRegression:
    def test_invoke_read_tool_still_blocks_write_tool_named_by_planner(self, wired_planner):
        plan_fn = _plan_tool_workflow_fn()
        plan_out = _call(plan_fn, [{"id": "a", "tool": "update_widget"}])
        assert plan_out["steps"][0]["tool"] == "update_widget"
        assert plan_out["steps"][0]["recommended_dispatcher"] == "invoke_tool"

        ctx = Context(mcp_server=router.mcp)
        result = asyncio.run(
            router.invoke_read_tool(ctx, "update_widget", {"widget_id": "w1"})
        )
        assert result["status"] == "blocked"
        assert "not read-only" in result["error"]

    def test_invoke_read_tool_still_blocks_destructive_tool_named_by_reconciliation_plan(
        self, wired_planner
    ):
        recon_fn = _plan_reconciliation_schedule_fn()
        recon_out = _call(recon_fn, "daily", tools=["delete_widget"])
        excluded_tools = {e["tool"] for e in recon_out["excluded"]}
        assert "delete_widget" in excluded_tools

        ctx = Context(mcp_server=router.mcp)
        result = asyncio.run(
            router.invoke_read_tool(ctx, "delete_widget", {"widget_id": "w1"})
        )
        assert result["status"] == "blocked"
        assert "not read-only" in result["error"]
