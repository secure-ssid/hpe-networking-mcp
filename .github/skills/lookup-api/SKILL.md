---
name: lookup-api
description: >
  Look up exact Aruba Central or Mist API endpoints, schemas, field names,
  and enum values from the local OpenAPI index. Use this when the user asks
  about a specific API endpoint, what HTTP method to use, what fields a
  request body has, or what values an enum accepts. Trigger on: /lookup-api,
  "what endpoint", "what method", "API for X", "schema for Y", "enum values".
---

# lookup-api skill

You have access to a parsed OpenAPI index (SQLite) via `hpe-networking-mcp`.
Use `lookup_api` for precise, authoritative API answers. Fall back to
`search_docs` only when `lookup_api` returns empty results.

## Workflow

1. Call `invoke_read_tool(name="lookup_api", arguments={"query": "..."})`.
2. If results are empty, call `invoke_read_tool(name="search_docs", arguments={"query": "..."})`.
3. Always include the `file_path` / operationId in your answer so the user
   can find the spec entry.

## Coverage

- **Aruba Central**: System, Security, Routing, Network Services, Interfaces,
  Monitoring, VLANs, Wireless, NAC, Troubleshooting, Telemetry, and more
  (~3,165 endpoints across 31 spec categories)
- **Mist**: Full Mist REST API (~1,050 endpoints)

## Example invocations

```
invoke_read_tool(
  name="lookup_api",
  arguments={"query": "DELETE endpoint to remove a WLAN from Mist site"}
)

invoke_read_tool(
  name="lookup_api",
  arguments={"query": "auth_type enum values for Central SSID profile"}
)

invoke_read_tool(
  name="lookup_api",
  arguments={"query": "GET /network-monitoring/v1/aps/{serial}"}
)
```
