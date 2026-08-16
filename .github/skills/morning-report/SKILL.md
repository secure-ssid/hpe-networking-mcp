---
name: morning-report
description: >
  Produce a last-24h networking operations digest (Central, and Mist/UXI/GLP
  when enabled). Use when the user asks for a morning report, overnight
  rundown, daily standup network summary, executive briefing, or "what broke
  overnight". Prefer the bundled MCP runbook over free-form tool guessing.
---

# morning-report skill

Use the `hpe-networking-mcp` router. Do not invent live estate facts.

## Workflow

1. `find_tool` → browse runbooks if needed, then:
2. `invoke_read_tool("load_skill", {"name": "morning-report"})`
3. Follow that runbook with `find_tool` + `invoke_read_tool` only.
4. Choose engineer vs executive tone from the user phrasing (see runbook).

## Rules

- Read-only: no alert clear, reboot, or config write.
- Skip disabled optional products with a one-line note.
- Lead with GREEN / YELLOW / RED status.
