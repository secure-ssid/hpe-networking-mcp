# hpe-networking-mcp examples

Tested, non-secret, safe-read-only MCP client and prompt/runbook configuration
examples.
Nothing under this directory is loaded automatically by any client -- copy
the file you need and edit the placeholders. Every absolute path shown is a
placeholder (`/path/to/hpe-networking-mcp`); replace it with your real clone
path, or use one of the `${workspaceFolder}`-relative configs below that
need no path edits at all.

See [`docs/mcp-client-recipes.md`](../docs/mcp-client-recipes.md) for the
full copy/paste walkthrough per client, and
[`docs/getting-started.md`](../docs/getting-started.md) for credentials and
install steps that come before any of these configs matter.

**Recommended:** VS Code + GitHub Copilot is the primary host path. Use
Copilot CLI when already subscribed, Crush for user-selected providers or
local Ollama, MCPJam for local trace/debug and cross-client validation, and
LibreChat/Open WebUI only for a persistent multi-provider browser platform.
This repository remains focused on the router, setup/doctor, configs, safety,
and docs; it does not include a custom GUI.

## Profile matrix

| Client | Transport | Profile | Example |
|---|---|---|---|
| VS Code + GitHub Copilot | stdio | **primary** safe-read-only minimal | [`../.vscode/mcp.json.example`](../.vscode/mcp.json.example) |
| GitHub Copilot CLI / app | stdio | safe-read-only minimal (when subscribed) | [`../.github/mcp.json`](../.github/mcp.json) |
| Generic stdio | stdio | minimal (`central,glp,rag`) | [`mcp-clients/stdio/minimal.mcp.json`](mcp-clients/stdio/minimal.mcp.json) |
| Generic stdio | stdio | full (all products, safe read-only) | [`mcp-clients/stdio/full.mcp.json`](mcp-clients/stdio/full.mcp.json) |
| Generic stdio | stdio | full read/write (all products, guarded writes enabled) | [`mcp-clients/stdio/full-read-write.mcp.json`](mcp-clients/stdio/full-read-write.mcp.json) |
| Generic HTTP | streamable HTTP | local loopback | [`mcp-clients/http/local.mcp.http.json`](mcp-clients/http/local.mcp.http.json) |
| Generic HTTP | streamable HTTP | non-loopback, bearer-protected | [`mcp-clients/http/bearer.mcp.http.json`](mcp-clients/http/bearer.mcp.http.json) + [`bearer.server.env`](mcp-clients/http/bearer.server.env) |
| GitHub Copilot CLI / app | stdio | portable placeholder | [`mcp-clients/copilot-cli.mcp-config.json`](mcp-clients/copilot-cli.mcp-config.json) |
| Cursor | stdio | minimal (committed default) | [`../.cursor/mcp.json`](../.cursor/mcp.json) |
| Cursor | stdio | full debug (direct backend servers) | [`../.cursor/mcp.dev.json`](../.cursor/mcp.dev.json) |
| Claude Code | stdio | project `.mcp.json` shape | [`mcp-clients/claude-code.mcp.json`](mcp-clients/claude-code.mcp.json) |
| Claude Code | stdio | optional launch/debug profiles | [`../.claude/launch.json`](../.claude/launch.json) |

Cursor, VS Code, GitHub Copilot, and Claude ship canonical host examples
under the repository root or this directory. `mcpServers` is used by generic
clients, GitHub Copilot, and Claude Code; VS Code intentionally uses the
different top-level `servers` shape. JSON files contain no comment keys so
they can be copied directly into a host config. `tests/unit/test_example_configs.py`
parses every row in this table and fails if a linked file goes missing, uses
an old `aruba-*`/`centralmcp`/`CENTRALMCP_*` identifier, or drifts from the
documented safe-read-only router profile.

## Which stdio config should I copy?

- **minimal** — the recommended safe default. Client-visible tool list stays at
  three entries (`find_tool`, `invoke_read_tool`, `invoke_tool`) while the
  router still reaches `central`, `glp`, and `rag` plus the always-on
  credential-free `interop-core` backend. The shipped host configs set
  `HPE_MCP_ACCESS_PROFILE=safe-read-only`, `HPE_MCP_READONLY=1`, and
  `HPE_MCP_PRODUCT_ACCESS=read-only`.
- **full** — adds every optional product starter
  (`clearpass,mist,apstra,aos8,edgeconnect,uxi,axis,design`) under
  `safe-read-only`. Use this only when you actually need one of those
  platforms in the current session.
- **full-read-write** — loads the same full backend set with ordinary platform
  writes enabled. Dry-run, confirmation, elicitation, and dedicated destructive
  gates remain active. The client-visible tool count is still three because
  both examples keep `HPE_MCP_ROUTER_MODE=minimal`.

## Which HTTP config should I copy?

- **local** — your MCP client connects to an already-running router on
  `127.0.0.1`. Start it first: `MCP_PORT=8010 bash scripts/run_http_router.sh`.
- **bearer** — a non-loopback deployment. The server refuses to start
  without both `MCP_ALLOWED_HOSTS` and `MCP_ALLOWED_ORIGINS` set (see
  `bearer.server.env`), and the client must send the matching
  `Authorization` header shown in `bearer.mcp.http.json`.

## cwd, PYTHONPATH, and credentials

Every stdio example sets three things explicitly so the router process can
find its own package and your credentials regardless of what directory the
MCP client happens to launch it from:

- `cwd` — the repository root. Relative paths the router reads at runtime
  (for example the default `config/credentials.yaml`) resolve from here.
- `PYTHONPATH` — the `src/` directory, so `hpe_networking_mcp.mcp_servers.*`
  imports resolve when the server is launched as a direct script path
  (`.../src/hpe_networking_mcp/mcp_servers/tool_router.py`) rather than via
  the installed `hpe-mcp-router` console script.
- `CREDS_PATH` — an explicit path to `config/credentials.yaml`, so the
  router does not depend on an ambient working directory to find
  credentials. Point this at any file you like, including a legacy
  `centralmcp` checkout's credentials file during a migration -- see
  [`../MIGRATION.md`](../MIGRATION.md#external-credentials-via-creds_path).

If you installed the package (`uv pip install .` / `pip install
hpe-networking-mcp`), prefer the installed console scripts over the direct
script path:

```json
{
  "mcpServers": {
    "hpe-networking-mcp": {
      "command": "hpe-mcp-router",
      "env": {
        "CREDS_PATH": "/path/to/hpe-networking-mcp/config/credentials.yaml",
        "HPE_MCP_ACCESS_PROFILE": "safe-read-only",
        "HPE_MCP_READONLY": "1",
        "HPE_MCP_ROUTER_MODE": "minimal",
        "HPE_MCP_TOOLSETS": "central,glp,rag",
        "HPE_MCP_PRODUCT_ACCESS": "read-only",
        "HPE_MCP_CENTRAL_WRITES": "0",
        "HPE_MCP_GLP_V2BETA1_WRITES": "0"
      }
    }
  }
}
```

With an installed console script, `cwd` and `PYTHONPATH` are unnecessary --
the package is already importable wherever Python is installed. Keep
`CREDS_PATH` explicit either way.

## Prompt and runbook examples

[`prompts/README.md`](prompts/README.md) has copy/paste `find_tool` /
`invoke_read_tool` / `invoke_tool` scenarios, including browsing and loading
a bundled skill (multi-step runbook). See
[`docs/example-prompts.md`](../docs/example-prompts.md) for the complete,
capability-labeled scenario set.

## Optional existing frontends

This repository does not add a custom web app. Existing frontends that can
consume the same safe-read-only router include:

- [Crush](https://github.com/charmbracelet/crush) — terminal frontend with
  `stdio`, HTTP, and SSE MCP support; useful for selected providers or local
  Ollama; configure it through `.crushrc`.
- [MCPJam Inspector](https://github.com/MCPJam/inspector) — local
  STDIO/HTTP debugging and evaluation frontend; hosted mode requires HTTPS
  and cannot launch a local STDIO process.
- [LibreChat](https://github.com/danny-avila/LibreChat) — self-hosted,
  persistent multi-provider browser platform configured through
  `librechat.yaml`; use Streamable HTTP for a running router.
- [Open WebUI](https://github.com/open-webui/open-webui) — persistent
  multi-provider browser platform with native MCP Streamable HTTP integration
  for v0.6.31+, admin-configured; use
  [mcpo](https://github.com/open-webui/mcpo) only when an OpenAPI bridge is
  needed.

These hosts own model selection, user access, and approval behavior. Keep the
router on `safe-read-only`, use a protected HTTP endpoint for remote/container
frontends, and never commit credentials or bearer tokens.
