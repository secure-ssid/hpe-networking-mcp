---
name: change-pre-check
title: Pre-change readiness checklist
description: |
  Capture a pre-change baseline before SSID, VLAN, firmware, or scope changes:
  health, alerts, target devices, and current config pointers. Use before any
  write workflow so post-change diffs have a clean reference.
platforms: [central, glp]
tags: [change, pre-check, baseline, maintenance]
tools: [get_tenant_health, list_sites, list_active_alerts, list_devices, list_ssids, find_tool, invoke_read_tool]
---

# Pre-change readiness checklist

## Objective

Establish a readable baseline and go/no-go signal before a planned change.
**Read-only.** Do not perform the change in this skill.

## Prerequisites

- Change target is known: site/group/global, device type, and change class
  (SSID, role, VLAN, port profile, firmware, GLP assignment, etc.).
- Writes stay blocked until the operator explicitly approves a later step.

## Procedure

### Step 1 — Confirm scope

Translate GUI language to Central scope:
- everywhere / org-wide → global scope id
- site or group name → `list_scopes` / site list match

Record: scope name, scope id, persona/device type.

### Step 2 — Health baseline

**Tools:** `get_tenant_health`, target site health if scoped
**Capture:** health scores and top degraded scopes.

### Step 3 — Alert baseline

**Tool:** `list_active_alerts` (critical/major)
**Capture:** open alert count and top signatures for the target scope.

### Step 4 — Inventory baseline

**Tool:** `list_devices` for the target scope
**Capture:** device counts by type and offline count.

### Step 5 — Config pointers

Depending on change class, read the current object only:
- SSID/WLAN → `list_ssids` / WLAN getters
- VLAN/role/port profile → matching config list/get tools via `find_tool`
- Firmware → compliance/version tools if available

Do not dump full config bodies unless needed for the specific change.

### Step 6 — Go / no-go

No-go if:
- target scope cannot be resolved uniquely
- a critical outage is already in progress on the same scope
- required write prerequisites (VLAN, role, auth server) are missing

## Output format

- Change statement (one sentence)
- Scope + device type
- Baseline table: health | critical alerts | offline devices | key config object
- Go/no-go with reasons
- Suggested post-check skill: `change-post-check`
