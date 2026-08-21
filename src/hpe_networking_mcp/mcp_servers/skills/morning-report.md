---
name: morning-report
title: Morning operations report
description: |
  Last-24h operator digest across enabled platforms (Central default; Mist,
  UXI, GLP when present). Covers activity/audit, active incidents, worst
  sites, and optional synthetic-sensor status. Supports engineer detail or a
  short executive summary based on phrasing. Read-only.
platforms: [central, mist, uxi, glp]
tags: [morning, daily-digest, alerts, audit, baseline, standup]
tools:
  [
    find_tool,
    invoke_read_tool,
    get_tenant_health,
    list_sites,
    list_sites_client_health,
    list_active_alerts,
    list_audit_logs,
    list_devices,
    list_clients,
    mist_status,
    mist_list_sites,
    mist_list_alarms,
    mist_get_org_sle_overview,
    mist_list_org_inventory,
    uxi_status,
    uxi_list_sensors,
    uxi_get_sensor_status,
    list_glp_reporting_statuses,
  ]
---

# Morning operations report

## Objective

Produce a single last-24h ops digest the operator can scan quickly. Lead with
a status color (GREEN / YELLOW / RED), then either a detailed engineer report
or a short executive paragraph.

**Read-only.** Never clear alerts, reboot devices, or write config.

## Output mode

Infer mode from the request:

| Phrasing | Mode |
|---|---|
| morning report, overnight rundown, daily digest, standup | **Engineer** (default) |
| executive summary, leadership briefing, 30-second summary, tell my manager | **Executive** |

Gather the same data either way; only the final template changes.

## Prerequisites

- Prefer `find_tool` → `invoke_read_tool` for every call.
- Skip any platform whose tools are missing from the catalog or return
  not-configured / auth errors. Note the skip in one line; do not invent data.
- Time window: last 24 hours from the operator request time. Pass ISO bounds
  only when a tool accepts them.

## Procedure

### Step 1 — Reachability snapshot

1. Central: `get_tenant_health` (or nearest health tool).
2. If Mist enabled: `mist_status` then continue only if configured.
3. If UXI enabled: `uxi_status`.
4. If GLP enabled: optional `list_glp_reporting_statuses` (failed only).

If Central is unavailable and no other platform works, stop with RED and the
error.

### Step 2 — Activity (who changed what)

- Central: `list_audit_logs` for the window, limit tight (≤50). Summarize top
  actors and action categories; never paste full payloads.
- Mist: if audit-style tools exist via `find_tool("mist audit")`, use them;
  otherwise state "Mist audit not in catalog" and continue.

### Step 3 — What's broken now

- Central: `list_active_alerts` critical/high first; group by site/category.
- Sites: `list_sites` / `list_sites_client_health` — keep worst 3–5 only.
- Devices: `list_devices` offline/down sample when supported.
- Mist: `mist_list_alarms` + `mist_get_org_sle_overview` when enabled.
- Bound every list (`limit` ≤ 50 unless the operator asks for more).

### Step 4 — Load / talkers (engineer mode only)

- Central: `list_clients` limited sample; note top sites/SSIDs if fields exist.
- Do not dump client MAC inventories into the report.

### Step 5 — UXI synthetic view (optional)

When UXI is enabled:

1. `uxi_list_sensors` (small page).
2. For offline or failing sensors only, `uxi_get_sensor_status`.
3. Remember UXI MACs are **sensor** MACs, not end-user devices.

### Step 6 — Status rubric

| Color | When |
|---|---|
| GREEN | No critical alerts; health ≥ 90; no material offline spike; UXI sensors mostly online |
| YELLOW | Major/high alerts, health 70–89, a few offline devices, or partial platform gaps |
| RED | Critical outage signals, health &lt; 70, many offline devices, or primary platform down |

## Output format

### Engineer template

1. Status color + one headline paragraph
2. Activity (top actors / change types)
3. Broken now (alert groups + worst sites)
4. Optional UXI / GLP notes
5. Suggested **read-only** next checks (name skills/tools, no writes)

### Executive template

1. Status color
2. 4–6 plain-language sentences (no tool names, no raw counts dump)
3. Top 1–2 business impacts
4. One recommended human action

## Honest gaps

- No durable day-over-day baseline store in this repository — do not claim
  "vs yesterday" unless the operator supplies a prior report.
- Mist/UXI sections require those products in `HPE_MCP_PRODUCTS`.
- Top-talker precision depends on which monitoring fields the tenant returns.
