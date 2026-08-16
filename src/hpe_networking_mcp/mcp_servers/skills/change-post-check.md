---
name: change-post-check
title: Post-change validation checklist
description: |
  Validate a completed Central/GLP change against the pre-change baseline:
  health deltas, new alerts, device reachability, and presence of the intended
  config object. Use immediately after SSID, VLAN, role, or assignment changes.
platforms: [central, glp]
tags: [change, post-check, validation, maintenance]
tools: [get_tenant_health, list_active_alerts, list_devices, list_ssids, find_tool, invoke_read_tool]
---

# Post-change validation checklist

## Objective

Confirm the intended change is visible and did not create an obvious outage.
This skill is read-only.

## Prerequisites

- Pre-change baseline numbers if available (from `change-pre-check`).
- Known change target (scope + object name).

## Procedure

### Step 1 — Intended object present

**Tools:** list/get for the changed object (`list_ssids`, VLAN/role getters, etc.)
**Expected:** object exists with the expected key fields (name, VLAN, security mode, scope).

### Step 2 — Device reachability

**Tool:** `list_devices` on the affected scope
**Compare:** offline count vs baseline. Investigate if offline jumped materially.

### Step 3 — Health delta

**Tool:** `get_tenant_health` / site health
**Compare:** health score vs baseline. Small transient dips can be normal after AP config push; sustained drops are not.

### Step 4 — New alerts

**Tool:** `list_active_alerts`
**Look for:** new critical/major alerts since change time on the same scope.

### Step 5 — Client sample (when wireless)

**Tool:** `list_clients` limited to the site/SSID if available
**Expected:** clients still associating; note auth failures if present.

## Output format

- Change verified: yes/no/partial
- Object field checklist
- Delta table: health | offline | critical alerts (before → after when known)
- Residual risks and recommended watch window
