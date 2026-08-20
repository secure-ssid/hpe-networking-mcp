"""Offline routing gates for exact find_tool / lookup_api discovery.

Phase 2 exit criteria (no live LanceDB or credentials):
- exact METHOD /path and operationId queries rank the generated tool first
- generated-only matches stay compact and cite provenance
- keyword ranking still prefers schema-matching serial/site/workspace tools
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

import hpe_networking_mcp.mcp_servers.tool_router as router
from hpe_networking_mcp.mcp_servers.shared import READ_ONLY


FIRMWARE_RECORD = {
    "generated_tool": "get_firmware_compliance",
    "classification": "generated-only",
    "router_profile": "opt-in",
    "platform": "central",
    "capability": "read",
    "operation_id": "readFirmwareCompliance",
    "key": "GET /network-config/v1alpha1/firmware-compliance",
    "family": "network-config",
    "source_file": "firmware.json",
    "summary": "Get firmware compliance",
}


def _stub_search(monkeypatch, *, tools=None, backends=None, generated=None):
    tools = tools or {}
    backends = backends or {"central-config": "demo.config"}
    monkeypatch.setattr(router, "_BACKEND", "lancedb")
    monkeypatch.setattr(router, "_BACKENDS", backends)
    monkeypatch.setattr(router, "_tool_index", tools)
    monkeypatch.setattr(
        router,
        "_tool_backend_names",
        {name: next(iter(backends)) for name in tools},
    )
    monkeypatch.setattr(router, "_load_all_backends", lambda: None)
    monkeypatch.setattr(router, "_generated_tool_records", generated or {})
    monkeypatch.setattr(router._embedder, "embed_query", lambda query: [0.0])
    monkeypatch.setattr(router._lance, "connect", lambda: object())
    monkeypatch.setattr(router._lance, "search_tools", lambda *args, **kwargs: [])


def _stub_exact(monkeypatch, record=FIRMWARE_RECORD):
    monkeypatch.setattr(
        "hpe_networking_mcp.pipeline.clients.capability_coverage.lookup_exact_query",
        lambda query, platforms=None: record,
    )


def test_find_tool_exact_path_surfaces_generated_only_when_unloaded(monkeypatch):
    _stub_search(monkeypatch)
    _stub_exact(monkeypatch)

    result = router.find_tool("GET /network-config/v1alpha1/firmware-compliance", top_k=3)

    assert result[0]["name"] == "get_firmware_compliance"
    assert result[0]["match"] == "exact"
    assert result[0]["origin"] == "generated"
    assert result[0]["classification"] == "generated-only"
    assert result[0]["currently_enabled"] is False
    assert result[0]["file_path"].endswith(
        "firmware.json#GET /network-config/v1alpha1/firmware-compliance"
    )
    assert "schema" not in result[0]


def test_find_tool_exact_operation_id_ranks_top1_when_loaded(monkeypatch):
    backend = MCPServer("routing-exact")

    @backend.tool(annotations=READ_ONLY)
    def get_firmware_compliance() -> dict:
        """Get firmware compliance."""
        return {}

    _stub_search(
        monkeypatch,
        tools=dict(backend._tool_manager._tools),
        backends={"central-config": "demo.config"},
        generated={
            "get_firmware_compliance": {
                "operation_id": "readFirmwareCompliance",
                "operation_key": "GET /network-config/v1alpha1/firmware-compliance",
                "manifest_platform": "central",
            }
        },
    )
    _stub_exact(monkeypatch)

    result = router.find_tool("readFirmwareCompliance", top_k=1)

    assert result[0]["name"] == "get_firmware_compliance"
    assert result[0]["match"] == "exact"
    assert result[0]["currently_enabled"] is True
    assert result[0]["operation_id"] == "readFirmwareCompliance"


def test_find_tool_origin_filter_hides_generated_only_exact_hit(monkeypatch):
    _stub_search(monkeypatch)
    _stub_exact(monkeypatch)
    assert router.find_tool("GET /network-config/v1alpha1/firmware-compliance", origin="curated") == []


def test_keyword_scope_terms_boost_matching_params(monkeypatch):
    backend = MCPServer("routing-keyword")

    @backend.tool(annotations=READ_ONLY)
    def list_device_health(serial: str | None = None, limit: int = 20, offset: int = 0) -> dict:
        """Device health for one serial."""
        return {}

    @backend.tool(annotations=READ_ONLY)
    def list_device_groups() -> dict:
        """Device groups."""
        return {}

    _stub_search(
        monkeypatch,
        tools=dict(backend._tool_manager._tools),
        backends={"central-monitoring": "demo.monitoring"},
    )
    monkeypatch.setattr(
        "hpe_networking_mcp.pipeline.clients.capability_coverage.lookup_exact_query",
        lambda query, platforms=None: None,
    )

    result = router.find_tool("device serial", top_k=3)
    names = [item["name"] for item in result]
    assert names[0] == "list_device_health"
    assert result[0]["pagination"] == "limit-offset"
    assert result[0]["default_limit"] == 20


def test_offline_routing_set_top1_and_provenance(monkeypatch):
    """Stand-in for the Phase 2 routing eval: exact queries must be top-1 with source."""
    cases = [
        ("GET /network-config/v1alpha1/firmware-compliance", "get_firmware_compliance"),
        ("readFirmwareCompliance", "get_firmware_compliance"),
    ]
    _stub_search(monkeypatch)
    _stub_exact(monkeypatch)
    hits = 0
    with_source = 0
    for query, expected in cases:
        result = router.find_tool(query, top_k=3)
        if result and result[0]["name"] == expected:
            hits += 1
        if result and result[0].get("file_path"):
            with_source += 1
    assert hits / len(cases) >= 0.99
    assert with_source == len(cases)
