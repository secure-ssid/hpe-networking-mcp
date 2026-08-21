---
name: cross-platform-rf-check
title: Site RF and channel health check
description: |
  Read-only RF health for a named site: channel utilization, radios, air
  quality, neighbors/rogues, and optional Mist assurance signals when enabled.
  Use for co-channel pressure, noisy 2.4/5/6 GHz, or "is RF bad at site X".
  Adapts to whichever wireless platform tools exist in the catalog.
platforms: [central, mist]
tags: [rf, wireless, channel, spectrum, air-quality, site]
tools:
  [
    find_tool,
    invoke_read_tool,
    find_scope,
    list_sites,
    get_site,
    list_devices,
    get_channel_utilization,
    get_ap_radios,
    list_radios,
    get_air_quality,
    get_ap_neighbors,
    list_rogue_aps,
    get_wireless_metrics,
    get_device_health,
    mist_list_sites,
    mist_get_site_sle_metric_summary,
    mist_get_site_assurance_snapshot,
    mist_list_alarms,
  ]
---

# Site RF and channel health check

## Objective

Build a per-band RF picture for one site and flag likely RF pain (high
utilization, missing radios, dense co-channel use, rogues) without changing
any radio parameters.

**Read-only.**

## Prerequisites

- Operator supplies a site name (or picks from `list_sites` / `mist_list_sites`).
- At least Central **or** Mist wireless tooling must be available.

## Procedure

### Step 0 — Platform scope

From the request, decide: Central-only, Mist-only, or both. Skip missing
backends with a one-line note.

### Step 1 — Resolve site

- Central: `find_scope` / `list_sites` / `get_site` → site id + health if present.
- Mist: `mist_list_sites` → site_id match by name.

### Step 2 — Central RF signals (when enabled)

Prefer bounded site/device scoped calls via `find_tool` if names differ:

1. AP inventory for the site (`list_devices` filtered to APs).
2. `get_channel_utilization` / `list_radios` / `get_ap_radios` on a **sample**
   of APs (worst clients/health first; cap detail, e.g. 10 APs unless asked).
3. `get_air_quality` when available.
4. `get_ap_neighbors` / `list_rogue_aps` for interference context.
5. `get_wireless_metrics` for site- or device-level aggregates if present.

### Step 3 — Mist RF / assurance (when enabled)

- `mist_get_site_assurance_snapshot` and/or `mist_get_site_sle_metric_summary`
  for wireless SLEs.
- `mist_list_alarms` for the site/org if scope parameters exist.
- There is **no** dedicated Mist channel-planning tool in this catalog — do not
  invent planner templates.

### Step 4 — Analyze

Per band (2.4 / 5 / 6 when data exists):

- radio count and down radios
- channels seen + obvious overcrowding on one channel
- utilization / noise highlights when fields exist
- rogue/neighbor pressure

Recommendations stay non-destructive (survey, move SSIDs, schedule RF changes
later with confirmation).

## Output format

1. Site identity + platforms used
2. Per-band summary table
3. Top AP outliers (serial, band, channel, util/noise if known)
4. Rogue/neighbor notes
5. Mist SLE/assurance blurb when present
6. Read-only next checks

## Honest gaps

Deferred / unavailable here:

- Mist `current_channel_planning` style APIs
- Composite `site_rf_check` tool
- Automated channel/power writes

Do not claim a full predictive RF design study from these monitoring reads.
