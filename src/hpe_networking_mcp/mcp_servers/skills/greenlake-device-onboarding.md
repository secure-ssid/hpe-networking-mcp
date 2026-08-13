---
name: greenlake-device-onboarding
title: GreenLake device onboarding checklist
description: |
  Walk device onboarding on HPE GreenLake Platform: locate the device,
  subscription/assignment state, workspace context, and service-catalog
  provisions relevant to networking services. Use when a device is missing
  from Central, unlicensed, or stuck in GLP inventory.
platforms: [glp, central]
tags: [greenlake, onboarding, subscription, inventory]
tools: [list_glp_devices, get_glp_device, list_glp_subscriptions, list_glp_service_provisions, list_glp_service_offers, find_tool, invoke_read_tool]
---

# GreenLake device onboarding checklist

## Objective

Determine whether a device is present in GLP, correctly subscribed/assigned,
and ready for the networking service (Central/Mist/etc.). Produce a clear
blocker list.

## Prerequisites

- Serial number, mac, or order/subscription identifier if available.
- GLP credentials configured (`glp_account` / `target_account`).

## Procedure

### Step 1 — Find the device in GLP

**Tools:** `list_glp_devices` / `get_glp_device`
**Expected:** device inventory record with serial, product, workspace.
**If missing:** stop and report "not in GLP inventory" — activate/claim path is outside pure read tools.

### Step 2 — Subscription state

**Tools:** `list_glp_subscriptions` and related getters via `find_tool`
**Expected:** an applicable subscription covering the device/product.
**If anomaly:** expired, unassigned, or wrong workspace subscriptions are common blockers.

### Step 3 — Service provisions / offers

**Tools:** `list_glp_service_provisions`, `list_glp_service_offers`
**Why:** Confirms the networking service is provisioned for the workspace/region.
**Expected:** active provision for the intended service.

### Step 4 — Cross-check Central (if enabled)

**Tools:** Central `list_devices` / `get_device`
**Expected:** same serial visible in Central after GLP assignment completes.
**If GLP-only:** report "in GLP, not yet in Central".

### Step 5 — Workspace / audit clues

**Tools:** workspace getters / recent audit logs if needed
**Use sparingly:** only when assignment history is unclear.

## Output format

- Device identity + GLP workspace
- Subscription: ok / missing / mismatched
- Service provision: ok / missing
- Central visibility: yes / no / not checked
- Ordered blockers and safe next actions
