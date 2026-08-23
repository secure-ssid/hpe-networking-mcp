"""Router-level regression: raised backend exceptions must be redacted.

Root cause (OX.Alpha-debugger, HX-1): ``SecretTokenizeMiddleware.after_call``
tokenizes returned results, but its ``on_error`` is a no-op; when a backend
tool *raises*, the exception propagates past the middleware interceptor and
``tool_router._dispatch_tool`` stringifies it raw --

    result = {"error": f"{type(e).__name__}: {e}"}

-- outside any tokenization/redaction pass, so a raised exception whose
message contains a bearer-shaped credential leaks that credential into the
client-visible error envelope verbatim.

This file proves the fix over the REAL router dispatch seam (the same chain
``install_router_middleware()``/``main()`` install for every live session),
matching the pattern used by ``test_router_error_hints.py``: a fixture
backend whose tools *raise* instead of returning error dicts, dispatched
through ``invoke_read_tool``/``invoke_tool``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp.server.mcpserver import Context, MCPServer

import hpe_networking_mcp.mcp_servers.tool_router as router
from hpe_networking_mcp.mcp_servers._middleware import uninstall_middleware
from hpe_networking_mcp.mcp_servers.shared import READ_ONLY

_RAW_BEARER = "Bearer sk-abc1234567890-raise-path-secret-value"


def _build_raising_backend() -> MCPServer:
    backend = MCPServer("fixture-raise-backend")

    @backend.tool(annotations=READ_ONLY)
    def bearer_call() -> dict[str, Any]:
        """Fixture tool that raises an exception carrying a bearer credential."""
        raise RuntimeError(_RAW_BEARER)

    @backend.tool(annotations=READ_ONLY)
    def boom_call() -> dict[str, Any]:
        """Fixture tool raising an ordinary, non-secret exception."""
        raise ValueError("boom")

    @backend.tool(annotations=READ_ONLY)
    def bearer_prose_call() -> dict[str, Any]:
        """Fixture tool raising a 401-style prose message containing the word
        ``Bearer`` -- must survive the credential mask unchanged."""
        raise RuntimeError("401 Unauthorized: Bearer token missing or expired")

    return backend


@pytest.fixture
def raise_router(monkeypatch):
    """Router wired to the raising fixture backend as ``mist-core``, with the
    real production middleware chain installed exactly as ``main()`` does."""
    backend = _build_raising_backend()
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
        uninstall_middleware(router.mcp)


def _call(name: str, arguments: dict[str, Any]) -> Any:
    """Invoke a router tool through the real installed middleware chain."""
    ctx = Context(mcp_server=router.mcp)
    return asyncio.run(router.mcp._tool_manager.call_tool(name, arguments, context=ctx))


def _flatten_text(value: Any) -> str:
    """Render a result tree to text so assertions can grep the raw credential."""
    return repr(value)


def test_raised_bearer_credential_is_redacted_in_client_visible_result(raise_router):
    """A backend tool raising an exception containing a bearer-shaped string
    must never surface that raw string to the caller: the caught-error dict
    is redacted before the envelope wraps it (HX-1 acceptance a)."""
    result = _call("invoke_read_tool", {"name": "bearer_call", "arguments": {}})

    text = _flatten_text(result)
    assert _RAW_BEARER not in text
    assert "sk-abc1234567890" not in text
    # The redacted marker must be present, and the failure still structured
    # as an enveloped error.
    assert "******" in text
    assert result["ok"] is False


def test_ordinary_raise_text_passes_through_unchanged(raise_router):
    """An ordinary, non-secret exception message must survive verbatim
    (HX-1 acceptance b) -- redaction must not mangle benign error text.
    The SDK reframes the raise as ``ToolError: Error executing tool <name>:
    boom``, so assert the benign message text passes through untouched and
    no redaction marker is introduced."""
    result = _call("invoke_tool", {"name": "boom_call", "arguments": {}})

    text = _flatten_text(result)
    assert "Error executing tool boom_call: boom" in text
    assert "boom" in text
    assert "******" not in text
    assert result["ok"] is False


@pytest.mark.parametrize(
    "prose",
    [
        "401 Unauthorized: Bearer token missing or expired",
        'WWW-Authenticate: Bearer realm="api", error="invalid_token"',
    ],
)
def test_bearer_prose_survives_unchanged(raise_router, prose):
    """The credential mask must NOT over-mask ordinary 401 prose that merely
    contains the word ``Bearer`` (Sentinel amendment, PR #39): real platform
    credentials are >=16 chars, so an 8+ quantifier preserves masking while
    letting prose like ``Bearer token missing or expired`` pass through."""
    result = _call("invoke_read_tool", {"name": "bearer_prose_call", "arguments": {}})

    # Probe the router's own mask helper directly (covering both rows of
    # Sentinel's probe shape), independent of the fixture backend.
    masked = router._redact_dispatch_error(prose)
    assert masked == prose
    assert "******" not in masked

    text = _flatten_text(result)
    assert "Bearer token missing or expired" in text
    assert "******" not in text