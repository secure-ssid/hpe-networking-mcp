"""Reconcile OpenAPI lookup hits against generated MCP operations.

RAG ``lookup_api`` answers "does this endpoint exist in the specs". This
module answers the follow-up: is it a curated workflow, a generated tool,
disabled by the default router profile, or a protocol-only gap such as
Central Streaming over WSS.

The index is built from committed generated manifests. It never fetches
upstream and never claims a generated-only operation is missing.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections import OrderedDict, defaultdict
from functools import lru_cache
from typing import Any

from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import (
    load_manifest,
    manifest_exists,
)

DEFAULT_PLATFORMS = ("central", "glp")
_ENDPOINT_REF_RE = re.compile(
    r"^openapi_specs/[^#]+#(GET|POST|PUT|PATCH|DELETE)\s+(/\S+)$"
)
_VERSION_SEGMENT_RE = re.compile(r"^v\d+[a-z0-9]*$", re.IGNORECASE)
_EXACT_ENDPOINT_RE = re.compile(
    r"^\s*(GET|POST|PUT|PATCH|DELETE)\s+(/[^\s?#]*)\s*$",
    re.IGNORECASE,
)
_OPERATION_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{1,255}$")
_FAMILY_PAGE_CACHE: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
_FAMILY_PAGE_CACHE_MAX = 64

# Generated Central/GLP backends are opt-in. The recommended router profile
# (`HPE_MCP_TOOLSETS=central,glp,rag`) exposes curated tools plus RAG lookup,
# not the thousands of generated operations.
DEFAULT_PROFILE_GENERATED = {
    "central": "opt-in",
    "glp": "opt-in",
}

PROTOCOL_ONLY: tuple[dict[str, Any], ...] = (
    {
        "platform": "central",
        "family": "streaming",
        "classification": "protocol-only",
        "protocol": "wss",
        "summary": (
            "Central Streaming API delivers live telemetry over WebSocket, "
            "not a REST OpenAPI operation."
        ),
        "subscription": "Advanced",
        "generated_tool": None,
        "curated_tool": "central_collect_streaming_events",
        "endpoints": [
            "/network-monitoring/v1alpha1/ap-events",
            "/network-services/v1alpha1/audit-trail-events",
            "/network-services/v1alpha1/geofence",
            "/network-services/v1alpha1/location",
            "/network-services/v1alpha1/rssi-events",
        ],
        "documentation_sources": [
            "developer_docs/developer_arubanetworks_com_new-central_docs_streaming-api-connection-management.md",
            "developer_docs/developer_arubanetworks_com_new-central_docs_streaming-api-cloudevents.md",
            "developer_docs/developer_arubanetworks_com_new-central_docs_streaming-api-event-ap-monitoring.md",
            "developer_docs/developer_arubanetworks_com_new-central_docs_streaming-api-event-audit-trail.md",
            "developer_docs/developer_arubanetworks_com_new-central_docs_streaming-api-event-geofence.md",
            "developer_docs/developer_arubanetworks_com_new-central_docs_streaming-api-event-location.md",
            "developer_docs/developer_arubanetworks_com_new-central_docs_streaming-api-event-location-analytics.md",
        ],
    },
)


def family_id(path: str) -> str:
    """Return the first non-version path segment as the API family id."""
    parts = [part for part in str(path or "").split("/") if part]
    if not parts:
        return "unknown"
    if _VERSION_SEGMENT_RE.fullmatch(parts[0]) and len(parts) > 1:
        return parts[1]
    return parts[0]


def _operation_record(platform: str, operation: dict[str, Any]) -> dict[str, Any]:
    method = str(operation.get("method") or "").upper()
    path = str(operation.get("path") or "")
    return {
        "platform": platform,
        "family": family_id(path),
        "method": method,
        "path": path,
        "key": operation.get("key") or f"{method} {path}".strip(),
        "operation_id": operation.get("operation_id"),
        "generated_tool": operation.get("name"),
        "capability": operation.get("capability"),
        "source_file": operation.get("source_file"),
        "summary": operation.get("summary") or "",
        "classification": "generated-only",
        "router_profile": DEFAULT_PROFILE_GENERATED.get(platform, "unknown"),
    }


@lru_cache(maxsize=8)
def load_platform_index(platform: str) -> dict[str, dict[str, Any]]:
    """Return ``{METHOD path: record}`` for one committed generated manifest."""
    if not manifest_exists(platform):
        return {}
    manifest = load_manifest(platform)
    index: dict[str, dict[str, Any]] = {}
    for operation in manifest.get("operations") or []:
        if not isinstance(operation, dict):
            continue
        record = _operation_record(platform, operation)
        if record["method"] and record["path"]:
            index[f"{record['method']} {record['path']}"] = record
    return index


@lru_cache(maxsize=8)
def load_operation_id_index(platform: str) -> dict[str, dict[str, Any]]:
    """Return ``{operationId.lower(): record}`` for one generated manifest."""
    index: dict[str, dict[str, Any]] = {}
    for record in load_platform_index(platform).values():
        operation_id = record.get("operation_id")
        if operation_id:
            index[str(operation_id).lower()] = record
    return index


def platform_index_digest(platform: str) -> str:
    """Stable short digest of one platform's generated method/path keys."""
    index = load_platform_index(platform)
    blob = "\n".join(sorted(index))
    return hashlib.sha256(f"{platform}:{len(index)}\n{blob}".encode()).hexdigest()[:12]


def clear_caches() -> None:
    """Drop manifest and family-page caches (tests and digest invalidation)."""
    load_platform_index.cache_clear()
    load_operation_id_index.cache_clear()
    _FAMILY_PAGE_CACHE.clear()


def lookup_operation(
    method: str,
    path: str,
    *,
    platforms: tuple[str, ...] = DEFAULT_PLATFORMS,
) -> dict[str, Any] | None:
    key = f"{str(method).upper()} {path}"
    for platform in platforms:
        record = load_platform_index(platform).get(key)
        if record is not None:
            return dict(record)
    return None


def lookup_by_operation_id(
    operation_id: str,
    *,
    platforms: tuple[str, ...] = DEFAULT_PLATFORMS,
) -> dict[str, Any] | None:
    wanted = str(operation_id or "").lower()
    if not wanted:
        return None
    for platform in platforms:
        record = load_operation_id_index(platform).get(wanted)
        if record is not None:
            return dict(record)
    return None


def _looks_like_operation_id(value: str) -> bool:
    """Accept camelCase OpenAPI operationIds, not snake_case tool names."""
    if not _OPERATION_ID_RE.fullmatch(value) or "_" in value:
        return False
    letters = [char for char in value if char.isalpha()]
    if len(letters) < 6:
        return False
    return any(char.isupper() for char in letters) and any(
        char.islower() for char in letters
    )


def parse_exact_api_query(query: str) -> tuple[str, tuple[str, ...]] | None:
    """Parse a literal ``METHOD /path`` or camelCase operationId query."""
    stripped = (query or "").strip()
    if not stripped:
        return None
    match = _EXACT_ENDPOINT_RE.fullmatch(stripped)
    if match:
        return "endpoint", (match.group(1).upper(), match.group(2))
    if _looks_like_operation_id(stripped):
        return "operation_id", (stripped,)
    return None


def lookup_exact_query(
    query: str,
    *,
    platforms: tuple[str, ...] = DEFAULT_PLATFORMS,
) -> dict[str, Any] | None:
    """Resolve an exact API query to a generated-operation coverage record."""
    parsed = parse_exact_api_query(query)
    if parsed is None:
        return None
    kind, parts = parsed
    if kind == "endpoint":
        return lookup_operation(parts[0], parts[1], platforms=platforms)
    return lookup_by_operation_id(parts[0], platforms=platforms)


def _parse_endpoint_ref(hit: dict[str, Any]) -> tuple[str, str] | None:
    method = hit.get("method")
    path = hit.get("path")
    if isinstance(method, str) and isinstance(path, str) and path.startswith("/"):
        return method.upper(), path
    file_path = str(hit.get("file_path") or "")
    match = _ENDPOINT_REF_RE.match(file_path)
    if match:
        return match.group(1), match.group(2)
    return None


def annotate_lookup_hits(
    hits: list[dict[str, Any]],
    *,
    platforms: tuple[str, ...] = DEFAULT_PLATFORMS,
) -> list[dict[str, Any]]:
    """Attach generated-tool coverage to endpoint-shaped lookup hits.

    Schema/enum hits are returned unchanged. Missing generated matches stay
    classified as ``indexed-only`` so callers do not treat them as missing
    APIs without checking the generated backend.
    """
    annotated: list[dict[str, Any]] = []
    for hit in hits:
        item = dict(hit)
        parsed = _parse_endpoint_ref(item)
        if parsed is None:
            annotated.append(item)
            continue
        method, path = parsed
        coverage = lookup_operation(method, path, platforms=platforms)
        if coverage is None:
            item["coverage"] = {
                "classification": "indexed-only",
                "method": method,
                "path": path,
                "family": family_id(path),
                "generated_tool": None,
                "note": (
                    "Present in the OpenAPI index but not in the Central/GLP "
                    "generated manifests. Confirm platform before wrapping."
                ),
            }
        else:
            item["coverage"] = {
                "classification": coverage["classification"],
                "platform": coverage["platform"],
                "family": coverage["family"],
                "method": coverage["method"],
                "path": coverage["path"],
                "generated_tool": coverage["generated_tool"],
                "capability": coverage["capability"],
                "router_profile": coverage["router_profile"],
                "operation_id": coverage["operation_id"],
            }
        annotated.append(item)
    return annotated


def _family_summary(bucket: dict[str, Any]) -> str:
    capabilities = bucket["capabilities"]
    parts = [
        f"{capabilities[cap]} {cap}"
        for cap in ("read", "diagnostic", "write", "destructive")
        if capabilities.get(cap)
    ]
    cap_text = ", ".join(parts) if parts else "unclassified"
    samples = ", ".join(bucket["sample_tools"][:3])
    sample_clause = f" Sample tools: {samples}." if samples else ""
    return (
        f"{bucket['family']}: {bucket['operation_count']} generated operations "
        f"({cap_text}).{sample_clause} "
        f"Router profile: {bucket['router_profile']}."
    )


def _family_cache_get(key: tuple[Any, ...]) -> dict[str, Any] | None:
    cached = _FAMILY_PAGE_CACHE.get(key)
    if cached is None:
        return None
    _FAMILY_PAGE_CACHE.move_to_end(key)
    return copy.deepcopy(cached)


def _family_cache_put(key: tuple[Any, ...], value: dict[str, Any]) -> None:
    _FAMILY_PAGE_CACHE[key] = copy.deepcopy(value)
    _FAMILY_PAGE_CACHE.move_to_end(key)
    while len(_FAMILY_PAGE_CACHE) > _FAMILY_PAGE_CACHE_MAX:
        _FAMILY_PAGE_CACHE.popitem(last=False)


def list_families(
    platform: str = "central",
    *,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Compact API-family summaries for one platform's generated manifest."""
    bounded = max(1, min(int(limit), 200))
    start = max(0, int(offset))
    digest = platform_index_digest(platform)
    cache_key = (platform, bounded, start, digest)
    cached = _family_cache_get(cache_key)
    if cached is not None:
        return cached
    index = load_platform_index(platform)
    buckets: dict[str, dict[str, Any]] = {}
    for record in index.values():
        family = record["family"]
        bucket = buckets.setdefault(
            family,
            {
                "family": family,
                "platform": platform,
                "classification": "generated-only",
                "router_profile": DEFAULT_PROFILE_GENERATED.get(platform, "unknown"),
                "operation_count": 0,
                "capabilities": defaultdict(int),
                "sample_tools": [],
            },
        )
        bucket["operation_count"] += 1
        capability = record.get("capability") or "unknown"
        bucket["capabilities"][capability] += 1
        if len(bucket["sample_tools"]) < 3 and record.get("generated_tool"):
            bucket["sample_tools"].append(record["generated_tool"])

    families = []
    for name in sorted(buckets):
        bucket = buckets[name]
        families.append(
            {
                "family": bucket["family"],
                "platform": bucket["platform"],
                "classification": bucket["classification"],
                "router_profile": bucket["router_profile"],
                "operation_count": bucket["operation_count"],
                "capabilities": dict(bucket["capabilities"]),
                "sample_tools": bucket["sample_tools"],
                "summary": _family_summary(bucket),
            }
        )
    protocol = [dict(item) for item in PROTOCOL_ONLY if item["platform"] == platform]
    page = families[start : start + bounded]
    result = {
        "platform": platform,
        "family_count": len(families),
        "limit": bounded,
        "offset": start,
        "index_digest": digest,
        "families": page,
        "protocol_only": protocol,
        "note": (
            "Generated operations are discoverable via find_tool / lookup_api; "
            "they are not dumped into the default router catalog."
        ),
    }
    _family_cache_put(cache_key, result)
    return result
