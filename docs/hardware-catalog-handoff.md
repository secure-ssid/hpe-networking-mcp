# Hardware catalog handoff

## What is ready

`catalog-core` provides the low-token `search_hardware_catalog` MCP tool for
HPE Aruba Networking and HPE Juniper hardware. It searches a local SQLite
index—no RAG embedding, vendor API call, or network access at query time.

- Exact SKU/part-number aliases are case- and punctuation-insensitive.
- Model/configuration queries return no more than five deterministic candidates.
- Responses are compact by default; pass `include_specs: true` for normalized
  details.
- Each result carries the official source URL, snapshot state, and lifecycle
  status when present.
- `ask_docs` sends SKU/configuration-selection requests here, while broad
  hardware-specification questions continue through the existing docs route.

## Use it

The Docker image builds the catalog automatically. For local development,
build it once after checkout or after updating the seed:

```bash
uv run python scripts/build_hardware_catalog.py
```

Examples:

```json
{"query": "CX 6300 PoE 48 port"}
{"query": "EX4400-48P", "include_specs": true}
{"query": "48 port PoE switch", "vendor": "juniper"}
```

In the recommended minimal router profile, discover it with `find_tool` and
call it through `invoke_read_tool`. Default/direct router modes also expose
the convenience wrapper directly.

## Current boundary

The committed official-source seed is intentionally **partial**: 13 products
and 28 aliases across selected Aruba CX, Juniper EX4400/EX4100, AP47, and Mist
Edge examples. Every response declares that coverage state. It is usable now for
fast SKU selection but must not be presented as the complete, current catalog.

The source policy is defined in
[`ingestion/hardware_catalog_manifest.json`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/ingestion/hardware_catalog_manifest.json):
use official Aruba/HPE/Juniper sources only; retain a last verified snapshot
and mark it stale if a source becomes unavailable—never substitute a reseller.

## Where to continue

1. Expand [`ingestion/hardware_catalog_seed.json`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/ingestion/hardware_catalog_seed.json)
   by official product family, starting with the remaining CX 6300/6200 SKUs,
   then switches, APs, gateways/routers, optics, power, and accessories.
2. Keep `coverage` as `partial` until the intended product families have a
   verified completeness policy.
3. Add `lifecycle_status` and lifecycle evidence where the official vendor
   source publishes it.
4. Rebuild the SQLite index and add a regression test for each new family or
   ambiguous model phrase.

## Relevant implementation

- `src/hpe_networking_mcp/pipeline/clients/hardware_catalog.py` — validation,
  SQLite/FTS build, ranking, response shape, and `ask_docs` classification.
- `src/hpe_networking_mcp/mcp_servers/catalog.py` — standalone MCP tool.
- `src/hpe_networking_mcp/mcp_servers/tool_router.py` — always-on backend and
  direct wrapper.
- `scripts/build_hardware_catalog.py` — reproducible local build.
- `tests/unit/test_hardware_catalog.py` — exact match, ranking, safety, and
  registration coverage.

## Verification already completed

```bash
uv run ruff check src/hpe_networking_mcp/pipeline/clients/hardware_catalog.py \
  src/hpe_networking_mcp/mcp_servers/catalog.py \
  src/hpe_networking_mcp/mcp_servers/rag.py \
  src/hpe_networking_mcp/mcp_servers/tool_router.py \
  scripts/build_hardware_catalog.py tests/unit/test_hardware_catalog.py

uv run pytest tests/unit/test_hardware_catalog.py \
  tests/unit/test_rag_ask_docs.py \
  tests/unit/test_tool_router_backends.py \
  tests/unit/test_write_gate_enforcement.py -q
```
