---
name: operator-runbooks
description: >
  Discover and load hpe-networking-mcp operator runbooks (skills) for network
  ops workflows: morning report, Central scope resolve/audit, SSID review,
  change pre/post check, WLAN Central↔Mist sync, RF site check, ClearPass
  policy audit, Mist audit, UXI diagnostics, AOS8 migration readiness, GLP
  onboarding. Use when the user wants a guided multi-step ops procedure.
---

# operator-runbooks skill

Bundled runbooks live on `rag-core` and are reached through the router.

## Browse

```text
invoke_read_tool("list_skills", {"detail": false})
invoke_read_tool("list_skills", {"platform": "central", "tag": "audit"})
invoke_read_tool("list_skills", {"platform": "mist"})
```

`list_skills` returns **metadata only**. Always `load_skill` before executing.

## Load

```text
invoke_read_tool("load_skill", {"name": "morning-report"})
invoke_read_tool("load_skill", {"name": "central-scope-walker"})
invoke_read_tool("load_skill", {"name": "central-scope-audit"})
invoke_read_tool("load_skill", {"name": "wlan-sync-validation"})
invoke_read_tool("load_skill", {"name": "cross-platform-rf-check"})
invoke_read_tool("load_skill", {"name": "clearpass-policy-audit"})
invoke_read_tool("load_skill", {"name": "mist-scope-audit"})
invoke_read_tool("load_skill", {"name": "uxi-diagnostics"})
invoke_read_tool("load_skill", {"name": "aos8-migration-readiness"})
```

## Execution rules

1. Router pattern: `find_tool` → `invoke_read_tool` (reads) / `invoke_tool` (writes).
2. Honor each runbook's read-only vs gated-write sections.
3. If a product backend is missing, say so — do not hallucinate tool results.
4. Optional products need `HPE_MCP_PRODUCTS` (clearpass, mist, aos8, uxi, …).
