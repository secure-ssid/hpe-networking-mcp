"""Real protocol-boundary MCP E2E smoke tests (SDK v2.x).

Every other test in this suite drives tools through
``MCPServer._tool_manager.call_tool(...)`` directly -- useful and fast, but
it never serializes a request/response across the actual JSON-RPC
boundary a real MCP client uses. These tests do: they build a real
``Client`` wired to a real server via the SDK's in-memory transport, so
``list_tools``/``call_tool`` go through the SDK's real request/response
(de)serialization, not just a Python function call.

Deliberately does NOT import ``hpe_networking_mcp.mcp_servers.tool_router`` (that pulls in an
embedding model / lance / redis backend selection at import time) -- a
small standalone MCPServer exercising the same shapes the router's
tools produce (read tool, blocked-write envelope, raised-exception error,
elicitation) is enough to prove the protocol boundary itself works with
this repo's real middleware chain installed.

This repo has no pytest-asyncio/anyio pytest plugin installed (see other
async tests in this suite), so each test wraps its async body in
``asyncio.run(...)`` rather than using an ``@pytest.mark.anyio`` marker.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from mcp.client import Client
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ElicitResult
from pydantic import BaseModel

from hpe_networking_mcp.mcp_servers._middleware import (
    NullStripMiddleware,
    ResponseEnvelopeMiddleware,
    install_middleware,
)
from hpe_networking_mcp.mcp_servers.shared import DESTRUCTIVE, READ_ONLY, enforce_platform_write

REPO_ROOT = Path(__file__).resolve().parents[2]


def _build_server() -> MCPServer:
    srv = MCPServer("e2e-smoke-server")

    @srv.tool(annotations=READ_ONLY)
    def list_devices(limit: int = 10) -> dict:
        """List devices (read-only)."""
        return {"items": [{"serial": "CN1"}, {"serial": "CN2"}][:limit]}

    @srv.tool(annotations=DESTRUCTIVE)
    def reboot_device(serial_number: str) -> dict:
        """Reboot a device -- gated by the real per-platform write helper."""
        blocked = enforce_platform_write("mist", "reboot_device")
        if blocked:
            return blocked
        return {"status": "rebooting", "serial_number": serial_number}

    @srv.tool()
    def boom() -> dict:
        """Always raises -- protocol-boundary error-envelope path."""
        raise RuntimeError("simulated tool failure")

    class ConfirmSchema(BaseModel):
        confirm: bool

    @srv.tool()
    async def confirm_reboot(ctx: Context, serial_number: str) -> dict:
        """Elicits a yes/no confirmation before "rebooting"."""
        result = await ctx.elicit(
            message=f"Reboot {serial_number}?", schema=ConfirmSchema
        )
        if result.action != "accept" or not result.data or not result.data.confirm:
            return {"status": "cancelled", "serial_number": serial_number}
        return {"status": "rebooting", "serial_number": serial_number}

    install_middleware(
        srv, [NullStripMiddleware(), ResponseEnvelopeMiddleware()]
    )
    return srv


def _text_payload(call_result) -> dict:
    """Extract and JSON-parse the first TextContent block of a CallToolResult."""
    assert call_result.content, "expected at least one content block"
    block = call_result.content[0]
    return json.loads(block.text)


def test_initialize_and_list_tools_over_real_protocol_boundary():
    async def _run():
        server = _build_server()
        async with Client(server) as client:
            return await client.list_tools()

    result = asyncio.run(_run())

    names = {tool.name for tool in result.tools}
    assert {"list_devices", "reboot_device", "boom", "confirm_reboot"} <= names

    # Annotations survive the wire round trip.
    by_name = {tool.name: tool for tool in result.tools}
    assert by_name["list_devices"].annotations.read_only_hint is True
    assert by_name["reboot_device"].annotations.destructive_hint is True


def test_minimal_router_discovery_contract_over_real_protocol_boundary():
    script = r'''
import asyncio
import json
import os

os.environ["HPE_MCP_RAG_BACKEND"] = "lancedb"
os.environ["HPE_MCP_ROUTER_MODE"] = "minimal"
os.environ["HPE_MCP_CENTRAL_WRITES"] = "1"

from mcp.server.mcpserver import MCPServer
from mcp.client import Client

import hpe_networking_mcp.mcp_servers.tool_router as router
from hpe_networking_mcp.mcp_servers.shared import IDEMPOTENT_WRITE

backend = MCPServer("protocol-discovery-backend")

@backend.tool(annotations=router.READ_ONLY)
def list_widgets(limit: int = 10) -> dict:
    return {"limit": limit}

@backend.tool(annotations=IDEMPOTENT_WRITE)
def update_widget(
    widget_id: str,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict:
    return {
        "widget_id": widget_id,
        "dry_run": dry_run,
        "confirm": confirm,
    }

tools = dict(backend._tool_manager._tools)
router._BACKENDS = {
    "central-monitoring": "demo.monitoring",
    "central-config": "demo.config",
}
router._tool_index = tools
router._tool_servers = {name: backend for name in tools}
router._tool_backend_names = {
    "list_widgets": "central-monitoring",
    "update_widget": "central-config",
}
router._load_all_backends = lambda: None
router._embedder.embed_query = lambda query: [0.0]
router._lance.connect = lambda: object()
router._lance.search_tools = lambda db, query, vec, top_k: []

async def main():
    async with Client(router.mcp) as client:
        listed = await client.list_tools()
        by_name = {tool.name: tool for tool in listed.tools}
        called = await client.call_tool(
            "find_tool",
            {
                "query": "widgets",
                "top_k": 1,
                "platform": "central",
                "server": "central-monitoring",
                "capability": "read",
            },
        )
        write_discovery = await client.call_tool(
            "find_tool",
            {
                "query": "update widget",
                "top_k": 1,
                "capability": "write",
            },
        )
        preview = await client.call_tool(
            "invoke_tool",
            {
                "name": "update_widget",
                "arguments": {"widget_id": "w1"},
            },
        )
        print(json.dumps({
            "names": sorted(by_name),
            "find_tool_properties": sorted(
                by_name["find_tool"].input_schema["properties"]
            ),
            "result": json.loads(called.content[0].text),
            "write_discovery": json.loads(write_discovery.content[0].text),
            "preview": json.loads(preview.content[0].text),
        }))

asyncio.run(main())
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout.strip().splitlines()[-1])

    assert payload["names"] == ["find_tool", "invoke_read_tool", "invoke_tool"]
    assert {
        "query",
        "top_k",
        "include_schema",
        "platform",
        "server",
        "capability",
    } <= set(payload["find_tool_properties"])
    result = payload["result"].get("result", payload["result"])
    if isinstance(result, dict):
        result = [result]
    assert result[0]["name"] == "list_widgets"
    assert result[0]["platform"] == "central"
    assert result[0]["capability"] == "read"
    assert result[0]["recommended_dispatcher"] == "invoke_read_tool"
    assert result[0]["currently_enabled"] is True
    write_result = payload["write_discovery"].get(
        "result", payload["write_discovery"]
    )
    if isinstance(write_result, dict):
        write_result = [write_result]
    write_contract = write_result[0]["execution_contract"]
    assert write_contract["capability"] == "write"
    assert write_contract["gate"]["state"] == "enabled"
    assert write_contract["dry_run"]["state"] == "default_preview"
    preview = payload["preview"].get("result", payload["preview"])
    assert preview["widget_id"] == "w1"
    assert preview["dry_run"] is True
    assert preview["execution_contract"]["dry_run"]["state"] == "preview"
    assert preview["execution_contract"]["idempotent"] is True


def test_read_tool_call_round_trips_real_json_rpc():
    async def _run():
        server = _build_server()
        async with Client(server) as client:
            return await client.call_tool("list_devices", {"limit": 1})

    result = asyncio.run(_run())

    assert result.is_error is False
    payload = _text_payload(result)
    assert payload == {"items": [{"serial": "CN1"}]}


def test_null_strip_middleware_runs_across_the_protocol_boundary():
    """A real client sends ``{"limit": None}`` (JSON null) for "use the
    default" -- NullStripMiddleware must strip it before Pydantic validation
    sees it, exactly as it does for router calls in production."""

    async def _run():
        server = _build_server()
        async with Client(server) as client:
            return await client.call_tool("list_devices", {"limit": None})

    result = asyncio.run(_run())

    assert result.is_error is False
    payload = _text_payload(result)
    assert payload == {"items": [{"serial": "CN1"}, {"serial": "CN2"}]}


def test_blocked_write_envelope_survives_the_wire(monkeypatch):
    """enforce_platform_write's blocked dict must reach the client as the
    ResponseEnvelopeMiddleware {ok, status, data, message, tool} shape --
    proving the whole middleware chain, not just the raw tool function,
    runs on a real protocol-boundary call."""
    monkeypatch.setenv("HPE_MCP_MIST_WRITES", "0")  # explicit disable — .env sets PRODUCT_ACCESS=read-write as fallback

    async def _run():
        server = _build_server()
        async with Client(server) as client:
            return await client.call_tool(
                "reboot_device", {"serial_number": "CN1"}
            )

    result = asyncio.run(_run())

    assert result.is_error is False  # envelope, not a transport-level error
    payload = _text_payload(result)
    assert payload["ok"] is False
    assert payload["status"] == 403  # write-gate policy refusal
    assert payload["data"]["status"] == "blocked"
    assert payload["tool"] == "reboot_device"
    assert "HPE_MCP_MIST_WRITES" in payload["message"]
    contract = payload["data"]["execution_contract"]
    assert contract["platform"] == "mist"
    assert contract["capability"] == "write"
    assert contract["gate"]["state"] == "disabled"
    assert contract["next_action"].startswith("Set HPE_MCP_MIST_WRITES=1")


def test_allowed_write_executes_over_the_wire(monkeypatch):
    monkeypatch.setenv("HPE_MCP_MIST_WRITES", "1")

    async def _run():
        server = _build_server()
        async with Client(server) as client:
            return await client.call_tool(
                "reboot_device", {"serial_number": "CN1"}
            )

    result = asyncio.run(_run())

    assert result.is_error is False
    payload = _text_payload(result)
    assert payload == {"status": "rebooting", "serial_number": "CN1"}


def test_raised_exception_becomes_structured_protocol_error():
    async def _run():
        server = _build_server()
        async with Client(server) as client:
            return await client.call_tool("boom", {})

    result = asyncio.run(_run())

    assert result.is_error is True
    assert "simulated tool failure" in result.content[0].text


def test_unknown_tool_call_returns_a_protocol_error():
    async def _run():
        server = _build_server()
        async with Client(server) as client:
            return await client.call_tool("does_not_exist", {})

    result = asyncio.run(_run())

    assert result.is_error is True


def test_elicitation_accept_flow_over_real_protocol_boundary():
    """Drives ctx.elicit() through a real elicitation_callback on the
    client session -- the actual SDK request/response round trip for a
    server-initiated elicitation, not a mocked ctx.elicit()."""

    async def auto_accept(context, params):
        return ElicitResult(action="accept", content={"confirm": True})

    async def _run():
        server = _build_server()
        async with Client(
            server, mode="legacy", elicitation_callback=auto_accept
        ) as client:
            return await client.call_tool(
                "confirm_reboot", {"serial_number": "CN1"}
            )

    result = asyncio.run(_run())

    assert result.is_error is False
    payload = _text_payload(result)
    assert payload == {"status": "rebooting", "serial_number": "CN1"}


def test_elicitation_decline_flow_over_real_protocol_boundary():
    async def auto_decline(context, params):
        return ElicitResult(action="decline")

    async def _run():
        server = _build_server()
        async with Client(
            server, mode="legacy", elicitation_callback=auto_decline
        ) as client:
            return await client.call_tool(
                "confirm_reboot", {"serial_number": "CN1"}
            )

    result = asyncio.run(_run())

    assert result.is_error is False
    payload = _text_payload(result)
    assert payload["ok"] is False
    assert payload["status"] == 409  # ResponseEnvelopeMiddleware's "cancelled" mapping
    assert payload["data"] == {"status": "cancelled", "serial_number": "CN1"}
