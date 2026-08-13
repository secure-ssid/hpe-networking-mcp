from __future__ import annotations

from scripts import ingest_tools


def test_server_specs_default_core_only(monkeypatch):
    monkeypatch.delenv("HPE_MCP_PRODUCTS", raising=False)

    specs = ingest_tools._server_specs()

    assert ("central-config", "hpe_networking_mcp.mcp_servers.config") in specs
    assert ("clearpass-core", "hpe_networking_mcp.mcp_servers.clearpass") not in specs


def test_server_specs_uses_products_argument(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCTS", "clearpass")

    specs = ingest_tools._server_specs("mist,apstra")

    assert ("clearpass-core", "hpe_networking_mcp.mcp_servers.clearpass") not in specs
    assert ("mist-core", "hpe_networking_mcp.mcp_servers.mist") in specs
    assert ("apstra-core", "hpe_networking_mcp.mcp_servers.apstra") in specs


def test_server_specs_can_include_all_optional_products(monkeypatch):
    monkeypatch.delenv("HPE_MCP_PRODUCTS", raising=False)

    specs = ingest_tools._server_specs("all")

    assert ("clearpass-core", "hpe_networking_mcp.mcp_servers.clearpass") in specs
    assert ("mist-core", "hpe_networking_mcp.mcp_servers.mist") in specs
    assert ("apstra-core", "hpe_networking_mcp.mcp_servers.apstra") in specs
    assert ("aos8-core", "hpe_networking_mcp.mcp_servers.aos8") in specs
    assert ("edgeconnect-core", "hpe_networking_mcp.mcp_servers.edgeconnect") in specs
