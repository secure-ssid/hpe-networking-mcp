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

ARG UV_VERSION=0.11.2
ARG PYTHON_VERSION=3.12-slim-bookworm

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv-bin

FROM python:${PYTHON_VERSION} AS builder
COPY --from=uv-bin /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Install dependencies first so this layer only invalidates when
# pyproject.toml / uv.lock change, not on every source edit.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --no-editable

# Now add application source and install the project itself.
COPY README.md LICENSE ./
COPY src/ ./src/
COPY scripts/ ./scripts/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

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

FROM python:${PYTHON_VERSION} AS runtime

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
RUN mkdir -p /app/state /app/outputs /app/data \
    && chown -R mcp:mcp /app/state /app/outputs /app/data

# The prebuilt OpenAPI spec index, at the exact path
# hpe_networking_mcp.pipeline.clients.specs_index.DB_PATH resolves to here:
# repo_root() falls back to the working directory for a non-editable install,
# and WORKDIR is /app. data/ is excluded from the build context by
# .dockerignore, so this file can only come from the specindex stage — never
# from a developer's local data/ directory.
COPY --from=specindex --chown=mcp:mcp /spec-index/specs.sqlite /app/data/specs.sqlite

COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/entrypoint.sh

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
