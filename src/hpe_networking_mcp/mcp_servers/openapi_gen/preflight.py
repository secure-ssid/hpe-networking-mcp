"""Dry-run preflight impact analysis for generated write tools.

Several vendor APIs (Mist most notably) treat ``PUT``/``PATCH`` bodies as a
*shallow* merge: top-level keys you omit are preserved, but any nested object
you *do* send **replaces** its stored counterpart wholesale. Sending

.. code-block:: json

    {"port_usages": {"client_data": {...}}}

therefore deletes every other port profile in ``port_usages`` -- which on a
switch template silently removes uplink and inter-switch-link configuration.

The dry-run preview is no defence on its own, because it only echoes the
request body back: a preview that patches one key looks byte-for-byte like a
preview that deletes five. This module fetches current state during
``dry_run`` and reports the keys the write would drop, so the deletion is
visible *before* it is executed.

Everything here fails open. A preflight that cannot run must never block or
alter a write; it simply omits the ``impact`` block.
"""

from __future__ import annotations

from typing import Any

# Sentinels used by upstream redaction/secret-masking middleware. A masked
# value differs from the real one on every comparison, so treating those as
# changes would produce a permanent false "would_change" entry.
_MASK_TOKENS = ("REDACT", "******")

# Collection envelopes: a GET returning one of these is a list endpoint, not
# the single resource this write targets, so diffing against it is meaningless.
_COLLECTION_KEYS = ("items", "results", "data")

# Keeps the impact block bounded for MCP clients on pathologically large diffs.
_MAX_ENTRIES = 50

# Server-managed fields that always differ or are absent from a request body.
_IGNORED_KEYS = frozenset(
    {
        "_pagination",
        "created_time",
        "modified_time",
        "id",
        "org_id",
        "site_id",
        "msp_id",
        "for_site",
    }
)

# Methods whose bodies are interpreted as a merge over existing state. POST
# creates a new resource, so there is nothing to overwrite.
MERGE_METHODS = frozenset({"PUT", "PATCH"})


def _is_masked(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    upper = value.upper()
    return any(token in upper for token in _MASK_TOKENS)


def _walk(
    current: dict[str, Any],
    proposed: dict[str, Any],
    prefix: str,
    out: dict[str, list[str]],
    *,
    report_missing: bool,
) -> None:
    for key, current_value in current.items():
        if key in _IGNORED_KEYS:
            continue
        dotted = f"{prefix}{key}"
        if key not in proposed:
            # Top level merges, so an omitted key is preserved rather than
            # dropped. Below the top level the parent object is replaced, so
            # an omitted key really is deleted.
            if report_missing:
                out["would_remove"].append(dotted)
            continue
        proposed_value = proposed[key]
        if isinstance(current_value, dict) and isinstance(proposed_value, dict):
            _walk(current_value, proposed_value, f"{dotted}.", out, report_missing=True)
        elif current_value != proposed_value:
            if _is_masked(current_value) or _is_masked(proposed_value):
                continue
            out["would_change"].append(dotted)
    for key in proposed:
        if key not in _IGNORED_KEYS and key not in current:
            out["would_add"].append(f"{prefix}{key}")


def nested_replace_impact(
    current: dict[str, Any],
    proposed: dict[str, Any],
) -> dict[str, Any]:
    """Diff ``proposed`` against ``current`` under merge-top/replace-nested rules.

    Top-level keys absent from ``proposed`` are treated as preserved. Keys
    absent from a *nested* object that ``proposed`` does send are reported in
    ``would_remove`` -- those are the silent deletions worth warning about.

    Returns a dict with ``would_remove``, ``would_change`` and ``would_add``
    lists of dotted paths. Lists are truncated at :data:`_MAX_ENTRIES`.
    """
    out: dict[str, list[str]] = {
        "would_remove": [],
        "would_change": [],
        "would_add": [],
    }
    _walk(current, proposed, "", out, report_missing=False)
    result: dict[str, Any] = {}
    for key, values in out.items():
        if not values:
            continue
        values.sort()
        if len(values) > _MAX_ENTRIES:
            result[key] = values[:_MAX_ENTRIES]
            result[f"{key}_truncated"] = len(values) - _MAX_ENTRIES
        else:
            result[key] = values
    return result


def extract_resource(payload: Any) -> dict[str, Any] | None:
    """Pull the single-resource object out of a read-executor response.

    Returns ``None`` for errors, non-object payloads, and collection
    envelopes, none of which can be meaningfully diffed against a write body.
    """
    if not isinstance(payload, dict) or "error" in payload:
        return None
    status = payload.get("status_code")
    if isinstance(status, int) and not 200 <= status < 300:
        return None
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return None
    for key in _COLLECTION_KEYS:
        nested = data.get(key)
        if isinstance(nested, list):
            return None
        if key == "data" and isinstance(nested, dict):
            data = nested
    return data if isinstance(data, dict) else None


async def build_write_impact(
    read_executor: Any,
    method: str,
    path: str,
    headers: dict[str, str],
    body: Any,
) -> dict[str, Any] | None:
    """Fetch current state for ``path`` and report what the write would drop.

    Returns ``None`` -- omitting the impact block entirely -- whenever the
    analysis does not apply or cannot be completed. This is deliberately
    fail-open: preflight is an advisory safety net and must never prevent a
    write the caller is entitled to make.
    """
    if method.upper() not in MERGE_METHODS or not isinstance(body, dict) or not body:
        return None
    try:
        payload = await read_executor("GET", path, {}, dict(headers))
    except Exception:  # noqa: BLE001 - advisory only; never surface read faults
        return None
    current = extract_resource(payload)
    if current is None:
        return None
    impact = nested_replace_impact(current, body)
    if not impact:
        return None
    impact["source"] = f"GET {path}"
    if impact.get("would_remove"):
        impact["warning"] = (
            "This request sends nested objects that replace stored state "
            "wholesale, so the listed keys would be DELETED. Re-send each "
            "nested object complete (merge your change into current state) "
            "to keep them."
        )
    return impact
