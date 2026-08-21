---
name: central-scope-walker
title: Resolve Central scope names to IDs
description: |
  Resolve a human scope reference (site, group, global, partial name) to
  Central scope_id / scope metadata before config or monitoring calls. Use
  when the operator says a site or group name and downstream tools need an ID.
  Read-only utility primitive.
platforms: [central]
tags: [central, scope, utility, primitive]
tools: [find_tool, invoke_read_tool, list_scopes, find_scope, get_global_scope_id, list_sites, list_device_groups]
---

# Resolve Central scope names to IDs

## Objective

Turn GUI language into a concrete Central scope reference the rest of the
session can reuse.

**Read-only.**

## Prerequisites

- Operator provides a name, path-like hint, or wants org-wide/global.
- Use router discovery if tool names differ in the active catalog.

## Procedure

### Step 1 — Org-wide / global

If the operator says everywhere, org-wide, all APs, or global:

**Tool:** `get_global_scope_id`  
**Return:** global scope id and stop unless they also want children listed.

### Step 2 — Named scope

1. Prefer `find_scope` with the operator string when available.
2. Else `list_scopes` (use paging/`full_list` carefully) and match
   `scope_name` case-insensitively.
3. Optional assists: `list_sites` or `list_device_groups` when the persona is
   clearly a site or device group.

### Step 3 — Match policy

Priority:

1. Exact scope_id match
2. Exact scope_name (case-insensitive)
3. Unique substring match on name
4. Multiple matches → return a short candidate table; do not guess

Capture when present: `scope_id`, `scope_name`, type/kind, parent if exposed,
device counts.

### Step 4 — Persona reminder

After resolving WHERE, record DEVICE TYPE if the next step is config:

| Operator language | Persona |
|---|---|
| APs / wireless / campus AP | `CAMPUS_AP` |
| Gateways / GW | `MOBILITY_GW` |
| Access / agg / core switch | matching switch persona |

Default wireless work to `CAMPUS_AP` only when the task is clearly WLAN.

## Output format

```text
query: <raw>
match: exact_name | substring | global | ambiguous | none
scope_id: ...
scope_name: ...
type: ...
notes: ...
```

If ambiguous, list ≤10 candidates and ask which one.

## When not to use

- Pure live client MAC lookup → `client-connectivity`
- GLP workspace/device inventory → `greenlake-device-onboarding`
