"""Regression: standalone-backend raised exceptions must be redacted (HX-3).

``ResponseEnvelopeMiddleware.on_error`` stringifies a raised backend tool
exception for the first (and only) time on the standalone (non-router) path:

    self.after_call(name, arguments, {"error": f"{type(exc).__name__}: {exc}"})

A bearer credential embedded in that string -- e.g. via the SDK framing
``ToolError("Error executing tool <name>: <message>")``, or an httpx
request-URL message -- reached the client-visible envelope verbatim. This
file pins that the error message is masked before the envelope is built,
ordinary errors pass through untouched, and the success path is unaffected.

Mirrors ``tests/unit/test_router_error_redaction.py`` (the HX-1 router-path
fix) but for the standalone-backend path this middleware owns.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp.server.mcpserver import MCPServer

from hpe_networking_mcp.mcp_servers._middleware import (
    NullStripMiddleware,
    ResponseEnvelopeMiddleware,
    install_middleware,
)

_RAW_BEARER = "Bearer sk-abc1234567890-raise-path-secret-value"


def _call_raw(server: MCPServer, name: str, arguments: dict):
    return asyncio.run(
        server._tool_manager.call_tool(name, arguments, None, convert_result=False)
    )


class TestEnvelopeOnError:
    def test_raised_bearer_credential_is_redacted_in_envelope(self):
        mw = ResponseEnvelopeMiddleware()

        envelope = mw.on_error("read_thing", {}, RuntimeError(_RAW_BEARER))

        text = repr(envelope)
        assert _RAW_BEARER not in text
        assert "sk-abc1234567890" not in text
        # The redacted marker is present and the failure stays structured.
        assert "******" in text
        assert envelope["ok"] is False
        assert envelope["status"] == 500

    def test_ordinary_raise_text_passes_through_unchanged(self):
        mw = ResponseEnvelopeMiddleware()

        envelope = mw.on_error("read_thing", {}, RuntimeError("kaboom"))

        text = repr(envelope)
        assert "kaboom" in text
        assert "******" not in text
        assert envelope["ok"] is False

    def test_httpx_status_in_message_is_still_recovered(self):
        """Redaction must not break the existing httpx status extraction."""
        mw = ResponseEnvelopeMiddleware()

        envelope = mw.on_error(
            "read_thing",
            {},
            RuntimeError("Server error '503 Service Unavailable' for url 'https://x'"),
        )

        assert envelope["status"] == 503

    @pytest.mark.parametrize(
        "prose",
        [
            "401 Unauthorized: Bearer token missing or expired",
            'WWW-Authenticate: Bearer realm="api", error="invalid_token"',
        ],
    )
    def test_bearer_prose_survives_unchanged(self, prose):
        """The credential mask must NOT over-mask ordinary 401 prose that
        merely contains the word ``Bearer`` (Sentinel amendment, PR #41):
        real platform credentials are >=16 chars, so an 8+ char minimum
        preserves masking while letting short bearer prose pass through."""
        from hpe_networking_mcp.mcp_servers._middleware.response_envelope import (
            _redact_envelope_error,
        )

        masked = _redact_envelope_error(prose)
        assert masked == prose
        assert "******" not in masked


class TestStandaloneBackendChain:
    def test_raised_bearer_credential_is_redacted_via_installed_chain(self):
        """End-to-end: a standalone backend whose tool raises a bearer-carrying
        exception dispatches as a masked envelope, not a raw leak."""
        server = MCPServer("standalone-redaction")

        @server.tool()
        def bearer_secret() -> dict:
            """Always raises a bearer-carrying exception."""
            raise RuntimeError(_RAW_BEARER)

        install_middleware(server, [NullStripMiddleware(), ResponseEnvelopeMiddleware()])

        result = _call_raw(server, "bearer_secret", {})

        text = repr(result)
        assert _RAW_BEARER not in text
        assert "sk-abc1234567890" not in text
        assert result["ok"] is False
        assert result["status"] == 500