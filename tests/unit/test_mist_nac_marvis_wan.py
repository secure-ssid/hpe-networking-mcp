from __future__ import annotations

import asyncio

import hpe_networking_mcp.mcp_servers.mist as mist


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


def _fake_get_client(payload, calls=None):
    class _FakeAsyncClient:
        def __init__(self, timeout=None, **_ignored):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            if calls is not None:
                calls.append({"url": url, "headers": headers or {}, "params": params or {}})
            return _FakeResp(200, payload)

    return _FakeAsyncClient


def _fake_write_client(calls):
    class _FakeAsyncClient:
        def __init__(self, timeout=None, **_ignored):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, headers=None, params=None, json=None):
            calls.append(
                {
                    "method": method,
                    "url": url,
                    "headers": headers or {},
                    "params": params or {},
                    "json": json,
                }
            )
            return _FakeResp(200, {"ok": True})

    return _FakeAsyncClient


def _configure(monkeypatch):
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")


# ---------------------------------------------------------------------------
# NAC / Access Assurance
# ---------------------------------------------------------------------------


def test_mist_list_nac_tags_compacts_and_pages(monkeypatch):
    _configure(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        mist.httpx,
        "AsyncClient",
        _fake_get_client([{"id": "tag1", "name": "iot", "match": "os", "junk": "drop"}], calls),
    )

    out = asyncio.run(mist.mist_list_nac_tags("org1", limit=25, page=2))

    assert calls[0]["url"] == "https://api.mist.com/api/v1/orgs/org1/nactags"
    assert calls[0]["params"] == {"limit": 25, "page": 2}
    assert out["nac_tags"]["items"] == [{"id": "tag1", "name": "iot", "match": "os"}]


def test_mist_list_nac_portals_compacts(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(
        mist.httpx,
        "AsyncClient",
        _fake_get_client(
            [{"id": "portal1", "name": "Guest", "type": "guest_portal", "extra": "x"}]
        ),
    )

    out = asyncio.run(mist.mist_list_nac_portals("org1"))

    assert out["nac_portals"]["items"] == [
        {"id": "portal1", "name": "Guest", "type": "guest_portal"}
    ]


def test_mist_list_nac_idps_reads_from_org_setting(monkeypatch):
    _configure(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        mist.httpx,
        "AsyncClient",
        _fake_get_client(
            {
                "mist_nac": {
                    "idps": [
                        {"id": "idp1", "user_realms": ["corp.com"], "junk": "drop"},
                    ]
                },
                "other_setting": 1,
            },
            calls,
        ),
    )

    out = asyncio.run(mist.mist_list_nac_idps("org1"))

    assert calls[0]["url"] == "https://api.mist.com/api/v1/orgs/org1/setting"
    assert out["nac_idps"]["items"] == [{"id": "idp1", "user_realms": ["corp.com"]}]


def test_mist_list_user_macs_compacts(monkeypatch):
    _configure(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        mist.httpx,
        "AsyncClient",
        _fake_get_client(
            [{"mac": "001122334455", "labels": ["iot"], "vlan": "20", "raw": "x"}], calls
        ),
    )

    out = asyncio.run(mist.mist_list_user_macs("org1"))

    assert calls[0]["url"] == "https://api.mist.com/api/v1/orgs/org1/usermacs/search"
    assert out["user_macs"]["items"] == [
        {"mac": "001122334455", "labels": ["iot"], "vlan": "20"}
    ]


def test_mist_upsert_user_mac_dry_run_normalizes_mac(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(
        mist.mist_upsert_user_mac("org1", "00:11-22.33:44:55", labels=["iot"], vlan="20")
    )

    assert out["dry_run"] is True
    assert out["normalized_mac"] == "001122334455"
    assert out["json"] == {"mac": "001122334455", "labels": ["iot"], "vlan": "20"}


def test_mist_upsert_user_mac_executes_with_confirm(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    calls: list = []
    monkeypatch.setattr(mist.httpx, "AsyncClient", _fake_write_client(calls))

    out = asyncio.run(
        mist.mist_upsert_user_mac(
            "org1", "001122334455", labels=["iot"], dry_run=False, confirm=True
        )
    )

    assert out["status_code"] == 200
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "https://api.mist.com/api/v1/orgs/org1/usermacs"
    assert calls[0]["json"] == {"mac": "001122334455", "labels": ["iot"]}


# ---------------------------------------------------------------------------
# Marvis AI
# ---------------------------------------------------------------------------


def test_mist_search_marvis_clients_compacts(monkeypatch):
    _configure(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        mist.httpx,
        "AsyncClient",
        _fake_get_client(
            [{"device_id": "dev1", "hostname": "phone1", "model": "Pixel", "x": "y"}], calls
        ),
    )

    out = asyncio.run(mist.mist_search_marvis_clients("org1", hostname="phone1"))

    assert calls[0]["url"] == (
        "https://api.mist.com/api/v1/orgs/org1/stats/marvisclients/search"
    )
    assert calls[0]["params"] == {"hostname": "phone1", "limit": 50}
    assert out["marvis_clients"]["items"] == [
        {"device_id": "dev1", "hostname": "phone1", "model": "Pixel"}
    ]


def test_mist_get_client_insights_normalizes_mac(monkeypatch):
    _configure(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        mist.httpx, "AsyncClient", _fake_get_client({"num-clients": 1, "score": 90}, calls)
    )

    out = asyncio.run(mist.mist_get_client_insights("site1", "00:11:22:33:44:55"))

    assert calls[0]["url"] == (
        "https://api.mist.com/api/v1/sites/site1/insights/client/001122334455"
    )
    assert out["normalized_mac"] == "001122334455"
    assert out["insights"] == {"num-clients": 1, "score": 90}


def test_mist_search_events_uses_device_events_endpoint(monkeypatch):
    _configure(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        mist.httpx,
        "AsyncClient",
        _fake_get_client(
            {"results": [{"type": "AP_DISCONNECTED", "mac": "aa", "timestamp": 1, "raw": "x"}]},
            calls,
        ),
    )

    out = asyncio.run(mist.mist_search_events("site1", event_type="AP_DISCONNECTED"))

    assert calls[0]["url"] == (
        "https://api.mist.com/api/v1/sites/site1/devices/events/search"
    )
    assert calls[0]["params"] == {
        "type": "AP_DISCONNECTED",
        "limit": 100,
        "duration": "1d",
        "sort": "-timestamp",
    }
    assert out["events"]["items"] == [{"type": "AP_DISCONNECTED", "mac": "aa", "timestamp": 1}]


def test_mist_get_marvis_settings_returns_nested_marvis_object(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(
        mist.httpx,
        "AsyncClient",
        _fake_get_client(
            {
                "marvis": {"disable_proactive_monitoring": False, "self_driving": {"x": 1}},
                "other_setting": 1,
            }
        ),
    )

    out = asyncio.run(mist.mist_get_marvis_settings("org1"))

    assert out["marvis_settings"] == {
        "disable_proactive_monitoring": False,
        "self_driving": {"x": 1},
    }


def test_mist_set_marvis_settings_wraps_body_under_marvis_key(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(
        mist.mist_set_marvis_settings("org1", {"disable_proactive_monitoring": True})
    )

    assert out["dry_run"] is True
    assert out["method"] == "PUT"
    assert out["path"] == "/api/v1/orgs/org1/setting"
    assert out["json"] == {"marvis": {"disable_proactive_monitoring": True}}


# ---------------------------------------------------------------------------
# Org inventory and device claims
# ---------------------------------------------------------------------------


def test_mist_list_org_inventory_excludes_magic_claim_code(monkeypatch):
    _configure(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        mist.httpx,
        "AsyncClient",
        _fake_get_client(
            [
                {
                    "id": "dev1",
                    "mac": "001122334455",
                    "model": "AP34",
                    "type": "ap",
                    "magic": "SECRET-CODE",
                }
            ],
            calls,
        ),
    )

    out = asyncio.run(mist.mist_list_org_inventory("org1", device_type="ap"))

    assert calls[0]["params"] == {"type": "ap", "limit": 100, "page": 1}
    assert out["inventory"]["items"] == [
        {"id": "dev1", "mac": "001122334455", "model": "AP34", "type": "ap"}
    ]


def test_mist_claim_devices_masks_codes_in_preview(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(mist.mist_claim_devices("org1", ["ABCDE-11111-FGHIJ"]))

    assert out["dry_run"] is True
    assert out["json"] == ["...GHIJ"]


def test_mist_claim_devices_requires_at_least_one_code(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(mist.mist_claim_devices("org1", []))

    assert "error" in out


def test_mist_claim_devices_executes_with_confirm_sends_bare_array(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    calls: list = []
    monkeypatch.setattr(mist.httpx, "AsyncClient", _fake_write_client(calls))

    out = asyncio.run(
        mist.mist_claim_devices("org1", ["ABCDE-11111-FGHIJ"], dry_run=False, confirm=True)
    )

    assert out["status_code"] == 200
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "https://api.mist.com/api/v1/orgs/org1/inventory"
    assert calls[0]["json"] == ["ABCDE-11111-FGHIJ"]


# ---------------------------------------------------------------------------
# Wired Assurance
# ---------------------------------------------------------------------------


def test_mist_list_switches_uses_unified_devices_endpoint(monkeypatch):
    _configure(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        mist.httpx,
        "AsyncClient",
        _fake_get_client(
            [{"id": "sw1", "mac": "aa", "model": "6300", "status": "connected", "junk": 1}],
            calls,
        ),
    )

    out = asyncio.run(mist.mist_list_switches("site1", limit=50, page=2))

    assert calls[0]["url"] == "https://api.mist.com/api/v1/sites/site1/stats/devices"
    assert calls[0]["params"] == {"type": "switch", "status": "all", "limit": 50, "page": 2}
    assert out["switches"]["items"] == [
        {"id": "sw1", "mac": "aa", "model": "6300", "status": "connected"}
    ]


def test_mist_list_switch_ports_uses_ports_search_endpoint(monkeypatch):
    _configure(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        mist.httpx,
        "AsyncClient",
        _fake_get_client(
            [{"port_id": "ge-0/0/1", "up": True, "speed": 1000, "junk": "x"}], calls
        ),
    )

    out = asyncio.run(mist.mist_list_switch_ports("site1", "00:11:22:33:44:55"))

    assert calls[0]["url"] == "https://api.mist.com/api/v1/sites/site1/stats/ports/search"
    assert calls[0]["params"] == {
        "mac": "001122334455",
        "device_type": "switch",
        "limit": 100,
    }
    assert out["normalized_mac"] == "001122334455"
    assert out["ports"]["items"] == [{"port_id": "ge-0/0/1", "up": True, "speed": 1000}]


# ---------------------------------------------------------------------------
# WAN Assurance
# ---------------------------------------------------------------------------


def test_mist_list_gateways_uses_unified_devices_endpoint(monkeypatch):
    _configure(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        mist.httpx,
        "AsyncClient",
        _fake_get_client(
            [{"id": "gw1", "model": "SSR120", "status": "connected", "is_ha": True, "junk": 1}],
            calls,
        ),
    )

    out = asyncio.run(mist.mist_list_gateways("site1", limit=50, page=3))

    assert calls[0]["url"] == "https://api.mist.com/api/v1/sites/site1/stats/devices"
    assert calls[0]["params"] == {"type": "gateway", "status": "all", "limit": 50, "page": 3}
    assert out["gateways"]["items"] == [
        {"id": "gw1", "model": "SSR120", "status": "connected", "is_ha": True}
    ]


def test_mist_get_gateway_uses_unified_devices_endpoint(monkeypatch):
    _configure(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        mist.httpx,
        "AsyncClient",
        _fake_get_client({"id": "gw1", "model": "SRX320", "status": "connected"}, calls),
    )

    out = asyncio.run(mist.mist_get_gateway("site1", "gw1"))

    assert calls[0]["url"] == "https://api.mist.com/api/v1/sites/site1/stats/devices/gw1"
    assert out["gateway"] == {"id": "gw1", "model": "SRX320", "status": "connected"}


# ---------------------------------------------------------------------------
# Curated read workflow: site assurance snapshot
# ---------------------------------------------------------------------------


def test_mist_get_site_assurance_snapshot_composes_existing_curated_reads(monkeypatch):
    _configure(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        mist.httpx,
        "AsyncClient",
        _fake_get_client([{"id": "x", "mac": "aa", "severity": "warn"}], calls),
    )

    out = asyncio.run(mist.mist_get_site_assurance_snapshot("site1", limit=25))

    urls = {call["url"] for call in calls}
    assert urls == {
        "https://api.mist.com/api/v1/sites/site1/stats/devices",
        "https://api.mist.com/api/v1/sites/site1/alarms/search",
    }
    assert set(out["sections"]) == {"switches", "gateways", "alarms"}
    assert out["site_id"] == "site1"
    assert out["degraded"] is False
    assert out["sections"]["switches"]["switches"]["items"]
    assert out["sections"]["gateways"]["gateways"]["items"]
    assert out["sections"]["alarms"]["alarms"]["items"]


def test_mist_get_site_assurance_snapshot_can_narrow_sections_and_flags_partial_errors(
    monkeypatch,
):
    _configure(monkeypatch)
    monkeypatch.setattr(
        mist.httpx,
        "AsyncClient",
        _fake_get_client([{"id": "x"}]),
    )

    out = asyncio.run(
        mist.mist_get_site_assurance_snapshot(
            "site1", include_gateways=False, include_alarms=False
        )
    )
    assert set(out["sections"]) == {"switches"}
    assert out["degraded"] is False

    none_selected = asyncio.run(
        mist.mist_get_site_assurance_snapshot(
            "site1",
            include_switches=False,
            include_gateways=False,
            include_alarms=False,
        )
    )
    assert "error" in none_selected


def test_mist_get_site_assurance_snapshot_reports_degraded_on_section_error(monkeypatch):
    monkeypatch.delenv("MIST_API_TOKEN", raising=False)
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")

    out = asyncio.run(mist.mist_get_site_assurance_snapshot("site1"))

    assert out["degraded"] is True
    assert all("error" in section for section in out["sections"].values())
