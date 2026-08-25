---
title: "0.3.0"
nav_order: 8
parent: "Releases"
---

# hpe-networking-mcp 0.3.0 - platform parity, migrations, and safety

Version 0.3.0 is the largest hpe-networking-mcp expansion so far. It updates the core
Aruba Central and GreenLake Platform surfaces, turns every optional product
starter into a guarded read/write lab backend, repairs AOS8 and Apstra
authentication, and refreshes the exact API index from current Aruba and Mist
sources.

![hpe-networking-mcp platform coverage](assets/platform-coverage.svg)

## Catalog snapshot

| Catalog | Tools | Intended use |
|---|---:|---|
| Generated operation manifests | 5,703 | Reproducible platform API coverage |
| Complete backend index | 6,133 | Discovery/dispatch across every enabled backend |
| Direct-all router | 6,136 | Full schema introspection plus three router tools |

Minimal mode remains the recommended three-tool client surface.

## Major additions

- **Aruba Central:** configuration checkpoint policy and automatic rollback
  status guidance, BGP, OSPF, VRF, high availability, telemetry, application experience, configuration health,
  topology, notification rules, device notes, onboarding, AP tunnels, named
  MPSK, visitors, and expanded gateway/AP diagnostics.
- **GreenLake Platform:** current devices and documented attribute grouping, Audit Logs
  v2beta1, subscriptions, workspaces, reporting, and guarded API-family writes.
- **ArubaOS 8:** UIDARUBA/X-CSRF/SESSION authentication, exhaustive exports, normalized WLAN,
  role, VLAN, AP-group, controller, and policy parsing, plus deterministic
  Classic Central and New Central migration candidates, warnings, diffs, and
  verification plans.
- **Mist, Apstra, ClearPass, UXI, and Axis:** Mist NAC/Marvis/Wired/WAN, Apstra
  official-SDK AuthToken sessions and object policies, ClearPass Insight/OnGuard activity,
  and UXI guarded lifecycle and assignment workflows.
- **EdgeConnect:** 1,216 generated operations plus live Swagger/API diagnostics.
  The pinned artifact declares API 7.2.0 internally, so production use still
  requires comparison with the target Orchestrator's Swagger document.

## Framework and transport safety

- Per-platform write gates with read-only defaults.
- Dry-run previews and explicit confirmation for optional product writes.
- Streamable HTTP `/livez`, `/readyz`, and `/healthz` endpoints.
- Host/origin validation and optional bearer protection for streamable HTTP.
- Protocol-level MCP tests, rate-limit metadata, deprecation/sunset handling,
  concurrent token refresh protection, and optional session-scoped secret
  tokenization.

## RAG and API source refresh

The current local release index separates prose retrieval from exact API lookup:

| Index | Current content |
|---|---:|
| LanceDB prose corpus | 47,633 chunks |
| OpenAPI specifications | 239 |
| Exact endpoints | 3,465 |
| Schemas | 10,297 |
| Fields | 57,131 |

Aruba specifications now resolve through the July 2026 ReadMe API registry
format. The official `mistsys/mist_openapi` 2606.1.1 snapshot is pinned and
verified. Weekly GitHub Actions checks report Aruba registry or Mist upstream
drift.

## Upgrade notes

1. Run `uv sync`.
2. Rebuild the router catalog. Use the safe read-only default, or set
   `HPE_MCP_PRODUCT_ACCESS=read-write` to index all 6,133 backend tools.
3. Download the latest prebuilt indexes or refresh and rebuild local sources.
4. Run `uv run python scripts/doctor.py`.
5. Review [optional product safety](optional-products.md) before enabling
   platform writes.
