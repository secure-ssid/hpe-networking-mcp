---
name: client-connectivity
title: Client connectivity investigation
description: |
  Investigate one wireless/wired client by MAC, name, or IP. Correlate the
  client record with AP/switch, site health, and recent auth or RF symptoms.
  Use when the user reports "can't connect", slow Wi-Fi, or roaming issues for
  a specific endpoint.
platforms: [central, nac]
tags: [client, connectivity, troubleshooting, wireless]
tools: [find_client, get_client, list_clients, get_device, get_alerts, find_tool, invoke_read_tool]
---

# Client connectivity investigation

## Objective

Determine whether a client problem is endpoint, RF/AP, switch/port, auth/NAC,
or site-wide. Return a short root-cause hypothesis and safe next checks.

## Prerequisites

- A client identifier: MAC preferred; name or IP acceptable.
- Prefer `find_tool` + `invoke_read_tool`.
- No disconnect/reboot unless the user explicitly requests and confirms.

## Procedure

### Step 1 — Resolve the client

**Tool:** `find_client` / `list_clients` / `get_client`
**Why:** Need associated AP/switch, SSID/VLAN, IP, status, and site.
**Expected result:** One primary client record (or a short candidate list).
**If anomaly:** Multiple matches → ask which one, or pick the currently connected record and state the assumption.

### Step 2 — Attachment point health

**Tool:** `get_device` on the associated AP or switch serial
**Why:** A down radio, high channel util, or switch port fault can look like a client bug.
**Expected result:** Device status, model, site, and any obvious faults.
**If anomaly:** Device offline/degraded → shift to device/site triage.

### Step 3 — Site context

**Tool:** site health / `get_alerts` for the client site
**Why:** Distinguishes single-client vs site-wide incidents.
**Expected result:** Site health score + relevant alerts in the last few hours.
**If anomaly:** Many failed clients or critical RF alerts → site incident, not one laptop.

### Step 4 — Auth / NAC signals (when available)

**Tool:** NAC MAC lookup / auth-related tools via `find_tool`
**Why:** Captive portal, MPSK, or MAC-auth failures often present as "won't join".
**Expected result:** Last auth result or registration state if tools exist.
**If anomaly:** Missing NAC tools → note "auth path not queryable in this session".

### Step 5 — Optional RF / client metrics

**Tool:** wireless metrics / client trends if present
**Why:** Low RSSI, high retries, or band steering issues explain performance complaints.
**Expected result:** One or two metric highlights, not raw dumps.

## Output format

- Client identity + current state (connected/failed/roaming)
- Attachment (AP/switch, SSID/VLAN, site)
- Likely cause (endpoint / RF / infra / auth / unknown)
- Safe next actions (ordered; mark any destructive ones as needing confirmation)
