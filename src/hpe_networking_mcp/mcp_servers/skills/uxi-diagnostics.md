---
name: uxi-diagnostics
title: UXI sensor and synthetic-test diagnostics
description: |
  Diagnose HPE Aruba UXI sensor health and correlate synthetic failures to
  Central/Mist/AOS8 infrastructure when those backends are enabled. Sensors
  are synthetic clients — their MACs are not end-user devices. Read-only
  correlation; assignment/group writes stay gated and out of band.
platforms: [uxi, central, mist, aos8]
tags: [uxi, diagnostics, sensors, synthetic, correlation, experience]
tools:
  [
    find_tool,
    invoke_read_tool,
    uxi_status,
    uxi_list_sensors,
    uxi_get_sensor_status,
    uxi_list_agents,
    uxi_list_groups,
    uxi_list_service_tests,
    uxi_list_wireless_networks,
    uxi_list_wired_networks,
    uxi_list_sensor_group_assignments,
    list_sites,
    list_wlans,
    list_clients,
    find_client,
    list_active_alerts,
    get_device_health,
    mist_list_sites,
    mist_list_wlans,
    mist_get_client,
    mist_list_alarms,
    aos8_find_client,
    aos8_get_client_detail,
    aos8_list_active_aps,
    aos8_get_alarms,
  ]
---

# UXI sensor and synthetic-test diagnostics

## Objective

Determine whether UXI pain is sensor/offline, test design, or upstream
network/auth/RF, and emit a single verdict: **GO / DEGRADED / CRITICAL**.

**Read-only** inside this skill.

## Critical interpretation rules

1. UXI sensors are **synthetic clients**. Any `macAddress` is the sensor's
   MAC — use it to find the attached AP/switch, never to blame a person.
2. Missing optional platforms are **INFO skips**, not CRITICAL failures.
3. If UXI itself is unconfigured, say so and stop (or continue only if the
   operator pastes sensor symptoms manually).

## Prerequisites

- `HPE_MCP_PRODUCTS` includes `uxi` for live pulls.
- Discover tools with `find_tool` first in minimal router mode.

## Procedure

### Step 1 — UXI reachability

- `uxi_status`
- On failure: report CRITICAL for UXI path only; still note other platforms
  were not the cause of the UXI API outage.

### Step 2 — Inventory

- `uxi_list_sensors`, `uxi_list_agents`, `uxi_list_groups`
- `uxi_list_service_tests`, `uxi_list_wireless_networks`, `uxi_list_wired_networks`
- Keep pages small; focus on failing/offline entities.

### Step 3 — Status fan-out (bounded)

- For offline or unhealthy sensors only, `uxi_get_sensor_status`.
- Cap detailed pulls (e.g. 15) unless the operator expands.
- Optional: `uxi_list_sensor_group_assignments` when group placement matters.

### Step 4 — Correlate anchors

From sensor/test fields, collect whatever exists: network name, group path,
SSID, site/location text, sensor MAC.

Then, only for enabled platforms:

| Platform | Correlation ideas |
|---|---|
| Central | `list_sites` / WLAN lists; `find_client` on **sensor MAC**; AP `get_device_health`; `list_active_alerts` |
| Mist | site/WLAN lists; `mist_get_client` for sensor MAC; site alarms |
| AOS8 | `aos8_find_client` / `aos8_get_client_detail`; `aos8_list_active_aps`; `aos8_get_alarms` |

If a platform is disabled, record `skipped: <platform> not enabled`.

### Step 5 — Verdict

| Verdict | Criteria |
|---|---|
| GO | Sensors online; tests healthy; no correlated infra alarms |
| DEGRADED | Partial sensor issues or single-site synthetic failures with mild infra signals |
| CRITICAL | Many sensors offline, widespread test failure, or clear correlated infra outage |

## Output format

1. Verdict
2. UXI summary counts (sensors/agents/tests; unhealthy samples)
3. Correlation table: sensor | network/group | infra attachment | evidence
4. Likely failure domain: sensor | RF | auth/NAC | WAN/DNS/DHCP | unknown
5. Read-only next steps

## Gated writes (out of band)

Group/sensor assignment tools (`uxi_assign_*`, `uxi_update_sensor`, …) need
explicit intent plus product write gates. Not part of diagnostics.

## Honest gaps

- UXI service tests have no create/update/delete API upstream (assignment only).
- Deep per-test historical analytics beyond list/status depend on what `uxi_get`
  allows; do not invent metrics.
