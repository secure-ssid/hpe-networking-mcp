"""Middleware installer for ``mcp.server.mcpserver`` tool managers.

Monkey-patches ``ToolManager.call_tool`` to run each installed
middleware's ``before_call`` and ``after_call`` hooks. This is a
deliberate trade: the shim has no middleware API, we don't want a new
dependency, and patching a single method keeps blast radius small.

Note on async: ``ToolManager.call_tool`` returns a coroutine, so the
installer wraps it as ``async def`` and awaits middleware hooks that return
awaitables. Synchronous hooks remain supported for simple mutations.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Protocol

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.func_metadata import _convert_to_content

logger = logging.getLogger(__name__)


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


async def _run_before(middlewares, name, args, context=None):
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


async def _run_after(middlewares, name, args, result, context=None):
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


async def _run_on_error(middlewares, name, args, exc, context=None):
    for mw in middlewares:
        try:
            handler = getattr(mw, "on_error", None)
            if handler is None:
                continue
            kwargs = {"context": context} if _accepts_context(handler) else {}
            substitute = await _maybe_await(handler(name, args, exc, **kwargs))
            if substitute is not None:
                return substitute
        except Exception as handler_exc:
            logger.warning(
                "middleware %s.on_error failed: %s", type(mw).__name__, handler_exc
            )
    return None


def install_middleware(server: MCPServer, middlewares: list[Middleware]) -> None:
    """Install ``middlewares`` on ``server``'s tool manager.

    Idempotent — a server that already has middleware installed will have
    its chain *replaced* rather than stacked, so re-imports in tests
    don't accumulate.
    """
    tm = server._tool_manager
    # Preserve the *real* original so re-install doesn't stack.
    original = getattr(tm, _INSTALLED_ATTR, None) or tm.call_tool
    if not getattr(tm, _INSTALLED_ATTR, None):
        setattr(tm, _INSTALLED_ATTR, original)

    async def wrapped_call_tool(name, arguments, context=None, convert_result=False):  # noqa: ANN001,ANN202
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
                    tool = tm.get_tool(name)
                    if tool is not None:
                        try:
                            return tool.fn_metadata.convert_result(substitute)
                        except Exception:
                            return _convert_to_content(substitute)
                return substitute
            raise

        result = await _run_after(middlewares, name, args, raw_result, context=context)
        if convert_result:
            tool = tm.get_tool(name)
            if tool is not None:
                try:
                    return tool.fn_metadata.convert_result(result)
                except Exception:
                    if result is not raw_result:
                        return _convert_to_content(result)
                    raise
        return result

    tm.call_tool = wrapped_call_tool  # type: ignore[method-assign]
