---
title: "0.9.0"
nav_order: 2
parent: "Releases"
---

# hpe-networking-mcp 0.9.0

Released 2026-08-16; later archived before publication (tag
`archive/release-v0.9.0`). These notes are kept for history — the changes
roll forward into the next published release after 0.8.0.

Version 0.9.0 is a feature release built entirely on the 0.8.0 foundation.
It expands the RAG corpus to 29 sources (including full Juniper hardware and
release-note coverage), modernizes retrieval with ANN indexes, metadata
filtering, caching, and content-hash deduplication, adds Docker packaging and
a morning-report skill, and ships a broad set of bug fixes and operator
runbooks.

## Highlights

### RAG corpus expansion — 29 sources, 262,104 chunks

Six new prose sources were added in two rounds:

| New source | Content |
|---|---|
| `mist_product_updates` | Full Mist cloud-platform release history (2017–present, 186 pages) |
| `aoscx_release_notes` | Full AOS-CX release notes back to 10.13.xx across all 14 switch series (~10,400 pages) |
| `aoscx_guides` | AOS-CX Fundamentals and CLI Reference guides (8 books, ~14,600 pages) |
| `clearpass_guide` | Standalone ClearPass Policy Manager admin guide (6.14, ~1,240 pages) |
| `junos_ex_hardware` | Juniper EX-series hardware install/maintenance guide (~720 pages) |
| `junos_ex_release_notes` | Junos OS EX-series platform-tagged release notes (~193 pages) |
| `junos_mx_hardware` | Juniper MX-series router hardware guide (~1,418 pages) |
| `junos_mx_release_notes` | Junos OS MX-series release notes (~338 pages) |
| `junos_qfx_hardware` | Juniper QFX data-center switch hardware guide (~739 pages) |
| `junos_qfx_release_notes` | Junos OS QFX-series release notes (~388 pages) |
| `junos_srx_hardware` | Juniper SRX firewall hardware guide (~706 pages) |
| `junos_srx_release_notes` | Junos OS SRX-series release notes (~516 pages) |

Total corpus grew from ~96,256 chunks (0.8.0) to **262,104 chunks** across
29 declared sources.

### RAG retrieval modernization

#### ANN index + metadata indexes

- Added cosine HNSW-SQ ANN index (`vector_idx`) over the embedding column —
  materialized for any table ≥ 3 rows.
- Added BTree metadata indexes over `source`, `doc_type`, `vendor`, `product`,
  `platform`, `model`, `release`, `version`, `document_family`, `record_type`,
  `authority`, and `freshness` — enabling `prefilter=True` pushdown.
- `build_search_indexes()` builds FTS + ANN + all BTree indexes in one call.
- `index_identity()` encodes table version, row count, and every index
  version — used as part of the cache key so a rebuilt table auto-invalidates
  cached queries.

#### Normalized metadata on ingestion

`ingest_docs.py` now derives `vendor`, `product`, `platform`, `model`,
`release`, `version`, `document_family`, `record_type`, `authority`, and
`freshness` from file paths and source URLs at ingest time. A legacy index
missing those columns falls back cleanly to the migration path.

Migrate an existing prebuilt index without re-embedding:

```bash
uv run python scripts/migrate_rag_metadata.py --dry-run
uv run python scripts/migrate_rag_metadata.py
```

#### Scoped metadata filtering in search

`_query_metadata_filter()` conservatively infers `vendor`, `model`, and
`release` from query text and pushes them as prefilters. The filter is only
applied when the query explicitly mentions release-oriented keywords (e.g.
"release notes", "enhancements", "version history") — general how-to
questions pass through without a filter so broad `tech_docs` coverage is not
accidentally excluded. A zero-hit metadata-filtered search automatically
retries without the filter.

#### Bounded LRU caches and prewarm

- `pipeline/clients/rag_cache.py` — thread-safe bounded LRU with `.get()`,
  `.set()`, `.clear()`, and `.stats()`.
- `_SEARCH_CACHE` (default 256 entries) and `_EMBED_CACHE` (default 512)
  are keyed on backend, index identity, and normalized query.
- `HPE_MCP_RAG_PREWARM=1` pays the ONNX model-load cost at MCP server startup
  instead of on the first user query.
- `HPE_MCP_RAG_CACHE_SIZE` and `HPE_MCP_RAG_EMBED_CACHE_SIZE` tune the LRU
  bounds for long-lived hosts.

**Measured warm p50 latency after modernization: ~10 ms** (was ~80–100 ms).

#### Content-hash deduplication

38% of the corpus is exact boilerplate repeated across files (license text,
upgrade steps, overview headers). Without dedup, a query like "AOS-CX upgrade
procedure" returned 10 results with character-for-character identical text.

- `_dedup_by_content()` in `rag.py` — collapses duplicate hits at search time,
  keeping the highest-scored representative and attaching `also_in` for
  complete citation provenance.
- `dedup_records()` in `ingest_docs.py` + `--dedup-on-ingest` flag — filters
  duplicates before embedding on a full rebuild, keeping the most authoritative
  source per `_DEDUP_SOURCE_PRIORITY`. Reduces embedded index by ~38%
  (262,104 → ~161,816 rows).
- `scripts/migrate_dedup_index.py` — deduplicates an existing prebuilt index
  atomically via staging table, without re-embedding.

```bash
uv run python scripts/migrate_dedup_index.py --dry-run
uv run python scripts/migrate_dedup_index.py
```

#### `compare_aoscx_releases` tool

New `rag-core` tool that compares AOS-CX enhancements across two releases for
a given switch series, returning a structured diff sourced from the release
notes corpus.

#### Milvus Lite pilot (opt-in)

Dense-only Milvus Lite adapter added as `pipeline/clients/milvus_client.py`
behind an optional extra (`uv sync --extra milvus-lite`). Measured warm p50
~283 ms vs LanceDB ~10 ms; `source_hit@5` 0.812 vs LanceDB 0.972 (no BM25
hybrid available in Milvus Lite). Remains opt-in until quality parity.

See `docs/milvus-lite-pilot.md` for comparison methodology and results.

### RAG eval metrics (post-modernization)

| Metric | 0.8.0 | **0.9.0** | Target |
|---|---|---|---|
| `source_hit@5` | 0.97 | **0.972** | ≥ 0.85 ✅ |
| `mrr` | 0.923 | **0.972** | ≥ 0.85 ✅ |
| `howto_recall@5` | 1.00 | **0.938** | ≥ 0.85 ✅ |
| `api_exact` | 1.00 | **1.000** | ≥ 0.95 ✅ |
| `structured_exact` | 1.00 | **1.000** | 1.00 ✅ |
| `duplicate_guard` | 1.00 | **1.000** | — ✅ |

### Docker packaging

- `Dockerfile` — multi-stage build; `hpe-mcp-router` stdio server in a slim
  Python 3.12 image.
- `docker-compose.router.yml` — streamable-HTTP deployment with optional
  `.env`/`secrets/` mounts.
- `docker/` — entrypoint helpers and health-check script.
- `.dockerignore` — excludes credentials, generated indexes, and
  `ingestion/sources/`.
- `tests/unit/test_docker_router_packaging.py` — validates layer contents,
  entrypoint, and health-check without running a container.

### Morning-report and operator runbooks

New skills added to `src/hpe_networking_mcp/mcp_servers/skills/`:

| Skill | Purpose |
|---|---|
| `morning-report.md` | Last-24h network ops digest (Central + Mist/UXI/GLP) |
| `central-scope-audit.md` | Central scope resolve/audit workflow |
| `central-scope-walker.md` | Walk and validate Central scope hierarchy |
| `clearpass-policy-audit.md` | ClearPass policy review runbook |
| `mist-scope-audit.md` | Mist org/site scope audit |
| `uxi-diagnostics.md` | UXI sensor diagnostics |
| `wlan-sync-validation.md` | WLAN Central↔Mist sync check |
| `cross-platform-rf-check.md` | RF site health across AP platforms |

GitHub Copilot host-side skill files updated to match:
`.github/skills/morning-report/` and `.github/skills/operator-runbooks/`.

### CI security hardening

- `.github/workflows/docker-build.yml` — Docker image lint and build-check CI.
- Bandit SAST and `pip-audit` dependency-CVE workflows added.

### Bug fixes

- **Reactive error hints** (`spec-index`): failed MCP tool calls now surface
  the status code meaning from the spec, not a bare HTTP status integer.
- **Redaction** (`_middleware`): nested stringified JSON blobs are now walked
  and redacted, not just top-level dicts.
- **CLI** (`run_ssid.py`): invalid `--rf-band` choices are rejected before
  they reach Central, with a clear error message.
- **Interop** (`interop.py`): corrected Central rf-band enum and deepened
  WLAN opmode coverage for Central↔Mist translation.
- **Monitoring** (`monitoring.py`): corrected Central query-param names for
  alert configs and insights endpoints.
- **RAG** (`rag.py`): `lookup_hardware_specs` registered as a callable MCP
  tool; device-model misattribution in `feature_navigator`/`product_datasheets`
  ranking fixed; orphan heading chunks in chunking fixed.
- **RAG diagnostics**: `ModuleNotFoundError` crash risk fixed under real MCP
  launch (not just test runs).
- **Diagram workflow**: diagram-intent detection bug fixed; `/docs remove`
  command added; `/status` enriched.

### New scripts and tools

| Script | Purpose |
|---|---|
| `scripts/benchmark_rag.py` | Cold/warm stage-level LanceDB latency benchmark |
| `scripts/benchmark_milvus.py` | Full-corpus Milvus Lite dense comparison harness |
| `scripts/migrate_rag_metadata.py` | Non-destructive metadata migration for prebuilt indexes |
| `scripts/migrate_dedup_index.py` | Remove exact-duplicate rows from existing prebuilt index |

### New client example

`examples/mcp-clients/claude-code.mcp.json` — Claude Code MCP client config.

## Corpus snapshot

| Artifact | Count |
|---|---:|
| Prose chunks | 262,104 |
| Declared RAG sources | 29 |
| Exact API endpoints | 4,106 |
| Schemas | 8,890 |
| Fields | 50,675 |
| Security advisories | 104 |
| Lifecycle records | 346 |
| Registered backend tools | 6,719 |
| Generated manifest operations | 6,144 |

## Validation

- 4,596 unit tests passed, 4 skipped.
- All 6,719 registered backend identities matched the tool index exactly.
- Strict RAG/API evaluation and local source/index manifest reconciliation passed.
- `source_hit@5=0.972`, `MRR=0.972`, `api_exact=1.0`.

## Known boundaries

- Milvus Lite remains opt-in (quality gap vs LanceDB hybrid).
- AOS-CX CLI Reference guides are not yet exhaustive across all switch-series
  generations (see manifest notes for gaps).
- Junos general software config guides (~24,700 pages) remain out of scope;
  only platform-tagged hardware and release-note pages are ingested.
- MX/QFX/SRX hardware datasheets (distinct from install/maintenance guides)
  not yet scraped.

See the [CHANGELOG](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/CHANGELOG.md) and [prebuilt index guide](release-indexes.md)
for release assets and restore instructions.
