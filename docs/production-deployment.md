---
title: "Production deployment"
nav_order: 7
---

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

## Run the published image

No checkout is needed for the container path: CI publishes the router to
GHCR scan-gated. Every build is pushed under a `sha-<short-sha>` tag (first
seven characters of the commit SHA) and that exact digest is promoted to
`latest` (builds from `main`) or the matching semver tag(s) (`v*` release
builds) only after the Trivy policy passes — so `latest` always points at
scan-approved bytes, and `sha-<short-sha>` pins one build exactly:

```bash
docker run -d --name hpe-networking-mcp \
  -p 127.0.0.1:8010:8010 \
  -e MCP_HOST=0.0.0.0 \
  -e MCP_ALLOWED_HOSTS='127.0.0.1:*,localhost:*' \
  -e MCP_ALLOWED_ORIGINS='http://127.0.0.1:*,http://localhost:*' \
  ghcr.io/secure-ssid/hpe-networking-mcp:latest
```

Once startup finishes (seconds), `curl http://127.0.0.1:8010/livez` answers
`{"status":"ok"}`. The loopback-only publish keeps the server off your LAN;
the `host:*` allowlist form is required whenever `MCP_HOST` is not loopback
(the guard behind that rule is described under "Loopback-only exposure by
default" below).

The default image ships the baked spec index (`/app/data/specs.sqlite`), so
credential-free exact-API lookup (`lookup_api`) works with no provisioning.
It does **no** prose retrieval by any backend: serving a real docs corpus
takes the `INSTALL_EXTRAS=ingestion` rebuild plus corpus mounts documented
in "Building a RAG-capable image" below.

The Compose quick start that follows instead builds from a checkout and is
the path to use when you want credentials supplied as file secrets and the
Redis/Ollama services managed alongside the router.

## Quick start

```bash
# 1. Provision secrets (never commit the real files this creates):

cp config/credentials.yaml.example secrets/credentials.yaml

# edit secrets/credentials.yaml with real Central/GLP client id/secret values
openssl rand -hex 32 > secrets/mcp_http_bearer_token
chmod 600 secrets/credentials.yaml secrets/mcp_http_bearer_token

# 2. Build and start the router. The spec index ships in the image -- there is
#    nothing to populate first. Naming the service matters: `redis` and
#    `ollama` are in docker-compose.yml's default profile, so omitting
#    `mcp-router` here would also start two containers the default image has
#    no client for (see "Prose retrieval" below).

docker compose -f docker-compose.yml -f docker-compose.router.yml --profile router up -d --build mcp-router

# 3. Verify:
curl http://127.0.0.1:8010/livez
```

A plain `docker compose up` (no `-f docker-compose.router.yml`, no
`--profile router`) continues to start only `redis`/`ollama`, exactly as
before this overlay existed. The overlay declares no `depends_on`, which is
what lets `up -d mcp-router` bring up the router on its own.

## Security choices

### Loopback-only exposure by default

The router's own code refuses to bind beyond loopback (`MCP_HOST` other than
`127.0.0.1`/`localhost`/`::1`) unless `MCP_ALLOWED_HOSTS` and
`MCP_ALLOWED_ORIGINS` are **both** set explicitly, with every wildcard entry
limited to the SDK-supported `<host>:*` port-wildcard form — a bare `*` or a
subdomain glob silently matches nothing and is refused — see
`UnsafeHttpBindingError` in
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
port. `MCP_ALLOWED_HOSTS`/`MCP_ALLOWED_ORIGINS` are set to `127.0.0.1:*`/
`localhost:*` port wildcards so the router's own DNS-rebinding protection
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

### This image is the exact-API-lookup deployment

The image **contains** `/app/data/specs.sqlite` — 2,734 endpoints, 6,363
schemas and 31,432 fields, built during the image build in a throwaway stage
from the 31 digest-pinned OpenAPI documents committed under `vendor/openapi/`.
`lookup_api` therefore answers on a bare `docker run` with no credentials, no
network, and no provisioning step. The corpus itself never reaches the runtime
image.

**The default image does no prose retrieval by any backend**, and that is the
whole statement — switching backends is not a way around it. `ask_docs` and
`search_docs` read a corpus built from scraped vendor documentation this
project has no licence to redistribute, so it can never ship in an image; you
build it yourself. On top of that the default image installs neither of the
two clients that could read one: the embedded LanceDB/ONNX stack (~700 MB) is
in the `ingestion` extra, and the `HPE_MCP_RAG_BACKEND=redis` alternative
needs the `redis` extra. Starting `docker-compose.yml`'s `redis` and `ollama`
services changes nothing on its own. What you actually get, verified in the
built image rather than inferred:

* `lookup_api` — full answers from the baked spec index.
* `ask_docs` — answers, but from the spec index, not the prose corpus. It
  falls back to structured evidence excerpts sourced `openapi_specs`, so the
  provenance in the reply says where the answer came from.
* `search_docs` — the degraded shape (`error` + `degraded` + `hint`), never a
  confident empty list:

```text
The document corpus needs the optional `lancedb` package, which is not
installed — rebuild the image with `docker build --build-arg
INSTALL_EXTRAS=ingestion` — see docs/production-deployment.md
```

#### Building a RAG-capable image

End-to-end checklist: from a bare checkout to prose answers served over
Docker. Do the steps in order — steps 1–2 run on the host from a source
checkout, steps 3–6 need only Docker.

```bash
# 1. Fetch the declared vendor sources. A fresh checkout has no
#    ingestion/sources/ tree (it is git-ignored and never committed), and
#    ingest_docs.py refuses to replace an index built from an empty one.
#    This crawls vendor sites for hours and accepts each vendor's document
#    terms -- nobody can accept those terms on your behalf:
python scripts/refresh_rag_sources.py --refresh-sources

# 2. Build the LanceDB corpus from what step 1 fetched:
uv run --extra ingestion python ingestion/ingest_docs.py

# 3. Build an image that can read it. `ingestion` gets the embedded LanceDB
#    backend, which needs no services. For the Redis backend instead, use
#    INSTALL_EXTRAS=redis and set HPE_MCP_RAG_BACKEND=redis; `all` gets both.
docker build --build-arg INSTALL_EXTRAS=ingestion \
  -t hpe-networking-mcp-router:rag .

# 4. Provision the two secrets the overlay mounts, exactly as in "Quick
#    start" above: secrets/credentials.yaml and secrets/mcp_http_bearer_token,
#    chmod 600, never committed. Compose cannot start the service without
#    them.

# 5. In docker-compose.router.yml set `image:` to that tag and add the
#    mounts, read-only, individually — never `./data:/app/data`, which
#    would shadow the baked spec index. Start WITHOUT `--build`: the service
#    also has a `build:` section, so `--build` would rebuild your :rag tag
#    straight from the Dockerfile, whose INSTALL_EXTRAS default is empty --
#    silently replacing the RAG-capable image with a bare one:
#      - ./data/docs.lance:/app/data/docs.lance:ro
#      - ./data/tools.lance:/app/data/tools.lance:ro
docker compose -f docker-compose.yml -f docker-compose.router.yml \
  --profile router up -d mcp-router

# 6. Verify prose retrieval end to end:
curl http://127.0.0.1:8010/livez   # router is up
#    then ask_docs a prose question from your MCP client: a cited,
#    corpus-backed answer means the mount landed; the degraded hint shown
#    under "This image is the exact-API-lookup deployment" means it did not.
```

No CI job publishes a RAG tag; this is a supported local build. The default
overlay declares no host mounts at all, because the default image has no code
that could open one. The `redis`/`ollama` services in `docker-compose.yml` are
infrastructure for this build and for running from source — start them
deliberately (drop `mcp-router` from the command above, or `up -d redis`), not
because the router needs them.

#### Nothing is ever downloaded for you

Neither the `Dockerfile` build, nor `docker/entrypoint.sh`, nor
`docker-compose.router.yml` fetches an index at build or at container start.
If you host your own archive internally, package it with
`scripts/package_indexes.py` — which emits the checksum manifest — and restore
it with the manifest *you* generated, or with the `spec-index-manifest.json`
published alongside a release:

```bash
# Optional, and run on the host: /app/data inside the container is read-only
# (see below), so a restore has nowhere to land there. The pinned digest is
# verified before anything is unpacked, and there is no default URL --
# --manifest or --url is required.
uv run python scripts/download_indexes.py --manifest your-bundle.json

# Then restart the router so a RAG-enabled image picks up the new files:
docker compose -f docker-compose.yml -f docker-compose.router.yml \
  --profile router restart mcp-router
```

`scripts/download_indexes.py` refuses non-HTTPS URLs, verifies a SHA-256
digest independently of any downloaded `.sha256` sidecar when a pinned
manifest is used, rejects path-traversal/symlink members inside the archive,
and swaps the new index into place atomically — see
`tests/unit/test_download_indexes.py`. This packaging relies on those existing
guarantees rather than re-implementing them; it only decides *when* that
script runs (explicitly, never automatically).

### Non-root, minimal runtime image

* The container runs as a dedicated, non-root `mcp` user (uid/gid `10001`),
  not a shared "system" uid range.
* `/app` (application code and the `uv`-managed virtualenv) is owned by
  `root` at the top level; only `/app/state`, `/app/outputs` and the `mcp`
  user's own home directory (`/home/mcp`, used for the `uv` cache) are
  writable by the running process.
* `/app/data` is **not** writable, and that is deliberate. Unlink and rename
  are governed by the directory's permissions, not the file's, so a writable
  `/app/data` would let anything running as `mcp` delete the baked
  `specs.sqlite` and drop a different index in its place — precisely the
  substitution a read-only file mode looks like it prevents and does not. The
  directory is root-owned `0555` and the index inside it root-owned `0444`.
  The query layer opens it with `file:…?mode=ro`, so nothing legitimate needs
  write access there; verified with `lookup_api` and with LanceDB reads
  through read-only bind mounts under that locked directory. Rebuilding the
  index *inside* the container consequently needs a writable mount over
  `/app/data` or a different user — build it on the host instead.
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
* **`fastembed`'s ONNX Runtime backend** needs `libgomp1`; the image installs
  it so that a `--build-arg INSTALL_EXTRAS=ingestion` build works without
  changing the runtime stage. The default image has no `fastembed` to use it.
  If you change the base image, keep an equivalent OpenMP runtime library
  available or embeddings-backed RAG tools will fail to import in a
  RAG-enabled build.
* **Non-loopback exposure needs a real proxy.** Setting
  `ports: ["0.0.0.0:8010:8010"]` (or binding to a LAN interface) without
  putting TLS termination, auth, and network policy in front of the
  container is explicitly out of scope for this packaging and is not the
  default; see "Loopback-only exposure by default" above.
* **Prose retrieval needs both a corpus and a RAG-enabled build**, and is not
  a one-command `docker run`. `lookup_api` works out of the box because the
  image ships `data/specs.sqlite`; `ask_docs` and `search_docs` need
  `data/docs.lance`, which you build yourself
  (`uv run --extra ingestion python ingestion/ingest_docs.py`) and mount, plus an image built
  with `--build-arg INSTALL_EXTRAS=ingestion`. Setting
  `HPE_MCP_RAG_BACKEND=redis` and starting the `redis` service is not an
  alternative route: that client is in the `redis` extra and is likewise
  absent from the default image. Without one of them the router still starts
  and serves every non-RAG tool (Central/GLP/monitoring/config/ops/NAC) plus
  `lookup_api` against the baked spec index, and the prose tools report their
  remedy.
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
  --profile router up -d --build mcp-router
curl http://127.0.0.1:8010/livez
docker compose -f docker-compose.yml -f docker-compose.router.yml \
  --profile router down
```
