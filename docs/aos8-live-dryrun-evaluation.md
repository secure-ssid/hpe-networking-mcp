---
title: "AOS8 live/dry-run evaluation"
nav_order: 12
parent: "Reference"
---

# AOS8 0.5 live/read-only dry-run evaluation

**Date:** 2026-07-25 (UTC)
**Branch/commit:** `feat/centralmcp-0.5.0` @ `86ef730` (historical branch name in the legacy `secure-ssid/centralmcp` repository, preserved verbatim as provenance; the current project is `hpe-networking-mcp`)
**Todo:** `aos8-live-dryrun-eval`
**Write mode:** No writes attempted. Every call in this evaluation was `GET`
or a stateless `preview()` call; `confirmation=True` was never passed and no
`aos8_create_migration_run`/`aos8_apply_migration_run(dry_run=False, ...)`
call was made against any target.

## Reproduction

- Offline / fixture-backed reproduction: `.venv/bin/python scripts/evaluate_aos8_050_readonly.py`
- New-Central-only live GET-only reproduction: `.venv/bin/python scripts/evaluate_aos8_050_readonly.py --live-new-central-readonly`

The original evidence recorded in the rest of this document was produced manually, before `scripts/evaluate_aos8_050_readonly.py` existed. The committed script is a reproducible harness for re-running an equivalent read-only evaluation going forward; it is not a replay or committed transcript of that exact original manual session.

## 1. Access availability

| Surface | Configured in this environment | Evidence |
|---|---|---|
| AOS8 (Mobility Conductor / controller) | **No** | `AOS8_BASE_URL`, `AOS8_USERNAME`, `AOS8_PASSWORD`, `AOS8_API_TOKEN`, `AOS8_CLIENT_IP` all unset in the shell environment; no `.env` file present; `config/credentials.yaml` has no AOS8 section (only `central_account`, `glp_account`, `glp` — see `config/credentials.yaml.example`, which documents no AOS8 keys either). |
| Central / New Central (`central_account`) | **Yes** | `config/credentials.yaml` `central_account.*` keys are present and non-placeholder. Live read-only calls succeeded (see §3). |
| GLP (`glp_account`/`glp`) | Yes (present in config), **not exercised** — out of scope for this AOS8-focused evaluation. |
| Classic Central group/GUID/serial | **No** — no explicit Classic group/GUID/serial is configured anywhere in this environment (checked `config/credentials.yaml`, env, `.env*`). Per the task's non-negotiable instruction, Classic scope is **never** inferred from New Central scopes, so no live Classic call was attempted. |

**Access availability: `false` for AOS8.** This blocks any live AOS8 export/login and any live Classic Central preview. Central/New Central read-only access is available and was used for the New Central portion of this evaluation.

## 2. Coverage classification — live vs. fixture-backed

| Area | Coverage | Basis |
|---|---|---|
| AOS8 source export/login/parsing | **Fixture-backed only** (not live) | No AOS8 credentials configured anywhere in this environment. `src/hpe_networking_mcp/pipeline/aos8_parsers.py`/`src/hpe_networking_mcp/pipeline/aos8_migration.py` are already exercised by 373 passing unit tests (`tests/unit/test_aos8_parsers.py`, `test_aos8_migration.py`, `test_aos8_session.py`) against synthetic AOS8-shaped export fixtures. No same-owner companion-repo fixture directory exists locally in this checkout (`tests/fixtures/` contains only `edgeconnect_compatibility`); the existing in-repo unit-test fixtures were used instead, per the task's fallback instruction. |
| New Central target-context resolution | **Live** | `list_scopes()`/`get_global_scope_id()` (`src/hpe_networking_mcp/mcp_servers/monitoring.py`) executed against the configured `central_account` tenant — both `GET`-only. |
| New Central adapter preview (dependency-ordered, stateless) | **Live** for scope/persona resolution + live preflight `GET`s; candidate objects are synthetic/representative (since no live AOS8 export exists) | `hpe_networking_mcp.mcp_servers.aos8.aos8_preview_migration_run(target_type="new_central", ...)` invoked directly (not through an MCP client) with representative candidates for every family in scope (see §4). |
| Classic Central adapter preview | **Fixture-backed only** (not live) | No Classic group/GUID/serial configured. Exercised `hpe_networking_mcp.pipeline.aos8_target_adapters.ClassicCentralAdapter` directly with the test suite's `FakeBackend` (in-memory, no network) — the same pattern `tests/unit/test_aos8_target_adapters.py` uses. |
| Verification (`aos8_verify_migration_run`) | **Not exercised** | Requires a persisted run (`aos8_create_migration_run`), which this evaluation deliberately avoided to keep the New Central tenant free of any persisted run state tied to synthetic candidates. `preview()`'s own preflight-read step already exercises the same live `GET` code path (`_aos8_migration_read_invoker`) that `verify()` would use. |

## 3. Read-only commands/tools used

All calls below were made by importing the router's underlying Python functions directly (not through a running MCP client), to keep the evaluation bounded and inspectable. Every call is read-only (`GET`) or a stateless `preview()`.

```python
from hpe_networking_mcp.mcp_servers.monitoring import get_global_scope_id, list_scopes
get_global_scope_id()                 # GET /network-config/v1/global -- org-wide scope id
list_scopes(full_list=True)           # GET /network-config/v1/{global,sites,device-groups}

from hpe_networking_mcp.mcp_servers.aos8 import aos8_preview_migration_run
aos8_preview_migration_run(
    target_type="new_central",
    candidates=[...],                 # synthetic, representative candidates (see §4)
    scope_name=<resolved live SITE scope name>,
    persona="CAMPUS_AP" | "MOBILITY_GW",
    conflict_policy="fail",
)
```

`aos8_preview_migration_run` is stateless (`AOS8MigrationOrchestrator.preview()`, `src/hpe_networking_mcp/pipeline/aos8_migration_orchestrator.py`): it never persists a run, and it forces every secret placeholder itself (`_placeholder_secret_inputs`) — no real secret value was ever in scope for any preview call. For candidates the adapter classifies `ready`, `preview()` still performs one real preflight `GET` against the configured Central tenant (per-candidate existence check only — e.g. `GET /network-config/v1/layer2-vlan/{id}`, `GET /network-config/v1/wlan-ssids/{name}`, `GET /network-config/v1alpha1/roles`); it never issues a `POST`/`PUT`/`PATCH`/`DELETE`. Observed HTTP verbs across the entire evaluation: **`GET` only** (confirmed via httpx debug logging during the run; no other verb appeared).

Real identifiers are never recorded here. The live-resolved site scope name is represented only as a truncated SHA-256 hash (`7966bcb94b9c...`) for run-to-run consistency checking, never the plaintext value.

For the Classic Central portion:

```python
from hpe_networking_mcp.pipeline.aos8_target_adapters import ClassicCentralAdapter, TargetContext, TargetType
# same FakeBackend/classic_adapter() pattern as tests/unit/test_aos8_target_adapters.py
# -- purely in-process, no network call of any kind.
```

## 4. Families/modes exercised

### New Central (live scope resolution + live preflight reads), persona `CAMPUS_AP`

| Candidate | Result | Notes |
|---|---|---|
| `vlan` | `ready` (conflict: absent) | Live `GET /network-config/v1/layer2-vlan/{id}` confirmed no existing object; preview stopped before any write. |
| `role` allow-all | `ready` (conflict: absent) | Live `GET /network-config/v1alpha1/roles` confirmed no name collision. |
| `role` custom ACL | `unsupported` | Matches contract matrix — only `allowall`/`sys_allow_all` is a verified mapping. |
| `wlan` open | `ready` (conflict: absent) | Live `GET /network-config/v1/wlan-ssids/{name}`; `opmode` argument correctly rendered `OPEN`. |
| `wlan` WPA2 Personal | `ready` (conflict: absent) | **Finding** — see §6. Placeholder secret correctly injected and masked (`passphrase: "******"`); no real/placeholder secret string ever appeared in the returned preview. |
| `wlan` WPA3 SAE | `ready` (conflict: absent) | Same as above; `opmode` rendered `WPA3_SAE`. |
| `wlan` Enhanced Open | `ready` (conflict: absent) | `opmode` rendered `ENHANCED_OPEN`; no secret required, matching the OWE contract (no PSK). |
| `wlan` WPA2/WPA3 transition-personal | `blocked` | Correctly blocked — `wpa3-transition-mode-enable` remains unvalidated live, exactly per the contract matrix. |
| `wlan` MAC-auth only | `unsupported` | Matches contract matrix — AAA-profile-attached WLANs remain fail-closed. |
| `wlan` Enterprise 802.1X | `unsupported` | Matches contract matrix. |
| `ap_group` | `unsupported`, **no live read invoked** | No adapter mapper exists; confirms the adapter fails closed before ever issuing a read for object types it cannot map. |
| `policy` | `unsupported`, **no live read invoked** | Same as above. |

### New Central (live scope resolution), persona `MOBILITY_GW`

| Candidate | Result | Notes |
|---|---|---|
| `auth_server` RADIUS | `blocked` | Object contract verified (per adapter), but blocked pending the SHARED config-assignment profile-type confirmation noted in §2.1/§5 of the contract matrix — **no live read was invoked for this candidate in this evaluation** (blocked before preflight), so this run did not additionally confirm the `auth-servers` preflight `GET` shape live. |
| `auth_server` LDAP | `unsupported` (missing secret) | Requires `admin_password`; candidate here deliberately omitted `requires_secret_input`/`secret_fields` to observe the fail-closed message, which was correct and specific. |
| `auth_server` TACACS | `unsupported` (missing secret) | Same pattern; correct fail-closed message naming `shared_secret`. |
| `auth_server` RadSec | `unsupported` | Explicitly rejected — "New Central auth-server mapping only supports RADIUS, LDAP, and TACACS today." Matches contract matrix `conditional`→still-blocked status. |
| `server_group` | `blocked` | Same SHARED-assignment caveat as `auth_server`/`aaa_profile`/`dot1x`/`macauth` below. |
| `aaa_profile` simple | `blocked` | Object contract verified; blocked on the same SHARED config-assignment caveat. |
| `aaa_profile` rich (dot1x/server-group refs) | `unsupported` | Correctly rejects `['dot1x_auth_profile', 'dot1x_server_group']` as unpreservable fields — matches contract matrix §6.3. |
| `dot1x_auth_profile` bare | `blocked` | Same SHARED-assignment caveat. |
| `dot1x_auth_profile` rich (extra settings) | `unsupported` | Correctly rejects `['reauthentication', 'use_session_key']` as not a verified 1:1 mapping. |
| `mac_auth_profile` bare | `blocked` | Same SHARED-assignment caveat. |
| `route` (IPv4) | `unsupported`, no read invoked | No adapter mapper exists — matches contract matrix. |
| `vrrp` | `unsupported`, no read invoked | No adapter mapper exists — matches contract matrix. |

### Classic Central (fixture-backed only — `FakeBackend`, no network)

| Candidate | Result | Notes |
|---|---|---|
| `wlan` open bridged | `ready` | `opmode: "opensystem"`, `wpa_passphrase: ""` — matches the one verified Classic `exact` row. |
| `wlan` WPA3 Personal, missing secret | `unsupported` | Correct fail-closed message naming `wpa_passphrase`. |
| `wlan` WPA3 Personal, placeholder secret supplied | `ready` | `opmode_transition_disable: true`, `wpa_passphrase: "***"` masked; no secret value leaked into the preview. |
| `wlan` WPA3-Enterprise, no explicit auth-server reference | `unsupported` | Correct fail-closed message — dependency "never auto-provisioned". |
| `wlan` WPA3-Enterprise, explicit `external_object_references` supplied | `ready`, `dry_run_only: true` | Matches contract matrix — conditional/dry-run-only; a real (non-dry-run) `execute()` is refused even with `confirmation=True` (verified by the existing unit test, not re-exercised destructively here). |
| `ap_group`, no target-group mapping | `unsupported` | Correct — "AOS8 AP groups are not Classic Central groups... supply an explicit operator-provided mapping". |
| `ap_group`, group mapped but no device serials | `unsupported` | Correct — still `manual` until device serials are supplied. |
| `auth_server` LDAP, `route`, custom-ACL `role`, `server_group`, `dot1x_auth_profile` | all `unsupported` | Matches contract matrix — "No verified Classic Central object REST exists" for each. |

Secret-leak check: every synthetic-secret string used above (`placeholder-only`, `actual-secret`-style literals used only in the earlier unit-test corroboration, not re-typed into live calls) was asserted absent from the JSON-serialized preview output before being discarded; only masked (`***`/`******`) values ever appeared.

## 5. Read-only preflight/verification outcomes

Every `ready`-classified candidate above that triggered a live preflight `GET` reported `conflict: "absent"` — i.e. the synthetic placeholder object names used (`centralmcp-eval-*` — the literal prefix used by that historical run; current builds use `hpe-mcp-lab-*`) do not exist in the configured tenant. Per the task's explicit instruction, the evaluation **stopped at this point and did not proceed to create** anything: no `aos8_create_migration_run`, no `aos8_apply_migration_run`, no `confirmation=True` anywhere.

`state/aos8_migrations/` was inspected before and after this evaluation and remains empty — `preview()` is confirmed stateless in practice, not just by code inspection. `git status --porcelain` shows no repository changes from any of the calls in this evaluation.

## 6. Findings

1. **Documentation drift (not a code bug).** `docs/aos8-migration-contract-matrix.md` §3 item 2 and §6.2's intro state "No adapter/target mapping exists yet for any mode this enriches" for WPA2 Personal / WPA3-SAE / Enhanced Open. This is now stale: `src/hpe_networking_mcp/pipeline/aos8_target_adapters.py` (commits after `519ace4`, up to `86ef730`) implements a mapping for all three modes on New Central, and this evaluation confirmed it live at the `preview()`/preflight-read level (§4). Classification should **remain `conditional`**, not move to `exact` — a full live `apply()` + read-back with a real (non-placeholder) secret is still required before "exact" is justified, and that is out of scope for a no-write evaluation. A minimal, bounded correction was applied to the contract matrix (see below) to remove the factually incorrect "no adapter mapping" claim without changing any classification.
2. **No functional bugs found.** All 373 existing AOS8 unit tests pass unchanged; every family/mode exercised in this evaluation (live and fixture-backed) matched its documented classification in the contract matrix (aside from finding #1's stale prose).
3. **No secret/operator-context leakage observed.** Every preview response was JSON-serialized and scanned for the literal secret/placeholder strings used in this evaluation; none were found unmasked. `secrets_persisted`/`operator_context_persisted` were `false` in every New Central preview response, as documented.

## 7. Blockers

- **AOS8 live access blocker:** no `AOS8_BASE_URL`/`AOS8_USERNAME`/`AOS8_PASSWORD` (or legacy `AOS8_API_TOKEN`) configured in this environment, and no same-owner companion-repo fixture directory present locally. AOS8 source-side coverage in this evaluation is fixture-backed only (existing `tests/unit/test_aos8_*` fixtures), not live.
- **Classic Central blocker:** no explicit Classic group/GUID/serial configured anywhere in this environment. Per the non-negotiable instruction, this was never inferred from the New Central scope that *is* configured. Classic coverage in this evaluation is fixture-backed only (`FakeBackend`), not live.
- Neither blocker prevents completing the feasible portion of this evaluation (New Central live preview/preflight-read + fixture-backed AOS8 parsing/Classic preview), so `aos8-live-dryrun-eval` is being closed as **done**, with the above two access gaps recorded precisely rather than treated as a hard block.

## 8. Contract matrix corrections applied

`docs/aos8-migration-contract-matrix.md`:
- §3 item 2 and §6.2 intro: corrected the stale "no adapter/target mapping exists yet" claim for WPA2 Personal/WPA3-SAE/Enhanced Open to reflect that a New Central adapter mapping now exists and was confirmed live at the preview/preflight-read level by this evaluation (2026-07-25); classification remains `conditional` pending a live apply + secret read-back, which is out of scope for a read-only evaluation.
- §9: added a reference to this file.

No classification cells changed value; only the stale prose describing why New Central WPA2 Personal/WPA3-SAE/Enhanced Open are `conditional` (rather than `manual`/`unsupported`, which was never their listed classification) was corrected.
