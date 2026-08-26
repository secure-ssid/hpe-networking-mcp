---
title: "Docker deployment"
nav_order: 7
---

# Docker deployment

One ordered path from nothing to a running, credentialed router:
**checkout → wizard → start → verify**. Everything else on this page is a
variation on those four steps. Containerizing is an alternative to the local
`uv run` / stdio workflow in [getting-started.md](getting-started.md), not a
later stage of it.

<div class="docs-callout docs-callout--safe" markdown="1">
Nothing here is required. The router runs perfectly well with a plain
`uv run hpe-mcp-router` on a laptop. Containerizing it only matters once
you're running the router unattended (a shared dev box, a small VM, a
Kubernetes pod) where the same non-root, no-secrets-in-the-image, no-silent
network fetch expectations from local development should still hold.
</div>

## 1. Get a checkout

```bash
git clone https://github.com/secure-ssid/hpe-networking-mcp
cd hpe-networking-mcp
```

The image builds from here. The spec index (2,700+ endpoints across 31
pinned OpenAPI documents) is baked in at build time, so there is nothing to
populate first.

## 2. Run the wizard

```bash
python3 scripts/setup_wizard.py --docker
```

It asks everything the deployment needs in one pass: which toolsets to load,
which optional products **and their credentials**, read-only or read/write
per platform, whether to use the RAG image and which vector backend, and
whether to publish beyond loopback. Accepting the recommended defaults at
the first question skips to credential capture — loopback-only, Central +
GreenLake + API lookup, read-only, no optional products.

Every answer is also a flag, for scripted or repeatable runs:

```bash
python3 scripts/setup_wizard.py --docker --yes \
  --toolsets central,glp,rag,mist \
  --products mist \
  --access-profile custom --product-access read-only
```

| Flag | Effect |
|---|---|
| `--yes` | take every default, prompt for nothing |
| `--toolsets a,b,c` | backend families to load (`HPE_MCP_TOOLSETS`); default `central,glp,rag` |
| `--products a,b` | optional products to enable, or `all`; unioned onto the toolsets |
| `--access-profile` | `safe-read-only`, `custom` (per-platform, the default), or `full-read-write` |
| `--product-access` | `read-only` or `read-write` for optional products under `custom` |
| `--router-mode` | `minimal` discovery, `default` wrappers, or `direct` registration |
| `--expose IP --expose IP` | publish beyond loopback; must be passed twice with the same value to acknowledge it |
| `--force` | rotate secrets and regenerate the overlay instead of keeping existing files |

It writes the following, all git-ignored. A rerun keeps existing files
unless `--force` is passed:

* `secrets/mcp_http_bearer_token` — a fresh 64-hex token, mode 0600, whose
  value is never printed;
* `secrets/credentials.yaml` — Central/GreenLake **identity**: base URLs,
  client ids, workspace ids. No `client_secret` keys;
* `secrets/central_client_secret`, `secrets/glp_client_secret` and one file
  per selected product credential (`secrets/mist_api_token`, …), each 0600;
* `docker-compose.router.local.yml` — a generated overlay layering over
  `docker-compose.yml`: a literal `127.0.0.1:<port>:<port>` publish line,
  hostname-derived allowlists, your toolset and product selection, one
  Compose secret per credential file, and every write gate defaulted to
  refused;
* `.env` — non-secret knobs only: router mode, toolsets, products, access
  profile, the nine per-platform write gates, and `HPE_MCP_RAG_BACKEND` when
  the redis backend was chosen. Secret values never land here; if the file
  already holds secret-shaped or credential-affecting keys the wizard warns
  listing them and leaves them byte-for-byte alone.

## 3. Start it

```bash
docker compose -f docker-compose.yml -f docker-compose.router.local.yml \
  --profile router up -d --build mcp-router
```

Naming `mcp-router` matters: `redis` and `ollama` sit in
`docker-compose.yml`'s default profile, so omitting the service name would
also start two containers the default image has no client for. If you chose
the redis RAG backend, start both of them — `... up -d mcp-router redis
ollama` — because that path keeps its vectors in redis and embeds each query
through ollama; the generated overlay declares `depends_on` and points
`REDIS_URL`/`OLLAMA_URL` at those services.

A plain `docker compose up` (no `-f docker-compose.router.local.yml`, no
`--profile router`) still starts only `redis`/`ollama`, exactly as before
this overlay existed.

## 4. Verify

```bash
curl http://127.0.0.1:8010/livez
# {"status":"ok"}
```

To confirm your selection actually reached the router rather than just the
host, ask the container what it loaded:

```bash
docker compose -f docker-compose.yml -f docker-compose.router.local.yml \
  exec -T mcp-router python -c \
  "from hpe_networking_mcp.mcp_servers.tool_router import _build_backends; print(sorted(_build_backends()))"
```

Each selected product appears as its own `<name>-core` backend. The
entrypoint also logs one line per credential it bridged
(`entrypoint: filled MIST_API_TOKEN from MIST_API_TOKEN_FILE ...`), visible
with `docker compose ... logs mcp-router`.

## Secrets: one value, one file

Every credential is its own 0600 file under `secrets/`, mounted as its own
Compose secret and read through the `<VAR>_FILE` → `<VAR>` bridge in
[`docker/entrypoint.sh`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/docker/entrypoint.sh). Nothing credential-shaped
is ever passed as a plaintext `environment:` value or written to `.env`.

That layout exists for rotation. Revoking one product's key is:

```bash
printf '%s' "$NEW_TOKEN" > secrets/mist_api_token
docker compose -f docker-compose.yml -f docker-compose.router.local.yml \
  restart mcp-router
```

No other credential is read, rewritten, or re-exposed — which is exactly
what a single shared `.env` cannot give you. A plain `restart` is enough
here: the Compose secret is a live mount of the host file, and the
entrypoint re-reads it every time the container starts.

Changing a **non-secret knob** in `.env` is the other case, and it needs
`... --profile router up -d mcp-router` rather than `restart` — Compose bakes
interpolated values into the container when it is created, so a restarted
container keeps the values it was built with.

Two paths, one for each kind of value:

| Value | Where it lives | How the container reads it |
|---|---|---|
| Central/GreenLake identity (base URLs, client ids, workspace ids) | `secrets/credentials.yaml` | mounted at `/run/secrets/credentials_yaml`, named by `CREDS_PATH` |
| Any single secret (client secrets, product API tokens, the bearer token) | `secrets/<name>`, one file each | `<VAR>_FILE=/run/secrets/<name>`, bridged by the entrypoint |

`config/credentials.yaml` is the *host* path used by the local `uv run`
workflow and keeps carrying secrets inline; `secrets/credentials.yaml` is the
*container* path and holds identity only. They are separate files on purpose:
`secrets/` is git-ignored wholesale and is what Compose mounts.

[`secrets/README.md`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/secrets/README.md) has the copyable Compose
snippet for wiring a secret by hand.

## Without the wizard

The tracked [`docker-compose.router.yml`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/docker-compose.router.yml)
is the same stack with the same knobs, minus the generated per-product
secret wiring (Compose refuses to start when a declared secret's file is
missing, so it declares only the two every deployment creates):

```bash
cp config/credentials.yaml.example secrets/credentials.yaml
# edit it with real Central/GLP client id/secret values
openssl rand -hex 32 > secrets/mcp_http_bearer_token
chmod 600 secrets/credentials.yaml secrets/mcp_http_bearer_token

docker compose -f docker-compose.yml -f docker-compose.router.yml \
  --profile router up -d --build mcp-router
curl http://127.0.0.1:8010/livez
```

Set `HPE_MCP_TOOLSETS`, `HPE_MCP_PRODUCTS`, `HPE_MCP_PRODUCT_ACCESS` and the
`HPE_MCP_*_WRITES` gates in `.env` to change what it loads, then re-run
`up -d` to apply them; every one of those variables is interpolated by that
file and defaults to the refusing value. To add an
optional product's credential, follow the snippet in
[`secrets/README.md`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/secrets/README.md).

## Kicking the tyres: the published image

<div class="docs-callout docs-callout--note" markdown="1">
No checkout, no credentials, no persistence — a look at the tool surface
only. It is not the deployment path above.
</div>

CI publishes every build to GHCR under a `sha-<short-sha>` tag (first seven
characters of the commit SHA) and promotes that exact digest to `latest`
(builds from `main`) or the matching semver tags (`v*` releases) only after
the Trivy policy passes — so `latest` always points at scan-approved bytes,
and `sha-<short-sha>` pins one build exactly:

```bash
docker run -d --name hpe-networking-mcp \
  -p 127.0.0.1:8010:8010 \
  -e MCP_HOST=0.0.0.0 \
  -e MCP_ALLOWED_HOSTS='127.0.0.1:*,localhost:*' \
  -e MCP_ALLOWED_ORIGINS='http://127.0.0.1:*,http://localhost:*' \
  ghcr.io/secure-ssid/hpe-networking-mcp:latest
```

`curl http://127.0.0.1:8010/livez` answers `{"status":"ok"}` within seconds.
The baked spec index makes credential-free exact-API lookup (`lookup_api`)
work with no provisioning. It does **no** prose retrieval: that needs the
`INSTALL_EXTRAS=ingestion` rebuild and a corpus, per
[Building a RAG-capable image](#building-a-rag-capable-image) below. The
`host:*` allowlist form is required whenever `MCP_HOST` is not loopback (see
[Loopback-only exposure by default](#loopback-only-exposure-by-default)).

## Files

| File | Purpose |
|---|---|
| [`Dockerfile`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/Dockerfile) | Multi-stage production image for the router (`hpe-mcp-router`) |
| [`.dockerignore`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/.dockerignore) | Keeps secrets, `.env`, local state, and built indexes out of the build context |
| [`docker/entrypoint.sh`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/docker/entrypoint.sh) | Expands `*_FILE` Docker-secret conventions into plain env vars, then execs the requested command |
| [`docker-compose.yml`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/docker-compose.yml) | Unchanged: optional localhost-only Redis/Ollama server backend |
| [`docker-compose.router.yml`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/docker-compose.router.yml) | Additive overlay: the containerized router, behind a Compose `router` profile |
| [`scripts/setup_wizard.py`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/scripts/setup_wizard.py) | `--docker` generates the secrets, `.env` and `docker-compose.router.local.yml` above |
| [`secrets/README.md`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/secrets/README.md) | The `CREDS_PATH` and `<VAR>_FILE` secret conventions, with copyable Compose wiring |


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
    `<VAR>_FILE` convention used by many official images. A non-empty
    `<VAR>` wins over its `<VAR>_FILE` counterpart; a set-but-empty
    `<VAR>` beside its counterpart is a misconfiguration and refuses
    startup.
* See [`secrets/README.md`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/secrets/README.md) for the full setup
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
uv run --extra ingestion python scripts/refresh_rag_sources.py --refresh-sources

# 2. Build the LanceDB corpus from what step 1 fetched:
uv run --extra ingestion python ingestion/ingest_docs.py

# 3. Build an image that can read it. `ingestion` gets the embedded LanceDB
#    backend, which needs no services. For the Redis backend instead, use
#    INSTALL_EXTRAS=redis and set HPE_MCP_RAG_BACKEND=redis; `all` gets both.
docker build --build-arg INSTALL_EXTRAS=ingestion \
  -t hpe-networking-mcp-router:rag .

# 4. Provision the two secrets the overlay mounts, exactly as in "Without the
#    wizard" above: secrets/credentials.yaml and secrets/mcp_http_bearer_token,
#    chmod 600, never committed. Compose cannot start the service without
#    them. (`setup_wizard.py --docker` does steps 4-5 for you, and its
#    generated overlay carries the INSTALL_EXTRAS build arg, so `--build` is
#    safe there.)

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
