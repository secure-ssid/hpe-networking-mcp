"""Cache-hygiene helpers for MCP servers.

Tool definitions are part of the top of the Anthropic prompt-cache hierarchy
(tools → system → messages). Any churn in the serialized tool block invalidates
every downstream cache. Two protections live here:

1. `stable_list_tools(server)` — monkey-patches the server's tool manager so
   `list_tools()` returns tools sorted by name. Source reordering in a server
   file no longer cascades into a cache bust for clients.

2. (Pydantic already emits schemas in source order of function args, and dict
   insertion order is stable in CPython ≥3.7, so schema-body sorting is not
   currently needed. If the MCP SDK or Pydantic changes this, revisit here.)

See: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer


def stable_list_tools(server: MCPServer) -> None:
    """Make `server`'s tool listing deterministic (alphabetical by tool name).

    Call this in each MCP server module after registering tools. It replaces
    the tool manager's `list_tools` with a sorted variant so two processes
    with the same registered tools always emit the same byte-for-byte tools
    block to the client.

    Idempotent: safe to call multiple times.
    """
    tm = server._tool_manager
    if getattr(tm, "_sorted_list_tools_applied", False):
        return
    original = tm.list_tools

    def sorted_list_tools():  # noqa: ANN202 — matches original signature
        return sorted(original(), key=lambda t: t.name)

    tm.list_tools = sorted_list_tools  # type: ignore[assignment]
    tm._sorted_list_tools_applied = True  # type: ignore[attr-defined]
