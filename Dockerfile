# syntax=docker/dockerfile:1
#
# Production image for the hpe-networking-mcp streamable-HTTP router
# (`hpe-mcp-router`, `scripts/run_http_router.sh`).
#
# Security choices (see docs/production-deployment.md for the full writeup):
#   * Multi-stage build — the runtime image never contains uv's build cache,
#     the git history, or dev/test tooling.
#   * Dependencies are resolved from the committed uv.lock only
#     (`--frozen`) — no floating-version installs, no network access at
#     container start (UV_NO_SYNC below).
#   * Runs as a dedicated non-root user; the working directory is
#     read-only-friendly (state/outputs/data are the only writable paths and
#     are meant to be bind/volume mounts, not image layers).
#   * No credentials, no config/credentials.yaml, no .env, and no secrets/
#     content are ever COPYed into the image — see .dockerignore. Real
#     secrets are supplied at *runtime* via Docker secrets / env files
#     (docker-compose.router.yml, secrets/README.md).
#   * The OpenAPI spec index (data/specs.sqlite) is BUILT during the image
#     build, in a throwaway stage, from the digest-pinned OpenAPI corpus
#     committed under vendor/openapi/ — offline, no network, no scrape. Only
#     the finished database is copied into the runtime image; the ~23 MB
#     corpus never reaches a layer the final image keeps. `lookup_api`
#     therefore answers on first start with nothing to download.
#   * The RAG prose corpus (data/docs.lance) is NOT built here and is never
#     downloaded during the build or at container start. It is scraped
#     third-party vendor documentation this project has no licence to
#     redistribute, so it stays an explicit, checksum-verified, opt-in step
#     run by the operator against a manifest they supply
#     (`uv run python scripts/download_indexes.py --manifest <manifest>`)
#     — see docs/production-deployment.md. Nothing silently trusts a network
#     artifact during image build or container start.
#   * The router's own loopback-only HTTP bind default (MCP_HOST=127.0.0.1)
#     is left untouched here; MCP_HOST/MCP_ALLOWED_HOSTS/MCP_ALLOWED_ORIGINS
#     are opt-in overrides supplied by the compose overlay, not this image.

# Base images are pinned by digest and written literally, not through an ARG.
# Dependabot's Docker parser matches the tag with /(?<tag>[\w][\w.-]{0,127})/,
# which cannot match `${VAR}`, so an ARG-indirected FROM is skipped outright --
# invisible, not mis-parsed. A digest with no bump mechanism silently stops
# receiving Debian security updates, so the pin and the `docker` ecosystem in
# .github/dependabot.yml are one change; neither is correct alone. The tag is
# kept beside the digest so the line stays readable and both move together.
#
# Trivy, not Dependabot, is the detector: .github/workflows/security.yml scans
# the built image on every push and fails on fixable HIGH/CRITICAL. The monthly
# Dependabot interval is a fix-delivery cadence, not a security SLA.
#
# uv is copied into the runtime stage (run_http_router.sh execs `uv run`), so
# its own vendored Rust crates are in the image's scan surface. 0.11.x ships
# quinn-proto 0.11.14 (GHSA-4w2j-m93h-cj5j) and rustls-webpki 0.103.9
# (GHSA-82j2-j2ch-gfr8); the 0.12 line clears both. Keep this at/above 0.12.5.
FROM ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 AS uv-bin

# Builder and runtime MUST carry byte-identical references -- one interpreter
# version, one CVE surface. The ARG used to guarantee that structurally; now
# only a test does (test_docker_router_packaging.py).
FROM python:3.12-slim-bookworm@sha256:a116514e19457bcb7af7efe9c3dd0b9b71e85b317694e7882a1c52aa15a78134 AS builder
COPY --from=uv-bin /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Optional extras, off by default. The base install is what serving MCP tools
# needs: the baked spec index answers `lookup_api` with no network and no
# extras. Prose retrieval (`ask_docs`/`search_docs`) additionally needs
# LanceDB + the ONNX embedding stack *and* a corpus you built yourself, which
# cannot ship in the image -- it is scraped vendor documentation this project
# has no licence to redistribute. Paying ~700 MB of pyarrow/onnxruntime in
# every image to serve the minority who have done that ingestion run is the
# wrong default, so it is opt-in:
#
#   docker build --build-arg INSTALL_EXTRAS=ingestion -t hpe-mcp-router:rag .
#
# Space-separated for more than one (`"ingestion redis"`). No CI job publishes
# a second tag from this; it is a documented local build.
ARG INSTALL_EXTRAS=""

# Install dependencies first so this layer only invalidates when
# pyproject.toml / uv.lock / INSTALL_EXTRAS change, not on every source edit.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --no-editable \
    $(for extra in ${INSTALL_EXTRAS}; do printf -- '--extra %s ' "$extra"; done)

# Now add application source and install the project itself.
COPY README.md LICENSE ./
COPY src/ ./src/
COPY scripts/ ./scripts/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
    $(for extra in ${INSTALL_EXTRAS}; do printf -- '--extra %s ' "$extra"; done)

# The spec index is built FROM builder rather than from a bare python image:
# scripts/build_spec_index.py imports
# hpe_networking_mcp.pipeline.clients.specs_index, so it needs both the
# project source and the installed environment that `builder` already has.
# Nothing added here survives into `runtime` except the database itself.
FROM builder AS specindex

# ingestion/openapi_registry_manifest.json supplies each document's portal
# version; without it the endpoints table carries thinner `version` values
# than a local `python scripts/build_spec_index.py` produces.
COPY ingestion/openapi_registry_manifest.json ./ingestion/
COPY vendor/ ./vendor/

# Written outside /app so that runtime's `COPY --from=builder /app /app`
# cannot pick it up implicitly: the database enters the final image through
# exactly one explicit COPY, and vendor/ enters through none.
RUN mkdir -p /spec-index \
    && /app/.venv/bin/python scripts/build_spec_index.py /spec-index/specs.sqlite

# Byte-identical to the builder FROM above, by test.
FROM python:3.12-slim-bookworm@sha256:a116514e19457bcb7af7efe9c3dd0b9b71e85b317694e7882a1c52aa15a78134 AS runtime

# scripts/run_http_router.sh execs `uv run hpe-mcp-router`; keep the uv
# binary in the runtime image too (UV_NO_SYNC below makes this a pure local
# exec with no network resolution, not a real "sync").
COPY --from=uv-bin /uv /usr/local/bin/uv

# ca-certificates: outbound HTTPS to Aruba Central/GLP and (only when the
# operator explicitly opts in) scripts/download_indexes.py.
# libgomp1: required by fastembed's onnxruntime backend used by rag.py.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 mcp \
    && useradd --uid 10001 --gid mcp --create-home --home-dir /home/mcp --shell /usr/sbin/nologin mcp

WORKDIR /app

COPY --from=builder --chown=mcp:mcp /app /app

# Writable mount points. Nothing sensitive lives here by default; mount
# named volumes or bind mounts over these in production (see
# docker-compose.router.yml and docs/production-deployment.md).
#
# /app/data is deliberately NOT among them. It holds the baked spec index,
# and unlink/rename are governed by the directory's mode, not the file's --
# a writable /app/data would let anything running as mcp delete the index and
# drop a different one in its place, which is exactly the substitution a
# read-only file mode looks like it prevents and does not. Root-owned 0555.
# Docker still creates a missing bind-mount point here itself, as root, so a
# RAG-enabled build can mount a corpus at /app/data/docs.lance without the
# directory being pre-created or the parent being writable (verified).
RUN mkdir -p /app/state /app/outputs /app/data \
    && chown -R mcp:mcp /app/state /app/outputs \
    && chown root:root /app/data \
    && chmod 555 /app/data

# The prebuilt OpenAPI spec index, at the exact path
# hpe_networking_mcp.pipeline.clients.specs_index.DB_PATH resolves to here:
# repo_root() falls back to the working directory for a non-editable install,
# and WORKDIR is /app. data/ is excluded from the build context by
# .dockerignore, so this file can only come from the specindex stage — never
# from a developer's local data/ directory.
#
# Mode 0444 on a 0555 root-owned directory: the router can read its own index
# and can neither rewrite it in place nor swap it out. The query layer opens
# it with `file:...?mode=ro`, so nothing legitimate needs write access.
# Rebuilding the index inside the container therefore needs a writable mount
# over /app/data or a different user -- see docs/production-deployment.md.
COPY --from=specindex --chmod=444 /spec-index/specs.sqlite /app/data/specs.sqlite

COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# optional_deps.require() defaults its remedy to `pip install '…[extra]'`,
# which is right for a source install and wrong here: /app/.venv is uv-managed
# with UV_NO_SYNC=1, and anything pip writes dies with the container. Override
# it with the remedy that actually applies to an image. `{extra}` is
# substituted with the missing extra by optional_deps.install_remedy().
ENV HPE_MCP_INSTALL_REMEDY="rebuild the image with \
`docker build --build-arg INSTALL_EXTRAS={extra}` \
— see docs/production-deployment.md"

ENV PATH=/app/.venv/bin:$PATH \
    VIRTUAL_ENV=/app/.venv \
    HOME=/home/mcp \
    UV_NO_SYNC=1 \
    UV_CACHE_DIR=/home/mcp/.cache/uv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Documents the router's default port; does not itself publish anything.
# Loopback-only exposure to the host is enforced in compose via
# "127.0.0.1:<port>:<port>" port mappings, matching the existing
# redis/ollama services in docker-compose.yml.
EXPOSE 8010

USER mcp

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request; port=os.environ.get('MCP_PORT','8010'); urllib.request.urlopen(f'http://127.0.0.1:{port}/livez', timeout=3).read(); sys.exit(0)"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["scripts/run_http_router.sh"]
