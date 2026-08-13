from unittest.mock import MagicMock

from hpe_networking_mcp.pipeline.clients.mcp_client import MCPClient


def test_get_device_scope_id_uses_device_inventory():
    central = MagicMock()
    central.get.return_value = {
        "devices": [{"serialNumber": "CN123", "scopeId": "scope-1"}]
    }

    assert MCPClient(central).get_device_scope_id("CN123") == "scope-1"
    central.get.assert_called_once_with(
        "/network-monitoring/v1/device-inventory",
        params={"filter": "serialNumber eq 'CN123'", "limit": 1},
    )


def test_get_device_scope_id_accepts_scopeID_casing():
    central = MagicMock()
    central.get.return_value = {
        "devices": [{"serialNumber": "CN123", "scopeID": "scope-2"}]
    }

    assert MCPClient(central).get_device_scope_id("CN123") == "scope-2"


def test_get_device_scope_id_returns_none_for_empty_result():
    central = MagicMock()
    central.get.return_value = {"devices": []}

    assert MCPClient(central).get_device_scope_id("CN123") is None


def test_get_device_scope_id_returns_none_on_client_error():
    central = MagicMock()
    central.get.side_effect = RuntimeError("boom")

    assert MCPClient(central).get_device_scope_id("CN123") is None


def test_get_device_by_serial_uses_server_side_filter_first():
    central = MagicMock()
    central.get.return_value = {
        "devices": [{"serialNumber": "CN999", "deviceType": "SWITCH"}]
    }

    device = MCPClient(central).get_device_by_serial("CN999")

    assert device == {"serialNumber": "CN999", "deviceType": "SWITCH"}
    central.get.assert_called_once_with(
        "/network-monitoring/v1/device-inventory",
        params={"filter": "serialNumber eq 'CN999'", "limit": 1},
    )


def test_get_device_by_serial_falls_back_to_v1alpha1_when_v1_errors():
    central = MagicMock()
    central.get.side_effect = [
        RuntimeError("v1 404"),
        {"devices": [{"serialNumber": "CN999", "deviceType": "SWITCH"}]},
    ]

    device = MCPClient(central).get_device_by_serial("CN999")

    assert device == {"serialNumber": "CN999", "deviceType": "SWITCH"}
    assert central.get.call_args_list[0] == (
        ("/network-monitoring/v1/device-inventory",),
        {"params": {"filter": "serialNumber eq 'CN999'", "limit": 1}},
    )
    assert central.get.call_args_list[1] == (
        ("/network-monitoring/v1alpha1/device-inventory",),
        {"params": {"filter": "serialNumber eq 'CN999'", "limit": 1}},
    )


def test_get_device_by_serial_escapes_odata_quotes_in_serial():
    central = MagicMock()
    # Force fallback scan: server-side filter finds nothing, then a single
    # short cursor page (no `next`) that also finds nothing.
    central.get.side_effect = [
        {"devices": []},
        {"devices": []},
    ]

    assert MCPClient(central).get_device_by_serial("CN'123") is None
    assert central.get.call_args_list[0] == (
        ("/network-monitoring/v1/device-inventory",),
        {"params": {"filter": "serialNumber eq 'CN''123'", "limit": 1}},
    )
    # Fallback scan uses cursor pagination (limit only, no `next` on page 1).
    assert central.get.call_args_list[1] == (
        ("/network-monitoring/v1/device-inventory",),
        {"params": {"limit": 100}},
    )


def test_get_devices_page_returns_items_and_next_cursor():
    central = MagicMock()
    central.get.return_value = {"devices": [{"serialNumber": "CN1"}], "next": "21"}

    items, cursor = MCPClient(central).get_devices_page(limit=20)

    assert items == [{"serialNumber": "CN1"}]
    assert cursor == "21"
    central.get.assert_called_once_with(
        "/network-monitoring/v1/device-inventory", params={"limit": 20}
    )


def test_get_devices_translates_legacy_offset_to_next_cursor():
    central = MagicMock()
    central.get.return_value = {"devices": []}

    MCPClient(central).get_devices(limit=50, offset=40)

    central.get.assert_called_once_with(
        "/network-monitoring/v1/device-inventory",
        params={"limit": 50, "next": "41"},
    )


def test_get_site_by_name_searches_full_site_list():
    central = MagicMock()
    central.get.return_value = {
        "items": [{"scopeName": f"site-{idx}", "id": f"id-{idx}"} for idx in range(60)]
    }

    site = MCPClient(central).get_site_by_name("site-55")

    assert site == {"scopeName": "site-55", "id": "id-55"}
    central.get.assert_called_once_with("/network-config/v1/sites")


def test_get_sites_applies_client_side_limit_and_offset():
    central = MagicMock()
    central.get.return_value = {
        "items": [
            {"id": "site-1"},
            {"id": "site-2"},
            {"id": "site-3"},
        ]
    }

    assert MCPClient(central).get_sites(limit=1, offset=1) == [{"id": "site-2"}]
    central.get.assert_called_once_with("/network-config/v1/sites")


def test_get_sites_clamps_negative_offset_and_large_limit():
    central = MagicMock()
    central.get.return_value = {"sites": [{"id": f"site-{idx}"} for idx in range(250)]}

    sites = MCPClient(central).get_sites(limit=999, offset=-10)

    assert len(sites) == 200
    assert sites[0] == {"id": "site-0"}


def test_get_clients_page_uses_cursor_not_offset():
    central = MagicMock()
    central.get.return_value = {"items": [], "next": "101"}

    items, cursor = MCPClient(central).get_clients_page(site_id="site-1", limit=999)

    assert items == []
    assert cursor == "101"
    central.get.assert_called_once_with(
        "/network-monitoring/v1/clients",
        params={"limit": 200, "site-id": "site-1"},
    )


def test_get_clients_omits_cursor_on_first_page():
    central = MagicMock()
    central.get.return_value = {"items": []}

    assert MCPClient(central).get_clients(site_id="site-1", limit=999, offset=-5) == []
    central.get.assert_called_once_with(
        "/network-monitoring/v1/clients",
        params={"limit": 200, "site-id": "site-1"},
    )


def test_get_clients_translates_legacy_offset_to_next_cursor():
    central = MagicMock()
    central.get.return_value = {"items": []}

    MCPClient(central).get_clients(limit=100, offset=25)

    central.get.assert_called_once_with(
        "/network-monitoring/v1/clients",
        params={"limit": 100, "next": "26"},
    )


def test_find_client_sweeps_pages_via_server_issued_cursor():
    central = MagicMock()
    central.get.side_effect = [
        {"items": [{"macAddress": "aa:bb:cc:dd:ee:ff"}], "next": "2"},
        {"items": [{"macAddress": "11:22:33:44:55:66"}]},
    ]

    found = MCPClient(central).find_client("11:22:33:44:55:66")

    assert found == {"macAddress": "11:22:33:44:55:66"}
    assert central.get.call_args_list[0] == (
        ("/network-monitoring/v1/clients",),
        {"params": {"limit": 100}},
    )
    assert central.get.call_args_list[1] == (
        ("/network-monitoring/v1/clients",),
        {"params": {"limit": 100, "next": "2"}},
    )


def test_get_alerts_uses_cursor_not_offset():
    central = MagicMock()
    central.get.return_value = {"items": []}

    assert MCPClient(central).get_alerts(severity="critical", limit=999, offset=25) == []
    central.get.assert_called_once_with(
        "/network-notifications/v1/alerts",
        params={
            "filter": "status eq 'Active' and severity eq 'Critical'",
            "limit": 200,
            "next": "26",
        },
    )
