# hpe-networking-mcp documentation

The GitHub Pages site is published from `main/docs` with Jekyll and the Cayman
theme. User journeys are written in Markdown; diagrams are authored in Mermaid
and committed as accessible SVG files so they render without browser-side
JavaScript.

## Start here

| Doc | Use it for |
|---|---|
| [index.md](index.md) | Task-based front door for new users, network operators, and contributors |
| [getting-started.md](getting-started.md) | Wizard install, credentials, optional products, MCP client setup, and indexes |
| [mcp-client-recipes.md](mcp-client-recipes.md) | Copy/paste stdio and streamable HTTP MCP client setup recipes |
| [../examples/README.md](../examples/README.md) | Tested, non-secret example configs: minimal/full stdio, local/bearer HTTP, Copilot CLI, and prompt/runbook examples |
| [../MIGRATION.md](../MIGRATION.md) | Local migration path from the legacy `secure-ssid/centralmcp` repository |
| [../CHANGELOG.md](../CHANGELOG.md) | Version history index into per-release notes |
| [tool-router.md](tool-router.md) | Low-token discovery, dispatch, and write-safety behavior |
| [example-prompts.md](example-prompts.md) | Complete scenarios with calls, expected shapes, and safety labels |
| [troubleshooting.md](troubleshooting.md) | Outcome-driven setup, authentication, transport, catalog, and RAG fixes |
| [optional-products.md](optional-products.md) | Optional product matrix, wizard behavior, env vars, and safety surface |
| [architecture/system-overview.md](architecture/system-overview.md) | Runtime, data, transport, and safety architecture |
| [product-workflows.md](product-workflows.md) | Typed ClearPass/Mist/Apstra/AOS8/EdgeConnect/UXI workflow roadmap |
| [aos8-migration-contract-matrix.md](aos8-migration-contract-matrix.md) | Authoritative AOS8-to-Classic/New Central migration contract matrix gating 0.5.0 implementation |
| [aos8-live-dryrun-evaluation.md](aos8-live-dryrun-evaluation.md) | Read-only live/fixture-backed evaluation record of the AOS8 migration pipeline against the contract matrix |
| [release-indexes.md](release-indexes.md) | Download, package, and release prebuilt RAG/OpenAPI indexes |
| [release-notes-0.8.0.md](release-notes-0.8.0.md) | Clean repository/package rename, MCP 2 transport repair, PII protection, interop tools, GLP inventory completion, strict RAG/catalog facts, and classified drift gates |
| [release-notes-0.7.0.md](release-notes-0.7.0.md) | Artifact/live-test gates, source lifecycle provenance, structured RAG intelligence, Central/GLP/AOS8/optional-product depth, observability/security, router automation, and release artifact automation |
| [central-v07-workflows.md](central-v07-workflows.md) | Central v0.7 depth workflows: VSF template lifecycle, bulk site/site-collection delete, firmware-compliance campaigns, config-health remediation, troubleshooting orchestration |
| [artifact-contracts.md](artifact-contracts.md) | Versioned/redacted artifact schemas and credential-gated live-test configuration shared by every v0.7 evaluator |
| [release-artifact-automation.md](release-artifact-automation.md) | Validation matrix, release bundle packaging, and restore/smoke-test tooling |
| [source-lifecycle-coverage.md](source-lifecycle-coverage.md) | Security/lifecycle source coverage, freshness states, and provenance pins |
| [release-notes-0.6.0.md](release-notes-0.6.0.md) | Security/lifecycle RAG, expanded Central/GLP/AOS8/Axis/Mist coverage, provenance, audit logging, and migration reports |
| [release-notes-0.5.0.md](release-notes-0.5.0.md) | Verified AOS8 migration expansion: source hardening, bounded Classic Central write lifecycle, expanded fail-closed New Central mappings, verification taxonomy, and read-only live/dry-run evaluation |
| [release-notes-0.4.0.md](release-notes-0.4.0.md) | Complete 0.4.0 migration execution, Mist diagnostics, EdgeConnect compatibility, GLP, and Axis changes |
| [release-notes-0.3.0.md](release-notes-0.3.0.md) | Prior 0.3.0 platform, migration, safety, and API-source changes (historical) |
| [capability-gap-matrix.md](capability-gap-matrix.md) | Reproducible executable-tool, generated-operation, and pinned-benchmark comparison plus ranked practical gaps |
| [tool-catalog.md](tool-catalog.md) | Per-backend counts, capability families, build modes, and safety notes |
| [architecture/RAG-ARCHITECTURE.md](architecture/RAG-ARCHITECTURE.md) | Embedded RAG design, eval results, and migration rationale |

## Visual asset workflow

Mermaid source files live in `docs/diagrams/`; generated SVGs live in
`docs/assets/diagrams/`.

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
panels are defined in `assets/css/style.scss`.

## Repo map

| Path | Purpose |
|---|---|
| `src/hpe_networking_mcp/mcp_servers/` | MCPServer backends, low-token router, prompts, middleware, optional product starters, and the always-loaded credential-free `interop-core` backend |
| `src/hpe_networking_mcp/cli/` | `hpe-mcp-doctor`, `hpe-mcp-run-pipeline`, `hpe-mcp-run-ssid` console-script implementations |
| `src/hpe_networking_mcp/pipeline/` | Migration pipeline, typed clients, credentials loading, state store, SSID helpers |
| `ingestion/` | Docs/API ingestion into LanceDB and SQLite |
| `ingestion/source_manifest.json` | RAG source seeds for product docs, OpenAPI, security advisories, and end-of-sale/end-of-life lifecycle notices |
| `scripts/setup_wizard.py` | Guided install, Central region, credentials, optional products, MCP configs, catalog, and doctor |
| `scripts/download_indexes.py` | Restore prebuilt docs/API/tool indexes from GitHub Releases |
| `scripts/package_indexes.py` | Package local indexes for a GitHub Release asset |
| `scripts/check_openapi_drift.py` / `scripts/check_mist_openapi_drift.py` | Detect Aruba ReadMe registry and official Mist OpenAPI changes |
| `scripts/check_nowireless_source_drift.py` | Read-only, path-specific freshness check for the pinned community `nowireless4u/hpe-networking-mcp` inputs (GLP vendored specs, Axis platform source, capability-benchmark evidence paths); community pins are benchmarks/inputs, not API authority -- see [capability-gap-matrix.md](capability-gap-matrix.md) |
| `scripts/check_security_lifecycle_drift.py` | Fresh/stale/unavailable/changed/coverage-gap state per security/lifecycle source, plus a bounded `source_freshness_result` artifact; see `ingestion/lifecycle_provenance.py` for the source-identity/schema pins under `ingestion/provenance/` |
| `scripts/report_capability_gaps.py` | Generate/check the reproducible capability gap matrix and pinned benchmark comparison |
| `scripts/generate_axis_manifest.py` | Rebuild/verify the deterministic SHA-pinned Axis Atmos Cloud manifest |
| `scripts/generate_edgeconnect_tools.py` | Fail-closed EdgeConnect Swagger/OpenAPI compatibility check and manifest generation |
| `src/hpe_networking_mcp/pipeline/aos8_migration_orchestrator.py` / `src/hpe_networking_mcp/pipeline/aos8_target_adapters.py` | Resumable AOS8 migration-run execution and guarded New Central/Classic target writes |
| `scripts/run_http_router.sh` | Start the minimal router over streamable HTTP |
| `docker-compose.yml` | Optional localhost-only Redis/Ollama server backend for power users |
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

# Download prebuilt RAG/OpenAPI indexes
uv run python scripts/download_indexes.py

# Build the router tool catalog
uv run python scripts/ingest_tools.py

# Include optional product starters in the tool catalog
uv run python scripts/ingest_tools.py --products all

# Include every guarded write tool (6,717 backend tools)
uv run python scripts/ingest_tools.py --complete-catalog

# Start the model-agnostic HTTP MCP router
MCP_PORT=8010 bash scripts/run_http_router.sh

# Check local setup without API calls
uv run hpe-mcp-doctor

# Run unit tests
uv run pytest tests/unit -q

# Run the full local release gate
uv run python scripts/validate_release.py --catalog-products all --strict-rag --strict-tool-index --min-tools 6705
```

The wizard can run `uv sync`, choose common Central API gateways, fill secrets
with no echo, write local `.env` for selected optional products, and add only
the product selector to local stdio MCP configs. The HTTP helper safely loads
expected `.env` assignments first and exits with listener details instead of
starting a duplicate router when the selected port is already in use.

The release helper enforces the documented tool catalog floor and checks local LanceDB tool-index freshness when `data/tools.lance` exists. `--min-tools 6705` is the platform API compatibility floor (the 6,705 vendor-facing platform API tools), not the complete registered backend total of 6,717 — validation passes at or above the floor; see [Tool catalog](tool-catalog.md) for both totals. The unit suite also carries static regression guards for async-safe MCP tools, shared `httpx` client boundaries, project metadata (`hpe-networking-mcp` package name with no direct sync SDK/`requests` runtime dependencies), committed low-token MCP config examples, local-only config files, router product/toolset docs, bounded generic read-only GET tools, MCP list default bounds, RAG/search top_k bounds, public tool-count claims, tool-count docstrings, rendered RAG/index doc-fact claims, tracked Markdown local links and images, Pages sitemap and robots metadata, documented router example arguments, product workflow tool-name tables, and wizard optional-product env tables.
