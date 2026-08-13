#!/usr/bin/env python3
"""Check local hpe-networking-mcp setup without making network or API calls."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import shlex
import shutil
import socket
import stat
import sys
from dataclasses import dataclass
from pathlib import Path


def _resolve_root() -> Path:
    """Repo root to diagnose: the source checkout, else the working directory.

    A source checkout resolves relative to this module. An installed wheel has
    no repo layout around it, so ``hpe-mcp-doctor`` falls back to the current
    working directory -- which is where a user's ``config/``, ``data/`` and
    ``.mcp.json`` actually live.
    """
    checkout_root = Path(__file__).resolve().parents[3]
    if (checkout_root / "pyproject.toml").is_file():
        return checkout_root
    return Path.cwd()


ROOT = _resolve_root()
DEFAULT_HTTP_PORT = 8010
OPTIONAL_PRODUCT_ENVS = {
    "clearpass": ("CLEARPASS_BASE_URL", "CLEARPASS_API_TOKEN"),
    "mist": ("MIST_HOST", "MIST_API_TOKEN"),
    "apstra": ("APSTRA_BASE_URL", "APSTRA_API_TOKEN"),
    "aos8": ("AOS8_BASE_URL", "AOS8_API_TOKEN"),
    "edgeconnect": ("EDGECONNECT_BASE_URL", "EDGECONNECT_API_TOKEN"),
    "uxi": ("UXI_CLIENT_ID", "UXI_CLIENT_SECRET"),
    "axis": ("AXIS_BASE_URL", "AXIS_API_TOKEN"),
    "design": (),  # local diagram generation; no vendor credentials
}
PLACEHOLDER_MARKERS = ("YOUR_", "REPLACE_ME", "PLACEHOLDER")
READ_ONLY_PRODUCT_ACCESS_VALUES = {"read-only", "readonly", "read_only", "ro"}
READ_WRITE_PRODUCT_ACCESS_VALUES = {"read-write", "readwrite", "read_write", "rw"}

# Mirrors hpe_networking_mcp.mcp_servers.tool_router's toolset/backend maps and
# hpe_networking_mcp.mcp_servers.shared.resolve_rag_backend's valid set.
# Duplicated here (rather than imported) so `hpe-mcp-doctor` keeps working
# -- reporting *why* the real server would refuse to start -- even when the
# `mcp`/`lancedb`/`fastembed` dependencies those modules import at module
# scope are not yet installed (doctor's own job is partly to notice that).
# Keep in sync with hpe_networking_mcp/mcp_servers/tool_router.py's
# _TOOLSET_BACKENDS/_OPTIONAL_BACKENDS and shared.py's _VALID_RAG_BACKENDS.
VALID_TOOLSETS = {
    "config",
    "monitoring",
    "nac",
    "ops",
    "glp",
    "rag",
    "central",
    "central-generated",
    "interop",
    "clearpass",
    "mist",
    "apstra",
    "aos8",
    "edgeconnect",
    "uxi",
    "axis",
    "design",
    "all",
}
VALID_RAG_BACKENDS = {"lancedb", "redis"}


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    detail: str


def _status_line(check: Check) -> str:
    return f"[{check.status}] {check.name}: {check.detail}"


def _command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _path_check(path: Path, name: str, *, missing_detail: str) -> Check:
    if path.exists():
        return Check("OK", name, f"{_display_path(path)} exists")
    return Check("WARN", name, missing_detail)


def _is_supported_sdk_wildcard(value: str) -> bool:
    """True for the one wildcard shape the installed MCP SDK's
    ``TransportSecurityMiddleware`` actually matches: a non-empty host/
    origin followed by a literal ``:*`` port suffix. Mirrors
    ``hpe_networking_mcp.mcp_servers.shared._is_supported_sdk_wildcard`` --
    duplicated (not imported) for the same import-safety reason as
    ``VALID_TOOLSETS`` above."""
    stripped = value.strip()
    if not stripped.endswith(":*"):
        return False
    base = stripped[: -len(":*")]
    return bool(base) and "*" not in base


def _has_unsupported_wildcard(values: list[str]) -> bool:
    return any("*" in value and not _is_supported_sdk_wildcard(value) for value in values)


def _permission_check(path: Path, name: str) -> Check | None:
    """Warn/fail on overly permissive local secret/config file permissions.

    Returns ``None`` (nothing to report) when the file does not exist --
    absence is already reported by a separate existence check -- or on a
    non-POSIX platform, where ``st_mode``'s owner/group/other bits are not
    meaningful in the same way and would otherwise produce a constant
    false-positive WARN. World-writable escalates to FAIL (any local
    account could tamper with credentials/config); group- or
    world-readable (but not writable) is a WARN (a local secret is exposed
    to other accounts on a shared host).
    """
    if os.name != "posix" or not path.exists():
        return None
    try:
        mode = path.stat().st_mode
    except OSError:
        return None
    if mode & stat.S_IWOTH:
        return Check(
            "FAIL",
            f"{name} permissions",
            f"{_display_path(path)} is world-writable; run `chmod 600 {path.name}`",
        )
    if mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH):
        return Check(
            "WARN",
            f"{name} permissions",
            f"{_display_path(path)} is readable by group/others; run `chmod 600 {path.name}`",
        )
    return Check(
        "OK", f"{name} permissions", f"{_display_path(path)} is not group/world readable"
    )


def _has_placeholders(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(errors="replace")
    return any(marker in text for marker in PLACEHOLDER_MARKERS)


def _is_placeholder_value(value: str) -> bool:
    return any(marker in value for marker in PLACEHOLDER_MARKERS)


def _load_json(path: Path) -> tuple[dict[str, object] | None, str | None]:
    try:
        data = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "top-level JSON value must be an object"
    return data, None


def _load_local_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key or key in os.environ:
            continue
        try:
            parsed = shlex.split(value, posix=True)
        except ValueError:
            continue
        os.environ[key] = parsed[0] if len(parsed) == 1 else value.strip()


def _server_map(data: dict[str, object]) -> dict[str, object] | None:
    servers = data.get("mcpServers") or data.get("servers")
    return servers if isinstance(servers, dict) else None


def _router_server(data: dict[str, object]) -> dict[str, object] | None:
    servers = _server_map(data)
    if servers is None:
        return None
    server = servers.get("hpe-networking-mcp")
    return server if isinstance(server, dict) else None


def _router_env_checks(data: dict[str, object]) -> list[Check]:
    server = _router_server(data)
    if server is None:
        return [
            Check(
                "WARN",
                "Local stdio router profile",
                "missing hpe-networking-mcp router server entry",
            )
        ]

    env = server.get("env")
    if not isinstance(env, dict):
        return [Check("WARN", "Local stdio router profile", "missing env object")]

    mode = env.get("HPE_MCP_ROUTER_MODE")
    toolsets = env.get("HPE_MCP_TOOLSETS")
    products = env.get("HPE_MCP_PRODUCTS")
    valid_modes = {"minimal", "default", "direct"}
    checks = [
        Check(
            "OK" if mode in valid_modes else "WARN",
            "Local stdio router mode",
            f"HPE_MCP_ROUTER_MODE is {mode}"
            if mode in valid_modes
            else "set HPE_MCP_ROUTER_MODE to minimal, default, or direct",
        ),
        Check(
            "OK" if toolsets in {"central,glp,rag", "all"} else "WARN",
            "Local stdio router toolsets",
            f"HPE_MCP_TOOLSETS is {toolsets}"
            if toolsets in {"central,glp,rag", "all"}
            else "set HPE_MCP_TOOLSETS=central,glp,rag or all",
        ),
    ]
    if products:
        checks.append(
            Check(
                "OK",
                "Local stdio optional products",
                f"HPE_MCP_PRODUCTS={products!r}",
            )
        )
    return checks


def _stdio_config_checks(path: Path) -> list[Check]:
    if not path.exists():
        return []

    checks: list[Check] = []
    data, error = _load_json(path)
    checks.append(
        Check(
            "OK" if error is None else "FAIL",
            "Local stdio MCP config JSON",
            "valid JSON object" if error is None else f"invalid JSON: {error}",
        )
    )

    text = path.read_text()
    checks.append(
        Check(
            "WARN" if "/path/to/hpe-networking-mcp" in text else "OK",
            "Local stdio MCP config paths",
            "replace /path/to/hpe-networking-mcp placeholders"
            if "/path/to/hpe-networking-mcp" in text
            else "no example placeholders found",
        )
    )
    if data is not None:
        checks.extend(_router_env_checks(data))
    return checks


def _http_config_checks(path: Path, host: str, port: int) -> list[Check]:
    if not path.exists():
        return []

    checks: list[Check] = []
    data, error = _load_json(path)
    checks.append(
        Check(
            "OK" if error is None else "FAIL",
            "Local HTTP MCP config JSON",
            "valid JSON object" if error is None else f"invalid JSON: {error}",
        )
    )
    if data is None:
        return checks

    servers = _server_map(data)
    if servers is None:
        return [
            *checks,
            Check("WARN", "Local HTTP MCP config URL", "missing mcpServers object"),
        ]

    urls = [
        server.get("url")
        for server in servers.values()
        if isinstance(server, dict) and isinstance(server.get("url"), str)
    ]
    if not urls:
        return [
            *checks,
            Check("WARN", "Local HTTP MCP config URL", "no server URL found"),
        ]

    expected = f"http://{host}:{port}/mcp"
    status = "OK" if expected in urls else "WARN"
    detail = f"matches {expected}" if status == "OK" else f"expected {expected}"
    checks.append(Check(status, "Local HTTP MCP config URL", detail))

    transports = [
        server.get("transport")
        for server in servers.values()
        if isinstance(server, dict) and isinstance(server.get("transport"), str)
    ]
    checks.append(
        Check(
            "OK" if "streamable-http" in transports else "WARN",
            "Local HTTP MCP transport",
            "transport is streamable-http"
            if "streamable-http" in transports
            else "set transport to streamable-http",
        )
    )
    return checks


def _csv_values(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip().lower() for item in value.split(",") if item.strip()]


def _product_access_check(value: str | None) -> Check:
    if value is None:
        return Check(
            "OK",
            "Optional product access",
            "unset; optional product writes default to read-only",
        )
    normalized = value.strip().lower()
    if normalized in READ_ONLY_PRODUCT_ACCESS_VALUES:
        return Check("OK", "Optional product access", "read-only")
    if normalized in READ_WRITE_PRODUCT_ACCESS_VALUES:
        return Check("OK", "Optional product access", "read-write")
    return Check(
        "WARN",
        "Optional product access",
        f"unrecognized HPE_MCP_PRODUCT_ACCESS={value!r}; optional writes fail closed",
    )


def _enabled_optional_products(products: str, toolsets: str | None) -> set[str]:
    product_values = set(_csv_values(products))
    toolset_values = set(_csv_values(toolsets))
    known_products = set(OPTIONAL_PRODUCT_ENVS)

    enabled = product_values & known_products
    if "all" in toolset_values:
        enabled.update(known_products)
    else:
        enabled.update(toolset_values & known_products)
    return enabled


def _port_listening(host: str, port: int) -> bool:
    target = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    try:
        addresses = socket.getaddrinfo(target, port, type=socket.SOCK_STREAM)
    except OSError:
        return False

    for family, socktype, proto, _, sockaddr in addresses:
        with socket.socket(family, socktype, proto) as sock:
            sock.settimeout(0.25)
            if sock.connect_ex(sockaddr) == 0:
                return True
    return False


def _dependency_checks() -> list[Check]:
    uv_available = _command_exists("uv")
    checks = [
        Check(
            "OK" if sys.version_info >= (3, 10) else "FAIL",
            "Python version",
            f"{sys.version.split()[0]} detected; hpe-networking-mcp requires >=3.10",
        ),
        Check(
            "OK" if uv_available else "WARN",
            "uv",
            "uv is available"
            if uv_available
            else "uv not found; install uv or use python directly",
        ),
    ]
    for module in ("httpx", "mcp", "lancedb", "fastembed", "yaml"):
        available = _module_available(module)
        checks.append(
            Check(
                "OK" if available else "WARN",
                f"Python module {module}",
                f"{module} import spec found" if available else f"{module} missing; run `uv sync`",
            )
        )
    return checks


def _config_checks() -> list[Check]:
    creds_path = Path(os.getenv("CREDS_PATH", ROOT / "config" / "credentials.yaml"))
    if not creds_path.is_absolute():
        creds_path = ROOT / creds_path

    credentials_check = _path_check(
        creds_path,
        "Credentials",
        missing_detail=(
            f"{creds_path} missing; copy config/credentials.yaml.example to "
            "config/credentials.yaml and fill in credentials"
        ),
    )
    if credentials_check.status == "OK" and _has_placeholders(creds_path):
        credentials_check = Check(
            "WARN",
            "Credentials",
            f"{_display_path(creds_path)} exists but still contains placeholders",
        )

    checks = [
        _path_check(
            ROOT / "pyproject.toml",
            "Project metadata",
            missing_detail="pyproject.toml missing; run from a hpe-networking-mcp checkout",
        ),
        credentials_check,
        _path_check(
            ROOT / ".mcp.json.example",
            "stdio MCP example",
            missing_detail=".mcp.json.example missing",
        ),
        _path_check(
            ROOT / ".mcp.http.json.example",
            "HTTP MCP example",
            missing_detail=".mcp.http.json.example missing",
        ),
        _path_check(
            ROOT / ".claude" / "launch.json",
            "Claude launch profiles",
            missing_detail=".claude/launch.json missing",
        ),
    ]

    local_stdio = ROOT / ".mcp.json"
    local_http = ROOT / ".mcp.http.json"
    checks.append(
        Check(
            "OK" if local_stdio.exists() else "WARN",
            "Local stdio MCP config",
            ".mcp.json exists"
            if local_stdio.exists()
            else "copy .mcp.json.example to .mcp.json for local stdio clients",
        )
    )
    checks.extend(_stdio_config_checks(local_stdio))
    checks.append(
        Check(
            "OK" if local_http.exists() else "WARN",
            "Local HTTP MCP config",
            ".mcp.http.json exists"
            if local_http.exists()
            else "copy .mcp.http.json.example to .mcp.http.json for local HTTP clients",
        )
    )
    raw_port = os.getenv("MCP_PORT", str(DEFAULT_HTTP_PORT))
    try:
        port = int(raw_port)
    except ValueError:
        port = DEFAULT_HTTP_PORT
    checks.extend(_http_config_checks(local_http, os.getenv("MCP_HOST", "127.0.0.1"), port))

    # Local secret/config file permissions -- credentials, .env, and the two
    # local (git-ignored) MCP client configs can all carry live tokens.
    # World-writable is a FAIL; group/world-readable (but not writable) is a
    # WARN. Skipped (returns None) for a file that does not exist -- that is
    # already reported above -- and on non-POSIX platforms.
    permission_checks = [
        _permission_check(creds_path, "Credentials"),
        _permission_check(ROOT / ".env", ".env"),
        _permission_check(local_stdio, "Local stdio MCP config"),
        _permission_check(local_http, "Local HTTP MCP config"),
    ]
    checks.extend(check for check in permission_checks if check is not None)

    return checks


def _index_checks() -> list[Check]:
    checks = []
    tool_index = ROOT / "data" / "tools.lance"
    docs_index = ROOT / "data" / "docs.lance"
    specs_index = ROOT / "data" / "specs.sqlite"
    checks.append(
        Check(
            "OK" if tool_index.exists() else "WARN",
            "Router tool index",
            "data/tools.lance exists"
            if tool_index.exists()
            else "missing; run `uv run python scripts/ingest_tools.py --products all`",
        )
    )
    checks.append(
        Check(
            "OK" if docs_index.exists() and specs_index.exists() else "WARN",
            "Docs/API RAG indexes",
            "data/docs.lance and data/specs.sqlite exist"
            if docs_index.exists() and specs_index.exists()
            else (
                "missing or partial; run `uv run python ingestion/ingest_docs.py` "
                "if RAG lookup is needed"
            ),
        )
    )
    return checks


def _ingest_source_names() -> set[str]:
    ingest_path = ROOT / "ingestion" / "ingest_docs.py"
    tree = ast.parse(ingest_path.read_text())
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name) or target.id != "SOURCE_META":
                continue
            value = ast.literal_eval(node.value)
            if not isinstance(value, dict):
                raise ValueError("SOURCE_META must be a dictionary")
            return {str(key) for key in value}
    raise ValueError("SOURCE_META not found")


def _source_manifest_checks() -> list[Check]:
    manifest_path = ROOT / "ingestion" / "source_manifest.json"
    if not manifest_path.exists():
        return [
            Check(
                "WARN",
                "RAG source manifest",
                "missing ingestion/source_manifest.json",
            )
        ]
    try:
        data = json.loads(manifest_path.read_text())
        source_names = _ingest_source_names()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, SyntaxError) as exc:
        return [Check("FAIL", "RAG source manifest", f"cannot validate: {exc}")]
    if not isinstance(data, list):
        return [Check("FAIL", "RAG source manifest", "top-level JSON value must be a list")]

    manifest_sources = {
        str(item.get("source", "")).strip()
        for item in data
        if isinstance(item, dict) and str(item.get("source", "")).strip()
    }
    missing = sorted(source_names - manifest_sources)
    extra = sorted(manifest_sources - source_names)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if extra:
            details.append(f"extra: {', '.join(extra)}")
        return [Check("WARN", "RAG source manifest", "; ".join(details))]
    return [
        Check(
            "OK",
            "RAG source manifest",
            f"{len(manifest_sources)} sources match ingestion SOURCE_META",
        )
    ]


def _runtime_checks() -> list[Check]:
    host = os.getenv("MCP_HOST", "127.0.0.1")
    raw_port = os.getenv("MCP_PORT", str(DEFAULT_HTTP_PORT))
    try:
        port = int(raw_port)
    except ValueError:
        return [
            Check(
                "FAIL",
                "HTTP router port",
                f"MCP_PORT={raw_port!r} is not an integer",
            )
        ]
    listening = _port_listening(host, port)
    products = os.getenv("HPE_MCP_PRODUCTS", "").strip()
    product_access = os.getenv("HPE_MCP_PRODUCT_ACCESS")
    toolsets = os.getenv("HPE_MCP_TOOLSETS")
    mode = os.getenv("HPE_MCP_ROUTER_MODE")

    valid_modes = {"minimal", "default", "direct"}
    mode_detail = (
        "unset in this shell; committed MCP client examples set 'minimal'"
        if mode is None
        else f"HPE_MCP_ROUTER_MODE={mode!r}"
    )
    toolsets_detail = (
        "unset in this shell; committed MCP client examples set 'central,glp,rag'"
        if toolsets is None
        else f"HPE_MCP_TOOLSETS={toolsets!r}"
    )

    unknown_products = sorted(set(_csv_values(products)) - set(OPTIONAL_PRODUCT_ENVS))
    unknown_toolsets = sorted(set(_csv_values(toolsets)) - VALID_TOOLSETS)
    rag_backend = os.getenv("HPE_MCP_RAG_BACKEND", "").strip().lower()
    checks = [
        Check(
            "OK" if mode is None or mode in valid_modes else "WARN",
            "Router mode",
            mode_detail,
        ),
        Check(
            # A non-empty, unrecognized HPE_MCP_TOOLSETS name refuses to
            # start the real router (reject_unknown_env_choices) -- FAIL,
            # not WARN. An empty/unset value, or any combination of
            # recognized names (not just the committed "central,glp,rag"
            # default), is fine.
            "FAIL" if unknown_toolsets else "OK",
            "Router toolsets",
            toolsets_detail
            if not unknown_toolsets
            else (
                "unrecognized names will refuse to start the router: "
                f"{', '.join(unknown_toolsets)}"
            ),
        ),
        Check(
            "OK",
            "Optional products",
            "disabled by default"
            if not products
            else f"HPE_MCP_PRODUCTS={products!r}",
        ),
        _product_access_check(product_access),
        Check(
            # Same escalation as toolsets above: the router rejects an
            # unrecognized HPE_MCP_PRODUCTS name outright now, it does
            # not silently ignore it.
            "FAIL" if unknown_products else "OK",
            "Optional product names",
            "all HPE_MCP_PRODUCTS names are recognized"
            if not unknown_products
            else (
                "unrecognized names will refuse to start the router: "
                f"{', '.join(unknown_products)}"
            ),
        ),
        Check(
            "OK" if not rag_backend or rag_backend in VALID_RAG_BACKENDS else "FAIL",
            "RAG backend",
            "unset; defaults to lancedb"
            if not rag_backend
            else (
                f"HPE_MCP_RAG_BACKEND={rag_backend!r}"
                if rag_backend in VALID_RAG_BACKENDS
                else f"HPE_MCP_RAG_BACKEND={rag_backend!r} is not recognized; "
                "the router will refuse to start"
            ),
        ),
        Check(
            "OK" if listening else "WARN",
            "HTTP router listener",
            f"{host}:{port} is listening"
            if listening
            else (
                f"{host}:{port} is not listening; start with "
                f"`MCP_PORT={port} bash scripts/run_http_router.sh`"
            ),
        ),
    ]

    for product in sorted(_enabled_optional_products(products, toolsets)):
        required = OPTIONAL_PRODUCT_ENVS[product]
        missing = [
            name
            for name in required
            if not (value := os.getenv(name, "").strip())
            or _is_placeholder_value(value)
        ]
        checks.append(
            Check(
                "OK" if not missing else "WARN",
                f"{product} required env",
                "required env vars are set"
                if not missing
                else f"missing or placeholder: {', '.join(missing)}",
            )
        )

    return checks


_PLATFORM_WRITE_ENV_VARS = (
    "HPE_MCP_CENTRAL_WRITES",
    "HPE_MCP_GLP_V2BETA1_WRITES",
    "HPE_MCP_AOS8_WRITES",
    "HPE_MCP_EDGECONNECT_WRITES",
    "HPE_MCP_APSTRA_WRITES",
    "HPE_MCP_MIST_WRITES",
    "HPE_MCP_CLEARPASS_WRITES",
    "HPE_MCP_UXI_WRITES",
    "HPE_MCP_AXIS_WRITES",
)
_LOOPBACK_HOST_VALUES = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _http_security_checks() -> list[Check]:
    """Env-only checks for the streamable-HTTP hardening in hpe_networking_mcp.mcp_servers.shared
    (host/origin allow-lists, optional bearer token, per-platform write
    gates). No network or API calls -- this only inspects environment
    variables, mirroring (not calling) the enforcement in
    hpe_networking_mcp.mcp_servers.shared.run_server / _configure_http_transport."""
    host = os.getenv("MCP_HOST", "127.0.0.1").strip()
    allowed_hosts = os.getenv("MCP_ALLOWED_HOSTS", "").strip()
    allowed_origins = os.getenv("MCP_ALLOWED_ORIGINS", "").strip()
    bearer_token = os.getenv("MCP_HTTP_BEARER_TOKEN", "").strip()
    is_loopback = host in _LOOPBACK_HOST_VALUES

    checks: list[Check] = []

    if is_loopback:
        checks.append(Check("OK", "HTTP bind host", f"MCP_HOST={host!r} is loopback-only"))
    else:
        if allowed_hosts and allowed_origins:
            checks.append(
                Check(
                    "OK",
                    "HTTP allow-list",
                    f"MCP_HOST={host!r} is non-loopback; MCP_ALLOWED_HOSTS/"
                    "MCP_ALLOWED_ORIGINS are both set",
                )
            )
        else:
            missing = [
                name
                for name, value in (
                    ("MCP_ALLOWED_HOSTS", allowed_hosts),
                    ("MCP_ALLOWED_ORIGINS", allowed_origins),
                )
                if not value
            ]
            checks.append(
                Check(
                    "FAIL",
                    "HTTP allow-list",
                    f"MCP_HOST={host!r} is non-loopback but missing {', '.join(missing)}; "
                    "the server will refuse to start (see UnsafeHttpBindingError)",
                )
            )
        host_entries = _csv_values(allowed_hosts)
        origin_entries = _csv_values(allowed_origins)
        if _has_unsupported_wildcard(host_entries) or _has_unsupported_wildcard(origin_entries):
            checks.append(
                Check(
                    "FAIL",
                    "HTTP allow-list wildcard",
                    "an allow-list entry uses '*' outside the installed MCP SDK's "
                    "supported '<host>:*' port-wildcard grammar (a bare '*' or a "
                    "subdomain glob silently matches nothing); the server will refuse "
                    "to start until it is replaced with an explicit value or the "
                    "supported port wildcard",
                )
            )
        checks.append(
            Check(
                "OK" if bearer_token else "WARN",
                "HTTP bearer token",
                "MCP_HTTP_BEARER_TOKEN is set"
                if bearer_token
                else "MCP_HTTP_BEARER_TOKEN is unset on a non-loopback bind; "
                "anything that can reach this host:port can call every tool",
            )
        )

    set_platform_gates = sorted(
        name for name in _PLATFORM_WRITE_ENV_VARS if os.getenv(name, "").strip()
    )
    checks.append(
        Check(
            "OK",
            "Per-platform write gates",
            "none overridden (Central/optional-product defaults apply)"
            if not set_platform_gates
            else f"overridden: {', '.join(set_platform_gates)}",
        )
    )

    return checks


def _local_startup_config_check() -> list[Check]:
    """Exercise the same credentials-file structure/URL validation the real
    server's ``/readyz`` performs (``pipeline.config.build_account_contexts``)
    -- still with zero network or API calls -- so a malformed credentials
    file, or a Central/GLP base/token URL that fails validation, is caught
    here instead of only surfacing later as an opaque auth failure.

    Never echoes a credential value: only our own ``ValueError`` messages
    (URLs/hostnames/workspace IDs, never a secret) are shown verbatim; any
    other exception (e.g. a YAML parse error, which can otherwise echo a
    raw snippet of the file -- including a ``client_secret:`` line -- back
    in its own message) is reported only by exception type.
    """
    creds_path = Path(os.getenv("CREDS_PATH", ROOT / "config" / "credentials.yaml"))
    if not creds_path.is_absolute():
        creds_path = ROOT / creds_path

    if not creds_path.exists():
        return [
            Check(
                "WARN",
                "Local startup config",
                f"{_display_path(creds_path)} missing; structure/URL validation skipped "
                "(see the Credentials check above)",
            )
        ]

    try:
        from hpe_networking_mcp.pipeline.config import build_account_contexts
    except Exception as exc:  # pragma: no cover - depends on local env state
        return [
            Check(
                "WARN",
                "Local startup config",
                f"could not import config validation ({type(exc).__name__}); run `uv sync`",
            )
        ]

    try:
        build_account_contexts(str(creds_path))
    except ValueError as exc:
        return [Check("FAIL", "Local startup config", str(exc))]
    except Exception as exc:  # noqa: BLE001 -- never leak file content/secrets
        return [
            Check(
                "FAIL",
                "Local startup config",
                f"credentials file failed to load ({type(exc).__name__}); "
                "check the file's YAML syntax",
            )
        ]
    return [
        Check(
            "OK",
            "Local startup config",
            "credentials parse and Central/GLP URL validation pass (no API calls made)",
        )
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit nonzero on WARN as well as FAIL",
    )
    args = parser.parse_args()
    _load_local_env(ROOT / ".env")

    checks = [
        *_dependency_checks(),
        *_config_checks(),
        *_index_checks(),
        *_source_manifest_checks(),
        *_runtime_checks(),
        *_http_security_checks(),
        *_local_startup_config_check(),
    ]

    print("hpe-networking-mcp local doctor\n")
    for check in checks:
        print(_status_line(check))

    failures = [check for check in checks if check.status == "FAIL"]
    warnings = [check for check in checks if check.status == "WARN"]
    ok_count = len(checks) - len(failures) - len(warnings)
    print(f"\nSummary: {len(failures)} fail, {len(warnings)} warn, {ok_count} ok")

    if failures or (args.strict and warnings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
