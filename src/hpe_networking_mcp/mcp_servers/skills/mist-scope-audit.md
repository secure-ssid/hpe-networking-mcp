---
name: mist-scope-audit
title: Mist site and WLAN configuration audit
description: |
  Bounded Juniper Mist configuration hygiene audit using tools actually
  present in this catalog: sites, WLANs, inventory, alarms, SLE overview,
  NAC tags/portals/IdPs, and user MACs. Read-only. Not a full org-template
  / RF-template walker — those list APIs are not wrapped here yet.
platforms: [mist]
tags: [mist, audit, wlan, configuration, sle, nac]
tools:
  [
    find_tool,
    invoke_read_tool,
    mist_status,
    mist_list_sites,
    mist_list_wlans,
    mist_list_org_inventory,
    mist_list_alarms,
    mist_get_org_sle_overview,
    mist_get_site_sle_metric_summary,
    mist_get_site_assurance_snapshot,
    mist_list_nac_tags,
    mist_list_nac_portals,
    mist_list_nac_idps,
    mist_list_user_macs,
    mist_list_switches,
    mist_list_gateways,
    ask_docs,
  ]
---

# Mist site and WLAN configuration audit

## Objective

Give operators a practical Mist hygiene report: site inventory, WLAN spread,
assurance/SLE posture, NAC-related objects, and obvious alarm hotspots.

**Read-only.** No WLAN delete, claim, or Marvis setting changes.

## Prerequisites

- `HPE_MCP_PRODUCTS` includes `mist` with working credentials.
- Confirm with `mist_status` / `find_tool("mist list sites")`.
- Pick org-wide summary or one site name to keep payloads small.

## Procedure

### Step 0 — Org context

- `mist_list_sites` (bounded). Record site count and name/id map.
- If multiple orgs could apply, stop and ask which org/site set.

### Step 1 — WLAN inventory

- `mist_list_wlans` for the chosen site (repeat only for a small site sample
  if org-wide — do not N+1 hundreds of sites unless asked).
- Flag: duplicate SSIDs, disabled WLANs that look production, open auth on
  non-guest names, missing expected SSID.

### Step 2 — Inventory + wired/WAN edge sample

- `mist_list_org_inventory` (secrets must stay redacted — tool already omits
  claim secrets).
- Optional: `mist_list_switches` / `mist_list_gateways` counts for the site.

### Step 3 — Assurance

- `mist_get_org_sle_overview`
- For the focus site: `mist_get_site_sle_metric_summary` and/or
  `mist_get_site_assurance_snapshot`
- `mist_list_alarms` severity-ordered sample

### Step 4 — Access Assurance / NAC-related objects

When relevant to the operator question:

- `mist_list_nac_tags`, `mist_list_nac_portals`, `mist_list_nac_idps`
- `mist_list_user_macs` only with a tight filter/limit

### Step 5 — Docs

For Mist best-practice questions beyond live state, `ask_docs` with
`source=mist_docs` or `mist_product_updates`. Cite paths.

## Output format

1. Scope (org/site) + site count
2. WLAN table (name | auth | enabled | notes)
3. SLE/assurance + alarm highlights
4. NAC-related object counts/names
5. INFO/DRIFT/REGRESSION findings (only when evidence supports them)
6. Explicit not-covered list

## Honest gaps

Not available as typed tools in this repository today:

- org WLAN/RF/switch/site **template** list APIs
- site-group membership deep audit
- port-profile / device-profile template walkers
- firmware auto-upgrade policy inventory beyond generic `mist_get`

Say "bounded Mist audit" — not a full template-governance sweep.

## Gated writes (out of band)

Writes such as `mist_delete_wlan`, `mist_ack_alarm`, `mist_claim_devices`,
`mist_upsert_user_mac` require explicit operator intent, product write access,
and the tool's dry-run/confirm rules. Never part of this audit.
