"""Shared validation for Central scope IDs used by configuration writes."""

from __future__ import annotations

from typing import Any

MAX_SCOPE_ID_LENGTH = 20


def normalize_scope_id(value: Any, *, field_name: str = "scope_id") -> str:
    """Return a canonical numeric scope ID or raise ``ValueError``.

    Central's scope-management APIs publish scope IDs as strings, while the
    legacy scope-map payloads used by this project require integer conversion.
    Validate once before any write so malformed IDs cannot fail mid-workflow.
    """
    if isinstance(value, bool):
        raise ValueError(
            f"{field_name} must contain 1-{MAX_SCOPE_ID_LENGTH} ASCII decimal digits"
        )
    if isinstance(value, int):
        text = str(value) if value >= 0 else ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        text = ""
    if (
        not text
        or len(text) > MAX_SCOPE_ID_LENGTH
        or not text.isascii()
        or not text.isdigit()
    ):
        raise ValueError(
            f"{field_name} must contain 1-{MAX_SCOPE_ID_LENGTH} ASCII decimal digits"
        )
    return text
