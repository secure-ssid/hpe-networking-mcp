"""Shared HTTP retry-header parsing."""

from __future__ import annotations

import email.utils
import math
import time


def parse_retry_after(value: str) -> float | None:
    """Parse Retry-After delta-seconds or an HTTP-date into seconds."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        pass
    else:
        return max(0.0, seconds) if math.isfinite(seconds) else None
    try:
        target = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    return max(0.0, target.timestamp() - time.time())
