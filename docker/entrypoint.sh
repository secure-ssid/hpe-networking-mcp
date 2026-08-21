#!/usr/bin/env bash
# Container entrypoint: expand Docker/Compose "*_FILE" secret conventions
# into the plain environment variables hpe-networking-mcp already reads,
# then hand off to the requested command (default: scripts/run_http_router.sh).
#
# This intentionally does NOT touch Python MCP/ingestion logic. It only
# bridges the standard "mount a secret file, point an env var at it" pattern
# (as used by official images such as postgres's POSTGRES_PASSWORD_FILE) onto
# environment variables the router already supports:
#
#   * CREDS_PATH already accepts a file path directly (see
#     src/hpe_networking_mcp/mcp_servers/shared.py), so a Docker secret
#     mounted at /run/secrets/credentials_yaml can be referenced with
#     CREDS_PATH=/run/secrets/credentials_yaml with no extra handling here.
#   * Literal secret *values* the code reads straight out of the process
#     environment (MCP_HTTP_BEARER_TOKEN, the optional-product API
#     tokens/secrets, etc.) have no file-path form, so this script reads a
#     same-named "<VAR>_FILE" variable's file contents into "<VAR>" when
#     "<VAR>" itself is not already set.
#
# A variable already present in the environment always wins -- this script
# only *fills in* values, matching the precedence rules documented in
# hpe_networking_mcp.pipeline.config.load_credentials and
# scripts/run_http_router.sh.
set -euo pipefail

for file_var in $(compgen -e | grep -E '_FILE$' || true); do
    base_var="${file_var%_FILE}"
    # Skip if the plain variable is already set (even to an empty string) --
    # an explicit value always takes precedence over a *_FILE hint.
    if [[ -n "${!base_var+set}" ]]; then
        continue
    fi
    secret_path="${!file_var}"
    if [[ -z "${secret_path}" ]]; then
        continue
    fi
    if [[ ! -f "${secret_path}" ]]; then
        echo "entrypoint: ${file_var}=${secret_path} does not exist or is not a regular file; skipping" >&2
        continue
    fi
    # Secrets must not contain embedded newlines; strip a single trailing
    # newline the way `docker secret create`/`echo` commonly leave behind.
    secret_value="$(cat -- "${secret_path}")"
    export "${base_var}=${secret_value}"
done

exec "$@"
