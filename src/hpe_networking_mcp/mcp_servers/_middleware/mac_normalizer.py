"""Optional outbound MAC address normalization middleware.

Central and adjacent network APIs can return MAC addresses as colon-separated,
dash-separated, or dotted values. Normalizing them for the model reduces
duplicate entity reasoning without changing API calls. The router enables this
middleware only when `HPE_MCP_NORMALIZE_MACS=1`.
"""

from __future__ import annotations

import re
from typing import Any


_SEPARATED_MAC_RE = re.compile(
    r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f])"
)
_DOTTED_MAC_RE = re.compile(
    r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}(?![0-9A-Fa-f])"
)


def _canonical_mac(value: str) -> str:
    chars = re.sub(r"[^0-9A-Fa-f]", "", value).lower()
    return ":".join(chars[index : index + 2] for index in range(0, 12, 2))


def normalize_mac_text(value: str) -> str:
    """Normalize separated/dotted MACs in a string to lowercase colon format."""
    value = _SEPARATED_MAC_RE.sub(lambda match: _canonical_mac(match.group(0)), value)
    return _DOTTED_MAC_RE.sub(lambda match: _canonical_mac(match.group(0)), value)


def normalize_macs(value: Any) -> Any:
    """Recursively normalize MAC-shaped strings in JSON-like data."""
    if isinstance(value, str):
        return normalize_mac_text(value)
    if isinstance(value, list):
        return [normalize_macs(item) for item in value]
    if isinstance(value, tuple):
        return tuple(normalize_macs(item) for item in value)
    if isinstance(value, dict):
        return {key: normalize_macs(item) for key, item in value.items()}
    return value


class MacNormalizeMiddleware:
    """Normalize outbound MAC address strings when explicitly enabled."""

    def before_call(self, name: str, arguments: dict[str, Any]) -> None:
        return None

    def after_call(self, name: str, arguments: dict[str, Any], result: Any) -> Any:
        normalized = normalize_macs(result)
        return normalized if normalized != result else None

    def on_error(self, name: str, arguments: dict[str, Any], exc: BaseException) -> None:
        return None
