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
#   * Prebuilt RAG/OpenAPI indexes (data/) are NOT downloaded during the
#     build or at container start. They stay an explicit, checksum-verified,
#     opt-in step run by the operator against a manifest they supply
#     (`uv run python scripts/download_indexes.py --manifest <your-manifest>`)
#     — see docs/production-deployment.md. This repository publishes no index
#     archive, so there is no tracked manifest to bake in, and nothing
#     silently trusts a network artifact during image build/startup.
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
