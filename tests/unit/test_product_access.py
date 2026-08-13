from __future__ import annotations

from hpe_networking_mcp.mcp_servers import shared, tool_router
from scripts import ingest_tools


def test_product_access_defaults_to_read_only_when_unset(monkeypatch):
    monkeypatch.delenv("HPE_MCP_PRODUCT_ACCESS", raising=False)

    assert shared.optional_product_access_mode() == "read-only"
    assert shared.optional_product_writes_allowed() is False
    assert tool_router._product_access() == "read-only"
    assert ingest_tools._product_access() == "read-only"


def test_product_access_accepts_read_write_aliases(monkeypatch):
    for value in ("read-write", "readwrite", "read_write", "rw"):
        monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", value)

        assert shared.optional_product_access_mode() == "read-write"
        assert shared.optional_product_writes_allowed() is True
        assert tool_router._product_access() == "read-write"
        assert ingest_tools._product_access() == "read-write"


def test_product_access_invalid_value_fails_closed(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-wrtie")

    assert shared.optional_product_access_mode() == "read-only"
    assert shared.optional_product_writes_allowed() is False
    assert tool_router._product_access() == "read-only"
    assert ingest_tools._product_access() == "read-only"


def test_direct_write_block_message_mentions_invalid_access(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-wrtie")

    out = shared.optional_product_write_blocked("clearpass_write")

    assert out["status"] == "blocked"
    assert "read-only or invalid" in out["error"]
