"""MCP server — Aruba/HPE documentation RAG tools (12 tools).

Covers: hybrid (vector + BM25) search over ingested Aruba Central developer
docs, tech docs, NAC docs, VSG docs, and HTML tech docs; exact API
endpoint/schema/enum lookup via the SQLite specs index; exact structured
security-advisory/lifecycle lookup, bounded list/filter/pagination, an
exact-only advisory<->lifecycle correlation, bounded RAG index
diagnostics (ingestion delta, source freshness, citation completeness),
local skills/runbook browse+load helpers, and a search over the user's own
local personal/internal document collection (separate index, never shared).

Default backend is the embedded stack — LanceDB + fastembed, no servers
needed (`clone -> uv sync -> run`). Set HPE_MCP_RAG_BACKEND=redis for the
optional Redis Stack + Ollama server deployment (vector-only + source boost).
"""

import re
from typing import Any

from mcp.server.mcpserver import MCPServer

from hpe_networking_mcp.mcp_servers.shared import READ_ONLY, READ_ONLY_LOCAL, resolve_rag_backend
from hpe_networking_mcp.mcp_servers.skills import list_skills_payload, load_skill_payload
from hpe_networking_mcp.pipeline import artifact_contracts as contracts
from hpe_networking_mcp.pipeline.clients import (
    advisory_index,
    hardware_specs,
    rag_diagnostics as rag_diagnostics_client,
    specs_index,
)

mcp = MCPServer("rag-core")

_BACKEND = resolve_rag_backend()

if _BACKEND == "redis":
    from hpe_networking_mcp.pipeline.clients.ollama_client import OllamaClient
    from hpe_networking_mcp.pipeline.clients.redis_client import get_client as _get_redis_client
    from hpe_networking_mcp.pipeline.clients.redis_client import vector_search

    _ollama = OllamaClient()
    try:
        _redis = _get_redis_client()
        _redis.ping()
    except Exception:
        _redis = None
else:
    from hpe_networking_mcp.pipeline.clients import lance_client
    from hpe_networking_mcp.pipeline.clients.embed_client import EmbedClient

    _embedder = EmbedClient()  # lazy — the ONNX model loads on first query

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
    # Juniper prose is left unboosted deliberately. Giving it developer_docs'
    # 0.10 measurably cost eval mrr (0.654 -> 0.629) because the fixtures are
    # Aruba-only, so the harm is measurable while the benefit is not. Within a
    # Juniper query the ordering that matters — mist_specs above mist_docs —
    # already mirrors Aruba's openapi_specs above tech_docs.
    "mist_docs": 0.0,
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
    "mist_product_updates": "juniper",
    "junos_ex_hardware": "juniper",
    "junos_ex_release_notes": "juniper",
    "juniper_lifecycle": "juniper",
    "juniper_security_advisories": "juniper",
    "juniper_kb": "juniper",
    "product_datasheets": "juniper",
}

# Brand-specific enough that a match is a deliberate signal, not incidental
# prose. Generic networking terms (vlan, radius, multicast) are absent on
# purpose — they say nothing about which vendor is being asked about.
_VENDOR_HINTS: dict[str, frozenset[str]] = {
    "juniper": frozenset({
        "juniper", "mist", "junos", "marvis", "jvd", "mxedge", "tunterm",
        "apstra", "ex", "qfx", "srx", "ssr", "vjunos", "wxlan",
    }),
    "aruba": frozenset({
        "aruba", "central", "aos", "instant", "clearpass", "hpe", "greenlake",
        "glp", "iap", "cx", "arubaos", "airwave", "edgeconnect", "silverpeak",
    }),
}

# Cross-vendor hits keep their retrieval score but lose their source boost and
# take this penalty. A penalty rather than a filter: vendor detection is a
# heuristic, so a genuinely strong cross-vendor match can still surface (Mist
# and Aruba docs legitimately reference each other in migration material).
_CROSS_VENDOR_PENALTY = 0.12

SourceFilter = str | tuple[str, ...] | None

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
    "juniper-kb": "juniper_kb",
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
    return [
        {
            "text": r["text"][:600] + "…" if len(r["text"]) > 600 else r["text"],
            "source": r["source"],
            "doc_type": r.get("doc_type"),
            "file_path": r["file_path"],
            "score": round(r["score"], 4),
        }
        for r in rows[:top_k]
    ]


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
    matched = {
        vendor for vendor, hints in _VENDOR_HINTS.items() if tokens & hints
    }
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
    hits.sort(key=lambda h: h["score"], reverse=True)
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


def _search_lancedb(query: str, top_k: int, source_filter: SourceFilter) -> list[dict[str, Any]]:
    try:
        db = lance_client.connect()
        query_vector = _embedder.embed_query(query)
        # Fetch well beyond top_k so the source boost has candidates to promote —
        # authoritative-but-lower-ranked docs are typically just outside top_k,
        # and boosting a list already truncated to top_k can only reorder it.
        hits = lance_client.hybrid_search(
            db, query, query_vector,
            top_k=max(top_k * 6, 30), source_filter=source_filter,
        )
    except (FileNotFoundError, ValueError) as exc:
        return [{"error": str(exc)}]
    hits = _boost_model_match(_boost_sources(hits, query), query)
    return _shape(hits, top_k)


def _search_redis(query: str, top_k: int, source_filter: SourceFilter) -> list[dict[str, Any]]:
    if _redis is None:
        return [{"error": "Redis not available — is the Redis Stack server running?"}]

    query_vector = _ollama.embed_query(query)
    # Fetch more candidates so re-ranking has room to promote higher-priority sources
    candidates = vector_search(
        _redis, query_vector, top_k=top_k * 3, source_filter=source_filter
    )

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
                  aoscx_guides, clearpass_guide, mist_docs, mist_product_updates,
                  junos_ex_hardware, junos_ex_release_notes,
                  security_advisories, lifecycle_notices, juniper_lifecycle,
                  juniper_security_advisories, feature_navigator, or
                  product_datasheets.
        doc_type: DEPRECATED — use source instead.
    """
    top_k = _clamp_top_k(top_k, 20)

    # Map legacy doc_type to source name when source is not provided
    source_filter = source
    if not source_filter and doc_type:
        source_filter = _DOC_TYPE_TO_SOURCE.get(doc_type)

    if _BACKEND == "redis":
        return _search_redis(query, top_k, source_filter)
    return _search_lancedb(query, top_k, source_filter)


@mcp.tool(annotations=READ_ONLY_LOCAL)
def lookup_api(query: str, top_k: int = 10) -> list[dict[str, Any]]:
    """Exact Aruba/Mist API lookup — endpoints, schemas, fields, enum values.

    Authoritative, lossless answers from the parsed OpenAPI specs (SQLite, no
    server needed). Use this INSTEAD of search_docs for questions like "what
    enum values does field X accept", "which endpoint configures Y and with
    what method", or "what fields does schema Z have". Returns [] when the
    specs hold no confident answer — fall back to search_docs in that case.

    Args:
        query: Natural language question, exact ``METHOD /path``, or exact
               operationId (e.g. "auth-type enum values for an auth profile",
               "GET /network-monitoring/v1/sites-client-health", or
               "listSitesClientHealthV1").
        top_k: Results to return (default 10, range 1-20).
    """
    try:
        return specs_index.lookup(query, top_k=_clamp_top_k(top_k, 20))
    except FileNotFoundError as exc:
        return [{"error": str(exc)}]


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
        "advisory_id",
        "severity",
        "status",
        "current_release",
        "notice_id",
        "published",
        "category",
        "event_type",
    ):
        if hit.get(key) is not None:
            citation[key] = hit[key]
    for key in ("cves", "product_skus", "replacement_skus"):
        value = hit.get(key)
        if isinstance(value, list) and value:
            citation[key] = value[:5]
    return citation


def lookup_hardware_specs(
    model: str,
) -> dict[str, Any]:
    """Look up authoritative hardware datasheet specifications for switches and APs.

    Returns switching capacity, throughput, stacking (VSF/Virtual Chassis),
    port configurations, PoE wattage, uplinks, architecture, and routing/security
    features for Aruba CX (6000, 6100, 6200, 6300, 6400, 8325, 8360, 10000),
    Juniper EX (2300, 4100, 4400, 4650), Aruba APs (635), and Mist APs (45).

    Args:
        model: Hardware model identifier, e.g. "cx6300", "6300", "ex4400", "8360", "ap635".
    """
    key = hardware_specs.detect_hardware_query(model) or model.lower().strip()
    spec = hardware_specs.get_hardware_specs(key)
    if not spec:
        return {
            "ok": False,
            "error": f"Hardware model '{model}' not found in hardware specifications catalog.",
            "available_models": sorted(hardware_specs.HARDWARE_CATALOG.keys()),
        }
    return {
        "ok": True,
        "model": key,
        "specs": spec,
        "formatted": hardware_specs.format_hardware_specs_markdown(key),
    }


@mcp.tool(annotations=READ_ONLY_LOCAL)
def ask_docs(
    question: str,
    top_k: int = 3,
    source: str | None = None,
) -> dict[str, Any]:
    """Return a compact cited answer from local docs/API indexes.

    Token-saving companion to `search_docs`: it returns the shortest useful
    extractive answer plus citations instead of dumping multiple long chunks.
    A question containing a literal CVE ID or vendor advisory ID consults
    `lookup_advisory` first (exact, never a guessed product filter);
    otherwise API-shaped questions consult `lookup_api` first; both fall
    back to prose RAG when no exact match exists.
    """
    k = max(1, min(top_k, 5))
    mode = "search_docs"
    hits: list[dict[str, Any]] = []

    if source is None:
        hw_model = hardware_specs.detect_hardware_query(question)
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
                            "file_path": f"hardware_specs_catalog:{hw_model}",
                            "source": "hardware_datasheets",
                            "doc_type": "datasheet",
                            "score": 1.0,
                        }
                    ],
                    "mode": "hardware_specs",
                }

    if source is None:
        identifier = _extract_exact_identifier(question)
        if identifier:
            advisory_hits = lookup_advisory(limit=k, **identifier)
            if advisory_hits and "error" not in advisory_hits[0]:
                mode = "lookup_advisory"
                hits = advisory_hits

    if not hits and source is None and _is_api_question(question):
        api_hits = lookup_api(question, top_k=k)
        if api_hits and "error" not in api_hits[0]:
            mode = "lookup_api"
            hits = api_hits

    if not hits:
        hits = search_docs(question, top_k=k, source=source)
        mode = "search_docs"

    if not hits:
        return {
            "answer": "No matching local documentation was found.",
            "citations": [],
            "mode": mode,
        }
    if "error" in hits[0]:
        return {"answer": hits[0]["error"], "citations": [], "mode": mode}

    top = hits[0]
    if mode == "lookup_advisory":
        answer = _summarize_advisory(top)
    else:
        text = str(top.get("text", "")).strip()
        answer = text[:900] + "…" if len(text) > 900 else text
    return {
        "answer": answer,
        "citations": [_citation(hit) for hit in hits[:k]],
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
    with `hpe-mcp docs ingest <folder>` — e.g. internal sales/technical
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
    from hpe_networking_mcp.cli_client import personal_ingest

    top_k = _clamp_top_k(top_k, 20)
    hits = personal_ingest.search_personal(query, collection=collection, top_k=top_k)
    if not hits:
        counts = personal_ingest.personal_collection_counts()
        if not counts:
            return [
                {
                    "error": (
                        "No personal documents have been ingested yet. Run "
                        "`hpe-mcp docs ingest <folder>` (or "
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
        SecretTokenizeMiddleware,
        install_middleware,
    )
    stable_list_tools(mcp)
    install_middleware(
        mcp,
        [
            NullStripMiddleware(),
            RateLimitMiddleware(rate=8.0),
            SecretTokenizeMiddleware(),
        ],
    )
    from hpe_networking_mcp.mcp_servers.shared import run_server
    run_server(mcp)
