---
name: network-docs
description: >
  Answer questions about HPE Aruba and Juniper Mist networking using the
  local RAG index (hpe-networking-mcp). Use this skill when the user asks
  how-to or concept questions about Aruba Central, Mist, ClearPass, AOS-CX,
  EX-series switches, APs, gateways, NAC, VLANs, SSIDs, firmware, or any
  Aruba/Mist/Juniper networking topic. Also use it when the user types
  /network-docs, /rag, or /hpe-docs before their question.
---

# network-docs skill

You have access to a local RAG index via the `hpe-networking-mcp` MCP server.
Always use MCP tools to answer — never guess from model memory alone.

## Tool selection

Use the router pattern: `find_tool` first, then `invoke_read_tool`.

| Question type | Tool to use | When |
|---|---|---|
| How-to, config, concepts, design | `ask_docs` | "how do I configure X", "what is Y", "design guidance for Z" |
| Exact API endpoint / field / enum | `lookup_api` | "what endpoint creates X", "what values does field Y accept" |
| Broader keyword search, want raw chunks | `search_docs` | exploratory, multiple results wanted |
| Security advisory / CVE | `lookup_advisory` | CVE IDs, advisory IDs, product vulnerability lookup |

## Source filters for `ask_docs` / `search_docs`

Narrow with `source=` when the topic is clearly vendor-specific:

| Topic | source value |
|---|---|
| Aruba Central UI / config | `techdocs_html` |
| Central developer / API guides | `developer_docs` |
| Mist config, onboarding, JVDs | `mist_docs` |
| NAC / ClearPass | `nac_docs` |
| AOS10 design docs (Wi-Fi, location services, private 5G, AOS operations) | `aos_techdocs` |
| AOS-CX switch release notes (full history, 10.13.xx+) | `aoscx_release_notes` |
| AOS-CX Fundamentals & CLI Reference guides (per switch series) | `aoscx_guides` |
| ClearPass Policy Manager admin guide (standalone CPPM, not Central NAC) | `clearpass_guide` |
| Mist cloud platform release notes (full history, 2017+) | `mist_product_updates` |
| Juniper EX-series hardware install/maintenance guide | `junos_ex_hardware` |
| Junos OS release notes (EX-series-specific only) | `junos_ex_release_notes` |
| VSG / DC design | `vsg_docs` |
| Security advisories | `security_advisories` or `juniper_security_advisories` |
| Lifecycle notices | `lifecycle_notices` or `juniper_lifecycle` |
| Juniper KB | `juniper_kb` |
| AOS-CX / AOS10 feature-by-release support | `feature_navigator` |
| Juniper EX-series switch / Mist AP hardware specs | `product_datasheets` |

Omit `source=` when unsure — the hybrid search will find the best match.

## What IS in the RAG

- **Aruba Central API**: ~4,200 endpoints (all Aruba Central REST API)
- **Mist API**: ~1,050 endpoints (full Mist REST API spec)
- **Mist docs**: config guides, onboarding, Juniper Validated Designs
- **Central techdocs & developer docs**: UI config, API guides
- **Juniper KB**: troubleshooting articles
- **NAC / ClearPass docs**
- **VSG / DC design guides**
- **Security advisories & lifecycle notices** (Aruba + Juniper)
- **AOS-CX feature/release support matrix**: per-platform Yes/No feature
  support at the latest AOS-CX release (20 of 25 switch platforms — the 5
  skipped are legacy ProVision/AOS-S switches, not real AOS-CX)
- **AOS10 feature/release support matrix**: AP/gateway feature support
  across all published AOS10 releases (shows which release added a feature)
- **AOS-CX switch release notes**: FULL history back to AOS-CX 10.13.xx —
  Overview, Compatibility, Products Supported, Upgrade info, Certifications,
  License, Version history, Enhancements, Resolved Issues, Feature Caveats,
  and Known Issues for every patch of every major-version group, across all
  14 published switch series (4100i, 5420, 6000/6100/6200/6300/6300L/6400,
  8100/8320/8325/8360/8400, 9300/9300S/10040, 10000) — roughly 10,400 pages
  total (~800-930 per long-lived series, ~290 for the two newest series).
- **AOS-CX Fundamentals & CLI Reference guides** (`aoscx_guides`): the
  switch OS's own admin/config books and full CLI command syntax reference
  (distinct from the release-notes changelog) — currently 2 Fundamentals
  Guides (4100i/6000/6100; 5420/6200) and 6 CLI Reference guides (6000/6100,
  6200, 6300/6400, 8100/8360, 8400, 10000), ~14,600 pages total. Not yet
  exhaustive across every switch-series generation — see the manifest note
  for known gaps.
- **ClearPass Policy Manager admin guide** (`clearpass_guide`): the
  standalone on-prem CPPM Online Help (current 6.14 release), ~1,240 pages —
  distinct from `nac_docs`, which covers Aruba Central's own cloud-hosted
  NAC/MAC-registration/MPSK features, not the ClearPass product itself.
- **Mist cloud platform release notes** (`mist_product_updates`): FULL
  history since July 2017 — one page per dated release covering Marvis,
  Access/Wireless/Wired/WAN Assurance, Location Services, and Premium
  Analytics feature changes (186 pages). This is the Mist-cloud-platform
  counterpart to AOS-CX release notes; `mist_docs` remains the source for
  evergreen howto/concept documentation, not dated feature announcements.
- **Juniper EX-series hardware guide** (`junos_ex_hardware`): the physical
  install/maintenance book per EX chassis family (site guidelines, cabling,
  power, safety, chassis component replacement) plus first-boot "configure
  Junos OS" pages, ~720 pages across every EX family (2300, 3400, 4000,
  4100(-f/-h), 4300, 4400, 4600, 4650, 9204/9208/9214/9251/9253). Deeper
  feature/CLI configuration guidance lives in the general Junos software
  config guides, a much larger corpus deliberately out of scope (see below).
- **Junos OS release notes, EX-series only** (`junos_ex_release_notes`):
  per-version new-features/resolved-issues/open-issues/what-changed pages
  specifically tagged for the EX platform, ~193 pages. The full Junos
  release-notes tree covers every platform (ACX/MX/NFX/PTX/QFX/SRX/SSR/JRR)
  at ~6,600 pages; only the EX-tagged subset is ingested — see "What is NOT
  in the RAG" below.
- **Juniper hardware datasheets**: EX-series switch specs (EX2300-EX9250)
  and Mist-line AP datasheets (AP21-AP66) — official juniper.net spec pages

## What is NOT in the RAG (fall back to web search or `lookup_hardware_specs`)

- Aruba/HPE-branded hardware datasheets (CX switch, Aruba AP exact specs) —
  not yet scraped. Note: arubanetworks.com and arubanetworking.hpe.com sit
  behind Akamai and return 403 to plain HTTP *and* headless-Chromium
  requests, but a non-headless Playwright browser with a realistic Chrome
  UA does get through — that is how aos_techdocs and aoscx_release_notes
  were built. support.hpe.com's docDisplay pages are not Akamai-blocked;
  they just require JS execution to pass an anonymous-session check before
  showing content (any real browser context should do). So this gap is
  "not yet scraped", not "unreachable". Until then, use the
  `lookup_hardware_specs` tool / `hardware_specs` catalog instead — it is a
  structured, hand-verified spec reference for CX and Aruba AP models and is
  already wired into `ask_docs` (a query naming a specific hardware model
  routes there automatically, before RAG).
- Real-time device state — use monitoring MCP tools instead
- Junos OS release notes and general software config guides for platforms
  other than EX (ACX/MX/NFX/PTX/QFX/SRX/SSR/JRR, and vSRX/cSRX) — out of
  scope for a switch/AP-focused project; only the EX-tagged release-notes
  subset (`junos_ex_release_notes`) is ingested. Junos' general (non-EX,
  non-release-notes) software configuration/feature guide corpus is also
  not ingested — at ~24,700 pages it is far larger than the EX hardware
  guide, and deeper EX-series CLI/feature configuration beyond
  `junos_ex_hardware`'s first-boot pages would have to come from there.
- Platform-generic Junos release-notes pages that are not EX-tagged by
  filename (e.g. software-installation-and-upgrade, jweb, system-management,
  evpn, pki — ~3,200 pages) — may or may not apply to EX-series switches but
  aren't filterable by filename alone; a candidate for later reconsideration.
- Juniper documentation outside of Mist, KB articles, EX hardware/release
  notes, and EX/AP datasheets

## Workflow

1. Call `find_tool` with a description of what you need.
2. Call `invoke_read_tool` with the tool name and arguments.
3. Cite the `file_path` from the result before giving config advice.
4. If RAG returns nothing useful, say so and fall back to a web search.

## Example invocations

```
invoke_read_tool(
  name="ask_docs",
  arguments={"question": "how do I configure WPA3-SAE SSID in Aruba Central", "source": "techdocs_html"}
)

invoke_read_tool(
  name="lookup_api",
  arguments={"query": "POST endpoint to create WLAN on Mist site"}
)

invoke_read_tool(
  name="ask_docs",
  arguments={"question": "EX4400 virtual chassis stacking configuration", "source": "mist_docs"}
)
```
