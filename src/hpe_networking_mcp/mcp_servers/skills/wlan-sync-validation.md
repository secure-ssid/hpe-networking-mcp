---
name: wlan-sync-validation
title: Central ↔ Mist WLAN consistency check
description: |
  Compare WLAN/SSID inventories between Aruba Central and Juniper Mist for
  a scope the operator names. Classifies SSIDs as in-sync, drift, or
  single-platform. Uses live list tools plus credential-free interop
  translators for field mapping. Read-only — does not push WLAN config.
platforms: [central, mist, interop]
tags: [wlan, ssid, mist, central, sync, drift, wireless]
tools:
  [
    find_tool,
    invoke_read_tool,
    list_ssids,
    list_wlans,
    get_ssid,
    get_wlan,
    list_scopes,
    find_scope,
    mist_status,
    mist_list_sites,
    mist_list_wlans,
    translate_central_wlan_to_mist,
    translate_mist_wlan_to_central,
  ]
---

# Central ↔ Mist WLAN consistency check

## Objective

Answer "are my SSIDs aligned across Central and Mist?" with a compact
diff, not a config push.

**Read-only.**

## Prerequisites

- Both platforms should be enabled for a full compare. If only one is
  present, inventory that side and stop with a partial result.
- Resolve Central scope (`central-scope-walker`) when not org-wide.
- Mist calls need a valid site/org context from `mist_list_sites` /
  operator-provided IDs.

## Procedure

### Step 1 — Reachability

- Central: a cheap read such as `list_ssids` with a small limit or scope resolve.
- Mist: `mist_status` then `mist_list_sites` / `mist_list_wlans`.

### Step 2 — Pull Central WLANs

- `list_ssids` and/or `list_wlans` for the scope.
- Pull `get_ssid` / `get_wlan` only for drift candidates or operator-named SSIDs.
- Capture when present: name, enabled, security/opmode, VLAN(s), hidden/broadcast, band.

### Step 3 — Pull Mist WLANs

- `mist_list_wlans` for the site (or org-scoped variant if `find_tool` shows one).
- Same field capture with Mist names (`auth`, `vlan_ids`, `hide_ssid`, etc.).

### Step 4 — Index and classify

Key by SSID name (case-insensitive):

| Bucket | Rule |
|---|---|
| In sync | Both sides present; compared fields equivalent after mapping |
| Drift | Both present; one or more fields differ |
| Central only | Missing on Mist |
| Mist only | Missing on Central |

### Step 5 — Field mapping assist

For drift rows, optionally run offline translators:

- `translate_central_wlan_to_mist`
- `translate_mist_wlan_to_central`

Treat translator `warnings` as first-class — especially security/auth mapping,
which is best-effort and **not** a guaranteed vendor contract.

Pay attention to inverted hidden-SSID semantics (`hide_ssid` vs broadcast flags).

### Step 6 — Bound the report

If either side returns a huge catalog, summarize counts and only detail:

- operator-named SSIDs, or
- drift + single-platform rows (cap detail rows, e.g. 25)

## Output format

1. Scope + platforms compared
2. Counts per bucket
3. Drift table: SSID | field | Central | Mist | notes/warnings
4. Single-platform SSID names
5. Next steps: read-only only (no apply). If the operator wants remediation,
   require an explicit write session with dry-run first on the target platform.

## Honest gaps

- No `translate_wlan_apply` orchestration tool in this repo.
- Mist org-template vs site-WLAN distinction may be incomplete depending on
  which Mist list tools are enabled.
- PSK values must never be printed; presence-only.
