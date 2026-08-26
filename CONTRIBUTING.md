# Contributing

Thanks for helping improve hpe-networking-mcp. This project is optimized for low-token,
lab-friendly HPE Networking MCP workflows, so changes should keep setup simple,
tool discovery compact, and credentials local.

Please follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) when participating in
issues, pull requests, reviews, and docs.

## Start locally

```bash
git clone https://github.com/secure-ssid/hpe-networking-mcp.git
cd hpe-networking-mcp
python3 scripts/setup_wizard.py --yes --skip-credentials
uv run hpe-mcp-doctor
```

Use fake hosts, fake IDs, and redacted payloads in examples, tests, screenshots,
and issue comments. Do not commit real `config/credentials.yaml`, `.env`, token
caches, tenant IDs, customer data, generated indexes, or local MCP config files.

## Before opening a pull request

1. Keep changes focused and update the matching docs when behavior, setup,
   environment variables, public tool counts, images, API provenance, or GitHub
   Pages content changes. Add a [`CHANGELOG.md`](CHANGELOG.md) entry for any
   user-visible change; version-specific detail still belongs in a
   `docs/release-notes-*.md` page.
2. Prefer the low-token router path (`find_tool`, `invoke_read_tool`,
   `invoke_tool`) for user-facing examples.
3. Run the smallest targeted tests that cover your change.
4. Run the local release gate before pushing:

```bash
uv run python scripts/validate_release.py --skip-rag
```

If you intentionally changed RAG/OpenAPI indexes or eval behavior, run the
relevant ingestion/eval commands instead of relying only on `--skip-rag`.
For a complete release/index change, use:

```bash
uv run python scripts/ingest_tools.py --complete-catalog
uv run python scripts/package_indexes.py --write-local-manifests
uv run python scripts/project_facts.py --write
uv run python scripts/validate_release.py --catalog-products all --strict-tool-index --min-tools 6712
uv run python scripts/check_openapi_drift.py
uv run python scripts/check_mist_openapi_drift.py
```

`--complete-catalog` pins every write gate and generated-tool flag before
loading the backends, so stale shell or `.env` values cannot silently shrink
the index. `--min-tools 6712` is the platform API compatibility floor (the
6,712 vendor-facing platform API tools), not the complete registered backend
total of 6,729 — validation passes at or above the floor; see
[`docs/tool-catalog.md`](docs/tool-catalog.md) for both totals.
After rebuilding an index, reconcile `data/SOURCE-MANIFEST.json` /
`data/INDEX-MANIFEST.json` and regenerate
[`docs/project-facts.json`](docs/project-facts.json) — the derived, canonical
source for every published count (package version, MCP server IDs, per-backend
tool counts, generated operations, exact `specs.sqlite` counts, RAG source
counts). Never hand-edit that file; `scripts/project_facts.py` (run without
arguments) fails on drift, and strict validation runs it with
`--require-indexes`.

Commits that change `.github/workflows/*` require a GitHub token with both
repository write permission and the OAuth `workflow` scope.

## Dependency update pull requests

Dependabot checks the `uv` dependency set monthly and limits open dependency
pull requests to keep review manageable. Treat dependency PRs like code changes:
review the changed dependency and lockfile impact, run targeted tests when the
package touches runtime behavior, and run the local release gate before merging.

## Security reports

Do not publish exploitable details or secrets in issues or pull requests. Follow
[SECURITY.md](SECURITY.md) for vulnerability reports and credential exposure.
