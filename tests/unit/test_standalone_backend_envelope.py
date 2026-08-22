"""A1: standalone backends emit the response envelope, including raised errors.

The router surface turns every backend failure -- returned error dicts AND
raised exceptions (``_dispatch_tool`` catches them into ``{"error": ...}``) --
into the ``{ok, status, data, message, tool, platform}`` envelope. Standalone
backends lacked ``ResponseEnvelopeMiddleware`` entirely, so the same failure
reached a client as a bare error dict or an SDK ``ToolError`` depending on how
the server was started. These tests pin the consistent shape.

They also pin a subtle installer property the envelope's ``on_error``
substitution depends on: a substituting ``on_error`` hook must not prevent
*later* middlewares' ``on_error`` hooks (audit log, metrics) from observing
the exception.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult

from hpe_networking_mcp.mcp_servers._middleware import (
    NullStripMiddleware,
    ResponseEnvelopeMiddleware,
    install_middleware,
)

#: Backends whose standalone chain must include the envelope. tool_router and
#: interop already install it; everything else is what A1 covers.
STANDALONE_BACKENDS = [
    "aos8",
    "apstra",
    "axis",
    "central_generated",
    "central_streaming",
    "clearpass",
    "config",
    "edgeconnect",
    "glp",
    "mist",
    "monitoring",
    "nac",
    "ops",
    "rag",
    "site_health",
    "uxi",
]


def _call_raw(server: MCPServer, name: str, arguments: dict):
    return asyncio.run(
        server._tool_manager.call_tool(name, arguments, None, convert_result=False)
    )


def _call_wire(server: MCPServer, name: str, arguments: dict):
    return asyncio.run(
        server._tool_manager.call_tool(name, arguments, None, convert_result=True)
    )


class TestRunOnErrorNotifiesAllMiddlewares:
    def test_substitute_does_not_skip_later_on_error_hooks(self):
        seen: list[str] = []

        class Substituter:
            def on_error(self, name, arguments, exc):
                return {"error": "handled"}

        class Recorder:
            def on_error(self, name, arguments, exc):
                seen.append(type(exc).__name__)
                return None

        server = MCPServer("on-error-fanout")

        @server.tool()
        def boom() -> str:
            """Always raises."""
            raise RuntimeError("kaboom")

        install_middleware(server, [Substituter(), Recorder()])

        result = _call_raw(server, "boom", {})

        assert result == {"error": "handled"}
        # The SDK wraps tool-body exceptions into ToolError before the
        # dispatcher (and therefore middleware) sees them.
        assert seen == ["ToolError"], (
            "audit/metrics-style on_error hooks after a substituting hook "
            "must still observe the exception"
        )


class TestEnvelopeOnError:
    def test_ordinary_exception_becomes_envelope(self):
        mw = ResponseEnvelopeMiddleware()

        envelope = mw.on_error("read_thing", {}, RuntimeError("kaboom"))

        assert envelope["ok"] is False
        assert envelope["status"] == 500
        assert envelope["tool"] == "read_thing"
        assert "kaboom" in envelope["message"]

    def test_httpx_status_in_message_is_recovered(self):
        mw = ResponseEnvelopeMiddleware()

        envelope = mw.on_error(
            "read_thing",
            {},
            RuntimeError("Server error '503 Service Unavailable' for url 'https://x'"),
        )

        assert envelope["status"] == 503

    @pytest.mark.parametrize(
        "exc",
        [
            MCPError(code=-32600, message="protocol"),
            asyncio.CancelledError(),
            KeyboardInterrupt(),
            SystemExit(1),
        ],
    )
    def test_protocol_and_control_exceptions_propagate(self, exc):
        """Elicitation/protocol errors and cancellation must never be swallowed."""
        mw = ResponseEnvelopeMiddleware()
        assert mw.on_error("read_thing", {}, exc) is None


class TestStandaloneBackendChain:
    def test_raised_error_dispatches_as_envelope(self):
        server = MCPServer("standalone-envelope")

        @server.tool()
        def boom() -> dict:
            """Always raises."""
            raise RuntimeError("kaboom")

        install_middleware(server, [NullStripMiddleware(), ResponseEnvelopeMiddleware()])

        result = _call_raw(server, "boom", {})

        assert isinstance(result, dict)
        assert result["ok"] is False
        assert result["status"] == 500
        assert result["tool"] == "boom"

    def test_raised_error_wire_result_is_valid_call_tool_result(self):
        server = MCPServer("standalone-envelope-wire")

        @server.tool()
        def boom() -> dict:
            """Always raises."""
            raise RuntimeError("kaboom")

        install_middleware(server, [NullStripMiddleware(), ResponseEnvelopeMiddleware()])

        result = _call_wire(server, "boom", {})

        assert isinstance(result, CallToolResult)
        assert "kaboom" in result.content[0].text

    @pytest.mark.parametrize("backend", STANDALONE_BACKENDS)
    def test_backend_main_installs_response_envelope(self, backend, monkeypatch):
        """Each standalone backend's __main__ chain must include the envelope.

        Backends install their chain in an ``if __name__ == "__main__":``
        block, so this executes the module fresh under that name with the
        installer and ``run_server`` stubbed, capturing the chain it built.
        """
        import runpy
        import warnings

        import hpe_networking_mcp.mcp_servers._middleware as middleware_mod
        import hpe_networking_mcp.mcp_servers.shared as shared_mod

        captured: list[list] = []

        def capturing_install(server, middlewares):
            captured.append(list(middlewares))
            # Do NOT actually install: a real install would wrap dispatchers
            # other tests rely on.
            return None

        monkeypatch.setattr(middleware_mod, "install_middleware", capturing_install)
        monkeypatch.setattr(shared_mod, "run_server", lambda *a, **k: None)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            runpy.run_module(
                f"hpe_networking_mcp.mcp_servers.{backend}", run_name="__main__"
            )

        assert captured, f"{backend} __main__ never installed middleware"
        chain = captured[0]
        assert any(isinstance(mw, ResponseEnvelopeMiddleware) for mw in chain), (
            f"{backend} standalone chain lacks ResponseEnvelopeMiddleware: "
            f"{[type(mw).__name__ for mw in chain]}"
        )
