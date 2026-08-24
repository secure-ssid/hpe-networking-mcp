---
title: "Documentation index"
nav_exclude: true
---

# hpe-networking-mcp documentation

The GitHub Pages site is published from `main/docs` with Jekyll and the
[just-the-docs](https://just-the-docs.com/) remote theme (tag-pinned in
`_config.yml`, so the stock `jekyll-build-pages` CI builds it unchanged).
Sidebar navigation, search, and heading anchors come from the theme; each page
declares its nav position with `title` / `nav_order` / `parent` frontmatter.
User journeys are written in Markdown; diagrams are drawn by this
project's own `design` MCP server and committed as accessible SVG files so they
render without browser-side JavaScript.

## Start here

### Guides

| Doc | Use it for |
|---|---|
| [index.md](index.md) | Task-based front door for new users, network operators, and contributors |
| [getting-started.md](getting-started.md) | Wizard install, credentials, optional products, MCP client setup, and indexes |
| [mcp-client-recipes.md](mcp-client-recipes.md) | Host-first MCP setup recipes for stdio and streamable HTTP |
| [production-deployment.md](production-deployment.md) | Containerized router packaging: Dockerfile, Compose overlay, secrets, and explicit prebuilt-index provisioning |
| [tool-router.md](tool-router.md) | Low-token discovery, dispatch, and write-safety behavior |
| [example-prompts.md](example-prompts.md) | Complete scenarios with calls, expected shapes, and safety labels |
| [optional-products.md](optional-products.md) | Optional product matrix, wizard behavior, env vars, and safety surface |
| [troubleshooting.md](troubleshooting.md) | Outcome-driven setup, authentication, transport, catalog, and RAG fixes |
| [known-limitations.md](known-limitations.md) | Current known limitations, their impact, and planned remediation |
| [../examples/README.md](../examples/README.md) | Tested, non-secret example configs: minimal/full stdio, local/bearer HTTP, Copilot CLI, and prompt/runbook examples |
| [../MIGRATION.md](../MIGRATION.md) | Local migration path from the legacy `secure-ssid/centralmcp` repository |

### Architecture

| Doc | Use it for |
|---|---|
| [architecture/how-it-works.md](architecture/how-it-works.md) | Canonical MCP + RAG mental model: router tools, indexes vs live APIs, dry-run/gates |
| [architecture/system-overview.md](architecture/system-overview.md) | Runtime, data, transport, and safety architecture |
| [architecture/RAG-ARCHITECTURE.md](architecture/RAG-ARCHITECTURE.md) | Embedded RAG design, eval results, and migration rationale |

### Reference

| Doc | Use it for |
|---|---|
| [tool-catalog.md](tool-catalog.md) | Per-backend counts, capability families, build modes, and safety notes |
| [product-workflows.md](product-workflows.md) | Typed ClearPass/Mist/Apstra/AOS8/EdgeConnect/UXI workflow roadmap |
| [central-v07-workflows.md](central-v07-workflows.md) | Central v0.7 depth workflows: VSF template lifecycle, bulk site/site-collection delete, firmware-compliance campaigns, config-health remediation, troubleshooting orchestration |
| [rag-coverage-matrix.md](rag-coverage-matrix.md) | Vendor, product, version, and document-class RAG coverage |
| [release-indexes.md](release-indexes.md) | Build the tool/API indexes from committed specs, build the RAG corpus locally, and why the corpus is never released |
| [capability-gap-matrix.md](capability-gap-matrix.md) | Reproducible executable-tool, generated-operation, and pinned-benchmark comparison plus ranked practical gaps |
| [source-lifecycle-coverage.md](source-lifecycle-coverage.md) | Security/lifecycle source coverage, freshness states, and provenance pins |
| [source-drift-gates.md](source-drift-gates.md) | Classified drift gates for vendored sources, OpenAPI registries, and RAG indexes |
| [artifact-contracts.md](artifact-contracts.md) | Versioned/redacted artifact schemas and credential-gated live-test configuration shared by every v0.7 evaluator |
| [release-artifact-automation.md](release-artifact-automation.md) | Validation matrix, release bundle packaging, and restore/smoke-test tooling |
| [aos8-migration-contract-matrix.md](aos8-migration-contract-matrix.md) | Authoritative AOS8-to-Classic/New Central migration contract matrix gating 0.5.0 implementation |
| [aos8-live-dryrun-evaluation.md](aos8-live-dryrun-evaluation.md) | Read-only live/fixture-backed evaluation record of the AOS8 migration pipeline against the contract matrix |
| [workflow-authoring-standard.md](workflow-authoring-standard.md) | Mandatory docstring, coercion, pagination, bounded-output, write-gate, and test standard for new curated workflows |
| [benchmark-methodology.md](benchmark-methodology.md) | Credential-free benchmark scenario format, metric definitions, CI regression gate, and head-to-head publication contract |
| [observability.md](observability.md) | Health/readiness endpoints, the two-flag metrics opt-in, snapshot schema, and the ratified Prometheus exposition design |

### Releases and archive

| Doc | Use it for |
|---|---|
| [release-notes-0.9.0.md](release-notes-0.9.0.md) | Current release: RAG corpus expansion (28 scraped sources, 262,104 chunks), ANN+metadata indexes, content-hash dedup, Docker packaging, and operator runbooks |
| [release-notes-0.8.0.md](release-notes-0.8.0.md) | Clean repository/package rename, MCP 2 transport repair, PII protection, interop tools, GLP inventory completion, strict RAG/catalog facts, and classified drift gates |
| [release-notes-0.7.0.md](release-notes-0.7.0.md) | Artifact/live-test gates, source lifecycle provenance, structured RAG intelligence, Central/GLP/AOS8/optional-product depth, observability/security, router automation, and release artifact automation |
| [release-notes-0.6.0.md](release-notes-0.6.0.md) | Security/lifecycle RAG, expanded Central/GLP/AOS8/Axis/Mist coverage, provenance, audit logging, and migration reports |
| [release-notes-0.5.0.md](release-notes-0.5.0.md) | Verified AOS8 migration expansion: source hardening, bounded Classic Central write lifecycle, expanded fail-closed New Central mappings, verification taxonomy, and read-only live/dry-run evaluation |
| [release-notes-0.4.0.md](release-notes-0.4.0.md) | Complete 0.4.0 migration execution, Mist diagnostics, EdgeConnect compatibility, GLP, and Axis changes (historical) |
| [release-notes-0.3.0.md](release-notes-0.3.0.md) | Prior 0.3.0 platform, migration, safety, and API-source changes (historical) |
| [../CHANGELOG.md](../CHANGELOG.md) | Version history index into per-release notes |
| [aos8-live-lab-evaluation-0.6.md](aos8-live-lab-evaluation-0.6.md) | In-progress 0.6-era live-lab evaluation (archive) |
| [milvus-lite-pilot.md](milvus-lite-pilot.md) | Opt-in Milvus Lite pilot, not wired into the router (archive) |
| [session-handoff.md](session-handoff.md) | Working-session handoff notes (archive) |

## Visual asset workflow

Diagram sources live in `docs/diagrams/`; generated SVGs live in
`docs/assets/diagrams/`. Three source kinds render through one pipeline:

| Source | Rendered by | Used for |
| --- | --- | --- |
| `*.json` | `design` MCP server (`export_flow_diagram` -> Graphviz) | Flowcharts and architecture maps |
| `*.mmd` | Mermaid CLI | Sequence diagrams only |
| `*.term` | This repo's terminal renderer | Credential-free command transcripts |

Flow models are plain node/link JSON. `rankdir` picks the layout axis (`LR` or
`TB`) and `extra.shape` selects `box`, `decision`, `store`, or `terminal`.
GitHub embeds SVGs through `<img>`, which only ever scales them down, so
`--check` fails any flow diagram wider than 985pt: past that, an 896px README
column shrinks its labels below 10px. Switch `rankdir` when it fires.

```bash
# Render all diagrams and attach accessibility/source metadata
uv run python scripts/render_docs_diagrams.py

# Verify committed SVGs match their sources
uv run python scripts/render_docs_diagrams.py --check

# Run documentation regression tests
uv run pytest tests/unit/test_docs_visual_assets.py tests/unit/test_markdown_links.py -q
```

Every generated diagram or terminal SVG must include a title, description,
responsive dimensions, and source digest. Pair each image with explanatory
prose and keep commands or expected output copyable in Markdown. Use fake
identifiers and credentials; do not put customer data or secrets in diagrams,
alt text, or metadata.

Shared responsive cards, callouts, figures, badges, checkpoints, and next-step
panels are defined in `assets/css/style.scss`, loaded after the theme via
`_includes/head_custom.html`.

## Repo map

| Path | Purpose |
|---|---|
| `src/hpe_networking_mcp/mcp_servers/` | MCPServer backends, low-token router, prompts, middleware, optional product starters, and the always-loaded credential-free `interop-core` backend |
| `src/hpe_networking_mcp/cli/` | Doctor and pipeline/SSID console-script implementations |
| `src/hpe_networking_mcp/pipeline/` | Migration pipeline, typed clients, credentials loading, state store, SSID helpers |
| `ingestion/` | Docs/API ingestion into LanceDB and SQLite |
| `ingestion/source_manifest.json` | RAG source seeds for product docs, OpenAPI, security advisories, and end-of-sale/end-of-life lifecycle notices |
| `scripts/setup_wizard.py` | Guided install, Central region, credentials, optional products, MCP configs, catalog, and doctor |
| `scripts/build_spec_index.py` | Offline rebuild of `data/specs.sqlite` from the committed `vendor/openapi` corpus — no network, no scrape |
| `scripts/download_indexes.py` | Hardened, checksum-verified restore of an index archive from a release pin (`--manifest`) or one you host yourself (`--url`) |
| `scripts/package_indexes.py` | Snapshot and checksum local indexes for transfer between your own machines |
| `scripts/check_openapi_drift.py` / `scripts/check_mist_openapi_drift.py` | Detect Aruba ReadMe registry and official Mist OpenAPI changes |
| `scripts/check_nowireless_source_drift.py` | Read-only, path-specific freshness check for the pinned community `nowireless4u/hpe-networking-mcp` inputs (GLP vendored specs, Axis platform source, capability-benchmark evidence paths); community pins are benchmarks/inputs, not API authority -- see [capability-gap-matrix.md](capability-gap-matrix.md) |
| `scripts/check_security_lifecycle_drift.py` | Fresh/stale/unavailable/changed/coverage-gap state per security/lifecycle source, plus a bounded `source_freshness_result` artifact; see `ingestion/lifecycle_provenance.py` for the source-identity/schema pins under `ingestion/provenance/` |
| `scripts/report_capability_gaps.py` | Generate/check the reproducible capability gap matrix and pinned benchmark comparison |
| `scripts/generate_axis_manifest.py` | Rebuild/verify the deterministic SHA-pinned Axis Atmos Cloud manifest |
| `scripts/generate_edgeconnect_tools.py` | Fail-closed EdgeConnect Swagger/OpenAPI compatibility check and manifest generation |
| `src/hpe_networking_mcp/pipeline/aos8_migration_orchestrator.py` / `src/hpe_networking_mcp/pipeline/aos8_target_adapters.py` | Resumable AOS8 migration-run execution and guarded New Central/Classic target writes |
| `scripts/run_http_router.sh` | Start the minimal router over streamable HTTP |
| `docker-compose.yml` | Optional localhost-only Redis/Ollama server backend for power users |
| `docker-compose.router.yml` | Optional overlay: containerized MCP router service, behind an opt-in `router` Compose profile -- see [production-deployment.md](production-deployment.md) |
| `Dockerfile` | Production image for the streamable-HTTP router -- non-root, frozen dependencies, no baked-in secrets (the OpenAPI spec index is baked in at build time) |
| `secrets/` | `*.example` templates for the Docker secrets `docker-compose.router.yml` mounts (real files are git-ignored) |
| `scripts/doctor.py` | Check local setup without making API calls |
| `scripts/` | Tool-catalog ingestion, release validation, local sync helpers |
| `.mcp.json.example` | Generic stdio MCP client example using the minimal router |
| `.mcp.http.json.example` | Generic streamable HTTP MCP client example |
| `tests/unit/` | Mocked unit coverage for tools, clients, middleware, routing, RAG, release gates |
| `tests/eval/` | RAG/API eval data and runner |
| `data/` | Local built indexes, git-ignored |

## Common commands

```bash
# Guided local setup
python3 scripts/setup_wizard.py

# Guided setup with selected optional products
python3 scripts/setup_wizard.py --products clearpass,mist

# Build the RAG prose corpus locally (crawls vendor portals; hours)
# Needs the `ingestion` extra; `--backend redis` additionally needs `redis`.
uv run --extra ingestion python ingestion/ingest_docs.py

# Build the router tool catalog
uv run python scripts/ingest_tools.py

# Include optional product starters in the tool catalog
uv run python scripts/ingest_tools.py --products all

# Include every guarded write tool (6,728 backend tools)
uv run python scripts/ingest_tools.py --complete-catalog

# Start the model-agnostic HTTP MCP router
MCP_PORT=8010 bash scripts/run_http_router.sh

# Check local setup without API calls
uv run hpe-mcp-doctor

# Run unit tests
uv run pytest tests/unit -q

# Run the full local release gate
uv run python scripts/validate_release.py --catalog-products all --strict-tool-index --min-tools 6711
```

The wizard can run `uv sync`, choose common Central API gateways, fill secrets
with no echo, write local `.env` for selected optional products, and add only
the product selector to local stdio MCP configs. The HTTP helper safely loads
expected `.env` assignments first and exits with listener details instead of
starting a duplicate router when the selected port is already in use.

The release helper enforces the documented tool catalog floor and checks local LanceDB tool-index freshness when `data/tools.lance` exists. `--min-tools 6711` is the platform API compatibility floor (the 6,711 vendor-facing platform API tools), not the complete registered backend total of 6,728, which also includes the protocol-only Central Streaming tool, the cross-platform site-health aggregator, the local GLP preflight diagnostic, and credential-free local tools — validation passes at or above the floor; see [Tool catalog](tool-catalog.md) for both totals. The unit suite also carries static regression guards for async-safe MCP tools, shared `httpx` client boundaries, project metadata (`hpe-networking-mcp` package name with no direct sync SDK/`requests` runtime dependencies), committed low-token MCP config examples, local-only config files, router product/toolset docs, bounded generic read-only GET tools, MCP list default bounds, RAG/search top_k bounds, public tool-count claims, tool-count docstrings, rendered RAG/index doc-fact claims, tracked Markdown local links and images, Pages sitemap and robots metadata, documented router example arguments, product workflow tool-name tables, and wizard optional-product env tables.
