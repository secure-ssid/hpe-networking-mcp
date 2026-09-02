from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver import MCPServer

import hpe_networking_mcp.mcp_servers.tool_router as router
from hpe_networking_mcp.mcp_servers.shared import (
    IDEMPOTENT_WRITE,
    InvalidRuntimeConfigError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_build_backends_default_has_core_only(monkeypatch):
    monkeypatch.delenv("HPE_MCP_PRODUCTS", raising=False)
    monkeypatch.delenv("HPE_MCP_TOOLSETS", raising=False)
    backends = router._build_backends()
    assert "central-config" in backends
    assert "central-streaming" in backends
    assert "clearpass-core" not in backends


def test_build_backends_enables_clearpass(monkeypatch):
    monkeypatch.delenv("HPE_MCP_TOOLSETS", raising=False)
    monkeypatch.setenv("HPE_MCP_PRODUCTS", "clearpass")
    backends = router._build_backends()
    assert backends.get("clearpass-core") == "hpe_networking_mcp.mcp_servers.clearpass"


def test_build_backends_enables_multiple_products(monkeypatch):
    monkeypatch.delenv("HPE_MCP_TOOLSETS", raising=False)
    monkeypatch.setenv(
        "HPE_MCP_PRODUCTS", "clearpass,mist,apstra,aos8,edgeconnect,uxi,axis,design"
    )
    backends = router._build_backends()
    assert backends.get("clearpass-core") == "hpe_networking_mcp.mcp_servers.clearpass"
    assert backends.get("mist-core") == "hpe_networking_mcp.mcp_servers.mist"
    assert backends.get("apstra-core") == "hpe_networking_mcp.mcp_servers.apstra"
    assert backends.get("aos8-core") == "hpe_networking_mcp.mcp_servers.aos8"
    assert backends.get("edgeconnect-core") == "hpe_networking_mcp.mcp_servers.edgeconnect"
    assert backends.get("uxi-core") == "hpe_networking_mcp.mcp_servers.uxi"
    assert backends.get("axis-core") == "hpe_networking_mcp.mcp_servers.axis"
    assert backends.get("design-core") == "hpe_networking_mcp.mcp_servers.design"


def test_build_backends_toolsets_narrow_core(monkeypatch):
    monkeypatch.delenv("HPE_MCP_PRODUCTS", raising=False)
    monkeypatch.setenv("HPE_MCP_TOOLSETS", "monitoring,rag")
    backends = router._build_backends()
    # catalog-core and interop-core are credential-free/read-only-local and
    # always loaded, independent of the selected toolsets.
    assert set(backends) == {
        "catalog-core",
        "central-monitoring",
        "rag-core",
        "interop-core",
    }


def test_always_on_local_backends_are_present_in_every_profile(monkeypatch):
    """Catalog and interop load on the default, minimal and narrow profiles alike."""
    monkeypatch.delenv("HPE_MCP_PRODUCTS", raising=False)
    for toolsets in (None, "central,glp,rag", "rag", "all"):
        if toolsets is None:
            monkeypatch.delenv("HPE_MCP_TOOLSETS", raising=False)
        else:
            monkeypatch.setenv("HPE_MCP_TOOLSETS", toolsets)
        backends = router._build_backends()
        assert backends.get("catalog-core") == "hpe_networking_mcp.mcp_servers.catalog", toolsets
        assert backends.get("interop-core") == "hpe_networking_mcp.mcp_servers.interop", toolsets
    assert router._TOOLSET_BACKENDS["catalog"] == {"catalog-core"}
    assert router._SERVER_PLATFORMS["catalog-core"] == "catalog"
    assert router._TOOLSET_BACKENDS["interop"] == {"interop-core"}
    assert router._SERVER_PLATFORMS["interop-core"] == "interop"


def test_build_backends_toolsets_can_enable_optional_products(monkeypatch):
    monkeypatch.delenv("HPE_MCP_PRODUCTS", raising=False)
    monkeypatch.setenv("HPE_MCP_TOOLSETS", "central,clearpass,apstra")
    backends = router._build_backends()
    assert "central-monitoring" in backends
    assert "central-streaming" in backends
    assert "glp-core" not in backends
    assert backends.get("clearpass-core") == "hpe_networking_mcp.mcp_servers.clearpass"
    assert backends.get("apstra-core") == "hpe_networking_mcp.mcp_servers.apstra"


def test_build_backends_toolsets_all_includes_known_optional(monkeypatch):
    monkeypatch.delenv("HPE_MCP_PRODUCTS", raising=False)
    monkeypatch.setenv("HPE_MCP_TOOLSETS", "all")
    backends = router._build_backends()
    assert "central-config" in backends
    assert "clearpass-core" in backends
    assert "mist-core" in backends
    assert "apstra-core" in backends
    assert "aos8-core" in backends
    assert "edgeconnect-core" in backends
    assert "uxi-core" in backends
    assert "axis-core" in backends
    assert "design-core" in backends


def test_load_all_backends_keeps_diagnostics_in_read_only_mode(monkeypatch):
    backend = MCPServer("diagnostic-backend")

    @backend.tool(annotations=router.DIAGNOSTIC)
    def run_diagnostic() -> dict:
        return {"ok": True}

    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")
    monkeypatch.setattr(router, "_BACKENDS", {"edgeconnect-core": "demo.diagnostic"})
    monkeypatch.setattr(router, "_tool_index", {})
    monkeypatch.setattr(router, "_tool_servers", {})
    monkeypatch.setattr(router, "_tool_backend_names", {})
    monkeypatch.setattr(
        router.importlib,
        "import_module",
        lambda path: SimpleNamespace(mcp=backend),
    )

    router._load_all_backends()

    assert "run_diagnostic" in router._tool_index


def test_load_all_backends_filters_optional_writes_when_read_only(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")
    monkeypatch.setenv("HPE_MCP_CLEARPASS_WRITES", "0")  # .env sets this; explicit disable wins
    monkeypatch.setattr(
        router, "_BACKENDS", {"clearpass-core": "hpe_networking_mcp.mcp_servers.clearpass"}
    )
    monkeypatch.setattr(router, "_tool_index", {})
    monkeypatch.setattr(router, "_tool_servers", {})
    monkeypatch.setattr(router, "_tool_backend_names", {})

    router._load_all_backends()

    assert "clearpass_status" in router._tool_index
    assert "clearpass_get" in router._tool_index
    assert "clearpass_write" not in router._tool_index
    assert "clearpass_delete_guest" not in router._tool_index


def test_load_all_backends_exposes_optional_writes_when_read_write(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(
        router, "_BACKENDS", {"clearpass-core": "hpe_networking_mcp.mcp_servers.clearpass"}
    )
    monkeypatch.setattr(router, "_tool_index", {})
    monkeypatch.setattr(router, "_tool_servers", {})
    monkeypatch.setattr(router, "_tool_backend_names", {})

    router._load_all_backends()

    assert "clearpass_status" in router._tool_index
    assert "clearpass_write" in router._tool_index
    assert "clearpass_delete_guest" in router._tool_index


@pytest.mark.parametrize(
    ("shared_access", "axis_override", "expected"),
    [
        ("read-only", "1", True),
        ("read-write", "0", False),
        ("read-write", "invalid", None),
    ],
)
def test_load_all_backends_honors_platform_override_precedence(
    monkeypatch,
    shared_access,
    axis_override,
    expected,
):
    backend = MCPServer("axis-override")

    @backend.tool(annotations=IDEMPOTENT_WRITE)
    def axis_update_widget() -> dict:
        return {"updated": True}

    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", shared_access)
    monkeypatch.setenv("HPE_MCP_AXIS_WRITES", axis_override)
    monkeypatch.setattr(router, "_BACKENDS", {"axis-core": "demo.axis"})
    monkeypatch.setattr(router, "_tool_index", {})
    monkeypatch.setattr(router, "_tool_servers", {})
    monkeypatch.setattr(router, "_tool_backend_names", {})
    monkeypatch.setattr(
        router.importlib,
        "import_module",
        lambda path: SimpleNamespace(mcp=backend),
    )

    if expected is None:
        with pytest.raises(InvalidRuntimeConfigError, match="HPE_MCP_AXIS_WRITES"):
            router._load_all_backends()
        return

    router._load_all_backends()
    assert ("axis_update_widget" in router._tool_index) is expected


def test_load_all_backends_rejects_cross_backend_name_collisions(monkeypatch):
    first = MCPServer("first")
    second = MCPServer("second")

    @first.tool()
    def duplicate_name() -> str:
        return "first"

    @second.tool()
    def duplicate_name() -> str:  # noqa: F811
        return "second"

    modules = {
        "demo.first": SimpleNamespace(mcp=first),
        "demo.second": SimpleNamespace(mcp=second),
    }
    monkeypatch.setattr(router, "_BACKENDS", {"first": "demo.first", "second": "demo.second"})
    monkeypatch.setattr(router, "_tool_index", {})
    monkeypatch.setattr(router, "_tool_servers", {})
    monkeypatch.setattr(router, "_tool_backend_names", {})
    monkeypatch.setattr(router.importlib, "import_module", modules.__getitem__)

    with pytest.raises(RuntimeError, match="duplicate backend tool name"):
        router._load_all_backends()


def test_direct_mode_registers_enabled_backend_tools(monkeypatch):
    backend = MCPServer("backend")
    target = MCPServer("router-direct")

    @backend.tool(annotations=router.READ_ONLY)
    def direct_example(value: str) -> str:
        return value

    # Unannotated, so it classifies as a *write* -- and this backend resolves to
    # no registered gate and is not an optional product, so nothing can enable
    # it. Direct mode must withhold it rather than publish an ungated write.
    @backend.tool()
    def direct_unannotated(value: str) -> str:
        return value

    monkeypatch.setattr(router, "_BACKENDS", {"demo": "demo.backend"})
    monkeypatch.setattr(router, "_tool_index", {})
    monkeypatch.setattr(router, "_tool_servers", {})
    monkeypatch.setattr(router, "_tool_backend_names", {})
    monkeypatch.setattr(
        router.importlib,
        "import_module",
        lambda module_path: SimpleNamespace(mcp=backend),
    )

    registered = router._register_direct_backend_tools(target)

    assert registered == ["direct_example"]
    assert "direct_example" in target._tool_manager._tools
    assert "direct_unannotated" not in target._tool_manager._tools


def test_public_docs_list_router_products_and_toolsets():
    readme = (REPO_ROOT / "README.md").read_text()
    getting_started = (REPO_ROOT / "docs" / "getting-started.md").read_text()
    tool_router = (REPO_ROOT / "docs" / "tool-router.md").read_text()
    optional_products = ",".join(router._OPTIONAL_BACKENDS)

    assert f"HPE_MCP_PRODUCTS={optional_products}" in readme
    assert f"HPE_MCP_PRODUCTS={optional_products}" in getting_started
    assert f"HPE_MCP_PRODUCTS={optional_products}" in tool_router

    for toolset in {*router._TOOLSET_BACKENDS, "all"}:
        assert f"`{toolset}`" in tool_router

    for text in (readme, tool_router):
        assert "`include_schema=true`" in text


def test_find_tool_filters_semantic_hits_from_disabled_backends(monkeypatch):
    monkeypatch.setattr(router, "_BACKEND", "lancedb")
    monkeypatch.setattr(router, "_BACKENDS", {"rag-core": "hpe_networking_mcp.mcp_servers.rag"})
    monkeypatch.setattr(router, "_keyword_hits", lambda query, limit, include_schema=False: [])
    monkeypatch.setattr(router._embedder, "embed_query", lambda query: [0.0])
    monkeypatch.setattr(router._lance, "connect", lambda: object())
    monkeypatch.setattr(
        router._lance,
        "search_tools",
        lambda db, query, vec, top_k, servers=None: [
            {
                "name": "create_vlan",
                "server": "central-config",
                "description": "disabled config tool",
                "schema_json": "{}",
                "score": 0.99,
            },
            {
                "name": "search_docs",
                "server": "rag-core",
                "description": "enabled rag tool",
                "schema_json": "{}",
                "score": 0.8,
            },
        ],
    )

    results = router.find_tool("vlan docs", top_k=5)

    assert [item["name"] for item in results] == ["search_docs"]


def test_find_tool_filters_keyword_hits_from_disabled_backends(monkeypatch):
    """The keyword pass must not recommend tools this deployment cannot invoke.

    ``_tool_index`` spans the whole catalog so discovery can reason about every
    backend, but only the enabled ones are actually callable. The semantic pass
    always dropped disabled-backend hits while the keyword pass did not, so a
    complete-catalog index surfaced raw endpoints the router would reject.
    """
    monkeypatch.setattr(router, "_BACKEND", "lancedb")
    monkeypatch.setattr(
        router, "_BACKENDS", {"rag-core": "hpe_networking_mcp.mcp_servers.rag"}
    )
    monkeypatch.setattr(router, "_load_all_backends", lambda: None)
    monkeypatch.setattr(router, "_generated_records", lambda: {})
    monkeypatch.setattr(
        router,
        "_tool_index",
        {
            "clearpass_network_device_get": SimpleNamespace(
                name="clearpass_network_device_get",
                description="Read a ClearPass network device",
                parameters={},
                annotations=SimpleNamespace(
                    read_only_hint=True, destructive_hint=False, idempotent_hint=True
                ),
            ),
            "list_devices": SimpleNamespace(
                name="list_devices",
                description="List network devices",
                parameters={},
                annotations=SimpleNamespace(
                    read_only_hint=True, destructive_hint=False, idempotent_hint=True
                ),
            ),
        },
    )
    monkeypatch.setattr(
        router,
        "_tool_backend_names",
        {
            "clearpass_network_device_get": "clearpass-core",
            "list_devices": "rag-core",
        },
    )

    hits = router._keyword_hits("network devices", limit=10)

    assert [h["name"] for h in hits] == ["list_devices"]


def test_find_tool_filters_optional_write_hits_when_read_only(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")
    monkeypatch.setenv("HPE_MCP_CLEARPASS_WRITES", "0")  # .env sets this; explicit disable wins
    monkeypatch.setattr(router, "_BACKEND", "lancedb")
    monkeypatch.setattr(
        router, "_BACKENDS", {"clearpass-core": "hpe_networking_mcp.mcp_servers.clearpass"}
    )
    monkeypatch.setattr(
        router,
        "_tool_index",
        {
            "clearpass_write": SimpleNamespace(
                annotations=SimpleNamespace(
                    read_only_hint=False,
                    destructive_hint=False,
                    idempotent_hint=True,
                )
            ),
            "clearpass_status": SimpleNamespace(
                annotations=SimpleNamespace(
                    read_only_hint=True,
                    destructive_hint=False,
                    idempotent_hint=True,
                )
            ),
        },
    )
    monkeypatch.setattr(
        router,
        "_tool_backend_names",
        {
            "clearpass_write": "clearpass-core",
            "clearpass_status": "clearpass-core",
        },
    )
    monkeypatch.setattr(router, "_keyword_hits", lambda query, limit, include_schema=False: [])
    monkeypatch.setattr(router._embedder, "embed_query", lambda query: [0.0])
    monkeypatch.setattr(router._lance, "connect", lambda: object())
    monkeypatch.setattr(
        router._lance,
        "search_tools",
        lambda db, query, vec, top_k, servers=None: [
            {
                "name": "clearpass_write",
                "server": "clearpass-core",
                "description": "write tool",
                "schema_json": "{}",
                "score": 0.99,
            },
            {
                "name": "clearpass_status",
                "server": "clearpass-core",
                "description": "status tool",
                "schema_json": "{}",
                "score": 0.8,
            },
        ],
    )

    results = router.find_tool("clearpass write status", top_k=5)

    assert [item["name"] for item in results] == ["clearpass_status"]


def test_find_tool_omits_schema_by_default(monkeypatch):
    monkeypatch.setattr(router, "_BACKEND", "lancedb")
    monkeypatch.setattr(
        router, "_BACKENDS", {"central-config": "hpe_networking_mcp.mcp_servers.config"}
    )
    monkeypatch.setattr(
        router,
        "_tool_index",
        {
            "create_vlan": SimpleNamespace(
                annotations=IDEMPOTENT_WRITE,
                parameters={"properties": {"vlan_id": {"type": "integer"}}},
            )
        },
    )
    monkeypatch.setattr(router, "_load_all_backends", lambda: None)
    monkeypatch.setattr(router, "_keyword_hits", lambda query, limit, include_schema=False: [])
    monkeypatch.setattr(router._embedder, "embed_query", lambda query: [0.0])
    monkeypatch.setattr(router._lance, "connect", lambda: object())
    monkeypatch.setattr(
        router._lance,
        "search_tools",
        lambda db, query, vec, top_k, servers=None: [
            {
                "name": "create_vlan",
                "server": "central-config",
                "description": "Create a VLAN",
                "schema_json": '{"properties": {"vlan_id": {"type": "integer"}}}',
                "score": 0.9,
            }
        ],
    )

    result = router.find_tool("create vlan", top_k=1)

    assert result[0]["params"] == ["vlan_id"]
    assert "schema" not in result[0]


def test_find_tool_can_include_schema_when_requested(monkeypatch):
    monkeypatch.setattr(router, "_BACKEND", "lancedb")
    monkeypatch.setattr(
        router, "_BACKENDS", {"central-config": "hpe_networking_mcp.mcp_servers.config"}
    )
    monkeypatch.setattr(
        router,
        "_tool_index",
        {
            "create_vlan": SimpleNamespace(
                annotations=IDEMPOTENT_WRITE,
                parameters={"properties": {"vlan_id": {"type": "integer"}}},
            )
        },
    )
    monkeypatch.setattr(router, "_load_all_backends", lambda: None)
    monkeypatch.setattr(router, "_keyword_hits", lambda query, limit, include_schema=False: [])
    monkeypatch.setattr(router._embedder, "embed_query", lambda query: [0.0])
    monkeypatch.setattr(router._lance, "connect", lambda: object())
    monkeypatch.setattr(
        router._lance,
        "search_tools",
        lambda db, query, vec, top_k, servers=None: [
            {
                "name": "create_vlan",
                "server": "central-config",
                "description": "Create a VLAN",
                "schema_json": '{"properties": {"vlan_id": {"type": "integer"}}}',
                "score": 0.9,
            }
        ],
    )

    result = router.find_tool("create vlan", top_k=1, include_schema=True)

    assert result[0]["schema"] == {"properties": {"vlan_id": {"type": "integer"}}}


def test_find_tool_hydrates_annotations_for_semantic_only_results(monkeypatch):
    def load_tools():
        router._tool_index["search_docs"] = SimpleNamespace(
            annotations=SimpleNamespace(
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
            )
        )

    monkeypatch.setattr(router, "_BACKEND", "lancedb")
    monkeypatch.setattr(router, "_BACKENDS", {"rag-core": "hpe_networking_mcp.mcp_servers.rag"})
    monkeypatch.setattr(router, "_tool_index", {})
    monkeypatch.setattr(router, "_keyword_hits", lambda query, limit, include_schema=False: [])
    monkeypatch.setattr(router, "_load_all_backends", load_tools)
    monkeypatch.setattr(router._embedder, "embed_query", lambda query: [0.0])
    monkeypatch.setattr(router._lance, "connect", lambda: object())
    monkeypatch.setattr(
        router._lance,
        "search_tools",
        lambda db, query, vec, top_k, servers=None: [
            {
                "name": "search_docs",
                "server": "rag-core",
                "description": "Search docs",
                "schema_json": "{}",
                "score": 0.9,
            }
        ],
    )

    result = router.find_tool("documentation help", top_k=1)

    assert result[0]["read_only"] is True
    assert result[0]["destructive"] is False
    assert result[0]["idempotent"] is True


@pytest.mark.parametrize(
    ("annotations", "schema", "capability", "dispatcher", "confirmation_required"),
    [
        (router.READ_ONLY, {}, "read", "invoke_read_tool", False),
        (router.DIAGNOSTIC, {}, "diagnostic", "invoke_tool", False),
        (
            IDEMPOTENT_WRITE,
            {"properties": {"confirm": {"type": "boolean"}}},
            "write",
            "invoke_tool",
            True,
        ),
        (router.DESTRUCTIVE, {}, "destructive", "invoke_tool", True),
    ],
)
def test_discovery_capability_is_normalized_from_annotations(
    monkeypatch,
    annotations,
    schema,
    capability,
    dispatcher,
    confirmation_required,
):
    monkeypatch.setattr(router, "_BACKENDS", {"central-config": "demo.config"})
    metadata = router._discovery_metadata(
        SimpleNamespace(annotations=annotations),
        "central-config",
        schema,
    )

    assert metadata["capability"] == capability
    assert metadata["recommended_dispatcher"] == dispatcher
    assert metadata["requires_confirmation"] is confirmation_required


def test_find_tool_filters_keyword_results_and_reports_write_contract(monkeypatch):
    backend = MCPServer("discovery-keyword")

    @backend.tool(annotations=router.READ_ONLY)
    def list_widgets(limit: int = 10) -> dict:
        return {"limit": limit}

    @backend.tool(annotations=IDEMPOTENT_WRITE)
    def update_widget(
        widget_id: str,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        return {"widget_id": widget_id, "dry_run": dry_run, "confirm": confirm}

    tools = dict(backend._tool_manager._tools)
    monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "0")
    monkeypatch.setattr(
        router,
        "_BACKENDS",
        {
            "central-monitoring": "demo.monitoring",
            "central-config": "demo.config",
        },
    )
    monkeypatch.setattr(router, "_tool_index", tools)
    monkeypatch.setattr(
        router,
        "_tool_backend_names",
        {
            "list_widgets": "central-monitoring",
            "update_widget": "central-config",
        },
    )
    monkeypatch.setattr(router, "_load_all_backends", lambda: None)
    monkeypatch.setattr(router, "_BACKEND", "lancedb")
    monkeypatch.setattr(router._embedder, "embed_query", lambda query: [0.0])
    monkeypatch.setattr(router._lance, "connect", lambda: object())
    monkeypatch.setattr(
        router._lance,
        "search_tools",
        lambda db, query, vec, top_k, servers=None: [],
    )

    result = router.find_tool(
        "widget",
        top_k=5,
        platform="central",
        server="central-config",
        capability="write",
    )

    assert [item["name"] for item in result] == ["update_widget"]
    item = result[0]
    assert item["platform"] == "central"
    assert item["capability"] == "write"
    assert item["recommended_dispatcher"] == "invoke_tool"
    assert item["requires_write_enablement"] is True
    assert item["currently_enabled"] is False
    assert item["supports_dry_run"] is True
    assert item["supports_confirm"] is True
    assert item["requires_confirmation"] is True
    assert item["read_only"] is False
    assert item["destructive"] is False
    assert item["idempotent"] is True
    assert item["origin"] == "curated"
    assert item["execution_contract"] == {
        "platform": "central",
        "capability": "write",
        "gate": {
            "env_var": "HPE_MCP_CENTRAL_WRITES",
            "state": "disabled",
            "source": "platform_override",
        },
        "dry_run": {"supported": True, "state": "default_preview"},
        "confirm": {"supported": True, "required": True},
        "idempotent": True,
        "next_action": (
            "Set HPE_MCP_CENTRAL_WRITES=1, then call invoke_tool with "
            "dry_run=true to preview."
        ),
    }


def test_find_tool_filters_generated_origin_and_operation_id(monkeypatch):
    backend = MCPServer("discovery-generated")

    @backend.tool(annotations=router.READ_ONLY)
    def generated_widget(widget_id: str) -> dict:
        return {"widget_id": widget_id}

    tools = dict(backend._tool_manager._tools)
    monkeypatch.setattr(router, "_BACKENDS", {"central-monitoring": "demo.monitoring"})
    monkeypatch.setattr(router, "_tool_index", tools)
    monkeypatch.setattr(
        router,
        "_tool_backend_names",
        {"generated_widget": "central-monitoring"},
    )
    monkeypatch.setattr(router, "_load_all_backends", lambda: None)
    monkeypatch.setattr(
        router,
        "_generated_tool_records",
        {
            "generated_widget": {
                "operation_id": "getGeneratedWidget",
                "operation_key": "GET /widgets/{widget_id}",
                "manifest_platform": "central",
            }
        },
    )
    monkeypatch.setattr(router, "_BACKEND", "lancedb")
    monkeypatch.setattr(router._embedder, "embed_query", lambda query: [0.0])
    monkeypatch.setattr(router._lance, "connect", lambda: object())
    monkeypatch.setattr(router._lance, "search_tools", lambda *args, **kwargs: [])

    result = router.find_tool(
        "generated widget",
        origin="generated",
        operation_id="getGeneratedWidget",
    )

    assert [item["name"] for item in result] == ["generated_widget"]
    assert result[0]["origin"] == "generated"
    assert result[0]["operation_id"] == "getGeneratedWidget"
    assert result[0]["operation_key"] == "GET /widgets/{widget_id}"

    assert router.find_tool("generated widget", origin="curated") == []


def test_find_tool_filters_semantic_results_by_diagnostic_capability(monkeypatch):
    backend = MCPServer("discovery-semantic")

    @backend.tool(annotations=router.READ_ONLY)
    def mist_widget_status() -> dict:
        return {"status": "ok"}

    @backend.tool(annotations=router.DIAGNOSTIC)
    def mist_widget_diagnostic() -> dict:
        return {"status": "healthy"}

    tools = dict(backend._tool_manager._tools)
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")
    monkeypatch.setattr(router, "_BACKEND", "lancedb")
    monkeypatch.setattr(router, "_BACKENDS", {"mist-core": "demo.mist"})
    monkeypatch.setattr(router, "_tool_index", tools)
    monkeypatch.setattr(
        router,
        "_tool_backend_names",
        {name: "mist-core" for name in tools},
    )
    monkeypatch.setattr(router, "_load_all_backends", lambda: None)
    monkeypatch.setattr(
        router,
        "_keyword_hits",
        lambda query, limit, include_schema=False: [],
    )
    monkeypatch.setattr(router._embedder, "embed_query", lambda query: [0.0])
    monkeypatch.setattr(router._lance, "connect", lambda: object())
    monkeypatch.setattr(
        router._lance,
        "search_tools",
        lambda db, query, vec, top_k, servers=None: [
            {
                "name": "mist_widget_status",
                "server": "mist-core",
                "description": "Read widget status",
                "schema_json": "{}",
                "score": 0.99,
            },
            {
                "name": "mist_widget_diagnostic",
                "server": "mist-core",
                "description": "Run widget diagnostic",
                "schema_json": "{}",
                "score": 0.9,
            },
        ],
    )

    result = router.find_tool(
        "widget health",
        top_k=2,
        platform="mist",
        server="mist-core",
        capability="diagnostic",
    )

    assert [item["name"] for item in result] == ["mist_widget_diagnostic"]
    item = result[0]
    assert item["capability"] == "diagnostic"
    assert item["recommended_dispatcher"] == "invoke_tool"
    assert item["requires_write_enablement"] is False
    assert item["currently_enabled"] is True
    assert item["supports_dry_run"] is False
    assert item["supports_confirm"] is False
    assert item["requires_confirmation"] is False
    assert item["read_only"] is False
    assert item["destructive"] is False
    assert item["idempotent"] is False
    assert "execution_contract" not in item


def _configure_dispatch_backend(monkeypatch, *, annotation=IDEMPOTENT_WRITE):
    backend = MCPServer("router-dispatch")
    calls: list[dict] = []

    @backend.tool(annotations=annotation)
    def update_widget(
        widget_id: str,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        calls.append(
            {"widget_id": widget_id, "dry_run": dry_run, "confirm": confirm}
        )
        if dry_run:
            return {"dry_run": True, "widget_id": widget_id}
        if not confirm:
            return {"error": "confirm=True is required."}
        return {"updated": widget_id}

    tool = backend._tool_manager._tools["update_widget"]
    monkeypatch.setattr(router, "_BACKENDS", {"central-config": "demo.config"})
    monkeypatch.setattr(router, "_tool_index", {"update_widget": tool})
    monkeypatch.setattr(router, "_tool_servers", {"update_widget": backend})
    monkeypatch.setattr(
        router,
        "_tool_backend_names",
        {"update_widget": "central-config"},
    )
    monkeypatch.setattr(router, "_load_all_backends", lambda: None)
    return calls


def test_router_dispatch_adds_contract_to_dry_run_preview(monkeypatch):
    monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
    calls = _configure_dispatch_backend(monkeypatch)

    result = asyncio.run(
        router._dispatch_tool(object(), "update_widget", {"widget_id": "w1"})
    )

    assert result["dry_run"] is True
    assert result["widget_id"] == "w1"
    assert calls == [{"widget_id": "w1", "dry_run": True, "confirm": False}]
    contract = result["execution_contract"]
    assert contract["dry_run"]["state"] == "preview"
    assert contract["confirm"] == {"supported": True, "required": True}
    assert contract["idempotent"] is True
    assert contract["next_action"] == (
        "Review the preview, then call invoke_tool again with "
        "dry_run=false and confirm=true."
    )


def test_router_dispatch_blocks_invalid_gate_without_calling_backend(monkeypatch):
    monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "invalid")
    calls = _configure_dispatch_backend(monkeypatch)

    result = asyncio.run(
        router._dispatch_tool(
            object(),
            "update_widget",
            {"widget_id": "w1", "dry_run": False, "confirm": True},
        )
    )

    assert calls == []
    assert result["status"] == "blocked"
    assert result["execution_contract"]["gate"]["state"] == "invalid"
    assert result["execution_contract"]["dry_run"]["state"] == "execution_requested"
    assert result["execution_contract"]["next_action"].startswith(
        "Set HPE_MCP_CENTRAL_WRITES=1"
    )


def test_router_dispatch_preserves_execution_result_and_contract(monkeypatch):
    monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
    _configure_dispatch_backend(monkeypatch)

    result = asyncio.run(
        router._dispatch_tool(
            object(),
            "update_widget",
            {"widget_id": "w1", "dry_run": False, "confirm": True},
        )
    )

    assert result["updated"] == "w1"
    assert result["execution_contract"]["dry_run"]["state"] == "execution_requested"
    assert result["execution_contract"]["next_action"].startswith(
        "No further safety action"
    )


def test_router_dispatch_preserves_mcpserver_validation(monkeypatch):
    monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
    calls = _configure_dispatch_backend(monkeypatch)

    result = asyncio.run(router._dispatch_tool(object(), "update_widget", {}))

    assert calls == []
    assert "validation" in result["error"].lower()
    assert result["execution_contract"]["capability"] == "write"


def test_router_dispatch_does_not_wrap_reads_or_diagnostics(monkeypatch):
    backend = MCPServer("router-non-write")

    @backend.tool(annotations=router.READ_ONLY)
    def read_widget() -> dict:
        return {"kind": "read"}

    @backend.tool(annotations=router.DIAGNOSTIC)
    def diagnose_widget() -> dict:
        return {"kind": "diagnostic"}

    tools = dict(backend._tool_manager._tools)
    monkeypatch.setattr(router, "_BACKENDS", {"central-ops": "demo.ops"})
    monkeypatch.setattr(router, "_tool_index", tools)
    monkeypatch.setattr(router, "_tool_servers", {name: backend for name in tools})
    monkeypatch.setattr(
        router,
        "_tool_backend_names",
        {name: "central-ops" for name in tools},
    )
    monkeypatch.setattr(router, "_load_all_backends", lambda: None)

    read_result = asyncio.run(router._dispatch_tool(object(), "read_widget"))
    diagnostic_result = asyncio.run(
        router._dispatch_tool(object(), "diagnose_widget")
    )

    assert read_result == {"kind": "read"}
    assert diagnostic_result == {"kind": "diagnostic"}


def test_find_tool_reports_semantic_error_without_keyword_fallback(monkeypatch):
    def raise_index_missing(query):
        raise RuntimeError("index missing")

    monkeypatch.setattr(router, "_BACKEND", "lancedb")
    monkeypatch.setattr(
        router, "_BACKENDS", {"central-config": "hpe_networking_mcp.mcp_servers.config"}
    )
    monkeypatch.setattr(router, "_keyword_hits", lambda query, limit, include_schema=False: [])
    monkeypatch.setattr(router._embedder, "embed_query", raise_index_missing)

    result = router.find_tool("create vlan", top_k=1)

    assert result == [
        {
            "error": "Tool semantic search unavailable: RuntimeError: index missing",
            "hint": "Rebuild the tool index with `uv run python scripts/ingest_tools.py`.",
        }
    ]


def test_default_router_exposes_ask_docs_wrapper_when_rag_enabled():
    assert "ask_docs" in router.mcp._tool_manager._tools
    assert "search_hardware_catalog" in router.mcp._tool_manager._tools
    assert "compare_hardware" in router.mcp._tool_manager._tools


def test_hardware_catalog_wrapper_forwards_compact_search_arguments(monkeypatch):
    calls = []

    async def fake_invoke_tool(ctx, name, arguments=None):
        calls.append((ctx, name, arguments))
        return {"ok": True}

    monkeypatch.setattr(router, "invoke_tool", fake_invoke_tool)

    result = asyncio.run(
        router.search_hardware_catalog(
            object(), "CX 6300 PoE 48 port", vendor="aruba", limit=3
        )
    )

    assert result == {"ok": True}
    assert calls == [
        (
            calls[0][0],
            "search_hardware_catalog",
            {
                "query": "CX 6300 PoE 48 port",
                "vendor": "aruba",
                "include_specs": False,
                "limit": 3,
            },
        )
    ]


def test_hardware_comparison_wrapper_forwards_device_identifiers(monkeypatch):
    calls = []

    async def fake_invoke_tool(ctx, name, arguments=None):
        calls.append((ctx, name, arguments))
        return {"ok": True}

    monkeypatch.setattr(router, "invoke_tool", fake_invoke_tool)

    result = asyncio.run(router.compare_hardware(object(), ["JL665A", "JL727B"]))

    assert result == {"ok": True}
    assert calls == [(calls[0][0], "compare_hardware", {"devices": ["JL665A", "JL727B"]})]


def test_invoke_tool_is_marked_destructive_because_it_can_dispatch_writes():
    annotations = router.mcp._tool_manager._tools["invoke_tool"].annotations

    assert annotations.read_only_hint is False
    assert annotations.destructive_hint is True


def test_invoke_read_tool_is_marked_read_only():
    annotations = router.mcp._tool_manager._tools["invoke_read_tool"].annotations

    assert annotations.read_only_hint is True
    assert annotations.destructive_hint is False


def test_ask_docs_wrapper_forwards_backend_question_arg(monkeypatch):
    calls = []

    async def fake_invoke_tool(ctx, name, arguments=None):
        calls.append((ctx, name, arguments))
        return {"answer": "ok"}

    monkeypatch.setattr(router, "invoke_tool", fake_invoke_tool)

    result = asyncio.run(router.ask_docs(object(), "How do I configure WLANs?", top_k=2))

    assert result == {"answer": "ok"}
    assert calls == [
        (
            calls[0][0],
            "ask_docs",
            {"question": "How do I configure WLANs?", "top_k": 2},
        )
    ]


def test_ask_docs_wrapper_forwards_follow_up_context(monkeypatch):
    calls = []

    async def fake_invoke_tool(ctx, name, arguments=None):
        calls.append((ctx, name, arguments))
        return {"answer": "ok"}

    monkeypatch.setattr(router, "invoke_tool", fake_invoke_tool)

    result = asyncio.run(
        router.ask_docs(
            object(),
            "what about 10.16 code?",
            top_k=2,
            context="Comparing Juniper EX4000 with Aruba CX 6100.",
        )
    )

    assert result == {"answer": "ok"}
    assert calls[0][1:] == (
        "ask_docs",
        {
            "question": "what about 10.16 code?",
            "top_k": 2,
            "context": "Comparing Juniper EX4000 with Aruba CX 6100.",
        },
    )


def test_find_device_wrapper_forwards_backend_serial_arg(monkeypatch):
    calls = []

    async def fake_invoke_tool(ctx, name, arguments=None):
        calls.append((ctx, name, arguments))
        return {"serialNumber": "AP1"}

    monkeypatch.setattr(router, "invoke_tool", fake_invoke_tool)

    result = asyncio.run(router.find_device(object(), "AP1"))

    assert result == {"serialNumber": "AP1"}
    assert calls == [(calls[0][0], "find_device", {"serial_number": "AP1"})]


def test_find_client_wrapper_forwards_backend_mac_arg(monkeypatch):
    calls = []

    async def fake_invoke_tool(ctx, name, arguments=None):
        calls.append((ctx, name, arguments))
        return {"macAddress": "aa:bb:cc:dd:ee:ff"}

    monkeypatch.setattr(router, "invoke_tool", fake_invoke_tool)

    result = asyncio.run(router.find_client(object(), "aa:bb:cc:dd:ee:ff"))

    assert result == {"macAddress": "aa:bb:cc:dd:ee:ff"}
    assert calls == [
        (calls[0][0], "find_client", {"mac_or_ip": "aa:bb:cc:dd:ee:ff"})
    ]


def test_get_site_wrapper_forwards_backend_name_arg(monkeypatch):
    calls = []

    async def fake_invoke_tool(ctx, name, arguments=None):
        calls.append((name, arguments))
        return {"name": "HQ"}

    monkeypatch.setattr(router, "invoke_tool", fake_invoke_tool)

    result = asyncio.run(router.get_site(object(), "HQ"))

    assert result == {"name": "HQ"}
    assert calls == [("get_site", {"name": "HQ"})]


def test_list_clients_wrapper_forwards_filters_and_needs_no_site_lookup(monkeypatch):
    calls = []

    async def fake_invoke_tool(ctx, name, arguments=None):
        calls.append((name, arguments))
        return [{"macAddress": "aa:bb:cc:dd:ee:ff"}]

    monkeypatch.setattr(router, "invoke_tool", fake_invoke_tool)

    result = asyncio.run(router.list_clients(object(), connection_type="Wireless"))

    assert result == [{"macAddress": "aa:bb:cc:dd:ee:ff"}]
    assert calls == [
        (
            "list_clients",
            {
                "site_id": None,
                "connection_type": "Wireless",
                "ssid": None,
                "limit": 100,
                "offset": 0,
            },
        )
    ]


def test_mist_clients_wrapper_defaults_org_id_and_narrows_window(monkeypatch):
    monkeypatch.setattr(router, "_WRAPPER_CACHE", {})
    monkeypatch.setenv("MIST_ORG_ID", "org-from-env")
    calls = []

    async def fake_invoke_tool(ctx, name, arguments=None):
        calls.append((name, arguments))
        return {"data": {"results": []}}

    monkeypatch.setattr(router, "invoke_tool", fake_invoke_tool)

    result = asyncio.run(router.mist_clients(object(), minutes=15))

    assert result == {"data": {"results": []}}
    assert calls == [
        (
            "mist_search_org_wireless_clients",
            {"org_id": "org-from-env", "duration": "15m", "limit": 50},
        )
    ]


def test_mist_clients_wrapper_prefers_explicit_org_id_over_env(monkeypatch):
    monkeypatch.setattr(router, "_WRAPPER_CACHE", {})
    monkeypatch.setenv("MIST_ORG_ID", "org-from-env")
    calls = []

    async def fake_invoke_tool(ctx, name, arguments=None):
        calls.append(arguments)
        return {}

    monkeypatch.setattr(router, "invoke_tool", fake_invoke_tool)

    asyncio.run(router.mist_clients(object(), org_id="org-explicit"))

    assert calls[0]["org_id"] == "org-explicit"


def test_mist_clients_wrapper_errors_without_org_id(monkeypatch):
    monkeypatch.setattr(router, "_WRAPPER_CACHE", {})
    monkeypatch.delenv("MIST_ORG_ID", raising=False)
    calls = []

    async def fake_invoke_tool(ctx, name, arguments=None):
        calls.append((name, arguments))
        return {}

    monkeypatch.setattr(router, "invoke_tool", fake_invoke_tool)

    result = asyncio.run(router.mist_clients(object()))

    assert "error" in result
    assert calls == []


def test_mist_devices_wrapper_forwards_device_type_and_env_org_id(monkeypatch):
    monkeypatch.setattr(router, "_WRAPPER_CACHE", {})
    monkeypatch.setenv("MIST_ORG_ID", "org-from-env")
    calls = []

    async def fake_invoke_tool(ctx, name, arguments=None):
        calls.append((name, arguments))
        return {"inventory": {"items": []}}

    monkeypatch.setattr(router, "invoke_tool", fake_invoke_tool)

    result = asyncio.run(router.mist_devices(object(), device_type="ap"))

    assert result == {"inventory": {"items": []}}
    assert calls == [
        (
            "mist_list_org_inventory",
            {"org_id": "org-from-env", "device_type": "ap", "limit": 100},
        )
    ]


def test_mist_devices_wrapper_errors_without_org_id(monkeypatch):
    monkeypatch.setattr(router, "_WRAPPER_CACHE", {})
    monkeypatch.delenv("MIST_ORG_ID", raising=False)

    result = asyncio.run(router.mist_devices(object()))

    assert "error" in result


def test_mist_ports_wrapper_forwards_switch_mac_and_env_site_id(monkeypatch):
    monkeypatch.setattr(router, "_WRAPPER_CACHE", {})
    monkeypatch.setenv("MIST_SITE_ID", "site-from-env")
    calls = []

    async def fake_invoke_tool(ctx, name, arguments=None):
        calls.append((name, arguments))
        return {"ports": {"items": []}}

    monkeypatch.setattr(router, "invoke_tool", fake_invoke_tool)

    result = asyncio.run(router.mist_ports(object(), switch_mac="aabbccddeeff"))

    assert result == {"ports": {"items": []}}
    assert calls == [
        (
            "mist_list_switch_ports",
            {"site_id": "site-from-env", "switch_mac": "aabbccddeeff", "limit": 100},
        )
    ]


def test_mist_ports_wrapper_errors_without_site_id(monkeypatch):
    monkeypatch.setattr(router, "_WRAPPER_CACHE", {})
    monkeypatch.delenv("MIST_SITE_ID", raising=False)

    result = asyncio.run(router.mist_ports(object(), switch_mac="aabbccddeeff"))

    assert "error" in result


def test_mist_health_wrapper_defaults_mist_site_id_from_env(monkeypatch):
    monkeypatch.setattr(router, "_WRAPPER_CACHE", {})
    monkeypatch.setenv("MIST_SITE_ID", "site-from-env")
    calls = []

    async def fake_invoke_tool(ctx, name, arguments=None):
        calls.append((name, arguments))
        return {"status": "available"}

    monkeypatch.setattr(router, "invoke_tool", fake_invoke_tool)

    result = asyncio.run(router.mist_health(object()))

    assert result == {"status": "available"}
    assert calls == [("get_site_health", {"limit": 50, "mist_site_id": "site-from-env"})]


def test_mist_health_wrapper_includes_central_site_when_provided(monkeypatch):
    monkeypatch.setattr(router, "_WRAPPER_CACHE", {})
    monkeypatch.delenv("MIST_SITE_ID", raising=False)
    calls = []

    async def fake_invoke_tool(ctx, name, arguments=None):
        calls.append((name, arguments))
        return {}

    monkeypatch.setattr(router, "invoke_tool", fake_invoke_tool)

    asyncio.run(router.mist_health(object(), central_site_name="HQ"))

    assert calls == [("get_site_health", {"limit": 50, "central_site_name": "HQ"})]


def test_mist_health_wrapper_errors_without_any_site_identifier(monkeypatch):
    monkeypatch.setattr(router, "_WRAPPER_CACHE", {})
    monkeypatch.delenv("MIST_SITE_ID", raising=False)

    result = asyncio.run(router.mist_health(object()))

    assert "error" in result


def test_cached_dispatch_reuses_result_within_ttl(monkeypatch):
    monkeypatch.setattr(router, "_WRAPPER_CACHE", {})
    monkeypatch.delenv("HPE_MCP_ROUTER_WRAPPER_CACHE_TTL_SECONDS", raising=False)
    calls = []

    async def fake_invoke_tool(ctx, name, arguments=None):
        calls.append((name, arguments))
        return {"call": len(calls)}

    monkeypatch.setattr(router, "invoke_tool", fake_invoke_tool)

    ctx = object()
    first = asyncio.run(router._cached_dispatch(ctx, "some_tool", {"a": 1}))
    second = asyncio.run(router._cached_dispatch(ctx, "some_tool", {"a": 1}))

    assert first == second == {"call": 1}
    assert len(calls) == 1


def test_cached_dispatch_bypassed_when_ttl_is_zero(monkeypatch):
    monkeypatch.setattr(router, "_WRAPPER_CACHE", {})
    monkeypatch.setenv("HPE_MCP_ROUTER_WRAPPER_CACHE_TTL_SECONDS", "0")
    calls = []

    async def fake_invoke_tool(ctx, name, arguments=None):
        calls.append((name, arguments))
        return {"call": len(calls)}

    monkeypatch.setattr(router, "invoke_tool", fake_invoke_tool)

    ctx = object()
    first = asyncio.run(router._cached_dispatch(ctx, "some_tool", {"a": 1}))
    second = asyncio.run(router._cached_dispatch(ctx, "some_tool", {"a": 1}))

    assert first == {"call": 1}
    assert second == {"call": 2}
    assert len(calls) == 2


def test_generated_record_for_collision_alias_keeps_provenance_on_generated_tool(
    monkeypatch,
):
    """A generated tool renamed on collision keeps its manifest provenance, and
    the curated tool that kept the plain name is not reported as generated.

    ``register_generated_tools`` renames a generated operation to
    ``<name>_g<digest>`` when a curated tool already owns ``<name>``. The
    provenance lookup must follow the tool that was actually registered.
    """
    record = {
        "operation_id": "get_sensor_status",
        "operation_key": "GET /sensors/{id}/status",
        "manifest_platform": "uxi",
        "_collision_alias": "uxi_get_sensor_status_gdeadbeef",
    }
    monkeypatch.setattr(
        router,
        "_generated_tool_records",
        {
            "uxi_get_sensor_status": record,
            "uxi_get_sensor_status_gdeadbeef": record,
        },
    )
    # Both names registered => the plain name is the curated tool.
    monkeypatch.setattr(
        router,
        "_tool_index",
        {
            "uxi_get_sensor_status": object(),
            "uxi_get_sensor_status_gdeadbeef": object(),
        },
    )

    curated = router._generated_record_for("uxi_get_sensor_status")
    generated = router._generated_record_for("uxi_get_sensor_status_gdeadbeef")

    assert curated is None
    assert generated == {
        "operation_id": "get_sensor_status",
        "operation_key": "GET /sensors/{id}/status",
        "manifest_platform": "uxi",
    }
    assert "_collision_alias" not in generated


def test_generated_record_for_uncollided_name_is_generated(monkeypatch):
    """Without a collision, the manifest name itself is the generated tool."""
    record = {
        "operation_id": "get_sensors",
        "operation_key": "GET /sensors",
        "manifest_platform": "uxi",
        "_collision_alias": "uxi_sensors_get_gdeadbeef",
    }
    monkeypatch.setattr(
        router,
        "_generated_tool_records",
        {"uxi_sensors_get": record, "uxi_sensors_get_gdeadbeef": record},
    )
    monkeypatch.setattr(router, "_tool_index", {"uxi_sensors_get": object()})

    assert router._generated_record_for("uxi_sensors_get") == {
        "operation_id": "get_sensors",
        "operation_key": "GET /sensors",
        "manifest_platform": "uxi",
    }
    assert router._generated_record_for("not_generated_at_all") is None


# ---------------------------------------------------------------------------
# Unknown-tool "platform not configured" detection
# ---------------------------------------------------------------------------


def test_optional_product_tool_prefixes_derived_from_optional_backends():
    """Prefix map is derived from _OPTIONAL_BACKENDS, not a second hand-typed
    list, and every prefixed product has a human-readable label (so a newly
    added optional product can't silently ship an unlabeled hint)."""
    assert set(router._OPTIONAL_PRODUCT_TOOL_PREFIXES) == set(router._OPTIONAL_BACKENDS) - {
        "design"
    }
    for product, prefix in router._OPTIONAL_PRODUCT_TOOL_PREFIXES.items():
        assert prefix == f"{product}_"
    assert set(router._OPTIONAL_PRODUCT_LABELS) == set(router._OPTIONAL_PRODUCT_TOOL_PREFIXES)


def test_unconfigured_platform_hint_flags_disabled_optional_product(monkeypatch):
    monkeypatch.setattr(
        router, "_BACKENDS", {"central-config": "hpe_networking_mcp.mcp_servers.config"}
    )

    hint = router._unconfigured_platform_hint("mist_get_site_stats")

    assert hint == {
        "reason": "platform_not_configured",
        "platform": "mist",
        "hint": (
            "The 'mist' backend is not currently enabled. Set HPE_MCP_PRODUCTS=mist "
            "(or include it in HPE_MCP_TOOLSETS) and configure Mist credentials, "
            "then restart the server."
        ),
    }


def test_unconfigured_platform_hint_covers_every_prefixed_optional_product(monkeypatch):
    monkeypatch.setattr(router, "_BACKENDS", {})
    for product in router._OPTIONAL_PRODUCT_TOOL_PREFIXES:
        hint = router._unconfigured_platform_hint(f"{product}_status")
        assert hint is not None and hint["platform"] == product
        assert hint["reason"] == "platform_not_configured"


def test_unconfigured_platform_hint_returns_none_when_platform_already_enabled(monkeypatch):
    monkeypatch.setattr(router, "_BACKENDS", {"mist-core": "hpe_networking_mcp.mcp_servers.mist"})

    # A typo of an already-enabled platform's tool name must NOT be flagged
    # platform_not_configured -- it should fall through to fuzzy suggestions.
    assert router._unconfigured_platform_hint("mist_get_site_stat") is None


def test_unconfigured_platform_hint_returns_none_for_non_prefixed_name(monkeypatch):
    monkeypatch.setattr(router, "_BACKENDS", {})
    assert router._unconfigured_platform_hint("totally_bogus_tool_name") is None


def test_unconfigured_platform_hint_excludes_design_product(monkeypatch):
    """design.py's tools (list_diagram_icons, drawio_network_design_diagram,
    ...) don't share a "design_" prefix, so a "design_..." guess is left to
    the ordinary fuzzy fallback rather than a possibly-wrong platform claim."""
    monkeypatch.setattr(router, "_BACKENDS", {})
    assert router._unconfigured_platform_hint("design_anything_at_all") is None


def test_dispatch_unknown_tool_reports_platform_not_configured(monkeypatch):
    """invoke_tool/invoke_read_tool's shared _unknown_tool_error also carries
    the platform_not_configured reason for an unconfigured-platform-prefixed
    name -- no raised exception needed (that's only the router's top-level
    on_error path)."""
    monkeypatch.setattr(router, "_BACKENDS", {})
    monkeypatch.setattr(router, "_backend_load_errors", {})

    error = router._unknown_tool_error("clearpass_get_endpoint_by_mac")

    assert error["status"] == "unknown_tool"
    assert error["reason"] == "platform_not_configured"
    assert error["platform"] == "clearpass"
    assert error["suggestions"] == []


def test_dispatch_unknown_tool_without_platform_match_is_unchanged(monkeypatch):
    """A genuinely unknown name (no platform prefix) keeps the exact
    pre-existing shape -- no reason/platform/suggestions fields added."""
    monkeypatch.setattr(router, "_BACKENDS", {})
    monkeypatch.setattr(router, "_backend_load_errors", {})

    error = router._unknown_tool_error("totally_bogus_tool_name")

    assert error == {
        "error": "Unknown tool 'totally_bogus_tool_name'. Use find_tool to discover.",
        "tool": "totally_bogus_tool_name",
        "status": "unknown_tool",
    }


def test_router_middleware_chain_wires_platform_hint_resolver():
    middlewares = router.build_router_middlewares()
    unknown_tool_mw = next(
        m for m in middlewares if type(m).__name__ == "UnknownToolSuggestMiddleware"
    )
    assert unknown_tool_mw._platform_hint_resolver is router._unconfigured_platform_hint
