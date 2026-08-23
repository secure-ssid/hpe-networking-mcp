"""Opt-in append-only audit records for router tool calls.

Set ``HPE_MCP_AUDIT_LOG=1`` to write ``state/tool-audit.jsonl`` or set it
to an explicit file path. Records contain argument keys and a digest of a
redacted copy, never raw argument or result values.

v0.7 adds bounded run/session correlation and a write/destructive
classification to every record, still without ever persisting user/tenant
input verbatim:

- ``run_id`` identifies *this server process* -- a random token generated
  once at import time, so records from the same process (potentially many
  connected clients) can be grouped without reusing any client-supplied
  identifier.
- ``session_id`` identifies *one connected MCP client session* -- a random
  token assigned the first time a given session object is seen, held in a
  bounded, weak-referenced map (mirrors ``SecretTokenizeMiddleware``'s
  session-vault scoping) so it can never grow without bound and is never
  derived from anything the client sent.
- ``classification`` is one of ``read``/``write``/``destructive``/
  ``diagnostic``/``unknown`` -- resolved by an optional, injected
  ``classifier`` callback (the owning server knows its own tool-capability
  map; this module does not hard-code one), defaulting to ``unknown`` when
  no classifier is wired in or it raises/returns something unrecognized.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import re
import secrets
import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hpe_networking_mcp._paths import repo_root
from hpe_networking_mcp.mcp_servers.shared import redact_sensitive

from ._outcome import classify_error_outcome, classify_outcome

ROOT = repo_root()
DEFAULT_AUDIT_PATH = ROOT / "state" / "tool-audit.jsonl"
_AUDIT_ENV = "HPE_MCP_AUDIT_LOG"

_KNOWN_CLASSIFICATIONS = frozenset({"read", "write", "destructive", "diagnostic", "unknown"})
_SESSION_ID_BYTES = 8
_MAX_FALLBACK_SESSIONS = 256
_SAFE_TARGET_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")

#: Router tools that dispatch into a *different* backend tool, and therefore
#: have a meaningful audit "target". ``invoke_read_tool_batch`` dispatches
#: several; its resolver collapses them to a single bounded label (see
#: ``hpe_networking_mcp.mcp_servers.tool_router._router_call_target``) rather than leaving the
#: record's target ``None``/``unknown``.
_DISPATCHING_TOOL_NAMES = frozenset(
    {"invoke_tool", "invoke_read_tool", "invoke_read_tool_batch"}
)

# One correlation id per *process* -- generated once at import time, never
# from client input, so every audit record written by this process (however
# many client sessions connect to it) can be grouped together.
_PROCESS_RUN_ID = f"run_{secrets.token_hex(8)}"


def audit_path() -> Path | None:
    raw = os.getenv(_AUDIT_ENV, "").strip()
    if not raw or raw.lower() in {"0", "false", "no", "off"}:
        return None
    if raw.lower() in {"1", "true", "yes", "on"}:
        return DEFAULT_AUDIT_PATH
    return Path(raw).expanduser()


def _argument_digest(arguments: dict[str, Any]) -> str:
    redacted = redact_sensitive(arguments)
    canonical = json.dumps(
        redacted,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _SessionCorrelation:
    """Bounded, process-random session id assignment.

    Never derived from any client-supplied value -- just "have we seen this
    session object before". Mirrors the ``WeakKeyDictionary`` + bounded LRU
    fallback pattern already used by ``SecretTokenizeMiddleware`` for the
    same reason: some session-like objects in tests are not weakly
    referenceable, so a small bounded fallback cache prevents an unbounded
    dict while keeping the common (real, weakly-referenceable) case
    self-cleaning as sessions disconnect.
    """

    def __init__(self) -> None:
        self._by_session: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()
        self._fallback: OrderedDict[int, tuple[Any, str]] = OrderedDict()

    def id_for(self, context: Any) -> str:
        try:
            session = getattr(context, "session", None) if context is not None else None
        except (ValueError, RuntimeError):
            # v2 SDK: Context.session raises ValueError outside a live request
            session = None
        if session is None:
            return "sess_none"
        try:
            sid = self._by_session.get(session)
            if sid is None:
                sid = f"sess_{secrets.token_hex(_SESSION_ID_BYTES)}"
                self._by_session[session] = sid
            return sid
        except TypeError:
            # Not weakly referenceable (some test doubles) -- bounded LRU
            # fallback so a long-lived process still cannot leak one entry
            # per historical connection forever.
            key = id(session)
            entry = self._fallback.get(key)
            if entry is None or entry[0] is not session:
                entry = (session, f"sess_{secrets.token_hex(_SESSION_ID_BYTES)}")
                self._fallback[key] = entry
            self._fallback.move_to_end(key)
            while len(self._fallback) > _MAX_FALLBACK_SESSIONS:
                self._fallback.popitem(last=False)
            return entry[1]


class AuditLogMiddleware:
    """Write one redacted JSONL record per completed or failed tool call."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        classifier: Callable[[str, dict[str, Any]], str] | None = None,
        target_resolver: Callable[[str, dict[str, Any]], str | None] | None = None,
    ):
        self.path = path
        self._classifier = classifier
        self._target_resolver = target_resolver
        self._starts: contextvars.ContextVar[list[float] | None] = contextvars.ContextVar(
            f"hpe_mcp_audit_starts_{id(self)}",
            default=None,
        )
        self._write_lock = threading.Lock()
        self._sessions = _SessionCorrelation()

    def _configured_path(self) -> Path | None:
        return self.path or audit_path()

    def _classify(self, name: str, arguments: dict[str, Any]) -> str:
        if self._classifier is None:
            return "unknown"
        try:
            value = self._classifier(name, arguments)
        except Exception:
            return "unknown"
        normalized = str(value).strip().lower() if value else "unknown"
        return normalized if normalized in _KNOWN_CLASSIFICATIONS else "unknown"

    def _target_tool(self, name: str, arguments: dict[str, Any]) -> str | None:
        if name not in _DISPATCHING_TOOL_NAMES:
            return None
        if self._target_resolver is None:
            return "unknown"
        try:
            target = self._target_resolver(name, arguments)
        except Exception:
            return "unknown"
        normalized = str(target).strip() if target else ""
        return normalized if _SAFE_TARGET_RE.fullmatch(normalized) else "unknown"

    def before_call(self, name: str, arguments: dict[str, Any], context: Any = None) -> None:
        if self._configured_path() is None:
            return None
        starts = list(self._starts.get() or [])
        starts.append(time.monotonic())
        self._starts.set(starts)
        return None

    def _duration_ms(self) -> float | None:
        starts = list(self._starts.get() or [])
        if not starts:
            return None
        started = starts.pop()
        self._starts.set(starts)
        return round((time.monotonic() - started) * 1000, 3)

    def _append_record(self, path: Path, line: str) -> None:
        """Blocking file I/O for one record — runs on a worker thread.

        Called exclusively via :func:`asyncio.to_thread` from :meth:`_write`
        so the dispatcher's event loop never performs ``mkdir``/``open``/
        ``write`` itself. The lock stays: ``to_thread`` dispatches onto the
        default executor, so concurrent records can arrive here in parallel.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    async def _write(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        outcome: str,
        context: Any = None,
        error_type: str | None = None,
    ) -> None:
        path = self._configured_path()
        if path is None:
            return
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run_id": _PROCESS_RUN_ID,
            "session_id": self._sessions.id_for(context),
            "tool": name,
            "target_tool": self._target_tool(name, arguments),
            "classification": self._classify(name, arguments),
            "argument_keys": sorted(str(key) for key in arguments),
            "argument_digest": _argument_digest(arguments),
            "outcome": outcome,
            "duration_ms": self._duration_ms(),
            "error_type": error_type,
        }
        line = json.dumps(record, sort_keys=True) + "\n"
        await asyncio.to_thread(self._append_record, path, line)

    async def after_call(
        self,
        name: str,
        arguments: dict[str, Any],
        result: Any,
        context: Any = None,
    ) -> None:
        await self._write(name, arguments, outcome=classify_outcome(result), context=context)
        return None

    async def on_error(
        self,
        name: str,
        arguments: dict[str, Any],
        exc: BaseException,
        context: Any = None,
    ) -> None:
        await self._write(
            name,
            arguments,
            outcome=classify_error_outcome(exc),
            context=context,
            error_type=type(exc).__name__,
        )
        return None
