# Changelog

All notable changes to `hpe-networking-mcp` are documented on this page. The
format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
version numbers follow the package version pinned in
[`pyproject.toml`](pyproject.toml) and mirrored in
[`docs/project-facts.json`](docs/project-facts.json).

Every per-release page under `docs/release-notes-*.md` is the detailed,
point-in-time record for that version -- catalog snapshot, upgrade
instructions, known gaps, and validation summary as they stood at release
time. This file is the compact index into those pages. See
[MIGRATION.md](MIGRATION.md) for the step-by-step move from the legacy
`secure-ssid/centralmcp` repository to this one.

## [Unreleased]

### Changed — BREAKING

- **Central writes are now denied by default.** `HPE_MCP_CENTRAL_WRITES` must be
  set to `1` (or `HPE_MCP_ACCESS_PROFILE=full-read-write`) to expose Central
  write and destructive tools. Previously Central alone defaulted to enabled,
  which meant `import hpe_networking_mcp` with no configuration produced a
  server willing to dispatch `reboot_device` and `disconnect_client`.
  Deployments relying on the implicit default must set the variable explicitly.

### Changed

- **Raised tool exceptions now return the response-envelope shape.** A tool
  that raises produces the same `{ok: false, status, data, message, tool,
  platform}` payload a returned error dict gets — on standalone backends and
  through the router alike — delivered as an `isError=true` result, instead
  of bare `ToolError` text. Elicitation/protocol errors and cancellation
  still propagate untouched.

### Added

- **`corpus_provenance`** — one read-only `rag-core` tool reporting what backs
  an answer. For the committed, digest-pinned `vendor/openapi` corpus behind
  `lookup_api` it returns the document count, API-path count, fetch dates and
  per-licence breakdown on a clean checkout with no network and no local
  build, with each document's `source_url`, SHA-256 and upstream pin available
  on request (`detail=True`, or `spec=` with a `lookup_api` `file_path`). For
  the locally built prose corpus behind `search_docs`/`ask_docs` it reports the
  per-source chunk counts and refresh times when the index exists, and names
  the command that builds it when it does not. A missing corpus, a corpus with
  missing files, and a corpus with no index built over it stay three distinct
  answers, each with its own remedy; a malformed manifest degrades rather than
  raising.

## [0.9.0] - 2026-08-16

Feature release. Full detail in [docs/release-notes-0.9.0.md](docs/release-notes-0.9.0.md).

### Added

- **RAG corpus**: 12 new Juniper sources (MX/QFX/SRX/EX hardware guides and
  release notes), AOS-CX release notes (all 14 switch series back to 10.13),
  AOS-CX Fundamentals/CLI Reference guides, Mist cloud release history,
  ClearPass Policy Manager admin guide — total **262,104 chunks across 29 sources**.
- **ANN index**: cosine HNSW-SQ `vector_idx` + 12 BTree metadata indexes
  (`vendor`, `product`, `platform`, `model`, `release`, etc.) — warm p50 ~10 ms.
- **Normalized metadata ingestion**: `derive_metadata()` populates 10 metadata
  fields from file paths at ingest time; scoped metadata prefilters in search.
- **Content-hash deduplication**: `_dedup_by_content()` collapses duplicate
  hits at search time; `--dedup-on-ingest` flag drops duplicates before
  embedding (saves ~38%); `scripts/migrate_dedup_index.py` migrates prebuilt
  indexes without re-embedding.
- **Bounded LRU caches**: `pipeline/clients/rag_cache.py` + `HPE_MCP_RAG_PREWARM`,
  `HPE_MCP_RAG_CACHE_SIZE`, `HPE_MCP_RAG_EMBED_CACHE_SIZE` env vars.
- **`compare_aoscx_releases` tool**: structured diff of AOS-CX enhancements
  across two releases for a given switch series.
- **Milvus Lite pilot**: opt-in adapter at `pipeline/clients/milvus_client.py`
  (`uv sync --extra milvus-lite`); documented in `docs/milvus-lite-pilot.md`.
- **Docker packaging**: `Dockerfile`, `docker-compose.router.yml`, `docker/`
  helpers, `.dockerignore`, CI build workflow.
- **Morning-report skill** and eight new operator runbooks (central-scope-audit,
  clearpass-policy-audit, mist-scope-audit, uxi-diagnostics, wlan-sync-validation,
  cross-platform-rf-check, central-scope-walker).
- **`search_internal_docs`**: local personal document ingestion for the MCP router.
- **Bandit SAST + pip-audit** security workflows.
- **`scripts/benchmark_rag.py`** and **`scripts/benchmark_milvus.py`** latency harnesses.
- **`scripts/migrate_rag_metadata.py`**: metadata migration for prebuilt indexes.
- **Claude Code** MCP client example (`examples/mcp-clients/claude-code.mcp.json`).

### Fixed

- Reactive error hints from `specs_index` on failed MCP tool calls.
- Nested stringified-blob redaction in `_middleware`.
- Invalid `--rf-band` choices rejected before reaching Central API.
- Central rf-band enum and WLAN opmode coverage in `interop.py`.
- Central query-param names for alert configs and insights in `monitoring.py`.
- `lookup_hardware_specs` registered as a callable MCP tool.
- Device-model misattribution in `feature_navigator`/`product_datasheets` ranking.
- Orphan heading chunks in RAG chunking (`chunk_text`).
- `ModuleNotFoundError` crash risk in `rag_diagnostics` under real MCP launch.
- Diagram-intent detection bug; `/docs remove` added; `/status` enriched.

### Changed

- Hero SVG updated to v0.9.0 with RAG and dedup metrics.
- Platform-coverage SVG updated with current corpus counts.
- `docs/architecture/RAG-ARCHITECTURE.md` documents all retrieval modernization.

## [0.8.0] - 2026-08-12

This release launches the renamed, restructured continuation of
`secure-ssid/centralmcp`. The legacy repository is untouched and still
exists as a historical reference; it is not deprecated in place, and no
history was rewritten -- this is a fresh repository with an equivalent
working tree. See [MIGRATION.md](MIGRATION.md) for the full local migration
walkthrough.

### Changed

- **Package name:** `centralmcp` -> `hpe-networking-mcp` (`pyproject.toml`
  `[project].name`).
- **Source layout:** flat `mcp_servers/`, `pipeline/`, `run_pipeline.py`,
  `run_ssid.py`, `scripts/doctor.py` modules moved under the installable
  `src/hpe_networking_mcp/` package (`src/hpe_networking_mcp/mcp_servers/`,
  `src/hpe_networking_mcp/pipeline/`, `src/hpe_networking_mcp/cli/`).
- **Console scripts:** the legacy project only exposed `run-pipeline` and
  `run-ssid`. This project adds two more and renames all four to a common
  `hpe-mcp-*` prefix:
  | Legacy | Current |
  |---|---|
  | `run-pipeline` | `hpe-mcp-run-pipeline` |
  | `run-ssid` | `hpe-mcp-run-ssid` |
  | *(none -- `python scripts/doctor.py`)* | `hpe-mcp-doctor` |
  | *(none -- `python mcp_servers/tool_router.py`)* | `hpe-mcp-router` |
- **Environment variable prefix:** every `CENTRALMCP_*` variable is renamed
  to `HPE_MCP_*` (`CENTRALMCP_PRODUCTS` -> `HPE_MCP_PRODUCTS`,
  `CENTRALMCP_TOOLSETS` -> `HPE_MCP_TOOLSETS`,
  `CENTRALMCP_ROUTER_MODE` -> `HPE_MCP_ROUTER_MODE`,
  `CENTRALMCP_READONLY` -> `HPE_MCP_READONLY`,
  `CENTRALMCP_RAG_BACKEND` -> `HPE_MCP_RAG_BACKEND`, and the router
  response-budget/cursor-TTL variables). `CREDS_PATH` is unchanged.
- **MCP server IDs (strict catalog):** the router and every backend server
  ID lost the ambiguous `aruba-*` prefix in favor of names scoped to what
  each backend actually talks to:
  | Legacy server ID | Current server ID |
  |---|---|
  | `aruba-tool-router` | `hpe-networking-mcp` (router) |
  | `aruba-config` | `central-config` |
  | `aruba-monitoring` | `central-monitoring` |
  | `aruba-nac` | `central-nac` |
  | `aruba-ops` | `central-ops` |
  | `aruba-central-generated` | `central-generated` |
  | `aruba-glp` | `glp-core` |
  | `aruba-rag` | `rag-core` |

  Optional product server IDs (`clearpass-core`, `mist-core`, `apstra-core`,
  `aos8-core`, `edgeconnect-core`, `uxi-core`, `axis-core`, `design-core`)
  are unchanged from the legacy project. The always-on credential-free
  `interop-core` backend (Central <-> Mist WLAN/site concept translation and
  bounded trend normalization) is **new** in this project -- it did not
  exist in the legacy `centralmcp` repository at all.
- **MCP SDK:** both projects build on the installed MCP Python SDK 2.x
  `MCPServer` API (`from mcp.server.mcpserver import MCPServer` and
  `mcp.server.mcpserver.server.MCPServer`) -- not the third-party
  [FastMCP](https://github.com/jlowin/fastmcp) framework, and not the SDK's
  legacy pre-2.x server class. If you are working from older personal notes
  that mention `FastMCP(...)`, replace them with `MCPServer(...)`; the
  runtime API this project depends on did not change during the rename.
- **MCP 2 HTTP transport:** streamable HTTP and bearer-protected HTTP now pass
  host, port, and transport-security settings through the supported MCP 2.x
  API, preserve the Starlette lifespan/session manager, and have real
  initialize/list/call protocol coverage.
- **Runtime dependencies:** refreshed the lock to current compatible releases,
  including LanceDB 0.37.1, Playwright 1.62.0, Redis 8.1.0, pypdf 6.15.0,
  Ruff 0.16.2, and their transitive security/runtime updates.
- **Repository/packaging URLs:** `Homepage`, `Repository`, `Documentation`,
  and `Changelog` in `pyproject.toml` now point at
  `secure-ssid/hpe-networking-mcp` instead of `secure-ssid/centralmcp`.
  `Changelog` specifically points at this file's GitHub URL
  (`.../blob/main/CHANGELOG.md`) rather than a versioned release-notes page,
  so it stays correct across every future release without an edit.
- **Strict tool catalog:** the complete backend catalog is the single
  source of truth for every published tool count -- see
  [`docs/project-facts.json`](docs/project-facts.json) and
  [`docs/tool-catalog.md`](docs/tool-catalog.md). No functional tool
  behavior changed as part of the rename itself; the added 0.8.0 capabilities
  below are represented in the current derived totals.

### Added

- `CHANGELOG.md` (this file) and [`MIGRATION.md`](MIGRATION.md).
- An `examples/` tree of tested, non-secret MCP client and prompt/runbook
  configurations -- see [`examples/README.md`](examples/README.md).
- `tests/unit/test_docs_facts.py`, extending the canonical-facts checker
  (`docs/project-facts.json` / `scripts/project_facts.py`) to rendered
  documentation pages that describe the RAG/structured-index state in
  prose, not just tables.
- The always-on credential-free `interop-core` backend itself (5 tools:
  Central <-> Mist WLAN/site concept translation and bounded trend
  normalization) -- new in this project, not present in legacy `centralmcp`.
- `docs/project-facts.json`'s `router_modes` section (and
  `hpe_networking_mcp.pipeline.project_facts.router_mode_facts()`), deriving
  minimal/default/direct-all client-visible router tool counts (3/18/6,722)
  instead of hand-entering them -- this replaced a stale "6,703 complete /
  6,710 direct-all" claim across several docs pages that had conflated the
  platform-API-only subtotal (6,703) with the complete registered backend
  total (6,715 = 6,703 platform API + 7 `design-core` + 5 `interop-core`).
- Opt-in reversible PII tokenization around router dispatch, preserving
  round-trip behavior without exposing plaintext identifiers to model-facing
  calls.
- Central-to-Mist and Mist-to-Central translation helpers plus
  vendor-neutral trend normalization in `interop-core`.
- Complete curated GLP Reporting and Service Catalog inventory coverage,
  with community-derived generated GLP specifications clearly separated from
  HPE-authoritative sources.
- Strict, reproducible RAG/catalog validation across 96,256 prose chunks,
  4,106 exact endpoints, 8,890 schemas, 50,675 fields, 104 advisories,
  346 lifecycle records, 16 declared sources, and 6,715 indexed backend tools.
- Classified source-drift gates that distinguish content drift, source-set
  change, pointer movement, stale pins, network unavailability, parser errors,
  and known coverage gaps; refresh plans are declarative, transactional, and
  fail closed on incomplete checks.
- Tested non-secret stdio, HTTP, bearer, Copilot, Cursor, VS Code, Claude, and
  prompt/runbook examples.

### Notes

- Local, git-ignored artifacts are unaffected by the rename: rebuild
  `data/specs.sqlite` with `uv run python scripts/build_spec_index.py`,
  `data/tools.lance` with `uv run python scripts/ingest_tools.py`, and
  `data/docs.lance` with `uv run python ingestion/ingest_docs.py`, the same
  way you did before.
- `config/credentials.yaml` and any private diagram icon packs are never
  committed and are not affected by the rename.
- No upstream API or documentation source was refreshed while preparing this
  release. The packaged index manifest records that fact and preserves each
  artifact's real modification time.

Detailed notes: [hpe-networking-mcp 0.8.0](docs/release-notes-0.8.0.md).

## 0.7.0 and earlier

Detailed per-release notes, unchanged from before the rename:

- [0.7.0](docs/release-notes-0.7.0.md) - artifact/live-test gates, source
  lifecycle provenance, structured RAG intelligence, Central/GLP/AOS8/
  optional-product depth, observability/security, router automation, and
  release artifact automation.
- [0.6.0](docs/release-notes-0.6.0.md) - security/lifecycle RAG, expanded
  Central/GLP/AOS8/Axis/Mist coverage, provenance, audit logging, and
  migration reports.
- [0.5.0](docs/release-notes-0.5.0.md) - verified ArubaOS 8 migration
  expansion: source hardening, bounded Classic Central write lifecycle,
  expanded fail-closed New Central mappings, verification taxonomy, and
  read-only live/dry-run evaluation.
- [0.4.0](docs/release-notes-0.4.0.md) - resumable migration execution,
  typed GLP, Mist diagnostics, EdgeConnect compatibility, and verified
  benchmarks.
- [0.3.0](docs/release-notes-0.3.0.md) - platform parity, migrations, and
  safety (largest expansion up to that point; historical).

See [`docs/README.md`](docs/README.md) for the complete documentation index,
and [`docs/release-indexes.md`](docs/release-indexes.md) for the current
prebuilt RAG/OpenAPI index snapshot.
