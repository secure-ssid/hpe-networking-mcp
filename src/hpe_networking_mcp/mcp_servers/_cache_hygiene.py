"""Cache-hygiene helpers for MCP servers.

Tool definitions are part of the top of the Anthropic prompt-cache hierarchy
(tools → system → messages). Any churn in the serialized tool block invalidates
every downstream cache. Two protections live here:

1. `stable_list_tools(server)` — makes the server's `list_tools()` return tools
   sorted by name. Source reordering in a server file no longer cascades into a
   cache bust for clients.

2. (Pydantic already emits schemas in source order of function args, and dict
   insertion order is stable in CPython ≥3.7, so schema-body sorting is not
   currently needed. If the MCP SDK or Pydantic changes this, revisit here.)

See: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from hpe_networking_mcp.mcp_servers import _sdk_compat


def stable_list_tools(server: MCPServer) -> None:
    """Make `server`'s tool listing deterministic (alphabetical by tool name).

    Call this in each MCP server module after registering tools. It replaces
    the tool listing with a sorted variant so two processes with the same
    registered tools always emit the same byte-for-byte tools block to the
    client.

    Idempotent: safe to call multiple times.
    """
    _sdk_compat.install_sorted_tool_listing(server)
