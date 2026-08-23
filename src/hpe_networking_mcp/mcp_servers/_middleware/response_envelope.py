"""Response envelope middleware for error and blocked tool results.

This intentionally wraps only failures/blocked states. Successful payloads pass
through unchanged so existing clients and tests keep their expected shapes while
small models get a reliable `ok=false` signal when something did not happen.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any

from mcp.shared.exceptions import MCPError

from hpe_networking_mcp.mcp_servers.shared import redact_tool_error_text

_BLOCKED_STATUS_HTTP = {
    "blocked": 403,
    "cancelled": 409,
    "declined": 409,
    "confirmation_unavailable": 409,
    "confirmation_required": 409,
    "not_found": 404,
    "forbidden": 403,
    "error": 500,
    # Terminal failure status from a device operation the backend itself
    # reports as failed without raising (e.g. atroubleshoot_poll in shared.py
    # returning status="FAILED"). Without this the result carries no `error`
    # key and would escape as an un-enveloped implicit success.
    "failed": 500,
    "failure": 500,
    # Router-emitted caller-error statuses (tool_router dispatch helpers).
    # Without these they fall through to the generic 500 fallback, reporting a
    # caller mistake -- typo'd tool name, stale cursor, malformed batch entry --
    # as a retryable server fault.
    "unknown_tool": 404,
    "invalid_cursor": 400,
    "invalid_call": 400,
}


def _status_code(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


# httpx renders raised transport errors as:
#   "Client error '404 Not Found' for url 'https://...'"
#   "Server error '503 Service Unavailable' for url 'https://...'"
# Backends surface that string verbatim in `error`, so the real upstream code
# is recoverable. Anchoring on the httpx prefix keeps unrelated digits in a
# message (counts, IDs, ports) from being mistaken for a status.
_HTTPX_STATUS_RE = re.compile(r"\b(?:Client|Server) error '(\d{3})\b")


def _redact_envelope_error(text: str) -> str:
    """Mask credentials in a client-visible standalone-backend error string.

    ``on_error`` stringifies a raised backend exception for the first (and
    only) time here, so any credential in the message must be masked before
    the error dict reaches the wire. Delegates to the shared
    ``shared.redact_tool_error_text`` helper (single credential-shape
    definition repo-wide, HX-1/HX-3 consolidation).
    """
    return redact_tool_error_text(text)


def _status_from_message(message: str | None) -> int | None:
    if not message:
        return None
    match = _HTTPX_STATUS_RE.search(message)
    if not match:
        return None
    code = int(match.group(1))
    return code if 400 <= code <= 599 else None


def _is_already_enveloped(result: dict[str, Any]) -> bool:
    return {"ok", "data", "tool"} <= set(result)


def _message_from(result: dict[str, Any]) -> str | None:
    for key in ("message", "error", "detail"):
        value = result.get(key)
        if value:
            return str(value)
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        return "; ".join(str(item) for item in errors)
    return None


def _blocked_status(result: dict[str, Any]) -> tuple[bool, int | None]:
    status = result.get("status")
    status_code = _status_code(status)
    if status_code is not None and status_code >= 400:
        return True, status_code
    if isinstance(status, str):
        normalized = status.strip().lower()
        if normalized in _BLOCKED_STATUS_HTTP:
            return True, _BLOCKED_STATUS_HTTP[normalized]
    has_error = "error" in result
    errors = result.get("errors")
    has_errors = isinstance(errors, list) and bool(errors)
    if not (has_error or has_errors):
        return False, None
    # Prefer the backend's own upstream code, then any code recoverable from
    # the message, before falling back to 500. Reporting an upstream 404/422 as
    # 500 tells clients a caller mistake is a retryable server fault.
    upstream = _status_code(result.get("status_code"))
    if upstream is not None and 400 <= upstream <= 599:
        return True, upstream
    from_message = _status_from_message(_message_from(result))
    if from_message is not None:
        return True, from_message
    return True, 500


class ResponseEnvelopeMiddleware:
    """Wrap error/blocked dict responses as `{ok, status, data, message, tool, platform}`.

    ``platform`` and an optional ``hint`` (a short, spec-grounded-when-possible
    explanation of what the status code means for this API) are both
    opt-in via constructor resolvers, so the router can wire real backend
    resolution without this module depending on the router at all, and so a
    bare ``ResponseEnvelopeMiddleware()`` -- as used directly in unit tests
    and any caller that hasn't wired resolvers -- keeps its exact original
    shape: ``platform`` stays ``None`` and the ``hint`` key is omitted
    entirely rather than set to ``None``.
    """

    def __init__(
        self,
        *,
        label_resolver: Callable[[str, dict[str, Any]], tuple[str, str, str]] | None = None,
        platform_resolver: Callable[[str | None], str | None] | None = None,
        hint_resolver: Callable[[str, int | None, str | None], str | None] | None = None,
    ) -> None:
        # label_resolver resolves (target_tool_name, backend_server, capability)
        # for one call -- built to accept tool_router's own `_router_call_labels`
        # directly, so a dispatched `invoke_tool`/`invoke_read_tool` failure is
        # attributed to the *backend* tool/server that actually failed, not the
        # generic dispatcher name.
        self._label_resolver = label_resolver
        # platform_resolver maps that backend server to a platform key -- built
        # to accept tool_router's own `_server_platform` directly.
        self._platform_resolver = platform_resolver
        self._hint_resolver = hint_resolver

    def before_call(self, name: str, arguments: dict[str, Any]) -> None:
        return None

    def after_call(
        self, name: str, arguments: dict[str, Any], result: Any
    ) -> dict[str, Any] | None:
        if not isinstance(result, dict) or _is_already_enveloped(result):
            return None

        should_wrap, status = _blocked_status(result)
        if not should_wrap:
            return None

        target_name = name
        backend = None
        if self._label_resolver is not None:
            try:
                resolved_target, backend, _capability = self._label_resolver(name, arguments)
                target_name = resolved_target or name
            except Exception:
                target_name, backend = name, None

        platform = None
        if self._platform_resolver is not None:
            try:
                platform = self._platform_resolver(backend)
            except Exception:
                platform = None

        envelope: dict[str, Any] = {
            "ok": False,
            "status": status,
            "data": result,
            "message": _message_from(result),
            "tool": name,
            "platform": platform,
        }

        if self._hint_resolver is not None:
            try:
                hint = self._hint_resolver(target_name, status, platform)
            except Exception:
                hint = None
            if hint is not None:
                envelope["hint"] = hint

        return envelope

    def on_error(
        self, name: str, arguments: dict[str, Any], exc: BaseException
    ) -> dict[str, Any] | None:
        """Envelope raised exceptions the same way as returned error dicts.

        The router surface catches backend exceptions into ``{"error": ...}``
        dicts which ``after_call`` then envelopes; a standalone backend needs
        this hook for the same shape. Protocol-level errors (``MCPError``
        subclasses such as elicitation requests) and control-flow exceptions
        (cancellation, interpreter exit) must reach the wire untouched, so
        they return ``None`` and keep propagating.
        """
        if isinstance(exc, (MCPError, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            return None
        # Standalone backends have no router-side dispatch helper, so a raised
        # backend exception is stringified here for the first (and only) time.
        # Mask any credential in the message before the error dict reaches the
        # wire -- single application point, reusing the shared walker (HX-3).
        message = _redact_envelope_error(f"{type(exc).__name__}: {exc}")
        return self.after_call(name, arguments, {"error": message})
