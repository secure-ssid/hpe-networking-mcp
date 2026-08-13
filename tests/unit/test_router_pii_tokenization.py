"""Router/protocol-level regression tests for PII tokenization.

``tests/unit/test_pii_tokenizer_middleware.py`` unit-tests
``PIITokenizeMiddleware`` in isolation. That was not enough: the middleware
was only installed on ``central-nac``/``clearpass-core``, and the documented
default profile (``HPE_MCP_ROUTER_MODE=minimal``,
``HPE_MCP_TOOLSETS=central,glp,rag``) never runs a backend's own middleware
at all -- backends are imported in-process and every call arrives at the
*router* as ``invoke_read_tool``/``invoke_tool``. So visitor/guest
``email``/``phone``/``company_name`` values reached the model in the clear on
the one path virtually every session actually uses.

These tests therefore exercise the real router seam, not the class:

1. ``mcp._tool_manager.call_tool(...)`` on the router with its real,
   production middleware chain installed via
   ``tool_router.install_router_middleware()`` -- the exact seam
   ``install_middleware`` patches and every MCP request flows through.
2. A real JSON-RPC round trip over the SDK's in-memory transport
   (``mcp.client.Client``), so serialization/deserialization of the
   tokenized payload is covered too.

Both prove the round trip: nested PII returned through ``invoke_read_tool``
is tokenized, and the opaque token resolves back to plaintext when the model
feeds it into a subsequent ``invoke_tool`` write argument.

Credential/network independent: the "NAC backend" here is a local
``MCPServer`` with two fixture tools. No Central/ClearPass client, config
file, or socket is involved.

This repo has no pytest-asyncio/anyio plugin (see the other async tests in
this suite), so each async body is wrapped in ``asyncio.run(...)``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp.client import Client
from mcp.server.mcpserver import Context, MCPServer

import hpe_networking_mcp.mcp_servers.tool_router as router
from hpe_networking_mcp.mcp_servers._middleware.pii_tokenizer import (
    PIITokenizeMiddleware,
)
from hpe_networking_mcp.mcp_servers.shared import IDEMPOTENT_WRITE, READ_ONLY

PII_TOKEN_PREFIX = "hpe_mcp_pii_"

# Plaintext that must never survive a router response once tokenization is on.
VISITOR_EMAIL = "ada.lovelace@example.com"
VISITOR_PHONE = "+1-555-0100"
SPONSOR_EMAIL = "sponsor@example.com"
COMPANY_NAME = "Analytical Engines Ltd"


def _build_nac_backend(seen: dict[str, Any]) -> MCPServer:
    """A local stand-in for central-nac/clearpass-core visitor tools.

    ``list_visitors`` returns PII nested two levels deep (list -> dict ->
    dict), which is what the flat unit tests did not cover. ``update_visitor``
    records the argument value it actually received so the resolve direction
    is asserted on real backend input, not on a middleware return value.
    """
    backend = MCPServer("central-nac")

    @backend.tool(annotations=READ_ONLY)
    def list_visitors(limit: int = 10) -> dict[str, Any]:
        """List visitors (fixture)."""
        return {
            "items": [
                {
                    "id": "v-1",
                    "visitor_name": "Ada Lovelace",
                    "email": VISITOR_EMAIL,
                    "phone": VISITOR_PHONE,
                    "company_name": COMPANY_NAME,
                    "sponsor": {"sponsor_email": SPONSOR_EMAIL},
                }
            ][:limit],
            "total": 1,
        }

    @backend.tool(annotations=IDEMPOTENT_WRITE)
    def update_visitor(visitor_id: str, email: str) -> dict[str, Any]:
        """Update a visitor (fixture) -- records what the backend received."""
        seen["visitor_id"] = visitor_id
        seen["email"] = email
        return {"status": "updated", "visitor_id": visitor_id}

    return backend


@pytest.fixture
def pii_router(monkeypatch):
    """Router wired to the fixture backend with the real middleware chain."""
    seen: dict[str, Any] = {}
    backend = _build_nac_backend(seen)
    tools = dict(backend._tool_manager._tools)

    monkeypatch.setattr(router, "_tool_index", tools, raising=True)
    monkeypatch.setattr(router, "_tool_servers", {n: backend for n in tools}, raising=True)
    monkeypatch.setattr(
        router, "_tool_backend_names", {n: "central-nac" for n in tools}, raising=True
    )
    monkeypatch.setattr(router, "_load_all_backends", lambda: None, raising=True)

    monkeypatch.setenv("HPE_MCP_TOKENIZE_PII", "1")
    monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
    monkeypatch.delenv("HPE_MCP_READONLY", raising=False)

    original_call_tool = router.mcp._tool_manager.call_tool
    router.install_router_middleware()
    try:
        yield seen
    finally:
        router.mcp._tool_manager.call_tool = original_call_tool


def _call(name: str, arguments: dict[str, Any]) -> Any:
    """Invoke a router tool through the installed middleware chain."""
    ctx = Context(mcp_server=router.mcp)
    return asyncio.run(
        router.mcp._tool_manager.call_tool(name, arguments, context=ctx)
    )


def _tokens_in(payload: Any) -> list[str]:
    text = json.dumps(payload, default=str)
    return [
        chunk
        for chunk in text.replace('"', " ").replace(",", " ").split()
        if chunk.startswith(PII_TOKEN_PREFIX)
    ]


# ---------------------------------------------------------------------------
# Chain composition
# ---------------------------------------------------------------------------
def test_router_middleware_chain_installs_pii_tokenizer():
    """The router's production chain includes PIITokenizeMiddleware."""
    chain = router.build_router_middlewares()
    assert any(isinstance(mw, PIITokenizeMiddleware) for mw in chain)


def test_pii_tokenizer_runs_after_secret_tokenizer():
    """Stable, documented ordering between the two independent vaults."""
    from hpe_networking_mcp.mcp_servers._middleware.secret_tokenizer import (
        SecretTokenizeMiddleware,
    )

    chain = router.build_router_middlewares()
    secret_at = next(
        i for i, mw in enumerate(chain) if isinstance(mw, SecretTokenizeMiddleware)
    )
    pii_at = next(i for i, mw in enumerate(chain) if isinstance(mw, PIITokenizeMiddleware))
    assert secret_at < pii_at


# ---------------------------------------------------------------------------
# Router dispatch seam
# ---------------------------------------------------------------------------
def test_invoke_read_tool_tokenizes_nested_pii(pii_router):
    result = _call("invoke_read_tool", {"name": "list_visitors", "arguments": {}})
    text = json.dumps(result, default=str)

    for plaintext in (VISITOR_EMAIL, VISITOR_PHONE, SPONSOR_EMAIL, COMPANY_NAME):
        assert plaintext not in text, f"{plaintext} leaked through router dispatch"

    # One token per PII-keyed value, including the nested sponsor.sponsor_email
    # and the list-nested visitor fields.
    assert len(_tokens_in(result)) == 5

    # Non-PII fields are untouched.
    assert "v-1" in text


def test_token_resolves_back_to_plaintext_on_a_later_write(pii_router):
    seen = pii_router
    read_result = _call("invoke_read_tool", {"name": "list_visitors", "arguments": {}})
    visitor = read_result["items"][0]
    email_token = visitor["email"]
    assert email_token.startswith(PII_TOKEN_PREFIX)

    write_result = _call(
        "invoke_tool",
        {
            "name": "update_visitor",
            "arguments": {"visitor_id": "v-1", "email": email_token},
        },
    )

    # The backend received the real address, never the token.
    assert seen["email"] == VISITOR_EMAIL
    assert seen["visitor_id"] == "v-1"
    assert write_result["status"] == "updated"


def test_tokenization_is_off_by_default(pii_router, monkeypatch):
    """Without the opt-in env var the router chain is a no-op (unchanged behavior)."""
    monkeypatch.delenv("HPE_MCP_TOKENIZE_PII", raising=False)

    result = _call("invoke_read_tool", {"name": "list_visitors", "arguments": {}})

    assert result["items"][0]["email"] == VISITOR_EMAIL
    assert _tokens_in(result) == []


def test_unknown_token_is_left_alone_and_never_reaches_plaintext(pii_router):
    """A token the vault never minted is passed through verbatim, not resolved."""
    seen = pii_router
    forged = PII_TOKEN_PREFIX + "0" * 32

    _call(
        "invoke_tool",
        {"name": "update_visitor", "arguments": {"visitor_id": "v-9", "email": forged}},
    )

    assert seen["email"] == forged


# ---------------------------------------------------------------------------
# Real JSON-RPC protocol boundary
# ---------------------------------------------------------------------------
def test_pii_round_trip_over_real_protocol_boundary(pii_router):
    """Same round trip through a real Client/server JSON-RPC session."""
    seen = pii_router

    async def _run() -> tuple[dict[str, Any], dict[str, Any]]:
        async with Client(router.mcp) as client:
            read = await client.call_tool(
                "invoke_read_tool", {"name": "list_visitors", "arguments": {}}
            )
            read_payload = json.loads(read.content[0].text)
            token = read_payload["items"][0]["email"]
            write = await client.call_tool(
                "invoke_tool",
                {
                    "name": "update_visitor",
                    "arguments": {"visitor_id": "v-1", "email": token},
                },
            )
            return read_payload, json.loads(write.content[0].text)

    read_payload, write_payload = asyncio.run(_run())

    serialized = json.dumps(read_payload)
    for plaintext in (VISITOR_EMAIL, VISITOR_PHONE, SPONSOR_EMAIL, COMPANY_NAME):
        assert plaintext not in serialized, f"{plaintext} crossed the protocol boundary"
    assert read_payload["items"][0]["email"].startswith(PII_TOKEN_PREFIX)

    # The token minted in this session resolved on the subsequent write.
    assert seen["email"] == VISITOR_EMAIL
    assert write_payload["status"] == "updated"
