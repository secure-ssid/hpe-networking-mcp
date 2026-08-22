---
title: "Artifact contracts"
nav_order: 9
parent: "Reference"
---

# v0.7 artifact contracts and live-test configuration

This page documents the shared, versioned artifact schemas and the
credential-gated live-test configuration that every v0.7 workstream (live
evaluation harnesses, platform-compatibility checks, migration reporting,
capability benchmarking, source-freshness/drift checks, and release
packaging) reuses instead of inventing another ad hoc JSON shape or
environment-variable convention.

## Locations

| Module | Purpose |
|---|---|
| [`src/hpe_networking_mcp/pipeline/artifact_contracts.py`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/src/hpe_networking_mcp/pipeline/artifact_contracts.py) | Versioned, bounded, redacted artifact schemas and the atomic JSON writer. |
| [`src/hpe_networking_mcp/pipeline/live_test_config.py`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/src/hpe_networking_mcp/pipeline/live_test_config.py) | Credential-gated, default-disabled per-platform live-test read/write configuration. |
| [`src/hpe_networking_mcp/pipeline/compliance.py`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/src/hpe_networking_mcp/pipeline/compliance.py) | Pure, network-free declarative compliance-policy evaluation (bounded operators, safe field extraction, no eval/exec) backing `evaluate_compliance_policy`. |
| [`tests/unit/test_artifact_contracts.py`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/tests/unit/test_artifact_contracts.py) | Contract validation, bounds, redaction, and digest determinism tests. |
| [`tests/unit/test_live_test_config.py`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/tests/unit/test_live_test_config.py) | Default-disabled, explicit-opt-in, and no-leak status API tests. |
| [`tests/unit/test_compliance.py`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/tests/unit/test_compliance.py) | Field extraction, every operator, fail-closed policy validation, bounds, and aggregate-report tests for `src/hpe_networking_mcp/pipeline/compliance.py`. |

Neither module makes network calls or writes indexes/release artifacts on
import; they are pure validation/serialization helpers for callers to use
inside future v0.7 scripts (in the spirit of
[`scripts/evaluate_aos8_060_lab.py`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/scripts/evaluate_aos8_060_lab.py)).

## Artifact kinds

Every artifact kind is explicitly schema-versioned (`SCHEMA_VERSIONS`,
starting at `1`), JSON serializable, and validated on construction
(`ArtifactValidationError` on any missing/malformed field or an
out-of-bound collection):

| Kind constant | Contract dataclass(es) | Represents |
|---|---|---|
| `LIVE_LIFECYCLE_EVIDENCE` | `LiveLifecycleEvidence` | Bounded evidence from one live read-only or disposable-write lifecycle probe against a real platform. |
| `PLATFORM_COMPATIBILITY_RESULT` | `PlatformCompatibilityEntry`, `PlatformCompatibilityMatrix` | One or more per-platform compatibility verdicts -- a one-entry matrix *is* a "result". |
| `MIGRATION_REPORT_METADATA` | `MigrationReportMetadata` | Metadata *about* a migration report (counts, formats, hashed device references) -- never the raw per-device rows, which stay in `src/hpe_networking_mcp/pipeline/reporter.py`'s CSV/JSON/HTML outputs. |
| `CAPABILITY_SNAPSHOT` | `PlatformCapabilityCount`, `CapabilitySnapshot` | Per-platform read/diagnostic/write/destructive tool counts, the reproducible core of `scripts/report_capability_gaps.py`. |
| `SOURCE_FRESHNESS_RESULT` | `SourceFreshnessEntry`, `SourceFreshnessSnapshot` | Per-source freshness/drift counts versus a minimum, the reproducible core of `scripts/check_security_lifecycle_drift.py`. |
| `RELEASE_ARTIFACT_MANIFEST` | `ManifestEntry`, `ReleaseArtifactManifest` | The manifest of artifact files produced for a release: filename, kind, schema version, size, SHA-256, generation timestamp, and redaction status for each entry. |
| `ROUTER_DEPENDENCY_PLAN` | `RouterPlanStep`, `RouterDependencyPlan` | A bounded, deterministic, read-only dependency/order plan produced by `hpe_networking_mcp.mcp_servers.tool_router.plan_tool_workflow` -- never a record of an executed workflow. |
| `ROUTER_RECONCILIATION_PLAN` | `ReconciliationEntry`, `RouterReconciliationPlan` | A bounded, read-only, plan-only recurring reconciliation schedule specification produced by `hpe_networking_mcp.mcp_servers.tool_router.plan_reconciliation_schedule`; `dry_run` is always `True`. |
| `VALIDATION_MATRIX_RESULT` | `ValidationMatrixEntry`, `ValidationMatrix` | The per-category (platform, RAG/source-freshness, router-automation) credential-gated classification produced by `scripts/run_v07_validation_matrix.py`: `offline_fixture`, `live_read`, `disposable_write`, `blocked`, `unavailable`, or `coverage_gap`, plus whether reads/writes are enabled and credentials are configured -- never raw credential values. |
| `COMPLIANCE_REPORT` | `ComplianceRuleResult`, `ComplianceObservationSummary`, `ComplianceReport` | A bounded, declarative compliance-policy evaluation report produced by `hpe_networking_mcp.mcp_servers.tool_router.evaluate_compliance_policy` (see `src/hpe_networking_mcp/pipeline/compliance.py`): per-rule `pass`/`fail`/`error`/`skipped` results and per-observation/aggregate counts against caller-supplied, already-retrieved observations -- never a live fetch, and `compliant` can never be `True` while any `fail`/`error` result exists. Every result's `actual` is recursively redacted (mirroring `hpe_networking_mcp.mcp_servers.shared`'s sensitive-key and this module's tenant-key semantics against every field-path segment, not just container keys) and depth/collection/string/byte-bounded *before* it ever reaches this contract, so a valid `ComplianceRuleResult.actual` always fits this contract's own serialized-size ceiling. |

Every collection that can grow has a hard, fail-closed bound (for example
`MAX_EVIDENCE_STEPS`, `MAX_MATRIX_PLATFORMS`, `MAX_MANIFEST_ENTRIES`) --
validation raises instead of silently truncating evidence.

## Redaction

Before any artifact is written, `redact_artifact_payload` runs these passes:

1. **Secrets** -- reuses `hpe_networking_mcp.mcp_servers.shared.redact_sensitive` (not
   reimplemented) to strip tokens, passwords, API keys, PSKs, and bearer/
   basic auth header values.
2. **Tenant/workspace/account/scope identifiers and raw response bodies** --
   fields such as `tenant_id`, `workspace_id`, `account_id`, `customer_id`,
   `glp_workspace_id`, `scope_id`/`scope_name`, and `cluster_scope_id` are
   replaced with a deterministic, irreversible `sha256:<hex12>` placeholder
   (`hash_identifier`), matching the convention already established by
   `scripts/evaluate_aos8_050_readonly.py`'s `_sanitize_identifier`. Keys
   that look like raw vendor payloads (`raw_response`, `response_body`,
   `cookies`, ...) are replaced with a fixed omission marker.
3. **Known values inside narrative text** -- callers pass credentials,
   tenant names/IDs, workspace IDs, and similar values through
   `known_sensitive_values`. Those exact bounded values are removed wherever
   they appear in reasons, errors, details, summaries, or other strings.

Key-based redaction cannot identify an arbitrary tenant name embedded in
free text without knowing that value. Live evaluators and compatibility
writers must therefore pass every known credential and target identifier via
`known_sensitive_values`; raw vendor response bodies remain prohibited.

`write_artifact(...)` applies this redaction by default (`redact=True`);
only pass `redact=False` for a payload that is already known-redacted, and
never to work around a validation failure. `LiveLifecycleEvidence` also
hard-rejects `secrets_included=True` and `raw_response_included=True` --
these fields exist so a caller cannot even attempt to persist raw
credentials or a raw vendor response body through this contract.

## Writing an artifact

```python
from hpe_networking_mcp.pipeline import artifact_contracts as contracts

entry = contracts.write_artifact(
    "outputs/aos8-live-evidence.json",
    contracts.LIVE_LIFECYCLE_EVIDENCE,
    {
        "platform": "aos8",
        "mode": "read_only",
        "generated_at": "2026-07-25T12:00:00+00:00",
        "steps": [{"name": "list_vlans", "status": "ok"}],
    },
    known_sensitive_values=[configured_tenant_id],
)
```

`write_artifact` validates the payload, redacts it, validates the redacted
shape again, serializes it
deterministically (sorted keys, so two logically-identical payloads always
produce the same SHA-256 digest), and writes it atomically (same-directory
temp file plus `os.replace`, mirroring
`src/hpe_networking_mcp/pipeline/aos8_migration_orchestrator.py`'s `MigrationRunStore`). It returns
a `ManifestEntry` (`filename`, `kind`, `schema_version`, `size_bytes`,
`sha256`, `generated_at`, `redacted`) ready to fold into a
`ReleaseArtifactManifest`.

## Live-test configuration

`src/hpe_networking_mcp/pipeline/live_test_config.py` is the generalized, reusable form of the
gating already hand-rolled in `scripts/evaluate_aos8_060_lab.py`. It governs
whether a *local evaluation harness* is allowed to make bounded live calls
against a real platform -- it is a separate safety domain from the
always-on MCP servers' write-tool gates (`HPE_MCP_CENTRAL_WRITES`,
`HPE_MCP_GLP_V2BETA1_WRITES`, `HPE_MCP_PRODUCT_ACCESS`, documented in
[Optional product starters](optional-products.md)).

| Rule | Behavior |
|---|---|
| Default | Every platform is fully disabled: no live calls of any kind. |
| Credential presence | Never implies authorization. `credentials_configured(platform)` reports presence only. |
| Read opt-in | `HPE_MCP_LIVE_TEST_<PLATFORM>_READ=1` enables bounded, read-only live calls. |
| Disposable-write opt-in | `HPE_MCP_LIVE_TEST_<PLATFORM>_WRITE=1` **and** the read opt-in together enable a disposable create/read-back/delete round trip against a lab-owned target. The write flag alone is never sufficient. |
| Status API | `live_test_status(platform)` returns env var names, booleans, and a "credentials configured" flag -- never a credential value. |

Supported platform keys (shared with `hpe_networking_mcp.mcp_servers.shared.PLATFORM_WRITE_GATE_NAMES`):
`central`, `glp`, `aos8`, `edgeconnect`, `apstra`, `mist`, `clearpass`,
`uxi`, `axis`.

| Platform | Read env var | Write env var | Credential env vars |
|---|---|---|---|
| Central | `HPE_MCP_LIVE_TEST_CENTRAL_READ` | `HPE_MCP_LIVE_TEST_CENTRAL_WRITE` | `SOURCE_BASE_URL`, `SOURCE_CLIENT_ID`, `SOURCE_CLIENT_SECRET` |
| GLP | `HPE_MCP_LIVE_TEST_GLP_READ` | `HPE_MCP_LIVE_TEST_GLP_WRITE` | `TARGET_BASE_URL`, `TARGET_CLIENT_ID`, `TARGET_CLIENT_SECRET` |
| AOS8 | `HPE_MCP_LIVE_TEST_AOS8_READ` | `HPE_MCP_LIVE_TEST_AOS8_WRITE` | `AOS8_BASE_URL`, `AOS8_USERNAME`, `AOS8_PASSWORD` |
| EdgeConnect | `HPE_MCP_LIVE_TEST_EDGECONNECT_READ` | `HPE_MCP_LIVE_TEST_EDGECONNECT_WRITE` | `EDGECONNECT_BASE_URL`, `EDGECONNECT_API_TOKEN` |
| Apstra | `HPE_MCP_LIVE_TEST_APSTRA_READ` | `HPE_MCP_LIVE_TEST_APSTRA_WRITE` | `APSTRA_BASE_URL`, `APSTRA_API_TOKEN` |
| Mist | `HPE_MCP_LIVE_TEST_MIST_READ` | `HPE_MCP_LIVE_TEST_MIST_WRITE` | `MIST_HOST`, `MIST_API_TOKEN` |
| ClearPass | `HPE_MCP_LIVE_TEST_CLEARPASS_READ` | `HPE_MCP_LIVE_TEST_CLEARPASS_WRITE` | `CLEARPASS_BASE_URL`, `CLEARPASS_API_TOKEN` |
| UXI | `HPE_MCP_LIVE_TEST_UXI_READ` | `HPE_MCP_LIVE_TEST_UXI_WRITE` | `UXI_CLIENT_ID`, `UXI_CLIENT_SECRET` |
| Axis | `HPE_MCP_LIVE_TEST_AXIS_READ` | `HPE_MCP_LIVE_TEST_AXIS_WRITE` | `AXIS_BASE_URL`, `AXIS_API_TOKEN` |

```python
from hpe_networking_mcp.pipeline import live_test_config as live_test

if live_test.live_test_read_enabled("aos8"):
    ...  # bounded, read-only live calls

if live_test.live_test_write_enabled("aos8"):
    ...  # disposable, lab-owned create/read-back/delete round trip

status = live_test.live_test_status("aos8")  # safe to log/print as-is
```

## Testing and linting these modules

```bash
uv run pytest tests/unit/test_artifact_contracts.py tests/unit/test_live_test_config.py
uv run ruff check src/hpe_networking_mcp/pipeline/artifact_contracts.py src/hpe_networking_mcp/pipeline/live_test_config.py
```

See [Release artifact automation](release-artifact-automation.md) for how
the validation-matrix runner, release-bundle packaging, and restore/
smoke-test tooling reuse these same schemas end to end.
