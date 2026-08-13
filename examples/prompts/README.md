# Prompt and runbook examples

Copy/paste scenarios for the low-token router profile
(`HPE_MCP_ROUTER_MODE=minimal`, `HPE_MCP_TOOLSETS=central,glp,rag`). Every
call shape below is `find_tool` -> `invoke_read_tool` / `invoke_tool`, never a
direct backend tool name -- that is the only supported dispatch path in
minimal mode. See [`docs/example-prompts.md`](../../docs/example-prompts.md)
for the complete, capability-labeled scenario set this file complements.

## 1. Credential-free discovery (no Central/GLP call)

```text
find_tool("ask Aruba docs with citations")
invoke_read_tool("ask_docs", {"question": "WPA3 SAE transition mode", "top_k": 5})
```

Reaches only the local RAG index (`rag-core`); safe to run before any
credentials are configured.

## 2. Read-only investigation

```text
find_tool("show critical alerts")
invoke_read_tool("list_active_alerts", {"severity": "CRITICAL", "limit": 20})
```

`invoke_read_tool` refuses any backend tool that is not annotated read-only,
so this call shape can never reach a write/destructive tool by mistake.

## 3. Dry-run before a write

```text
find_tool("build an underlay SSID")
invoke_tool("build_underlay_ssid", {
  "scope_id": "GLOBAL_SCOPE_ID",
  "persona": "CAMPUS_AP",
  "ssid_name": "example-ssid",
  "opmode": "WPA3_SAE",
  "passphrase": "REPLACE_ME",
  "vlan_id": 100,
  "dry_run": true
})
```

`build_underlay_ssid` is annotated as a write tool (not read-only), so even a
`dry_run: true` preview must go through `invoke_tool`, not
`invoke_read_tool` -- the router gates on the tool's declared safety
classification, not the runtime arguments.

Always confirm the printed payload with the operator before re-running the
same call with `invoke_tool` and `dry_run: false`.

## 4. Browse and load a bundled runbook (skill)

Skills are multi-step operator runbooks bundled with `rag-core` and reachable
through the router without knowing the exact skill name up front:

```text
find_tool("browse bundled runbooks")
invoke_read_tool("list_skills", {"platform": "central", "tag": "change"})
invoke_read_tool("load_skill", {"name": "change-pre-check"})
```

`load_skill` returns the full runbook body (objective, prerequisites,
numbered procedure, and expected outcomes) for the AI client to execute
turn-by-turn. See
[`src/hpe_networking_mcp/mcp_servers/skills/change-pre-check.md`](../../src/hpe_networking_mcp/mcp_servers/skills/change-pre-check.md)
for the underlying runbook, and
[`src/hpe_networking_mcp/mcp_servers/skills/`](../../src/hpe_networking_mcp/mcp_servers/skills/)
for the full bundled set (infrastructure health check, AOS8 migration
readiness, change pre/post-check, SSID review, client connectivity,
GreenLake device onboarding, and network design diagrams).

## 5. Confirmation-gated destructive change

```text
find_tool("AOS8 apply migration run")
invoke_tool("aos8_apply_migration_run", {"run_id": "RUN_ID", "dry_run": false, "confirm": true})
```

Some destructive ops tools (`reboot_device`, `port_bounce`, `poe_bounce`,
`disconnect_client`) have no `confirm` argument at all -- they confirm
interactively through MCP elicitation on the client instead. Check the
tool's schema before assuming either shape.

## 6. GreenLake Reporting / Service Catalog read

```text
find_tool("GreenLake service catalog offers")
invoke_read_tool("list_glp_service_offers", {"limit": 20})
```

GLP Reporting and Service Catalog reads stay in the default read-only
profile; no `HPE_MCP_PRODUCT_ACCESS` override is required for reads.
