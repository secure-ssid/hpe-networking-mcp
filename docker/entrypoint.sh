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
# scripts/run_http_router.sh. One exception below: a variable set to the
# EMPTY string beside its own *_FILE hint is treated as a misconfiguration
# and aborts startup instead of silently disabling whatever it guards.
set -euo pipefail

_BRIDGE_RE='^(MCP_HTTP_BEARER_TOKEN|[A-Z0-9_]+_(API_TOKEN|CLIENT_SECRET|PASSWORD|SESSION_COOKIE|CSRF_TOKEN))$'

for file_var in $(compgen -e | grep -E '_FILE$' || true); do
    base_var="${file_var%_FILE}"
    secret_path="${!file_var}"
    # An explicitly-set non-empty value always wins over a *_FILE hint --
    # the same precedence the Python config layer documents.
    if [[ -n "${!base_var:-}" ]]; then
        continue
    fi
    # A set-but-EMPTY plain variable beside its own _FILE twin is never a
    # deliberate configuration: the precedence rule above would keep the
    # empty string, every consumer reads that as "unset", and whatever the
    # secret guards silently turns off (a set-but-empty MCP_HTTP_BEARER_TOKEN
    # is exactly a router serving /mcp with no authentication). Refusing to
    # start beats a container that comes up looking healthy and auth-free.
    # This fires before family recognition on purpose: an unrecognized pair
    # is still a misconfiguration worth stopping for.
    if [[ -n "${!base_var+set}" && -n "${secret_path}" ]]; then
        echo "entrypoint: ${base_var} is set but EMPTY while ${file_var}=${secret_path} is also set; refusing to start" >&2
        echo "entrypoint: the empty ${base_var} would silently override what ${file_var} provides -- unset ${base_var} or drop ${file_var}; exactly one of the two may carry the value." >&2
        exit 1
    fi
    # Only recognized secret-shaped variables are ever exported. Anything
    # else stays out of the process environment on principle.
    if ! [[ "${base_var}" =~ ${_BRIDGE_RE} ]]; then
        echo "entrypoint: ${file_var} is set but '${base_var}' is not a recognized secret variable; NOT exporting it (extend _BRIDGE_RE in docker/entrypoint.sh if this is a new secret family)" >&2
        continue
    fi
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

# Profile surfacing: the default image installs none of the optional
# extras, so its prose-RAG tools have no backend at all. Announce that once,
# loudly, at start rather than letting ask_docs/search_docs disappoint
# later; lookup_api is unaffected (the spec index ships in every image).
# HPE_MCP_IMAGE_EXTRAS is baked by the Dockerfile runtime stage.
if [[ -z "${HPE_MCP_IMAGE_EXTRAS:-}" ]]; then
    echo "entrypoint: prose-RAG tools disabled in this image — rebuild with --build-arg INSTALL_EXTRAS=ingestion; lookup_api unaffected" >&2
fi

exec "$@"
