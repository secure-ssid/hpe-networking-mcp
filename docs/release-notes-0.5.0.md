---
title: "0.5.0"
nav_order: 5
parent: "Releases"
---

# hpe-networking-mcp 0.5.0 - verified ArubaOS 8 migration expansion

Version 0.5.0 focuses entirely on ArubaOS 8 (AOS8) migration correctness: a
hardened source foundation, a bounded and honest Classic Central write
lifecycle, an expanded (but still fail-closed) New Central write surface, a
new per-candidate verification taxonomy, and a read-only live/dry-run
evaluation of the whole hpe_networking_mcp.pipeline. Investigation effort was applied equally to
Classic Central and New Central; the result is **not** claimed parity between
them, and this release does not claim to have executed any live write against
either target. **Tool counts and router modes are unchanged from 0.4.0** —
no tool was added, removed, or renamed. Tool *behavior* did change on the
config-tool surface that AOS8 migration writes reuse: see
[Config-tool changes in this release](#config-tool-changes-in-this-release)
below for the full list and required upgrade steps.

![hpe-networking-mcp platform coverage](assets/platform-coverage.svg)

## Catalog snapshot (unchanged from 0.4.0)

| Catalog | Tools | Intended use |
|---|---:|---|
| Generated manifest operations | 5,703 | Reproducible platform API coverage across nine manifests |
| Active generated tools | 5,686 | Manifest operations that register as callable tools (17 intentionally excluded) |
| Curated tools | 476 | Hand-tuned, confirmed-working workflows |
| Complete backend index | 6,162 | Discovery/dispatch across every enabled backend (curated + active generated) |
| Direct-all router | 6,165 | Full schema introspection plus three router tools |
| Minimal router | 3 | Recommended low-token client surface (unchanged) |

Capability totals across the complete backend index remain **2,813 read**,
**164 diagnostic**, **2,382 write**, **803 destructive**. See the
[capability gap matrix](capability-gap-matrix.md) for the full,
reproducible, per-platform breakdown. `scripts/report_capability_gaps.py
--check` confirms the committed matrix is current against these counts.

## Config-tool changes in this release

No tool was added, removed, or renamed on the `central-config` server — the
following are **behavior/signature changes on existing tools**, made while
building the AOS8 migration write path (`src/hpe_networking_mcp/mcp_servers/config.py`):

- **New `wpa3_transition` parameter** on `build_underlay_ssid` and
  `build_overlay_ssid` (default `False`). It is **keyword-only**, added after
  every 0.4.0 parameter (including `dry_run`), so every 0.4.0 positional
  parameter — in particular `dry_run` — keeps its exact 0.4.0 positional
  index (commit `1f79256`). An old positional call that passed `True` for
  `dry_run` still binds `True` to `dry_run` and still only previews the
  payload; it can never execute a write. `wpa3_transition` is an explicit,
  never-inherited opt-in for a WPA3-transition-mode SSID; every currently
  supported pure opmode (`OPEN`, `WPA2_PERSONAL`, `WPA3_SAE`,
  `ENHANCED_OPEN`) continues to default to `False` and behaves exactly as
  before if the parameter is omitted. Existing callers that do not pass this
  argument see no change in behavior.
- **Stronger, fail-closed failure handling** on `create_role`, `update_role`,
  `delete_role`, `create_config_assignment`, `delete_config_assignment`, and
  `delete_overlay_ssid`. These tools previously returned a **2xx-shaped
  dict with the failure buried in an ad hoc `errors` list** on a non-2xx
  response — a caller that only checked "did this raise?" would treat a
  rejected write as applied, and the ad hoc
  `result.setdefault("errors", []).append(...)` pattern itself could raise
  `AttributeError` if the parsed response body was not a dict. All six tools
  now call the shared `hpe_networking_mcp.mcp_servers.shared.validate_write_result` helper on
  both the raw HTTP response and the parsed envelope, and **raise
  `WriteResultError`** (a `RuntimeError` subclass) on a non-2xx status, a
  non-empty `errors`/`error` field (list, string, or dict), an explicit
  `success`/`ok: False`, or a `failed`/`failure`/`error` `status` field. A
  legitimate empty/success 2xx body is never rejected. This is the same
  validation already used by the AOS8 migration write invoker
  (`src/hpe_networking_mcp/mcp_servers/aos8.py`); it is now applied consistently whether these
  tools are called directly, via the router, or via the AOS8 migration
  tools.
- No tool count changed as part of this hardening — only the six tools
  above changed their failure-reporting contract.

## Source foundation hardening

Before any target write behavior changed, the AOS8 source model and safety
plumbing were audited and fixed:

- **Normalized WLAN security intent** — AOS8 `opmode`/AAA-profile combinations
  are parsed into one explicit, normalized security intent per WLAN instead of
  leaving ambiguous raw fields for adapters to reinterpret.
- **Role-only AAA handling** — AAA profiles that reference only a role (no
  server group/auth-server dependency) are recognized and modeled distinctly
  from profiles with external dependencies, so role-only WLANs are not
  over-blocked by dependency checks that do not apply to them.
- **Type-aware auth-server dependencies** — RADIUS, LDAP, and TACACS
  auth-server references are tracked with their concrete type instead of a
  single untyped reference, so dependency resolution and secret-field
  requirements match the correct target schema per type.
- **Redaction fixes** — credential/secret fields are consistently redacted
  across exports, plans, and previews; a prior gap that could leak an
  unredacted secret in specific field-shape combinations is closed.
- **Source aliases** — additional AOS8 export field-name aliases are
  recognized so minor naming variants across firmware/export versions do not
  silently drop data.
- **Fail-closed warnings** — malformed, unsupported, or ambiguous source
  fields now produce an explicit warning and are preserved verbatim rather
  than silently dropped or guessed at.

These fixes are covered by the parser/migration regression suites in
`tests/unit/test_aos8_parsers.py` and `tests/unit/test_aos8_migration.py`.

## Classic Central: bounded, honest write lifecycle

Classic Central gets a complete, narrow, and explicitly bounded write
lifecycle built on the only verified Classic object REST in this repository —
`full_wlan`:

- **Full `full_wlan` GET/POST/complete-PUT lifecycle**, with a **mandatory
  read-back** after every write so the adapter never reports success on the
  strength of a write response alone.
- **Verified mappings**: **open** WLANs map exactly (`exact` classification)
  with no silent field loss. **WPA3-Personal** is also a verified, tested
  mapping (official-sample-evidenced `opmode=wpa3-sae-aes` with
  `opmode_transition_disable=true` and a transient, caller-supplied
  passphrase) but stays `conditional`, not `exact`, for the same reason as
  every New Central secured mode below: no target has a live-confirmed apply
  + secret read-back yet, only a fixture-backed unit-test round trip.
- **Conditional, dry-run-only WPA3-Enterprise**: only accepted when the
  candidate carries an **explicit reference to an existing auth-server
  object** (never auto-provisioned); even then, execution stays
  **dry-run-only** — a real (non-dry-run) apply is refused even with
  `confirmation=True`, because there is no verified live read-back path for
  this mode yet.
- **Precise manual/unsupported guidance** for every Classic object with no
  verified object REST (AP groups without an explicit device-group mapping,
  auth servers, routes, custom-ACL roles, server groups, dot1x profiles) —
  each rejection names the exact missing dependency or unmapped field instead
  of a generic failure.
- **Dedicated Classic target resolution**: Classic scope/group resolution is
  entirely independent of New Central scope resolution. A Classic group,
  GUID, or serial is never inferred from a New Central scope — the two are
  never treated as equivalent.
- **No automatic AP-group equivalence**: an AOS8 `ap_groups` profile is not
  auto-mapped to a Classic Central group or device group; an operator must
  supply an explicit target-group mapping and device serials before an AP
  group is considered anything but `unsupported`.

## New Central: expanded but still fail-closed

- **Secured WLAN preview mappings** now exist for **OPEN**, **WPA2_PERSONAL**,
  **WPA3_SAE**, and **ENHANCED_OPEN** — confirmed live at the
  `preview()`/preflight-read level in this release's evaluation (see below).
  These remain `conditional`, not `exact`: a full live `apply()` plus a
  real-secret read-back is still required before "exact" is justified.
- **Pure SAE transition mode stays disabled.** WPA2/WPA3 transition-personal
  candidates (`wpa3-transition-mode-enable`) are correctly blocked pending
  live confirmation of that field's behavior; this release does not enable
  it.
- **Role assignment is verified independently of the role object itself** —
  a role's config-assignment tuple is checked separately from its library
  object, since the two can disagree.
- **Auth-server / server-group / AAA / dot1x / macauth object contracts
  remain blocked** wherever the underlying config-assignment profile-type is
  not yet confirmed live (the SHARED-assignment caveat recorded in the
  [contract matrix](aos8-migration-contract-matrix.md)) — these candidates
  report `blocked`, not a false success.
- **Routes, VRRP, AP-group mapping, and custom policy remain fail-closed.**
  No adapter mapping exists for these families; candidates are rejected
  before any read or write is attempted, and this release adds none.

## Verification taxonomy

`aos8_verify_migration_run` reports one of six per-candidate statuses:
`verified`, `partially_verified`, `failed`, `unverifiable`, `unsupported`, or
`not_applied`. Verification is:

- **Bounded** — reads are paged and capped; verification never triggers an
  unbounded scan of target state.
- **Exact-path precedence** — when a curated tool's path/method diverges from
  the generated OpenAPI spec, the generated spec is authoritative for
  verification (see the config-assignment divergence recorded in the
  contract matrix).
- **Aware of Classic flat/nested support** — Classic Central's `full_wlan`
  payload shape is compared field-for-field against its own schema, not
  against the New Central nested shape.
- **Assignment-aware** — a role's config-assignment is verified as its own
  tuple, distinct from the role library object; secret fields are always
  reported `unverifiable` (never a false `mismatch`, since target reads never
  return secret values).

## Operator maps, external references, and secrets stay non-persistent

- **Operator-supplied maps and external object references used during a
  `preview()` call are stateless-preview-only and are never persisted.**
  Persistent migration runs (`aos8_create_migration_run`) reject them outright
  rather than silently dropping them.
- Any stale persisted run state referencing since-removed operator context is
  sanitized rather than surfaced as if still valid.
- Real secret values are **wholesale-redacted** in every backend output that
  touches a secret-context field — no partial masking, no field-shape-specific
  exception.
- **This release does not add rollback support.** New Central guidance remains
  limited to its documented post-change checkpoint policy and automatic
  device rollback; Classic Central guidance remains export-before-apply. No
  manual checkpoint listing, restore, or rollback workflow is claimed.

## Live/read-only dry-run evaluation

A read-only, no-write evaluation of the updated pipeline was completed
against this environment and is recorded in full in
[`docs/aos8-live-dryrun-evaluation.md`](aos8-live-dryrun-evaluation.md), gated
by the [AOS8 migration contract matrix](aos8-migration-contract-matrix.md):

- **New Central**: GET-only preflight/read-only evaluation completed live
  against the configured `central_account` tenant — scope resolution, role
  conflict checks, and per-family `preview()` calls for every in-scope
  candidate. Every HTTP call observed was `GET`; no `POST`/`PUT`/`PATCH`/
  `DELETE` was ever issued.
- **AOS8 source and Classic Central**: live access was **unavailable** in this
  environment (no `AOS8_BASE_URL`/`AOS8_USERNAME`/`AOS8_PASSWORD` and no
  explicit Classic group/GUID/serial configured anywhere). Both surfaces were
  exercised **fixture-backed only**, using the existing unit-test fixtures and
  in-memory fake backend — never inferring Classic scope from a New Central
  scope.
- **No writes were attempted anywhere in this evaluation.**
  `aos8_create_migration_run`/`aos8_apply_migration_run(dry_run=False, ...)`
  were never called against any target, and `confirmation=True` was never
  passed.
- One documentation-drift finding (stale "no adapter mapping exists" prose in
  the contract matrix for the now-implemented WPA2 Personal/WPA3-SAE/Enhanced
  Open New Central mappings) was found and corrected; no functional bugs were
  found, and no classification cell changed value.

## Validation

Run as the final release gate for this version:

- **1,587 unit tests passed** (`uv run pytest tests/unit -q`) — including the
  MCP protocol end-to-end suite (**10 passed**,
  `tests/unit/test_mcp_protocol_e2e.py`), the config-tool write-result
  validation regression suite (**26 passed**,
  `tests/unit/test_config_write_result_validation.py`), the AOS8
  read-only evaluation script's own tests (**6 passed**,
  `tests/unit/test_evaluate_aos8_050_readonly.py`), and this release's
  complete-branch review-fix regressions: `WPA2_PSK` deprecated-alias
  coverage in `tests/unit/test_ssid_underlay.py`/
  `tests/unit/test_run_ssid_cli.py`, and `aos8_preview_migration_run`/
  `aos8_create_migration_run` 0.4.0 positional-signature-compatibility
  coverage in `tests/unit/test_aos8_migration_orchestrator.py`.
- **20-sample RAG/API eval green** (`tests/eval/run_eval.py --ci`):
  `source_hit@k` 0.9, `keyword_hit` 1.0, `mrr` 0.9, `howto_recall@k` 0.9,
  `api_exact` 1.0.
- `scripts/check_generated_tool_manifests.py` — all nine manifests validated,
  5,703 generated operations confirmed.
- `scripts/report_capability_gaps.py --check` — capability gap matrix current.
- Non-mutating tool catalog count — **6,162** tools discovered with
  `products=all`, matching the documented complete backend index.

All of the above are run together by `scripts/validate_release.py`.

## Upgrade notes

1. No dependency changes in this release; `uv sync` is a no-op if your
   environment already matches 0.4.0.
2. No tool catalog changes. Rebuilding the router catalog
   (`uv run python scripts/ingest_tools.py --products all`) is optional but
   harmless; the tool count stays at 6,162.
3. If you use AOS8 migration tools, review the updated
   [AOS8 migration contract matrix](aos8-migration-contract-matrix.md) and
   [live/dry-run evaluation](aos8-live-dryrun-evaluation.md) before relying on
   any `conditional` mapping (WPA2 Personal, WPA3-SAE, Enhanced Open on New
   Central; WPA3-Personal and WPA3-Enterprise on Classic Central) — all five still require
   `dry_run=True` review and, where noted, remain dry-run-only.
4. If you plan a live AOS8 evaluation of your own environment, set
   `AOS8_BASE_URL` plus `AOS8_USERNAME`/`AOS8_PASSWORD` (or the legacy
   `AOS8_API_TOKEN`), and optionally `AOS8_CLIENT_IP` /
   `AOS8_SESSION_TTL_SECONDS`. See
   [optional-products.md](optional-products.md#arubaos-8-migration-prerequisites)
   for the full prerequisite list, including what a Classic Central evaluation
   additionally requires (an explicit group/GUID/serial — never inferred from
   a New Central scope).
5. No rollback capability was added in this release, and no live AOS8 or
   Classic Central write, nor exact secured-WLAN apply parity with New
   Central, is claimed. Treat every `conditional` mapping as preview-only
   until you have independently confirmed a live apply plus read-back in your
   own environment.
6. **If any caller (your own code, an automation, or an AOS8 migration run)
   depended on `create_role`, `update_role`, `delete_role`,
   `create_config_assignment`, `delete_config_assignment`, or
   `delete_overlay_ssid` returning a 2xx-shaped dict even when the
   underlying write was rejected**, that caller must be updated before
   upgrading: these six tools now raise `hpe_networking_mcp.mcp_servers.shared.WriteResultError`
   on a non-2xx response or an error-shaped envelope instead of returning a
   success-shaped result with the failure buried in an `errors` list. Wrap
   calls to these tools in a `try`/`except WriteResultError` (or the
   generic exception handling your caller already uses) instead of checking
   the returned dict for an `errors` key. Add or re-run targeted coverage —
   `tests/unit/test_config_write_result_validation.py` — against your own
   integration if you maintain a fork or wrapper around these tools. No
   change is needed for callers that already treat "no exception raised" as
   the only success signal *and* never inspected the previous ad hoc
   `errors` list themselves; only callers that inspected the old buried
   `errors` field to detect failure need to change error-detection logic to
   a `try`/`except` instead.
7. **`build_underlay_ssid`/`build_overlay_ssid` positional-signature
   compatibility**: `wpa3_transition` is keyword-only and was added after
   every 0.4.0 parameter, including `dry_run` — every 0.4.0 positional call
   site (including one that passed `True` positionally for `dry_run`) binds
   identically to 0.4.0 (commit `1f79256`) and never executes a write. No
   caller action is required; see
   [`tests/unit/test_ssid_dryrun_positional_compat.py`](../tests/unit/test_ssid_dryrun_positional_compat.py)
   for the reproducible positional-signature and write-guard coverage.
8. To reproduce this release's AOS8 read-only evaluation yourself, run
   `scripts/evaluate_aos8_050_readonly.py` (offline/fixture-backed by
   default; pass `--live-new-central-readonly` for a GET-only live New
   Central check). See
   [live/dry-run evaluation](aos8-live-dryrun-evaluation.md#reproduction)
   for exact commands — the prose evaluation findings recorded in that file
   were produced manually before this script existed; the script reproduces
   an equivalent read-only evaluation, not a replay of that exact session.

See the [0.4.0 release notes](release-notes-0.4.0.md) for the prior
resumable-migration-execution, typed GLP, and Mist/EdgeConnect/Axis history,
and the [0.3.0 release notes](release-notes-0.3.0.md) for earlier platform,
migration, and safety context.
