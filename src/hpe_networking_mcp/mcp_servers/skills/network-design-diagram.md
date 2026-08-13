---
name: network-design-diagram
title: Network design diagram (Draw.io / Graphviz / NeXt)
description: |
  Draw or export an editable network topology diagram. Primary path is Draw.io
  / diagrams.net XML; optional Graphviz DOT/SVG/PNG and NeXt UI JSON. Use when
  the user asks to draw a network diagram, export topology, produce a drawio
  file, render graphviz, or build a design topology map from inventory.
platforms: [design, central]
tags: [design, diagram, topology, drawio, graphviz, nextui, network-design]
tools:
  [
    list_skills,
    load_skill,
    find_tool,
    invoke_read_tool,
    list_sites,
    get_topology,
    list_diagram_roles_and_vendors,
    validate_diagram_model,
    drawio_network_design_diagram,
    export_graphviz_topology,
    export_next_ui_topology,
    list_diagram_icons,
    resolve_diagram_icon,
  ]
---

# Network design diagram

## Objective

Produce an editable network design artifact (Draw.io primary) plus optional
Graphviz/NeXt exports. Prefer live Central topology when a site is known.

## Prerequisites

- Design backend enabled:
  ```bash
  export HPE_MCP_PRODUCTS=design
  # optional local vendor icons (do not commit third-party logos):
  # export HPE_MCP_DIAGRAM_ICON_DIR=/path/to/icon-pack
  ```
- In router minimal mode, keep `design` in `HPE_MCP_PRODUCTS` even when
  `HPE_MCP_TOOLSETS=central,glp,rag` (products are unioned onto toolsets).
- Rebuild tool catalog after enabling so `find_tool` semantic search can see
  design tools: `uv run python scripts/ingest_tools.py --products all`
- Prefer `find_tool` + `invoke_read_tool`. All design tools are read-only.

## Procedure

### Step 1 — Gather topology input

**Tool:** `list_sites` → `get_topology` (monitoring), when a live site is in scope.
**Why:** Live nodes/links beat a hand-built model when inventory exists.
**Expected result:** Topology payload with nodes + links (or a clear gap).
**If anomaly:** No site/topology API → build a structured model instead
(roles: core_switch, access_switch, campus_ap, gateway, clearpass, mist_ap, …).
Discover accepted roles/vendors with `list_diagram_roles_and_vendors`.

### Step 2 — Validate the design model

**Tool:** `validate_diagram_model`
**Why:** Catch missing ids/links before export.
**Expected result:** ok/valid model ready for export.

### Step 3 — Primary export (Draw.io)

**Tool:** `drawio_network_design_diagram` with `save=true`
**Why:** Editable diagrams.net / Draw.io XML is the default operator artifact.
**Expected result:** `outputs/diagrams/*.drawio` path (or inline XML if save=false).

### Step 4 — Optional Graphviz image

**Tool:** `export_graphviz_topology` (`render_format=svg` or `png` if `dot` installed)
**Why:** Direct image generation / icon-pack rendering.
**Expected result:** DOT (+ rendered image when Graphviz is available).

### Step 5 — Optional NeXt UI

**Tool:** `export_next_ui_topology`
**Why:** Interactive web dashboard JSON + HTML stub.
**Expected result:** `.next.json` + preview HTML under `outputs/diagrams/`.

### Step 6 — Icons (as needed)

**Tool:** `list_diagram_icons` / `resolve_diagram_icon`
**Why:** Confirm local vendor/role art; do not invent external logo URLs.

## Icon policy

Ship only generic shapes in-repo. For Mist/Juniper product art, download under
Juniper terms from:
https://www.juniper.net/us/en/company/images/image-library-logos-and-product-photos.html
into `HPE_MCP_DIAGRAM_ICON_DIR/vendors/mist/`.

## Output format

- Which export ran and saved paths
- Whether topology came from live inventory or a hand-built model
- Next open step (e.g. open the `.drawio` in diagrams.net)
