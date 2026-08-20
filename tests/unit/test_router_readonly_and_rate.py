"""Router-level regression tests for two audited fixes.

1. Convenience wrappers (list_sites/find_device/ask_docs/...) fan a single
   inbound MCP call into exactly one backend call via ``invoke_tool``. That
   backend call is charged one token at the dispatch rate gate, so the wrapper
   must be exempt from RateLimitMiddleware -- otherwise each wrapped read draws
   two tokens (middleware seam + gate) for one backend request.

2. The global ``HPE_MCP_READONLY`` kill switch hides/blocks only
   ``write``/``destructive`` tools -- consistently across discovery
   (keyword + semantic), dispatch, and direct-mode registration -- while
   leaving ``read`` and ``diagnostic`` tools fully available. It layers on top
   of the per-platform write gates without replacing them.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp.server.mcpserver import Context, MCPServer

import hpe_networking_mcp.mcp_servers.tool_router as router
from hpe_networking_mcp.mcp_servers.shared import (
    DESTRUCTIVE,
    DIAGNOSTIC,
    IDEMPOTENT_WRITE,
    READ_ONLY,
)


# ---------------------------------------------------------------------------
# Backend with one tool of each capability, wired like _load_all_backends.
# ---------------------------------------------------------------------------
def _build_backend() -> MCPServer:
    srv = MCPServer("test-backend")

    @srv.tool(annotations=READ_ONLY)
    def read_tool(value: int = 1) -> dict[str, Any]:
        return {"cap": "read", "value": value}

    @srv.tool(annotations=IDEMPOTENT_WRITE)
    def write_tool(value: int = 1) -> dict[str, Any]:
        return {"cap": "write", "value": value}

    @srv.tool(annotations=DESTRUCTIVE)
    def destroy_tool(value: int = 1) -> dict[str, Any]:
        return {"cap": "destructive", "value": value}

    @srv.tool()
    async def diag_tool(ctx: Context, value: int = 1) -> dict[str, Any]:
        return {"cap": "diagnostic", "value": value}

    # Give the diagnostic tool the DIAGNOSTIC annotation (MCPServer's bare
    # @tool() leaves annotations None otherwise).
    srv._tool_manager._tools["diag_tool"].annotations = DIAGNOSTIC
    return srv


@pytest.fixture
def wired_router(monkeypatch):
    backend = _build_backend()
    tools = dict(backend._tool_manager._tools)
    monkeypatch.setattr(router, "_tool_index", tools, raising=True)
    monkeypatch.setattr(
        router, "_tool_servers", {n: backend for n in tools}, raising=True
    )
    monkeypatch.setattr(
        router, "_tool_backend_names", {n: "test-backend" for n in tools}, raising=True
    )
    monkeypatch.setattr(router, "_load_all_backends", lambda: None, raising=True)
    return backend


def _dispatch(name: str, arguments: dict[str, Any] | None = None) -> Any:
    ctx = Context(mcp_server=router.mcp)
    return asyncio.run(router._dispatch_tool(ctx, name, arguments))


# ---------------------------------------------------------------------------
# Fix 1: dispatching wrappers are exempt from RateLimitMiddleware
# ---------------------------------------------------------------------------
class TestWrapperRateExemption:
    def test_all_wrappers_recorded_as_dispatching(self):
        # Every convenience/RAG wrapper that internally calls invoke_tool is
        # recorded so __main__ can exempt it. (In minimal mode the set is
        # empty because no wrappers are registered -- still a valid state.)
        for name in router._WRAPPER_DISPATCHING_TOOLS:
            assert name in router.mcp._tool_manager._tools

    def test_exempt_union_covers_primitives_and_wrappers(self):
        union = router._DISPATCHING_ROUTER_TOOLS | router._WRAPPER_DISPATCHING_TOOLS
        assert {"invoke_tool", "invoke_read_tool", "invoke_read_tool_batch"} <= union
        # No non-dispatching local tool (find_tool, planners) is exempt.
        assert "find_tool" not in union

    def test_wrapped_read_costs_exactly_one_token(self, wired_router, monkeypatch):
        from hpe_networking_mcp.mcp_servers._middleware.rate_limit import RateLimitMiddleware

        # Ensure list_sites is treated as a dispatching wrapper regardless of
        # router mode (minimal mode skips wrapper registration at import time).
        monkeypatch.setattr(
            router,
            "_WRAPPER_DISPATCHING_TOOLS",
            router._WRAPPER_DISPATCHING_TOOLS | {"list_sites"},
        )
        exempt = router._DISPATCHING_ROUTER_TOOLS | router._WRAPPER_DISPATCHING_TOOLS
        rl = RateLimitMiddleware(rate=1000.0, exempt_names=exempt)
        charges = {"gate": 0}

        async def gate() -> None:
            charges["gate"] += 1

        monkeypatch.setattr(router, "_dispatch_rate_gate", gate, raising=False)

        async def scenario() -> int:
            # Simulate a wrapper: middleware seam first (exempt => 0), then the
            # wrapper body dispatches one backend call (gate charges 1).
            wrapper_name = "list_sites"  # representative dispatching wrapper
            await rl.before_call(wrapper_name, {})
            mw = 0 if wrapper_name in rl.exempt_names else 1
            await router._dispatch_tool(Context(mcp_server=router.mcp), "read_tool", {})
            return mw + charges["gate"]

        assert asyncio.run(scenario()) == 1

    def test_non_dispatching_tool_still_charged_by_middleware(self):
        from hpe_networking_mcp.mcp_servers._middleware.rate_limit import RateLimitMiddleware

        exempt = router._DISPATCHING_ROUTER_TOOLS | router._WRAPPER_DISPATCHING_TOOLS
        rl = RateLimitMiddleware(rate=1000.0, exempt_names=exempt)
        # find_tool makes no backend call and is not exempt -> charged once.
        assert "find_tool" not in rl.exempt_names


# ---------------------------------------------------------------------------
# Fix 7: HPE_MCP_READONLY kill switch
# ---------------------------------------------------------------------------
class TestReadonlyKillSwitch:
    def test_safe_profile_blocks_write_discovery_and_dispatch(
        self, wired_router, monkeypatch
    ):
        monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "safe-read-only")
        names = {hit["name"] for hit in router._keyword_hits("tool", limit=10)}
        assert "read_tool" in names
        assert "write_tool" not in names

        blocked = _dispatch("write_tool", {})
        assert blocked["status"] == "blocked"
        assert "HPE_MCP_ACCESS_PROFILE=safe-read-only" in blocked["error"]

    def test_readonly_blocks_only_writes(self, wired_router, monkeypatch):
        monkeypatch.setenv("HPE_MCP_READONLY", "1")
        idx = router._tool_index
        assert router._readonly_blocks(idx["read_tool"]) is False
        assert router._readonly_blocks(idx["diag_tool"]) is False
        assert router._readonly_blocks(idx["write_tool"]) is True
        assert router._readonly_blocks(idx["destroy_tool"]) is True

    def test_readonly_off_blocks_nothing(self, wired_router, monkeypatch):
        monkeypatch.delenv("HPE_MCP_READONLY", raising=False)
        idx = router._tool_index
        assert router._readonly_blocks(idx["write_tool"]) is False
        assert router._readonly_blocks(idx["destroy_tool"]) is False

    def test_dispatch_blocks_write_and_destructive(self, wired_router, monkeypatch):
        monkeypatch.setenv("HPE_MCP_READONLY", "1")
        for name in ("write_tool", "destroy_tool"):
            out = _dispatch(name, {})
            assert isinstance(out, dict) and out.get("status") == "blocked"
            assert "HPE_MCP_READONLY" in out.get("error", "")

    def test_dispatch_allows_read_and_diagnostic(self, wired_router, monkeypatch):
        monkeypatch.setenv("HPE_MCP_READONLY", "1")
        read_out = _dispatch("read_tool", {"value": 5})
        # read result is unwrapped (dispatch returns the raw backend dict)
        assert read_out == {"cap": "read", "value": 5}
        diag_out = _dispatch("diag_tool", {"value": 3})
        assert diag_out.get("result", diag_out).get("cap") == "diagnostic"

    def test_dispatch_write_does_not_charge_rate_gate(self, wired_router, monkeypatch):
        monkeypatch.setenv("HPE_MCP_READONLY", "1")
        charges = {"n": 0}

        async def gate() -> None:
            charges["n"] += 1

        monkeypatch.setattr(router, "_dispatch_rate_gate", gate, raising=False)
        _dispatch("write_tool", {})
        # A readonly-refused call must not consume quota it never used.
        assert charges["n"] == 0

    def test_keyword_discovery_hides_writes(self, wired_router, monkeypatch):
        monkeypatch.setenv("HPE_MCP_READONLY", "1")
        hits = router._keyword_hits("tool", limit=10)
        names = {h["name"] for h in hits}
        assert "read_tool" in names
        assert "write_tool" not in names and "destroy_tool" not in names

    def test_keyword_discovery_shows_writes_when_off(self, wired_router, monkeypatch):
        monkeypatch.delenv("HPE_MCP_READONLY", raising=False)
        monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
        names = {h["name"] for h in router._keyword_hits("tool", limit=10)}
        assert {"read_tool", "write_tool"} <= names

    def test_direct_mode_registration_skips_writes(self, wired_router, monkeypatch):
        monkeypatch.setenv("HPE_MCP_READONLY", "1")
        monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
        target = MCPServer("direct-target")
        registered = router._register_direct_backend_tools(target)
        assert "read_tool" in registered and "diag_tool" in registered
        assert "write_tool" not in registered
        assert "destroy_tool" not in registered


# ---------------------------------------------------------------------------
# Fix 7 (standalone seam): install_platform_write_gate honors HPE_MCP_READONLY
# ---------------------------------------------------------------------------
class _GateTool:
    def __init__(self, ann: Any) -> None:
        self.annotations = ann


class _GateManager:
    def __init__(self, tools: dict[str, Any]) -> None:
        self._tools = tools
        self.ran: list[str] = []

    def get_tool(self, name: str) -> Any:
        return self._tools.get(name)

    async def call_tool(self, name, arguments, context=None, convert_result=False):  # noqa: ANN001
        self.ran.append(name)
        return {"ran": name}


class _GateMCP:
    def __init__(self, name: str, tools: dict[str, Any]) -> None:
        self.name = name
        self._tool_manager = _GateManager(tools)


class TestStandaloneGateReadonly:
    def _backend(self):
        import hpe_networking_mcp.mcp_servers.shared as sh

        return _GateMCP(
            "central-config",  # central: writes enabled by default
            {
                "r": _GateTool(sh.READ_ONLY),
                "w": _GateTool(sh.DESTRUCTIVE),
                "d": _GateTool(sh.DIAGNOSTIC),
            },
        )

    def _call(self, mcp_obj, name):
        return asyncio.run(mcp_obj._tool_manager.call_tool(name, {}, Context(mcp_server=mcp_obj)))

    def test_readonly_blocks_writes_on_standalone_backend(self, monkeypatch):
        import hpe_networking_mcp.mcp_servers.shared as sh

        mcp_obj = self._backend()
        assert sh.install_platform_write_gate(mcp_obj) is True
        monkeypatch.setenv("HPE_MCP_READONLY", "1")
        blocked = self._call(mcp_obj, "w")
        assert blocked.get("status") == "blocked"
        assert "HPE_MCP_READONLY" in blocked.get("error", "")
        # read + diagnostic still run
        assert self._call(mcp_obj, "r") == {"ran": "r"}
        assert self._call(mcp_obj, "d") == {"ran": "d"}

    def test_readonly_off_preserves_platform_gate(self, monkeypatch):
        import hpe_networking_mcp.mcp_servers.shared as sh

        mcp_obj = self._backend()
        sh.install_platform_write_gate(mcp_obj)
        monkeypatch.delenv("HPE_MCP_READONLY", raising=False)
        # central writes are enabled by default -> the write runs.
        assert self._call(mcp_obj, "w") == {"ran": "w"}

    def test_full_profile_allows_standalone_central_write(self, monkeypatch):
        import hpe_networking_mcp.mcp_servers.shared as sh

        monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "full-read-write")
        monkeypatch.delenv("HPE_MCP_READONLY", raising=False)
        monkeypatch.delenv("HPE_MCP_CENTRAL_WRITES", raising=False)
        mcp_obj = self._backend()
        assert sh.install_platform_write_gate(mcp_obj) is True
        assert self._call(mcp_obj, "w") == {"ran": "w"}

    def test_router_server_is_not_gated(self, monkeypatch):
        import hpe_networking_mcp.mcp_servers.shared as sh

        # The router must NOT be gated (platform None), or its DESTRUCTIVE-
        # annotated invoke_tool dispatcher would be wholesale-blocked and could
        # no longer dispatch read/diagnostic tools.
        monkeypatch.delenv("HPE_MCP_PRODUCT_ACCESS", raising=False)
        monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "safe-read-only")
        router_mcp = _GateMCP("hpe-networking-mcp", {})
        assert sh.install_platform_write_gate(router_mcp) is False
