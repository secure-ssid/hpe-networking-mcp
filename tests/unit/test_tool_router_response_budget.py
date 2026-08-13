"""Unit tests for the router's dispatch-level response budgeting (v0.7).

Covers ``hpe_networking_mcp.mcp_servers.tool_router._bound_router_response`` both directly and
through ``invoke_tool``/``invoke_read_tool`` dispatch, proving:

- Small dict/list/scalar responses pass through byte-for-byte unchanged
  (no new keys added) -- default behavior is preserved.
- Error/blocked dicts (an ``error`` key present) are never touched, even
  when huge.
- Oversized list/dict responses get a stable ``_response_bounds``
  continuation marker plus the existing ``_pagination`` shape from
  ``hpe_networking_mcp.mcp_servers.shared.bound_collection_response``.
- A dict with nothing sliceable that still exceeds the byte budget falls
  back to a bounded text ``preview``.
- The two env var overrides (``HPE_MCP_ROUTER_RESPONSE_MAX_ITEMS`` /
  ``HPE_MCP_ROUTER_RESPONSE_MAX_BYTES``) are honored and invalid values
  fall back to defaults rather than raising.
- Budgeting is reachable end-to-end through real dispatch
  (``invoke_tool``/``invoke_read_tool``), not just the helper in isolation.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp.server.mcpserver import Context, MCPServer

import hpe_networking_mcp.mcp_servers.tool_router as router
from hpe_networking_mcp.mcp_servers.shared import READ_ONLY

# ---------------------------------------------------------------------------
# Direct unit tests for _bound_router_response
# ---------------------------------------------------------------------------


class TestBoundRouterResponseUnit:
    def test_small_dict_passes_through_unchanged(self):
        result = {"a": 1, "b": [1, 2, 3]}
        out = router._bound_router_response(result, max_items=500, max_bytes=200_000)
        assert out is result

    def test_small_list_passes_through_unchanged(self):
        result = [1, 2, 3]
        out = router._bound_router_response(result, max_items=500, max_bytes=200_000)
        assert out is result

    def test_scalar_passes_through_unchanged(self):
        assert router._bound_router_response("hello", max_items=1, max_bytes=1) == "hello"
        assert router._bound_router_response(42, max_items=1, max_bytes=1) == 42
        assert router._bound_router_response(None, max_items=1, max_bytes=1) is None
        assert router._bound_router_response(True, max_items=1, max_bytes=1) is True

    def test_error_dict_never_touched_even_when_huge(self):
        result = {"error": "boom", "detail": "x" * 5000}
        out = router._bound_router_response(result, max_items=1, max_bytes=10)
        assert out is result

    def test_oversized_list_gets_pagination_and_response_bounds(self):
        result = list(range(100))
        out = router._bound_router_response(result, max_items=10, max_bytes=200_000)
        assert isinstance(out, dict)
        assert "_pagination" in out
        assert out["_pagination"]["truncated"] is True
        assert out["_response_bounds"]["truncated"] is True
        assert out["_response_bounds"]["reason"] == "item_budget"
        assert out["_response_bounds"]["item_limit"] == 10
        assert len(out["items"]) == 10

    def test_requested_item_budget_is_capped_to_shared_collection_limit(self):
        result = {"items": [{"i": i} for i in range(600)]}
        out = router._bound_router_response(
            result,
            max_items=500,
            max_bytes=200_000,
        )
        assert len(out["items"]) == router.MAX_LIST_LIMIT
        assert out["_pagination"]["limit"] == router.MAX_LIST_LIMIT
        assert out["_response_bounds"]["item_limit"] == router.MAX_LIST_LIMIT

    def test_dict_with_oversized_nested_list_gets_bounded(self):
        result = {"devices": [{"serial": f"sn-{i}"} for i in range(200)], "meta": "ok"}
        out = router._bound_router_response(result, max_items=5, max_bytes=200_000)
        assert len(out["devices"]) == 5
        assert out["_response_bounds"]["reason"] == "item_budget"
        assert out["meta"] == "ok"

    def test_byte_budget_alone_triggers_item_shrink(self):
        result = {"items": [{"blob": "y" * 200} for _ in range(50)]}
        out = router._bound_router_response(result, max_items=500, max_bytes=2000)
        assert len(out["items"]) < 50
        assert "byte_budget" in out["_response_bounds"]["reason"]

    def test_nothing_sliceable_falls_back_to_preview(self):
        result = {"summary": "z" * 5000}
        out = router._bound_router_response(result, max_items=500, max_bytes=1024)
        assert "_response_bounds" in out
        assert out["_response_bounds"]["reason"] == "byte_budget"
        assert "preview" in out
        assert isinstance(out["preview"], str)
        assert len(out["preview"].encode("utf-8")) <= 1024

    def test_huge_single_item_falls_back_to_preview_when_slicing_cannot_help(self):
        # A single giant item: slicing the primary list to 1 item still
        # can't fit the byte budget, so the byte-preview fallback must win.
        result = {"items": ["z" * 5000]}
        out = router._bound_router_response(result, max_items=500, max_bytes=1024)
        assert "preview" in out
        assert out["_response_bounds"]["reason"] == "byte_budget"

    def test_within_item_budget_but_not_byte_budget_reports_combined_reason(self):
        result = {"items": [{"blob": "y" * 500} for _ in range(3)]}
        out = router._bound_router_response(result, max_items=500, max_bytes=600)
        assert "_response_bounds" in out
        # Item count (3) is within the 500 budget, so shrinking is driven
        # purely by the byte budget.
        assert out["_response_bounds"]["reason"] in {"byte_budget", "item_budget+byte_budget"}


# ---------------------------------------------------------------------------
# Env var configuration
# ---------------------------------------------------------------------------


class TestResponseBudgetEnvConfig:
    def test_items_env_override_is_honored(self, monkeypatch):
        monkeypatch.setenv(router._RESPONSE_BUDGET_ITEMS_ENV, "3")
        assert router._response_budget_items() == 3

    def test_items_env_above_shared_limit_is_clamped(self, monkeypatch):
        monkeypatch.setenv(router._RESPONSE_BUDGET_ITEMS_ENV, "500")
        assert router._response_budget_items() == router.MAX_LIST_LIMIT

    def test_items_env_invalid_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(router._RESPONSE_BUDGET_ITEMS_ENV, "not-a-number")
        assert router._response_budget_items() == router._RESPONSE_BUDGET_DEFAULT_ITEMS

    def test_items_env_missing_falls_back_to_default(self, monkeypatch):
        monkeypatch.delenv(router._RESPONSE_BUDGET_ITEMS_ENV, raising=False)
        assert router._response_budget_items() == router._RESPONSE_BUDGET_DEFAULT_ITEMS

    def test_bytes_env_override_is_honored(self, monkeypatch):
        monkeypatch.setenv(router._RESPONSE_BUDGET_BYTES_ENV, "5000")
        assert router._response_budget_bytes() == 5000

    def test_bytes_env_below_minimum_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(router._RESPONSE_BUDGET_BYTES_ENV, "1")
        assert router._response_budget_bytes() == router._RESPONSE_BUDGET_DEFAULT_BYTES

    def test_bytes_env_invalid_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(router._RESPONSE_BUDGET_BYTES_ENV, "garbage")
        assert router._response_budget_bytes() == router._RESPONSE_BUDGET_DEFAULT_BYTES


# ---------------------------------------------------------------------------
# End-to-end through real dispatch (invoke_tool / invoke_read_tool)
# ---------------------------------------------------------------------------


def _build_backend() -> MCPServer:
    srv = MCPServer("budget-test-backend")

    @srv.tool(annotations=READ_ONLY)
    def small_result() -> dict[str, Any]:
        return {"ok": True, "value": 1}

    @srv.tool(annotations=READ_ONLY)
    def big_list_result() -> list[int]:
        return list(range(1000))

    @srv.tool(annotations=READ_ONLY)
    def error_like_result() -> dict[str, Any]:
        return {"error": "backend refused", "detail": "x" * 10_000}

    return srv


@pytest.fixture
def wired_budget_router(monkeypatch):
    backend = _build_backend()
    tools = dict(backend._tool_manager._tools)
    servers = {name: backend for name in tools}
    monkeypatch.setattr(router, "_tool_index", tools, raising=True)
    monkeypatch.setattr(router, "_tool_servers", servers, raising=True)
    monkeypatch.setattr(
        router, "_tool_backend_names", {name: "budget-test-backend" for name in tools}, raising=True
    )
    monkeypatch.setattr(router, "_load_all_backends", lambda: None, raising=True)
    return backend


def _invoke(name: str) -> Any:
    ctx = Context(mcp_server=router.mcp)
    return asyncio.run(router.invoke_tool(ctx, name, {}))


def _invoke_read(name: str) -> Any:
    ctx = Context(mcp_server=router.mcp)
    return asyncio.run(router.invoke_read_tool(ctx, name, {}))


class TestDispatchAppliesResponseBudget:
    def test_small_result_unaffected_through_invoke_tool(self, wired_budget_router):
        out = _invoke("small_result")
        assert out == {"ok": True, "value": 1}

    def test_small_result_unaffected_through_invoke_read_tool(self, wired_budget_router):
        out = _invoke_read("small_result")
        assert out == {"ok": True, "value": 1}

    def test_big_list_result_gets_bounded_through_invoke_tool(
        self, wired_budget_router, monkeypatch
    ):
        monkeypatch.setenv(router._RESPONSE_BUDGET_ITEMS_ENV, "25")
        out = _invoke("big_list_result")
        assert isinstance(out, dict)
        assert "_pagination" in out
        assert out["_response_bounds"]["truncated"] is True
        assert len(out["items"]) == 25

    def test_error_like_result_never_bounded_through_invoke_tool(
        self, wired_budget_router, monkeypatch
    ):
        monkeypatch.setenv(router._RESPONSE_BUDGET_BYTES_ENV, "1024")
        out = _invoke("error_like_result")
        assert out["error"] == "backend refused"
        assert "_response_bounds" not in out
        assert len(out["detail"]) == 10_000
