"""Shared, bounded tool-call outcome classification.

``AuditLogMiddleware`` and ``MetricsMiddleware`` both need the same "what
happened" bucket for a completed or failed tool call. Centralizing it here
means the two middlewares can never silently diverge, and — critically for
metrics cardinality — the return value of :func:`classify_outcome` is always
one of a small, fixed set of strings, never a value derived from tool
arguments, result payloads, or exception messages.
"""

from __future__ import annotations

import asyncio
from typing import Any

# Every outcome bucket either middleware may record. Bounded by
# construction: metrics/audit code should never widen this with a raw,
# unbounded string (a result's ``status`` field, an exception message, ...).
KNOWN_OUTCOMES: tuple[str, ...] = (
    "success",
    "error",
    "exception",
    "cancelled",
    "timeout",
    "blocked",
    "declined",
    "confirmation_required",
    "confirmation_unavailable",
    "forbidden",
    "not_found",
    "failed",
    "failure",
)

_BLOCKED_LIKE_STATUSES = frozenset(
    {
        "blocked",
        "cancelled",
        "declined",
        "confirmation_required",
        "confirmation_unavailable",
        "forbidden",
        "not_found",
        "failed",
        "failure",
        "error",
    }
)


def classify_outcome(result: Any) -> str:
    """Classify a completed tool call's result into a bounded outcome bucket.

    Never inspects argument or result *values* beyond the small, known
    ``status``/``ok``/``error`` control fields and the presence of entries in
    the ``errors`` list already used by ``ResponseEnvelopeMiddleware``. It
    never reads error-list contents, so this stays safe to call on results
    that may contain secret-shaped or identifier-shaped data.
    """
    if not isinstance(result, dict):
        return "success"
    if result.get("ok") is False or result.get("error"):
        return "error"
    status = str(result.get("status") or "").strip().lower()
    if status in _BLOCKED_LIKE_STATUSES:
        return status
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        return "error"
    return "success"


def classify_error_outcome(exc: BaseException) -> str:
    """Classify a raised exception into a bounded outcome bucket.

    Distinguishes cooperative cancellation (``asyncio.CancelledError``) and
    ``asyncio.wait_for``-style timeouts (``TimeoutError``/
    ``asyncio.TimeoutError`` -- the same builtin type on Python 3.11+, a
    distinct subclass of ``Exception`` on 3.10) from any other exception --
    never reads ``str(exc)``.
    """
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    return "exception"
