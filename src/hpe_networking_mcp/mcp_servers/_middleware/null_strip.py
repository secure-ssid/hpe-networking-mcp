"""NullStripMiddleware — drops explicit ``None`` argument values.

Many MCP clients send ``null`` for optional parameters instead of omitting
them. Pydantic rejects ``None`` when the declared type doesn't allow it
(``default=None`` only skips validation when the key is *absent*).
Removing ``None``-valued keys before the tool runs lets the Pydantic
default kick in normally.

Ported (MIT) from
https://github.com/nowireless4u/hpe-networking-mcp/blob/main/src/hpe_networking_mcp/middleware/null_strip.py
Adapted for the hpe-networking-mcp middleware install API — the behavioural
contract is identical: strip top-level ``None`` keys only. Nested dicts
and non-``None`` falsy values (``0``, ``""``, ``False``, ``[]``) are
preserved exactly.

(c) 2025 Hewlett Packard Enterprise Development LP, MIT.
Adapted 2026 for secure-ssid/hpe-networking-mcp.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class NullStripMiddleware:
    """Strip top-level ``None`` values from tool call arguments.

    Intentionally shallow: we only touch the top-level args dict because
    that's where the client-sends-null pain is. Recursing into nested
    dicts risks eating legitimate ``None`` sentinels (e.g. a caller that
    explicitly passes ``None`` inside a payload to mean "unset").
    """

    def before_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        if not arguments:
            return None
        filtered = {k: v for k, v in arguments.items() if v is not None}
        if len(filtered) == len(arguments):
            return None
        stripped = sorted(set(arguments) - set(filtered))
        logger.debug("NullStrip on %s: removed %s", name, stripped)
        return filtered

    # No-op hooks so Protocol checks pass uniformly
    def after_call(self, name: str, arguments: dict[str, Any], result: Any) -> None:
        return None

    def on_error(self, name: str, arguments: dict[str, Any], exc: BaseException) -> None:
        return None
