---
name: clearpass-policy-audit
title: ClearPass policy and service audit
description: |
  Read-only ClearPass review of services, enforcement policies, roles,
  recent auth failures, and cluster/version health. Use for "how is guest
  auth set up", "show enforcement policies", or NAC policy hygiene.
  Does not compile Mermaid decision graphs — that compiler is not in this
  catalog.
platforms: [clearpass, nac]
tags: [clearpass, policy, nac, audit, enforcement, services]
tools:
  [
    find_tool,
    invoke_read_tool,
    clearpass_status,
    clearpass_list_services,
    clearpass_get_service,
    clearpass_list_enforcement_policies,
    clearpass_get_enforcement_policy,
    clearpass_list_roles,
    clearpass_list_auth_failures,
    clearpass_list_access_tracker_sessions,
    clearpass_list_endpoints,
    clearpass_get_server_version,
    clearpass_list_cluster_servers,
    list_auth_servers,
    list_aaa_profiles,
  ]
---

# ClearPass policy and service audit

## Objective

Summarize ClearPass policy surface area the operator can act on: which
services exist, which enforcement policies they use, role inventory, and
whether auth is currently failing.

**Read-only.** No guest create, disconnect, attribute patch, or service
enable/disable unless the operator starts a separate gated write flow.

## Prerequisites

- `HPE_MCP_PRODUCTS` includes `clearpass` and credentials are set
  (`CLEARPASS_BASE_URL`, `CLEARPASS_API_TOKEN`).
- First call `clearpass_status` / `find_tool("clearpass")`. If absent, stop:
  backend not enabled.

## Procedure

### Step 1 — Platform health

- `clearpass_get_server_version`
- `clearpass_list_cluster_servers`
- Note node count and any obvious down/split signals in the payload.

### Step 2 — Service catalog

- `clearpass_list_services` (bounded).
- Prefer operator-facing **names**; avoid dumping internal numeric IDs in the
  summary unless required for a follow-up API call.
- If the operator named a service, `clearpass_get_service` for that row only.

### Step 3 — Enforcement policies + roles

- `clearpass_list_enforcement_policies` then `clearpass_get_enforcement_policy`
  for the policies tied to services in scope (cap detail pulls, e.g. ≤5).
- `clearpass_list_roles` for the role vocabulary.
- Summarize: service → policy → profiles/roles when fields exist.

### Step 4 — Live auth pain

- `clearpass_list_auth_failures` (small limit).
- Optional: `clearpass_list_access_tracker_sessions` filtered if the operator
  gave a username/MAC.
- Group by failure reason / NAD when present.

### Step 5 — Optional Central NAC cross-check

If Central NAC tools are enabled, compare name-level auth server / AAA
profile inventory (`list_auth_servers`, `list_aaa_profiles`) for obvious
mismatches. Name match only — do not claim full policy equivalence.

## Output format

1. ClearPass reachability + version/cluster one-liner
2. Services table: name | enabled | type/hints | enforcement policy
3. Top failure reasons (if any)
4. Role/policy notes
5. Recommended read-only follow-ups

## Gated writes (out of band)

Only if the operator **explicitly** requests a change afterward:

- Preview with product write gates + `dry_run`/`confirm` patterns on tools such
  as `clearpass_set_service_enabled`, `clearpass_create_guest`,
  `clearpass_update_endpoint_attributes`, `clearpass_disconnect_session`.
- Never run those inside this audit skill.

## Honest gaps

Unavailable here (defer):

- `clearpass_compile_policy_flow` / automatic Mermaid decision graphs
- role-mapping policy deep compiler
- what-if simulation engine

If the operator asks to "draw the policy flow", explain the gap and offer the
service/enforcement tables above plus `ask_docs` with `source=clearpass_guide`.
