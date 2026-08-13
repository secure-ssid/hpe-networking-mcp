"""Response envelope middleware for error and blocked tool results.

This intentionally wraps only failures/blocked states. Successful payloads pass
through unchanged so existing clients and tests keep their expected shapes while
small models get a reliable `ok=false` signal when something did not happen.
"""

from __future__ import annotations

import re
from typing import Any


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
    """Wrap error/blocked dict responses as `{ok, status, data, message, tool}`."""

    def before_call(self, name: str, arguments: dict[str, Any]) -> None:
        return None

    def after_call(self, name: str, arguments: dict[str, Any], result: Any) -> dict[str, Any] | None:
        if not isinstance(result, dict) or _is_already_enveloped(result):
            return None

        should_wrap, status = _blocked_status(result)
        if not should_wrap:
            return None

        return {
            "ok": False,
            "status": status,
            "data": result,
            "message": _message_from(result),
            "tool": name,
            "platform": None,
        }

    def on_error(self, name: str, arguments: dict[str, Any], exc: BaseException) -> None:
        return None
