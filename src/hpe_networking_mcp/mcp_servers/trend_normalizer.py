"""Trend / time-series normalization helpers -- one bounded shape across vendors.

Provenance / license note: this module is an original implementation
written for this repository. It is not derived from, and does not copy,
any code from the ``nowireless4u/hpe-networking-mcp`` benchmark or any
other project. It closes a capability gap surfaced while auditing this
repo against that benchmark's reference feature set (reusable trend
normalization helpers) -- see ``tests/unit/test_trend_normalizer.py`` for
the audit note.

Central's ``*-trends`` endpoints (see
``hpe_networking_mcp.mcp_servers.monitoring.get_device_trends`` /
``get_switch_interface_trends``) and Mist's ``insights``/SLE endpoints (see
``hpe_networking_mcp.mcp_servers.mist.mist_get_client_insights`` /
``mist_get_org_insights``) both currently pass the vendor's raw JSON
straight through under a ``"trends"``/``"insights"`` key -- each vendor
shapes that payload differently, and any caller wanting a uniform
time-series view has to hand-write vendor-specific parsing.

This module gives any tool one bounded, best-effort way to fold *either*
raw shape (or an unrecognized one) into a common list of
``{"timestamp": ..., "value": ...}`` samples, without needing to know
which vendor produced it.

Design note -- why this is introspective rather than schema-locked: neither
vendor's ``*-trends``/``insights`` *response body* schema is captured in
this repo's committed OpenAPI manifests (only the request path/parameters
are -- the manifests are generated from each vendor's official spec, and
neither publishes a body schema for these particular endpoints in the
pinned specs). Hard-coding exact field names would therefore be an
unverified guess dressed up as a contract. Instead, ``normalize_trend_series``
recognizes several common time-series shapes (a list of `{timestamp, value}`-
ish dicts, a dict wrapping such a list under a common container key, or a
dict of parallel arrays) and falls back to ``normalized=False`` with the
original payload preserved under ``raw`` when it cannot confidently
recognize the shape -- callers should always check ``normalized`` before
relying on ``samples``.

This module makes no network calls and does not mutate its input.
"""

from __future__ import annotations

from typing import Any

DEFAULT_MAX_SAMPLES = 500

# Keys checked (in order) when looking for a per-sample timestamp.
_TIMESTAMP_KEYS = ("timestamp", "ts", "time", "t", "last_updated", "datetime")

# Keys checked (in order) when looking for a per-sample value, once the
# timestamp key (if any) on that same dict is known and excluded.
_VALUE_KEYS = ("value", "val", "v", "count", "utilization", "percentage", "avg", "average")

# Keys checked (in order) for a dict that simply wraps a sample list.
_CONTAINER_KEYS = ("datapoints", "series", "data", "points", "samples", "values")


def _extract_timestamp(item: dict[str, Any]) -> Any:
    for key in _TIMESTAMP_KEYS:
        if key in item:
            return item[key]
    return None


def _extract_value(item: dict[str, Any], exclude: set[str]) -> Any:
    for key in _VALUE_KEYS:
        if key in item and key not in exclude:
            return item[key]
    for key, val in item.items():
        if key in exclude:
            continue
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return val
    return None


def _find_sample_list(raw: dict[str, Any]) -> list[Any] | None:
    for key in _CONTAINER_KEYS:
        value = raw.get(key)
        if isinstance(value, list):
            return value

    # Parallel-array shape: one list-valued key that looks like a timestamp
    # axis, plus exactly one other list-valued key of the same length.
    ts_key = next(
        (key for key in _TIMESTAMP_KEYS if isinstance(raw.get(key), list)), None
    )
    if ts_key is None:
        return None
    timestamps = raw[ts_key]
    value_key = next(
        (
            key
            for key, val in raw.items()
            if key != ts_key and isinstance(val, list) and len(val) == len(timestamps)
        ),
        None,
    )
    if value_key is None:
        return None
    return [
        {"timestamp": ts, "value": val} for ts, val in zip(timestamps, raw[value_key], strict=True)
    ]


def _unnormalized(raw: Any, metric: str | None) -> dict[str, Any]:
    return {
        "metric": metric,
        "normalized": False,
        "samples": [],
        "sample_count": 0,
        "truncated": False,
        "raw": raw,
    }


def normalize_trend_series(
    raw: Any,
    *,
    metric: str | None = None,
    max_samples: int = DEFAULT_MAX_SAMPLES,
) -> dict[str, Any]:
    """Fold a vendor trend/insights payload into one bounded sample list.

    Args:
        raw: The vendor's raw trend/insights JSON. Supports a bare list of
            samples, a dict wrapping such a list under a common key
            (``data``/``series``/``points``/...), or a dict of parallel
            timestamp/value arrays. Anything else is returned unnormalized
            (see ``normalized`` below) -- this function never raises on an
            unrecognized shape.
        metric: Optional descriptive label stamped onto the result (e.g.
            ``"cpu"``, ``"throughput"``); purely cosmetic, never used to
            drive parsing.
        max_samples: Bounds the returned sample list. When the recognized
            series has more than this many entries, the oldest are dropped
            and ``truncated`` is set -- the input order is assumed
            oldest-first, matching every trend endpoint reviewed in this
            repo.

    Returns:
        Dict with keys:

        - ``metric``: echoes the ``metric`` argument.
        - ``normalized``: ``True`` if a recognizable time-series shape was
          found, ``False`` otherwise.
        - ``samples``: ``list[{"timestamp": ..., "value": ...}]``, bounded
          by ``max_samples``. Empty when ``normalized`` is ``False``.
        - ``sample_count``: total recognized samples before bounding.
        - ``truncated``: ``True`` if ``sample_count`` exceeded ``max_samples``.
        - ``raw``: present (and equal to the input) only when
          ``normalized`` is ``False``, so the caller can still inspect or
          forward the original payload.
    """
    if max_samples <= 0:
        raise ValueError(f"max_samples must be positive, got {max_samples}")

    if isinstance(raw, list):
        candidates: list[Any] | None = raw
    elif isinstance(raw, dict):
        candidates = _find_sample_list(raw)
    else:
        candidates = None

    if candidates is None:
        return _unnormalized(raw, metric)

    samples: list[dict[str, Any]] = []
    for item in candidates:
        if isinstance(item, dict):
            ts = _extract_timestamp(item)
            exclude = {key for key in _TIMESTAMP_KEYS if key in item}
            val = _extract_value(item, exclude)
            if ts is None and val is None:
                continue
            samples.append({"timestamp": ts, "value": val})
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            samples.append({"timestamp": None, "value": item})
        # Any other item shape (string, None, ...) is skipped rather than
        # guessed at.

    if not candidates:
        # Recognized container shape, just empty -- distinct from "could not
        # find a time-series shape at all".
        return {
            "metric": metric,
            "normalized": True,
            "samples": [],
            "sample_count": 0,
            "truncated": False,
        }

    if not samples:
        # We found a list, but nothing in it looked sample-shaped -- treat
        # as unrecognized rather than silently claiming an empty series.
        return _unnormalized(raw, metric)

    total = len(samples)
    truncated = total > max_samples
    bounded = samples[-max_samples:] if truncated else samples

    return {
        "metric": metric,
        "normalized": True,
        "samples": bounded,
        "sample_count": total,
        "truncated": truncated,
    }
