"""Every MCP tool-dispatch error path must yield a schema-valid CallToolResult.

Regression coverage for the streamable-HTTP failure where an enveloped error
(a 5xx-class ``{ok: false, ...}`` dict from ResponseEnvelopeMiddleware, hint
included) reached the SDK's wire sieve as a bare ``list[ContentBlock]`` -- not
a ``CallToolResult`` -- and was rejected with INTERNAL_ERROR "Handler returned
an invalid result" (mcp/server/runner.py). The trigger shape: a tool whose
output schema wraps its return value (``wrap_output`` -- e.g. ``-> list[...]``)
returning an error dict at runtime; ``fn_metadata.convert_result`` then raises
on the envelope and the middleware installer's fallback returned the raw
content list.
"""

from __future__ import annotations

import asyncio
import socket

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult

from hpe_networking_mcp.mcp_servers._middleware import (
    NullStripMiddleware,
    ResponseEnvelopeMiddleware,
    install_middleware,
)


def _build_envelope_server(name: str = "envelope-wire-shape") -> MCPServer:
    """A server with one wrap-output tool that fails at runtime.

    ``-> list[str]`` gives the tool an output schema with ``wrap_output``;
    returning an error dict instead drives the envelope -> convert-raises ->
    fallback path that used to escape as a bare content list.
    """
    server = MCPServer(name)

    @server.tool()
    def failing_list_tool() -> list[str]:
        """Always fails with a 5xx-class error dict."""
        return {  # type: ignore[return-value]
            "error": "Server error '503 Service Unavailable' for url 'https://api.example/x'",
            "status": "error",
        }

    install_middleware(server, [NullStripMiddleware(), ResponseEnvelopeMiddleware()])
    return server


def test_enveloped_error_on_wrap_output_tool_converts_to_call_tool_result():
    server = _build_envelope_server()

    result = asyncio.run(
        server._tool_manager.call_tool(
            "failing_list_tool", {}, None, convert_result=True
        )
    )

    assert isinstance(result, CallToolResult), (
        f"wire path must never see a bare {type(result).__name__}; "
        "the SDK runner rejects it as 'Handler returned an invalid result'"
    )
    assert result.content, "envelope payload must survive as content"


def test_enveloped_error_on_plain_tool_still_converts():
    server = MCPServer("envelope-plain")

    @server.tool()
    def failing_plain_tool() -> dict:
        """Always fails with a 5xx-class error dict."""
        return {"error": "boom", "status": "error"}

    install_middleware(server, [NullStripMiddleware(), ResponseEnvelopeMiddleware()])

    result = asyncio.run(
        server._tool_manager.call_tool(
            "failing_plain_tool", {}, None, convert_result=True
        )
    )

    assert isinstance(result, CallToolResult)
    assert result.content


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            _reader, writer = await asyncio.open_connection(host, port)
        except OSError:
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise TimeoutError(f"nothing listening on {host}:{port}")


def test_enveloped_error_survives_streamable_http_round_trip():
    """The exact OX-debugger repro shape: 5xx-class envelope over the wire."""
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    host = "127.0.0.1"
    port = _free_loopback_port()
    server = _build_envelope_server("envelope-wire-shape-http")
    app = server.streamable_http_app(host=host)

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    uv_server = uvicorn.Server(config)

    async def _run():
        serve_task = asyncio.create_task(uv_server.serve())
        try:
            await _wait_for_port(host, port)
            async with streamable_http_client(f"http://{host}:{port}/mcp") as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    # Must not raise MCPError(INTERNAL_ERROR, "Handler returned
                    # an invalid result") -- the result has to validate.
                    call = await session.call_tool("failing_list_tool", {})
                    assert call.is_error, "schema-breaking envelope must be flagged"
                    assert call.content, "envelope payload must reach the client"
                    assert "503" in call.content[0].text or "ok" in call.content[0].text
        finally:
            uv_server.should_exit = True
            await serve_task

    asyncio.run(_run())
