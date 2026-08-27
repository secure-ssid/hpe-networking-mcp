#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -f "${ROOT}/.env" ]]; then
  while IFS= read -r assignment; do
    export "${assignment}"
  done < <(uv run --project "${ROOT}" python - "${ROOT}/.env" <<'PY'
import os
import re
import sys

from dotenv import load_dotenv

env_key = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
allowed_keys = {
    "HPE_MCP_ACCESS_PROFILE",
    "HPE_MCP_PRODUCTS",
    "HPE_MCP_PRODUCT_ACCESS",
    "HPE_MCP_ALLOW_LOCAL_PRODUCT_URLS",
    "HPE_MCP_ALLOW_PLACEHOLDER_URLS",
    "HPE_MCP_DIAGRAM_ICON_DIR",
    "HPE_MCP_DIAGRAM_ALLOW_LARGE_ICONS",
    "HPE_MCP_ROUTER_MODE",
    "HPE_MCP_ROUTER_EAGER_LOAD",
    "HPE_MCP_ROUTER_WRAPPER_CACHE_TTL_SECONDS",
    "HPE_MCP_READONLY",
    "HPE_MCP_TOOLSETS",
    "HPE_MCP_RAG_BACKEND",
    "HPE_MCP_RAG_CACHE_SIZE",
    "HPE_MCP_RAG_EMBED_CACHE_SIZE",
    "HPE_MCP_RAG_PREWARM",
    "HPE_MCP_MILVUS_PATH",
    "HPE_MCP_BOUND_LISTS",
    "HPE_MCP_NORMALIZE_MACS",
    "HPE_MCP_SWITCH_GROUP_NAME",
    "HPE_MCP_EMBED_PROVIDERS",
    "HPE_MCP_NOMIC_PREFIXES",
    "HPE_MCP_ROUTER_RESPONSE_MAX_ITEMS",
    "HPE_MCP_ROUTER_RESPONSE_MAX_BYTES",
    "HPE_MCP_ROUTER_BATCH_RESPONSE_MAX_BYTES",
    "HPE_MCP_ROUTER_CURSOR_TTL_SECONDS",
    "HPE_MCP_CENTRAL_GENERATED_TOOLS",
    "HPE_MCP_GLP_GENERATED_TOOLS",
    "HPE_MCP_AOS8_GENERATED_TOOLS",
    "HPE_MCP_APSTRA_GENERATED_TOOLS",
    "HPE_MCP_CLEARPASS_GENERATED_TOOLS",
    "HPE_MCP_MIST_GENERATED_TOOLS",
    "HPE_MCP_UXI_GENERATED_TOOLS",
    "HPE_MCP_EDGECONNECT_GENERATED_TOOLS",
    "HPE_MCP_AOS8_ROLLBACK_WRITES",
    "HPE_MCP_AOS8_MIGRATION_STATE_DIR",
    "HPE_MCP_GLP_V2BETA1_WRITES",
    "HPE_MCP_CENTRAL_WRITES",
    "HPE_MCP_AOS8_WRITES",
    "HPE_MCP_EDGECONNECT_WRITES",
    "HPE_MCP_APSTRA_WRITES",
    "HPE_MCP_MIST_WRITES",
    "HPE_MCP_CLEARPASS_WRITES",
    "HPE_MCP_UXI_WRITES",
    "HPE_MCP_AXIS_WRITES",
    "HPE_MCP_TROUBLESHOOTING_API_VERSION",
    "HPE_MCP_TOKENIZE_SECRETS",
    "HPE_MCP_TOKENIZE_PII",
    "HPE_MCP_AUDIT_LOG",
    "HPE_MCP_METRICS",
    "HPE_MCP_METRICS_HTTP",
    "HPE_MCP_ALLOW_INSECURE_HTTP_BINDING",
    "MCP_HOST",
    "MCP_PORT",
    "MCP_ALLOWED_HOSTS",
    "MCP_ALLOWED_ORIGINS",
    "MCP_DNS_REBINDING_PROTECTION",
    "MCP_HTTP_BEARER_TOKEN",
    "CLEARPASS_BASE_URL",
    "CLEARPASS_API_TOKEN",
    "MIST_HOST",
    "MIST_API_TOKEN",
    "APSTRA_BASE_URL",
    "APSTRA_USERNAME",
    "APSTRA_PASSWORD",
    "APSTRA_API_TOKEN",
    "AOS8_BASE_URL",
    "AOS8_USERNAME",
    "AOS8_PASSWORD",
    "AOS8_API_TOKEN",
    "EDGECONNECT_BASE_URL",
    "EDGECONNECT_API_TOKEN",
    "EDGECONNECT_AUTH_HEADER",
    "UXI_CLIENT_ID",
    "UXI_CLIENT_SECRET",
    "UXI_BASE_URL",
    "UXI_TOKEN_URL",
    "AXIS_BASE_URL",
    "AXIS_API_TOKEN",
    "GLP_GENERATED_REGION",
}
inherited_keys = set(os.environ)
load_dotenv(sys.argv[1], override=False)
legacy_prefix = "CENTRALMCP_"
legacy_keys = set()
with open(sys.argv[1], encoding="utf-8") as env_file:
    for line in env_file:
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1).startswith(legacy_prefix):
            legacy_keys.add(match.group(1))
if legacy_keys:
    print(
        "WARNING: ignored legacy environment keys in .env; use the HPE_MCP_* "
        "prefix: " + ", ".join(sorted(legacy_keys)),
        file=sys.stderr,
    )
for key in sorted(allowed_keys):
    value = os.environ.get(key)
    if (
        value is not None
        and "\n" not in value
        and "\r" not in value
        and env_key.match(key)
        and key not in inherited_keys
    ):
        print(f"{key}={value}")
PY
  )
fi

normalize_access_profile() {
  printf '%s' "$1" \
    | LC_ALL=C tr '[:upper:]' '[:lower:]' \
    | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

# Banner honesty: the default image ships no prose-RAG backend (the
# `ingestion` extra is opt-in at build time), so its toolsets line must not
# advertise bare `rag` without saying so. HPE_MCP_IMAGE_EXTRAS is baked into
# the runtime stage by the Dockerfile; when it is unset we are a source
# checkout, where the operator manages extras directly and the plain
# toolsets list stands.
rag_toolsets_note() {
  if [[ -z "${HPE_MCP_IMAGE_EXTRAS+x}" ]]; then
    return 0
  fi
  # Normalize before matching: the Dockerfile documents space-separated
  # extras ("ingestion redis"), while comma lists ("ingestion,redis")
  # build too -- uv splits commas into repeated --extra flags.
  case " ${HPE_MCP_IMAGE_EXTRAS//,/ } " in
    *" ingestion "*) ;;
    *)
      printf ' (prose-RAG backend NOT installed: rebuild with --build-arg INSTALL_EXTRAS=ingestion)'
      ;;
  esac
}

export MCP_TRANSPORT="${MCP_TRANSPORT:-streamable-http}"
export MCP_HOST="${MCP_HOST:-127.0.0.1}"
export MCP_PORT="${MCP_PORT:-8010}"
export HPE_MCP_ROUTER_MODE="${HPE_MCP_ROUTER_MODE:-minimal}"
export HPE_MCP_TOOLSETS="${HPE_MCP_TOOLSETS:-central,glp,rag}"
HPE_MCP_ACCESS_PROFILE="$(
  normalize_access_profile "${HPE_MCP_ACCESS_PROFILE:-safe-read-only}"
)"
export HPE_MCP_ACCESS_PROFILE="${HPE_MCP_ACCESS_PROFILE:-safe-read-only}"
if [[ "${HPE_MCP_ACCESS_PROFILE}" == "safe-read-only" ]]; then
  export HPE_MCP_READONLY="${HPE_MCP_READONLY:-1}"
fi
if [[ "${HPE_MCP_ACCESS_PROFILE}" == "full-read-write" ]]; then
  default_product_access="read-write"
else
  default_product_access="read-only"
fi
export HPE_MCP_PRODUCT_ACCESS="${HPE_MCP_PRODUCT_ACCESS:-${default_product_access}}"

case "${MCP_HOST}" in
  127.0.0.1|localhost|::1) ;;
  *)
    {
      echo "WARNING: MCP_HOST=${MCP_HOST} is not loopback."
      echo "Credential-backed MCP tools may be reachable from the network; protect with firewall/auth/TLS."
      echo "The server itself will refuse to start unless MCP_ALLOWED_HOSTS and"
      echo "MCP_ALLOWED_ORIGINS are both set explicitly (no wildcard) -- see"
      echo "src/hpe_networking_mcp/mcp_servers/shared.py's UnsafeHttpBindingError. Set MCP_HTTP_BEARER_TOKEN"
      echo "too if this endpoint is reachable by anything other than a trusted proxy."
      echo
    } >&2
    ;;
esac

port_is_listening() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"${MCP_PORT}" -sTCP:LISTEN >/dev/null 2>&1
    return
  fi

  if command -v python3 >/dev/null 2>&1; then
    python3 - "${MCP_HOST}" "${MCP_PORT}" <<'PY'
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])
target = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
for family, socktype, proto, _, sockaddr in socket.getaddrinfo(target, port, type=socket.SOCK_STREAM):
    with socket.socket(family, socktype, proto) as sock:
        sock.settimeout(0.25)
        if sock.connect_ex(sockaddr) == 0:
            sys.exit(0)
sys.exit(1)
PY
    return
  fi

  return 1
}

if port_is_listening; then
  {
    echo "Port ${MCP_PORT} is already in use; not starting another router."
    if command -v lsof >/dev/null 2>&1; then
      lsof -nP -iTCP:"${MCP_PORT}" -sTCP:LISTEN
    else
      echo "A TCP listener accepted connections on ${MCP_HOST}:${MCP_PORT}."
    fi
    echo
    echo "Stop the existing listener with: kill <PID>"
  } >&2
  exit 1
fi

if [[ -n "${MCP_HTTP_BEARER_TOKEN:-}" ]]; then
  bearer_status="enabled (Authorization: Bearer <token> required on /mcp)"
else
  bearer_status="disabled (set MCP_HTTP_BEARER_TOKEN to require a shared secret)"
fi

cat <<EOF
Starting hpe-networking-mcp HTTP router
  endpoint: http://${MCP_HOST}:${MCP_PORT}/mcp
  health:   http://${MCP_HOST}:${MCP_PORT}/livez, /readyz, /healthz (no auth, no MCP negotiation)
  mode:     ${HPE_MCP_ROUTER_MODE}
  toolsets: ${HPE_MCP_TOOLSETS}$(rag_toolsets_note)
  products: ${HPE_MCP_PRODUCTS:-none}
  profile:  ${HPE_MCP_ACCESS_PROFILE}
  optional: ${HPE_MCP_PRODUCT_ACCESS}
  bearer:   ${bearer_status}
  metrics:  ${HPE_MCP_METRICS:-0} (http snapshot: ${HPE_MCP_METRICS_HTTP:-0})
  audit:    ${HPE_MCP_AUDIT_LOG:-0}

Foreground stop: Ctrl-C
Background stop:
  lsof -nP -iTCP:${MCP_PORT} -sTCP:LISTEN
  kill <PID>
EOF

cd "${ROOT}"
exec uv run hpe-mcp-router
