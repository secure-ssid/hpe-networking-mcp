"""Deterministic, globally-unique tool naming for generated operations.

Policy (see task requirement 2):

* prefer ``operationId``, snake-cased, prefixed with the platform key;
* otherwise fall back to ``method + path`` slug;
* enforce a maximum name length by appending a short, stable SHA-256 digest;
* fail (raise :class:`DuplicateNameError`) on an unresolved duplicate rather
  than silently overwriting another operation's tool.
"""

from __future__ import annotations

import hashlib
import re

MAX_NAME_LEN = 60
_DIGEST_LEN = 8

_CAMEL_BOUNDARY_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_BOUNDARY_2 = re.compile(r"([a-z0-9])([A-Z])")
_NON_ALNUM = re.compile(r"[^0-9A-Za-z]+")
_MULTI_US = re.compile(r"_+")


class DuplicateNameError(Exception):
    """Raised when two operations resolve to the same tool name."""


def snake(value: str) -> str:
    """Convert an arbitrary identifier (camelCase, path, etc.) to snake_case."""
    value = _NON_ALNUM.sub("_", value)
    value = _CAMEL_BOUNDARY_1.sub(r"\1_\2", value)
    value = _CAMEL_BOUNDARY_2.sub(r"\1_\2", value)
    value = _MULTI_US.sub("_", value)
    return value.strip("_").lower()


def digest(method: str, path: str) -> str:
    """Return a short, stable digest for a ``method``/``path`` pair."""
    payload = f"{method.upper()} {path}".encode()
    return hashlib.sha256(payload).hexdigest()[:_DIGEST_LEN]


def _truncate_with_digest(name: str, method: str, path: str) -> str:
    keep = MAX_NAME_LEN - _DIGEST_LEN - 1
    return f"{name[:keep].rstrip('_')}_{digest(method, path)}"


def base_name(platform: str, method: str, path: str, operation_id: str | None) -> str:
    """Compute the preferred (pre-collision) tool name for one operation."""
    platform = snake(platform)
    if operation_id and snake(operation_id):
        core = snake(operation_id)
    else:
        core = f"{method.lower()}_{snake(path)}"
    name = f"{platform}_{core}"
    if len(name) > MAX_NAME_LEN:
        name = _truncate_with_digest(name, method, path)
    return name


class NameAllocator:
    """Allocate globally-unique names, failing on unresolved collisions."""

    def __init__(self) -> None:
        self._used: dict[str, str] = {}  # name -> operation key
        self._op_keys: set[str] = set()

    @property
    def used(self) -> dict[str, str]:
        return dict(self._used)

    def allocate(self, platform: str, method: str, path: str, operation_id: str | None) -> str:
        op_key = f"{method.upper()} {path}"
        if op_key in self._op_keys:
            raise DuplicateNameError(f"duplicate operation key {op_key!r} in spec")
        self._op_keys.add(op_key)
        name = base_name(platform, method, path, operation_id)
        if name not in self._used:
            self._used[name] = op_key
            return name
        # Collision: disambiguate deterministically with the op digest.
        candidate = _truncate_with_digest(name, method, path)
        if candidate not in self._used:
            self._used[candidate] = op_key
            return candidate
        raise DuplicateNameError(
            f"unresolved duplicate tool name {name!r} / {candidate!r} for {op_key} "
            f"(already used by {self._used[candidate]!r})"
        )
