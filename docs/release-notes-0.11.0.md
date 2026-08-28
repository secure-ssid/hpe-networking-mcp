---
title: "0.11.0"
nav_order: 1
parent: "Releases"
---

# hpe-networking-mcp 0.11.0 - one-pass Docker setup, one file per credential, one Docker page

Version 0.11.0 is a Docker release. It makes `scripts/setup_wizard.py --docker`
ask every deployment question in a single pass and emit a bundle that actually
runs with those answers; it gives every credential its own `0600` file so
rotating one key never touches another; and it collapses the Docker
documentation from two competing procedures spread across seven files into one
ordered page.

Nothing here is breaking. Existing `secrets/credentials.yaml` files keep
working unchanged, and the new per-platform write-gate lines cannot alter an
existing deployment's posture — see [Upgrading from 0.10.0](#upgrading-from-0100).

## Highlights

### `--docker` collected your product selection and threw it away

This is the defect the release exists to fix. Running:

```bash
python3 scripts/setup_wizard.py --docker --products mist
```

produced a stack that loaded **no Mist backend** and held **no Mist token**.
The selection reached a summary line and nothing else: `HPE_MCP_PRODUCTS` was
never written, the generated overlay had no key for it, and product credentials
were prompted only in the local `uv` flow.

The nine `HPE_MCP_*_WRITES` gates failed the same way. The wizard wrote them
into `.env`, where nothing read them — Compose injects only variables a service
names explicitly, and neither overlay named them. Under the default `custom`
profile that made the entire per-platform write model inert inside containers.

Both now reach the container, and a test asserts it from inside one:

```
$ docker compose ... exec -T mcp-router python -c \
    "from hpe_networking_mcp.mcp_servers.tool_router import _build_backends; print(sorted(_build_backends()))"
['central-config', 'central-monitoring', 'central-nac', 'central-ops',
 'central-streaming', 'glp-core', 'interop-core', 'mist-core',
 'rag-core', 'site-health']
```

### One pass through every deployment question

`--docker` now asks, in order: recommended-defaults shortcut, toolsets,
optional products **and their credentials**, aggregate access profile,
per-platform write gates, client hostname, RAG image and vector backend.
Accepting the first question takes loopback-only, Central + GreenLake + API
lookup, read-only, no optional products — four prompts to a complete bundle.

Every answer is also a flag. `--toolsets` is new; `--products`,
`--access-profile`, `--product-access`, `--router-mode`, `--expose` and
`--force` already existed but were undocumented.

### One secret value, one file, one Compose secret, one `<VAR>_FILE`

Every credential is now its own `0600` file under `secrets/`, mounted as its
own Compose secret and read through the `<VAR>_FILE` → `<VAR>` bridge that
`docker/entrypoint.sh` already implemented. Rotation is the point:

```bash
printf '%s' "$NEW_TOKEN" > secrets/mist_api_token
docker compose -f docker-compose.yml -f docker-compose.router.local.yml \
  restart mcp-router
```

No other credential is read, rewritten, or re-exposed. A plain `restart`
suffices because a Compose secret is a live mount of the host file and the
entrypoint re-reads it on every start.

`secrets/credentials.yaml` keeps Central/GreenLake **identity** — base URLs,
client ids, workspace ids — and no `client_secret` keys. `load_credentials()`
already ranks process environment above the YAML and reads
`SOURCE_CLIENT_SECRET` / `TARGET_CLIENT_SECRET`, so this needed no loader
change. Hand-written files carrying secrets inline keep working.

A guard mirrors `_BRIDGE_RE` from `docker/entrypoint.sh` and refuses to emit a
secret the container would silently ignore; a test pins the two byte-equal so
they cannot drift.

### Three fixes found by running the thing

- **`.env` changes need `up -d`, not `restart`.** Compose bakes interpolated
  values into a container at creation. Verified: a gate read `0` after
  `restart` and `1` only after recreate. Secret files are the opposite — live
  mounts, so `restart` is enough. Both are now documented distinctly.
- **The redis RAG backend embeds through Ollama, not fastembed**
  (`mcp_servers/rag.py`), so its image extra is `redis` alone and it needs
  `depends_on` on both `redis` and `ollama`.
- **`OllamaClient` hard-coded `http://localhost:11434`**, which inside the
  router container is the router itself. The redis RAG backend could never have
  worked in Docker. It is now overridable with `OLLAMA_URL`, matching
  `REDIS_URL`.

### Credentials the router was silently discarding

`scripts/run_http_router.sh`'s environment allow-list omitted
`APSTRA_USERNAME`, `APSTRA_PASSWORD`, `AOS8_USERNAME` and `AOS8_PASSWORD`.
Session login is the primary authentication path for both platforms, so a
`.env` carrying them was dropped on the floor. `PRODUCT_ENV` also handed out
`EDGECONNECT_AUTH_HEADER: Authorization`, a value the generated-tool guard
rejects outright — the runtime default is `X-Auth-Token`.

### Documentation: one Docker page

`docs/production-deployment.md` becomes **Docker deployment**, restructured
into one ordered path — checkout → wizard → start → verify. The manual
procedure folds in as "Without the wizard"; the credential-free `docker run` is
demoted to a labelled tyre-kick. README and `docs/index.md` stop duplicating
it. `docs/optional-products.md` and `docs/troubleshooting.md` gain the Docker
sections they never had.

Separately, **24 links on the published site were returning 404**. From
`docs/*.md` a `../` link escapes the Pages site root, which is `docs/`, not the
repository root. The existing link test only checked filesystem resolution, so
it validated the GitHub-repo reading experience and was blind to the Pages one.
Those links now use absolute `blob/main/` URLs, and a new gate fails any
`docs/` link that resolves outside `docs/`.

### Router fast-path wrappers for chatbot-style clients

A read-only, low-latency chatbot deployment surfaced two related gaps in
`default` router mode. Mist has no curated "list current clients" tool and
almost every Mist tool needs `org_id` (often `site_id`), which a client has
no way to know without an extra `mist_get_self` round trip repeated every
conversation. Central had none of that org/site problem, but was still
missing a direct wrapper for two of the most common questions: "get a site
by name" and "list connected clients" — both fell back to `find_tool`'s
semantic search for lack of one.

Six new router-native wrappers close both gaps: `mist_clients`,
`mist_devices`, `mist_ports`, `mist_health` (Mist, all with `org_id`/
`site_id` defaulted from `MIST_ORG_ID`/`MIST_SITE_ID` when the caller omits
them), and `get_site`, `list_clients` (Central, reusing the existing
curated tools' own names, same as `find_client`/`list_sites`). `mist_clients`
also narrows its search window to `duration=<minutes>m` instead of Mist's
own ~14-day default, and every Mist wrapper result is cached for
`HPE_MCP_ROUTER_WRAPPER_CACHE_TTL_SECONDS` (default 30s, `0` disables).

Separately, `HPE_MCP_ROUTER_EAGER_LOAD=1` imports every enabled backend at
process startup instead of lazily on the first real query — mist-core alone
is ~1.9s to import, and a latency-sensitive deployment can now pay that once
at boot instead of on a user's first question.

The four Mist wrappers are gated on router mode only, never also on which
optional products are enabled: `default` mode's client-visible tool count
must stay identical whether probed under the documented recommended profile
or under every toolset at once (`router_mode_facts()` asserts this), and
gating on `mist-core` specifically would have broken that. A call against a
backend that was never loaded degrades through the same unknown-tool/
platform-hint path any other missing tool name already does.

Live-verified end to end over the real MCP wire protocol against a running
`mcp-router` container, hitting production Mist Cloud and Aruba Central
data: `mist_clients`/`mist_devices` in 4-14ms (first touch, no lazy-import
delay), `mist_health` in ~157ms (concurrent switches+gateways+alarms), and
`list_clients(connection_type="Wireless")` returning real client data in one
0.77s call with no site lookup first.

## Catalog snapshot

Every value below is `docs/project-facts.json` at this release, generated by
`scripts/project_facts.py`, except the two vendored-OpenAPI rows, which are
summed from `vendor/openapi/MANIFEST.json` at the same commit. Locally built
index counts are deliberately excluded: they describe the machine that ran the
generator, not the release.

| Artifact | Count |
|---|---:|
| Registered backend tools | 6,729 |
| Platform API backend tools | 6,712 |
| Curated tools | 601 |
| Platform curated tools | 584 |
| Generated tools registered | 6,128 |
| Generated manifest operations | 6,145 |
| Backends (server ids) | 18 |
| Optional platform products | 8 |
| Vendored OpenAPI specs | 31 |
| Spec-index endpoints (offline-derivable) | 2,734 |
| Spec-index schemas (offline-derivable) | 6,363 |
| Spec-index fields (offline-derivable) | 31,432 |

Client-visible tool counts by router mode: **minimal 3**, **default 25**,
**direct-all 6,741** (6,729 registered plus 12 router-native tools).

The single-tool increase over 0.10.0 is `glp_get_server_hardware_inventory_report`,
from advancing the vendored GreenLake pin to `05d596a01ea6` — two routine
upstream OAS syncs (2026-08-17 and 2026-08-24). Purely additive; nothing was
removed or renamed.

## Validation

- Full unit suite: **5,061 passed, 4 skipped**.
- `ruff check .` clean tree-wide, no exclusions and no added `# noqa`.
- Docker path exercised end to end against a clean checkout: image build,
  `/livez`, four `entrypoint: filled …` bridge lines, `mist-core` present in
  `_build_backends()` inside the container, write gates readable inside the
  container, and rotation isolation observed at `/proc/1/environ` — the rotated
  token changed while the untouched Central secret did not.
- `docker compose … config` exit 0 for both the LanceDB and redis overlays.
- `scripts/check_nowireless_source_drift.py`: **4 current, 0 drifted**.

## Upgrading from 0.10.0

**No action is required, and nothing changes behaviour on its own.**

- **Write gates are unchanged.** `env_flag()` resolves an unset variable and
  `"0"` identically to `False`, so the new `HPE_MCP_*_WRITES` lines in both
  overlays cannot enable or disable anything that was not already set. They
  only make `=1` work, which it never did inside a container before.
- **Existing `secrets/credentials.yaml` keeps working.** The identity/secret
  split applies to files the wizard writes. `load_credentials()` still reads
  inline `client_secret` values.
- **Rebuild the image to pick up the fixes.** `run_http_router.sh` and
  `ollama_client.py` are baked in:

  ```bash
  docker compose -f docker-compose.yml -f docker-compose.router.yml \
    --profile router up -d --build mcp-router
  ```

- **Re-running the wizard is safe** once real credentials are in place: with no
  placeholders remaining it rewrites nothing. While placeholders are still
  present it re-prompts and resets `HPE_MCP_TOOLSETS` in `.env` to the
  recommended default; optional products survive, because the generated overlay
  bakes the selection into its own `${VAR:-…}` default.

## Known boundaries

- No CI job publishes a RAG-capable image. `INSTALL_EXTRAS` builds remain a
  documented local build.
- Prose retrieval still needs a corpus you build yourself; the shipped image
  answers `lookup_api` from the baked spec index only.
- The redis RAG backend now has correct container wiring, but it has not been
  exercised end to end against a populated Redis corpus in CI.
