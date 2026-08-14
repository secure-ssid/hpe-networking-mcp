"""Unit tests for the standalone hpe-mcp client core (no live MCP server)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpe_networking_mcp.cli_client.banner import package_version, render_banner, select_wordmark
from hpe_networking_mcp.cli_client.config import (
    ServerProfile,
    built_in_profiles,
    load_client_config,
)
from hpe_networking_mcp.cli_client.documents import DocumentStore
from hpe_networking_mcp.cli_client.safety import SafetyPolicy, tool_is_read_only
from hpe_networking_mcp.cli_client.sessions import profile_to_params
from hpe_networking_mcp.cli_client.skills import discover_skills


class _Tool:
    def __init__(self, name: str, annotations=None, description: str = ""):
        self.name = name
        self.annotations = annotations
        self.description = description


def test_banner_contains_project_name():
    text = render_banner(width=100, mode="shell", profile="local-router")
    assert "hpe-networking-mcp" in text
    assert "mode=shell" in text
    assert package_version()


def test_banner_compacts_on_narrow_terminal():
    assert select_wordmark(width=40) == "hpe-networking-mcp"


def test_built_in_profiles_include_stdio_and_http():
    profiles = built_in_profiles()
    assert "local-router" in profiles
    assert profiles["local-router"].transport == "stdio"
    assert profiles["local-router"].command
    assert profiles["local-http"].transport == "streamable-http"
    assert profiles["local-http"].url


def test_load_config_merges_user_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_dir = tmp_path / ".config" / "hpe-mcp"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(
        json.dumps(
            {
                "defaultProfile": "extra",
                "servers": {
                    "extra": {
                        "transport": "streamable-http",
                        "url": "http://127.0.0.1:9999/mcp",
                        "description": "test",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    cfg = load_client_config(repo_root=tmp_path)
    assert "extra" in cfg.profiles
    assert cfg.default_profile == "extra"
    assert cfg.profiles["extra"].url.endswith("/mcp")


def test_profile_to_params_stdio_and_http():
    stdio = profile_to_params(
        ServerProfile(name="s", transport="stdio", command="python", args=["-m", "x"])
    )
    assert stdio.command == "python"
    http = profile_to_params(
        ServerProfile(name="h", transport="streamable-http", url="http://127.0.0.1:8010/mcp")
    )
    assert http.url.endswith("/mcp")


def test_safety_blocks_writes_by_default():
    ro = _Tool("ask_docs", annotations={"readOnlyHint": True})
    wr = _Tool("create_site", annotations={"destructiveHint": True})
    unknown_writey = _Tool("create_vlan")
    policy = SafetyPolicy(read_only_default=True)

    assert tool_is_read_only(ro) is True
    assert tool_is_read_only(wr) is False
    assert tool_is_read_only(unknown_writey) is False

    assert policy.check(ro).allowed is True
    assert policy.check(wr).allowed is False
    assert policy.check(wr).requires_confirm is True

    open_policy = SafetyPolicy(read_only_default=False, allow_writes=True, confirmed=True)
    assert open_policy.check(wr).allowed is True


def test_document_store_add_list_search(tmp_path: Path):
    store = DocumentStore(root=tmp_path / "collections")
    src = tmp_path / "note.md"
    src.write_text("# EX4400 uplink notes\nuse 100G QSFP28\n", encoding="utf-8")
    rec = store.add_file(src, collection="lab", title="EX4400 notes", tags=["ex4400"])
    assert rec.id
    assert len(store.list("lab")) == 1
    hits = store.search("EX4400", collection="lab")
    assert hits and hits[0].id == rec.id
    assert store.remove(rec.id) is True
    assert store.list("lab") == []


def test_document_store_uri_bookmark(tmp_path: Path):
    store = DocumentStore(root=tmp_path / "collections")
    rec = store.add_uri_record(
        "https://example.com/guide.html",
        collection="mist",
        title="Guide",
    )
    assert rec.source_uri.startswith("https://")
    assert store.list("mist")[0].title == "Guide"


def test_discover_skills_finds_repo_skills():
    skills = discover_skills()
    names = {s.name for s in skills}
    # Repo ships network-docs and lookup-api under .github/skills/
    assert "network-docs" in names or "lookup-api" in names


def test_mcp_cli_parser_help():
    from hpe_networking_mcp.cli.mcp_cli import build_parser

    parser = build_parser()
    help_text = parser.format_help()
    assert "rag" in help_text
    assert "shell" in help_text
    assert "invoke-read" in help_text


def test_normalize_argv_defaults_to_shell():
    from hpe_networking_mcp.cli.mcp_cli import _normalize_argv

    assert _normalize_argv([]) == ["shell"]
    assert _normalize_argv(["--quiet"]) == ["--quiet", "shell"]
    assert _normalize_argv(["--json", "profiles"]) == ["--json", "profiles"]
    assert _normalize_argv(["version"]) == ["version"]
    assert _normalize_argv(["-h"]) == ["-h"]


def test_mcp_cli_version_offline():
    from hpe_networking_mcp.cli.mcp_cli import main

    assert main(["version"]) == 0


def test_mcp_cli_skills_list_offline():
    from hpe_networking_mcp.cli.mcp_cli import main

    assert main(["--json", "skills", "list"]) == 0


def test_mcp_cli_profiles_offline():
    from hpe_networking_mcp.cli.mcp_cli import main

    assert main(["--json", "profiles"]) == 0


def test_repo_root_launcher_exists():
    from hpe_networking_mcp.cli_client.config import repo_root_from_package

    launcher = repo_root_from_package() / "hpe-mcp"
    assert launcher.is_file()
    assert launcher.stat().st_mode & 0o111  # executable bit
