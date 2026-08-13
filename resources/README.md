# API Reference — Postman Collections

The Postman collections are not committed to this repo (too large for GitHub).
They are supporting references rather than the authoritative exact lookup
source used by the MCP.

## Download

Import directly into Postman from the official HPE Aruba Networking workspace:

**[HPE Aruba Networking Central — Postman Collection](https://www.postman.com/hpe-aruba-networking/new-hpe-aruba-networking-central/collection/32717089-1d8b9f9e-2137-4a7d-b735-1b3c06f87e70)**

Or run the download script to pull both collections locally:

```bash
python resources/download_collections.py
```

The script saves them to `resources/` (git-ignored).

## Contents

| Collection | Purpose |
|---|---|
| `MRT APIs.postman_collection.json` | Monitoring, health, trends, troubleshooting endpoints |
| `Configuration APIs.postman_collection.json` | SSID, VLAN, profile, firmware config endpoints |

## Current OpenAPI sources

`lookup_api` is built from git-ignored files under
`ingestion/sources/openapi_specs/`:

- Aruba reference pages resolved through their July 2026 `oasPublicUrl`
  pointers and the ReadMe API registry.
- The pinned official `mistsys/mist_openapi` 2606.1.1 snapshot.

Refresh and validate those sources with:

```bash
uv run python ingestion/scrape_openapi.py
uv run python ingestion/scrape_cnac_spec.py
uv run python ingestion/fetch_mist_openapi.py
uv run python scripts/check_openapi_drift.py
uv run python scripts/check_mist_openapi_drift.py
```

The current exact index contains 239 specs, 3,465 endpoints, 10,297 schemas,
and 57,131 fields.
