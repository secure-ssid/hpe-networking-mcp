---
name: infrastructure-health-check
title: Cross-platform infrastructure health snapshot
description: |
  One-shot operational overview across enabled platforms — tenant/site health,
  active alerts, offline devices, and GreenLake reporting failures when GLP is
  enabled. Use when the user asks "is everything healthy?", wants a daily
  standup, or needs a baseline before deeper troubleshooting.
platforms: [central, glp, mist, clearpass]
tags: [health, monitoring, daily-standup, baseline]
tools: [get_tenant_health, list_sites, get_alerts, list_devices, list_glp_reporting_statuses, find_tool, invoke_read_tool]
---

# Cross-platform infrastructure health snapshot

## Objective

Surface degraded or alarming conditions across enabled platforms in one concise
report. Prefer under 60 seconds wall-clock and a result the operator can scan
in under 30 seconds.

## Prerequisites

- At least one backend must be configured (Central is the default).
- Use `find_tool` + `invoke_read_tool` rather than guessing tool names.
- Do not run destructive tools.

## Procedure

### Step 1 — Tenant / org health

**Tool:** `get_tenant_health` (or find an equivalent health tool)
**Why:** Establishes the top-level health score and worst scopes.
**Expected result:** Overall health score plus per-scope breakdown.
**If anomaly:** Health below 80 or missing scopes → prioritize those sites next.

### Step 2 — Site health table

**Tool:** `list_sites` / site health helpers
**Why:** Identifies the worst 3–5 sites by client or device health.
**Expected result:** Compact table: site, health, offline devices, clients.
**If anomaly:** Drill only into the bottom sites; avoid dumping every site.

### Step 3 — Active alerts

**Tool:** `get_alerts` / alert list tools (severity critical/major first)
**Why:** Alerts often explain health drops faster than device walks.
**Expected result:** Top alerts with severity, scope, and age.
**If anomaly:** Group repeated alert types; note flapping vs sustained.

### Step 4 — Offline / degraded devices

**Tool:** `list_devices` filtered to offline/down when supported
**Why:** Confirms whether health issues are broad or a few serials.
**Expected result:** Count by device type + sample serials/sites.
**If anomaly:** One site owning most offline devices points to uplink/power.

### Step 5 — GreenLake reporting (if GLP enabled)

**Tool:** `list_glp_reporting_statuses`
**Why:** Catch workspace/reporting pipeline failures outside Central MRT.
**Expected result:** Failed or non-success statuses with IDs.
**If anomaly:** Include status IDs and counts; do not open every record unless asked.

### Step 6 — Optional products (only if enabled)

If Mist/ClearPass/etc. are enabled, use `find_tool` for org alarms / system
events. Skip platforms that are not in the tool catalog.

## Output format

1. One-line overall status (healthy / degraded / critical)
2. Table: scope/site | health | critical alerts | offline devices | note
3. Top 3 recommended next actions (read-only first)
