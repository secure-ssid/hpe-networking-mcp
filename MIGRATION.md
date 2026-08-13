# Migrating from `secure-ssid/centralmcp`

This project is a renamed, restructured continuation of the legacy
`secure-ssid/centralmcp` repository. This page is the step-by-step local
migration path for anyone who has an existing `centralmcp` checkout. See
[CHANGELOG.md](CHANGELOG.md) for the itemized list of what actually renamed
(package, server IDs, env vars, CLIs).

## What did *not* change

- **Your legacy `centralmcp` checkout is untouched.** Cloning
  `hpe-networking-mcp` into a sibling folder does not modify, move, or
  delete anything in the old repository. Nothing here writes into a
  `centralmcp` working tree.
- **No git history was rewritten.** `hpe-networking-mcp` is a fresh
  repository with an equivalent working tree at the time of the rename --
  it does not share commit history with `centralmcp`, and the old
  repository's history, issues, and releases stay exactly where they are.
- **Local, git-ignored artifacts are per-checkout, not shared.** RAG/tool
  indexes (`data/docs.lance`, `data/tools.lance`, `data/specs.sqlite`),
  credentials (`config/credentials.yaml`, `.env`), token caches, and private
  diagram icon packs (`resources/diagram_icons/private/`) all live under
  `.gitignore` in both repositories. Moving to the new repo does not carry
  any of these over automatically -- rebuild or copy them deliberately (see
  below).
- **Your home-level Copilot MCP configuration is untouched by this
  migration.** `~/.copilot/mcp-config.json` (or any other global/home MCP
  client config) is deliberately left pointed at whatever it already points
  to. Do not repoint it at the new checkout until you have independently
  validated the new server locally -- see
  [Deferring the home-level Copilot config switch](#deferring-the-home-level-copilot-config-switch).

## Recommended layout: sibling folder

Clone `hpe-networking-mcp` next to your existing `centralmcp` checkout
rather than replacing it in place:

```text
~/dev/
  centralmcp/            # legacy checkout -- left running, untouched
  hpe-networking-mcp/    # new checkout -- validate here first
```

```bash
cd ~/dev
git clone https://github.com/secure-ssid/hpe-networking-mcp.git
cd hpe-networking-mcp
python3 scripts/setup_wizard.py --yes --skip-credentials
uv run hpe-mcp-doctor
```

Keeping both checkouts on disk lets you diff behavior, fall back instantly
if something in the new layout doesn't work for your setup, and migrate
per-client MCP config at your own pace instead of a single cutover moment.

## Step-by-step migration

1. **Clone the new repository as a sibling**, as shown above. Do not delete
   or move the old `centralmcp` checkout.
2. **Copy credentials manually, if you want to reuse them.** Credentials are
   git-ignored in both repos and never auto-migrate:

   ```bash
   cp ~/dev/centralmcp/config/credentials.yaml \
      ~/dev/hpe-networking-mcp/config/credentials.yaml
   ```

   Or point `CREDS_PATH` at the existing legacy file instead of copying it
   (see [External credentials via `CREDS_PATH`](#external-credentials-via-creds_path)).
3. **Rebuild or re-download local indexes.** `data/` is git-ignored in both
   repos, so the new checkout starts with no local RAG/tool indexes:

   ```bash
   uv run python scripts/download_indexes.py
   uv run python scripts/ingest_tools.py --products all
   ```

4. **Reinstall any private diagram icon packs**, if you use the diagram
   tooling. These are local-only and never committed:

   ```bash
   uv run python scripts/install_diagram_icon_packs.py --from-downloads
   ```

5. **Rename environment variables in your shell profile / `.env`.** Replace
   every `CENTRALMCP_*` variable with the matching `HPE_MCP_*` name (see the
   full table in [CHANGELOG.md](CHANGELOG.md#changed)); `CREDS_PATH` is
   unchanged.
6. **Update per-client MCP configs one at a time**, validating each before
   moving to the next -- see [Per-client MCP config migration](#per-client-mcp-config-migration).
7. **Leave `~/.copilot/mcp-config.json` (or any other global client config)
   pointed at the legacy checkout until you have validated the new one**,
   per [Deferring the home-level Copilot config switch](#deferring-the-home-level-copilot-config-switch).

## External credentials via `CREDS_PATH`

You do not have to copy `config/credentials.yaml` into the new checkout at
all. Every entry point that loads credentials honors `CREDS_PATH`, so you
can point the new repository directly at your existing legacy file and skip
duplicating secrets on disk:

```bash
export CREDS_PATH=~/dev/centralmcp/config/credentials.yaml
uv run hpe-mcp-doctor
```

This also works per-MCP-client-config -- add `CREDS_PATH` to the `env` block
of a stdio config (see `.mcp.json.example` / `examples/`) instead of setting
it in your shell.

## Per-client MCP config migration

Update stdio configs (`.mcp.json`, `.cursor/mcp.json`,
`.vscode/mcp.json`, `.claude/launch.json`) to reference the new checkout
path, the renamed `hpe-networking-mcp` server entry, and `HPE_MCP_*`
env vars instead of `CENTRALMCP_*`. See
[`examples/README.md`](examples/README.md) for a complete, tested profile
matrix (minimal stdio, full stdio, local HTTP, bearer HTTP) and
[`docs/mcp-client-recipes.md`](docs/mcp-client-recipes.md) for the
per-client copy/paste walkthrough. Absolute paths in every committed
example are placeholders (`/path/to/hpe-networking-mcp`) -- replace them
with your real clone path, or use `${workspaceFolder}`-relative configs
(Cursor, VS Code) that need no path edits at all.

Do not run both the legacy `centralmcp` router and the new
`hpe-networking-mcp` router as the same named MCP server in one client at
the same time; give them distinct server names (e.g. keep the legacy entry
as `centralmcp` while validating `hpe-networking-mcp` alongside it) until
you are ready to remove the old one.

## Deferring the home-level Copilot config switch

If you use the Copilot CLI/app with a home-level MCP configuration
(`~/.copilot/mcp-config.json`), **do not repoint it at the new checkout as
part of this migration.** Validate the new repository first using a
project-local or per-client config (`.mcp.json`, `.cursor/mcp.json`, etc.)
so a mistake in the new setup cannot break your default Copilot MCP
environment. Once you've confirmed `hpe-mcp-doctor` passes and a real
`find_tool` / `invoke_read_tool` round trip works end-to-end against the new
checkout, update the home-level config yourself, on your own schedule, using
the same `hpe-networking-mcp` server entry and `HPE_MCP_*` env vars shown in
[`examples/README.md`](examples/README.md).

## Verifying the migration worked

```bash
cd ~/dev/hpe-networking-mcp
uv run hpe-mcp-doctor
uv run pytest tests/unit -q
```

`hpe-mcp-doctor` validates local config and readiness only -- dependency,
config-path, index, credential-file/env presence and placeholder checks, and
local listener status. It never makes a Central, GLP, or optional-product
API call of any kind (read or write); it only confirms credentials are
*present and well-formed*, not that they authenticate against a live tenant.
See [Troubleshooting](docs/troubleshooting.md) if any check fails.

## Decommissioning the legacy checkout (optional, later)

There is no requirement to remove `centralmcp` -- keep it as long as you
find it useful for comparison, or as an offline reference. When you are
ready, simply stop pointing any MCP client at it and delete the directory;
nothing in `hpe-networking-mcp` depends on the legacy checkout existing.
