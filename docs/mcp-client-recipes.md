---
title: "MCP client recipes"
nav_order: 3
---

# MCP client recipes

This page assumes the low-token router profile from
[getting-started.md](getting-started.md):

```env
HPE_MCP_ROUTER_MODE=minimal
HPE_MCP_TOOLSETS=central,glp,rag
```

This exposes only `find_tool`, `invoke_read_tool`, and `invoke_tool` in
minimal mode while still letting the router reach the backend catalog on
demand. The complete index can contain 6,728 backend tools, while minimal
mode keeps only three discovery/dispatch tools in client context.
The remaining profiles in that file are direct debug servers
(`central-monitoring`, `central-config`, `central-ops`, `central-nac`,
`central-streaming`, `glp-core`)
plus two CLI launch entries for the migration pipeline and SSID builder.
On first use, Claude Code asks you to approve project MCP servers; a trusted
workspace or an explicit `claude mcp add`/`claude mcp list` workflow is
required. `.claude/launch.json` remains an optional launch/debug profile, not
the only Claude integration path. Its router profiles are also safe
read-only; direct backend and CLI entries are for targeted debugging.
See the [Claude Code MCP reference](https://code.claude.com/docs/en/mcp) for
current approval and transport behavior.

### GitHub Copilot CLI and app

For Copilot CLI's project-level repository configuration, use
[`.github/mcp.json`](../.github/mcp.json). It uses the `mcpServers` shape,
relative `src`/`config` paths, and `type: "stdio"` so the same server
definition is portable across Copilot CLI and other MCP hosts:

```bash
copilot mcp list
```

Copilot CLI also discovers a root `.mcp.json` (copy the generic or Claude
example) and user configuration at `~/.copilot/mcp-config.json`. Project
configs require folder trust; prompt mode skips an untrusted project config
unless `GITHUB_COPILOT_PROMPT_MODE_WORKSPACE_MCP=true` is set. Copilot CLI
does not read VS Code's `.vscode/mcp.json` because that file uses the
host-specific top-level `servers` key. The built-in GitHub MCP server is
already available in Copilot CLI, so this repository config adds only the
HPE server. The GitHub Copilot app can use servers configured for repositories
or Copilot CLI and also has its own MCP settings; Copilot cloud agent and code
review repository configuration is entered in GitHub repository settings,
not this local file. Those cloud surfaces run tools autonomously without
approval, currently support tools rather than MCP resources/prompts, and do
not support remote OAuth MCP servers, so keep their server configuration
separate and allowlist read-only tools. See the [Copilot app
settings](https://docs.github.com/en/copilot/how-tos/github-copilot-app/customize-github-copilot-app)
and [repository MCP
settings](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/configure-mcp-servers)
for those host-specific paths.

Copilot CLI's `/mcp add` and `copilot mcp add` commands can create equivalent
entries, and the host controls model selection, permissions, trust prompts,
and tool approvals; an MCP JSON file cannot select or upgrade the model. See
the [GitHub Copilot CLI MCP
documentation](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers)
for current registry, trust, and configuration precedence.
The shipped canonical host router examples also set the aggregate
`safe-read-only` profile; model choice, agent mode, tool approval, and
workspace trust remain owned by the host application.

By the end of this page you will have picked the right transport for your
client, copied an accurate config for it, started the router if needed, and
run one MCP call to confirm the connection works.

## Recommended host path

1. **Primary:** use VS Code with GitHub Copilot and the workspace
   `.vscode/mcp.json` example. This is the best-supported editor workflow for
   this repository.
2. **If already subscribed:** use GitHub Copilot CLI with `.github/mcp.json`
   or a root `.mcp.json`; do not add a second frontend just to select a model.
3. **Optional terminal alternative:** use Crush when you want to choose among
   providers yourself or run a local Ollama model.
4. **Local validation:** use MCPJam Inspector for traces, protocol debugging,
   and cross-client checks rather than as the production chat host.
5. **Browser platforms:** choose LibreChat or Open WebUI only when you need a
   persistent, multi-provider browser experience. They are optional existing
   frontends, not a reason to add a GUI to this repository.

The repository stays focused on the router, setup/doctor workflow, portable
configs, safety gates, and documentation. Model selection remains a host
responsibility.

## Guided network diagrams

For a request such as “build a network diagram,” use the design backend through
the router rather than guessing an export tool:

```text
find_tool("guided network diagram with vendor icons")
invoke_read_tool("list_diagram_roles_and_vendors", {})
invoke_read_tool("list_diagram_icons", {})
```

The guided workflow asks only for missing choices:

1. diagram purpose: logical, physical, wireless, rack/device, or
   troubleshooting;
2. topology source: live Central inventory, supplied topology, or hand-built
   design;
3. site/group scope when live data is requested;
4. output: editable Draw.io (default), Graphviz, or NeXt UI;
5. generic role icons or vendor icons, plus Aruba/HPE, Juniper/Mist,
   ClearPass, mixed-vendor, or generic branding;
6. detail level, labels, links, layout, notes, and filename.

Live topology is input to the model, not the diagram itself. The workflow
labels the result as live or illustrative, validates it, previews the nodes,
links, icon source, and output path, then exports under the diagram artifact
boundary. If the design backend is disabled, enable the `design` product
before retrying; do not silently substitute a fabricated live diagram.

## Choose a transport: stdio vs streamable HTTP

<figure class="docs-figure">
  <img src="assets/diagrams/client-transport-choice.svg"
       alt="Decision tree for choosing stdio, local streamable HTTP, or protected non-loopback HTTP.">
  <figcaption>If your client launches the MCP server process itself, use
  stdio and <code>.mcp.json</code>. If your client connects to a server
  that is already running, use streamable HTTP and
  <code>.mcp.http.json</code> — pointed at a loopback listener on the same
  machine, or at a non-loopback listener that is protected with host/origin
  allow-lists and a bearer token.</figcaption>
</figure>

<div class="docs-compact-table" markdown="1">

| Style | Use when | Config |
|---|---|---|
| stdio | Your client launches the MCP server process | `.mcp.json.example`, `.github/mcp.json`, `.cursor/mcp.json`, `.vscode/mcp.json.example`, Claude Code `.mcp.json` |
| streamable HTTP | Your client connects to an already-running local MCP server | `.mcp.http.json.example` + `scripts/run_http_router.sh` |

</div>

## Profile matrix

The root-level committed configs above are all the **minimal safe-read-only**
router profile (`HPE_MCP_ROUTER_MODE=minimal`,
`HPE_MCP_TOOLSETS=central,glp,rag`, `HPE_MCP_ACCESS_PROFILE=safe-read-only`,
`HPE_MCP_READONLY=1`, and `HPE_MCP_PRODUCT_ACCESS=read-only`; no optional
products) -- the recommended default for every client. For the **full
safe-read-only** profile, the separate **full read/write** profile, a
non-loopback **bearer-protected HTTP** profile, and a Copilot CLI/app
example, see the tested configs and full client/transport/profile matrix in
[`examples/README.md`](../examples/README.md). `cwd`/`PYTHONPATH`/
`CREDS_PATH` behavior and the package-installed (`hpe-mcp-router`)
alternative to the direct script path are also documented there.

<div class="docs-callout docs-callout--info" markdown="1">

Any MCP-capable AI client/model can connect over streamable HTTP if the
client supports remote MCP servers — this is not limited to the clients
listed below.

</div>

## Copy/paste client configs

<div class="audience-grid">
  <div class="audience-card" markdown="1">

### Generic stdio client

Run the wizard:

```bash
python3 scripts/setup_wizard.py --yes --skip-credentials
```

Or copy the generic file manually:

```bash
cp .mcp.json.example .mcp.json
```

Edit `.mcp.json` and replace `/path/to/hpe-networking-mcp` with your local clone
path:

```json
{
  "mcpServers": {
    "hpe-networking-mcp": {
      "command": "/path/to/hpe-networking-mcp/.venv/bin/python3",
      "args": ["/path/to/hpe-networking-mcp/src/hpe_networking_mcp/mcp_servers/tool_router.py"],
      "cwd": "/path/to/hpe-networking-mcp",
      "env": {
        "PYTHONPATH": "/path/to/hpe-networking-mcp/src",
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

  </div>
  <div class="audience-card" markdown="1">

### Cursor

The committed `.cursor/mcp.json` is already the default low-token router
profile — no copy step needed:

```json
{
  "mcpServers": {
    "hpe-networking-mcp": {
      "command": "${workspaceFolder}/.venv/bin/python3",
      "args": ["${workspaceFolder}/src/hpe_networking_mcp/mcp_servers/tool_router.py"],
      "cwd": "${workspaceFolder}",
      "env": {
        "PYTHONPATH": "${workspaceFolder}/src",
        "CREDS_PATH": "${workspaceFolder}/config/credentials.yaml",
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

Use `.cursor/mcp.dev.json` only when debugging direct backend servers — it
registers the seven core Aruba servers (`central-monitoring`, `central-config`,
`central-ops`, `central-nac`, `central-streaming`, `glp-core`, `rag-core`) plus
`central-generated` directly, so it costs more tool-list context than
the router profile. Copy it over `mcp.json` only while debugging one tool.

  </div>
  <div class="audience-card" markdown="1">

### VS Code

```bash
cp .vscode/mcp.json.example .vscode/mcp.json
```

Keep the `hpe-networking-mcp` server entry enabled for normal use:

```json
{
  "servers": {
    "hpe-networking-mcp": {
      "type": "stdio",
      "command": "${workspaceFolder}/.venv/bin/python3",
      "args": ["${workspaceFolder}/src/hpe_networking_mcp/mcp_servers/tool_router.py"],
      "env": {
        "PYTHONPATH": "${workspaceFolder}/src",
        "CREDS_PATH": "${workspaceFolder}/config/credentials.yaml",
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

VS Code's `.vscode/mcp.json` intentionally uses the top-level `servers` key,
not `mcpServers`. When the Agent Host is enabled, a workspace `.mcp.json` or
Copilot's user config is the more portable choice; VS Code still owns model
selection, trust, and tool approval. See the
[VS Code MCP server reference](https://code.visualstudio.com/docs/agent-customization/mcp-servers).

  </div>
  <div class="audience-card" markdown="1">

### Claude Code

Claude Code's project-scoped configuration is a root `.mcp.json` file. Copy
the tested shape from
[`examples/mcp-clients/claude-code.mcp.json`](../examples/mcp-clients/claude-code.mcp.json)
to `.mcp.json`, then replace the documented placeholder paths:

```bash
cp examples/mcp-clients/claude-code.mcp.json .mcp.json
```

The file uses the `mcpServers` shape and an explicit `type: "stdio"` entry:

```json
{
  "mcpServers": {
    "hpe-networking-mcp": {
      "type": "stdio",
      "command": "/path/to/hpe-networking-mcp/.venv/bin/python3",
      "args": [
        "/path/to/hpe-networking-mcp/src/hpe_networking_mcp/mcp_servers/tool_router.py"
      ],
      "cwd": "/path/to/hpe-networking-mcp",
      "env": {
        "PYTHONPATH": "/path/to/hpe-networking-mcp/src",
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

9: demand. The complete index can contain 6,728 backend tools, while minimal
10: The remaining profiles in that file are direct debug servers
(`central-monitoring`, `central-config`, `central-ops`, `central-nac`,
`central-streaming`, `glp-core`)
plus two CLI launch entries for the migration pipeline and SSID builder.
On first use, Claude Code asks you to approve project MCP servers; a trusted
workspace or an explicit `claude mcp add`/`claude mcp list` workflow is
required. `.claude/launch.json` remains an optional launch/debug profile, not
the only Claude integration path. Its router profiles are also safe
read-only; direct backend and CLI entries are for targeted debugging.
See the [Claude Code MCP reference](https://code.claude.com/docs/en/mcp) for
current approval and transport behavior.

### GitHub Copilot CLI and app

For Copilot CLI's project-level repository configuration, use
[`.github/mcp.json`](../.github/mcp.json). It uses the `mcpServers` shape,
relative `src`/`config` paths, and `type: "stdio"` so the same server
definition is portable across Copilot CLI and other MCP hosts:

```bash
copilot mcp list
```

Copilot CLI also discovers a root `.mcp.json` (copy the generic or Claude
example) and user configuration at `~/.copilot/mcp-config.json`. Project
configs require folder trust; prompt mode skips an untrusted project config
unless `GITHUB_COPILOT_PROMPT_MODE_WORKSPACE_MCP=true` is set. Copilot CLI
does not read VS Code's `.vscode/mcp.json` because that file uses the
host-specific top-level `servers` key. The built-in GitHub MCP server is
already available in Copilot CLI, so this repository config adds only the
HPE server. The GitHub Copilot app can use servers configured for repositories
or Copilot CLI and also has its own MCP settings; Copilot cloud agent and code
review repository configuration is entered in GitHub repository settings,
not this local file. Those cloud surfaces run tools autonomously without
approval, currently support tools rather than MCP resources/prompts, and do
not support remote OAuth MCP servers, so keep their server configuration
separate and allowlist read-only tools. See the [Copilot app
settings](https://docs.github.com/en/copilot/how-tos/github-copilot-app/customize-github-copilot-app)
and [repository MCP
settings](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/configure-mcp-servers)
for those host-specific paths.

Copilot CLI's `/mcp add` and `copilot mcp add` commands can create equivalent
entries, and the host controls model selection, permissions, trust prompts,
and tool approvals; an MCP JSON file cannot select or upgrade the model. See
the [GitHub Copilot CLI MCP
documentation](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers)
for current registry, trust, and configuration precedence.

  </div>
</div>

## Optional existing frontends

No custom web app is included. If a terminal, inspector, or self-hosted chat
frontend is more useful than an editor host, these existing projects can use
the same router:

### Crush

[Charmbracelet Crush](https://github.com/charmbracelet/crush) is an optional
terminal alternative with host-owned model selection and permission prompts.
It is useful when users want to choose providers themselves or run a local
Ollama model. Its current MCP support includes `stdio`, HTTP, and SSE. Install
it with Homebrew or npm, then register the local router in a project
`.crushrc` (or use the equivalent global configuration):

```bash
mcp add hpe-networking-mcp \
  --type stdio \
  --command /path/to/hpe-networking-mcp/.venv/bin/python3 \
  --args /path/to/hpe-networking-mcp/src/hpe_networking_mcp/mcp_servers/tool_router.py \
  --env PYTHONPATH /path/to/hpe-networking-mcp/src \
  --env CREDS_PATH /path/to/hpe-networking-mcp/config/credentials.yaml \
  --env HPE_MCP_ACCESS_PROFILE safe-read-only \
  --env HPE_MCP_READONLY 1 \
  --env HPE_MCP_ROUTER_MODE minimal \
  --env HPE_MCP_TOOLSETS central,glp,rag \
  --env HPE_MCP_PRODUCT_ACCESS read-only
```

Keep Crush's normal permission prompts enabled; do not use `--yolo` for a
credential-backed network server. The paths above are placeholders only.

### MCPJam Inspector

[MCPJam Inspector](https://github.com/MCPJam/inspector) is an existing
testing/debugging frontend rather than a production chat surface. Its local
terminal and desktop versions support local STDIO and HTTP/S servers:

```bash
npx @mcpjam/inspector@latest
```

Use it to inspect the router, exercise `find_tool` and
`invoke_read_tool`, and compare host behavior. The hosted MCPJam app accepts
HTTPS URLs only and cannot launch this repository's local STDIO process, so
use the local inspector for STDIO or the loopback HTTP profile for a local
HTTP test.

### LibreChat

[LibreChat](https://github.com/danny-avila/LibreChat) is an optional
self-hosted browser platform for users who need a persistent, multi-provider
chat experience. Current LibreChat releases support MCP entries in
`librechat.yaml`, including `streamable-http`; point it at an already-running
router instead of embedding credentials in a tracked file:

```yaml
mcpServers:
  hpe-networking-mcp:
    type: streamable-http
    url: http://127.0.0.1:8010/mcp
    chatMenu: true
```

Use a protected, network-reachable URL when LibreChat runs in another
container or host; `127.0.0.1` then refers to that frontend's own network
namespace. Keep API credentials in the deployment environment and preserve
the router's `safe-read-only` profile.

### Open WebUI

[Open WebUI](https://github.com/open-webui/open-webui) is another optional
persistent, multi-provider browser platform. It has native MCP support in
v0.6.31 and later through **Admin Settings → Integrations → Add Server →
MCP (Streamable HTTP)**. MCP registration is admin-only. For a Docker
deployment, use a host-reachable URL such as
`http://host.docker.internal:8010/mcp`, not `localhost`; set
`WEBUI_SECRET_KEY` so OAuth-related credentials survive restarts. If a
deployment needs an OpenAPI bridge instead, the existing
[open-webui/mcpo](https://github.com/open-webui/mcpo) project can proxy an
MCP command or Streamable HTTP endpoint; it is optional and does not require
adding a web app to this repository.

<div class="docs-checkpoint">
  <span class="docs-checkpoint__number">1</span>
  <div class="docs-checkpoint__body" markdown="1">

**Checkpoint:** whichever config you copied, confirm the local doctor sees it
before opening your client:

```bash
uv run hpe-mcp-doctor
```

Look for `[OK] Local stdio MCP config: .mcp.json exists` (or the matching
HTTP line below) in the output.

  </div>
</div>

## Streamable HTTP

Start the local HTTP router. The helper defaults to port `8010`, matching
`.mcp.http.json.example`:

```bash
python3 scripts/setup_wizard.py --yes --skip-credentials
MCP_PORT=8010 bash scripts/run_http_router.sh
```

Copy the generic HTTP client snippet:

```bash
cp .mcp.http.json.example .mcp.http.json
```

```json
{
  "mcpServers": {
    "hpe-networking-mcp-http": {
      "url": "http://127.0.0.1:8010/mcp",
      "transport": "streamable-http"
    }
  }
}
```

Point your MCP client to:

```text
http://127.0.0.1:8010/mcp
```

If you change `MCP_HOST` or `MCP_PORT`, update `.mcp.http.json` to match. The
HTTP helper safely loads expected local `.env` assignments first, so optional
products selected in the wizard are available to the router process. Its
startup banner prints `HPE_MCP_ACCESS_PROFILE`, selected products, and
`HPE_MCP_PRODUCT_ACCESS` so write visibility is obvious before connecting a
client. It warns if the local `.env` still contains the retired
`CENTRALMCP_*` prefix; rename those assignments to `HPE_MCP_*` or remove them.
If the
port is already in use, `scripts/run_http_router.sh` exits before starting
another router and prints the listener details:

```bash
lsof -nP -iTCP:8010 -sTCP:LISTEN
kill <PID>
```

### Reconnect after a router restart

Streamable HTTP session handles belong to the running router process. Restarting
the router invalidates handles held by an existing MCP client, so requests made
with the old handle can return `404 Session expired` or `404 Invalid or expired
session ID` even while `/livez` is healthy. Refresh or restart the MCP client
connection, then let it perform a new `initialize` request; do not reuse the
old session handle.

<div class="docs-callout docs-callout--danger" markdown="1">

For non-loopback HTTP, configure explicit host and origin allow-lists — the
server refuses to start on a non-loopback bind without both:

```env
MCP_ALLOWED_HOSTS=mcp.example.test
MCP_ALLOWED_ORIGINS=https://client.example.test
MCP_HTTP_BEARER_TOKEN=replace-with-a-long-random-value
```

The client must send `Authorization: Bearer <token>`. Static bearer
protection is supported only with `streamable-http`; configuring it with SSE
refuses startup.

</div>

## Optional product clients

Keep optional products disabled unless you want them in the current MCP session.
The wizard can enable only the starters you choose, write the matching local
`.env`, and add the product selector to local stdio MCP configs:

```bash
python3 scripts/setup_wizard.py --products clearpass,mist
```

Use `--with-products` only when you want every starter backend enabled. Add
`--access-profile full-read-write` only for trusted sessions that need all
loaded write tools visible. Use `--access-profile custom --product-access
read-write` for the legacy mixed-gate behavior.

## Verify local setup

Run the local doctor before opening the client:

```bash
uv run hpe-mcp-doctor
```

It does not call Central, GLP, or optional product APIs. It checks copied
local configs, placeholder paths, HTTP URL/transport mismatch, low-token
router profile drift, optional product env, local indexes, RAG
source-manifest drift, and listener status.

## First useful MCP call flow

```text
find_tool("show critical alerts")
invoke_read_tool("list_active_alerts", {"severity": "CRITICAL", "limit": 20})
```

<div class="docs-checkpoint">
  <span class="docs-checkpoint__number">2</span>
  <div class="docs-checkpoint__body" markdown="1">

**Expected result:** a bounded, paginated response — for example, on a
fresh/quiet tenant (fake sample data):

```json
{
  "items": [],
  "_pagination": {"offset": 0, "limit": 20, "total": 0, "truncated": false}
}
```

An empty `items` list with valid `_pagination` still confirms the client,
router, and credentials are wired together correctly — it just means there
are no matching alerts right now.

  </div>
</div>

<span class="docs-badge docs-badge--read">read</span> Use `invoke_read_tool`
for investigations.
<span class="docs-badge docs-badge--destructive">destructive</span> Use
`invoke_tool` only after intentional write/destructive user intent.

<div class="docs-next" markdown="1">

## Next steps

- [example-prompts.md](example-prompts.md) — more copy/paste
  `find_tool` / `invoke_read_tool` flows.
- [getting-started.md](getting-started.md) — install, credentials, catalog,
  and validation steps that come before this page.
- [troubleshooting.md](troubleshooting.md) — fixes for connection, auth, and
  transport problems.
- [tool-router.md](tool-router.md) — router modes, safety gates, and
  observability in depth.

</div>
