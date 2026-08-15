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
| AOS-CX techdocs | `aos_techdocs` |
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
- **Juniper hardware datasheets**: EX-series switch specs (EX2300-EX9250)
  and Mist-line AP datasheets (AP21-AP66) — official juniper.net spec pages

## What is NOT in the RAG (fall back to web search or `lookup_hardware_specs`)

- Aruba/HPE-branded hardware datasheets (CX switch, Aruba AP exact specs) —
  arubanetworks.com, arubanetworking.hpe.com, and www.hpe.com are all
  network/WAF-blocked from this MCP's ingestion environment (confirmed via
  plain HTTP and a real headless-browser request). Use the
  `lookup_hardware_specs` tool / `hardware_specs` catalog instead — it is a
  structured, hand-verified spec reference for CX and Aruba AP models and is
  already wired into `ask_docs` (a query naming a specific hardware model
  routes there automatically, before RAG).
- Real-time device state — use monitoring MCP tools instead
- Juniper documentation outside of Mist, KB articles, and EX/AP datasheets

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
