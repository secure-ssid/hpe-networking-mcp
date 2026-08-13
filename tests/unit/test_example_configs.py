"""Every example config under ``examples/`` parses, is non-secret, and uses
current identifiers.

``examples/`` is the tested, non-secret configuration tree described in
``examples/README.md``'s profile matrix (minimal/full stdio, local/bearer
HTTP, Copilot CLI). Unlike ``tests/unit/test_mcp_config_examples.py`` (which
covers the root-level committed profiles: ``.mcp.json.example``,
``.cursor/*.json``, ``.vscode/mcp.json.example``, ``.claude/launch.json``),
this module covers the additional example tree and the profile-matrix links
in ``examples/README.md`` that tie the two together, and adds an explicit
stale-identifier guard so a leftover ``aruba-*`` server ID, ``centralmcp``
package name, or ``CENTRALMCP_*`` env var cannot ship in a new example.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "examples"

JSON_EXAMPLES = sorted(EXAMPLES_DIR.rglob("*.json"))
MARKDOWN_EXAMPLES = sorted(EXAMPLES_DIR.rglob("*.md"))
ALL_EXAMPLE_TEXT_FILES = sorted(
    p for p in EXAMPLES_DIR.rglob("*") if p.is_file() and p.suffix in {".json", ".md", ".env"}
)

STALE_SERVER_ID_RE = re.compile(r"\baruba-(?:tool-router|config|monitoring|nac|ops|glp|rag)\b")
STALE_ENV_RE = re.compile(r"\bCENTRALMCP_[A-Z_]+\b")
STALE_PACKAGE_RE = re.compile(r"\bcentralmcp\b", re.IGNORECASE)
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def test_examples_tree_exists_and_is_non_empty():
    assert EXAMPLES_DIR.is_dir()
    assert JSON_EXAMPLES
    assert MARKDOWN_EXAMPLES


@pytest.mark.parametrize("path", JSON_EXAMPLES, ids=lambda p: str(p.relative_to(EXAMPLES_DIR)))
def test_json_examples_parse(path: Path):
    json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "path",
    [*JSON_EXAMPLES, EXAMPLES_DIR / "mcp-clients" / "http" / "bearer.server.env"],
    ids=lambda p: str(p.relative_to(EXAMPLES_DIR)),
)
def test_configs_use_no_stale_identifiers(path: Path):
    # Scoped to actual client/server configs (JSON + the HTTP server .env),
    # not the explanatory Markdown pages: examples/README.md and
    # examples/prompts/README.md legitimately name the legacy
    # secure-ssid/centralmcp project when explaining the migration path
    # (see MIGRATION.md), which would otherwise false-positive here.
    text = path.read_text(encoding="utf-8", errors="replace")

    assert not STALE_SERVER_ID_RE.search(text), f"{path}: stale aruba-* server id"
    assert not STALE_ENV_RE.search(text), f"{path}: stale CENTRALMCP_* env var"
    assert not STALE_PACKAGE_RE.search(text), f"{path}: stale centralmcp package reference"


@pytest.mark.parametrize(
    "path", ALL_EXAMPLE_TEXT_FILES, ids=lambda p: str(p.relative_to(EXAMPLES_DIR))
)
def test_examples_have_no_real_user_paths(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")

    # Real machine-specific paths must never appear; only the documented
    # placeholder (/path/to/hpe-networking-mcp) or ${workspaceFolder}-style
    # client substitutions are allowed.
    assert "/Users/" not in text, f"{path}: real user home path"
    assert "/home/" not in text, f"{path}: real user home path"


def _stdio_env(path: Path, server_name: str = "hpe-networking-mcp") -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    servers = data.get("mcpServers") or data.get("servers", {})
    assert server_name in servers, f"{path}: missing {server_name!r} server entry"
    return servers[server_name].get("env", {})


def test_minimal_stdio_example_uses_the_low_token_router_profile():
    env = _stdio_env(EXAMPLES_DIR / "mcp-clients" / "stdio" / "minimal.mcp.json")

    assert env.get("HPE_MCP_ROUTER_MODE") == "minimal"
    assert env.get("HPE_MCP_TOOLSETS") == "central,glp,rag"
    assert "HPE_MCP_PRODUCTS" not in env


def test_full_stdio_example_enables_every_optional_product_read_only():
    from hpe_networking_mcp.mcp_servers import tool_router

    env = _stdio_env(EXAMPLES_DIR / "mcp-clients" / "stdio" / "full.mcp.json")

    assert env.get("HPE_MCP_ROUTER_MODE") == "minimal"
    assert env.get("HPE_MCP_TOOLSETS") == "central,glp,rag"
    products = {p.strip() for p in env.get("HPE_MCP_PRODUCTS", "").split(",") if p.strip()}
    assert products == set(tool_router._OPTIONAL_BACKENDS)
    assert env.get("HPE_MCP_PRODUCT_ACCESS") == "read-only"


def test_copilot_cli_example_uses_the_low_token_router_profile():
    env = _stdio_env(EXAMPLES_DIR / "mcp-clients" / "copilot-cli.mcp-config.json")

    assert env.get("HPE_MCP_ROUTER_MODE") == "minimal"
    assert env.get("HPE_MCP_TOOLSETS") == "central,glp,rag"
    assert "HPE_MCP_PRODUCTS" not in env


def test_local_http_example_targets_loopback_streamable_http():
    data = json.loads(
        (EXAMPLES_DIR / "mcp-clients" / "http" / "local.mcp.http.json").read_text()
    )
    server = data["mcpServers"]["hpe-networking-mcp-http"]

    assert server["transport"] == "streamable-http"
    assert server["url"].startswith("http://127.0.0.1:")


def test_bearer_http_example_requires_authorization_header_and_matching_server_env():
    data = json.loads(
        (EXAMPLES_DIR / "mcp-clients" / "http" / "bearer.mcp.http.json").read_text()
    )
    server = data["mcpServers"]["hpe-networking-mcp-http"]

    assert server["transport"] == "streamable-http"
    assert server["url"].startswith("https://")
    assert server["headers"]["Authorization"].startswith("Bearer ")

    server_env = (EXAMPLES_DIR / "mcp-clients" / "http" / "bearer.server.env").read_text()
    for required in ("MCP_ALLOWED_HOSTS=", "MCP_ALLOWED_ORIGINS=", "MCP_HTTP_BEARER_TOKEN="):
        assert required in server_env


def test_examples_readme_profile_matrix_links_resolve():
    readme = EXAMPLES_DIR / "README.md"
    text = readme.read_text(encoding="utf-8")
    missing = []

    for match in MARKDOWN_LINK_RE.finditer(text):
        target = match.group(1).split("#", 1)[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        candidate = (readme.parent / target).resolve()
        if not candidate.exists():
            missing.append(target)

    assert missing == []


def test_examples_readme_documents_every_committed_example_file():
    readme_text = (EXAMPLES_DIR / "README.md").read_text(encoding="utf-8")

    for path in [*JSON_EXAMPLES, EXAMPLES_DIR / "mcp-clients" / "http" / "bearer.server.env"]:
        relative = path.relative_to(EXAMPLES_DIR).as_posix()
        assert relative in readme_text, f"{relative} is not linked from examples/README.md"


def test_root_readme_and_docs_readme_reference_the_examples_tree():
    readme = (REPO_ROOT / "README.md").read_text()
    docs_readme = (REPO_ROOT / "docs" / "README.md").read_text()

    assert "examples/" in readme
    assert "examples/" in docs_readme
