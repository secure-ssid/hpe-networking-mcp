"""Router-level integration tests for reactive error hints.

``tests/unit/test_specs_index_responses.py`` and ``test_error_help.py``
unit-test the spec-grounded lookup and the combined hint function in
isolation. ``test_mcp_middleware.py`` unit-tests ``ResponseEnvelopeMiddleware``
with no resolvers wired at all (the class's original, unconfigured shape).

Neither proves the actual wiring: that a REAL failed dispatch through the
router's REAL, production ``build_router_middlewares()`` chain -- the same
chain ``install_router_middleware()``/``main()`` install for every live
session -- gets ``platform`` resolved from the dispatched tool's real
backend server, and ``hint`` populated by ``error_help.reactive_hint`` using
that platform. This file proves that end-to-end over the real router
dispatch seam, the same way ``test_router_pii_tokenization.py`` proves PII
tokenization there rather than only on the isolated middleware class.

Uses the REAL default ``error_help``/``specs_index`` ``DB_PATH`` (no
monkeypatched db), so these tests also incidentally prove the feature
degrades correctly in this repo's *actual current* state: the committed
``data/specs.sqlite`` predates the ``responses`` table this backlog item
added, so the spec-grounded half is expected to be absent (``None``) until a
future real rebuild. Assertions use ``.startswith()``/membership against the
generic fallback text rather than exact equality so they keep passing after
that rebuild happens too, without weakening today's coverage.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp.server.mcpserver import Context, MCPServer

import hpe_networking_mcp.mcp_servers.tool_router as router
from hpe_networking_mcp.mcp_servers._middleware import uninstall_middleware
from hpe_networking_mcp.mcp_servers.shared import READ_ONLY
from hpe_networking_mcp.pipeline.clients.error_help import _GENERIC_STATUS_HINTS


def _build_fixture_backend() -> MCPServer:
    backend = MCPServer("fixture-backend")

    @backend.tool(annotations=READ_ONLY)
    def rate_limited_call() -> dict[str, Any]:
        """Fixture tool that always fails with a real, message-recoverable 429."""
        return {"error": "Client error '429 Too Many Requests' for url 'https://x/y'"}

    @backend.tool(annotations=READ_ONLY)
    def teapot_call() -> dict[str, Any]:
        """Fixture tool failing with a code with no generic-fallback entry."""
        return {"status_code": 418, "error": "I'm a teapot"}

    return backend


@pytest.fixture
def hint_router(monkeypatch):
    """Router wired to the fixture backend as ``mist-core`` (a real,
    ``_SERVER_PLATFORMS``-mapped backend), with the real middleware chain
    installed exactly as ``install_router_middleware()``/``main()`` do."""
    backend = _build_fixture_backend()
    tools = dict(backend._tool_manager._tools)

    monkeypatch.setattr(router, "_tool_index", tools, raising=True)
    monkeypatch.setattr(router, "_tool_servers", {n: backend for n in tools}, raising=True)
    monkeypatch.setattr(
        router, "_tool_backend_names", {n: "mist-core" for n in tools}, raising=True
    )
    monkeypatch.setattr(router, "_load_all_backends", lambda: None, raising=True)
    monkeypatch.delenv("HPE_MCP_READONLY", raising=False)

    router.install_router_middleware()
    try:
        yield tools
    finally:
        # ``router.mcp`` is module-level and shared with every other test in the
        # session; leaving the chain installed makes a later install here look
        # like it is about to drop someone else's interceptor.
        uninstall_middleware(router.mcp)


def _call(name: str, arguments: dict[str, Any]) -> Any:
    """Invoke a router tool through the real installed middleware chain."""
    ctx = Context(mcp_server=router.mcp)
    return asyncio.run(router.mcp._tool_manager.call_tool(name, arguments, context=ctx))


def test_dispatched_failure_resolves_backend_platform_and_generic_hint(hint_router):
    """invoke_read_tool dispatching to a mist-core-backed tool that fails
    with a 429 must resolve platform="mist" (from the *backend* the target
    tool belongs to, not the outer "invoke_read_tool" dispatcher name) and
    populate a hint at least containing the generic 429 fallback text."""
    result = _call("invoke_read_tool", {"name": "rate_limited_call", "arguments": {}})

    assert result["ok"] is False
    assert result["status"] == 429
    assert result["tool"] == "invoke_read_tool"  # outer call name -- unchanged behavior
    assert result["platform"] == "mist"
    assert result["hint"].startswith(_GENERIC_STATUS_HINTS[429])


def test_invoke_tool_dispatch_path_resolves_identically(hint_router):
    """The write-capable dispatcher (invoke_tool) must resolve platform/hint
    the same way as invoke_read_tool -- both funnel through the same
    _router_call_labels-based label_resolver."""
    result = _call("invoke_tool", {"name": "rate_limited_call", "arguments": {}})

    assert result["ok"] is False
    assert result["platform"] == "mist"
    assert result["hint"].startswith(_GENERIC_STATUS_HINTS[429])


def test_unresolvable_platform_still_gets_generic_hint_alone(monkeypatch, hint_router):
    """A backend server id with no _SERVER_PLATFORMS entry still resolves to
    *some* platform string (_server_platform's documented fallback), for
    which the spec index has no rows -- but the generic-fallback half of the
    hint must not depend on that resolution succeeding meaningfully."""
    monkeypatch.setattr(
        router,
        "_tool_backend_names",
        dict.fromkeys(hint_router, "totally-unknown-server"),
        raising=True,
    )

    result = _call("invoke_read_tool", {"name": "rate_limited_call", "arguments": {}})

    assert result["ok"] is False
    assert result["hint"] == _GENERIC_STATUS_HINTS[429]


def test_status_code_with_no_generic_entry_omits_hint_key_not_none(hint_router):
    """418 has no _GENERIC_STATUS_HINTS entry, and the committed specs index
    has no 'responses' table yet -- so today this must omit the 'hint' key
    entirely from the envelope, never set it to None, matching the
    additive/opt-in contract test_wraps_error_dict enforces for the
    unconfigured middleware."""
    result = _call("invoke_read_tool", {"name": "teapot_call", "arguments": {}})

    assert result["ok"] is False
    assert result["status"] == 418
    assert result["platform"] == "mist"
    assert "hint" not in result


def test_unknown_tool_dispatch_failure_is_unaffected(hint_router):
    """A dispatch to a name absent from the catalog must keep its existing
    404 unknown_tool behavior -- this feature must not change that path."""
    result = _call("invoke_read_tool", {"name": "does_not_exist", "arguments": {}})

    assert result["ok"] is False
    assert result["status"] == 404
