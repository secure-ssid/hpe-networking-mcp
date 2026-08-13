# hpe-networking-mcp 0.7.0

Version 0.7.0 adds versioned/redacted artifact contracts and credential-gated
live-test configuration used across every workstream, deepens Central, GLP,
and optional-product coverage against authoritative sources, expands RAG with
exact structured advisory/lifecycle listing and correlation, documents the
security/lifecycle source coverage boundary explicitly, adds router-native
workflow planning with opaque response-continuation cursors, adds opt-in
observability (audit log and metrics), and ships end-to-end release artifact
automation (validation matrix, deterministic release bundle, restore/
smoke-test, SBOM, and provenance). No live vendor writes were performed to
produce this release; every live-capable evaluator stays fail-closed behind
an explicit environment opt-in and reports its gate status honestly when
that opt-in is absent.

## Catalog snapshot

| Metric | Count |
|---|---:|
| Generated manifest operations | 6,143 |
| Active generated tools | 6,126 |
| Curated tools | 573 |
| Complete backend tools (read-write, generated GLP included) | 6,699 |
| Direct-all client-visible tools | 6,702 |
| Core profile | 360 |
| Optional read-only profile | 2,813 |
| Optional read-write profile | 5,795 |
| Capability totals | 3,147 read / 165 diagnostic / 2,544 write / 843 destructive |

The default minimal router still exposes only `find_tool`,
`invoke_read_tool`, and `invoke_tool`. Default (non-minimal) router mode
grew from 12 to 14 client-visible tools with the addition of
`plan_tool_workflow` and `plan_reconciliation_schedule` (see
[Router automation](#router-automation-and-response-continuation) below).

Compared with 0.6.0 (6,056 generated / 6,039 active / 506 curated / 6,545
backend tools), the largest single contributor is Juniper Apstra's generated
manifest, which grew from 48 to 135 operations by re-inspecting the same
pinned `aos-sdk-api` 6.1.2.post1 SDK for top-level resource pools, device/rack
profiles, system agents, telemetry, and blueprint-scoped IBA. The remaining
growth is curated: five Central workflows, ten ClearPass workflows, one Mist
workflow, five EdgeConnect workflows, and the GLP/RAG additions below.

See the [capability gap matrix](capability-gap-matrix.md), [tool
catalog](tool-catalog.md), and [release index guide](release-indexes.md) for
the full reproducible breakdown.

## Artifact contracts and live-test configuration

Every v0.7 evaluator, compatibility checker, and release-packaging step
reuses one shared, versioned, bounded, redacted set of contracts instead of
inventing another ad hoc JSON shape:

- `src/hpe_networking_mcp/pipeline/artifact_contracts.py` defines eight schema-versioned artifact
  kinds (live lifecycle evidence, platform compatibility results, migration
  report metadata, capability snapshots, source-freshness results, release
  artifact manifests, router dependency/reconciliation plans, and the v0.7
  validation-matrix result), each validated on construction and redacted
  (secrets, tenant/workspace/account/scope identifiers, and known sensitive
  values in free text) before `write_artifact` serializes and atomically
  writes it.
- `src/hpe_networking_mcp/pipeline/live_test_config.py` generalizes the AOS8 0.6 lab-evaluation
  gating into a reusable, default-disabled, per-platform (Central, GLP,
  AOS8, EdgeConnect, Apstra, Mist, ClearPass, UXI, Axis) read/write opt-in.
  Credential presence never implies authorization; write access always
  requires the read opt-in too.

See [Artifact contracts and live-test configuration](artifact-contracts.md).

## Central v0.7 depth workflows

Five new schema-backed workflows, all verified against the committed
OpenAPI source or the generated Central manifest before implementation, with
dry-run defaults, explicit confirmation, existing write gates, validated
write results, and read-back verification:

- VSF stacking-template lifecycle (`build_vsf_template` /
  `delete_vsf_template`), the only schema-backed generic "template" resource
  in New Central.
- Bulk site / site-collection delete (`delete_sites_bulk` /
  `delete_site_collections_bulk`), up to 100 IDs per call.
- Firmware-compliance campaigns (`run_firmware_compliance_campaign`) across
  up to 25 scope/persona targets with independent per-target failure
  isolation.
- Config-health remediation planning and bounded, chunked execution
  (`plan_config_health_remediation` / `execute_config_health_remediation`).
- Bounded troubleshooting orchestration (`run_troubleshooting_bundle`)
  composing existing ARP/LLDP/ping/show tools for CX and AOS-S.

A credential-gated live evaluator
(`scripts/evaluate_central_070_readonly.py`) and a disposable-write
create/read-back/delete VSF-template harness ship alongside; both stay
gated behind `src/hpe_networking_mcp/pipeline/live_test_config.py` and were not run live in this
environment (the read gate was unset here). See [Central v0.7
workflows](central-v07-workflows.md).

## GreenLake Platform depth

GLP's curated tool count grew from 76 to 105 with region-aware read
coverage for Compute Ops Management (servers, alerts, groups, jobs), Storage
Fleet and Block Storage (systems, volumes, hosts), Virtualization and
guarded VM power (VMs, hypervisor managers/clusters, datastores, single and
bounded bulk power operations), Backup & Recovery status plus a guarded
run-protection-job-now write, Data Services issues and async operations, and
a new read-only `plan_glp_reconciliation` composite that flags likely drift
across devices/subscriptions/users/RBAC/scope groups/audit logs/reporting
without ever writing. A credential-gated live evaluator and disposable-write
harness (`scripts/evaluate_glp_070_depth.py`) exercise a bounded sample of
these tools against a real workspace only when explicitly enabled.

## Optional product depth (`v07-optional-depth`)

Every optional backend gained authoritative-source-grounded depth without
inventing an endpoint:

- **Apstra**: top-level resource pools, device/rack profiles, system
  agents, telemetry, and blueprint-scoped IBA, re-derived from the same
  pinned SDK; unmodeled verbs are recorded as explicit `coverage_gaps`
  provenance rather than guessed at.
- **ClearPass**: typed Access Tracker session search/disconnect, endpoint
  and guest inventory/create, policy (roles, enforcement policies), service
  management (list/get/enable-disable), syslog export configuration, and
  diagnostics (server version, cluster servers).
- **Mist**: typed org/site SLE assurance summary reads alongside the
  existing assurance-snapshot composite.
- **EdgeConnect**: confirmed alarm acknowledge/clear/summary and flow
  list/stats workflows.
- **Axis**: a three-layer evaluation harness
  (`scripts/evaluate_axis_lab.py`) — an always-on offline split-CRUD
  contract check across all 11 entity families, a bounded opt-in read-only
  live check, and a disposable-write plan that is only ever generated and
  hashed, never executed.
- **UXI**: confirmed the 25-operation manifest is fully exposed; the one
  permanent upstream gap (service tests have no create/update/delete API)
  is documented rather than worked around.

Every backend also gets one bounded, redacted evidence artifact via
`scripts/build_optional_product_evidence.py`. See [Optional product
starters](optional-products.md#v07-optional-product-depth-v07-optional-depth).

## ArubaOS 8 rollback planning

`src/hpe_networking_mcp/pipeline/aos8_rollback.py` adds reverse-dependency-order rollback/
compensation *planning*, and separately gated execution, for previously
*applied* migration-run candidates. Every rollback step is derived from the
same already-reviewed target-adapter mapping used at apply time; a
candidate whose object type has no verified inverse (for example `vlan`) is
always explicitly refused, never approximated. A credential-gated
disposable-write lifecycle evaluator
(`scripts/evaluate_aos8_070_disposable_lifecycle.py`) exercises this against
a lab-owned target only when explicitly enabled.

## RAG: structured security/lifecycle intelligence expansion

Building on 0.6's exact `lookup_advisory`/`check_product_lifecycle` tools,
`rag-core` adds four bounded, read-only tools (573 curated backend tools
now include RAG's growth from 5 to 9):

- `list_advisories` / `list_lifecycle_events` — paginated (`limit` ≤ 200)
  listing with exact filters (product/model, CVE, advisory ID, severity
  floor, SKU, category, event type, source family, date range).
- `correlate_advisory_lifecycle` — links an advisory's products to
  lifecycle records only on exact, normalized string equality; every
  response separates `exact_matches` from `unresolved_products` with an
  explicit `match_basis` — never a fuzzy/semantic guess.
- `rag_diagnostics` — combines citation-completeness, source-freshness, and
  ingestion-delta checks, all read-only and network-free.
- `ask_docs` now routes a literal CVE or vendor advisory ID straight to
  `lookup_advisory`, the same way it already routes API-shaped questions to
  `lookup_api`.

The RAG eval set grew from 24 to 31 questions to cover negative queries, a
documented coverage-gap query, and one row per new structured tool type. All
31 questions now hit at rank 1 (`source_hit@k` / `mrr` / `howto_recall@k` /
`api_exact` / `structured_exact` / `structured_list_exact` = 1.00 on the
current 51,737-chunk / 244-spec index). See [RAG
architecture](architecture/RAG-ARCHITECTURE.md#v07--structured-securitylifecycle-intelligence-expansion).

## Security/lifecycle source coverage and provenance

[Source lifecycle coverage](source-lifecycle-coverage.md) now documents,
with verified evidence, that there is **no reliable official
machine-readable source for current (post-2020) Aruba-branded lifecycle
notices** beyond the historical End-of-Sale archive and a static 2020 PDF.
This is recorded as an explicit, permanent `coverage_gap` state — never
reported as `fresh` — and `correlate_advisory_lifecycle` /
`check_product_lifecycle` answer honestly (empty, not a fabricated "still
supported") for current Aruba products.

`ingestion/lifecycle_provenance.py` adds committed provenance pins
(`ingestion/provenance/*.json`) recording each security/lifecycle source
family's exact endpoint URLs and parser-dependent structural markers, so an
unreviewed source-URL or schema change is rejected as `changed` rather than
silently mis-parsed. Juniper Mist/Apstra lifecycle discovery now also
merges the reviewed seed URLs with whatever the official Juniper EOL index
nav currently discloses, deduplicated by URL.

## Router automation and response continuation

- **`plan_tool_workflow`** and **`plan_reconciliation_schedule`** are two
  new read-only, plan-only router-native tools (outside `minimal` mode):
  deterministic dependency-ordered workflow planning (Kahn's algorithm,
  caller-order tie-breaking, cycle detection) and recurring reconciliation
  schedule specification (named cadence, bounded interval, or
  syntactically-validated 5-field cron) over the router's own loaded
  catalog. Both are pure planning — `plan_reconciliation_schedule`'s
  `dry_run` is always `True`, and neither ever executes a tool.
- **Response budgets**: every `invoke_tool`/`invoke_read_tool` result now
  passes through a deterministic, configurable bounding step
  (`HPE_MCP_ROUTER_RESPONSE_MAX_ITEMS`, default 200;
  `HPE_MCP_ROUTER_RESPONSE_MAX_BYTES`, default 200,000). An in-budget
  response is returned byte-for-byte unchanged.
- **Opaque continuation cursors** (`invoke_read_tool` only): a clipped
  response gains a `next_cursor` string and `resumable: true`; passing that
  cursor back on a repeated call with the same tool/arguments fetches the
  next page. `invoke_tool` never emits or accepts a cursor.
- `scripts/generate_router_automation_report.py` produces versioned,
  redacted `router_dependency_plan` / `router_reconciliation_plan`
  artifacts fully offline, for release evidence.

See [Tool router: observability](tool-router.md#observability-audit-log-and-metrics)
and [response budgets/cursors](tool-router.md#response-budgets-and-continuation-metadata).

## Observability and security hardening

- **Opt-in redacted audit log** (`HPE_MCP_AUDIT_LOG=1`): one JSONL record
  per completed/failed router call (`run_id`, bounded `session_id`,
  capability classification, tool/target-tool, argument key names, a
  SHA-256 argument digest, outcome, duration, and error type) — argument
  and result *values* are never written.
- **Opt-in bounded in-process metrics** (`HPE_MCP_METRICS=1`, optional
  `HPE_MCP_METRICS_HTTP=1` for a `GET /metrics` snapshot on streamable
  HTTP): request/latency/outcome counters bucketed by an allow-listed,
  capped `(tool, backend)` label set with a fixed overflow bucket beyond
  512 series. Metrics never read argument values, result values, or
  exception messages.
- A shared `classify_outcome` helper (`src/hpe_networking_mcp/mcp_servers/_middleware/_outcome.py`)
  keeps the audit log and metrics middleware's "what happened" bucketing
  from silently diverging.
- Added regression coverage (`tests/unit/test_observability_secret_leak.py`)
  asserting neither middleware can leak a secret value through any log or
  metrics path.

## Release artifact automation

- **Validation matrix** (`scripts/run_v07_validation_matrix.py`) classifies
  every v0.7 coverage category (Central, GLP, AOS8, each optional product,
  Axis, RAG/source-freshness, router automation) into exactly one of
  `offline_fixture` / `live_read` / `disposable_write` / `blocked` /
  `unavailable` / `coverage_gap`, without making a live call itself.
- **Release bundle packaging** (`scripts/build_release_bundle.py`)
  assembles validation-matrix, capability-snapshot, optional-product, Axis,
  and router-automation evidence; a deterministic CycloneDX 1.5 SBOM
  (`src/hpe_networking_mcp/pipeline/sbom.py`) from `uv.lock`; a `CHECKSUMS.txt`; a
  `provenance.json` (builder identity + SHA-256 subjects — not a signed
  attestation); a `release-manifest.json`; and prebuilt RAG/OpenAPI indexes
  when present locally — into one deterministic `.tar.gz` plus `.sha256`.
- **Restore/smoke-test** (`scripts/restore_release_bundle.py`) generalizes
  the existing safe-extraction pattern with file-count/size bounds, path-
  traversal and non-regular-file rejection, a repository-root/guarded-
  directory extraction refusal, checksum verification, and post-extraction
  schema re-validation of every manifest-listed file against its own
  artifact contract.
- **`.github/workflows/release-artifacts.yml`** builds the bundle, restores/
  smoke-tests it, uploads it as a workflow artifact, and (tag pushes only)
  runs `actions/attest-build-provenance`, using least-required permissions.
  It never publishes a GitHub Release and never runs on a schedule.

See [Release artifact automation](release-artifact-automation.md).

## Safety model (unchanged posture)

- No live vendor writes were performed to produce this release. Live-
  capable evaluators are fail-closed by default and require an explicit,
  documented environment opt-in per platform.
- Optional product writes remain hidden and blocked in read-only mode
  (`HPE_MCP_PRODUCT_ACCESS=read-only` default).
- Generic dispatch (`invoke_tool`) remains explicitly destructive-capable;
  read-only dispatch continues through `invoke_read_tool`.
- Every new write workflow (Central VSF/bulk-delete/firmware campaigns, GLP
  VM power/backup run-now, ClearPass/EdgeConnect/Axis guarded writes)
  defaults to `dry_run=True` and requires explicit `confirm=True`.

## Upgrade instructions

1. Re-sync dependencies: `uv sync --frozen`.
2. Rebuild the router tool index for your desired profile:
   ```bash
   uv run python scripts/ingest_tools.py --products all
   # or, for the full read-write release catalog:
   HPE_MCP_PRODUCT_ACCESS=read-write HPE_MCP_GLP_GENERATED_TOOLS=1 \
     uv run python scripts/ingest_tools.py --products all
   ```
3. RAG/OpenAPI indexes are unchanged in content for this release (51,737
   prose chunks, 244 specs); re-download or rebuild only if you maintain
   your own local copy and want to confirm it matches
   `docs/release-indexes.md`.
4. No credential, environment-variable, or config-file schema changes are
   required. New env vars (`HPE_MCP_AUDIT_LOG`, `HPE_MCP_METRICS`,
   `HPE_MCP_METRICS_HTTP`, `HPE_MCP_LIVE_TEST_<PLATFORM>_READ`/
   `_WRITE`) are additive and default off.
5. Run `uv run python scripts/validate_release.py --catalog-products all --strict-rag --strict-tool-index --min-tools 6699` before publishing from a fork or downstream branch.

## Known coverage gaps

Carried forward from the [capability gap matrix](capability-gap-matrix.md),
unchanged in kind by this release:

1. **ArubaOS 8** — broader verified migration mappings and live evaluation
   beyond the verified subset; rollback planning is new in 0.7, but it only
   ever compensates for actions this repo's own adapters can already apply.
2. **EdgeConnect** — a real current 9.3+ Orchestrator Swagger has not yet
   been acquired and validated through the compatibility doctor.
3. **Axis Atmos Cloud** — the 47-operation manifest remains a reviewed
   benchmark-derived registry, not an official Axis specification or
   target-verified capture.
4. **Cross-platform** — most workflows are validated against fixtures and
   manifests rather than sustained live estates; this release's evaluators
   are fixture-backed by default and were not run live here.
5. **Current-Aruba lifecycle coverage** (new, explicitly documented in
   0.7) — no reliable official machine-readable source exists for current
   Aruba-branded lifecycle notices; `check_product_lifecycle` and
   `correlate_advisory_lifecycle` correctly report empty/`unresolved`
   rather than guessing.

## Validation summary

Reproduced for this release:

```bash
uv run python -m pytest tests/unit -q                # 2,540 passed
uv run --with pyyaml python tests/eval/run_eval.py    # 31/31 questions, all metrics 1.00
uv run python scripts/report_capability_gaps.py --check   # docs/capability-gap-matrix.md is current
git diff --check                                      # clean
uv run python scripts/run_v07_validation_matrix.py --output outputs/validation-matrix.json
uv run python scripts/build_release_bundle.py --output-dir dist
uv run python scripts/restore_release_bundle.py dist/hpe-networking-mcp-release-artifacts-v0.7.0.tar.gz
```

No live vendor API calls were made anywhere in this validation pass.
`HPE_MCP_LIVE_TEST_<PLATFORM>_READ`/`_WRITE` were left unset throughout, so
the validation matrix classified every live-capable category as `blocked`
(safe default) except the always-on offline self-checks, which reported
`offline_fixture`: `apstra`, `clearpass`, `edgeconnect`, `mist`, `uxi`, `axis`
(compatibility + split-CRUD contract), and `router_automation` (dependency
plan against the 6-server enabled backend catalog). `central`, `glp`,
`aos8`, and `rag_source_freshness` reported `blocked` (no unset opt-in
attempted, matching the safety model above).

Release bundle `hpe-networking-mcp-release-artifacts-v0.7.0.tar.gz` (built locally,
not published): CycloneDX 1.5 SBOM with 99 components; a human/CI-readable
`provenance.json` recording the local build source commit and every subject
file's SHA-256 (not a signed attestation — GitHub artifact attestation runs
separately in CI from the same `CHECKSUMS.txt` subjects); an 11-file redacted
evidence set (validation matrix, capability snapshot, six optional-product
compatibility/lab results, two router-automation plans) each schema-
validated against its own artifact contract; and the prebuilt
`hpe-networking-mcp-rag-index-v0.7.0.tar.gz` (51,737 prose chunks / 244 specs).
`scripts/restore_release_bundle.py` extracted 17 members (511,626,575 bytes)
into a throwaway directory, verified the archive checksum, and
schema/structurally validated all 13 manifest-listed files, then cleaned up.
Neither the bundle nor `outputs/validation-matrix.json` is committed —
both are gitignored, release-only build products.

See the [capability gap matrix](capability-gap-matrix.md), [tool
catalog](tool-catalog.md), and [release index guide](release-indexes.md) for
reproducible counts and packaging details. The [0.6.0 release
notes](release-notes-0.6.0.md) and earlier notes remain available for
historical context.
