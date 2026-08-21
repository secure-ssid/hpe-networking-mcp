"""The single place allowed to touch ``MCPServer``'s private tool manager.

Everything the rest of this package needs from the MCP SDK's tool registry is
expressed here as a named, intent-revealing function. No other module may write
``._tool_manager``; ``tests/unit/test_no_private_sdk_access.py`` enforces that.

**Before bumping ``mcp``, read**
``tests/unit/test_no_private_sdk_access.py::test_sdk_compat_matches_the_installed_sdk``.
That test is the tripwire for this whole module: it pins every SDK internal
relied on below -- registry access, the internal-vs-wire attribute split,
verbatim ``Tool`` republication by object identity, raw dispatch returning a
Python value, and the claim/replace/restore interception seam including the
fact that the *public* ``MCPServer.call_tool`` still routes through it. An
upstream rename must fail there, as one loud and obvious test, rather than as a
silently missing write gate. If that test goes red on an SDK bump, do not
"fix" it by loosening the assertion -- re-derive the seam and re-run the
write-gate suites.

Why a quarantine rather than a migration
----------------------------------------
``mcp`` 2.x publishes ``MCPServer.list_tools()``, ``MCPServer.call_tool()``,
``add_tool()`` and ``remove_tool()``. Three capabilities this package depends on
have no public equivalent:

1. **Synchronous registry introspection.** ``list_tools()`` is a coroutine even
   though its body is synchronous. The router's backend indexer, the generated
   tool registrar, the unknown-tool suggester and the project-facts collector all
   run in synchronous code, some of it under a live event loop, so they cannot
   await it.
2. **Publishing a pre-built ``Tool``.** ``add_tool()`` only accepts a callable and
   re-derives the tool with ``Tool.from_function``, which rebuilds ``parameters``
   and ``fn_metadata`` and discards ``title``/``icons``/``meta``/``structured_output``.
   Direct router mode must republish the backend's *exact* ``Tool`` object so the
   schema it advertises stays byte-identical to the backend's (and so
   post-registration schema edits survive). ``MCPServer(tools=[...])`` takes
   ``Tool`` objects, but only at construction.
3. **Raw in-process dispatch.** ``MCPServer.call_tool()`` calls the manager with
   ``convert_result=True``, i.e. it is a *wire-serialization* API: a ``dict``
   return becomes pretty-printed ``TextContent`` with no ``structuredContent``, and
   a ``list`` return fans out into one ``TextContent`` per element. That is not
   losslessly reversible, and the router's response-budget engineering operates on
   the raw Python value. The manager's ``call_tool(..., convert_result=False)`` is
   the only faithful in-process dispatch entry.

Why the write gate intercepts *here* and not at ``ServerMiddleware``
--------------------------------------------------------------------
``MCPServer.middleware`` looks like the supported seam and is not one. It is
the low-level ``ServerMiddleware`` chain: *inbound-message* tier, wrapping
``tools/call`` requests that arrive over a transport. ``mcp.server.context.
ServerMiddleware`` exposes no tool-level hook at all, and the chain ships
holding only ``OpenTelemetryMiddleware`` and ``RequestStateBoundary`` -- both
wire-tier.

Measured, not assumed: registering a spy on ``MCPServer.middleware`` and then
calling ``await server.call_tool(name, args)`` in-process records **nothing**;
the spy's observed-method list comes back empty. That matters here because
backend servers in this deployment never receive a wire message -- the router
imports them as modules and dispatches in-process -- so a middleware-tier write
gate would observe **0%** of the traffic it exists to gate, silently deleting
the security boundary for every router dispatch and every direct by-name call.

Intercepting the tool manager's dispatcher is the only position covering all
three paths, which all converge on it: the SDK's wire handler (via
``MCPServer.call_tool``), a direct in-process ``server.call_tool(name, ...)``,
and the router's raw dispatch (:func:`call_tool_raw`). Maximal coverage beats
API purity for a security boundary. See ``shared.install_platform_write_gate``
and ``.superpowers/sdd/hpe-mcp-repo-a-improvement-plan/task-8-report.md`` §2.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.tools.base import Tool

__all__ = [
    "call_tool_raw",
    "claim_dispatcher",
    "get_tool",
    "install_sorted_tool_listing",
    "register_tool_object",
    "set_dispatcher",
    "tool_names",
    "tool_registry",
]

_SORTED_LISTING_ATTR = "_hpe_mcp_sorted_list_tools_applied"


def tool_registry(server: MCPServer) -> Mapping[str, Tool]:
    """The server's live ``{name: Tool}`` registry.

    The mapping is the manager's own dict, so it tracks later registrations --
    callers that need a snapshot must copy it. Typed ``Mapping`` because mutating
    it directly is not this module's contract: use :func:`register_tool_object`.
    """
    return server._tool_manager._tools


def tool_names(server: MCPServer) -> list[str]:
    """Registered tool names, sorted. The synchronous counterpart to ``list_tools()``."""
    return sorted(server._tool_manager._tools)


def get_tool(server: MCPServer, name: str) -> Tool | None:
    """The internal ``Tool`` object for ``name``, or ``None``.

    Returns the *internal* tool -- ``annotations``, ``parameters``, ``fn`` and
    ``fn_metadata`` -- not the ``MCPTool`` wire form that ``list_tools()`` builds.
    """
    return server._tool_manager.get_tool(name)


def register_tool_object(server: MCPServer, name: str, tool: Tool) -> None:
    """Publish an already-built ``Tool`` under ``name``, verbatim.

    Unlike ``MCPServer.add_tool``, this does not re-derive the tool from its
    function, so the advertised schema and metadata are preserved exactly.
    """
    server._tool_manager._tools[name] = tool


async def call_tool_raw(
    server: MCPServer,
    name: str,
    arguments: dict[str, Any],
    context: Any = None,
) -> Any:
    """Dispatch ``name`` in-process and return the tool's raw Python value.

    Goes through the manager's dispatcher, so any interception installed by
    :func:`set_dispatcher` -- notably the platform write gate -- applies.
    """
    return await server._tool_manager.call_tool(
        name, arguments, context, convert_result=False
    )


def _installed_attr(marker: str) -> str:
    return f"{marker}__wrapper"


def claim_dispatcher(server: MCPServer, marker: str) -> Callable[..., Awaitable[Any]]:
    """Return the dispatcher an interceptor registered under ``marker`` must call.

    The first claim under a given ``marker`` snapshots the dispatcher currently
    in place and remembers it on the manager, so two interceptors using
    different markers compose: each wraps whatever it found.

    A **re-claim is only safe while this marker's wrapper is still the outermost
    dispatcher.** Otherwise the remembered snapshot is stale -- something else
    has wrapped since -- and rebuilding from it would silently discard every
    interceptor installed in between. When one of those is the write gate, that
    is a security boundary disappearing with no error, no log and no failing
    test, so this refuses instead.

    Raises:
        RuntimeError: on a re-claim whose saved original is no longer current.
            Install order is fixed and one-shot per server; hitting this means a
            second install, which is the bug.
    """
    manager = server._tool_manager
    original = getattr(manager, marker, None)
    if original is None:
        original = manager.call_tool
        setattr(manager, marker, original)
        return original
    current = manager.call_tool
    installed = getattr(manager, _installed_attr(marker), None)
    if (installed is not None and current is installed) or current == original:
        # Either this marker's own wrapper is still outermost, or nothing has
        # been installed since the claim. Both are safe to rebuild from: the
        # saved original is still current, so no interceptor can be dropped.
        return original
    raise RuntimeError(
        f"refusing to re-install {marker!r} on server {getattr(server, 'name', '?')!r}: "
        "another interceptor has wrapped the tool dispatcher since this one was "
        "installed, so rebuilding from the saved original would silently remove it "
        "(the platform write gate is one such interceptor). Install each interceptor "
        "exactly once, outermost last."
    )


def set_dispatcher(
    server: MCPServer,
    dispatcher: Callable[..., Awaitable[Any]],
    marker: str | None = None,
) -> None:
    """Install ``dispatcher`` as the server's tool-call entry point.

    It must accept ``(name, arguments, context=None, convert_result=False)`` --
    the manager's own signature -- because the SDK calls it positionally from
    ``MCPServer.call_tool``.

    Pass the same ``marker`` used for :func:`claim_dispatcher` so a later
    re-claim can tell whether this wrapper is still the outermost one.
    """
    server._tool_manager.call_tool = dispatcher  # type: ignore[method-assign]
    if marker is not None:
        setattr(server._tool_manager, _installed_attr(marker), dispatcher)


def release_dispatcher(server: MCPServer, marker: str) -> bool:
    """Uninstall the interceptor registered under ``marker``, restoring what it wrapped.

    The symmetric counterpart to :func:`claim_dispatcher`, and the only correct
    way to take an interceptor back off. Restoring by hand -- assigning
    ``call_tool`` back to some remembered value -- leaves this module's
    bookkeeping pointing at a wrapper that is no longer installed, and the next
    claim then cannot tell a safe refresh from a silent removal.

    Returns ``True`` if an interceptor was removed, ``False`` if none was
    installed. Raises ``RuntimeError`` if something else has wrapped the
    dispatcher since -- unwinding out of order would drop that one, which is the
    same hazard :func:`claim_dispatcher` refuses.
    """
    manager = server._tool_manager
    original = getattr(manager, marker, None)
    if original is None:
        return False
    installed = getattr(manager, _installed_attr(marker), None)
    if installed is not None and manager.call_tool is not installed:
        raise RuntimeError(
            f"refusing to release {marker!r} on server {getattr(server, 'name', '?')!r}: "
            "another interceptor was installed on top of it, so restoring the saved "
            "original would silently remove that one too. Release in reverse install "
            "order."
        )
    manager.call_tool = original  # type: ignore[method-assign]
    delattr(manager, marker)
    if installed is not None:
        delattr(manager, _installed_attr(marker))
    return True


def install_sorted_tool_listing(server: MCPServer) -> bool:
    """Make ``list_tools()`` return tools ordered by name. Idempotent.

    Returns ``True`` if this call installed the ordering, ``False`` if it was
    already in place.
    """
    manager = server._tool_manager
    if getattr(manager, _SORTED_LISTING_ATTR, False):
        return False
    original = manager.list_tools

    def sorted_list_tools() -> list[Tool]:
        return sorted(original(), key=lambda tool: tool.name)

    manager.list_tools = sorted_list_tools  # type: ignore[method-assign]
    setattr(manager, _SORTED_LISTING_ATTR, True)
    return True
