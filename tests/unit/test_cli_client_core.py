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
    # Wordmark is Unicode box-drawing; project name appears in meta/version line context
    assert "mode=shell" in text
    assert "profile=local-router" in text
    assert package_version() in text


def test_prettify_rag_answer():
    from hpe_networking_mcp.cli_client.output import prettify_tool_text

    raw = json.dumps(
        {
            "answer": "Use a three-tier campus with L3 core and L2 access.",
            "citations": [
                {
                    "file_path": "vsg_docs/campus.md",
                    "source": "vsg_docs",
                    "score": 1.0,
                }
            ],
            "mode": "search_docs",
        }
    )
    pretty = prettify_tool_text(raw)
    assert "three-tier" in pretty
    assert "vsg_docs/campus.md" in pretty
    assert "Sources" in pretty


def test_configure_readline_idempotent():
    from hpe_networking_mcp.cli_client.repl_input import configure_readline

    assert configure_readline() is True
    assert configure_readline() is True


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


def test_tui_app_imports_and_help_text():
    from hpe_networking_mcp.cli_client.config import ClientConfig, ServerProfile
    from hpe_networking_mcp.cli_client.safety import SafetyPolicy
    from hpe_networking_mcp.cli_client.tui import (
        CSS,
        HELP_TEXT,
        HpeMcpApp,
        history_line_is_safe,
        normalize_tui_input,
    )

    assert "Just type a networking question" in HELP_TEXT
    assert "#log" in CSS
    assert normalize_tui_input("best way to design a 3 tier network") == [
        "ask",
        "best way to design a 3 tier network",
    ]
    assert normalize_tui_input("/api create WLAN") == ["api", "create", "WLAN"]
    assert normalize_tui_input("find wireless client") == [
        "find",
        "wireless",
        "client",
    ]
    assert normalize_tui_input("help me design a core") == [
        "ask",
        "help me design a core",
    ]
    assert normalize_tui_input("docs for EX4400") == [
        "ask",
        "docs for EX4400",
    ]
    assert normalize_tui_input("docs list") == ["docs", "list"]
    assert normalize_tui_input("/") == ["help"]
    assert history_line_is_safe("best way to design a campus") is True
    assert history_line_is_safe("/api token=secret-value") is False
    # Construct without contacting a real MCP server.
    cfg = ClientConfig(
        profiles={
            "local-router": ServerProfile(
                name="local-router",
                transport="stdio",
                command="false",
                args=[],
            )
        },
        default_profile="local-router",
    )
    app = HpeMcpApp(cfg, SafetyPolicy())
    assert app.TITLE == "hpe-networking-mcp"
    assert any(b.key == "ctrl+q" for b in app.BINDINGS)


def test_tui_mounts_at_80x24_without_connecting():
    import asyncio

    from textual.widgets import Input, RichLog

    from hpe_networking_mcp.cli_client.config import ClientConfig, ServerProfile
    from hpe_networking_mcp.cli_client.safety import SafetyPolicy
    from hpe_networking_mcp.cli_client.tui import HpeMcpApp

    class NoConnectApp(HpeMcpApp):
        def connect_session(self) -> None:
            return None

    cfg = ClientConfig(
        profiles={
            "local-router": ServerProfile(
                name="local-router",
                transport="stdio",
                command="false",
            )
        },
        default_profile="local-router",
    )
    app = NoConnectApp(cfg, SafetyPolicy())

    async def _run() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            field = app.query_one("#cmd", Input)
            assert "Ask a networking question" in (field.placeholder or "")
            assert app.query_one("#log", RichLog)
            await pilot.press("ctrl+q")

    asyncio.run(_run())


def test_tui_ctrl_c_cancels_active_request():
    import asyncio

    from textual.widgets import Input

    from hpe_networking_mcp.cli_client.config import ClientConfig, ServerProfile
    from hpe_networking_mcp.cli_client.safety import SafetyPolicy
    from hpe_networking_mcp.cli_client.tui import HpeMcpApp

    class FakeManager:
        connected: dict[str, object] = {}
        tools: dict[str, object] = {}

        async def __aexit__(self, *_exc: object) -> None:
            return None

    class SlowApp(HpeMcpApp):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self.request_started = asyncio.Event()

        def connect_session(self) -> None:
            return None

        async def _dispatch(self, argv: list[str]) -> str:
            self.request_started.set()
            await asyncio.sleep(30)
            return "unexpected"

    cfg = ClientConfig(
        profiles={
            "local-router": ServerProfile(
                name="local-router",
                transport="stdio",
                command="false",
            )
        },
        default_profile="local-router",
    )
    app = SlowApp(cfg, SafetyPolicy())

    async def _run() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            app.mgr = FakeManager()  # type: ignore[assignment]
            field = app.query_one("#cmd", Input)
            field.value = "design a resilient campus"
            field.focus()
            await pilot.press("enter")
            await asyncio.wait_for(app.request_started.wait(), timeout=2)
            await pilot.press("ctrl+c")
            await pilot.pause(0.1)
            assert app._busy is False
            assert app._command_worker is None

    asyncio.run(_run())


def test_document_store_search_content(tmp_path: Path):
    store = DocumentStore(root=tmp_path / "collections")
    src1 = tmp_path / "switch_config.txt"
    src1.write_text("vlan 100 name CORPORATE-DATA\ninterface 1/1/1 tagged 100", encoding="utf-8")
    src2 = tmp_path / "ap_setup.md"
    src2.write_text("# AP Setup\nConfigure SSID CorpSecure with WPA3 Enterprise", encoding="utf-8")

    store.add_file(src1, collection="configs", title="Core Switch")
    store.add_file(src2, collection="wireless", title="AP Guide")

    hits = store.search_content("CORPORATE-DATA")
    assert len(hits) == 1
    assert hits[0]["title"] == "Core Switch"
    assert "CORPORATE-DATA" in hits[0]["snippet"]

    hits_wpa3 = store.search_content("wpa3", collection="wireless")
    assert len(hits_wpa3) == 1
    assert hits_wpa3[0]["title"] == "AP Guide"


def test_diagram_intent_parsing_and_model():
    from hpe_networking_mcp.cli_client.diagram_workflow import (
        DiagramPreferences,
        parse_diagram_intent,
    )

    pref = parse_diagram_intent(
        "draw a three-tier network with VSX aggregation and Aruba vendor icons in graphviz"
    )
    assert pref.format == "graphviz"
    assert pref.icon_style == "vendor"
    assert pref.vendor == "aruba"
    assert "three" in pref.title.lower()

    assert len(pref.nodes) >= 4
    assert len(pref.links) >= 3
    node_roles = {n["role"] for n in pref.nodes}
    assert "core_switch" in node_roles
    assert "agg_switch" in node_roles
    assert "access_switch" in node_roles

    # Test detailed multi-vendor + auth profiling prompt
    pref_rich = parse_diagram_intent(
        "3 tier network with mist going into detail with how clients authenticating with profiled roles etc with ex and cx"
    )
    roles_rich = {n["role"] for n in pref_rich.nodes}
    assert "client" in roles_rich
    assert "mist_ap" in roles_rich
    assert "access_switch" in roles_rich
    node_ids = {n["id"] for n in pref_rich.nodes}
    assert "acc_cx" in node_ids
    assert "acc_ex" in node_ids
    assert "client_corp" in node_ids
    assert "client_guest" in node_ids
    assert "client_iot" in node_ids


def test_redact_sensitive_text():
    from hpe_networking_mcp.cli_client.output import redact_sensitive_text

    raw = "Connecting with token: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 and password=SuperSecret123!"
    redacted = redact_sensitive_text(raw)
    assert "eyJhbG" not in redacted
    assert "SuperSecret123!" not in redacted
    assert "***REDACTED***" in redacted



def test_format_tool_schema():
    from hpe_networking_mcp.cli_client.output import format_tool_schema

    class MockInputSchema:
        properties = {
            "query": {"type": "string", "description": "Search query"},
            "limit": {"type": "integer", "description": "Max results"},
        }
        required = ["query"]

    class MockTool:
        name = "find_tool"
        description = "Find backend tools"
        inputSchema = MockInputSchema()
        annotations = {"readOnlyHint": True}

    rendered = format_tool_schema("find_tool", MockTool())
    assert "find_tool" in rendered
    assert "Find backend tools" in rendered
    assert "query" in rendered
    assert "Required" in rendered



def test_session_manager_connection_state():
    from hpe_networking_mcp.cli_client.sessions import ConnectionState, SessionManager

    mgr = SessionManager.create()
    assert mgr.state == ConnectionState.DISCONNECTED
    assert mgr.connected == {}
    assert mgr.tools == {}


def test_command_status_and_tool_explore():
    import asyncio
    from hpe_networking_mcp.cli_client.commands import cmd_status, cmd_tool_explore
    from hpe_networking_mcp.cli_client.config import ClientConfig, ServerProfile
    from hpe_networking_mcp.cli_client.safety import SafetyPolicy
    from hpe_networking_mcp.cli_client.sessions import SessionManager

    class MockTool:
        name = "ask_docs"
        description = "Query RAG corpus"
        inputSchema = {"properties": {"question": {"type": "string"}}, "required": ["question"]}
        annotations = {"readOnlyHint": True}

    class FakeGroup:
        tools = {"ask_docs": MockTool()}

    mgr = SessionManager(group=FakeGroup())
    cfg = ClientConfig(
        profiles={"test": ServerProfile(name="test", transport="stdio", command="true")},
        default_profile="test",
    )
    safety = SafetyPolicy()

    async def _run():
        ret_status = cmd_status(mgr, cfg, json_mode=True)
        assert ret_status == 0

        ret_tool = await cmd_tool_explore(mgr, safety, tool_name="ask_docs", json_mode=True)
        assert ret_tool == 0

    asyncio.run(_run())


def test_tui_slash_and_natural_language_handling():
    from hpe_networking_mcp.cli_client.tui import normalize_tui_input

    assert normalize_tui_input("/diagram campus network") == ["diagram", "campus", "network"]
    assert normalize_tui_input("/tool list_devices") == ["tool", "list_devices"]
    assert normalize_tui_input("/status") == ["status"]
    assert normalize_tui_input("/skills") == ["skills"]
    assert normalize_tui_input("/docs") == ["docs"]
    assert normalize_tui_input("specs on cx6300") == ["ask", "specs on cx6300"]

