---
title: "RAG coverage matrix"
nav_order: 4
parent: "Reference"
---

# RAG coverage matrix

This page records what the local RAG and exact-lookup indexes can answer, what
is still being expanded, and what must remain an explicit coverage gap.
Authoritative inputs are vendor-published manuals, API references,
configuration/user guides, release notes, technical notes, knowledge-base
articles, security/lifecycle notices, datasheets, and validated designs.
GitHub/community material is not treated as authoritative.

Counts are derived from [`docs/project-facts.json`](project-facts.json), not
hand-maintained here. Rebuild the facts after a source refresh with:

```bash
uv run python scripts/project_facts.py --write
uv run python scripts/package_indexes.py --write-local-manifests
```

## Current coverage

| Platform or product family | Indexed sources and lookup path | Current strength | Known gap or boundary |
|---|---|---|---|
| Aruba Central / New Central | `developer_docs`, `tech_docs`, `techdocs_html`, `nac_docs`, `vsg_docs`, `openapi_specs`; exact API lookup uses SQLite | Strong New Central API, configuration, monitoring, NAC, and design coverage | Classic Central, New Central, and Central-on-prem terminology/version routing needs to be explicit; overlapping registry bundles need deduplication |
| GreenLake Platform | 1,000+ generated/curated MCP tools; no dedicated prose/API RAG source | Broad callable surface and typed workflows | Highest-priority documentation gap: add official GLP API and operator documentation plus exact lookup/eval coverage |
| AOS-CX switches | `aoscx_release_notes`, `aoscx_guides`, `aos_techdocs`, `product_specs` | Release-note and CLI/fundamentals expansion is in progress; hardware/API material exists | Complete 10.13+ guide families and obtain a current REST/OpenAPI source; keep software-version applicability visible |
| Aruba APs / AOS 10 | `aos_techdocs`, `tech_docs`, `vsg_docs`, curated hardware catalog | Configuration/design guidance and several AP models | Expand Wi-Fi 7/700-series, outdoor/remote AP, antenna/accessory, regulatory, mounting, and dated datasheet coverage |
| ClearPass | `clearpass_guide`, `nac_docs`, `product_specs` | Current administration guide and NAC workflows | Align API/spec branches with guide and release versions; add release notes, upgrade paths, compatibility, and exact current API coverage |
| Juniper Mist | `mist_docs`, `mist_product_updates`, pinned Mist OpenAPI, Juniper KB/lifecycle/security sources | Strongest non-Aruba prose and exact API coverage | Add product/version-focused evals and review any intentionally excluded regional/GovCloud source families |
| Junos / EX switches | `junos_ex_hardware`, `junos_ex_release_notes`, datasheets, Juniper KB | Hardware/install and release-note coverage | Add curated configuration/troubleshooting guides for VLANs, Virtual Chassis, EVPN/VXLAN, PoE, upgrades, telemetry, and Mist adoption |
| Junos / MX routers | `junos_mx_hardware`, `junos_mx_release_notes` | Hardware/install and release-note coverage, same sitemap/DITA pipeline as EX | Add curated MX configuration/troubleshooting guides (routing protocols, MPLS, services cards); no MX datasheet source yet |
| Junos / QFX switches | `junos_qfx_hardware`, `junos_qfx_release_notes` | Hardware/install and release-note coverage, same sitemap/DITA pipeline as EX | Add curated QFX data-center fabric/EVPN-VXLAN configuration guides; no QFX datasheet source yet |
| Junos / SRX firewalls | `junos_srx_hardware`, `junos_srx_release_notes` (release notes also cover vSRX/cSRX virtual forms) | Hardware/install and release-note coverage, same sitemap/DITA pipeline as EX | Add curated SRX security-policy/NAT/VPN configuration guides; no SRX datasheet source yet |
| EdgeConnect | Generated OpenAPI tools and curated backend | Large callable API surface | Obtain and verify a current target-instance 9.3+ Swagger before treating generated coverage as production-verified; add operator docs/wizards |
| Apstra / Axis / UXI | Generated/curated optional backends and selected product sources | Optional tools and safety/doctor paths exist | Verify official or target-instance source provenance and add product-specific docs/evals; UXI live verification remains credential-dependent |
| Security and lifecycle | Structured SQLite advisory/lifecycle records plus prose sources | Exact advisory, lifecycle, listing, correlation, freshness, and citation diagnostics | Current Aruba-branded lifecycle coverage after the historical archive remains an explicit documented gap; empty is not “supported” |

## Lookup contract

Use the most authoritative path for the question:

| Question | Preferred tool | Why |
|---|---|---|
| Exact endpoint, HTTP method, operation ID, field, schema, or enum | `lookup_api` | Structured OpenAPI data avoids semantic guessing |
| Exact hardware model specification | `lookup_hardware_specs` | Curated records are distinct from prose chunks and datasheet availability |
| SKU or model/configuration selection | `search_hardware_catalog` | Exact local SKU aliases and bounded SQLite candidates; includes official source, snapshot, and lifecycle status when available |
| Side-by-side hardware comparison | `compare_hardware` | Resolves exact SKUs or returns model variants for selection; compares only verified local SQLite fields and labels unavailable data `unknown` |
| Advisory or CVE | `lookup_advisory`, `list_advisories` | Exact identifiers and bounded filters |
| Lifecycle event | `check_product_lifecycle`, `list_lifecycle_events` | Structured dates/SKUs with explicit source boundaries |
| How-to or design concept | `ask_docs` | Compact cited answer over prose sources |
| Raw evidence or source comparison | `search_docs` | Returns bounded source chunks |
| Operator procedure | `list_skills`, `load_skill` | Uses local tool names and safety gates |

An empty result means “not found in the indexed authoritative sources.” It
must not be converted into a claim that a product is supported, secure, or
configured.

## Refresh and review rules

1. Add every new source to `ingestion/source_manifest.json` with its authority,
   scraper, provenance, expected source family, and refresh behavior.
2. Keep raw source files and local indexes generated/ignored; do not commit
   credentials or scraped content.
3. Run the source-specific scraper or the declarative refresh planner, then
   rebuild docs/spec indexes and the tool catalog when applicable.
4. Run the RAG evaluation before and after retrieval changes. A source-count
   increase is not a quality result unless the relevant vendor/version
   questions improve or remain within the release thresholds.
5. Record blocked or unavailable official sources as coverage gaps with
   evidence, rather than substituting GitHub/community material without an
   explicit policy decision.
