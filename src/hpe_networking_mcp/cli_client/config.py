"""Named MCP server profiles and user/repo config discovery."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

TransportKind = Literal["stdio", "streamable-http", "sse"]

DEFAULT_PROFILE = "local-router"
USER_CONFIG_DIR_NAMES = (".config/hpe-mcp", ".hpe-mcp")
CONFIG_FILENAMES = ("config.json", "mcp-client.json", "servers.json")


@dataclass(frozen=True)
class ServerProfile:
    """One named MCP server the CLI can connect to."""

    name: str
    transport: TransportKind = "stdio"
    # stdio
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    # http / sse
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    # metadata
    description: str = ""

    def is_http(self) -> bool:
        return self.transport in {"streamable-http", "sse"}


@dataclass
class ClientConfig:
    """Resolved CLI configuration."""

    profiles: dict[str, ServerProfile] = field(default_factory=dict)
    default_profile: str = DEFAULT_PROFILE
    config_path: Path | None = None
    user_data_dir: Path | None = None

    def get(self, name: str | None = None) -> ServerProfile:
        key = (name or self.default_profile).strip()
        if key not in self.profiles:
            known = ", ".join(sorted(self.profiles)) or "(none)"
            raise KeyError(f"unknown profile {key!r}; known: {known}")
        return self.profiles[key]


def user_config_dirs() -> list[Path]:
    home = Path.home()
    dirs = [home / name for name in USER_CONFIG_DIR_NAMES]
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        dirs.insert(0, Path(xdg) / "hpe-mcp")
    return dirs


def default_user_data_dir() -> Path:
    """Primary personal data root (docs collections, history, caches)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "hpe-mcp"
    return Path.home() / ".config" / "hpe-mcp"


def repo_root_from_package() -> Path:
    # .../src/hpe_networking_mcp/cli_client/config.py -> repo root
    return Path(__file__).resolve().parents[3]


def _default_stdio_env(root: Path) -> dict[str, str]:
    return {
        "PYTHONPATH": str(root / "src"),
        "CREDS_PATH": str(root / "config" / "credentials.yaml"),
        "HPE_MCP_ROUTER_MODE": os.environ.get("HPE_MCP_ROUTER_MODE", "minimal"),
        "HPE_MCP_TOOLSETS": os.environ.get("HPE_MCP_TOOLSETS", "central,glp,rag"),
        "HPE_MCP_PRODUCTS": os.environ.get("HPE_MCP_PRODUCTS", "design"),
    }


def built_in_profiles(repo_root: Path | None = None) -> dict[str, ServerProfile]:
    """Profiles that work out of the box against this checkout."""
    root = repo_root or repo_root_from_package()
    venv_python = root / ".venv" / "bin" / "python3"
    python = str(venv_python if venv_python.exists() else Path(os.environ.get("PYTHON", "python3")))
    router_mod = "hpe_networking_mcp.mcp_servers.tool_router"
    env = _default_stdio_env(root)

    profiles = {
        DEFAULT_PROFILE: ServerProfile(
            name=DEFAULT_PROFILE,
            transport="stdio",
            command=python,
            args=["-m", router_mod],
            env=env,
            cwd=str(root),
            description="Launch the local hpe-networking-mcp router over stdio",
        ),
        "local-http": ServerProfile(
            name="local-http",
            transport="streamable-http",
            url=os.environ.get("HPE_MCP_HTTP_URL", "http://127.0.0.1:8010/mcp"),
            description="Connect to a running loopback streamable-HTTP router",
        ),
    }
    return profiles


def _parse_profile(name: str, raw: dict[str, Any]) -> ServerProfile:
    transport = str(raw.get("transport") or raw.get("type") or "stdio").strip().lower()
    if transport in {"http", "streamable_http", "streamable-http"}:
        transport = "streamable-http"
    if transport not in {"stdio", "streamable-http", "sse"}:
        raise ValueError(f"profile {name!r}: unsupported transport {transport!r}")

    return ServerProfile(
        name=name,
        transport=transport,  # type: ignore[arg-type]
        command=raw.get("command"),
        args=list(raw.get("args") or []),
        env={str(k): str(v) for k, v in dict(raw.get("env") or {}).items()},
        cwd=raw.get("cwd"),
        url=raw.get("url"),
        headers={str(k): str(v) for k, v in dict(raw.get("headers") or {}).items()},
        description=str(raw.get("description") or ""),
    )


def _load_json_profiles(path: Path) -> tuple[dict[str, ServerProfile], str | None]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: root must be a JSON object")

    # Accept either {"servers": {...}} / {"mcpServers": {...}} or a flat map.
    block = data.get("servers") or data.get("mcpServers") or data.get("profiles") or data
    if not isinstance(block, dict):
        raise ValueError(f"{path}: servers block must be an object")

    # Skip non-object keys like defaultProfile at the top when flat.
    profiles: dict[str, ServerProfile] = {}
    for name, raw in block.items():
        if name in {"defaultProfile", "default_profile", "default"} and not isinstance(raw, dict):
            continue
        if not isinstance(raw, dict):
            continue
        # Ignore VS Code-style "inputs" etc.
        if "command" not in raw and "url" not in raw:
            continue
        profiles[name] = _parse_profile(name, raw)

    default = data.get("defaultProfile") or data.get("default_profile") or data.get("default")
    if isinstance(default, str):
        return profiles, default
    return profiles, None


def discover_config_paths(repo_root: Path | None = None) -> list[Path]:
    """Ordered candidate config files (later wins when merging)."""
    root = repo_root or repo_root_from_package()
    paths: list[Path] = []
    # User-level first so repo can override for this checkout if desired —
    # actually: built-ins < user < repo < env override. Caller merges that way.
    for directory in user_config_dirs():
        for name in CONFIG_FILENAMES:
            paths.append(directory / name)
    for name in CONFIG_FILENAMES:
        paths.append(root / ".hpe-mcp" / name)
        paths.append(root / name)
    # Generic MCP client recipes already used by editors.
    paths.append(root / ".mcp.json")
    paths.append(root / ".mcp.http.json")
    return paths


def load_client_config(
    *,
    repo_root: Path | None = None,
    config_path: Path | str | None = None,
    profile_override: str | None = None,
) -> ClientConfig:
    """Load built-in profiles, then merge user/repo JSON configs."""
    root = repo_root or repo_root_from_package()
    cfg = ClientConfig(
        profiles=built_in_profiles(root),
        default_profile=DEFAULT_PROFILE,
        user_data_dir=default_user_data_dir(),
    )

    paths: list[Path]
    if config_path is not None:
        paths = [Path(config_path)]
    else:
        env_path = os.environ.get("HPE_MCP_CLIENT_CONFIG")
        paths = [Path(env_path)] if env_path else discover_config_paths(root)

    last_path: Path | None = None
    for path in paths:
        if not path.is_file():
            continue
        try:
            profiles, default = _load_json_profiles(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        cfg.profiles.update(profiles)
        if default:
            cfg.default_profile = default
        last_path = path

    cfg.config_path = last_path
    if profile_override:
        cfg.default_profile = profile_override
    return cfg
