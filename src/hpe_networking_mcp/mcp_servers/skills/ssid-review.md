---
name: ssid-review
title: SSID / WLAN configuration review
description: |
  Review existing SSIDs and related roles/VLANs for a scope. Use before
  creating a new SSID, when auditing guest vs corporate WLANs, or when the
  user asks what wireless networks are configured where.
platforms: [central]
tags: [ssid, wlan, wireless, audit]
tools: [list_ssids, list_scopes, get_global_scope_id, list_roles, find_tool, invoke_read_tool, ask_docs, lookup_api]
---

# SSID / WLAN configuration review

## Objective

Produce a compact inventory of SSIDs for the requested scope, with security
mode, VLAN/role hints, and gaps (missing guest network, open SSIDs, etc.).

## Prerequisites

- Scope intent: org-wide, site name, or group name.
- Default persona is `CAMPUS_AP` unless the user specifies gateways.

## Procedure

### Step 1 — Resolve scope

- org-wide → `get_global_scope_id`
- named site/group → `list_scopes` and match `scope_name`

### Step 2 — List SSIDs

**Tool:** `list_ssids` (and any WLAN list/get tools via `find_tool`)
**Capture:** name, enabled, security mode, VLAN/role references, scope.

### Step 3 — Related objects

For SSIDs that reference roles/VLANs, resolve names with role/VLAN list tools
when available. Do not recursively dump every profile.

### Step 4 — Docs/API clarification (as needed)

If a field/enum is unclear (e.g. WPA3 transition), use `lookup_api` first,
then `ask_docs`.

### Step 5 — Risk highlights

Flag:
- open / enhanced-open guest exposure on corporate scopes
- duplicate SSID names across scopes
- missing critical SSID the user expected

## Output format

Table: SSID | scope | security | VLAN/role | enabled | notes

If the user wants a new SSID next, stop after the review and confirm:
WHERE, DEVICE TYPE, SECURITY mode, passphrase (if any), and VLAN IDs before
any dry-run write.
