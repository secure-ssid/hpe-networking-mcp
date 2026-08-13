"""Capability classification for generated operations.

Defaults (task requirement 5):

* ``GET`` / ``HEAD``    -> ``"read"``       (read-only, execute directly)
* ``DELETE``            -> ``"destructive"``
* ``POST`` / ``PUT`` / ``PATCH`` -> ``"write"``

An override manifest (``overrides/<platform>.json``) can reclassify individual
operations by their operation key (``"METHOD /path"``) -- for example a POST
diagnostic that is effectively read-only, or a side-effecting GET.
"""

from __future__ import annotations

READ = "read"
DIAGNOSTIC = "diagnostic"
WRITE = "write"
DESTRUCTIVE = "destructive"

CAPABILITIES = (READ, DIAGNOSTIC, WRITE, DESTRUCTIVE)

_READ_METHODS = {"GET", "HEAD"}
_DESTRUCTIVE_METHODS = {"DELETE"}
_WRITE_METHODS = {"POST", "PUT", "PATCH"}


def default_capability(method: str) -> str:
    m = method.upper()
    if m in _READ_METHODS:
        return READ
    if m in _DESTRUCTIVE_METHODS:
        return DESTRUCTIVE
    if m in _WRITE_METHODS:
        return WRITE
    # OPTIONS / TRACE and anything unexpected: treat as read-only metadata.
    return READ


def classify(method: str, op_key: str, overrides: dict[str, str] | None = None) -> str:
    """Return the capability for one operation, honoring an override map."""
    if overrides:
        override = overrides.get(op_key)
        if override is not None:
            override = override.strip().lower()
            if override not in CAPABILITIES:
                raise ValueError(
                    f"invalid capability override {override!r} for {op_key}; "
                    f"expected one of {CAPABILITIES}"
                )
            return override
    return default_capability(method)


def is_read(capability: str) -> bool:
    return capability == READ
