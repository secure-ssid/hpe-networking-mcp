# hpe-networking-mcp examples

Tested, non-secret MCP client and prompt/runbook configuration examples.
Nothing under this directory is loaded automatically by any client -- copy
the file you need and edit the placeholders. Every absolute path shown is a
placeholder (`/path/to/hpe-networking-mcp`); replace it with your real clone
path, or use one of the `${workspaceFolder}`-relative configs below that
need no path edits at all.

See [`docs/mcp-client-recipes.md`](../docs/mcp-client-recipes.md) for the
full copy/paste walkthrough per client, and
[`docs/getting-started.md`](../docs/getting-started.md) for credentials and
install steps that come before any of these configs matter.

## Profile matrix

| Client | Transport | Profile | Example |
|---|---|---|---|
| Generic stdio | stdio | minimal (`central,glp,rag`) | [`mcp-clients/stdio/minimal.mcp.json`](mcp-clients/stdio/minimal.mcp.json) |
| Generic stdio | stdio | full (all optional products, read-only) | [`mcp-clients/stdio/full.mcp.json`](mcp-clients/stdio/full.mcp.json) |
| Generic HTTP | streamable HTTP | local loopback | [`mcp-clients/http/local.mcp.http.json`](mcp-clients/http/local.mcp.http.json) |
| Generic HTTP | streamable HTTP | non-loopback, bearer-protected | [`mcp-clients/http/bearer.mcp.http.json`](mcp-clients/http/bearer.mcp.http.json) + [`bearer.server.env`](mcp-clients/http/bearer.server.env) |
| Copilot CLI / app | stdio | minimal | [`mcp-clients/copilot-cli.mcp-config.json`](mcp-clients/copilot-cli.mcp-config.json) |
| Cursor | stdio | minimal (committed default) | [`../.cursor/mcp.json`](../.cursor/mcp.json) |
| Cursor | stdio | full debug (direct backend servers) | [`../.cursor/mcp.dev.json`](../.cursor/mcp.dev.json) |
| VS Code | stdio | minimal | [`../.vscode/mcp.json.example`](../.vscode/mcp.json.example) |
| Claude Desktop / Code | stdio | minimal + direct debug servers | [`../.claude/launch.json`](../.claude/launch.json) |

Cursor, VS Code, and Claude already ship their canonical example under the
repository root (`.cursor/`, `.vscode/`, `.claude/`) so this table links to
those instead of duplicating them -- duplicated copies would drift the next
time a profile changes. `tests/unit/test_example_configs.py` parses every
row in this table and fails if a linked file goes missing, uses an old
`aruba-*`/`centralmcp`/`CENTRALMCP_*` identifier, or does not match the
documented low-token router profile.

## Which stdio config should I copy?

- **minimal** — the recommended default. Client-visible tool list stays at
  three entries (`find_tool`, `invoke_read_tool`, `invoke_tool`) while the
  router still reaches `central`, `glp`, and `rag` plus the always-on
  credential-free `interop-core` backend.
- **full** — adds every optional product starter
  (`clearpass,mist,apstra,aos8,edgeconnect,uxi,axis,design`) read-only. Use
  this only when you actually need one of those platforms in the current
  session; it costs more router-reachable surface, though the client-visible
  tool count is unchanged (the router profile still gates what
  `find_tool`/`invoke_tool` can dispatch to).

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
        "HPE_MCP_ROUTER_MODE": "minimal",
        "HPE_MCP_TOOLSETS": "central,glp,rag"
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
