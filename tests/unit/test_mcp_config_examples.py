import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLIENT_CONFIGS = [
    REPO_ROOT / ".mcp.json.example",
    REPO_ROOT / ".cursor" / "mcp.json",
    REPO_ROOT / ".vscode" / "mcp.json.example",
]
HTTP_CONFIGS = [
    REPO_ROOT / ".mcp.http.json.example",
]
COMMITTED_CONFIGS = [
    *CLIENT_CONFIGS,
    *HTTP_CONFIGS,
    REPO_ROOT / ".cursor" / "mcp.dev.json",
    REPO_ROOT / ".claude" / "launch.json",
]
LOCAL_ONLY_CONFIGS = [
    ".mcp.json",
    ".claude/mcp.json",
    ".claude/settings.local.json",
    ".vscode/mcp.json",
]
STALE_SERVER_ID_RE = re.compile(r"\baruba-(?:tool-router|config|monitoring|nac|ops|glp|rag)\b")
STALE_ENV_RE = re.compile(r"\bCENTRALMCP_[A-Z_]+\b")
STALE_PACKAGE_RE = re.compile(r"\bcentralmcp\b", re.IGNORECASE)


def _router_env(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text())
    servers = data.get("mcpServers") or data.get("servers", {})
    router = servers.get("hpe-networking-mcp")
    assert router is not None, (
        f"{path.relative_to(REPO_ROOT)} must define the hpe-networking-mcp router"
    )
    return router.get("env", {})


def test_committed_mcp_client_configs_use_low_token_router_profile():
    for path in CLIENT_CONFIGS:
        env = _router_env(path)

        assert env.get("HPE_MCP_ROUTER_MODE") == "minimal"
        assert env.get("HPE_MCP_TOOLSETS") == "central,glp,rag"
        assert "HPE_MCP_PRODUCTS" not in env


def test_committed_mcp_configs_do_not_include_local_filesystem_servers():
    for path in COMMITTED_CONFIGS:
        text = path.read_text()
        data = json.loads(text)
        servers = data.get("mcpServers") or data.get("servers", {})

        assert "obsidian-vault" not in servers
        assert "/Users/" not in text


def test_committed_configs_use_no_stale_identifiers():
    """Every committed profile stays on current server IDs, env prefix, and
    package name -- no leftover ``aruba-*`` server, ``CENTRALMCP_*`` env var,
    or ``centralmcp`` package reference from the pre-rename project.
    """
    for path in COMMITTED_CONFIGS:
        text = path.read_text()

        assert not STALE_SERVER_ID_RE.search(text), f"{path}: stale aruba-* server id"
        assert not STALE_ENV_RE.search(text), f"{path}: stale CENTRALMCP_* env var"
        assert not STALE_PACKAGE_RE.search(text), f"{path}: stale centralmcp package reference"


def test_committed_http_config_targets_loopback_streamable_http():
    for path in HTTP_CONFIGS:
        data = json.loads(path.read_text())
        servers = data.get("mcpServers") or data.get("servers", {})
        server = next(iter(servers.values()))

        assert server["transport"] == "streamable-http"
        assert server["url"].startswith("http://127.0.0.1:")


def test_local_only_mcp_configs_are_not_tracked():
    try:
        tracked = subprocess.check_output(
            ["git", "ls-files", *LOCAL_ONLY_CONFIGS],
            cwd=REPO_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).splitlines()
    except subprocess.CalledProcessError:
        tracked = []

    assert tracked == []


def test_claude_launch_includes_low_token_router_profile():
    data = json.loads((REPO_ROOT / ".claude" / "launch.json").read_text())
    configs = data.get("configurations", [])
    router = next(
        (
            config
            for config in configs
            if config.get("name") == "hpe-networking-mcp MCP server (minimal)"
        ),
        None,
    )

    assert router is not None
    assert router.get("runtimeArgs") == ["-m", "hpe_networking_mcp.mcp_servers.tool_router"]
    assert router.get("env", {}).get("HPE_MCP_ROUTER_MODE") == "minimal"
    assert router.get("env", {}).get("HPE_MCP_TOOLSETS") == "central,glp,rag"
    assert "HPE_MCP_PRODUCTS" not in router.get("env", {})


def test_repo_agent_docs_reference_claude_launch_router_config():
    claude_md = (REPO_ROOT / "CLAUDE.md").read_text()
    mcp_engineer = (REPO_ROOT / ".claude" / "agents" / "mcp-engineer.md").read_text()

    for text in (claude_md, mcp_engineer):
        assert ".claude/launch.json" in text
        assert "HPE_MCP_TOOLSETS=central,glp,rag" in text


def test_public_setup_docs_reference_claude_launch_router_config():
    readme = (REPO_ROOT / "README.md").read_text()
    getting_started = (REPO_ROOT / "docs" / "getting-started.md").read_text()

    for text in (readme, getting_started):
        assert ".claude/launch.json" in text
        assert "minimal" in text
        assert "hpe-networking-mcp" in text


def _env_blocks(path: Path) -> list[dict[str, str]]:
    """Every ``env`` mapping in a committed MCP config, whatever its shape."""
    data = json.loads(path.read_text())
    blocks = []
    containers = [
        *(data.get("mcpServers") or {}).values(),
        *(data.get("servers") or {}).values(),
        *data.get("configurations", []),
    ]
    for container in containers:
        env = container.get("env") if isinstance(container, dict) else None
        if isinstance(env, dict):
            blocks.append({str(k): str(v) for k, v in env.items()})
    return blocks


def test_every_committed_and_example_config_uses_valid_router_selections():
    """No committed profile may ship a selection the router rejects.

    ``HPE_MCP_TOOLSETS`` accepts every toolset name plus the ``all``
    keyword; ``HPE_MCP_PRODUCTS`` accepts only specific product names and
    raises ``InvalidRuntimeConfigError`` on ``all`` (the invalid
    ``HPE_MCP_PRODUCTS=all`` example this guards against). Router mode and
    product access are likewise closed value sets. Validated against the
    router's own frozensets, so a new product/toolset never needs this test
    updated -- only a genuinely invalid example fails it.
    """
    from hpe_networking_mcp.mcp_servers import tool_router

    valid_modes = {"minimal", "default", "direct"}
    valid_access = {"read-only", "read-write"}
    problems = []

    configs = [*COMMITTED_CONFIGS, *sorted((REPO_ROOT / "examples").rglob("*.json"))]
    for path in configs:
        for env in _env_blocks(path):
            name = path.relative_to(REPO_ROOT)
            toolsets = {v.strip() for v in env.get("HPE_MCP_TOOLSETS", "").split(",") if v.strip()}
            products = {v.strip() for v in env.get("HPE_MCP_PRODUCTS", "").split(",") if v.strip()}
            if not toolsets <= tool_router._VALID_TOOLSETS:
                problems.append(f"{name}: invalid toolsets {sorted(toolsets)}")
            if not products <= tool_router._VALID_PRODUCTS:
                problems.append(f"{name}: invalid products {sorted(products)}")
            mode = env.get("HPE_MCP_ROUTER_MODE")
            if mode is not None and mode not in valid_modes:
                problems.append(f"{name}: invalid HPE_MCP_ROUTER_MODE {mode!r}")
            access = env.get("HPE_MCP_PRODUCT_ACCESS")
            if access is not None and access not in valid_access:
                problems.append(f"{name}: invalid HPE_MCP_PRODUCT_ACCESS {access!r}")

    assert problems == []
