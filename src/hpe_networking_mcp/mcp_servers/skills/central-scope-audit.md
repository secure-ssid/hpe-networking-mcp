---
name: central-scope-audit
title: Central configuration scope audit
description: |
  Read-only audit of Aruba Central scopes and high-value library objects:
  scope inventory, SSID/WLAN placement, roles, auth servers/groups, named
  VLANs, and config assignments. Flags likely hierarchy smells (site-local
  RADIUS, duplicate SSIDs, unassigned library objects). Not a full VSG
  tree walker — this catalog lacks committed/effective scope-tree APIs.
platforms: [central]
tags: [central, audit, configuration, scope, drift, wlan, nac]
tools:
  [
    find_tool,
    invoke_read_tool,
    list_scopes,
    find_scope,
    get_global_scope_id,
    list_ssids,
    list_wlans,
    list_roles,
    list_role_acls,
    list_auth_servers,
    list_server_groups,
    list_aaa_profiles,
    list_named_vlans,
    list_config_assignments,
    get_scope_maps,
    list_device_groups,
    ask_docs,
    lookup_api,
  ]
---

# Central configuration scope audit

## Objective

Produce a bounded configuration hygiene report for a Central estate (org-wide
or one site/collection). Focus on objects this MCP can actually list.

**Read-only.** No create/update/delete. Remediation is a separate confirmed
write session.

## Prerequisites

1. Resolve scope with `central-scope-walker` / `find_scope` / `get_global_scope_id`.
2. For large tenants, prefer one site or device group over org-wide dumps.
3. Keep `limit` ≤ 100 on list tools unless the operator expands the window.

## Procedure

### Step 0 — Scope inventory

- `list_scopes` or site/group lists for the chosen boundary.
- Record counts: sites, groups, other scope types if labeled.
- Note gaps (unnamed scopes, duplicate names).

### Step 1 — Wireless library + placement

- `list_ssids` / `list_wlans` (and `get_scope_maps` when useful).
- Flag: duplicate SSID names, disabled production-looking SSIDs, open/enhanced-open
  on non-guest naming, missing expected SSID the operator named.

### Step 2 — Roles and simple policy linkage

- `list_roles`; sample `list_role_acls` only for roles referenced by SSIDs or
  named by the operator.
- Flag roles with no ACL/policy signal when the tool returns empty bindings.

### Step 3 — Auth path

- `list_auth_servers`, `list_server_groups`, `list_aaa_profiles` (NAC server).
- Flag single-server production groups, empty groups, and profiles that
  reference missing servers when detectable.
- Best-practice note (guidance, not automatic enforcement): campus RADIUS
  sources are usually library/global rather than one-off per small site.
  If assignment tools show site-only auth objects, mark as **review**.

### Step 4 — Named VLANs

- `list_named_vlans` for the scope when available.
- Flag unnamed critical IDs only if the operator cares about specific VLANs;
  do not spam every VLAN in a large fabric.

### Step 5 — Config assignments sample

- `list_config_assignments` bounded to the scope or profile types under review.
- Flag library objects with zero assignments when the API exposes that, and
  assignments pointing at missing objects when errors say so.

### Step 6 — Docs assist (optional)

For unclear enums or hierarchy guidance: `lookup_api` then `ask_docs`
(source hints: `techdocs_html`, `vsg_docs`). Cite `file_path`.

## Severity labels

| Label | Meaning |
|---|---|
| REGRESSION | Likely breaks auth/forwarding or contradicts explicit operator standard |
| DRIFT | Inconsistent or brittle placement; schedule cleanup |
| INFO | Hygiene / documentation only |

## Output format

1. Scope header (boundary, counts)
2. Category summary table: area | objects seen | issues
3. REGRESSION list first, then DRIFT, then INFO
4. Safe next reads (no writes)
5. Explicit **not covered** list (see gaps)

## Honest gaps (deferred capabilities)

This repository does **not** currently expose:

- committed vs effective scope-tree APIs
- full alias / object-group / net-service walkers
- automated VSG per-setting compliance scoring

Do not claim a full Validated Solution Guide pass. Say "bounded MCP audit".
