"""Response envelope middleware for error and blocked tool results.

This intentionally wraps only failures/blocked states. Successful payloads pass
through unchanged so existing clients and tests keep their expected shapes while
small models get a reliable `ok=false` signal when something did not happen.
"""

from __future__ import annotations

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
}


def _status_code(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


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
    if "error" in result:
        return True, 500
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        return True, 500
    return False, None


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
