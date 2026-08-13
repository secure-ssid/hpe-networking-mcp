"""SecretTokenizeMiddleware — opt-in, reversible secret/PII tokenization.

Problem: ``hpe_networking_mcp.mcp_servers.shared.redact_sensitive`` irreversibly masks
sensitive-looking values (to ``"******"``) before a tool result reaches a
model, which is the right *default*. But some NOC workflows legitimately
need to read a secret from one tool (a webhook signing key, a temporary
PSK, an API token minted by a prior call) and pass it back into a *write*
tool a few calls later — irreversible masking breaks that round trip, and
disabling redaction entirely would put plaintext secrets in front of the
model (and any transcript/log capturing its context).

This middleware, enabled only with ``HPE_MCP_TOKENIZE_SECRETS=1``,
replaces sensitive-keyed string values in tool *results* with an opaque,
session-scoped token (``hpe_mcp_secret_<32 hex chars>``) instead of
``"******"``. The plaintext is held in a bounded, TTL'd in-memory vault
keyed by that token. If the *same* token string later appears in a
*subsequent* tool call's arguments (the model echoing back exactly what it
was shown), ``before_call`` swaps it back to the real plaintext
transparently before the tool runs — the model itself never observes the
plaintext at any point, only the opaque placeholder.

Session scoping uses the identity of the MCP ``ServerSession`` object
reachable from the MCPServer ``Context`` passed through by
``hpe_networking_mcp.mcp_servers._middleware.install`` (stable for the lifetime of one
connected client; the current SDK — 1.28.1 — has no separate persistent
session-id string, only session *objects*). Calls made with no context
(e.g. direct ToolManager calls in tests, or any call before a session is
negotiated) share one fallback scope — acceptable because that scope is
itself process-local and disabled by default.

This does NOT replace ``redact_sensitive()``. When
``HPE_MCP_TOKENIZE_SECRETS`` is unset (the default), every hook here is
a no-op and behavior is unchanged.
"""

from __future__ import annotations

import os
import re
import secrets
import time
import weakref
from collections import OrderedDict
from typing import Any

from hpe_networking_mcp.mcp_servers.shared import _is_sensitive_key

_ENV_FLAG = "HPE_MCP_TOKENIZE_SECRETS"
_TOKEN_PREFIX = "hpe_mcp_secret_"
_TOKEN_RE = re.compile(re.escape(_TOKEN_PREFIX) + r"[0-9a-f]{32}")

DEFAULT_MAX_ENTRIES = 256
DEFAULT_TTL_SECONDS = 900.0  # 15 minutes
DEFAULT_MAX_SESSION_VAULTS = 256


def tokenize_enabled() -> bool:
    """Whether secret tokenization is opted into for this process."""
    return os.environ.get(_ENV_FLAG, "").strip().lower() in ("1", "true", "yes")


class _SessionVault:
    """Bounded, TTL'd token -> plaintext store for one session scope.

    Insertion order == recency here (every ``put`` appends; nothing is
    reinserted on read), so evicting the oldest entry when at capacity is
    a straightforward FIFO-over-a-dict eviction — simple and sufficient
    for a short-TTL secret-passing buffer.
    """

    def __init__(self, max_entries: int, ttl_seconds: float):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._entries: dict[str, tuple[str, float]] = {}

    def _purge_expired(self, now: float) -> None:
        expired = [key for key, (_, ts) in self._entries.items() if now - ts > self.ttl_seconds]
        for key in expired:
            del self._entries[key]

    def put(self, plaintext: str) -> str:
        now = time.monotonic()
        self._purge_expired(now)
        while len(self._entries) >= self.max_entries:
            oldest_key = next(iter(self._entries))
            del self._entries[oldest_key]
        token = _TOKEN_PREFIX + secrets.token_hex(16)
        self._entries[token] = (plaintext, now)
        return token

    def resolve(self, token: str) -> str | None:
        now = time.monotonic()
        self._purge_expired(now)
        entry = self._entries.get(token)
        return entry[0] if entry else None

    def __len__(self) -> int:
        return len(self._entries)


def _walk_tokenize(value: Any, vault: _SessionVault, parent_key: Any = None) -> tuple[Any, bool]:
    """Recursively replace sensitive-keyed string values with vault tokens.

    Returns ``(new_value, changed)``.
    """
    if isinstance(value, dict):
        changed = False
        out: dict[Any, Any] = {}
        for key, item in value.items():
            new_item, item_changed = _walk_tokenize(item, vault, parent_key=key)
            out[key] = new_item
            changed = changed or item_changed
        return out, changed
    if isinstance(value, list):
        changed = False
        out_list = []
        for item in value:
            new_item, item_changed = _walk_tokenize(item, vault, parent_key=parent_key)
            out_list.append(new_item)
            changed = changed or item_changed
        return out_list, changed
    if isinstance(value, tuple):
        new_tuple, changed = _walk_tokenize(list(value), vault, parent_key=parent_key)
        return tuple(new_tuple), changed
    if (
        isinstance(value, str)
        and value
        and value != "******"
        and parent_key is not None
        and _is_sensitive_key(parent_key)
    ):
        return vault.put(value), True
    return value, False


def _walk_resolve(value: Any, vault: _SessionVault) -> tuple[Any, bool]:
    """Recursively replace vault-token strings with their stored plaintext.

    Returns ``(new_value, changed)``.
    """
    if isinstance(value, dict):
        changed = False
        out: dict[Any, Any] = {}
        for key, item in value.items():
            new_item, item_changed = _walk_resolve(item, vault)
            out[key] = new_item
            changed = changed or item_changed
        return out, changed
    if isinstance(value, list):
        changed = False
        out_list = []
        for item in value:
            new_item, item_changed = _walk_resolve(item, vault)
            out_list.append(new_item)
            changed = changed or item_changed
        return out_list, changed
    if isinstance(value, tuple):
        new_tuple, changed = _walk_resolve(list(value), vault)
        return tuple(new_tuple), changed
    if isinstance(value, str):
        match = _TOKEN_RE.fullmatch(value.strip())
        if match:
            plaintext = vault.resolve(match.group(0))
            if plaintext is not None:
                return plaintext, True
    return value, False


def _transport_session_id(context: Any) -> str | None:
    """Stable per-connection id from the transport, when one exists.

    Streamable HTTP with sessions exposes one; stdio and stateless HTTP do
    not (the SDK documents ``session_id`` as ``None`` there).
    """
    if context is None:
        return None
    try:
        request_context = getattr(context, "request_context", None)
    except (ValueError, RuntimeError, LookupError):
        # v2 SDK raises "Context is not available outside of a request"
        # rather than returning None when there is no live request.
        request_context = None
    for owner in (context, request_context):
        if owner is None:
            continue
        try:
            session_id = getattr(owner, "session_id", None)
        except (ValueError, RuntimeError, LookupError):
            continue
        if isinstance(session_id, str) and session_id.strip():
            return session_id.strip()
    return None


def _is_per_request_sdk_session(session: Any) -> bool:
    """True when ``session`` is an installed-SDK ``ServerSession``.

    MCP SDK v2 builds a *fresh* ``ServerSession`` (and ``Connection``) for
    every inbound request -- its own docstring says "Per-request proxy ...
    built once per inbound request by the kernel's ``_make_context``". Keying
    a session-scoped vault on that object therefore produced one vault per
    *call*, so a token minted by a read could never be resolved by the
    following write: the documented round trip silently did nothing over a
    real MCP session. Detecting that type lets this middleware fall back to a
    scope that actually spans requests, while any other session-ish object
    (a test double, or a future SDK with a genuinely per-connection session)
    keeps the original object-identity scoping.
    """
    try:
        from mcp.server.session import ServerSession
    except Exception:  # pragma: no cover - SDK layout change / not installed
        return False
    return isinstance(session, ServerSession)


class SecretTokenizeMiddleware:
    """Reversible secret/PII tokenization, gated by ``HPE_MCP_TOKENIZE_SECRETS``.

    Domain modules adopt this by adding it to their existing
    ``install_middleware(mcp, [...])`` list -- it is inert (every hook is a
    no-op) until the env var is set, so adding it is safe by default::

        from hpe_networking_mcp.mcp_servers._middleware import SecretTokenizeMiddleware
        install_middleware(mcp, [NullStripMiddleware(), SecretTokenizeMiddleware(), ...])
    """

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_session_vaults: int = DEFAULT_MAX_SESSION_VAULTS,
    ):
        if max_entries <= 0:
            raise ValueError(f"max_entries must be positive, got {max_entries}")
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
        if max_session_vaults <= 0:
            raise ValueError(
                f"max_session_vaults must be positive, got {max_session_vaults}"
            )
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self.max_session_vaults = max_session_vaults
        self._vaults: weakref.WeakKeyDictionary[Any, _SessionVault] = (
            weakref.WeakKeyDictionary()
        )
        self._opaque_vaults: OrderedDict[Any, tuple[Any, _SessionVault]] = OrderedDict()
        self._fallback_vault = _SessionVault(self.max_entries, self.ttl_seconds)

    def _vault_for(self, context: Any) -> _SessionVault:
        """Resolve the vault whose scope this call belongs to.

        Order, most specific first:

        1. The transport's session id (streamable HTTP with sessions) -- a
           genuine per-connection scope, so two concurrent HTTP clients stay
           isolated.
        2. A non-SDK session object's identity -- preserves the original
           per-session isolation contract for test doubles and any SDK whose
           session really does span requests.
        3. One process-wide vault. This is what stdio and stateless HTTP get,
           because SDK v2 hands every request a brand-new ``ServerSession``
           (see ``_is_per_request_sdk_session``) and exposes no per-connection
           handle to key on. It is the correct scope for stdio -- one client
           per process for the life of that process -- and safe for stateless
           HTTP: tokens are 128 bits of ``secrets`` randomness, bounded in
           count, and expire with the vault TTL, so another caller cannot
           reach a value without already holding the exact token.
        """
        try:
            session = getattr(context, "session", None) if context is not None else None
        except (ValueError, RuntimeError, LookupError):
            # v2 SDK: Context.session raises outside a live request
            session = None

        session_id = _transport_session_id(context)
        if session_id is not None:
            return self._keyed_vault(session_id, session_id)

        if session is None or _is_per_request_sdk_session(session):
            return self._fallback_vault

        try:
            vault = self._vaults.get(session)
            if vault is None:
                vault = _SessionVault(self.max_entries, self.ttl_seconds)
                self._vaults[session] = vault
            return vault
        except TypeError:
            # Some test/dummy session objects are not weak-referenceable.
            # Keep a bounded strong-reference LRU so ids cannot be reused
            # while an entry exists and long-lived servers cannot leak one
            # vault per historical connection forever.
            return self._keyed_vault(id(session), session)

    def _keyed_vault(self, key: Any, owner: Any) -> _SessionVault:
        """Bounded strong-reference LRU of vaults for non-weak-keyable scopes."""
        entry = self._opaque_vaults.get(key)
        if entry is None or entry[0] is not owner:
            entry = (owner, _SessionVault(self.max_entries, self.ttl_seconds))
            self._opaque_vaults[key] = entry
        self._opaque_vaults.move_to_end(key)
        while len(self._opaque_vaults) > self.max_session_vaults:
            self._opaque_vaults.popitem(last=False)
        return entry[1]

    def before_call(
        self, name: str, arguments: dict[str, Any], context: Any = None
    ) -> dict[str, Any] | None:
        if not tokenize_enabled() or not arguments:
            return None
        vault = self._vault_for(context)
        resolved, changed = _walk_resolve(arguments, vault)
        return resolved if changed else None

    def after_call(
        self, name: str, arguments: dict[str, Any], result: Any, context: Any = None
    ) -> Any:
        if not tokenize_enabled():
            return None
        vault = self._vault_for(context)
        tokenized, changed = _walk_tokenize(result, vault)
        return tokenized if changed else None

    def on_error(
        self, name: str, arguments: dict[str, Any], exc: BaseException, context: Any = None
    ) -> None:
        return None
