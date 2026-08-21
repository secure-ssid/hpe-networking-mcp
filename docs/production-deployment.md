# Production deployment (Docker)

This is a packaging guide for running the hpe-networking-mcp streamable-HTTP
router (`hpe-mcp-router`) in a container, in addition to (not instead of)
the local `uv run` / stdio workflow described in
[getting-started.md](getting-started.md) and
[mcp-client-recipes.md](mcp-client-recipes.md). It complements, and does not
replace, the optional local-only Redis/Ollama server backend already
documented for `docker-compose.yml`.

<div class="docs-callout docs-callout--safe" markdown="1">
Nothing here is required. The router runs perfectly well with a plain
`uv run hpe-mcp-router` on a laptop. Containerizing it only matters once
you're running the router unattended (a shared dev box, a small VM, a
Kubernetes pod) where the same non-root, no-secrets-in-the-image, no-silent
network fetch expectations from local development should still hold.
</div>

## Files

| File | Purpose |
|---|---|
| [`../Dockerfile`](../Dockerfile) | Multi-stage production image for the router (`hpe-mcp-router`) |
| [`../.dockerignore`](../.dockerignore) | Keeps secrets, `.env`, local state, and built indexes out of the build context |
| [`../docker/entrypoint.sh`](../docker/entrypoint.sh) | Expands `*_FILE` Docker-secret conventions into plain env vars, then execs the requested command |
| [`../docker-compose.yml`](../docker-compose.yml) | Unchanged: optional localhost-only Redis/Ollama server backend |
| [`../docker-compose.router.yml`](../docker-compose.router.yml) | Additive overlay: the containerized router, behind a Compose `router` profile |
| [`../secrets/README.md`](../secrets/README.md) | How to provision `config/credentials.yaml` and the bearer token as Docker secrets |
| [`../secrets/mcp_http_bearer_token.example`](../secrets/mcp_http_bearer_token.example) | Placeholder bearer-token secret template |

## Quick start

```bash
# 1. Provision secrets (never commit the real files this creates):
cp config/credentials.yaml.example secrets/credentials.yaml
# edit secrets/credentials.yaml with real Central/GLP client id/secret values
openssl rand -hex 32 > secrets/mcp_http_bearer_token
chmod 600 secrets/credentials.yaml secrets/mcp_http_bearer_token

# 2. Build and start the router alongside the unchanged redis/ollama services.
#    The spec index ships in the image -- there is nothing to populate first.
docker compose -f docker-compose.yml -f docker-compose.router.yml \
  --profile router up -d --build

# 3. Verify:
curl http://127.0.0.1:8010/livez
```

A plain `docker compose up` (no `-f docker-compose.router.yml`, no
`--profile router`) continues to start only `redis`/`ollama`, exactly as
before this overlay existed.

## Security choices

### Loopback-only exposure by default

The router's own code refuses to bind beyond loopback (`MCP_HOST` other than
`127.0.0.1`/`localhost`/`::1`) unless `MCP_ALLOWED_HOSTS` and
`MCP_ALLOWED_ORIGINS` are **both** set explicitly, with no wildcard entries
— see `UnsafeHttpBindingError` in
`src/hpe_networking_mcp/mcp_servers/shared.py`. That check runs inside the
container exactly as it does locally; this packaging doesn't touch it.

`docker-compose.router.yml` sets `MCP_HOST=0.0.0.0` because Docker's
port-publish proxy connects to the container's own network-namespace
address, not to `127.0.0.1` inside it — a process bound strictly to
`127.0.0.1` inside a container is unreachable from the host even with a
port published. The actual "loopback only from the host's point of view"
guarantee instead comes from the **publish** side:

```yaml
ports:
  - "127.0.0.1:8010:8010"
```

This is the same pattern `docker-compose.yml` already uses for `redis` and
`ollama` — the container-internal bind address and the host-published
address are two different security boundaries, and only the second one is
what actually decides whether something outside the machine can reach the
port. `MCP_ALLOWED_HOSTS`/`MCP_ALLOWED_ORIGINS` are set to `127.0.0.1`/
`localhost` (non-wildcard) so the router's own DNS-rebinding protection
still applies once `MCP_HOST=0.0.0.0` is in effect.

If you need the router reachable from other machines (not just
`localhost` on the Docker host), change the publish spec to
`"0.0.0.0:8010:8010"` (or a specific interface) **and** put a real
reverse proxy / TLS terminator / firewall in front of it, and set
`MCP_HTTP_BEARER_TOKEN` (see below). This packaging deliberately does not
make that the default.

### No credentials baked into the image

* `.dockerignore` excludes `.env`, `config/credentials.yaml`, and everything
  under `secrets/` except the tracked `*.example` templates and
  `secrets/README.md` — the Docker build daemon never even receives these
  files as build context, so they can't end up in an image layer by
  accident.
* The `Dockerfile` never `COPY`s any of those paths.
* Real secrets are supplied at container **start**, two ways:
  * `CREDS_PATH=/run/secrets/credentials_yaml` — `config/credentials.yaml`'s
    equivalent content is already loaded from whatever path `CREDS_PATH`
    names (see `hpe_networking_mcp.pipeline.config.load_credentials`), and
    Docker secrets are already ordinary files mounted read-only at
    `/run/secrets/<name>`, so this needs no extra glue.
  * `MCP_HTTP_BEARER_TOKEN_FILE=/run/secrets/mcp_http_bearer_token` —
    `docker/entrypoint.sh` reads this file's contents into
    `MCP_HTTP_BEARER_TOKEN` before starting the router, the common
    `<VAR>_FILE` convention used by many official images. An already-set
    `<VAR>` always wins over its `<VAR>_FILE` counterpart.
* See [`../secrets/README.md`](../secrets/README.md) for the full setup
  steps and how to add more `*_FILE` secrets (optional-product API tokens,
  etc.).

### The spec index ships in the image; nothing is ever downloaded for you

The image **contains** `/app/data/specs.sqlite`. It is built during the image
build, in a throwaway stage, from the 31 digest-pinned OpenAPI documents
committed under `vendor/openapi/`, so `lookup_api` answers on a bare
`docker run` with no credentials, no network, and no extra step. The corpus
itself never reaches the runtime image.

Nothing else is provisioned automatically. Neither the `Dockerfile` build, nor
`docker/entrypoint.sh`, nor `docker-compose.router.yml` downloads an index at
build or at container start. The RAG prose corpus (`data/docs.lance`) is
scraped vendor documentation this project does not distribute, so `ask_docs`
and `search_docs` have nothing to answer from until you build it yourself —
see [release-indexes.md](release-indexes.md).

> **The overlay does not shadow the baked index.**
> `docker-compose.router.yml` bind-mounts `./data/docs.lance` and
> `./data/tools.lance` individually, never `./data` as a whole, precisely so
> that `/app/data/specs.sqlite` from the image stays visible. `lookup_api`
> therefore works on a first `docker compose up` with an empty host `./data`.
> Pinned by `tests/unit/test_docker_router_packaging.py`.
>
> To run a *different* index than the image's, add an explicit mount for it:
>
> ```yaml
> - ./data/specs.sqlite:/app/data/specs.sqlite:ro
> ```

If you host your own archive internally, package it with
`scripts/package_indexes.py` -- which emits the checksum manifest -- and
restore it with the manifest *you* generated, or with the
`spec-index-manifest.json` published alongside a release:

```bash
# Optional: restore from an archive you host yourself, or from a release's
# spec-index-manifest.json. The pinned digest is verified before anything is
# unpacked, and there is no default URL: --manifest or --url is required.
docker compose -f docker-compose.yml -f docker-compose.router.yml \
  run --rm --no-deps mcp-router \
  uv run python scripts/download_indexes.py --manifest /app/state/your-index-bundle.json \
  --output-dir /app/data-download

# Move the verified output into the bind-mounted ./data on the host, then
# restart mcp-router so it picks up the new files:
mv data-download/data/* data/
docker compose -f docker-compose.yml -f docker-compose.router.yml \
  --profile router restart mcp-router
```

`docker-compose.router.yml` bind-mounts the two artifacts the image cannot
ship — `./data/docs.lance` (scraped prose corpus) and `./data/tools.lance`
(router tool index) — **read-only**, and nothing else. So:

* The host, not the container, is the source of truth for those two artifacts
  — you can inspect, diff, or roll them back with ordinary shell tools between
  container restarts. Restart `mcp-router` after replacing them so the running
  process picks up the change.
* The mounts are read-only from the container's point of view, so the
  container process cannot overwrite the host's `data/` even if compromised.
  The baked `/app/data/specs.sqlite` is root-owned and mode `0444` for the
  same reason: it is no longer covered by a read-only mount.
* Both host paths start empty. `data/` is git-ignored, so a clone ships
  neither, and the overlay cannot smuggle in a prebuilt corpus.
* `scripts/download_indexes.py` already refuses non-HTTPS URLs, verifies a
  SHA-256 digest independently of any downloaded `.sha256` sidecar when a
  pinned manifest is used, rejects path-traversal/symlink members inside the
  archive, and swaps the new index into place atomically — see
  `tests/unit/test_download_indexes.py`. This packaging relies on those
  existing guarantees rather than re-implementing them; it only decides
  *when* that script runs (explicitly, never automatically).
* If you maintain your own index build pipeline instead, point those two
  paths at whatever directory your pipeline populates -- the Compose bind
  mounts don't care how `data/docs.lance` and `data/tools.lance` got there,
  only that they exist before `mcp-router` starts (or after a restart).

### Non-root, minimal runtime image

* The container runs as a dedicated, non-root `mcp` user (uid/gid `10001`),
  not a shared "system" uid range.
* `/app` (application code and the `uv`-managed virtualenv) is owned by
  `root` at the top level; only `/app/state`, `/app/outputs`, `/app/data`,
  and the `mcp` user's own home directory (`/home/mcp`, used for the `uv`
  cache) are writable by the running process. The baked
  `/app/data/specs.sqlite` inside that directory is root-owned and mode
  `0444`, so the router can read its own index but not rewrite it.
* Dependencies are resolved once, at build time, from the committed
  `uv.lock` (`uv sync --frozen`) — the runtime image sets `UV_NO_SYNC=1` so
  `uv run hpe-mcp-router` at container start never attempts network
  dependency resolution.
* The image is multi-stage: the `builder` stage (full `uv` cache, `apt`
  package lists) never reaches the `runtime` stage.

## Host / runtime limitations

* **Docker Compose v2.20.2+ / Compose Spec** is assumed for the `router`
  Compose profile and file-based `secrets:` block used here; older
  Compose plugin versions may not support one or both. Validated locally
  against Docker Compose v5.3.0.
* **BuildKit** (`# syntax=docker/dockerfile:1`, `--mount=type=cache`,
  `COPY --chmod=`) is required to build the image; this is the default for
  any reasonably current Docker Engine/Desktop, but very old Docker
  installs without BuildKit enabled cannot build this `Dockerfile` as-is.
* **No GPU passthrough is configured** for the router image (it doesn't
  need one). If you also run the optional `ollama` service from
  `docker-compose.yml` with GPU acceleration, that remains a separate,
  already-documented `deploy.resources.reservations.devices` block in
  `docker-compose.yml` — unaffected by this overlay.
* **`fastembed`'s ONNX Runtime backend** needs `libgomp1` at runtime; the
  image installs it. If you change the base image, keep an equivalent
  OpenMP runtime library available or embeddings-backed RAG tools will fail
  to import.
* **Non-loopback exposure needs a real proxy.** Setting
  `ports: ["0.0.0.0:8010:8010"]` (or binding to a LAN interface) without
  putting TLS termination, auth, and network policy in front of the
  container is explicitly out of scope for this packaging and is not the
  default; see "Loopback-only exposure by default" above.
* **Prose-corpus provisioning is a host-visible extra step**, not a
  one-command `docker run`. `lookup_api` works out of the box because the
  image ships `data/specs.sqlite`, but `ask_docs` and `search_docs` need
  `data/docs.lance`, which you build yourself
  (`uv run python ingestion/ingest_docs.py`) and bind-mount. The overlay
  mounts that path on its own rather than mounting `./data` wholesale, so
  leaving it empty costs you only the RAG tools: the router still starts and
  serves non-RAG tools (Central/GLP/monitoring/config/ops/NAC) against the
  baked spec index.
* **Firmware upgrade caveat carries over unchanged**: `set_firmware_compliance`
  remains the supported path; `/firmware/v1/upgrade` still 404s on this
  Central instance regardless of how the router is deployed.

## Validating this packaging

```bash
# Structural/static checks (YAML parse, non-root user, no baked secrets,
# no silent index download, loopback-only publish, profile gating):
uv run pytest tests/unit/test_docker_router_packaging.py tests/unit/test_docker_compose.py -q

# Compose merge/validation (no daemon required):
docker compose -f docker-compose.router.yml --profile router config
docker compose -f docker-compose.yml -f docker-compose.router.yml --profile router config

# Full build + start + healthcheck (requires a local Docker daemon):
docker build -t hpe-networking-mcp-router:local .
docker compose -f docker-compose.yml -f docker-compose.router.yml \
  --profile router up -d --build
curl http://127.0.0.1:8010/livez
docker compose -f docker-compose.yml -f docker-compose.router.yml \
  --profile router down
```
