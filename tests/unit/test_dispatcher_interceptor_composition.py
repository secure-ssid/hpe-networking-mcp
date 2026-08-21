"""Re-installing a dispatcher interceptor must never silently remove another.

The write gate and the middleware chain both intercept the same seam --
``ToolManager.call_tool``, claimed through ``_sdk_compat.claim_dispatcher`` under
their own marker. Each remembers the dispatcher it found so it can call onward.

That snapshot goes stale the moment a *second* interceptor wraps on top. Before
this was fixed, re-installing whichever was installed first rebuilt the chain
from its stale snapshot, dropping everything installed after it -- with no
error, no log and no failing test. When the dropped interceptor is the platform
write gate, a security boundary disappears and the next write executes.

Three docstrings actively recommended the operation that did it:
``claim_dispatcher`` ("compose in either install order"),
``install_platform_write_gate`` ("composes safely ... in either order") and
``install_middleware`` ("Idempotent"). A docstring that oversells a security
seam is worse than none, so those now describe the real contract: install each
interceptor exactly once, outermost last; an unsafe re-claim raises.

These tests pin the behaviour, not the prose.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp.server.mcpserver import Context, MCPServer

from hpe_networking_mcp.mcp_servers import _sdk_compat
from hpe_networking_mcp.mcp_servers._middleware import install_middleware
from hpe_networking_mcp.mcp_servers.shared import (
    DESTRUCTIVE,
    READ_ONLY,
    install_platform_write_gate,
)

GATE_ENV = (
    "HPE_MCP_ACCESS_PROFILE",
    "HPE_MCP_READONLY",
    "HPE_MCP_CENTRAL_WRITES",
    "HPE_MCP_PRODUCT_ACCESS",
    "HPE_MCP_GLP_V2BETA1_WRITES",
)


class _RecordingMiddleware:
    """Observes calls so a dropped chain is visible, not merely inferred."""

    def __init__(self, seen: list[str]) -> None:
        self.seen = seen

    def before_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.seen.append(name)
        return arguments


def _backend() -> MCPServer:
    srv = MCPServer("central-config")

    @srv.tool(annotations=READ_ONLY)
    def read_thing() -> dict[str, Any]:
        return {"ok": True}

    @srv.tool(annotations=DESTRUCTIVE)
    def destroy_thing() -> dict[str, Any]:
        return {"destroyed": True}

    return srv


def _call(server: MCPServer, name: str) -> Any:
    return asyncio.run(
        server._tool_manager.call_tool(name, {}, Context(mcp_server=server))
    )


def _blocked(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("status") == "blocked"


@pytest.fixture(autouse=True)
def _deny_by_default(monkeypatch):
    """Central writes off: the gate must refuse `destroy_thing` while installed."""
    for key in GATE_ENV:
        monkeypatch.delenv(key, raising=False)


def test_production_install_order_gates_writes_and_runs_middleware():
    """Baseline: middleware first, gate outermost -- the real `run_server` order."""
    srv = _backend()
    seen: list[str] = []
    install_middleware(srv, [_RecordingMiddleware(seen)])
    assert install_platform_write_gate(srv) is True

    assert _blocked(_call(srv, "destroy_thing"))
    # The gate refuses before delegating, so the inner chain never sees it.
    assert seen == []
    # A read passes through the gate into the middleware chain.
    assert _call(srv, "read_thing") == {"ok": True}
    assert seen == ["read_thing"]


def test_reinstalling_middleware_under_a_gate_raises_instead_of_dropping_it():
    """The defect, inverted: this used to remove the write gate and return None.

    Reproduced before the fix as::

        after mw-then-gate:     blocked=True   middleware_saw=[]
        after re-install of mw: blocked=False  middleware_saw=['destroy_thing']

    The second line is a destructive tool executing on a server whose write gate
    was installed and enabled. Now the re-install refuses.
    """
    srv = _backend()
    seen: list[str] = []
    install_middleware(srv, [_RecordingMiddleware(seen)])
    install_platform_write_gate(srv)
    assert _blocked(_call(srv, "destroy_thing"))

    with pytest.raises(RuntimeError) as excinfo:
        install_middleware(srv, [_RecordingMiddleware(seen)])

    message = str(excinfo.value)
    assert "another interceptor has wrapped the tool dispatcher" in message
    assert "central-config" in message

    # The refusal must leave the gate exactly as it was, not half-torn-down.
    assert _blocked(_call(srv, "destroy_thing"))
    assert _call(srv, "read_thing") == {"ok": True}


def test_the_write_gate_survives_a_middleware_reinstall_attempt():
    """The security invariant, asserted independently of *how* it is preserved.

    Kept separate from the ``RuntimeError`` contract on purpose. If someone
    later decides re-snapshotting is preferable to refusing, that is a defensible
    change and this test must still pass -- what must never happen is the write
    gate going missing. Before the fix this failed with the destructive tool
    executing and the middleware chain, not the gate, observing the call.
    """
    srv = _backend()
    seen: list[str] = []
    install_middleware(srv, [_RecordingMiddleware(seen)])
    install_platform_write_gate(srv)
    assert _blocked(_call(srv, "destroy_thing"))

    try:
        install_middleware(srv, [_RecordingMiddleware(seen)])
    except RuntimeError:
        pass

    after = _call(srv, "destroy_thing")
    assert _blocked(after), (
        "the platform write gate was silently removed by re-installing the "
        f"middleware chain: destroy_thing returned {after!r}, and the middleware "
        f"chain observed {seen!r} -- meaning the call reached the tool body"
    )
    assert _blocked(_call(srv, "destroy_thing"))
    assert _call(srv, "read_thing") == {"ok": True}


def test_reinstalling_the_gate_is_still_a_safe_refresh():
    """The gate is outermost, so replacing its own wrapper drops nothing.

    This is the case that must keep working: `install_platform_write_gate` is
    documented idempotent and tests re-install it freely.
    """
    srv = _backend()
    seen: list[str] = []
    install_middleware(srv, [_RecordingMiddleware(seen)])
    install_platform_write_gate(srv)

    assert install_platform_write_gate(srv) is True

    assert _blocked(_call(srv, "destroy_thing"))
    assert _call(srv, "read_thing") == {"ok": True}
    # The middleware chain survived the refresh -- it was not rebuilt away.
    assert seen == ["read_thing"]


def test_reinstalling_middleware_alone_still_replaces_rather_than_stacks():
    """No second interceptor, so the historical idempotency guarantee holds."""
    srv = _backend()
    seen: list[str] = []
    install_middleware(srv, [_RecordingMiddleware(seen)])
    install_middleware(srv, [_RecordingMiddleware(seen)])

    assert _call(srv, "read_thing") == {"ok": True}
    assert seen == ["read_thing"], "chain stacked instead of being replaced"


def test_claim_dispatcher_refuses_a_stale_reclaim_directly():
    """The seam itself, without either interceptor, so the rule is unambiguous."""
    srv = _backend()
    pristine = srv._tool_manager.call_tool

    first = _sdk_compat.claim_dispatcher(srv, "_marker_a")
    assert first == pristine

    async def wrapper_a(name, arguments, context=None, convert_result=False):
        return await first(name, arguments, context=context, convert_result=convert_result)

    _sdk_compat.set_dispatcher(srv, wrapper_a, "_marker_a")

    # Re-claiming while still outermost is the safe refresh.
    assert _sdk_compat.claim_dispatcher(srv, "_marker_a") == pristine

    # Someone else wraps on top...
    second = _sdk_compat.claim_dispatcher(srv, "_marker_b")
    assert second is wrapper_a

    async def wrapper_b(name, arguments, context=None, convert_result=False):
        return await second(name, arguments, context=context, convert_result=convert_result)

    _sdk_compat.set_dispatcher(srv, wrapper_b, "_marker_b")

    # ...so marker A's saved original is now stale and rebuilding would drop B.
    with pytest.raises(RuntimeError, match="_marker_a"):
        _sdk_compat.claim_dispatcher(srv, "_marker_a")

    # B, still outermost, may refresh.
    assert _sdk_compat.claim_dispatcher(srv, "_marker_b") is wrapper_a
