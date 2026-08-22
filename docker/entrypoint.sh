#!/usr/bin/env bash
# Container entrypoint: bridge Docker/Compose "*_FILE" secret conventions
# onto the environment variables hpe-networking-mcp reads, then hand off to
# the requested command (default: scripts/run_http_router.sh).
#
# This intentionally does NOT touch Python MCP/ingestion logic. Bridging is
# narrow by design:
#
#   * File-path variables need no bridge at all and are passed through
#     untouched -- CREDS_PATH already accepts a path directly (see
#     src/hpe_networking_mcp/mcp_servers/shared.py), so a credentials secret
#     mounted at /run/secrets/credentials_yaml is referenced with
#     CREDS_PATH=/run/secrets/credentials_yaml.
#
#   * Literal secret *values* the code reads out of os.environ have no native
#     file form yet, so this script bridges ONLY the documented secret-shaped
#     families listed below. It never blanket-exports an arbitrary
#     "<anything>_FILE" variable into the process environment: anything
#     outside these families is skipped with a loud operator-facing message.
#     Every bridged value is also announced on stderr so an operator reading
#     container logs can see exactly which variables were filled.
#
# Bridged families (kept in lockstep with secrets/README.md section 2):
#   MCP_HTTP_BEARER_TOKEN        streamable-HTTP bearer shared secret
#   <PREFIX>_API_TOKEN           optional-product API tokens (MIST_, AXIS_, ..)
#   <PREFIX>_CLIENT_SECRET       OAuth client secrets (UXI_CLIENT_SECRET, ..)
#   <PREFIX>_PASSWORD            product login passwords (APSTRA_, AOS8_)
#   <PREFIX>_SESSION_COOKIE      session-cookie secrets (MIST_SESSION_COOKIE)
#   <PREFIX>_CSRF_TOKEN          CSRF token secrets (MIST_CSRF_TOKEN)
#
# A variable already present in the environment always wins -- this script
# only fills in unset values, matching the precedence rules documented in
# hpe_networking_mcp.pipeline.config.load_credentials and
# scripts/run_http_router.sh.
set -euo pipefail

_BRIDGE_RE='^(MCP_HTTP_BEARER_TOKEN|[A-Z0-9_]+_(API_TOKEN|CLIENT_SECRET|PASSWORD|SESSION_COOKIE|CSRF_TOKEN))$'

for file_var in $(compgen -e | grep -E '_FILE$' || true); do
    base_var="${file_var%_FILE}"
    # Skip if the plain variable is already set (even to an empty string) --
    # an explicit value always takes precedence over a *_FILE hint.
    if [[ -n "${!base_var+set}" ]]; then
        continue
    fi
    # Only recognized secret-shaped variables are ever exported. Anything
    # else stays out of the process environment on principle.
    if ! [[ "${base_var}" =~ ${_BRIDGE_RE} ]]; then
        echo "entrypoint: ${file_var} is set but '${base_var}' is not a recognized secret variable; NOT exporting it (extend _BRIDGE_RE in docker/entrypoint.sh if this is a new secret family)" >&2
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
    echo "entrypoint: filled ${base_var} from ${file_var} (${secret_path})" >&2
    export "${base_var}=${secret_value}"
done

exec "$@"
