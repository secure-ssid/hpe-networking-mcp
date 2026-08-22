"""Middleware installer for ``mcp.server.mcpserver`` tool dispatch.

Intercepts the server's tool dispatcher (through
``hpe_networking_mcp.mcp_servers._sdk_compat``, the one module allowed to touch
the SDK's private tool manager) to run each installed middleware's
``before_call`` and ``after_call`` hooks. This is a deliberate trade: the SDK's
own ``ServerMiddleware`` chain is inbound-wire-message tier and never sees the
in-process calls this package makes, we don't want a new dependency, and
replacing a single dispatcher keeps blast radius small.

Note on async: the dispatcher is a coroutine function, so the installer wraps it
as ``async def`` and awaits middleware hooks that return awaitables.
Synchronous hooks remain supported for simple mutations.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Protocol

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.func_metadata import _convert_to_content
from mcp.types import CallToolResult

from hpe_networking_mcp.mcp_servers import _sdk_compat

logger = logging.getLogger(__name__)


def _wire_result(server: MCPServer, name: str, value: Any) -> Any:
    """Convert a tool result for the wire, never returning a bare content list.

    ``fn_metadata.convert_result`` raises when the value no longer matches the
    tool's output schema -- exactly what an enveloped error does to a
    wrap-output tool (``-> list[...]``). Falling back to ``_convert_to_content``
    alone returns ``list[ContentBlock]``, which the SDK's wire sieve rejects
    with INTERNAL_ERROR "Handler returned an invalid result". The fallback
    therefore wraps the content in a real ``CallToolResult`` so every error
    path reachable through tool dispatch stays schema-valid. ``is_error`` is
    set: the value by definition does not conform to the tool's advertised
    output schema, and the flag is what exempts the result from client-side
    structured-content validation (mcp.client.session only revalidates
    non-error results).
    """
    tool = _sdk_compat.get_tool(server, name)
    if tool is not None:
        try:
            return tool.fn_metadata.convert_result(value)
        except Exception:
            pass
    return CallToolResult(content=_convert_to_content(value), is_error=True)


class Middleware(Protocol):
    """Minimal middleware surface.

    Either hook may mutate ``arguments`` / return a new ``result``. If a
    hook raises, the error is logged and swallowed so a broken middleware
    can't crash the server (fail-open).
    """

    def before_call(self, name: str, arguments: dict[str, Any]) -> Any:
        """Mutate / replace call arguments. Return ``None`` to leave args unchanged."""
        ...

    def after_call(self, name: str, arguments: dict[str, Any], result: Any) -> Any:
        """Mutate / replace the tool result. Return ``None`` to leave result unchanged."""
        ...

    def on_error(self, name: str, arguments: dict[str, Any], exc: BaseException) -> Any:
        """Called when the wrapped tool raises. Return a value to swallow + substitute.
        Return ``None`` (default) to let the exception propagate."""
        ...


_INSTALLED_ATTR = "_hpe_mcp_middleware_original"


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _accepts_context(fn: Any) -> bool:
    """True if ``fn`` declares a ``context`` parameter.

    Checked once per hook call (cheap: ``inspect.signature`` on a bound
    method is not on any hot network path) rather than baked into the
    ``Middleware`` protocol, so existing middlewares with the original
    2/3-arg ``before_call``/``after_call``/``on_error`` signatures keep
    working unmodified -- only middlewares that explicitly opt in by
    declaring ``context`` receive it.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    return "context" in sig.parameters


async def _run_before(
    middlewares: list[Middleware],
    name: str,
    args: dict[str, Any],
    context: Any = None,
) -> dict[str, Any]:
    for mw in middlewares:
        try:
            before = getattr(mw, "before_call", None)
            if before is not None:
                kwargs = {"context": context} if _accepts_context(before) else {}
                new_args = await _maybe_await(before(name, args, **kwargs))
                if new_args is not None:
                    args = new_args
        except Exception as exc:
            logger.warning("middleware %s.before_call failed: %s", type(mw).__name__, exc)
    return args


async def _run_after(
    middlewares: list[Middleware],
    name: str,
    args: dict[str, Any],
    result: Any,
    context: Any = None,
) -> Any:
    for mw in middlewares:
        try:
            after = getattr(mw, "after_call", None)
            if after is not None:
                kwargs = {"context": context} if _accepts_context(after) else {}
                new_result = await _maybe_await(after(name, args, result, **kwargs))
                if new_result is not None:
                    result = new_result
        except Exception as exc:
            logger.warning("middleware %s.after_call failed: %s", type(mw).__name__, exc)
    return result


async def _run_on_error(
    middlewares: list[Middleware],
    name: str,
    args: dict[str, Any],
    exc: BaseException,
    context: Any = None,
) -> Any:
    substitute = None
    for mw in middlewares:
        try:
            handler = getattr(mw, "on_error", None)
            if handler is None:
                continue
            kwargs = {"context": context} if _accepts_context(handler) else {}
            # Every hook runs even after an earlier one substituted a result:
            # side-effect hooks (audit log, metrics) must still observe the
            # exception. The FIRST non-None substitute wins.
            offered = await _maybe_await(handler(name, args, exc, **kwargs))
            if substitute is None and offered is not None:
                substitute = offered
        except Exception as handler_exc:
            logger.warning(
                "middleware %s.on_error failed: %s", type(mw).__name__, handler_exc
            )
    return substitute


def install_middleware(server: MCPServer, middlewares: list[Middleware]) -> None:
    """Install ``middlewares`` on ``server``'s tool dispatcher.

    Call this **once per server**, and before anything else wraps the
    dispatcher. Re-installing while this chain is still the outermost wrapper
    replaces it rather than stacking, so repeated imports in tests do not
    accumulate -- but re-installing after something else has wrapped (notably
    ``shared.install_platform_write_gate``, which every backend gets from
    ``shared.run_server``) raises ``RuntimeError`` instead of silently
    rebuilding from a stale snapshot and dropping that interceptor.
    """
    original = _sdk_compat.claim_dispatcher(server, _INSTALLED_ATTR)

    async def wrapped_call_tool(
        name: str,
        arguments: dict[str, Any] | None,
        context: Any = None,
        convert_result: bool = False,
    ) -> Any:
        args = dict(arguments) if arguments else {}
        args = await _run_before(middlewares, name, args, context=context)

        try:
            raw_result = await original(name, args, context=context, convert_result=False)
        except BaseException as exc:
            substitute = await _run_on_error(middlewares, name, args, exc, context=context)
            if substitute is not None:
                # Route the substitute through after_call too, so an on_error
                # result (e.g. UnknownToolSuggestMiddleware's hint dict) gets
                # the same envelope/secret-tokenize treatment as any other
                # failure/blocked result instead of escaping in a bespoke shape.
                substitute = await _run_after(
                    middlewares, name, args, substitute, context=context
                )
                if convert_result:
                    converted = _wire_result(server, name, substitute)
                    # A substitute always replaces a raised exception, so the
                    # wire result is an error by definition -- keep the
                    # protocol-level isError contract intact.
                    if isinstance(converted, CallToolResult):
                        converted.is_error = True
                    return converted
                return substitute
            raise

        result = await _run_after(middlewares, name, args, raw_result, context=context)
        if convert_result:
            if result is raw_result:
                # Middleware left the result untouched: convert exactly as the
                # SDK would, so a conversion failure surfaces through the SDK's
                # own error path rather than being masked here.
                tool = _sdk_compat.get_tool(server, name)
                if tool is not None:
                    return tool.fn_metadata.convert_result(result)
            return _wire_result(server, name, result)
        return result

    _sdk_compat.set_dispatcher(server, wrapped_call_tool, _INSTALLED_ATTR)


def uninstall_middleware(server: MCPServer) -> bool:
    """Remove this module's middleware chain from ``server``, restoring what it wrapped.

    Returns ``True`` if a chain was removed. Intended for callers that install a
    chain onto a server they do not exclusively own -- notably tests wiring the
    module-level router -- so the next install starts from a clean seam instead
    of inheriting a half-restored one.
    """
    return _sdk_compat.release_dispatcher(server, _INSTALLED_ATTR)
