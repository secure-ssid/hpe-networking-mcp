"""MCP server — Aruba/HPE documentation RAG tools (16 tools).

Covers: hybrid (vector + BM25) search over ingested Aruba Central developer
docs, tech docs, NAC docs, VSG docs, HTML tech docs, Junos CLI, Junos
EX/MX/QFX/SRX hardware and release-note prose, and Mist docs/API-reference/
product-update prose; exact API endpoint/schema/enum lookup via the SQLite
specs index with generated-tool coverage; compact Central/GLP API-family
summaries; exact structured security-advisory/lifecycle lookup, bounded
list/filter/pagination, an exact-only advisory<->lifecycle correlation, an
exact curated hardware datasheet catalog lookup (CX/EX/AP specs, not part of
the document corpus), bounded RAG index diagnostics (ingestion delta, source
freshness, citation completeness), corpus provenance for both the committed
OpenAPI corpus and the locally built prose index, local skills/runbook
browse+load helpers, and a search over the user's own local personal/internal
document collection (separate index, never shared).

Default backend is the embedded stack — LanceDB + fastembed, no servers
needed (`clone -> uv sync -> run`). Set HPE_MCP_RAG_BACKEND=redis for the
optional Redis Stack + Ollama server deployment (vector-only + source boost).
"""

import importlib.util
import json
import os
import re
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from hpe_networking_mcp import optional_deps
from hpe_networking_mcp.mcp_servers.shared import READ_ONLY, READ_ONLY_LOCAL, resolve_rag_backend
from hpe_networking_mcp.mcp_servers.skills import list_skills_payload, load_skill_payload
from hpe_networking_mcp.pipeline import artifact_contracts as contracts
from hpe_networking_mcp.pipeline.clients import (
    advisory_index,
    aoscx_release_index,
    hardware_catalog,
    hardware_specs,
    rag_cache,
    specs_index,
)
from hpe_networking_mcp.pipeline.clients import rag_diagnostics as rag_diagnostics_client
from hpe_networking_mcp.pipeline.clients.capability_coverage import (
    annotate_lookup_hits,
    list_families,
    parse_exact_api_query,
)

mcp = MCPServer("rag-core")

_BACKEND = resolve_rag_backend()

#: Set when ``HPE_MCP_RAG_BACKEND=redis`` is selected on an install without
#: the ``redis`` extra. The RAG tools then degrade with the install command
#: instead of the whole server failing to import -- every non-RAG tool the
#: router serves is unaffected by a missing vector backend.
_REDIS_MISSING: optional_deps.MissingOptionalDependency | None = None

if _BACKEND == "redis":
    from hpe_networking_mcp.pipeline.clients.ollama_client import OllamaClient

    _ollama = OllamaClient()
    _redis = None
    try:
        from hpe_networking_mcp.pipeline.clients.redis_client import (
            get_client as _get_redis_client,
        )
        from hpe_networking_mcp.pipeline.clients.redis_client import vector_search
    except ImportError as exc:
        _REDIS_MISSING = optional_deps.missing(
            "The Redis vector backend (HPE_MCP_RAG_BACKEND=redis)",
            module="redis",
            extra="redis",
        )
        _REDIS_MISSING.__cause__ = exc
    else:
        try:
            _redis = _get_redis_client()
            _redis.ping()
        except Exception:
            _redis = None
else:
    from hpe_networking_mcp.pipeline.clients import lance_client
    from hpe_networking_mcp.pipeline.clients.embed_client import EmbedClient

    _embedder = EmbedClient()  # lazy — the ONNX model loads on first query


def _cache_size(env_name: str, default: int) -> int:
    raw = os.getenv(env_name, str(default)).strip()
    try:
        return max(1, min(int(raw), 4096))
    except ValueError:
        return default


_SEARCH_CACHE = rag_cache.BoundedCache[
    tuple[str, str, str, int, str | tuple[str, ...] | None], list[dict[str, Any]]
](
    max_entries=_cache_size("HPE_MCP_RAG_CACHE_SIZE", 256)
)
_EMBED_CACHE = rag_cache.BoundedCache[tuple[str, str], tuple[float, ...]](
    max_entries=_cache_size("HPE_MCP_RAG_EMBED_CACHE_SIZE", 256)
)


def warm_up_rag() -> None:
    """Load the local embedding model on demand for long-lived hosts."""
    if _BACKEND != "redis":
        _embedder.warm_up()


if _BACKEND != "redis" and os.getenv("HPE_MCP_RAG_PREWARM", "").strip().casefold() in {
    "1",
    "true",
    "yes",
    "on",
}:
    warm_up_rag()

# Redis backend only — the LanceDB path replaces this static re-rank with
# hybrid BM25+vector RRF fusion (R5).
# Higher boost = preferred when scores are close.
# openapi_specs: ground truth for field schemas and valid enum values.
# developer_docs: official API reference and how-to guides (current product).
# vsg_docs: design guides — conceptually stable, best-practice focused.
# nac_docs: dedicated NAC portal docs.
# tech_docs / techdocs_html: product UI docs — useful but may lag API changes.
# Recalibrated (doubled) after the H13 cosine fix: similarity now spans the full
# 0–1 range (was effectively halved), so boosts double to keep the same relative gaps.
_SOURCE_BOOST: dict[str, float] = {
    "openapi_specs": 0.16,
    "mist_specs": 0.16,
    "developer_docs": 0.10,
    # Official Mist API reference prose (endpoints, webhooks, examples). Kept
    # below mist_specs so the OpenAPI JSON still wins exact field/enum ties.
    "mist_api_docs": 0.10,
    # Juniper howto prose is left unboosted deliberately. Giving mist_docs
    # developer_docs' 0.10 measurably cost eval mrr (0.654 -> 0.629) because
    # the fixtures are Aruba-only, so the harm is measurable while the benefit
    # is not. Within a Juniper query the ordering that matters — mist_specs
    # above mist_api_docs above mist_docs — already mirrors Aruba's
    # openapi_specs above developer_docs above tech_docs.
    "mist_docs": 0.0,
    "junos_cli": 0.0,
    "vsg_docs": 0.06,
    "nac_docs": 0.04,
    "tech_docs": 0.0,
    "techdocs_html": 0.0,
}

# The corpus covers two vendors, but boosts alone cannot tell them apart:
# every file under openapi_specs/ shares one source label, so Aruba's 26 specs
# and Juniper's mist.openapi.json both draw the top 0.16. A Mist question would
# then promote an Aruba schema over Juniper prose purely on that boost. Splitting
# the Mist spec into its own boost key keeps the ranking vendor-aware without
# re-ingesting the corpus under new source folders.
# The spec has shipped under two filenames: the legacy unpinned
# `mist.openapi.json` and the pinned `mist-openapi.json` that
# fetch_mist_openapi.py writes. Match both, or the canonical file silently
# ranks as an Aruba spec.
_MIST_SPEC_MARKERS = ("mist.openapi", "mist-openapi")

_SOURCE_VENDOR: dict[str, str] = {
    "openapi_specs": "aruba",
    "developer_docs": "aruba",
    "vsg_docs": "aruba",
    "nac_docs": "aruba",
    "tech_docs": "aruba",
    "techdocs_html": "aruba",
    "aos_techdocs": "aruba",
    "aoscx_release_notes": "aruba",
    "aoscx_guides": "aruba",
    "clearpass_guide": "aruba",
    "devhub": "aruba",
    "feature_navigator": "aruba",
    "security_advisories": "aruba",
    "lifecycle_notices": "aruba",
    "product_specs": "aruba",
    "mist_specs": "juniper",
    "mist_docs": "juniper",
    "mist_api_docs": "juniper",
    "junos_cli": "juniper",
    "mist_product_updates": "juniper",
    "junos_ex_hardware": "juniper",
    "junos_ex_release_notes": "juniper",
    "junos_mx_hardware": "juniper",
    "junos_mx_release_notes": "juniper",
    "junos_qfx_hardware": "juniper",
    "junos_qfx_release_notes": "juniper",
    "junos_srx_hardware": "juniper",
    "junos_srx_release_notes": "juniper",
    "juniper_lifecycle": "juniper",
    "juniper_security_advisories": "juniper",
    "juniper_kb": "juniper",
    "product_datasheets": "juniper",
}

# Brand-specific enough that a match is a deliberate signal, not incidental
# prose. Generic networking terms (vlan, radius, multicast) are absent on
# purpose — they say nothing about which vendor is being asked about.
_VENDOR_HINTS: dict[str, frozenset[str]] = {
    "juniper": frozenset(
        {
            "juniper",
            "mist",
            "junos",
            "marvis",
            "jvd",
            "mxedge",
            "tunterm",
            "apstra",
            "ex",
            "qfx",
            "srx",
            "ssr",
            "vjunos",
            "wxlan",
        }
    ),
    "aruba": frozenset(
        {
            "aruba",
            "central",
            "aos",
            "instant",
            "clearpass",
            "hpe",
            "greenlake",
            "glp",
            "iap",
            "cx",
            "arubaos",
            "airwave",
            "edgeconnect",
            "silverpeak",
        }
    ),
}

# Cross-vendor hits keep their retrieval score but lose their source boost and
# take this penalty. A penalty rather than a filter: vendor detection is a
# heuristic, so a genuinely strong cross-vendor match can still surface (Mist
# and Aruba docs legitimately reference each other in migration material).
_CROSS_VENDOR_PENALTY = 0.12

SourceFilter = str | tuple[str, ...] | None

_SOURCE_FILTER_RE = re.compile(r"^[a-z0-9_]+$")
_MAX_SOURCE_FILTERS = 20
_MAX_EVIDENCE_ANSWER_CHARS = 2400
_MAX_EVIDENCE_EXCERPT_CHARS = 600
_MAX_FOLLOW_UP_CONTEXT_CHARS = 2000
_SOFTWARE_VERSION_HINTS = {
    "code",
    "firmware",
    "image",
    "release",
    "release-notes",
    "software",
    "version",
}

_DOC_TYPE_TO_SOURCE: dict[str, str | tuple[str, ...]] = {
    "developer-docs": "developer_docs",
    "tech-docs": "tech_docs",
    "techdocs-html": "techdocs_html",
    "nac": "nac_docs",
    "vsg": "vsg_docs",
    "openapi": "openapi_specs",
    "product-openapi": "product_specs",
    "aos-techdocs": "aos_techdocs",
    "aoscx-release-notes": "aoscx_release_notes",
    "aoscx-guides": "aoscx_guides",
    "clearpass-guide": "clearpass_guide",
    "mist-docs": "mist_docs",
    "mist-product-updates": "mist_product_updates",
    "junos-ex-hardware": "junos_ex_hardware",
    "junos-ex-release-notes": "junos_ex_release_notes",
    "junos-mx-hardware": "junos_mx_hardware",
    "junos-mx-release-notes": "junos_mx_release_notes",
    "junos-qfx-hardware": "junos_qfx_hardware",
    "junos-qfx-release-notes": "junos_qfx_release_notes",
    "junos-srx-hardware": "junos_srx_hardware",
    "junos-srx-release-notes": "junos_srx_release_notes",
    "juniper-kb": "juniper_kb",
    "junos-cli": "junos_cli",
    "mist-api-docs": "mist_api_docs",
    "devhub": "devhub",
    "feature-navigator": "feature_navigator",
    "security-advisory": ("security_advisories", "juniper_security_advisories"),
    "lifecycle": ("lifecycle_notices", "juniper_lifecycle"),
    "product-datasheet": "product_datasheets",
}
_API_QUERY_HINTS = {
    "api",
    "endpoint",
    "enum",
    "field",
    "schema",
    "method",
    "url",
    "path",
    "payload",
    "request",
    "response",
}


def _clamp_top_k(value: int, max_value: int) -> int:
    return max(1, min(value, max_value))


def _shape(rows: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
    shaped = []
    for r in rows[:top_k]:
        hit: dict[str, Any] = {
            "text": r["text"][:600] + "…" if len(r["text"]) > 600 else r["text"],
            "source": r["source"],
            "doc_type": r.get("doc_type"),
            "file_path": r["file_path"],
            "score": round(r["score"], 4),
        }
        if r.get("also_in"):
            hit["also_in"] = r["also_in"]
        shaped.append(hit)
    return shaped


def _boost_key(hit: dict[str, Any]) -> str:
    """Boost-table key for a hit, splitting openapi_specs by vendor.

    Source is the ingest folder name, so every OpenAPI file lands under
    openapi_specs regardless of vendor. The Mist spec is identified by filename
    so it can carry Juniper's boost instead of Aruba's.
    """
    source = hit.get("source", "")
    if source == "openapi_specs" and any(
        marker in str(hit.get("file_path", "")) for marker in _MIST_SPEC_MARKERS
    ):
        return "mist_specs"
    return source


def _detect_vendor(query: str) -> str | None:
    """Infer the vendor a query is about, or None when it is ambiguous.

    Returns None when the query names both vendors or neither — a comparison or
    a generic networking question should rank on relevance alone rather than
    having one vendor's docs penalised on a guess.
    """
    tokens = {
        tok.strip(".,:;?!()[]{}\"'").lower()
        for tok in query.replace("/", " ").replace("-", " ").split()
    }
    matched = {vendor for vendor, hints in _VENDOR_HINTS.items() if tokens & hints}
    return matched.pop() if len(matched) == 1 else None


def _boost_sources(hits: list[dict[str, Any]], query: str = "") -> list[dict[str, Any]]:
    """Apply _SOURCE_BOOST to hybrid hits, then re-sort.

    The boosts are calibrated against cosine similarity's 0-1 range, but hybrid
    search returns RRF-fused scores that bunch around 0.01-0.03. Adding the
    boosts to those raw values would swamp relevance entirely and effectively
    sort by source, so the fused scores are min-max normalised across the
    candidate set first. That keeps the calibrated gaps between sources
    meaningful while preserving the relevance ordering within each source.

    Boosts rank sources by authority, which only holds within a vendor: the
    corpus spans Aruba and Juniper, so an unqualified boost lets the top-ranked
    Aruba spec outrank Juniper prose on a Mist question. When the query names
    one vendor, hits from the other forfeit their boost and take a penalty.
    """
    if not hits:
        return hits
    vendor = _detect_vendor(query)
    scores = [h.get("score", 0.0) for h in hits]
    lo, hi = min(scores), max(scores)
    span = hi - lo
    for h in hits:
        norm = (h.get("score", 0.0) - lo) / span if span else 1.0
        key = _boost_key(h)
        hit_vendor = _SOURCE_VENDOR.get(key)
        if vendor and hit_vendor and hit_vendor != vendor:
            h["score"] = norm - _CROSS_VENDOR_PENALTY
        else:
            h["score"] = norm + _SOURCE_BOOST.get(key, 0.0)
    parsed = parse_exact_api_query(query)
    needle = None
    if parsed and parsed[0] == "endpoint":
        method, path = parsed[1]
        needle = f"{method} {path}".lower()

    def _exact_path_hit(hit: dict[str, Any]) -> bool:
        if not needle:
            return False
        blob = f"{hit.get('file_path', '')} {hit.get('text', '')}".lower()
        return needle in blob

    hits.sort(key=lambda h: (not _exact_path_hit(h), -h.get("score", 0.0)))
    return hits


# feature_navigator and product_datasheets ship one file per hardware model
# with near-identical structure — every CX switch file repeats the same
# section headings ("## ACL", "## VXLAN", ...) and the same "Yes/No"
# boilerplate, so a query naming one model (e.g. "CX 8400") can be outscored
# by a *different* model's file that happens to share more generic content
# terms. _boost_sources cannot fix this: it ranks by source authority, and
# every file in the family carries the same source label. Matches an explicit
# vendor-prefixed model mention such as "CX 8400", "cx-8400", or "EX4400" —
# deliberately never a bare number, so "port 8400" or "VLAN 100" cannot
# trigger it.
_MODEL_TOKEN_RE = re.compile(r"\b(?:cx|ex)[\s-]?(\d{3,5})\b", re.IGNORECASE)
_RELEASE_TOKEN_RE = re.compile(r"\b(?:10|20)\.\d+(?:\.\d+)?\b")
_METADATA_SCOPE_RE = re.compile(
    r"\b(?:release[- ]notes?|version history|enhancements?|resolved issues?|"
    r"known issues?|caveats?|fundamentals?|cli reference|feature navigator|"
    r"feature support|support matrix)\b",
    re.IGNORECASE,
)

# Sources that ship one near-duplicate file per hardware model, where a
# file_path model match is a deliberate signal rather than incidental prose
# overlap (e.g. two unrelated docs both mentioning "2024").
_MODEL_FAMILY_SOURCES = frozenset({"feature_navigator", "product_datasheets", "product_specs"})

# Deliberately NOT calibrated like _SOURCE_BOOST (max 0.16, a small nudge that
# lets a clearly-more-relevant hit still win). Within one model family the raw
# relevance gap between siblings is mostly noise: every CX file repeats the
# same "## VXLAN\n- EVPN...: Yes" boilerplate, so min-max normalising a
# same-family candidate set turns a marginal raw-score difference (e.g.
# 0.0313 vs 0.0229 out of ~0.03) into a large normalised gap (1.0 vs 0.17) that
# looks decisive but is not — it is an artifact of normalising over 20+
# near-duplicate rows. A small additive nudge could not close that gap, so
# this is sized to always beat the largest possible non-matching combination
# (normalised 1.0 + the biggest _SOURCE_BOOST, 0.16) with margin to spare.
# Non-family sources are never touched by this at all, so a genuinely more
# authoritative hit from outside the family (an OpenAPI spec, curated
# hardware-spec entry, etc.) still competes on its own merits.
_MODEL_MATCH_BOOST = 1.2


def _detect_model_token(query: str) -> str | None:
    """Extract a normalised device-model number from a query, or None.

    Only fires on an explicit vendor-prefixed mention, never a bare number.
    Returns None when the query names zero or multiple distinct models — a
    comparison question ("CX 6400 vs CX 8400") must not silently boost only
    the first one mentioned, mirroring _detect_vendor's ambiguity rule.
    """
    models = {m.lower() for m in _MODEL_TOKEN_RE.findall(query)}
    return models.pop() if len(models) == 1 else None


def _query_metadata_filter(query: str, columns: set[str]) -> dict[str, str]:
    """Derive only high-confidence metadata filters from explicit query terms."""
    filters: dict[str, str] = {}
    vendor = _detect_vendor(query)
    if vendor and "vendor" in columns:
        filters["vendor"] = vendor

    # Model/release columns describe release-note and guide records, not every
    # generic support matrix or how-to page. Only push these filters when the
    # query explicitly names a release-oriented scope; otherwise a valid
    # record with null metadata would be filtered out before semantic search.
    if _METADATA_SCOPE_RE.search(query):
        model = _detect_model_token(query)
        if model and "model" in columns:
            filters["model"] = model

        releases = {match.group(0) for match in _RELEASE_TOKEN_RE.finditer(query)}
        if len(releases) == 1 and "release" in columns:
            filters["release"] = next(iter(releases))
    return filters


def _boost_model_match(hits: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Boost hits whose file_path names the exact model the query asked about.

    Runs after _boost_sources so it builds on the already-normalised scores.
    A no-op when the query does not name a single, specific model, or when no
    candidate file_path contains it — matching family hits are boosted,
    non-matching family hits and every non-family hit are left exactly as
    _boost_sources ranked them.
    """
    if not hits:
        return hits
    model = _detect_model_token(query)
    if not model:
        return hits
    for h in hits:
        if _boost_key(h) not in _MODEL_FAMILY_SOURCES:
            continue
        if model in str(h.get("file_path", "")).lower():
            h["score"] = h.get("score", 0.0) + _MODEL_MATCH_BOOST
    hits.sort(key=lambda h: h.get("score", 0.0), reverse=True)
    return hits


def _dedup_by_content(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse hits with identical content into one representative row.

    38% of the corpus is boilerplate repeated verbatim across many source files
    (license text, upgrade-procedure steps, overview headers). Without this
    step, a query like "AOS-CX upgrade procedure" returns 10 results whose text
    is character-for-character identical — only the ``file_path`` differs.

    Strategy:
    - Group by ``content_hash`` (exact duplicate detection).
    - Keep the highest-scored hit from each group.
    - Attach a ``also_in`` list of alternative ``file_path`` values so callers
      can still see all provenance paths without the result list being flooded.
    - Hits without a ``content_hash`` (legacy index rows) are treated as unique
      and pass through unchanged.

    Ordering is preserved: the merged list is sorted by the representative's
    score so ranking is unchanged after deduplication.
    """
    seen: dict[str, dict[str, Any]] = {}
    no_hash: list[dict[str, Any]] = []

    for hit in hits:
        ch = hit.get("content_hash")
        if not ch:
            no_hash.append(hit)
            continue
        if ch not in seen:
            seen[ch] = {**hit, "_alt_paths": []}
        else:
            existing = seen[ch]
            if hit.get("score", 0.0) > existing.get("score", 0.0):
                alt_paths = existing["_alt_paths"] + [existing["file_path"]]
                seen[ch] = {**hit, "_alt_paths": alt_paths}
            else:
                existing["_alt_paths"].append(hit["file_path"])

    deduped: list[dict[str, Any]] = []
    for hit in seen.values():
        alt_paths = hit.pop("_alt_paths", [])
        if alt_paths:
            hit = {**hit, "also_in": alt_paths[:5]}
        deduped.append(hit)

    result = deduped + no_hash
    result.sort(key=lambda h: h.get("score", 0.0), reverse=True)
    return result


def _degraded_optional_dep(
    exc: optional_deps.MissingOptionalDependency,
) -> list[dict[str, Any]]:
    """Render an uninstalled extra the same way an unbuilt index is rendered.

    Same reasoning as :func:`_degraded_spec_index`, same three keys, for the
    same reason: an empty list is a real answer ("the corpus was consulted and
    holds nothing") and an absent package consulted nothing. ``error`` is the
    full diagnostic naming the capability and the missing module; ``hint`` is
    the ``pip install`` alone, taken from the exception so the command is
    written once in ``optional_deps`` and cannot drift.
    """
    return [{"error": str(exc), "degraded": True, "hint": exc.remedy}]


def _search_lancedb(query: str, top_k: int, source_filter: SourceFilter) -> list[dict[str, Any]]:
    try:
        db = lance_client.connect()
        cache_key = (getattr(_embedder, "model_name", ""), rag_cache.normalize_query(query))
        cached_vector = _EMBED_CACHE.get(cache_key)
        if cached_vector is None:
            cached_vector = tuple(_embedder.embed_query(query))
            _EMBED_CACHE.set(cache_key, cached_vector)
        query_vector = list(cached_vector)
        # Fetch well beyond top_k so the source boost has candidates to promote —
        # authoritative-but-lower-ranked docs are typically just outside top_k,
        # and boosting a list already truncated to top_k can only reorder it.
        hits = lance_client.hybrid_search(
            db,
            query,
            query_vector,
            top_k=max(top_k * 6, 30),
            source_filter=source_filter,
            metadata_filter=_query_metadata_filter(query, lance_client.docs_columns(db)),
        )
        if not hits:
            # Metadata is intentionally conservative and can be absent from
            # legacy indexes or incomplete for a document family. A scoped
            # miss must not become a false "no documentation" answer.
            hits = lance_client.hybrid_search(
                db,
                query,
                query_vector,
                top_k=max(top_k * 6, 30),
                source_filter=source_filter,
            )
    except optional_deps.MissingOptionalDependency as exc:
        return _degraded_optional_dep(exc)
    except FileNotFoundError as exc:
        # Same reasoning as _degraded_spec_index: a missing prose index
        # consulted nothing, so it renders as degraded-with-remedy rather
        # than a bare error string. The hint is state-aware — a fresh clone
        # has no scraped corpus, and "run the build" is not a remedy there.
        return [{"error": str(exc), "degraded": True, "hint": _prose_remedy()}]
    except ValueError as exc:
        return [{"error": str(exc)}]
    hits = _boost_model_match(_boost_sources(hits, query), query)
    hits = _dedup_by_content(hits)
    return _shape(hits, top_k)


def _search_redis(query: str, top_k: int, source_filter: SourceFilter) -> list[dict[str, Any]]:
    if _REDIS_MISSING is not None:
        return _degraded_optional_dep(_REDIS_MISSING)
    if _redis is None:
        return [{"error": "Redis not available — is the Redis Stack server running?"}]

    query_vector = _ollama.embed_query(query)
    # Fetch more candidates so re-ranking has room to promote higher-priority sources
    candidates = vector_search(_redis, query_vector, top_k=top_k * 3, source_filter=source_filter)

    # Re-rank: boosted_score = raw_score + source_boost. Applies even under filters —
    # a filter narrows the candidate set, boosting still orders within it.
    # Redis returns cosine scores already in 0-1, the range the boosts are
    # calibrated for, so they are added directly with no normalisation. The
    # cross-vendor rule is shared with the LanceDB path so both backends rank
    # a Mist question the same way.
    vendor = _detect_vendor(query)
    for r in candidates:
        key = _boost_key(r)
        hit_vendor = _SOURCE_VENDOR.get(key)
        if vendor and hit_vendor and hit_vendor != vendor:
            r["score"] = r["score"] - _CROSS_VENDOR_PENALTY
        else:
            r["score"] = r["score"] + _SOURCE_BOOST.get(key, 0.0)
    candidates.sort(key=lambda r: r["score"], reverse=True)
    candidates = _boost_model_match(candidates, query)
    return _shape(candidates, top_k)


def _normalize_source_filter(source_filter: SourceFilter) -> SourceFilter:
    """Validate and normalize a comma-separated source filter.

    MCP clients expose this as a string, while internal callers may already
    provide a tuple. Normalizing both forms keeps the LanceDB and Redis paths
    on the same safe, bounded contract.
    """
    if not source_filter:
        return None

    raw_values = (source_filter,) if isinstance(source_filter, str) else tuple(source_filter)
    values: list[str] = []
    for raw in raw_values:
        if not isinstance(raw, str):
            raise ValueError(f"invalid source filter: {source_filter!r}")
        parts = raw.split(",")
        if any(not part.strip() for part in parts):
            raise ValueError(f"invalid source filter: {source_filter!r}")
        values.extend(part.strip() for part in parts)

    values = list(dict.fromkeys(values))
    if len(values) > _MAX_SOURCE_FILTERS or any(
        not _SOURCE_FILTER_RE.fullmatch(value) for value in values
    ):
        raise ValueError(f"invalid source filter: {source_filter!r}")
    return values[0] if len(values) == 1 else tuple(values)


@mcp.tool(annotations=READ_ONLY_LOCAL)
def compare_aoscx_releases(
    platform: str,
    from_version: str,
    to_version: str,
    sections: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Compare AOS-CX feature support and release-note changes exactly.

    Uses structured Feature Navigator snapshots for feature deltas and exact
    release-note file/range filtering for enhancements, resolved issues, and
    caveats. It does not use embeddings or semantic ranking.

    Args:
        platform: Switch platform, for example ``6100`` or ``CX 6100``.
        from_version: Baseline release/family, for example ``10.13``.
        to_version: Target release/family, for example ``10.16``.
        sections: Optional subset of ``features``, ``enhancements``,
                  ``resolved_issues``, and ``caveats``.
        limit: Combined results returned (default 50, range 1-200).
    """
    try:
        return aoscx_release_index.compare(
            platform=platform,
            from_version=from_version,
            to_version=to_version,
            sections=sections,
            limit=limit,
        )
    except (FileNotFoundError, ValueError) as exc:
        return {
            "platform": platform,
            "requested_range": {"from": from_version, "to": to_version},
            "errors": [str(exc)],
            "results": [],
            "count": 0,
            "truncated": False,
        }


@mcp.tool(annotations=READ_ONLY_LOCAL)
def search_docs(
    query: str,
    top_k: int = 5,
    source: str | None = None,
    doc_type: str | None = None,
) -> list[dict[str, Any]]:
    """Search Aruba/HPE network documentation.

    Hybrid (vector + keyword) search over developer guides, tech docs, and
    NAC/VSG guides. OpenAPI records stay in the exact SQLite `lookup_api`
    index rather than being embedded.

    Args:
        query:    Natural language question or keywords.
        top_k:    Results to return (default 5, range 1-20).
        source:   Filter by source folder — developer_docs, tech_docs, nac_docs,
                  vsg_docs, techdocs_html, aos_techdocs, aoscx_release_notes,
                  aoscx_guides, clearpass_guide, mist_docs, mist_api_docs,
                  mist_product_updates, junos_cli, junos_ex_hardware,
                  junos_ex_release_notes, junos_mx_hardware,
                  junos_mx_release_notes, junos_qfx_hardware,
                  junos_qfx_release_notes, junos_srx_hardware,
                  junos_srx_release_notes, security_advisories,
                  lifecycle_notices, juniper_lifecycle,
                  juniper_security_advisories, feature_navigator, or
                  product_datasheets.
        doc_type: DEPRECATED — use source instead.
    """
    top_k = _clamp_top_k(top_k, 20)

    # Map legacy doc_type to source name when source is not provided
    source_filter: SourceFilter = source
    if not source_filter and doc_type:
        source_filter = _DOC_TYPE_TO_SOURCE.get(doc_type)

    try:
        source_filter = _normalize_source_filter(source_filter)
    except ValueError as exc:
        return [{"error": str(exc)}]

    normalized_query = rag_cache.normalize_query(query)
    if _BACKEND == "redis":
        index_identity = "redis"
    else:
        try:
            index_identity = lance_client.index_identity(lance_client.connect())
        except Exception:
            index_identity = "unavailable"
    cache_key = (
        _BACKEND,
        index_identity,
        normalized_query,
        top_k,
        source_filter,
    )
    cached = _SEARCH_CACHE.get(cache_key)
    if cached is not None:
        return [dict(hit) for hit in cached]

    if _BACKEND == "redis":
        results = _search_redis(query, top_k, source_filter)
    else:
        results = _search_lancedb(query, top_k, source_filter)
    if results and not any("error" in hit for hit in results):
        _SEARCH_CACHE.set(cache_key, [dict(hit) for hit in results])
    return results


def _degraded_spec_index(exc: FileNotFoundError) -> list[dict[str, Any]]:
    """Render an unusable spec index so a model cannot mistake it for a miss.

    ``lookup_api`` returning ``[]`` is a real, useful answer: the specs were
    consulted and hold nothing, so the caller should fall back to prose
    search. A *missing* index consulted nothing. Rendering the two the same
    way is the worst outcome available here — a model handed ``[]`` concludes
    the endpoint does not exist and tells the operator so, and in a
    network-automation tool that fabrication is strictly worse than an error.

    So the marker is deliberately a non-empty list carrying ``degraded`` and a
    ``hint``, and it keeps ``error`` so callers written against the older
    shape (``ask_docs``' ``"error" not in hits[0]`` check) still route around
    it. The return type is unchanged: still ``list[dict[str, Any]]``.

    ``error`` is the full diagnostic and names the path that was looked for;
    ``hint`` is the remedy alone, taken from ``specs_index`` so the command is
    written once and cannot drift from the one in the exception. They are not
    the same string: repeating one text under two keys costs the model a
    second read and tells it nothing new.
    """
    return [
        {
            "error": str(exc),
            "degraded": True,
            "hint": specs_index.MISSING_INDEX_REMEDY,
        }
    ]


@mcp.tool(annotations=READ_ONLY_LOCAL)
def lookup_api(
    query: str,
    top_k: int = 10,
    source: str | None = None,
    platform: str | None = None,
    version: str | None = None,
    include_metadata: bool = False,
) -> list[dict[str, Any]]:
    """Exact Aruba/Mist API lookup — endpoints, schemas, fields, enum values.

    Authoritative, lossless answers from the parsed OpenAPI specs (SQLite, no
    server needed). Use this INSTEAD of search_docs for questions like "what
    enum values does field X accept", "which endpoint configures Y and with
    what method", or "what fields does schema Z have". Returns [] when the
    specs hold no confident answer — fall back to search_docs in that case.
    An unbuilt index is NOT that: it returns a single entry with
    ``degraded: true`` and a ``hint`` naming the build command. Treat that as
    "unknown, index absent", never as "no such endpoint".

    Args:
        query: Natural language question, exact ``METHOD /path``, or exact
               operationId (e.g. "auth-type enum values for an auth profile",
               "GET /network-monitoring/v1/sites-client-health", or
               "listSitesClientHealthV1").
        top_k: Results to return (default 10, range 1-20).
        source: Optional exact source family, such as ``openapi_specs`` or
                ``product_specs``.
        platform: Optional platform filter, such as ``central``, ``mist``,
                  ``aoscx``, or ``clearpass``.
        version: Optional exact software/API version filter.
        include_metadata: Include platform, version, API version, and source URL
                          provenance in each hit.
    """
    try:
        hits = specs_index.lookup(
            query,
            top_k=_clamp_top_k(top_k, 20),
            source=source,
            platform=platform,
            version=version,
            include_metadata=include_metadata,
        )
    except FileNotFoundError as exc:
        return _degraded_spec_index(exc)
    return annotate_lookup_hits(hits)


@mcp.tool(annotations=READ_ONLY_LOCAL)
def list_api_families(
    platform: str = "central",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Compact generated API-family summaries for Central or GLP.

    Discover a family (firmware, audit-log, network-config, …) and a few
    sample generated tools without loading the full generated catalog.
    Generated operations stay opt-in in the recommended router profile;
    use find_tool / lookup_api to reach a specific operation.

    Args:
        platform: ``central`` or ``glp`` (default central).
        limit: Families per page (default 50, range 1-200).
        offset: Families to skip (default 0).
    """
    name = (platform or "central").strip().lower()
    if name not in {"central", "glp"}:
        return {"error": f"unsupported platform {platform!r}; use central or glp"}
    return list_families(name, limit=limit, offset=offset)


@mcp.tool(annotations=READ_ONLY)
def lookup_advisory(
    product: str | None = None,
    cve: str | None = None,
    advisory_id: str | None = None,
    min_severity: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Look up exact security-advisory metadata from official indexed sources.

    Filter by product text, CVE, or advisory ID. Unlike semantic search, this
    returns structured severity, status, release dates, affected product names,
    CVEs, and authoritative source paths. At least one identifier is required.

    Args:
        product: Product/model/version text contained in the advisory.
        cve: Exact CVE identifier, such as CVE-2025-13914.
        advisory_id: Exact vendor advisory ID, such as HPESBNW04987.
        min_severity: Optional low, medium, high, or critical threshold.
        limit: Results to return (default 20, range 1-200).
    """
    try:
        return advisory_index.lookup_advisories(
            product=product,
            cve=cve,
            advisory_id=advisory_id,
            min_severity=min_severity,
            limit=max(1, min(limit, 200)),
        )
    except FileNotFoundError as exc:
        return [{"error": str(exc)}]


@mcp.tool(annotations=READ_ONLY)
def check_product_lifecycle(
    product: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Find official end-of-sale/end-of-life records for a product or SKU.

    Searches the structured HPE Networking and Juniper Mist/Apstra lifecycle
    tables and returns notice IDs, dates, product/replacement SKUs, source
    family, file path, and authoritative source URL.

    Coverage boundary: the HPE Networking notices are a historical archive
    (legacy HP/H3C/3Com/ProCurve categories; nothing published after 2020)
    plus a static Aruba hardware End of Sale PDF snapshot -- there is no
    reliable official machine-readable source yet for current, post-2020
    Aruba-branded lifecycle notices (see docs/source-lifecycle-coverage.md).
    Do not imply this table is exhaustive for current Aruba products; treat
    an empty result as "not found in these sources", not "still supported".
    """
    try:
        return advisory_index.lookup_lifecycle(
            product,
            limit=max(1, min(limit, 200)),
        )
    except FileNotFoundError as exc:
        return [{"error": str(exc)}]


@mcp.tool(annotations=READ_ONLY)
def list_advisories(
    product: str | None = None,
    cve: str | None = None,
    advisory_id: str | None = None,
    min_severity: str | None = None,
    source_family: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """List/paginate security advisories with exact filters — no identifier required.

    Companion to `lookup_advisory` (which requires an identifier): this
    lists and paginates across every advisory matching zero or more exact
    filters, and reports how many rows matched in total so a caller knows
    whether to page further.

    Args:
        product: Product/model/version text contained in the advisory.
        cve: Exact CVE identifier, such as CVE-2025-13914.
        advisory_id: Exact vendor advisory ID, such as HPESBNW04987.
        min_severity: Optional low, medium, high, or critical threshold.
        source_family: Exact source authority — security_advisories or
            juniper_security_advisories.
        since: Inclusive lower-bound date (YYYY-MM-DD) on the advisory's
            release date.
        until: Inclusive upper-bound date (YYYY-MM-DD) on the advisory's
            release date.
        limit: Rows per page (default 20, range 1-200).
        offset: Rows to skip for pagination (default 0).
    """
    try:
        return advisory_index.list_advisories(
            product=product,
            cve=cve,
            advisory_id=advisory_id,
            min_severity=min_severity,
            source_family=source_family,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}


@mcp.tool(annotations=READ_ONLY)
def list_lifecycle_events(
    product: str | None = None,
    product_sku: str | None = None,
    replacement_sku: str | None = None,
    category: str | None = None,
    event_type: str | None = None,
    source_family: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """List/paginate lifecycle notices with exact filters — no identifier required.

    Companion to `check_product_lifecycle` (which requires a product/SKU
    text match): this lists and paginates across every lifecycle record
    matching zero or more exact filters, with the same coverage boundary
    (see `check_product_lifecycle` and docs/source-lifecycle-coverage.md) —
    an empty result means "not found in these sources", not "still
    supported".

    Args:
        product: Free-text product/model search across the indexed record.
        product_sku: Exact product SKU in the record's parsed `product_skus`
            list (case-insensitive).
        replacement_sku: Exact replacement SKU in the record's parsed
            `replacement_skus` list (case-insensitive).
        category: Exact product category, e.g. Switches or Wireless.
        event_type: Exact lifecycle event type/state, e.g.
            end-of-sale/end-of-life.
        source_family: Exact source authority — lifecycle_notices or
            juniper_lifecycle.
        since: Inclusive lower-bound date (YYYY-MM-DD) on the published date.
        until: Inclusive upper-bound date (YYYY-MM-DD) on the published date.
        limit: Rows per page (default 20, range 1-200).
        offset: Rows to skip for pagination (default 0).
    """
    try:
        return advisory_index.list_lifecycle_events(
            product=product,
            product_sku=product_sku,
            replacement_sku=replacement_sku,
            category=category,
            event_type=event_type,
            source_family=source_family,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}


@mcp.tool(annotations=READ_ONLY)
def correlate_advisory_lifecycle(
    product: str | None = None,
    advisory_id: str | None = None,
    cve: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Link advisory product applicability to lifecycle records — exact only.

    For each matching advisory, its listed product/model strings are
    normalized (case/whitespace only) and compared against every lifecycle
    record's product/replacement SKUs. A link is reported only on exact
    normalized equality — this is never a fuzzy or semantic claim. An
    advisory product with no such match is reported under
    `unresolved_products`, not silently dropped and not implied "not
    affected" or "still supported".

    Args:
        product: Product/model/version text (at least one of product,
            advisory_id, or cve is required).
        advisory_id: Exact vendor advisory ID, such as HPESBNW04987.
        cve: Exact CVE identifier, such as CVE-2025-13914.
        limit: Advisories to correlate (default 20, range 1-200).
    """
    try:
        return advisory_index.correlate_advisory_lifecycle(
            product=product,
            advisory_id=advisory_id,
            cve=cve,
            limit=limit,
        )
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}


@mcp.tool(annotations=READ_ONLY)
def rag_diagnostics(include_ingestion_delta: bool = True) -> dict[str, Any]:
    """Report bounded RAG security/lifecycle index freshness and completeness.

    Combines three read-only, network-free diagnostics scoped to the
    structured advisory/lifecycle sources (not the full prose corpus):

    - `citation_completeness`: per source authority, what fraction of
      records have their key citation fields populated (advisory
      severity/date, lifecycle published date/SKUs, source URL, ID).
    - `source_freshness`: the latest `source_freshness_result` artifact from
      `scripts/check_security_lifecycle_drift.py`, reduced to per-status
      counts (fresh/stale/unavailable/changed/coverage_gap) plus each
      source's bounded entry. Reported as an error (not "fresh") until that
      script has produced an artifact.
    - `ingestion_delta`: new/changed/removed/unchanged content-hash counts
      for the security-advisory and lifecycle source families versus the
      current LanceDB docs table — no embedding, no writes.

    Never returns raw source bodies — only counts, statuses, and short
    bounded strings already stored in the persisted artifacts/tables.

    Args:
        include_ingestion_delta: set False to skip the slightly slower
            content-hash diff and return only freshness + citation counts.
    """
    result: dict[str, Any] = {}
    try:
        result["citation_completeness"] = advisory_index.citation_completeness()
    except FileNotFoundError as exc:
        result["citation_completeness"] = {"error": str(exc)}

    try:
        result["source_freshness"] = rag_diagnostics_client.freshness_summary()
    except (FileNotFoundError, contracts.ArtifactValidationError) as exc:
        result["source_freshness"] = {"error": str(exc)}

    if include_ingestion_delta:
        try:
            result["ingestion_delta"] = rag_diagnostics_client.ingestion_delta()
        except (FileNotFoundError, ValueError) as exc:
            result["ingestion_delta"] = {"error": str(exc)}

    return result


# ---------------------------------------------------------------------------
# Corpus provenance — what backed an answer, how fresh, under what licence
# ---------------------------------------------------------------------------

#: Where a locally built prose index lives. Mirrors ``lance_client.DATA_DIR``
#: and its ``docs`` table *without* importing that module: ``lance_client`` is
#: only imported on the LanceDB backend, and this tool has to answer the same
#: way under ``HPE_MCP_RAG_BACKEND=redis``. Read through the module attribute
#: so a test can point it somewhere else.
PROSE_DATA_DIR = specs_index.ROOT / "data"
#: Where the scraped prose corpus lives before ingestion. Read through the
#: module attribute for the same reason as PROSE_DATA_DIR.
PROSE_SOURCES_DIR = specs_index.ROOT / "ingestion" / "sources"
PROSE_DOCS_INDEX_NAME = "docs.lance"
#: Written by ``scripts/package_indexes.py --write-local-manifests``. It is the
#: only on-disk record of *what* went into the built prose index and when each
#: source was last refreshed; the index directory itself says neither.
PROSE_INDEX_MANIFEST_NAME = "INDEX-MANIFEST.json"

#: Restoring 23 MB that is already committed is a different command from
#: building an index over it, so the two remedies are never interchanged.
VENDOR_CORPUS_REMEDY = (
    "the vendored OpenAPI corpus is committed to this repository — restore it "
    "with `git checkout -- vendor/openapi`, or re-fetch and re-verify every "
    "pin with `python scripts/vendor_openapi_corpus.py`"
)
#: A corpus that is present but incomplete is a third state, and it is the one
#: an operator is most likely to misdiagnose: the remaining documents still
#: answer, so the instinct is to blame the index. Restoring named files that
#: git already holds is neither a rebuild nor a scrape, and the text says so.
VENDOR_FILES_MISSING_REMEDY = (
    "the named documents are committed and digest-pinned in "
    "vendor/openapi/MANIFEST.json — restore them with "
    "`git checkout -- vendor/openapi`; nothing here needs re-fetching, "
    "scraping or a network, and the documents still on disk keep answering "
    "meanwhile"
)
PROSE_CORPUS_REMEDY = (
    "build the prose index with `uv run python ingestion/ingest_docs.py` — the "
    "corpus under ingestion/sources/ is scraped vendor documentation and is "
    "deliberately not distributed"
)
#: A fresh clone holds no scraped sources at all, so the build command alone
#: is not a remedy there -- it would ingest nothing. The two states get two
#: remedies, and `_prose_remedy` picks between them from the on-disk corpus.
PROSE_CORPUS_FETCH_REMEDY = (
    "ingestion/sources/ holds no scraped corpus on this install — fetch the "
    "declared sources with `python scripts/refresh_rag_sources.py "
    "--refresh-sources` (network; see docs/getting-started.md), then build "
    "with `uv run python ingestion/ingest_docs.py`. Prose retrieval is "
    "optional: exact API lookup (`lookup_api`) builds offline with "
    "`python scripts/build_spec_index.py` and needs neither"
)
PROSE_MANIFEST_REMEDY = (
    "describe the built index with "
    "`uv run python scripts/package_indexes.py --write-local-manifests`"
)
_REDIS_PROSE_NOTE = (
    "HPE_MCP_RAG_BACKEND=redis keeps the prose corpus inside the Redis server "
    "rather than in data/docs.lance; this tool reports the on-disk index only, "
    "so freshness for a Redis corpus has to come from that deployment"
)

#: The extra whose absence turns prose retrieval off entirely. The default
#: install ships neither, so "no corpus" and "no retrieval stack" are two
#: distinct facts and both are reported.
_PROSE_MODULE, _PROSE_EXTRA = ("redis", "redis") if _BACKEND == "redis" else (
    "lancedb",
    "ingestion",
)

#: Manifest keys that pin a document to a re-fetchable upstream. Group A (the
#: 30 New Central documents) pins through the ReadMe api-registry; group B
#: (mist.openapi.json) pins to a git commit. See vendor/openapi/NOTICE.md.
_PIN_KEYS = ("registry_id", "registry_sha256", "upstream_repo", "upstream_commit")

#: A missing-file list is a bug report, not a corpus dump.
_MAX_MISSING_FILES = 20


def _repo_path(path: Path) -> str:
    try:
        return str(path.relative_to(specs_index.ROOT))
    except ValueError:
        return str(path)


def _load_json(path: Path) -> tuple[Any, str | None]:
    """Parse ``path``, returning the reason instead of raising.

    Provenance is what a caller reaches for when something already looks
    wrong. A truncated manifest raising out of the tool would replace the one
    answer that could explain the state of the install with a stack trace.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"{_repo_path(path)} is not present"
    except (OSError, ValueError) as exc:
        return None, f"{_repo_path(path)} is unreadable: {exc}"


def _vendor_document(entry: Any) -> dict[str, Any] | None:
    """One manifest entry as a provenance row, or None if it says nothing."""
    if not isinstance(entry, dict):
        return None
    name = Path(str(entry.get("file") or "")).name
    if not name:
        return None
    document: dict[str, Any] = {
        "file": name,
        "title": entry.get("title"),
        "source_url": entry.get("source_url"),
        "sha256": entry.get("sha256"),
        "fetched": entry.get("fetched"),
        "license": entry.get("license"),
        "api_paths": entry.get("path_count"),
    }
    if pin := {key: entry[key] for key in _PIN_KEYS if entry.get(key)}:
        document["pin"] = pin
    return document


def _license_groups(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-licence counts, verbatim and never merged.

    ``vendor/openapi`` holds two regimes: 30 proprietary-but-redistributable
    HPE documents and one MIT Juniper document. Collapsing them into a single
    line would let a redistributor read "MIT" over material that is not MIT,
    which is the one claim this corpus must never make. The text is the
    manifest's own, so it cannot drift from NOTICE.md.
    """
    groups: dict[Any, dict[str, Any]] = {}
    for document in documents:
        text = document["license"]
        group = groups.setdefault(
            text, {"license": text, "document_count": 0, "api_paths": 0}
        )
        group["document_count"] += 1
        group["api_paths"] += int(document["api_paths"] or 0)
    return sorted(
        groups.values(),
        key=lambda group: (-group["document_count"], str(group["license"] or "")),
    )


def _selected_document_name(spec: str) -> str:
    """The document filename inside a ``lookup_api`` ``file_path``.

    Hits come back as ``openapi_specs/<file>#<ref>``, so a caller holding one
    can hand it straight back without having to parse it first.
    """
    return Path(spec.split("#", 1)[0].strip()).name.casefold()


def _spec_index_state() -> dict[str, Any]:
    db_path = Path(specs_index.DB_PATH)
    state: dict[str, Any] = {"built": db_path.is_file(), "path": _repo_path(db_path)}
    if not state["built"]:
        state["remedy"] = specs_index.MISSING_INDEX_REMEDY
    return state


def _vendor_provenance(detail: bool, spec: str | None) -> dict[str, Any]:
    """Provenance for the committed OpenAPI corpus behind ``lookup_api``.

    Three states are kept apart on purpose, because each has a different fix,
    and each carries its own remedy rather than borrowing another's: the
    corpus is absent (``available: False`` + ``remedy``), the corpus is
    present but documents it declares are missing from disk
    (``files_missing`` + ``remedy``), and the corpus is present but no index
    was built over it (``index.built`` + ``index.remedy``).

    ``available`` answers "did anything back this answer", not "is the corpus
    whole": a partly restored corpus stays ``True`` because the documents
    still on disk genuinely serve ``lookup_api``. ``files_missing`` is how a
    caller weighs that, so it is always present -- an empty list is the
    positive statement that nothing declared is gone.
    """
    corpus_dir = specs_index.VENDOR_OPENAPI_DIR
    manifest_path = corpus_dir / specs_index.VENDOR_MANIFEST_NAME
    common = {
        "corpus": _repo_path(corpus_dir),
        "backs": ["lookup_api", "list_api_families"],
        "index": _spec_index_state(),
    }
    data, error = _load_json(manifest_path)
    entries = data.get("specs") if isinstance(data, dict) else None
    entries = entries if isinstance(entries, list) else []
    documents = [row for row in map(_vendor_document, entries) if row is not None]
    unreadable = len(entries) - len(documents)
    if not documents:
        return {
            "available": False,
            "error": error
            or f"{_repo_path(manifest_path)} declares no readable vendored document",
            "remedy": VENDOR_CORPUS_REMEDY,
            **common,
        }

    missing = [row["file"] for row in documents if not (corpus_dir / row["file"]).is_file()]
    fetched = sorted(row["fetched"] for row in documents if row["fetched"])
    section: dict[str, Any] = {
        "available": True,
        "document_count": len(documents),
        "api_paths": sum(int(row["api_paths"] or 0) for row in documents),
        "fetched": {"earliest": fetched[0], "latest": fetched[-1]} if fetched else None,
        "licenses": _license_groups(documents),
        "files_missing": missing[:_MAX_MISSING_FILES],
        **common,
    }
    if len(missing) > _MAX_MISSING_FILES:
        section["files_missing_count"] = len(missing)
    if missing:
        section["remedy"] = VENDOR_FILES_MISSING_REMEDY
    if unreadable:
        section["unreadable_entries"] = unreadable
    notice = corpus_dir / "NOTICE.md"
    if notice.is_file():
        section["notice"] = _repo_path(notice)

    if spec is None and not detail:
        section["detail_hint"] = (
            "call with detail=True for every document's source_url, sha256, fetch "
            "date, licence and upstream pin, or spec=<file> for one"
        )
        return section
    if spec is None:
        section["documents"] = documents
        return section
    wanted = _selected_document_name(spec)
    section["documents"] = [row for row in documents if row["file"].casefold() == wanted]
    if not section["documents"]:
        section["note"] = (
            f"the corpus holds no document named {spec!r} — call with detail=True "
            "to list every document it does hold"
        )
    return section


def _prose_backend_installed() -> bool:
    try:
        return importlib.util.find_spec(_PROSE_MODULE) is not None
    except (ImportError, ValueError):
        return False


def _prose_corpus_populated() -> bool:
    """True when at least one scraped source folder holds a file.

    The fetch/build remedy split turns on this: ``ingest_docs.py`` over an
    empty ``ingestion/sources/`` ingests nothing, so "run the build" is only
    a remedy once something is there to build from. One populated folder is
    enough — the build's own required-sources guard reports anything still
    missing, and duplicating that list here would drift from it.
    """
    try:
        if not PROSE_SOURCES_DIR.is_dir():
            return False
        return any(
            child.is_file()
            for folder in PROSE_SOURCES_DIR.iterdir()
            if folder.is_dir()
            for child in folder.iterdir()
        )
    except OSError:
        return False


def _prose_remedy() -> str:
    remedy = (
        PROSE_CORPUS_REMEDY if _prose_corpus_populated() else PROSE_CORPUS_FETCH_REMEDY
    )
    if _prose_backend_installed():
        return remedy
    return f"{optional_deps.install_remedy(_PROSE_EXTRA)}, then {remedy}"


def _prose_sources(manifest: Any) -> list[dict[str, Any]] | None:
    """Per-source rows from the index manifest, or None when it describes none.

    ``None`` rather than ``[]``: an empty list would read as "the index was
    built and holds nothing from any source", which is a different — and, on a
    machine that has one, false — statement from "nothing here describes it".
    """
    sources = manifest.get("sources") if isinstance(manifest, dict) else None
    if not isinstance(sources, dict):
        return None
    rows = [
        {
            "source": name,
            "indexed_chunks": detail.get("indexed_chunk_count"),
            "last_refreshed_at": detail.get("last_refreshed_at"),
            "required": bool(detail.get("required")),
        }
        for name, detail in sorted(sources.items())
        if isinstance(detail, dict)
    ]
    return rows or None


def _prose_provenance() -> dict[str, Any]:
    """Provenance for the locally built prose index behind ``ask_docs``.

    Absent by default and legitimately so — the corpus is scraped vendor
    documentation this project does not redistribute — so absence is reported
    with the command that would produce it, never as an empty corpus.
    """
    index_path = PROSE_DATA_DIR / PROSE_DOCS_INDEX_NAME
    section: dict[str, Any] = {
        "available": index_path.exists(),
        "index_path": _repo_path(index_path),
        "backend": _BACKEND,
        "retrieval_installed": _prose_backend_installed(),
        "backs": ["search_docs", "ask_docs"],
        "sources": None,
    }
    if _BACKEND == "redis":
        section["note"] = _REDIS_PROSE_NOTE
    if not section["available"]:
        section["remedy"] = _prose_remedy()
        return section

    manifest_path = PROSE_DATA_DIR / PROSE_INDEX_MANIFEST_NAME
    manifest, error = _load_json(manifest_path)
    if error is not None:
        section["error"] = error
        section["remedy"] = PROSE_MANIFEST_REMEDY
        return section
    section["manifest"] = _repo_path(manifest_path)
    artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else None
    built = artifacts.get(PROSE_DOCS_INDEX_NAME) if isinstance(artifacts, dict) else None
    section["built_at"] = built.get("modified_at") if isinstance(built, dict) else None
    section["sources"] = _prose_sources(manifest)
    if section["sources"] is None:
        section["remedy"] = PROSE_MANIFEST_REMEDY
    return section


@mcp.tool(annotations=READ_ONLY_LOCAL)
def corpus_provenance(
    detail: bool = False,
    spec: str | None = None,
) -> dict[str, Any]:
    """Report what material backs an answer — corpus, freshness, licence, digest.

    Use this to weigh a `lookup_api`, `search_docs` or `ask_docs` answer
    instead of trusting it: a spec fetched before a feature shipped cannot
    describe it, and a document under a proprietary licence cannot be
    redistributed on the strength of this repository's MIT licence.

    Two corpora, reported together because a caller cannot tell which one
    served it:

    - `api_specs` — the committed, digest-pinned `vendor/openapi` corpus that
      backs `lookup_api`. Present in every clone and every image; needs no
      network and no local build.
    - `prose_docs` — the locally built document index that backs `search_docs`
      and `ask_docs`. Scraped vendor documentation, deliberately not
      distributed, so it is normally absent and says so with the command that
      builds it.

    Three absences stay three answers: no corpus (`available: false`), a
    corpus whose declared files are gone (`files_missing`), and a corpus with
    no index built over it (`index.built: false`) each carry their own remedy.

    Args:
        detail: return every document's source URL, SHA-256, fetch date,
            licence and upstream pin. Off by default — the summary is a few
            hundred tokens, the full corpus is several thousand.
        spec: return one document instead of all of them. Accepts a bare
            filename or a `lookup_api` `file_path` such as
            `openapi_specs/mist.openapi.json#Wlan`.
    """
    return {
        "api_specs": _vendor_provenance(detail=detail, spec=spec),
        "prose_docs": _prose_provenance(),
    }


_CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,}\b", re.IGNORECASE)
_ADVISORY_ID_RE = re.compile(r"\bHPESBNW\d{4,}\b", re.IGNORECASE)


def _is_api_question(question: str) -> bool:
    tokens = {
        tok.strip(".,:;?!()[]{}\"'").lower()
        for tok in question.replace("/", " ").replace("-", " ").split()
    }
    return bool(tokens & _API_QUERY_HINTS)


def _extract_exact_identifier(question: str) -> dict[str, str] | None:
    """Return an exact lookup_advisory kwarg found in the question, if any.

    Only an unambiguous, literal CVE ID or vendor advisory ID triggers exact
    routing — never a guessed/extracted product name, which would risk a
    wrong (fuzzy) filter presented as authoritative.
    """
    cve_match = _CVE_RE.search(question)
    if cve_match:
        return {"cve": cve_match.group(0).upper()}
    advisory_match = _ADVISORY_ID_RE.search(question)
    if advisory_match:
        return {"advisory_id": advisory_match.group(0).upper()}
    return None


def _summarize_advisory(hit: dict[str, Any]) -> str:
    """Bounded, non-body summary for an ask_docs answer over lookup_advisory hits."""
    parts = [str(hit.get("advisory_id") or hit.get("title") or "advisory")]
    if hit.get("title") and hit.get("advisory_id"):
        parts.append(f"— {hit['title']}")
    if hit.get("severity"):
        parts.append(f"(severity: {hit['severity']})")
    if hit.get("current_release"):
        parts.append(f"current release: {hit['current_release']}")
    text = " ".join(parts)
    return text[:900] + "…" if len(text) > 900 else text


def _citation(hit: dict[str, Any]) -> dict[str, Any]:
    citation = {
        "file_path": hit.get("file_path"),
        "source": hit.get("source") or hit.get("source_family"),
        "doc_type": hit.get("doc_type"),
        "score": hit.get("score"),
    }
    for key in (
        "source_url",
        "heading_breadcrumb",
        "advisory_id",
        "severity",
        "status",
        "current_release",
        "notice_id",
        "published",
        "category",
        "event_type",
        "platform",
        "version",
        "api_version",
    ):
        if hit.get(key) is not None:
            citation[key] = hit[key]
    for key in ("cves", "product_skus", "replacement_skus"):
        value = hit.get(key)
        if isinstance(value, list) and value:
            citation[key] = value[:5]
    return citation


def _evidence_text(hit: dict[str, Any], mode: str) -> str:
    if mode == "lookup_advisory":
        return _summarize_advisory(hit)
    return str(hit.get("text", "")).strip()


def _evidence_boundary_note(hits: list[dict[str, Any]]) -> str:
    """Flag when merged evidence spans multiple sources/platforms/versions.

    Bounded multi-hit synthesis can silently blend excerpts that do not
    actually agree with each other -- e.g. two hits from different AOS-CX
    release trains, or one Central and one Mist source answering the same
    natural-language question differently. Returning one smoothed-over
    answer in that case would misrepresent the evidence as a single
    unambiguous authority. This adds an explicit, short caveat instead of
    silently merging when the underlying hits disagree on identity.
    """
    sources = {
        h.get("source") or h.get("source_family")
        for h in hits
        if h.get("source") or h.get("source_family")
    }
    platforms = {h.get("platform") for h in hits if h.get("platform")}
    versions = {
        h.get("version") or h.get("api_version")
        for h in hits
        if h.get("version") or h.get("api_version")
    }
    if len(versions) > 1 or len(platforms) > 1 or len(sources) > 1:
        return (
            "Boundary: these excerpts may span different sources, platforms, "
            "or software versions -- verify applicability to your deployment "
            "before acting on them.\n\n"
        )
    return ""


def _bounded_evidence_answer(
    hits: list[dict[str, Any]],
    mode: str,
    limit: int,
) -> tuple[str, list[dict[str, Any]]]:
    """Build a bounded answer from distinct retrieved evidence excerpts."""
    evidence: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()

    for hit in hits[:limit]:
        text = _evidence_text(hit, mode)
        fingerprint = " ".join(text.casefold().split())
        if not fingerprint or fingerprint in seen:
            continue
        seen.add(fingerprint)
        evidence.append((text, hit))

    if not evidence:
        return "No matching local documentation was found.", []

    if len(evidence) == 1:
        text, hit = evidence[0]
        answer = text[:900] + "…" if len(text) > 900 else text
        return answer, [hit]

    boundary_note = _evidence_boundary_note([hit for _, hit in evidence])
    header = "Retrieved evidence excerpts:\n"
    labels = [f"[{index}] " for index in range(1, len(evidence) + 1)]
    fixed_chars = (
        len(boundary_note) + len(header) + sum(map(len, labels)) + 2 * (len(evidence) - 1)
    )
    excerpt_limit = min(
        _MAX_EVIDENCE_EXCERPT_CHARS,
        max(1, (_MAX_EVIDENCE_ANSWER_CHARS - fixed_chars) // len(evidence)),
    )

    blocks = []
    selected = []
    for label, (text, hit) in zip(labels, evidence, strict=True):
        excerpt = text
        if len(excerpt) > excerpt_limit:
            excerpt = excerpt[: excerpt_limit - 1].rstrip() + "…"
        blocks.append(f"{label}{excerpt}")
        selected.append(hit)

    return boundary_note + header + "\n\n".join(blocks), selected


@mcp.tool(annotations=READ_ONLY_LOCAL)
def lookup_hardware_specs(
    model: str,
) -> dict[str, Any]:
    """Look up series-level hardware specifications for switches and APs.

    Exact, curated catalog lookup (no RAG search) — use this INSTEAD of
    ask_docs/search_docs for hardware specification questions. Returns switching
    capacity, throughput, stacking (VSF/Virtual Chassis), port configurations,
    PoE wattage, uplinks, architecture, and routing/security features for
    Aruba CX (6000, 6100, 6200, 6300, 6400, 8325, 8360, 10000), Juniper EX
    (2300, 4000, 4100, 4400, 4650), Aruba APs (635), and Mist APs (45).

    Entries describe a switch/AP *series* and carry no source URL, so they
    cannot confirm an ordering part number; use search_hardware_catalog for a
    SKU.

    Args:
        model: Hardware model identifier, e.g. "cx6300", "ex4000", "ex4400",
            "8360", or "ap635".

    On a miss, returns ``{"ok": False, "available_models": [...]}`` listing every
    catalogued key instead of raising, so a caller can retry with a valid model.
    """
    key = hardware_specs.detect_hardware_query(model) or model.lower().strip()
    spec = hardware_specs.get_hardware_specs(key)
    if not spec:
        return {
            "ok": False,
            # "not_found" maps to HTTP 404 in ResponseEnvelopeMiddleware. Without
            # it a resolved "this model isn't catalogued" answer fell through to
            # the generic 500 fallback and was reported to clients as a server
            # fault with "retrying may help" -- advice that can only waste calls,
            # since the result is deterministic.
            "status": "not_found",
            "error": f"Hardware model '{model}' not found in hardware specifications catalog.",
            "available_models": sorted(hardware_specs.HARDWARE_CATALOG.keys()),
            "guidance": (
                "This catalog is series-level (e.g. 'cx6300'), so it holds no "
                "ordering part numbers. To resolve a SKU such as JL658A use "
                "search_hardware_catalog."
            ),
        }
    return {
        "ok": True,
        "model": key,
        "specs": spec,
        "formatted": hardware_specs.format_hardware_specs_markdown(key),
    }


def _contextual_question(question: str, context: str | None) -> str:
    """Combine a follow-up with a bounded prior-turn summary for retrieval."""
    question = question.strip()
    context = (context or "").strip()
    if not context:
        return question
    return (
        "Prior conversation context:\n"
        f"{context[:_MAX_FOLLOW_UP_CONTEXT_CHARS]}\n\n"
        f"Follow-up question:\n{question}"
    )


def _is_software_version_question(question: str) -> bool:
    tokens = {
        token.strip(".,:;?!()[]{}\"'").casefold()
        for token in question.replace("/", " ").replace("-", " ").split()
    }
    return bool(tokens & _SOFTWARE_VERSION_HINTS) or bool(
        re.search(r"\b(?:10|20)\.\d+(?:\.\d+)?\b", question)
    )


@mcp.tool(annotations=READ_ONLY_LOCAL)
def ask_docs(
    question: str,
    top_k: int = 3,
    source: str | None = None,
    context: str | None = None,
) -> dict[str, Any]:
    """Return a compact cited answer from local docs/API indexes.

    Token-saving companion to `search_docs`: it returns the shortest useful
    extractive answer plus citations instead of dumping multiple long chunks.
    For an ambiguous follow-up, pass a short standalone summary of the prior
    turn in ``context``; it is combined with the question for exact routing
    and document retrieval.
    A question containing a literal CVE ID or vendor advisory ID consults
    `lookup_advisory` first (exact, never a guessed product filter);
    otherwise API-shaped questions consult `lookup_api` first; both fall
    back to prose RAG when no exact match exists.
    """
    k = max(1, min(top_k, 5))
    retrieval_question = _contextual_question(question, context)
    mode = "search_docs"
    hits: list[dict[str, Any]] = []

    if source is None and hardware_catalog.is_catalog_query(question):
        catalog_result = hardware_catalog.search(question, include_specs=False, limit=k)
        if catalog_result.get("ok"):
            catalog_hits = catalog_result.get("results") or []
            return {
                "answer": hardware_catalog.format_compact_answer(catalog_result),
                "citations": [
                    {
                        "file_path": f"hardware_catalog:{item['sku']}",
                        "source": "hardware_catalog",
                        "doc_type": "hardware-catalog",
                        "score": 1.0,
                        "source_url": item["source"]["url"],
                    }
                    for item in catalog_hits
                ],
                "mode": "hardware_catalog",
            }
        # An index miss after a real SKU/configuration request should not be
        # replaced with semantically similar prose. It needs more product
        # traits, or its local catalog needs building/refreshing.
        if catalog_result.get("match_type") == "no_match" or catalog_result.get("hint"):
            return {
                "answer": hardware_catalog.format_compact_answer(catalog_result),
                "citations": [],
                "mode": "hardware_catalog",
            }

    if source is None:
        hw_model = hardware_specs.detect_hardware_query(question)
        if (
            hw_model is None
            and context
            and not _is_software_version_question(question)
        ):
            hw_model = hardware_specs.detect_hardware_query(retrieval_question)
        if hw_model:
            hw_info = hardware_specs.get_hardware_specs(hw_model)
            if hw_info:
                return {
                    "answer": hardware_specs.format_hardware_specs_markdown(hw_model),
                    "citations": [
                        {
                            # Not a scraped/ingested file — a pseudo-path into
                            # the curated hardware_specs.py catalog. Do not
                            # format this as a real datasheet file path; no
                            # such file exists in the repo or ingestion corpus.
                            # source/doc_type must not claim datasheet
                            # provenance either: hardware_specs.py carries no
                            # source URLs, and the entries are series-level,
                            # so they cannot answer a per-SKU question.
                            "file_path": f"hardware_specs_catalog:{hw_model}",
                            "source": "hardware_specs_catalog",
                            "doc_type": "curated-summary",
                            "score": 1.0,
                            "coverage": "series-level",
                            "source_url": None,
                        }
                    ],
                    "mode": "hardware_specs",
                }

    if source is None:
        identifier = _extract_exact_identifier(retrieval_question)
        if identifier:
            advisory_hits = lookup_advisory(limit=k, **identifier)
            if advisory_hits and "error" not in advisory_hits[0]:
                mode = "lookup_advisory"
                hits = advisory_hits

    if not hits and source is None and _is_api_question(retrieval_question):
        api_hits = lookup_api(retrieval_question, top_k=k)
        if api_hits and "error" not in api_hits[0]:
            mode = "lookup_api"
            hits = api_hits

    if not hits:
        hits = search_docs(retrieval_question, top_k=k, source=source)
        mode = "search_docs"

    if not hits:
        return {
            "answer": "No matching local documentation was found.",
            "citations": [],
            "mode": mode,
        }
    if "error" in hits[0]:
        return {"answer": hits[0]["error"], "citations": [], "mode": mode}

    answer, evidence_hits = _bounded_evidence_answer(hits, mode, k)
    return {
        "answer": answer,
        "citations": [_citation(hit) for hit in evidence_hits],
        "mode": mode,
    }


@mcp.tool(annotations=READ_ONLY_LOCAL)
def search_internal_docs(
    query: str,
    top_k: int = 5,
    collection: str = "internal",
) -> list[dict[str, Any]]:
    """Search your local personal/internal document collection.

    Hybrid (vector + keyword) search over documents you ingested yourself
    with `python scripts/ingest_personal_docs.py <folder>` — e.g. internal sales/technical
    enablement decks, transcripts, or notes. This is a separate, local-only
    index stored under ~/.config/hpe-mcp/personal/ — never the shared,
    repository-distributed RAG corpus, and never uploaded anywhere. If you
    have not ingested anything yet, this returns an empty list; run the CLI
    ingest command first.

    Args:
        query:      Natural language question or keywords.
        top_k:      Results to return (default 5, range 1-20).
        collection: Which personal collection to search (default "internal").
    """
    from hpe_networking_mcp.pipeline import personal_ingest

    top_k = _clamp_top_k(top_k, 20)
    # The personal index is LanceDB + fastembed, both in the `ingestion`
    # extra. Without it there is nothing to search, and saying so beats
    # returning [] -- which reads as "you have no such document".
    try:
        hits = personal_ingest.search_personal(query, collection=collection, top_k=top_k)
        if not hits:
            counts = personal_ingest.personal_collection_counts()
    except optional_deps.MissingOptionalDependency as exc:
        return _degraded_optional_dep(exc)
    if not hits:
        if not counts:
            return [
                {
                    "error": (
                        "No personal documents have been ingested yet. Run "
                        "`python scripts/ingest_personal_docs.py <folder>` (or "
                        "personal_ingest.ingest_folder(...) directly) first."
                    )
                }
            ]
    return hits


@mcp.tool(annotations=READ_ONLY_LOCAL)
def list_skills(
    platform: str | None = None,
    tag: str | None = None,
    detail: bool = False,
) -> dict[str, Any]:
    """Browse bundled multi-step runbooks (skills).

    Call this early for operational workflows (health checks, change windows,
    client triage, GLP onboarding, SSID review, AOS 8 migration readiness).
    Returns compact metadata by default; set detail=True for descriptions and
    suggested tool lists. Load a full runbook with load_skill(name).
    """
    return list_skills_payload(platform=platform, tag=tag, detail=detail)


@mcp.tool(annotations=READ_ONLY_LOCAL)
def load_skill(name: str) -> dict[str, Any]:
    """Load one skill's full markdown runbook body by name.

    Pass the skill name from list_skills. Lookup is case-insensitive and
    supports a unique substring match when no exact name is found.
    """
    return load_skill_payload(name)


if __name__ == "__main__":
    from hpe_networking_mcp.mcp_servers._cache_hygiene import stable_list_tools
    from hpe_networking_mcp.mcp_servers._middleware import (
        NullStripMiddleware,
        RateLimitMiddleware,
        ResponseEnvelopeMiddleware,
        SecretTokenizeMiddleware,
        install_middleware,
    )

    stable_list_tools(mcp)
    install_middleware(
        mcp,
        [
            NullStripMiddleware(),
            RateLimitMiddleware(rate=8.0),
            ResponseEnvelopeMiddleware(),
            SecretTokenizeMiddleware(),
        ],
    )
    from hpe_networking_mcp.mcp_servers.shared import run_server

    run_server(mcp)
