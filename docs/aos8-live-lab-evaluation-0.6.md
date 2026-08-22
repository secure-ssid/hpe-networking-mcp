---
title: "AOS8 0.6 live-lab evaluation"
nav_order: 1
parent: "Archive"
---

# AOS8 0.6 live-lab evaluation

**Status:** in progress

**Branch:** `feat/centralmcp-0.6.0` (historical branch name in the legacy `secure-ssid/centralmcp` repository, preserved verbatim as provenance; the current project is `hpe-networking-mcp`)

**Safety mode:** read-only target evidence; no configuration write attempted

This document records live evidence gathered for the 0.6 migration work. It is
separate from the completed
[0.5 read-only evaluation](aos8-live-dryrun-evaluation.md) because the 0.6
harness permits OAuth/session authentication while independently blocking
non-read data-plane requests.

## Reproduction

```bash
# Offline baseline
uv run python scripts/evaluate_aos8_060_lab.py --offline

# Live AOS8 evidence when source credentials are configured
uv run python scripts/evaluate_aos8_060_lab.py \
  --live-aos8-readonly \
  --config-path /md \
  --limit 100 \
  --max-items-per-type 1000

# Live Classic or New Central evidence with an explicit target
uv run python scripts/evaluate_aos8_060_lab.py \
  --live-central-readonly \
  --target-type new_central \
  --scope-name "<explicit scope>" \
  --persona CAMPUS_AP \
  --candidates inputs/aos8-lab-candidates.json
```

The complete controlled-write workflow is documented in
[optional-products.md](optional-products.md#aos8-06-live-lab-evidence-harness).
No controlled write has been executed for this evaluation.

## Current access

| Surface | Availability | Current evidence |
|---|---|---|
| New Central | Available | OAuth token bootstrap succeeded, then the data-plane guard observed only `GET` requests. |
| AOS8 source | Blocked | `AOS8_BASE_URL`, `AOS8_USERNAME`, `AOS8_PASSWORD`, and legacy `AOS8_API_TOKEN` are not configured. |
| Classic Central | Blocked | No explicit Classic group, GUID, or device serial is configured; New Central scope data is never reused. |

## New Central live result

The 0.6 harness refreshed the OAuth token through the HPE authentication
endpoint, installed the Central data-plane request guard, and evaluated the
representative AP-persona candidate set against an explicit `Global` target.

- Coverage: `live_get_only`
- Data-plane HTTP methods observed: `GET`
- Representative result rows: 12
- Live blockers: none
- Target identifiers persisted in evidence: no
- Target secrets supplied or persisted: no
- Migration run created: no
- Configuration write attempted: no

The bounded reads covered global scope resolution, role preflight, VLAN
preflight, and WLAN preflight. This confirms the harness can authenticate
without weakening the post-authentication GET-only contract. It does not
promote any conditional mapping to exact because no create/read-back/delete
lifecycle was executed.

A separate bounded `GET /network-config/v1alpha1/config-assignments` observed
41 distinct `profile-type` values. The returned values included all five
blocked AOS8 dependency families:

- `auth-servers`
- `server-groups`
- `aaa-profile`
- `dot1xauth`
- `macauth`

This confirms the literal values and supports exact client-side assignment
tuple verification. It does not authorize assignment writes: collection POST,
tuple read-back, instance DELETE, and object cleanup still require a
disposable lab round trip.

A second bounded GET used the extra `profile-type=roles` query parameter. The
service returned 28 assignment tuples and every returned `profile-type` was
`roles`, confirming that the live service honors the filter even though the
committed GET schema does not declare it. Verification continues to inspect
the full returned tuple client-side.

## Contract findings incorporated

- `get_network_profile(profile_type="static-route")` now exposes
  `/network-config/v1alpha1/static-route` for bounded evidence reads.
- `get_network_profile(profile_type="vrrp-interface")` now exposes
  `/network-config/v1alpha1/vrrp` for bounded evidence reads.
- Generic set/delete remains blocked for both types because the Gateway IPv4
  destination contract, `/v1` versus `/v1alpha1` static-route divergence,
  VRRP VLAN/interface attachment, and tracking normalization are unresolved.
- Live Gateway policy reads confirmed the nested
  `policy/security-policy/policy-rule[]` shape, including rule position,
  `CONDITION_DEFAULT`, `RULE_ANY`, `ADDRESS_ANY` source/destination, and
  `ACTION_ALLOW`/`ACTION_DENY`.
- The New Central adapter now generates a dry-run-only Gateway policy preview
  for the exact conservative subset of ordered IPv4 `any`-to-`any` rules,
  `any` service, permit/allow or deny action, and absent/disabled logging.
  Named services, aliases, IPv6, logging, and non-Gateway personas fail closed.
  Real policy apply remains blocked until a disposable create/read-back/delete
  lifecycle succeeds.
- Live device-group reads returned four bounded group records with scope,
  group type, description, and device-count metadata. This is inventory
  evidence only; AP-group mapping and device moves remain unimplemented.
- Migration-relevant `create_auth_server`, `delete_auth_server`,
  `create_aaa_profile`, and `delete_aaa_profile` writes now validate both the
  raw HTTP status and parsed response envelope. Non-2xx responses and
  success-shaped 2xx bodies carrying explicit failure markers raise instead
  of being eligible for a later success classification.

Live bounded reads against the `MOBILITY_GW` device function also confirmed:

- `GET /network-config/v1alpha1/static-route` succeeds. The one returned
  profile exposed `name` plus `default-gateway[]` entries containing
  `dg-name`, `forwarding-type`, `ipv4-address`, and `metric`; it did not expose
  a general destination/prefix route shape. This is useful evidence for
  default routes only and does not justify a general AOS8 route mapper.
- `GET /network-config/v1alpha1/vrrp` succeeds and returned an empty profile
  collection in this scope. The endpoint is available, but there is no live
  VLAN/interface/virtual-router shape to map yet.

## Remaining live gates

1. Configure AOS8 source credentials and collect a bounded sanitized export.
2. Provide an explicit Classic group/GUID/serial and run Classic GET-only WLAN
   preflight evidence.
3. Generate and review a controlled-write plan for a disposable SHARED
   profile and assignment.
4. Execute create/assign/read-back/unassign/delete only after the unchanged
   plan digest and exact target are confirmed.
5. Execute the dry-run-reviewed Gateway policy create/read-back/delete
   lifecycle for a lab-owned `centralmcp-lab-*` policy (historical prefix from that run; current builds guard on `hpe-mcp-lab-*`).
6. Capture a non-empty Gateway interface-VRRP read and a non-default static
   route before implementing route or VRRP writes.

Until those gates are completed, affected mappings remain conditional,
manual, or unsupported exactly as recorded in the
[contract matrix](aos8-migration-contract-matrix.md).
