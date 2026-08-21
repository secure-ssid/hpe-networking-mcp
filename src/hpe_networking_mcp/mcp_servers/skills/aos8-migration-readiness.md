---
name: aos8-migration-readiness
title: AOS 8 → Central migration readiness
description: |
  Assess ArubaOS 8 controller configuration readiness for migration into
  Aruba Central (New or Classic). Walks export + deterministic migration
  plan + dependency stages, flags secrets and reference-only families, and
  stays preview-only until the operator explicitly starts a gated run.
  AOS 6 / Instant-only paths are out of scope.
platforms: [aos8, central]
tags: [aos8, migration, readiness, central, preview]
tools:
  [
    find_tool,
    invoke_read_tool,
    invoke_tool,
    aos8_status,
    aos8_login,
    aos8_export_all,
    aos8_migration_plan,
    aos8_migration_dependency_plan,
    aos8_migration_batch_plan,
    aos8_preview_migration_run,
    aos8_list_controllers,
    aos8_list_aps,
    aos8_get_md_hierarchy,
    aos8_get_cluster_state,
    list_scopes,
    get_global_scope_id,
    ask_docs,
  ]
---

# AOS 8 → Central migration readiness

## Objective

Produce a readiness verdict and staged plan for AOS 8 → Central migration
using this repository's migration tools. Default path is **read-only /
preview**. Creating or applying a migration run is a separate, explicitly
confirmed phase.

## Out of scope

- AOS 6 controller migrations
- Instant-only estates that never used AOS 8 MD hierarchy
- Silent production writes

## Prerequisites

1. `HPE_MCP_PRODUCTS` / toolsets include `aos8`.
2. `find_tool("aos8")` returns migration tools; if not, stop — backend off.
3. Operator names hierarchy node (default `/md`) and desired target type
   (`new_central` or `classic_central`) when planning stages.

## Procedure

### Step 1 — Session + hierarchy

- `aos8_status` (login only if the tool reports no session and credentials exist).
- `aos8_get_md_hierarchy` — do not assume all config lives only at `/md` root.
- Optional live context: `aos8_list_controllers`, `aos8_list_aps`,
  `aos8_get_cluster_state`. If live-state calls degrade, continue with config
  export and mark live checks inconclusive.

### Step 2 — Export

- `aos8_export_all(config_path=...)`
- Surface export `warnings` and missing object families honestly.

### Step 3 — Deterministic plan

- `aos8_migration_plan(config_path=...)`
- Inspect candidates for `classic_central` and/or `new_central`, per-object
  diff/warnings, and unsupported fields.

### Step 4 — Dependency stages

- `aos8_migration_dependency_plan(target_type=..., migration_plan=...)`
- Report stages by `apply_order` and counts: `ready`, `blocked`,
  `reference_only`, `requires_secret_input`.
- Call out every blocked dependency and every reference-only family (manual
  recreation — no automatic write in-repo for those).

### Step 5 — Optional batching / preview

Only when the operator wants deeper preview **without** persistence:

- `aos8_migration_batch_plan` if available for chunking.
- `aos8_preview_migration_run` for **ready** candidates only (never blocked).
- Preview may perform existence GETs on Central; it must not apply writes.

### Step 6 — Central landing zone (if Central enabled)

- Resolve destination scope (`get_global_scope_id` / `list_scopes`).
- Note missing groups/sites the operator must create before apply.

### Step 7 — Verdict

| Verdict | When |
|---|---|
| GO | Export ok; no hard blockers; secrets plan understood |
| PARTIAL | Some families ready, others blocked/reference-only |
| BLOCKED | Missing source session, empty export, or unresolved hard dependencies |
| EMPTY-SOURCE | No migratable candidates |

## Gated write phase (NOT default)

Only after explicit operator confirmation naming target + scope + persona:

1. Still prefer dry-run / preview first.
2. `aos8_create_migration_run` / `aos8_apply_migration_run` require product
   write access and tool-level confirmations.
3. Follow with `aos8_verify_migration_run` and, if needed, rollback tools.
4. Never invent secret values for `requires_secret_input` candidates.

## Output format

1. Reachability + hierarchy node
2. Export warnings
3. Candidate totals by target type
4. Stage table (order, ready/blocked/reference-only/secrets)
5. Manual recreation list
6. Verdict + safest next step (usually more preview or fix blockers)

## Docs

Use `ask_docs` / contract docs for mapping questions; live tools beat memory
for inventory. Cite paths when docs are used.
