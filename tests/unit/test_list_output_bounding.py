"""Regression tests for list-tool output bounding and server-page offset handling."""

from __future__ import annotations

from unittest.mock import MagicMock

from hpe_networking_mcp.mcp_servers import config, monitoring, tool_router


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


def test_list_config_templates_does_not_reapply_offset_to_server_page(monkeypatch):
    client = MagicMock()
    # 150 templates total; the endpoint honored limit=100/offset=100 and
    # returned the 50 remaining. The tool must not slice them again.
    client._request.return_value = _Resp(
        {"items": [{"name": f"t{i}"} for i in range(100, 150)]}
    )
    monkeypatch.setattr(config, "get_client", lambda: client)

    result = config.list_config_templates(limit=100, offset=100)

    assert len(result["items"]) == 50
    assert result["_pagination"]["offset"] == 100
    assert result["_pagination"]["truncated"] is False


def test_list_config_assignments_bounds_by_default(monkeypatch):
    client = MagicMock()
    client._request.return_value = _Resp(
        {"items": [{"profile": f"a{i}"} for i in range(30)]}
    )
    monkeypatch.setattr(config, "get_client", lambda: client)

    result = config.list_config_assignments(limit=10, offset=10)

    assert [it["profile"] for it in result["items"]] == [f"a{i}" for i in range(10, 20)]
    assert result["_pagination"]["total"] == 30
    assert result["_pagination"]["truncated"] is True


def test_list_config_assignments_full_list_returns_raw_payload(monkeypatch):
    client = MagicMock()
    payload = {"items": [{"profile": f"a{i}"} for i in range(30)]}
    client._request.return_value = _Resp(payload)
    monkeypatch.setattr(config, "get_client", lambda: client)

    result = config.list_config_assignments(full_list=True)

    assert result == payload


def test_list_firmware_upgrades_bounds_by_default(monkeypatch):
    client = MagicMock()
    client.get.return_value = {
        "items": [
            {"serialNumber": f"SN{i}", "upgradeStatus": "IN_PROGRESS"} for i in range(5)
        ]
    }
    monkeypatch.setattr(config, "get_client", lambda: client)

    result = config.list_firmware_upgrades(limit=2, offset=2)

    assert [it["serialNumber"] for it in result["items"]] == ["SN2", "SN3"]
    assert result["_pagination"]["total"] == 5
    assert result["_pagination"]["truncated"] is True
    assert result["errors"] == []


def test_list_sites_does_not_reapply_offset_when_bound_lists_enabled(monkeypatch):
    monkeypatch.setenv("HPE_MCP_BOUND_LISTS", "1")
    mcp_client = MagicMock()
    # get_sites returned the server page for offset=1 (the one remaining site).
    mcp_client.get_sites.return_value = [{"siteId": "s2", "name": "Lab"}]
    monkeypatch.setattr(monitoring, "get_mcp_client", lambda: mcp_client)

    result = monitoring.list_sites(limit=1, offset=1)

    mcp_client.get_sites.assert_called_once_with(limit=1, offset=1)
    assert result["items"] == [{"siteId": "s2", "name": "Lab"}]
    assert result["_pagination"]["offset"] == 1


def test_router_wrappers_expose_only_backend_supported_params():
    tools = tool_router.mcp._tool_manager._tools

    sites_props = tools["list_sites"].parameters["properties"]
    devices_props = tools["list_devices"].parameters["properties"]
    scopes_props = tools["list_scopes"].parameters["properties"]

    # Backend monitoring.list_sites/list_devices have no full_list parameter —
    # advertising one here would be silently dropped by MCPServer.
    assert "full_list" not in sites_props
    assert "full_list" not in devices_props
    # Backend monitoring.list_scopes pages; the wrapper must forward paging.
    assert {"limit", "offset", "full_list"} <= set(scopes_props)
