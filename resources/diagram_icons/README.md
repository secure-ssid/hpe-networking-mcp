# Diagram icon packs

## What ships in git

**Generic geometric SVGs only** under `generic/`. No vendor logos, Visio
stencils, or HPE PPTX extracts are committed.

## Local private packs (gitignored)

Everything under `private/` stays on your machine:

```text
resources/diagram_icons/private/
  hpe-aruba-visio/              # VisioCafe .vss
  hpe-aruba-symbols/            # older Aruba Symbols .vss
  hpe-technical-icons-2026/     # PPTX → named SVGs + catalog.json
  juniper-official/             # public Visio zips + Mist/product photos
  vendors/aruba|hpe|mist|juniper|.../  # role aliases for Graphviz
```

Or point an external tree:

```bash
export HPE_MCP_DIAGRAM_ICON_DIR=/path/to/your/pack
```

### One-shot install from Downloads

```bash
uv run python scripts/install_diagram_icon_packs.py --from-downloads
```

Looks for:

| File | Installs to |
|---|---|
| `HPE_Technical_Networking_Icons_2026.pptx` | `private/hpe-technical-icons-2026` + `private/vendors/*` |
| `HPE-Aruba-Networking*.zip` / `HPE-Recent_*.zip` | `private/hpe-aruba-visio` |
| `HPE-Aruba-Symbols.zip` | `private/hpe-aruba-symbols` |
| `--juniper` (public CDN) | `private/juniper-official` + `vendors/juniper|mist` |

### License (important)

HPE VisioCafe / PPTX / Symbols packs are for **local diagram creation**.
Do **not** redistribute, reverse-engineer, or host them. hpe-networking-mcp keeps
them gitignored for that reason.

## How tools use icons

| Tool | Uses what |
|---|---|
| **Microsoft Visio** | `.vss` from private packs → copy into `Documents/My Shapes/HPE` |
| **`drawio_network_design_diagram`** | mxgraph/generic shapes (no auto-embed of `.vss`) |
| **`export_graphviz_topology`** | PNG/SVG role files via `vendors/<vendor>/<role>.svg` |
| **`list_diagram_icons`** | Discovers local SVG/PNG + `.vss` and reports sources |

Resolution order for Graphviz: env dir → `private/` → in-repo `generic/`.

### Role → Visio stencil hints

| Design role | Stencil pack |
|---|---|
| `core_switch` / `agg_switch` / `access_switch` | `HPE-Aruba-Switches-*.vss` |
| `campus_ap` | `HPE-Aruba-Wireless.vss` / Symbols Access Points |
| `gateway` / `controller` | `HPE-Aruba-Gateways+Controllers.vss` |
| `clearpass` / `firewall` | `HPE-Aruba-Security.vss` |
| EdgeConnect | `HPE-Aruba-EdgeConnect.vss` |
| `server` | `HPE-Compute-AI.vss`, `HPE-Synergy.vss` |

## Optional free generic icons (Flaticon)

https://www.flaticon.com/free-icons/network-diagram

Manual download only (hpe-networking-mcp does **not** scrape Flaticon). Keep the
required attribution under Flaticon terms, then place files as:

```text
resources/diagram_icons/private/vendors/generic/<role>.svg
# or HPE_MCP_DIAGRAM_ICON_DIR/vendors/generic/<role>.svg
```

## Juniper / Mist (public library — still local/gitignored)

Public packs from Juniper stay under `private/juniper-official/` (not committed):

```bash
uv run python scripts/install_diagram_icon_packs.py --juniper
# or included with --from-downloads
```

| Asset | Location | Used by |
|---|---|---|
| Visio icon ZIPs (EX/QFX/MX/SRX/SSR/…) | `private/juniper-official/zips` + `visio/` | Microsoft Visio |
| Mist AP + EX/QFX/SRX/MX/SSR product photos | `private/juniper-official/product-photos` | Graphviz roles |
| Role aliases | `private/vendors/juniper/*`, `private/vendors/mist/*` | `export_graphviz_topology` |

Sources:
- https://www.juniper.net/us/en/products/icons-and-stencils.html
- https://www.juniper.net/us/en/company/images/image-library-logos-and-product-photos.html

Follow Juniper brand terms; do not redistribute the downloaded binaries.

## Filename convention (PNG/SVG for Graphviz)

```text
private/vendors/<vendor>/<role>.svg|png
private/vendors/<vendor>/default.svg|png
vendors/<vendor>/<role>.svg|png   # optional non-private
generic/<role>.svg                # shipped fallbacks
```

Roles: `cloud`, `firewall`, `router`, `core_switch`, `agg_switch`,
`access_switch`, `gateway`, `campus_ap`, `mist_ap`, `clearpass`,
`controller`, `server`, `client`, `edgeconnect`, `generic`.
