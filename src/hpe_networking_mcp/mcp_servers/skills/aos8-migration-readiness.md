---
name: aos8-migration-readiness
title: AOS 8 → Central migration readiness
description: |
  Assess AOS 8 controller/config readiness for migration into Aruba Central.
  Use when planning AOS 8 cutover, inventorying profiles, or deciding what can
  translate cleanly versus needs redesign.
platforms: [aos8, central]
tags: [aos8, migration, readiness]
tools: [find_tool, invoke_read_tool, ask_docs]
---

# AOS 8 → Central migration readiness

## Objective

Produce a readiness summary: reachable AOS 8 inventory, high-risk features,
and a staged migration order. Do not write Central config in this skill.

## Prerequisites

- `aos8-core` backend enabled (`HPE_MCP_PRODUCTS` / toolsets includes aos8).
- If AOS 8 tools are absent from the catalog, stop and report that the backend
  is not enabled.

## Procedure

### Step 1 — Confirm AOS 8 tools exist

**Tool:** `find_tool` query "aos8"
**If none:** backend not loaded — cannot continue.

### Step 2 — Inventory controllers / config roots

Use AOS 8 read tools for controllers, hierarchies, and key profile classes
(WLAN, AAA, roles, VLANs, policy). Keep payloads bounded with limit/filters.

### Step 3 — Classify migration difficulty

For each major object class:
- direct map (simple VLAN/SSID/PSK)
- needs redesign (complex roles, PEF policies, legacy portals)
- out of scope / manual

Use `ask_docs` for Central target concepts when mapping is unclear.

### Step 4 — Propose staged order

Typical order:
1. underlay VLANs / named VLANs
2. roles / basic policy
3. auth servers / server groups
4. WLANs / SSIDs
5. gateway cluster / overlay pieces

### Step 5 — Central landing zone check

If Central is enabled, verify destination scopes/groups exist and note gaps.

## Output format

- AOS 8 reachability
- Object-class table: class | count | difficulty | notes
- Recommended stage plan
- Blockers before first write
