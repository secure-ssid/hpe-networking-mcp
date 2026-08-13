# Central v0.7 depth workflows (`v07-central-depth`)

Scope: Central-only. Adds schema-backed template/bulk/firmware-campaign/
config-health/troubleshooting-orchestration workflows on top of the existing
curated `central-config` / `central-monitoring` / `central-ops` tools, plus a
credential-gated live evaluator and a disposable-write lifecycle harness.
This page documents those additions only — it does not restate or revise
any global tool-count claims (see `README.md` / `docs/architecture/RAG-ARCHITECTURE.md`
for those, owned by the release process).

## Why these five workflows

Every endpoint below was verified against the committed OpenAPI source
(`ingestion/sources/openapi_specs/*.json`) and/or the generated Central
manifest (`src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/central.json`, 1,677 operations)
before any tool was written — no Classic-Central-shaped path was guessed.

| Workflow | New Central schema source | Gap this closes |
|---|---|---|
| VSF stacking-template lifecycle | `ingestion/sources/openapi_specs/vsf-template.json` (`GET/POST/PATCH/DELETE /network-config/v1alpha1/vsf-templates[/{name}]`) | The only schema-backed generic "template" resource in New Central; the existing `list_config_templates` only *probes* speculative Classic-shaped endpoints |
| Bulk site / site-collection delete | `scope-management-1fkx1wmq0s.json` (`DELETE /network-config/v1/sites/bulk`, `DELETE /network-config/v1/site-collections/bulk`) | Device-group bulk delete already existed; site/site-collection bulk delete did not |
| Firmware-compliance campaign | Orchestrates the existing manifest-confirmed `/network-config/v1alpha1/firmware-compliance` POST/PATCH flow across multiple targets | No bulk-firmware endpoint exists in the schema — this composes the single-target endpoint safely across scopes/personas instead of inventing one |
| Config-health remediation planning + execution | `configuration-health-b374ik1cmq.json` (`config-health/devices`, `config-health/active-issue`, `config-health/devices-resync`, `maxItems: 50`) | Planning was previously manual; resync had no schema-enforced chunk bound |
| Troubleshooting orchestration | Composes existing confirmed `/network-troubleshooting/*` LLDP/ARP/ping/show tools | No bounded, multi-step diagnostic bundle existed |

Endpoints considered and explicitly **not** implemented because no
authoritative schema exists in this manifest:

- Classic-style config templates with `%variable%` substitution or a
  separate template-group assignment step (`list_config_templates` now
  documents this precisely instead of guessing further).
- Bulk delete for `/network-config/v1alpha1/device-collections/bulk` —
  deprecated in the spec in favor of `device-groups/bulk` (already covered
  by the existing `delete_device_groups`).

## New tools

All writes below share one contract: `dry_run=True` by default (no network
call), an explicit `confirm=True` is required to execute, execution is
gated behind the existing Central write gate
(`HPE_MCP_CENTRAL_WRITES`, default enabled — `enforce_platform_write`
in `src/hpe_networking_mcp/mcp_servers/shared.py`), the executed response is validated
(`validate_write_result`, raises `WriteResultError` on a non-2xx/error
envelope), and a read-back call confirms what Central actually stored.

### `src/hpe_networking_mcp/mcp_servers/config.py` (75 → 80 tools)

| Tool | Annotation | Notes |
|---|---|---|
| `build_vsf_template` | `IDEMPOTENT_WRITE` | Create/update a LOCAL-scoped VSF template (`number_of_members` 1-10); read-back via GET after write |
| `delete_vsf_template` | `DESTRUCTIVE` | Delete + read-back; a 404 on read-back is surfaced as `read_back={"deleted_confirmed": True}` |
| `delete_sites_bulk` | `DESTRUCTIVE` | Up to 100 site IDs per call; irreversible (no read-back — verify with `list_sites` first) |
| `delete_site_collections_bulk` | `DESTRUCTIVE` | Same shape/bound as `delete_sites_bulk` |
| `run_firmware_compliance_campaign` | `IDEMPOTENT_WRITE` | Up to 25 `{scope_id, device_function}` targets; every target is attempted independently — one target's failure never aborts the rest (`targets_failed`, per-target `error`); each successful target is read back via `get_firmware_compliance` |

`get_network_profile` / `set_network_profile` / `delete_network_profile`
also work generically against `profile_type="vsf-template"` (added to
`_NETWORK_PROFILE_TYPES`), consistent with the existing BGP/OSPF/VRF/VSX/
telemetry profile family.

### `src/hpe_networking_mcp/mcp_servers/monitoring.py` (85 → 87 tools)

| Tool | Annotation | Notes |
|---|---|---|
| `plan_config_health_remediation` | `READ_ONLY` | Bounded scan (`max_devices_scanned` ≤ 200) of `list_devices_config_health` + `get_device_config_issues`; makes no config changes |
| `execute_config_health_remediation` | `IDEMPOTENT_WRITE` | Chunks ≤200 serials into groups of ≤50 (the `resyncCfgDevices` schema max) and resyncs each chunk independently — one chunk's failure never aborts the rest; reads back `get_device_config_issues` per serial in each successful chunk |

`resync_device_config` now enforces the same 50-serial schema bound
directly (`ValueError` above the limit) instead of silently forwarding an
oversized request.

### `src/hpe_networking_mcp/mcp_servers/ops.py` (40 → 41 tools)

| Tool | Annotation | Notes |
|---|---|---|
| `run_troubleshooting_bundle` | `DIAGNOSTIC` | CX or AOS-S only. Always runs an ARP-table step (CX also runs LLDP); optional ping (`destination`) and show (`commands`, ≤5, each must start with `show `) steps. Composes existing tools only — no new endpoint. Each step's failure is captured independently; the bundle never aborts early |

## Live evaluation: `scripts/evaluate_central_070_readonly.py`

Credential-gated per `src/hpe_networking_mcp/pipeline/live_test_config.py`; every artifact is
written through `src/hpe_networking_mcp/pipeline/artifact_contracts.write_artifact`
(`live_lifecycle_evidence` kind), which redacts secrets and hashes any
identifier before the file touches disk.

- **Default (offline, fixture-backed):** exercises a bounded 3-step
  sequence against an in-memory fake client — no network I/O.
- **`--live-read`:** swaps in the real `hpe_networking_mcp.mcp_servers.shared.get_client()`
  and runs the same bounded steps as GET-only calls. Requires
  `HPE_MCP_LIVE_TEST_CENTRAL_READ=1`; a GET-only guard is installed on
  the client so any non-GET verb raises before transmission. Blocked
  (exit code 1, no artifact written) when the env gate is unset.
- **`--live-write` / `run_disposable_write_lifecycle`:** a disposable
  create → read-back → delete VSF-template round trip
  (`build_vsf_template` / `delete_vsf_template`) against an operator-supplied
  lab scope. Gated by `HPE_MCP_LIVE_TEST_CENTRAL_WRITE=1` **and** the
  read gate (`live_test_config.live_test_write_enabled`). `main()` never
  calls this function in this repo revision — it only reports gate status
  when `--live-write` is passed — by design, so this task never executes a
  live write.

### Live status for this evaluation run

`HPE_MCP_LIVE_TEST_CENTRAL_READ` was **not set** in this environment, so
per the task's read-only-gating rule, no live call was attempted here —
only the default offline/fixture-backed evaluation was run
(`.venv/bin/python scripts/evaluate_central_070_readonly.py`). Central
credentials happen to be present in `config/credentials.yaml`, but
credential presence never implies authorization (see
`src/hpe_networking_mcp/pipeline/live_test_config.py`) — set
`HPE_MCP_LIVE_TEST_CENTRAL_READ=1` explicitly to run the bounded
GET-only path.

## Tests

| File | Covers |
|---|---|
| `tests/unit/test_config_v07_workflows.py` | VSF template dry_run/confirm/gate/read-back/validated-result; bulk-delete bounds + payload shape; firmware campaign bounds + partial failure |
| `tests/unit/test_monitoring_config_health_remediation.py` | Remediation planning (healthy-device skip, scan bound, per-device error capture); execution chunk bound, dry_run/confirm/gate, partial-chunk-failure |
| `tests/unit/test_ops_troubleshooting_bundle.py` | Device-type/command validation, CX vs AOS-S step composition, per-step partial-failure isolation |
| `tests/unit/test_evaluate_central_070_readonly.py` | Offline artifact validity + redaction (no raw fixture identifiers), live-read gating, disposable-write harness never auto-executed and hashes its identifier |

Run: `.venv/bin/python -m pytest tests/unit/test_config_v07_workflows.py tests/unit/test_monitoring_config_health_remediation.py tests/unit/test_ops_troubleshooting_bundle.py tests/unit/test_evaluate_central_070_readonly.py -q`
