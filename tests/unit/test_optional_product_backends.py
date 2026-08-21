from __future__ import annotations

import asyncio

import pytest

import hpe_networking_mcp.mcp_servers.aos8 as aos8
import hpe_networking_mcp.mcp_servers.apstra as apstra
import hpe_networking_mcp.mcp_servers.edgeconnect as edgeconnect
import hpe_networking_mcp.mcp_servers.uxi as uxi


@pytest.fixture(autouse=True)
def _enable_validated_legacy_edgeconnect_for_backend_tests(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_ALLOW_LEGACY_API", "1")


class _Resp:
    status_code = 200
    text = '{"ok":true}'

    def json(self):
        return {"ok": True}


class _ListResp:
    status_code = 200
    text = '[{"id":1},{"id":2},{"id":3}]'

    def json(self):
        return [{"id": 1}, {"id": 2}, {"id": 3}]


@pytest.mark.parametrize(
    ("module", "status_func", "env_base", "env_token"),
    [
        (apstra, apstra.apstra_status, "APSTRA_BASE_URL", "APSTRA_API_TOKEN"),
        (aos8, aos8.aos8_status, "AOS8_BASE_URL", "AOS8_API_TOKEN"),
        (
            edgeconnect,
            edgeconnect.edgeconnect_status,
            "EDGECONNECT_BASE_URL",
            "EDGECONNECT_API_TOKEN",
        ),
    ],
)
def test_optional_product_status_unconfigured(
    module,
    status_func,
    env_base,
    env_token,
    monkeypatch,
):
    monkeypatch.delenv(env_base, raising=False)
    monkeypatch.delenv(env_token, raising=False)

    out = status_func()

    assert out["configured"] is False
    assert out["has_token"] is False


def test_uxi_status_unconfigured(monkeypatch):
    monkeypatch.delenv("UXI_CLIENT_ID", raising=False)
    monkeypatch.delenv("UXI_CLIENT_SECRET", raising=False)

    out = uxi.uxi_status()

    assert out["configured"] is False
    assert out["has_client_id"] is False
    assert out["has_client_secret"] is False
    assert len(out["tools"]) == len(set(out["tools"]))
    assert {
        "uxi_list_agent_group_assignments",
        "uxi_list_sensor_group_assignments",
        "uxi_list_network_group_assignments",
        "uxi_list_service_test_group_assignments",
    }.issubset(out["tools"])
    assert "uxi_delete_sensor" not in out["tools"]


def test_uxi_get_rejects_absolute_url(monkeypatch):
    monkeypatch.setenv("UXI_CLIENT_ID", "client")
    monkeypatch.setenv("UXI_CLIENT_SECRET", "secret")

    out = asyncio.run(uxi.uxi_get("https://evil.example/sensors"))

    assert "error" in out
    assert "relative API path" in out["error"]


def test_uxi_get_rejects_unsupported_path(monkeypatch):
    monkeypatch.setenv("UXI_CLIENT_ID", "client")
    monkeypatch.setenv("UXI_CLIENT_SECRET", "secret")

    out = asyncio.run(uxi.uxi_get("/admin/secrets"))

    assert "error" in out
    assert "/sensors/{id}/status" in out["error"]


def test_uxi_get_rejects_sensor_paths_other_than_status(monkeypatch):
    monkeypatch.setenv("UXI_CLIENT_ID", "client")
    monkeypatch.setenv("UXI_CLIENT_SECRET", "secret")

    out = asyncio.run(uxi.uxi_get("/sensors/s1/details"))

    assert "error" in out
    assert "/sensors/{id}/status" in out["error"]


def test_uxi_list_sensors_fetches_token_and_compacts(monkeypatch):
    calls = []

    class _TokenResp:
        status_code = 200
        text = '{"access_token":"tok","expires_in":3600}'

        def json(self):
            return {"access_token": "tok", "expires_in": 3600}

    class _SensorsResp:
        status_code = 200
        text = '{"items":[{"id":"s1","name":"Sensor","raw":"omit"}],"next":null}'

        def json(self):
            return {
                "items": [
                    {
                        "id": "s1",
                        "name": "Sensor",
                        "modelNumber": "G6",
                        "ethernetMacAddress": "00:11:22:33:44:55",
                        "groupName": "Lab",
                        "raw": "omit",
                    }
                ],
                "next": None,
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            calls.append(("init", timeout))

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, data=None, headers=None):
            calls.append(("post", url, data, headers))
            return _TokenResp()

        async def get(self, url, headers=None, params=None):
            calls.append(("get", url, headers, params))
            return _SensorsResp()

    monkeypatch.setenv("UXI_CLIENT_ID", "client")
    monkeypatch.setenv("UXI_CLIENT_SECRET", "secret")
    monkeypatch.setenv("UXI_BASE_URL", "https://api.capenetworks.com/networking-uxi/v1alpha1")
    monkeypatch.setattr(uxi, "_TOKEN_CACHE", {})
    monkeypatch.setattr(uxi.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(uxi.uxi_list_sensors(next_cursor="cursor", page_size=999))

    assert out["status_code"] == 200
    assert out["data"]["items"] == [
        {
            "id": "s1",
            "name": "Sensor",
            "modelNumber": "G6",
            "ethernetMacAddress": "00:11:22:33:44:55",
            "groupName": "Lab",
        }
    ]
    assert ("post", "https://sso.common.cloud.hpe.com/as/token.oauth2", {
        "grant_type": "client_credentials",
        "client_id": "client",
        "client_secret": "secret",
    }, {"Content-Type": "application/x-www-form-urlencoded"}) in calls
    assert calls[-1] == (
        "get",
        "https://api.capenetworks.com/networking-uxi/v1alpha1/sensors",
        {"Authorization": "Bearer tok", "Accept": "application/json"},
        {"limit": 100, "next": "cursor"},
    )


def test_uxi_rejects_unsafe_token_url_before_sending_secret(monkeypatch):
    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            raise AssertionError("token request should not be attempted")

    monkeypatch.setenv("UXI_CLIENT_ID", "client")
    monkeypatch.setenv("UXI_CLIENT_SECRET", "secret")
    monkeypatch.setenv("UXI_TOKEN_URL", "https://127.0.0.1/token")
    monkeypatch.setattr(uxi.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(uxi.uxi_list_sensors())

    assert "error" in out
    assert "UXI token URL" in out["error"]


def test_uxi_sensor_status_rejects_unsafe_id():
    out = asyncio.run(uxi.uxi_get_sensor_status("../bad"))

    assert "error" in out
    assert "UXI resource IDs" in out["error"]


def test_apstra_get_rejects_non_api_path(monkeypatch):
    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_API_TOKEN", "secret")

    out = asyncio.run(apstra.apstra_get("/bad/path"))

    assert "error" in out
    assert "/api/*" in out["error"]


def test_apstra_get_rejects_dot_segment_bypass(monkeypatch):
    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_API_TOKEN", "secret")

    out = asyncio.run(apstra.apstra_get("/api/../admin"))

    assert "error" in out
    assert "dot segments" in out["error"]


def test_apstra_get_rejects_double_encoded_dot_segment_bypass(monkeypatch):
    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_API_TOKEN", "secret")

    out = asyncio.run(apstra.apstra_get("/api/%252e%252e/admin"))

    assert "error" in out
    assert "double-encoded" in out["error"]


def test_apstra_get_calls_httpx(monkeypatch):
    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            called["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["headers"] = headers or {}
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_API_TOKEN", "secret")
    monkeypatch.setattr(apstra.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(apstra.apstra_get("/api/blueprints", {"limit": 1}))

    assert out["status_code"] == 200
    assert out["data"] == {"ok": True}
    assert called["url"] == "https://apstra.example.com/api/blueprints"
    assert called["headers"]["AuthToken"] == "secret"
    assert "Authorization" not in called["headers"]


def test_apstra_get_bounds_list_payloads(monkeypatch):
    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            return _ListResp()

    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_API_TOKEN", "secret")
    monkeypatch.setattr(apstra.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(apstra.apstra_get("/api/blueprints", limit=2, offset=1))

    assert out["data"] == {
        "items": [{"id": 2}, {"id": 3}],
        "_pagination": {
            "offset": 1,
            "limit": 2,
            "total": 3,
            "truncated": False,
        },
    }


def test_apstra_list_blueprints_compacts(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '[{"id":"bp1"}]'

        def json(self):
            return [
                {
                    "id": "bp1",
                    "label": "DC1",
                    "status": "ready",
                    "raw": "omitted",
                }
            ]

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            return _Resp()

    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_API_TOKEN", "secret")
    monkeypatch.setattr(apstra.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(apstra.apstra_list_blueprints(limit=10))

    assert called["url"] == "https://apstra.example.com/api/blueprints"
    assert out["blueprints"]["items"] == [
        {"id": "bp1", "label": "DC1", "status": "ready"}
    ]


def test_apstra_list_templates_compacts(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"items":[{"id":"tmpl1"}]}'

        def json(self):
            return {
                "items": [
                    {
                        "id": "tmpl1",
                        "label": "5-stage Clos",
                        "design": "datacenter",
                        "version": "1.0",
                        "raw": "omitted",
                    },
                    {
                        "id": "tmpl2",
                        "label": "Collapsed core",
                        "design": "datacenter",
                        "version": "1.0",
                        "raw": "omitted",
                    },
                ]
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            return _Resp()

    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_API_TOKEN", "secret")
    monkeypatch.setattr(apstra.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(apstra.apstra_list_templates(limit=1))

    assert called["url"] == "https://apstra.example.com/api/design/templates"
    assert out["templates"]["items"] == [
        {"id": "tmpl1", "label": "5-stage Clos", "design": "datacenter", "version": "1.0"}
    ]
    assert out["templates"]["_pagination"]["truncated"] is True


def test_apstra_list_anomalies_quotes_blueprint_id_and_compacts(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"items":[{"id":"a1"}]}'

        def json(self):
            return {
                "items": [
                    {
                        "id": "a1",
                        "type": "bgp",
                        "severity": "critical",
                        "details": "omitted",
                    }
                ]
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            return _Resp()

    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_API_TOKEN", "secret")
    monkeypatch.setattr(apstra.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(apstra.apstra_list_anomalies("bp 1"))

    assert called["url"] == "https://apstra.example.com/api/blueprints/bp%201/anomalies"
    assert out["blueprint_id"] == "bp 1"
    assert out["anomalies"]["items"] == [
        {"id": "a1", "type": "bgp", "severity": "critical"}
    ]


def test_apstra_list_racks_quotes_blueprint_id_and_compacts(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"items":[{"id":"rack1"}]}'

        def json(self):
            return {
                "items": [
                    {
                        "id": "rack1",
                        "label": "Rack 1",
                        "rack_type": "rack_based",
                        "leaf_count": 2,
                        "spine_count": 0,
                        "raw": "omitted",
                    },
                    {
                        "id": "rack2",
                        "label": "Rack 2",
                        "rack_type": "rack_based",
                        "leaf_count": 2,
                        "spine_count": 0,
                        "raw": "omitted",
                    },
                ]
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            return _Resp()

    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_API_TOKEN", "secret")
    monkeypatch.setattr(apstra.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(apstra.apstra_list_racks("bp 1", limit=1))

    assert called["url"] == "https://apstra.example.com/api/blueprints/bp%201/racks"
    assert out["blueprint_id"] == "bp 1"
    assert out["racks"]["items"] == [
        {
            "id": "rack1",
            "label": "Rack 1",
            "rack_type": "rack_based",
            "leaf_count": 2,
            "spine_count": 0,
        }
    ]
    assert out["racks"]["_pagination"]["truncated"] is True


def test_apstra_list_routing_zones_quotes_blueprint_id_and_compacts(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"items":[{"id":"sz1"}]}'

        def json(self):
            return {
                "items": [
                    {
                        "id": "sz1",
                        "label": "default",
                        "vni": 10001,
                        "vrf_name": "default",
                        "raw": "omitted",
                    }
                ]
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            return _Resp()

    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_API_TOKEN", "secret")
    monkeypatch.setattr(apstra.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(apstra.apstra_list_routing_zones("bp 1"))

    assert called["url"] == (
        "https://apstra.example.com/api/blueprints/bp%201/security-zones"
    )
    assert out["blueprint_id"] == "bp 1"
    assert out["routing_zones"]["items"] == [
        {"id": "sz1", "label": "default", "vni": 10001, "vrf_name": "default"}
    ]


def test_apstra_list_virtual_networks_quotes_blueprint_id_and_compacts(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"items":[{"id":"vn1"}]}'

        def json(self):
            return {
                "items": [
                    {
                        "id": "vn1",
                        "label": "App",
                        "vn_type": "vxlan",
                        "security_zone_id": "sz1",
                        "virtual_gateway_ipv4": "10.10.1.1",
                        "ipv4_subnet": "10.10.1.0/24",
                        "bound_to": [{"system_id": "leaf1", "vlan_id": 101}],
                        "raw": "omitted",
                    },
                    {
                        "id": "vn2",
                        "label": "Db",
                        "vn_type": "vxlan",
                        "security_zone_id": "sz1",
                        "virtual_gateway_ipv4": "10.10.2.1",
                        "ipv4_subnet": "10.10.2.0/24",
                        "bound_to": [{"system_id": "leaf2", "vlan_id": 102}],
                        "raw": "omitted",
                    },
                ]
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            return _Resp()

    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_API_TOKEN", "secret")
    monkeypatch.setattr(apstra.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(apstra.apstra_list_virtual_networks("bp 1", limit=1))

    assert called["url"] == (
        "https://apstra.example.com/api/blueprints/bp%201/virtual-networks"
    )
    assert out["blueprint_id"] == "bp 1"
    assert out["virtual_networks"]["items"] == [
        {
            "id": "vn1",
            "label": "App",
            "vn_type": "vxlan",
            "security_zone_id": "sz1",
            "virtual_gateway_ipv4": "10.10.1.1",
            "ipv4_subnet": "10.10.1.0/24",
            "bound_to": [{"system_id": "leaf1", "vlan_id": 101}],
        }
    ]
    assert out["virtual_networks"]["_pagination"]["truncated"] is True


def test_apstra_list_remote_gateways_quotes_blueprint_id_and_compacts(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"remote_gateways":[{"id":"gw1"}]}'

        def json(self):
            return {
                "remote_gateways": [
                    {
                        "id": "gw1",
                        "gw_name": "remote-a",
                        "gw_ip": "198.51.100.1",
                        "gw_asn": 65001,
                        "evpn_route_types": "all",
                        "local_gw_nodes": ["leaf1"],
                        "raw": "omitted",
                    }
                ]
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            return _Resp()

    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_API_TOKEN", "secret")
    monkeypatch.setattr(apstra.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(apstra.apstra_list_remote_gateways("bp 1"))

    assert called["url"] == (
        "https://apstra.example.com/api/blueprints/bp%201/remote_gateways"
    )
    assert out["blueprint_id"] == "bp 1"
    assert out["remote_gateways"]["remote_gateways"] == [
        {
            "id": "gw1",
            "gw_name": "remote-a",
            "gw_ip": "198.51.100.1",
            "gw_asn": 65001,
            "local_gw_nodes": ["leaf1"],
            "evpn_route_types": "all",
        }
    ]


def test_apstra_list_connectivity_templates_quotes_blueprint_id_and_compacts(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"policies":[{"id":"ct1"}]}'

        def json(self):
            return {
                "policies": [
                    {
                        "id": "ct1",
                        "label": "Access VLAN",
                        "policy_type": "batch",
                        "visible": True,
                        "used": False,
                        "raw": "omitted",
                    },
                    {
                        "id": "ct2",
                        "label": "Trunk",
                        "policy_type": "batch",
                        "visible": True,
                        "used": True,
                        "raw": "omitted",
                    },
                ]
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            return _Resp()

    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_API_TOKEN", "secret")
    monkeypatch.setattr(apstra.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(apstra.apstra_list_connectivity_templates("bp 1", limit=1))

    assert called["url"] == (
        "https://apstra.example.com/api/blueprints/bp%201/endpoint-policies"
    )
    assert out["blueprint_id"] == "bp 1"
    assert out["connectivity_templates"]["policies"] == [
        {
            "id": "ct1",
            "label": "Access VLAN",
            "policy_type": "batch",
            "visible": True,
            "used": False,
        }
    ]
    assert out["connectivity_templates"]["_pagination"]["truncated"] is True


def test_apstra_list_application_endpoints_uses_read_only_post_and_compacts(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"application_points":[{"id":"if1"}]}'

        def json(self):
            return {
                "application_points": [
                    {
                        "id": "if1",
                        "label": "leaf1:xe-0/0/1",
                        "system_id": "leaf1",
                        "interface_id": "xe-0/0/1",
                        "interface_name": "xe-0/0/1",
                        "assigned": False,
                        "raw": "omitted",
                    },
                    {
                        "id": "if2",
                        "label": "leaf1:xe-0/0/2",
                        "system_id": "leaf1",
                        "interface_id": "xe-0/0/2",
                        "interface_name": "xe-0/0/2",
                        "assigned": True,
                        "raw": "omitted",
                    },
                ]
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, headers=None, params=None, json=None):
            called["url"] = url
            called["headers"] = headers or {}
            return _Resp()

    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_API_TOKEN", "secret")
    monkeypatch.setattr(apstra.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(apstra.apstra_list_application_endpoints("bp 1", limit=1))

    assert called["url"] == (
        "https://apstra.example.com/api/blueprints/bp%201/obj-policy-application-points"
    )
    assert called["headers"]["AuthToken"] == "secret"
    assert "Authorization" not in called["headers"]
    assert out["blueprint_id"] == "bp 1"
    assert out["application_endpoints"]["application_points"] == [
        {
            "id": "if1",
            "label": "leaf1:xe-0/0/1",
            "system_id": "leaf1",
            "interface_id": "xe-0/0/1",
            "interface_name": "xe-0/0/1",
            "assigned": False,
        }
    ]
    assert out["application_endpoints"]["_pagination"]["truncated"] is True


def test_apstra_get_diff_status_quotes_blueprint_id_and_compacts(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"status":"staged"}'

        def json(self):
            return {
                "status": "staged",
                "staging_version": 7,
                "active_version": 6,
                "has_uncommitted_changes": True,
                "raw": "omitted",
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            return _Resp()

    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_API_TOKEN", "secret")
    monkeypatch.setattr(apstra.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(apstra.apstra_get_diff_status("bp 1"))

    assert called["url"] == "https://apstra.example.com/api/blueprints/bp%201/diff-status"
    assert out["blueprint_id"] == "bp 1"
    assert out["diff_status"] == {
        "status": "staged",
        "staging_version": 7,
        "active_version": 6,
        "has_uncommitted_changes": True,
    }


def test_apstra_list_protocol_sessions_quotes_blueprint_id_and_compacts(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"sessions":[{"id":"bgp1"}]}'

        def json(self):
            return {
                "sessions": [
                    {
                        "id": "bgp1",
                        "protocol": "bgp",
                        "local_system_id": "leaf1",
                        "remote_system_id": "spine1",
                        "state": "Established",
                        "raw": "omitted",
                    }
                ]
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            return _Resp()

    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_API_TOKEN", "secret")
    monkeypatch.setattr(apstra.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(apstra.apstra_list_protocol_sessions("bp 1"))

    assert called["url"] == (
        "https://apstra.example.com/api/blueprints/bp%201/protocol-sessions"
    )
    assert out["blueprint_id"] == "bp 1"
    assert out["protocol_sessions"]["sessions"] == [
        {
            "id": "bgp1",
            "protocol": "bgp",
            "local_system_id": "leaf1",
            "remote_system_id": "spine1",
            "state": "Established",
        }
    ]


def test_apstra_get_system_info_quotes_blueprint_id_and_compacts(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"systems":[{"id":"sys1"}]}'

        def json(self):
            return {
                "systems": [
                    {
                        "id": "sys1",
                        "label": "leaf-1",
                        "role": "leaf",
                        "status": "ready",
                        "management_ip": "192.0.2.10",
                        "raw": "omitted",
                    },
                    {
                        "id": "sys2",
                        "label": "spine-1",
                        "role": "spine",
                        "status": "ready",
                        "management_ip": "192.0.2.11",
                        "raw": "omitted",
                    },
                ]
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            return _Resp()

    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_API_TOKEN", "secret")
    monkeypatch.setattr(apstra.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(apstra.apstra_get_system_info("bp 1", limit=1))

    assert called["url"] == (
        "https://apstra.example.com/api/blueprints/bp%201/experience/web/system-info"
    )
    assert out["blueprint_id"] == "bp 1"
    assert out["systems"]["systems"] == [
        {
            "id": "sys1",
            "label": "leaf-1",
            "role": "leaf",
            "status": "ready",
            "management_ip": "192.0.2.10",
        }
    ]
    assert out["systems"]["_pagination"]["truncated"] is True


def test_aos8_get_rejects_non_v1_path(monkeypatch):
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")

    out = asyncio.run(aos8.aos8_get("/api/bad"))

    assert "error" in out
    assert "/v1/*" in out["error"]


def test_aos8_get_rejects_dot_segment_bypass(monkeypatch):
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")

    out = asyncio.run(aos8.aos8_get("/v1/../admin"))

    assert "error" in out
    assert "dot segments" in out["error"]


def test_aos8_get_rejects_double_encoded_dot_segment_bypass(monkeypatch):
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")

    out = asyncio.run(aos8.aos8_get("/v1/%252e%252e/admin"))

    assert "error" in out
    assert "double-encoded" in out["error"]


def test_aos8_get_calls_httpx(monkeypatch):
    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            called["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["headers"] = headers or {}
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(aos8.aos8_get("/v1/configuration/object", {"limit": 1}))

    assert out["status_code"] == 200
    assert out["data"] == {"ok": True}
    assert called["url"] == "https://mm.example.com/v1/configuration/object"
    assert called["headers"]["Authorization"] == "Bearer secret"


def test_aos8_show_command_rejects_non_show(monkeypatch):
    out = asyncio.run(aos8.aos8_show_command("write memory"))

    assert "error" in out
    assert "Only 'show' commands" in out["error"]


def test_aos8_show_command_calls_showcommand_and_strips_envelope(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"rows":[{"name":"mc1"}]}'

        def json(self):
            return {
                "_global_result": {"status": "0"},
                "_meta": {"rows": ["name"]},
                "rows": [{"name": "mc1"}, {"name": "mc2"}, {"name": "mc3"}],
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(
        aos8.aos8_show_command(" show switchinfo", config_path="/md/branch1", limit=2, offset=1)
    )

    assert called["url"] == "https://mm.example.com/v1/configuration/showcommand"
    assert called["params"] == {"command": "show switchinfo", "config_path": "/md/branch1"}
    assert out["command"] == "show switchinfo"
    assert "_global_result" not in out["data"]
    assert "_meta" not in out["data"]
    assert out["data"]["rows"] == [{"name": "mc2"}, {"name": "mc3"}]


def test_aos8_list_aps_runs_show_ap_database_and_compacts(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"AP Database":[{"Name":"ap1"}]}'

        def json(self):
            return {
                "_global_result": {"status": "0"},
                "_meta": {"AP Database": ["Name"]},
                "AP Database": [
                    {
                        "Name": "ap1",
                        "Group": "HQ",
                        "IP Address": "192.0.2.10",
                        "Status": "Up",
                        "Raw": "omitted",
                    },
                    {
                        "Name": "ap2",
                        "Group": "HQ",
                        "IP Address": "192.0.2.11",
                        "Status": "Down",
                        "Raw": "omitted",
                    },
                ],
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(aos8.aos8_list_aps(config_path="/md/branch1", limit=1))

    assert called["url"] == "https://mm.example.com/v1/configuration/showcommand"
    assert called["params"] == {
        "command": "show ap database",
        "config_path": "/md/branch1",
    }
    assert out["config_path"] == "/md/branch1"
    assert out["aps"]["AP Database"] == [
        {"Name": "ap1", "Group": "HQ", "IP Address": "192.0.2.10", "Status": "Up"}
    ]
    assert out["aps"]["_pagination"]["truncated"] is True


@pytest.mark.parametrize(
    ("tool_func", "expected_command", "payload", "output_key", "expected_item"),
    [
        (
            aos8.aos8_list_active_aps,
            "show ap active",
            {
                "Active APs": [
                    {
                        "Name": "ap1",
                        "Group": "HQ",
                        "IP Address": "192.0.2.10",
                        "Status": "Up",
                        "Raw": "omitted",
                    },
                    {
                        "Name": "ap2",
                        "Group": "HQ",
                        "IP Address": "192.0.2.11",
                        "Status": "Up",
                        "Raw": "omitted",
                    },
                ]
            },
            "active_aps",
            {"Name": "ap1", "Group": "HQ", "IP Address": "192.0.2.10", "Status": "Up"},
        ),
        (
            aos8.aos8_list_bss,
            "show ap bss-table",
            {
                "BSS Table": [
                    {
                        "BSSID": "aa:bb:cc:dd:ee:ff",
                        "AP Name": "ap1",
                        "ESSID": "Corp",
                        "Channel": 36,
                        "Clients": 12,
                        "Raw": "omitted",
                    },
                    {
                        "BSSID": "aa:bb:cc:dd:ee:00",
                        "AP Name": "ap2",
                        "ESSID": "Guest",
                        "Channel": 149,
                        "Clients": 4,
                        "Raw": "omitted",
                    },
                ]
            },
            "bss",
            {
                "BSSID": "aa:bb:cc:dd:ee:ff",
                "AP Name": "ap1",
                "ESSID": "Corp",
                "Channel": 36,
                "Clients": 12,
            },
        ),
        (
            aos8.aos8_get_radio_summary,
            "show ap radio-summary",
            {
                "Radio Summary": [
                    {
                        "AP Name": "ap1",
                        "Radio": "5GHz",
                        "Channel": 36,
                        "EIRP": 18,
                        "Clients": 12,
                        "Raw": "omitted",
                    },
                    {
                        "AP Name": "ap2",
                        "Radio": "2.4GHz",
                        "Channel": 6,
                        "EIRP": 9,
                        "Clients": 4,
                        "Raw": "omitted",
                    },
                ]
            },
            "radio_summary",
            {"AP Name": "ap1", "Radio": "5GHz", "Channel": 36, "EIRP": 18, "Clients": 12},
        ),
    ],
)
def test_aos8_visibility_show_tools_compact_outputs(
    monkeypatch,
    tool_func,
    expected_command,
    payload,
    output_key,
    expected_item,
):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"rows":[]}'

        def json(self):
            return {
                "_global_result": {"status": "0"},
                "_meta": {"rows": []},
                **payload,
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(tool_func(config_path="/md/branch1", limit=1))

    assert called["url"] == "https://mm.example.com/v1/configuration/showcommand"
    assert called["params"] == {
        "command": expected_command,
        "config_path": "/md/branch1",
    }
    assert out["config_path"] == "/md/branch1"
    list_key = next(key for key in out[output_key] if key != "_pagination")
    assert out[output_key][list_key] == [expected_item]
    assert out[output_key]["_pagination"]["truncated"] is True


@pytest.mark.parametrize(
    ("tool_func", "expected_command", "payload", "output_key", "expected_item"),
    [
        (
            aos8.aos8_list_controllers,
            "show switches",
            {
                "Switches": [
                    {
                        "Name": "mc-1",
                        "Switch IP": "192.0.2.10",
                        "Model": "MM-VA",
                        "Role": "master",
                        "Status": "up",
                        "Raw": "omitted",
                    },
                    {
                        "Name": "md-1",
                        "Switch IP": "192.0.2.11",
                        "Model": "7205",
                        "Role": "local",
                        "Status": "up",
                        "Raw": "omitted",
                    },
                ]
            },
            "controllers",
            {
                "Name": "mc-1",
                "Switch IP": "192.0.2.10",
                "Model": "MM-VA",
                "Role": "master",
                "Status": "up",
            },
        ),
        (
            aos8.aos8_get_version,
            "show version",
            {
                "Version": [
                    {
                        "ArubaOS Version": "8.10.0.11",
                        "Build": "123456",
                        "Build Date": "2026-01-01",
                        "Model": "MM-VA",
                        "Raw": "omitted",
                    },
                    {
                        "ArubaOS Version": "8.10.0.10",
                        "Build": "123455",
                        "Build Date": "2025-12-01",
                        "Model": "7205",
                        "Raw": "omitted",
                    },
                ]
            },
            "version",
            {
                "ArubaOS Version": "8.10.0.11",
                "Build": "123456",
                "Build Date": "2026-01-01",
                "Model": "MM-VA",
            },
        ),
        (
            aos8.aos8_list_licenses,
            "show license",
            {
                "Licenses": [
                    {
                        "Name": "AP",
                        "Installed": 128,
                        "Used": 64,
                        "Expires": "Never",
                        "Status": "valid",
                        "Raw": "omitted",
                    },
                    {
                        "Name": "PEF",
                        "Installed": 128,
                        "Used": 60,
                        "Expires": "Never",
                        "Status": "valid",
                        "Raw": "omitted",
                    },
                ]
            },
            "licenses",
            {
                "Name": "AP",
                "Installed": 128,
                "Used": 64,
                "Expires": "Never",
                "Status": "valid",
            },
        ),
    ],
)
def test_aos8_conductor_show_tools_do_not_send_config_path_and_compact(
    monkeypatch,
    tool_func,
    expected_command,
    payload,
    output_key,
    expected_item,
):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"rows":[]}'

        def json(self):
            return {
                "_global_result": {"status": "0"},
                "_meta": {"rows": []},
                **payload,
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(tool_func(limit=1))

    assert called["url"] == "https://mm.example.com/v1/configuration/showcommand"
    assert called["params"] == {"command": expected_command}
    list_key = next(key for key in out[output_key] if key != "_pagination")
    assert out[output_key][list_key] == [expected_item]
    assert out[output_key]["_pagination"]["truncated"] is True


def test_aos8_list_clients_runs_show_user_table_and_compacts(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"Users":[{"Name":"alice"}]}'

        def json(self):
            return {
                "_global_result": {"status": "0"},
                "_meta": {"Users": ["Name"]},
                "Users": [
                    {
                        "Name": "alice",
                        "MAC Address": "aa:bb:cc:dd:ee:01",
                        "IP Address": "192.0.2.50",
                        "AP Name": "ap1",
                        "SSID": "Corp",
                        "Role": "employee",
                        "VLAN": 20,
                        "Raw": "omitted",
                    },
                    {
                        "Name": "bob",
                        "MAC Address": "aa:bb:cc:dd:ee:02",
                        "IP Address": "192.0.2.51",
                        "AP Name": "ap2",
                        "SSID": "Corp",
                        "Role": "employee",
                        "VLAN": 20,
                        "Raw": "omitted",
                    },
                ],
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(aos8.aos8_list_clients(config_path="/md/branch1", limit=1))

    assert called["url"] == "https://mm.example.com/v1/configuration/showcommand"
    assert called["params"] == {
        "command": "show user-table",
        "config_path": "/md/branch1",
    }
    assert out["config_path"] == "/md/branch1"
    assert out["clients"]["Users"] == [
        {
            "Name": "alice",
            "MAC Address": "aa:bb:cc:dd:ee:01",
            "IP Address": "192.0.2.50",
            "AP Name": "ap1",
            "SSID": "Corp",
            "Role": "employee",
            "VLAN": 20,
        }
    ]
    assert out["clients"]["_pagination"]["truncated"] is True


def test_aos8_find_client_requires_exactly_one_selector(monkeypatch):
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")

    missing = asyncio.run(aos8.aos8_find_client())
    multiple = asyncio.run(aos8.aos8_find_client(mac="aa:bb", ip="192.0.2.50"))

    assert missing == {"error": "Provide exactly one of mac, ip, or username."}
    assert multiple == {"error": "Provide exactly one of mac, ip, or username."}


@pytest.mark.parametrize(
    ("tool_call", "expected_command", "output_key", "payload", "expected_item", "expect_path"),
    [
        (
            lambda: aos8.aos8_find_client(
                mac="aa:bb:cc:dd:ee:01", config_path="/md/branch1", limit=1
            ),
            "show user-table mac aa:bb:cc:dd:ee:01",
            "client",
            {
                "Users": [
                    {
                        "Name": "alice",
                        "MAC Address": "aa:bb:cc:dd:ee:01",
                        "IP Address": "192.0.2.50",
                        "AP Name": "ap1",
                        "SSID": "Corp",
                        "Role": "employee",
                        "Raw": "omitted",
                    },
                    {
                        "Name": "other",
                        "MAC Address": "aa:bb:cc:dd:ee:02",
                        "IP Address": "192.0.2.51",
                        "AP Name": "ap2",
                        "SSID": "Guest",
                        "Role": "guest",
                        "Raw": "omitted",
                    },
                ]
            },
            {
                "Name": "alice",
                "MAC Address": "aa:bb:cc:dd:ee:01",
                "IP Address": "192.0.2.50",
                "AP Name": "ap1",
                "SSID": "Corp",
                "Role": "employee",
            },
            True,
        ),
        (
            lambda: aos8.aos8_get_client_detail(
                mac="aa:bb:cc:dd:ee:01",
                config_path="/md/branch1",
                limit=1,
            ),
            "show user-table verbose mac aa:bb:cc:dd:ee:01",
            "client_detail",
            {
                "Client Details": [
                    {
                        "Name": "alice",
                        "MAC Address": "aa:bb:cc:dd:ee:01",
                        "IP Address": "192.0.2.50",
                        "Authentication": "802.1X",
                        "Mobility Role": "employee",
                        "Uptime": "1h",
                        "Raw": "omitted",
                    },
                    {
                        "Name": "other",
                        "MAC Address": "aa:bb:cc:dd:ee:02",
                        "IP Address": "192.0.2.51",
                        "Authentication": "MAC",
                        "Mobility Role": "guest",
                        "Uptime": "2h",
                        "Raw": "omitted",
                    },
                ]
            },
            {
                "Name": "alice",
                "MAC Address": "aa:bb:cc:dd:ee:01",
                "IP Address": "192.0.2.50",
                "Authentication": "802.1X",
                "Mobility Role": "employee",
                "Uptime": "1h",
            },
            True,
        ),
        (
            lambda: aos8.aos8_get_client_history(mac="aa:bb:cc:dd:ee:01", limit=1),
            "show ap association history client-mac aa:bb:cc:dd:ee:01",
            "client_history",
            {
                "History": [
                    {
                        "Time": "2026-07-01 01:00:00",
                        "AP Name": "ap1",
                        "BSSID": "aa:bb:cc:00:00:01",
                        "Event": "Associated",
                        "Reason": "Success",
                        "Raw": "omitted",
                    },
                    {
                        "Time": "2026-07-01 00:55:00",
                        "AP Name": "ap2",
                        "BSSID": "aa:bb:cc:00:00:02",
                        "Event": "Roamed",
                        "Reason": "RSSI",
                        "Raw": "omitted",
                    },
                ]
            },
            {
                "Time": "2026-07-01 01:00:00",
                "AP Name": "ap1",
                "BSSID": "aa:bb:cc:00:00:01",
                "Event": "Associated",
                "Reason": "Success",
            },
            False,
        ),
    ],
)
def test_aos8_client_troubleshooting_tools_map_commands_and_compact(
    monkeypatch,
    tool_call,
    expected_command,
    output_key,
    payload,
    expected_item,
    expect_path,
):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"rows":[]}'

        def json(self):
            return {
                "_global_result": {"status": "0"},
                "_meta": {"rows": []},
                **payload,
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(tool_call())

    assert called["url"] == "https://mm.example.com/v1/configuration/showcommand"
    expected_params = {"command": expected_command}
    if expect_path:
        expected_params["config_path"] = "/md/branch1"
        assert out["config_path"] == "/md/branch1"
    assert called["params"] == expected_params
    list_key = next(key for key in out[output_key] if key != "_pagination")
    assert out[output_key][list_key] == [expected_item]
    assert out[output_key]["_pagination"]["truncated"] is True


def test_aos8_get_system_logs_caps_count_and_does_not_send_config_path(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"System Logs":[]}'

        def json(self):
            return {
                "_global_result": {"status": "0"},
                "_meta": {"System Logs": []},
                "System Logs": [
                    {
                        "Time": "2026-07-01 01:00:00",
                        "Module": "stm",
                        "Severity": "warning",
                        "Message": "AP radio changed channel",
                        "Raw": "omitted",
                    },
                    {
                        "Time": "2026-07-01 00:59:00",
                        "Module": "auth",
                        "Severity": "info",
                        "Message": "User authenticated",
                        "Raw": "omitted",
                    },
                ],
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(aos8.aos8_get_system_logs(count=999, limit=1))

    assert called["url"] == "https://mm.example.com/v1/configuration/showcommand"
    assert called["params"] == {"command": "show log system 200"}
    assert out["count"] == 200
    assert out["system_logs"]["System Logs"] == [
        {
            "Time": "2026-07-01 01:00:00",
            "Module": "stm",
            "Severity": "warning",
            "Message": "AP radio changed channel",
        }
    ]
    assert out["system_logs"]["_pagination"]["truncated"] is True


@pytest.mark.parametrize(
    ("tool_call", "expected_command", "payload", "output_key", "expected_item", "expect_path"),
    [
        (
            lambda: aos8.aos8_get_alarms(config_path="/md/branch1", limit=1),
            "show alarms",
            {
                "Alarms": [
                    {
                        "Time": "2026-07-01 01:00:00",
                        "Severity": "critical",
                        "Category": "AP",
                        "Description": "AP down",
                        "Status": "active",
                        "Raw": "omitted",
                    },
                    {
                        "Time": "2026-07-01 00:55:00",
                        "Severity": "minor",
                        "Category": "License",
                        "Description": "License warning",
                        "Status": "active",
                        "Raw": "omitted",
                    },
                ]
            },
            "alarms",
            {
                "Time": "2026-07-01 01:00:00",
                "Severity": "critical",
                "Category": "AP",
                "Description": "AP down",
                "Status": "active",
            },
            True,
        ),
        (
            lambda: aos8.aos8_get_audit_trail(limit=1),
            "show audit-trail",
            {
                "Audit Trail": [
                    {
                        "Time": "2026-07-01 01:00:00",
                        "User": "admin",
                        "IP Address": "192.0.2.10",
                        "Command": "configure terminal",
                        "Config Path": "/md/branch1",
                        "Result": "success",
                        "Raw": "omitted",
                    },
                    {
                        "Time": "2026-07-01 00:50:00",
                        "User": "ops",
                        "IP Address": "192.0.2.11",
                        "Command": "show switches",
                        "Config Path": "/md",
                        "Result": "success",
                        "Raw": "omitted",
                    },
                ]
            },
            "audit_trail",
            {
                "Time": "2026-07-01 01:00:00",
                "User": "admin",
                "IP Address": "192.0.2.10",
                "Command": "configure terminal",
                "Config Path": "/md/branch1",
                "Result": "success",
            },
            False,
        ),
        (
            lambda: aos8.aos8_get_events(config_path="/md/branch1", limit=1),
            "show events",
            {
                "Events": [
                    {
                        "Time": "2026-07-01 01:00:00",
                        "Type": "system",
                        "Severity": "warning",
                        "Source": "controller",
                        "Description": "AP rebooted",
                        "Raw": "omitted",
                    },
                    {
                        "Time": "2026-07-01 00:45:00",
                        "Type": "auth",
                        "Severity": "info",
                        "Source": "aaa",
                        "Description": "Client authenticated",
                        "Raw": "omitted",
                    },
                ]
            },
            "events",
            {
                "Time": "2026-07-01 01:00:00",
                "Type": "system",
                "Severity": "warning",
                "Source": "controller",
                "Description": "AP rebooted",
            },
            True,
        ),
    ],
)
def test_aos8_events_audit_show_tools_map_commands_and_compact(
    monkeypatch,
    tool_call,
    expected_command,
    payload,
    output_key,
    expected_item,
    expect_path,
):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"rows":[]}'

        def json(self):
            return {
                "_global_result": {"status": "0"},
                "_meta": {"rows": []},
                **payload,
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(tool_call())

    assert called["url"] == "https://mm.example.com/v1/configuration/showcommand"
    expected_params = {"command": expected_command}
    if expect_path:
        expected_params["config_path"] = "/md/branch1"
        assert out["config_path"] == "/md/branch1"
    assert called["params"] == expected_params
    list_key = next(key for key in out[output_key] if key != "_pagination")
    assert out[output_key][list_key] == [expected_item]
    assert out[output_key]["_pagination"]["truncated"] is True


@pytest.mark.parametrize(
    ("tool_call", "expected_command", "payload", "output_key", "expected_item", "expect_path"),
    [
        (
            lambda: aos8.aos8_get_md_hierarchy(limit=1),
            "show configuration node-hierarchy",
            {
                "Configuration node hierarchy": [
                    {
                        "Config Path": "/md/branch1",
                        "Name": "branch1",
                        "Device Type": "managed-device",
                        "IP Address": "192.0.2.10",
                        "Status": "up",
                        "Raw": "omitted",
                    },
                    {
                        "Config Path": "/md/branch2",
                        "Name": "branch2",
                        "Device Type": "managed-device",
                        "IP Address": "192.0.2.11",
                        "Status": "up",
                        "Raw": "omitted",
                    },
                ]
            },
            "md_hierarchy",
            {
                "Config Path": "/md/branch1",
                "Name": "branch1",
                "Device Type": "managed-device",
                "IP Address": "192.0.2.10",
                "Status": "up",
            },
            False,
        ),
        (
            lambda: aos8.aos8_get_rf_neighbors(
                ap_name=" ap-1 ",
                config_path="/md/branch1",
                limit=1,
            ),
            "show ap arm-neighbors ap-name ap-1",
            {
                "ARM Neighbors": [
                    {
                        "AP Name": "ap-1",
                        "Neighbor AP Name": "ap-2",
                        "BSSID": "aa:bb:cc:dd:ee:ff",
                        "Channel": 36,
                        "RSSI": -67,
                        "SNR": 28,
                        "Raw": "omitted",
                    },
                    {
                        "AP Name": "ap-1",
                        "Neighbor AP Name": "ap-3",
                        "BSSID": "aa:bb:cc:dd:ee:00",
                        "Channel": 40,
                        "RSSI": -72,
                        "SNR": 22,
                        "Raw": "omitted",
                    },
                ]
            },
            "rf_neighbors",
            {
                "AP Name": "ap-1",
                "Neighbor AP Name": "ap-2",
                "BSSID": "aa:bb:cc:dd:ee:ff",
                "Channel": 36,
                "RSSI": -67,
                "SNR": 28,
            },
            True,
        ),
        (
            lambda: aos8.aos8_get_cluster_state(limit=1),
            "show lc-cluster group-membership",
            {
                "Cluster Members": [
                    {
                        "Cluster": "cluster1",
                        "Controller": "md-1",
                        "IP Address": "192.0.2.20",
                        "Role": "master",
                        "State": "up",
                        "Status": "active",
                        "Raw": "omitted",
                    },
                    {
                        "Cluster": "cluster1",
                        "Controller": "md-2",
                        "IP Address": "192.0.2.21",
                        "Role": "standby",
                        "State": "up",
                        "Status": "active",
                        "Raw": "omitted",
                    },
                ]
            },
            "cluster_state",
            {
                "Cluster": "cluster1",
                "Controller": "md-1",
                "IP Address": "192.0.2.20",
                "Role": "master",
                "State": "up",
                "Status": "active",
            },
            False,
        ),
        (
            lambda: aos8.aos8_get_ap_wired_ports(ap_name=" ap-1 ", limit=1),
            "show ap port status ap-name ap-1",
            {
                "AP Wired Ports": [
                    {
                        "AP Name": "ap-1",
                        "Port": "0",
                        "Status": "up",
                        "Mode": "access",
                        "VLAN": 20,
                        "Speed": "1G",
                        "Duplex": "full",
                        "Raw": "omitted",
                    },
                    {
                        "AP Name": "ap-1",
                        "Port": "1",
                        "Status": "down",
                        "Mode": "access",
                        "VLAN": 30,
                        "Speed": "auto",
                        "Duplex": "auto",
                        "Raw": "omitted",
                    },
                ]
            },
            "wired_ports",
            {
                "AP Name": "ap-1",
                "Port": "0",
                "Status": "up",
                "Mode": "access",
                "VLAN": 20,
                "Speed": "1G",
                "Duplex": "full",
            },
            False,
        ),
        (
            lambda: aos8.aos8_get_ipsec_tunnels(limit=1),
            "show crypto ipsec sa",
            {
                "IPsec SAs": [
                    {
                        "Peer IP": "198.51.100.10",
                        "Local IP": "192.0.2.10",
                        "Remote IP": "198.51.100.10",
                        "SPI": "0x1234",
                        "State": "established",
                        "Uptime": "1d",
                        "Raw": "omitted",
                    },
                    {
                        "Peer IP": "198.51.100.11",
                        "Local IP": "192.0.2.10",
                        "Remote IP": "198.51.100.11",
                        "SPI": "0x5678",
                        "State": "established",
                        "Uptime": "2h",
                        "Raw": "omitted",
                    },
                ]
            },
            "ipsec_tunnels",
            {
                "Peer IP": "198.51.100.10",
                "Local IP": "192.0.2.10",
                "Remote IP": "198.51.100.10",
                "SPI": "0x1234",
                "State": "established",
                "Uptime": "1d",
            },
            False,
        ),
    ],
)
def test_aos8_differentiator_show_tools_map_commands_and_compact(
    monkeypatch,
    tool_call,
    expected_command,
    payload,
    output_key,
    expected_item,
    expect_path,
):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"rows":[]}'

        def json(self):
            return {
                "_global_result": {"status": "0"},
                "_meta": {"rows": []},
                **payload,
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(tool_call())

    assert called["url"] == "https://mm.example.com/v1/configuration/showcommand"
    expected_params = {"command": expected_command}
    if expect_path:
        expected_params["config_path"] = "/md/branch1"
        assert out["config_path"] == "/md/branch1"
    assert called["params"] == expected_params
    list_key = next(key for key in out[output_key] if key != "_pagination")
    assert out[output_key][list_key] == [expected_item]
    assert out[output_key]["_pagination"]["truncated"] is True


def test_aos8_get_rf_neighbors_requires_ap_name(monkeypatch):
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")

    out = asyncio.run(aos8.aos8_get_rf_neighbors(ap_name=" "))

    assert out == {"error": "ap_name is required."}


def test_aos8_get_ap_wired_ports_requires_ap_name(monkeypatch):
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")

    out = asyncio.run(aos8.aos8_get_ap_wired_ports(ap_name=" "))

    assert out == {"error": "ap_name is required."}


@pytest.mark.parametrize(
    ("tool_func", "expected_command", "payload", "output_key", "expected_item"),
    [
        (
            aos8.aos8_get_ap_arm_history,
            "show ap arm history",
            {
                "ARM History": [
                    {
                        "Time": "2026-07-01 01:00:00",
                        "AP Name": "ap1",
                        "Radio": "5GHz",
                        "Channel": 36,
                        "Event": "channel-change",
                        "Reason": "interference",
                        "Raw": "omitted",
                    },
                    {
                        "Time": "2026-07-01 00:50:00",
                        "AP Name": "ap2",
                        "Radio": "2.4GHz",
                        "Channel": 6,
                        "Event": "power-change",
                        "Reason": "coverage",
                        "Raw": "omitted",
                    },
                ]
            },
            "arm_history",
            {
                "Time": "2026-07-01 01:00:00",
                "AP Name": "ap1",
                "Radio": "5GHz",
                "Channel": 36,
                "Event": "channel-change",
                "Reason": "interference",
            },
        ),
        (
            aos8.aos8_get_ap_monitor_stats,
            "show ap monitor stats",
            {
                "Monitor Stats": [
                    {
                        "AP Name": "ap1",
                        "BSSID": "aa:bb:cc:dd:ee:ff",
                        "Channel": 36,
                        "RSSI": -58,
                        "SNR": 35,
                        "Utilization": 42,
                        "Raw": "omitted",
                    },
                    {
                        "AP Name": "ap2",
                        "BSSID": "aa:bb:cc:dd:ee:00",
                        "Channel": 149,
                        "RSSI": -65,
                        "SNR": 28,
                        "Utilization": 55,
                        "Raw": "omitted",
                    },
                ]
            },
            "monitor_stats",
            {
                "AP Name": "ap1",
                "BSSID": "aa:bb:cc:dd:ee:ff",
                "Channel": 36,
                "RSSI": -58,
                "SNR": 35,
                "Utilization": 42,
            },
        ),
    ],
)
def test_aos8_ap_debug_show_tools_map_commands_and_compact(
    monkeypatch,
    tool_func,
    expected_command,
    payload,
    output_key,
    expected_item,
):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"rows":[]}'

        def json(self):
            return {
                "_global_result": {"status": "0"},
                "_meta": {"rows": []},
                **payload,
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(tool_func(config_path="/md/branch1", limit=1))

    assert called["url"] == "https://mm.example.com/v1/configuration/showcommand"
    assert called["params"] == {
        "command": expected_command,
        "config_path": "/md/branch1",
    }
    assert out["config_path"] == "/md/branch1"
    list_key = next(key for key in out[output_key] if key != "_pagination")
    assert out[output_key][list_key] == [expected_item]
    assert out[output_key]["_pagination"]["truncated"] is True


def test_aos8_list_ssid_profiles_uses_config_object(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"ssid_prof":[{"profile-name":"Corp"}]}'

        def json(self):
            return {
                "_global_result": {"status": "0"},
                "_meta": {"ssid_prof": ["profile-name"]},
                "ssid_prof": [{"profile-name": "Corp", "opmode": "wpa2-aes"}],
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(aos8.aos8_list_ssid_profiles(config_path="/md/branch1"))

    assert called["url"] == "https://mm.example.com/v1/configuration/object/ssid_prof"
    assert called["params"] == {"config_path": "/md/branch1"}
    assert out["config_path"] == "/md/branch1"
    assert out["ssid_profiles"]["ssid_prof"] == [
        {"profile-name": "Corp", "opmode": "wpa2-aes"}
    ]


@pytest.mark.parametrize(
    ("tool_func", "expected_path", "payload", "output_key", "expected_item"),
    [
        (
            aos8.aos8_list_virtual_aps,
            "/v1/configuration/object/virtual_ap",
            {
                "virtual_ap": [
                    {
                        "profile-name": "Corp-VAP",
                        "ssid-profile": "Corp",
                        "aaa-profile": "dot1x",
                        "vlan": 20,
                        "forward-mode": "tunnel",
                        "Raw": "omitted",
                    },
                    {
                        "profile-name": "Guest-VAP",
                        "ssid-profile": "Guest",
                        "aaa-profile": "guest",
                        "vlan": 30,
                        "forward-mode": "bridge",
                        "Raw": "omitted",
                    },
                ]
            },
            "virtual_aps",
            {
                "profile-name": "Corp-VAP",
                "ssid-profile": "Corp",
                "aaa-profile": "dot1x",
                "vlan": 20,
                "forward-mode": "tunnel",
            },
        ),
        (
            aos8.aos8_list_user_roles,
            "/v1/configuration/object/role",
            {
                "role": [
                    {
                        "role": "employee",
                        "acl": "allowall",
                        "vlan": 20,
                        "captive-portal-profile": "none",
                        "Raw": "omitted",
                    },
                    {
                        "role": "guest",
                        "acl": "guest-logon",
                        "vlan": 30,
                        "captive-portal-profile": "guest",
                        "Raw": "omitted",
                    },
                ]
            },
            "user_roles",
            {
                "role": "employee",
                "acl": "allowall",
                "vlan": 20,
                "captive-portal-profile": "none",
            },
        ),
    ],
)
def test_aos8_wlan_object_reads_compact(
    monkeypatch, tool_func, expected_path, payload, output_key, expected_item
):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"items":[]}'

        def json(self):
            return {
                "_global_result": {"status": "0"},
                "_meta": {"items": []},
                **payload,
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(tool_func(config_path="/md/branch1", limit=1))

    assert called["url"] == f"https://mm.example.com{expected_path}"
    assert called["params"] == {"config_path": "/md/branch1"}
    assert out["config_path"] == "/md/branch1"
    list_key = next(key for key in out[output_key] if key != "_pagination")
    assert out[output_key][list_key] == [expected_item]
    assert out[output_key]["_pagination"]["truncated"] is True


def test_aos8_wlan_object_reads_unwrap_data_envelope(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"_data":{"role":[]}}'

        def json(self):
            return {
                "_global_result": {"status": "0"},
                "_meta": {"items": []},
                "_data": {
                    "role": [
                        {
                            "role": "employee",
                            "acl": "allowall",
                            "vlan": 20,
                            "Raw": "omitted",
                        },
                        {
                            "role": "guest",
                            "acl": "guest-logon",
                            "vlan": 30,
                            "Raw": "omitted",
                        },
                    ]
                },
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(aos8.aos8_list_user_roles(config_path="/md/branch1", limit=1))

    assert called["url"] == "https://mm.example.com/v1/configuration/object/role"
    assert called["params"] == {"config_path": "/md/branch1"}
    assert out["user_roles"]["role"] == [
        {"role": "employee", "acl": "allowall", "vlan": 20}
    ]
    assert out["user_roles"]["_pagination"]["truncated"] is True


def test_edgeconnect_get_rejects_unknown_path(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")

    out = asyncio.run(edgeconnect.edgeconnect_get("/api/bad"))

    assert "error" in out
    assert "/gms/rest/*" in out["error"]


def test_edgeconnect_get_rejects_dot_segment_bypass(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")

    out = asyncio.run(edgeconnect.edgeconnect_get("/gms/rest/../admin"))

    assert "error" in out
    assert "dot segments" in out["error"]


def test_edgeconnect_get_rejects_double_encoded_dot_segment_bypass(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")

    out = asyncio.run(edgeconnect.edgeconnect_get("/gms/rest/%252e%252e/admin"))

    assert "error" in out
    assert "double-encoded" in out["error"]


def test_edgeconnect_get_calls_httpx_with_custom_auth_header(monkeypatch):
    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            called["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["headers"] = headers or {}
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("EDGECONNECT_AUTH_HEADER", "X-Auth-Token")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(edgeconnect.edgeconnect_get("/gms/rest/appliance", {"limit": 1}))

    assert out["status_code"] == 200
    assert out["data"] == {"ok": True}
    assert called["url"] == "https://orch.example.com/gms/rest/appliance"
    assert called["headers"]["X-Auth-Token"] == "secret"


def test_edgeconnect_list_appliances_compacts(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '[{"nePk":"1"}]'

        def json(self):
            return [
                {
                    "nePk": "1",
                    "hostName": "ec-1",
                    "model": "EC-V",
                    "status": "normal",
                    "raw": "omitted",
                }
            ]

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(edgeconnect.edgeconnect_list_appliances(limit=10))

    assert called["url"] == "https://orch.example.com/gms/rest/appliance"
    assert out["appliances"]["items"] == [
        {"nePk": "1", "hostName": "ec-1", "model": "EC-V", "status": "normal"}
    ]


def test_edgeconnect_get_system_info_compacts(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"hostName":"ec-1"}'

        def json(self):
            return {
                "hostName": "ec-1",
                "modelShort": "EC-V",
                "status": "Normal",
                "release": "ECOS 9.5.2.1",
                "alarmSummary": {"num_outstanding": 0},
                "raw": "omitted",
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://ec.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(edgeconnect.edgeconnect_get_system_info())

    assert called["url"] == "https://ec.example.com/rest/json/systemInfo"
    assert out["system_info"] == {
        "hostName": "ec-1",
        "modelShort": "EC-V",
        "status": "Normal",
        "release": "ECOS 9.5.2.1",
        "alarmSummary": {"num_outstanding": 0},
    }


def test_edgeconnect_get_interface_state_compacts(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"interfaces":[{"name":"wan0"}]}'

        def json(self):
            return {
                "interfaces": [
                    {
                        "name": "wan0",
                        "ifName": "wan0",
                        "ipAddress": "192.0.2.10",
                        "adminStatus": "up",
                        "operStatus": "up",
                        "speed": "1G",
                        "raw": "omitted",
                    },
                    {
                        "name": "lan0",
                        "ifName": "lan0",
                        "ipAddress": "192.0.2.11",
                        "adminStatus": "up",
                        "operStatus": "down",
                        "speed": "1G",
                        "raw": "omitted",
                    },
                ]
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(
        edgeconnect.edgeconnect_get_interface_state(ne_pk="1.NE", cached=False, limit=1)
    )

    assert called["url"] == "https://orch.example.com/gms/rest/interfaceState"
    assert called["params"] == {"nePk": "1.NE", "cached": False}
    assert out["ne_pk"] == "1.NE"
    assert out["interface_state"]["interfaces"] == [
        {
            "name": "wan0",
            "ifName": "wan0",
            "ipAddress": "192.0.2.10",
            "adminStatus": "up",
            "operStatus": "up",
            "speed": "1G",
        }
    ]
    assert out["interface_state"]["_pagination"]["truncated"] is True


def test_edgeconnect_get_interface_state_sends_cached_default(monkeypatch):
    called = {}

    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        called["path"] = path
        called["params"] = params
        called["paginate"] = paginate
        return {"status_code": 200, "data": {"interfaces": []}}

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(edgeconnect.edgeconnect_get_interface_state(ne_pk="1.NE"))

    assert called == {
        "path": "/gms/rest/interfaceState",
        "params": {"nePk": "1.NE", "cached": True},
        "paginate": False,
    }
    assert out["interface_state"]["interfaces"] == []


def test_edgeconnect_list_interface_labels_compacts_nested_map(monkeypatch):
    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        assert path == "/gms/rest/gms/interfaceLabels"
        assert params == {"active": True}
        assert paginate is False
        return {
            "status_code": 200,
            "data": {
                "wan": {
                    "1": {"name": "MPLS", "active": True, "topology": 0, "raw": "omitted"},
                    "2": {
                        "name": "Internet",
                        "active": True,
                        "topology": 0,
                        "raw": "omitted",
                    },
                },
                "lan": {
                    "4": {"name": "Voice", "active": True, "topology": 0, "raw": "omitted"},
                },
            },
        }

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(edgeconnect.edgeconnect_list_interface_labels(active=True, limit=2))

    assert out["active"] is True
    assert out["interface_labels"]["interface_labels"] == [
        {"id": "1", "name": "MPLS", "type": "wan", "topology": 0, "active": True},
        {"id": "2", "name": "Internet", "type": "wan", "topology": 0, "active": True},
    ]
    assert out["interface_labels"]["_pagination"]["truncated"] is True


def test_edgeconnect_list_interface_labels_compacts_type_filter_map(monkeypatch):
    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        assert path == "/gms/rest/gms/interfaceLabels"
        assert params == {"type": "wan"}
        assert paginate is False
        return {
            "status_code": 200,
            "data": {
                "1": {"name": "MPLS", "active": True, "topology": 0, "raw": "omitted"},
                "2": {"name": "Internet", "active": True, "topology": 0, "raw": "omitted"},
            },
        }

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(edgeconnect.edgeconnect_list_interface_labels(label_type=" WAN ", limit=1))

    assert out["label_type"] == "wan"
    assert out["interface_labels"]["interface_labels"] == [
        {"id": "1", "name": "MPLS", "topology": 0, "active": True}
    ]
    assert out["interface_labels"]["_pagination"]["truncated"] is True


def test_edgeconnect_list_interface_labels_rejects_unknown_type(monkeypatch):
    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        raise AssertionError("invalid label_type should not execute")

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(edgeconnect.edgeconnect_list_interface_labels(label_type="dmz"))

    assert out == {"error": "label_type must be one of: lan, wan"}


def test_edgeconnect_get_bypass_mode_compacts(monkeypatch):
    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        assert path == "/gms/rest/bypass"
        assert params == {"nePk": "1.NE", "cached": "false"}
        assert paginate is False
        return {
            "status_code": 200,
            "data": {
                "bypass_actual": False,
                "bypass_config": True,
                "status": "Normal",
                "raw": "omitted",
            },
        }

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(edgeconnect.edgeconnect_get_bypass_mode("1.NE", cached=False))

    assert out["ne_pk"] == "1.NE"
    assert out["bypass_mode"] == {
        "bypass_actual": False,
        "bypass_config": True,
        "status": "Normal",
    }


def test_edgeconnect_get_link_integrity_status_compacts_and_trims(monkeypatch):
    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        assert path == "/gms/rest/linkIntegrityTest/status"
        assert params == {"nePk": "1.NE"}
        assert paginate is False
        return {
            "status_code": 200,
            "data": {
                "active": False,
                "result": "abcdef",
                "raw": "omitted",
            },
        }

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(edgeconnect.edgeconnect_get_link_integrity_status("1.NE", max_result_chars=3))

    assert out["ne_pk"] == "1.NE"
    assert out["link_integrity_status"] == {
        "active": False,
        "result": "abc...",
        "result_truncated": True,
    }


def test_edgeconnect_get_disk_report_compacts(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"disks":[{"name":"disk0"}]}'

        def json(self):
            return {
                "disks": [
                    {
                        "name": "disk0",
                        "model": "SSD",
                        "serial": "abc123",
                        "capacity": "256GB",
                        "used": "100GB",
                        "health": "ok",
                        "raw": "omitted",
                    },
                    {
                        "name": "disk1",
                        "model": "SSD",
                        "serial": "def456",
                        "capacity": "256GB",
                        "used": "120GB",
                        "health": "ok",
                        "raw": "omitted",
                    },
                ]
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(edgeconnect.edgeconnect_get_disk_report(ne_pk="1.NE", limit=1))

    assert called["url"] == "https://orch.example.com/gms/rest/configReportDisk"
    assert called["params"] == {"nePk": "1.NE"}
    assert out["ne_pk"] == "1.NE"
    assert out["disk_report"]["disks"] == [
        {
            "name": "disk0",
            "model": "SSD",
            "serial": "abc123",
            "capacity": "256GB",
            "used": "100GB",
            "health": "ok",
        }
    ]
    assert out["disk_report"]["_pagination"]["truncated"] is True


def test_edgeconnect_get_disk_report_compacts_documented_maps(monkeypatch):
    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        assert path == "/gms/rest/configReportDisk"
        assert params == {"nePk": "1.NE"}
        assert paginate is False
        return {
            "status_code": 200,
            "data": {
                "disks": {
                    "disk0": {
                        "model": "SSD",
                        "serial": "abc123",
                        "capacity": "256GB",
                        "used": "100GB",
                        "health": "ok",
                        "raw": "omitted",
                    }
                },
                "controller": {
                    "model": "RAID",
                    "status": "ok",
                    "raw": "omitted",
                },
                "diskImage": {
                    "version": "9.5.2.1",
                    "active": True,
                    "raw": "omitted",
                },
            },
        }

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(edgeconnect.edgeconnect_get_disk_report(ne_pk="1.NE"))

    assert out["disk_report"]["disks"] == [
        {
            "disk": "disk0",
            "model": "SSD",
            "serial": "abc123",
            "capacity": "256GB",
            "used": "100GB",
            "health": "ok",
        }
    ]
    assert out["disk_report"]["controllers"] == [
        {
            "controller": "controller",
            "model": "RAID",
            "status": "ok",
        }
    ]
    assert out["disk_report"]["disk_images"] == [
        {
            "diskImage": "diskImage",
            "version": "9.5.2.1",
            "active": True,
        }
    ]


def test_edgeconnect_get_appliance_reachability_compacts(monkeypatch):
    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        assert path == "/gms/rest/reachability/appliance"
        assert params == {"nePk": "1.NE"}
        assert paginate is False
        return {
            "status_code": 200,
            "data": {
                "hostName": "ec-1",
                "ipAddress": "192.0.2.10",
                "reachable": True,
                "rest": True,
                "ssh": False,
                "https": True,
                "websocket": False,
                "webProtocol": "https",
                "userName": "admin",
                "unsavedChanges": False,
                "lastSeen": 123456,
                "raw": "omitted",
            },
        }

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(
        edgeconnect.edgeconnect_get_appliance_reachability(ne_pk="1.NE", source="appliance")
    )

    assert out["ne_pk"] == "1.NE"
    assert out["source"] == "appliance"
    assert out["reachability"] == {
        "hostName": "ec-1",
        "ipAddress": "192.0.2.10",
        "reachable": True,
        "rest": True,
        "ssh": False,
        "https": True,
        "websocket": False,
        "webProtocol": "https",
        "userName": "admin",
        "unsavedChanges": False,
        "lastSeen": 123456,
    }


def test_edgeconnect_get_appliance_reachability_compacts_gms_fields(monkeypatch):
    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        assert path == "/gms/rest/reachability/gms"
        assert params == {"nePk": "1.NE"}
        assert paginate is False
        return {
            "status_code": 200,
            "data": {
                "hostName": "ec-1",
                "reachable": True,
                "webSocket": "connected",
                "username": "operator",
                "status": "ok",
                "raw": "omitted",
            },
        }

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(
        edgeconnect.edgeconnect_get_appliance_reachability(ne_pk="1.NE", source="gms")
    )

    assert out["reachability"] == {
        "hostName": "ec-1",
        "reachable": True,
        "webSocket": "connected",
        "username": "operator",
        "status": "ok",
    }


def test_edgeconnect_get_appliance_reachability_rejects_unknown_source():
    out = asyncio.run(
        edgeconnect.edgeconnect_get_appliance_reachability(ne_pk="1.NE", source="bad")
    )

    assert "error" in out
    assert "appliance" in out["error"]
    assert "gms2" in out["error"]


def test_edgeconnect_list_appliance_reachability_compacts_map(monkeypatch):
    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        assert path == "/gms/rest/reachability/gms2/appliancesReachability"
        assert params is None
        assert paginate is False
        return {
            "status_code": 200,
            "data": {
                "1.NE": {
                    "hostName": "ec-1",
                    "reachable": True,
                    "webSocket": "connected",
                    "lastSeen": 123456,
                    "raw": "omitted",
                },
                "2.NE": {
                    "hostName": "ec-2",
                    "reachable": False,
                    "reason": "timeout",
                    "raw": "omitted",
                },
            },
        }

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(edgeconnect.edgeconnect_list_appliance_reachability(limit=1))

    assert out["reachability"]["appliances"] == [
        {
            "nePk": "1.NE",
            "hostName": "ec-1",
            "reachable": True,
            "webSocket": "connected",
            "lastSeen": 123456,
        }
    ]
    assert out["reachability"]["_pagination"]["truncated"] is True


def test_edgeconnect_get_maintenance_mode_compacts_map(monkeypatch):
    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        assert path == "/gms/rest/maintenanceMode"
        assert params is None
        assert paginate is False
        return {
            "status_code": 200,
            "data": {
                "1.NE": {
                    "hostName": "ec-1",
                    "maintenanceMode": True,
                    "reason": "lab change",
                    "userName": "admin",
                    "raw": "omitted",
                },
                "2.NE": {
                    "hostName": "ec-2",
                    "maintenanceMode": False,
                    "raw": "omitted",
                },
            },
        }

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(edgeconnect.edgeconnect_get_maintenance_mode(limit=1))

    assert out["maintenance_mode"]["appliances"] == [
        {
            "nePk": "1.NE",
            "hostName": "ec-1",
            "maintenanceMode": True,
            "reason": "lab change",
            "userName": "admin",
        }
    ]
    assert out["maintenance_mode"]["_pagination"]["truncated"] is True


def test_edgeconnect_get_maintenance_mode_bounds_upstream_lists(monkeypatch):
    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        assert path == "/gms/rest/maintenanceMode"
        assert params is None
        assert paginate is False
        return {
            "status_code": 200,
            "data": {
                "pauseOrchestration": ["1.NE", "2.NE"],
                "suppressAlarm": ["3.NE", "4.NE"],
            },
        }

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(edgeconnect.edgeconnect_get_maintenance_mode(limit=1))

    assert out["maintenance_mode"]["pauseOrchestration"]["items"] == ["1.NE"]
    assert out["maintenance_mode"]["pauseOrchestration"]["_pagination"]["truncated"] is True
    assert out["maintenance_mode"]["suppressAlarm"]["items"] == ["3.NE"]
    assert out["maintenance_mode"]["suppressAlarm"]["_pagination"]["truncated"] is True


def test_edgeconnect_get_appliance_network_role_site_compacts(monkeypatch):
    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        assert path == "/gms/rest/appliance/networkRoleAndSite"
        assert params == {"nePk": "1.NE"}
        assert paginate is False
        return {
            "status_code": 200,
            "data": {
                "hostName": "ec-1",
                "site": "Lab",
                "sitePriority": 100,
                "networkRole": "1",
                "region": "us-west",
                "groupName": "wan-edge",
                "raw": "omitted",
            },
        }

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(edgeconnect.edgeconnect_get_appliance_network_role_site(ne_pk="1.NE"))

    assert out["ne_pk"] == "1.NE"
    assert out["network_role_site"] == {
        "nePk": "1.NE",
        "hostName": "ec-1",
        "site": "Lab",
        "sitePriority": 100,
        "networkRole": "1",
        "region": "us-west",
        "groupName": "wan-edge",
    }


def test_edgeconnect_list_alarms_compacts_outstanding(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"outstanding":[{"id":"alarm1"}]}'

        def json(self):
            return {
                "outstanding": [
                    {
                        "id": "alarm1",
                        "severity": "critical",
                        "message": "Link down",
                        "raw": "omitted",
                    },
                    {
                        "id": "alarm2",
                        "severity": "minor",
                        "message": "Peer changed",
                        "raw": "omitted",
                    },
                ]
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://ec.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(edgeconnect.edgeconnect_list_alarms(limit=1))

    assert called["url"] == "https://ec.example.com/rest/json/alarm"
    assert out["alarms"]["outstanding"] == [
        {"id": "alarm1", "severity": "critical", "message": "Link down"}
    ]
    assert out["alarms"]["_pagination"]["truncated"] is True


def test_edgeconnect_get_topology_link_info_compacts_links(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"nePks":["1.NE","2.NE","3.NE"],"linkInfo":[[],[[0,1]],[[0,2]]]}'

        def json(self):
            return {
                "nePks": ["1.NE", "2.NE", "3.NE"],
                "linkInfo": [
                    [],
                    [[0, 1]],
                    [[0, 2]],
                ],
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(edgeconnect.edgeconnect_get_topology_link_info(overlay_id="all", limit=1))

    assert called["url"] == "https://orch.example.com/gms/rest/gms/topologyConfig/linkInfo/v2"
    assert called["params"] == {"overlayId": "all"}
    assert out["overlay_id"] == "all"
    assert out["topology_links"]["items"] == [
        {
            "srcNePk": "1.NE",
            "destNePk": "2.NE",
            "status": "1",
        }
    ]
    assert out["topology_links"]["_pagination"]["truncated"] is True


def test_edgeconnect_get_route_maps_compacts_maps(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"data":{"RM-CORP":{"sequence":10}}}'

        def json(self):
            return {
                "options": {"cached": True},
                "data": {
                    "RM-CORP": {
                        "prio": {
                            "10": {
                                "match": {"prefix": "10.0.0.0/8"},
                                "set": {"localPreference": 200},
                                "comment": "prefer corp",
                                "gms_marked": False,
                            }
                        },
                        "raw": "omitted",
                    },
                    "RM-GUEST": {
                        "prio": {
                            "20": {
                                "match": {"prefix": "192.0.2.0/24"},
                                "set": {"metric": 50},
                                "comment": "guest",
                                "gms_marked": False,
                            }
                        },
                        "raw": "omitted",
                    },
                },
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(edgeconnect.edgeconnect_get_route_maps(ne_pk="1.NE", cached=False, limit=1))

    assert called["url"] == "https://orch.example.com/gms/rest/routeMaps"
    assert called["params"] == {"nePk": "1.NE", "cached": False}
    assert out["ne_pk"] == "1.NE"
    assert out["route_maps"]["items"] == [
        {
            "name": "RM-CORP",
            "prio": {
                "10": {
                    "match": {"prefix": "10.0.0.0/8"},
                    "set": {"localPreference": 200},
                    "comment": "prefer corp",
                    "gms_marked": False,
                }
            },
        }
    ]
    assert out["route_maps"]["_pagination"]["truncated"] is True


def test_edgeconnect_list_route_labels_compacts_map(monkeypatch):
    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        assert path == "/gms/rest/routeLabels"
        assert params is None
        assert paginate is False
        return {
            "status_code": 200,
            "data": {
                "routeLabels": {
                    "100": {
                        "name": "Corp",
                        "description": "Corporate routes",
                        "color": "#00aa00",
                        "active": True,
                        "raw": "omitted",
                    },
                    "200": {
                        "name": "Guest",
                        "description": "Guest routes",
                        "color": "#ffaa00",
                        "active": True,
                        "raw": "omitted",
                    },
                }
            },
        }

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(edgeconnect.edgeconnect_list_route_labels(limit=1))

    assert out["route_labels"]["route_labels"] == [
        {
            "id": "100",
            "name": "Corp",
            "description": "Corporate routes",
            "color": "#00aa00",
            "active": True,
        }
    ]
    assert out["route_labels"]["_pagination"]["truncated"] is True


def test_edgeconnect_list_address_groups_compacts(monkeypatch):
    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        assert path == "/gms/rest/ipObjects/addressGroup"
        assert params is None
        assert paginate is False
        return {
            "status_code": 200,
            "data": [
                {
                    "name": "OfficeNetwork",
                    "type": "AG",
                    "rules": [
                        {
                            "includedIPs": ["192.168.1.0/24"],
                            "excludedIPs": [],
                            "includedGroups": [],
                            "comment": "Office IPs",
                        }
                    ],
                    "raw": "omitted",
                },
                {
                    "name": "DataCenterNetwork",
                    "type": "AG",
                    "rules": [{"includedIPs": ["172.16.0.0/16"]}],
                    "raw": "omitted",
                },
            ],
        }

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(edgeconnect.edgeconnect_list_address_groups(limit=1))

    assert out["address_groups"]["address_groups"] == [
        {
            "name": "OfficeNetwork",
            "type": "AG",
            "rules": [
                {
                    "includedIPs": ["192.168.1.0/24"],
                    "excludedIPs": [],
                    "includedGroups": [],
                    "comment": "Office IPs",
                }
            ],
        }
    ]
    assert out["address_groups"]["_pagination"]["truncated"] is True


def test_edgeconnect_list_service_groups_compacts_single(monkeypatch):
    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        assert path == "/gms/rest/ipObjects/serviceGroup"
        assert params == {"name": "WebServices"}
        assert paginate is False
        return {
            "status_code": 200,
            "data": {
                "name": "WebServices",
                "type": "SG",
                "rules": [
                    {
                        "protocol": "tcp",
                        "ports": "80,443",
                        "comment": "web",
                    }
                ],
                "raw": "omitted",
            },
        }

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(edgeconnect.edgeconnect_list_service_groups(name=" WebServices "))

    assert out["name"] == "WebServices"
    assert out["service_groups"]["service_groups"] == [
        {
            "name": "WebServices",
            "type": "SG",
            "rules": [{"protocol": "tcp", "ports": "80,443", "comment": "web"}],
        }
    ]


def test_edgeconnect_list_services_compacts_map(monkeypatch):
    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        assert path == "/gms/rest/gms/services"
        assert params is None
        assert paginate is False
        return {
            "status_code": 200,
            "data": {
                "zscaler_west": {
                    "name": "Zscaler West",
                    "img": "/img/zscaler.png",
                    "peerName": "zscaler-west-tunnel",
                    "enabled": True,
                    "raw": "omitted",
                },
                "netskope_main": {
                    "name": "Netskope Main",
                    "peerName": "netskope-main-tunnel",
                    "enabled": True,
                    "raw": "omitted",
                },
            },
        }

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(edgeconnect.edgeconnect_list_services(limit=1))

    assert out["services"]["services"] == [
        {
            "id": "zscaler_west",
            "name": "Zscaler West",
            "img": "/img/zscaler.png",
            "peerName": "zscaler-west-tunnel",
            "enabled": True,
        }
    ]
    assert out["services"]["_pagination"]["truncated"] is True


def test_edgeconnect_list_third_party_services_compacts_map(monkeypatch):
    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        assert path == "/gms/rest/gms/thirdPartyServices"
        assert params is None
        assert paginate is False
        return {
            "status_code": 200,
            "data": {
                "sp_zscaler": {
                    "name": "Zscaler Cloud",
                    "img": "/webclient/img/zscaler.png",
                    "peerName": "Zscaler_*",
                    "enabled": True,
                    "raw": "omitted",
                },
                "sp_netskope": {
                    "name": "Netskope Cloud",
                    "peerName": "Netskope_*",
                    "enabled": False,
                    "raw": "omitted",
                },
            },
        }

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(edgeconnect.edgeconnect_list_third_party_services(limit=1))

    assert out["third_party_services"]["third_party_services"] == [
        {
            "id": "sp_zscaler",
            "name": "Zscaler Cloud",
            "img": "/webclient/img/zscaler.png",
            "peerName": "Zscaler_*",
            "enabled": True,
        }
    ]
    assert out["third_party_services"]["_pagination"]["truncated"] is True


def test_edgeconnect_list_zones_compacts_map(monkeypatch):
    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        assert path == "/gms/rest/zones"
        assert params == {"allVRFZones": True}
        assert paginate is False
        return {
            "status_code": 200,
            "data": {
                "1": {"name": "Voice", "raw": "omitted"},
                "2": {"name": "Data", "raw": "omitted"},
                "metadata": {"raw": "not a zone"},
            },
        }

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(edgeconnect.edgeconnect_list_zones(all_vrf_zones=True, limit=1))

    assert out["all_vrf_zones"] is True
    assert out["zones"]["zones"] == [{"id": 1, "name": "Voice"}]
    assert out["zones"]["_pagination"]["truncated"] is True


def test_edgeconnect_get_zone_firewall_status_compacts(monkeypatch):
    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        assert path == "/gms/rest/zones/eeEnable"
        assert params is None
        assert paginate is False
        return {
            "status_code": 200,
            "data": {"enable": True, "raw": "omitted"},
        }

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(edgeconnect.edgeconnect_get_zone_firewall_status())

    assert out["zone_firewall_status"] == {"enable": True}


def test_edgeconnect_get_next_zone_id_compacts(monkeypatch):
    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        assert path == "/gms/rest/zones/nextId"
        assert params is None
        assert paginate is False
        return {
            "status_code": 200,
            "data": {"nextId": 10, "raw": "omitted"},
        }

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(edgeconnect.edgeconnect_get_next_zone_id())

    assert out["next_zone_id"] == {"nextId": 10}


def test_edgeconnect_list_vrf_segment_zones_compacts(monkeypatch):
    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        assert path == "/gms/rest/zones/vrfSegmentZonesMap"
        assert params is None
        assert paginate is False
        return {
            "status_code": 200,
            "data": [
                {"zoneId": 0, "zoneName": "Default", "vrfId": 0, "vrfName": "Default"},
                {"zoneId": 101, "zoneName": "Voice", "vrfId": 1, "vrfName": "Corporate"},
            ],
        }

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(edgeconnect.edgeconnect_list_vrf_segment_zones(limit=1))

    assert out["vrf_segment_zones"]["zones"] == [
        {"zoneId": 0, "zoneName": "Default", "vrfId": 0, "vrfName": "Default"}
    ]
    assert out["vrf_segment_zones"]["_pagination"]["truncated"] is True


def test_edgeconnect_list_vrf_zone_map_compacts_nested_map(monkeypatch):
    async def _fake_get(path, params=None, limit=50, offset=0, paginate=True):
        assert path == "/gms/rest/zones/vrfZonesMap"
        assert params is None
        assert paginate is False
        return {
            "status_code": 200,
            "data": {
                "1": {
                    "0": {"id": 0, "name": "Default", "raw": "omitted"},
                    "1": {"id": 101, "name": "Voice", "raw": "omitted"},
                }
            },
        }

    monkeypatch.setattr(edgeconnect, "_edgeconnect_get", _fake_get)

    out = asyncio.run(edgeconnect.edgeconnect_list_vrf_zone_map(limit=1))

    assert out["vrf_zone_map"]["zones"] == [
        {"id": 0, "zoneIndex": 0, "name": "Default", "vrfId": 1}
    ]
    assert out["vrf_zone_map"]["_pagination"]["truncated"] is True


def test_edgeconnect_empty_topology_and_route_maps_return_paginated_items(monkeypatch):
    responses = [
        {"nePks": ["1.NE"], "linkInfo": [[], [], []]},
        {"options": {"cached": True}, "data": {}},
    ]
    called = []

    class _Resp:
        status_code = 200

        @property
        def text(self):
            return "{}"

        def json(self):
            return responses.pop(0)

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called.append((url, params or {}))
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    topology = asyncio.run(edgeconnect.edgeconnect_get_topology_link_info())
    routes = asyncio.run(edgeconnect.edgeconnect_get_route_maps(ne_pk="1.NE"))

    assert topology["topology_links"]["items"] == []
    assert topology["topology_links"]["_pagination"]["total"] == 0
    assert routes["route_maps"]["items"] == []
    assert routes["route_maps"]["_pagination"]["total"] == 0
    assert called == [
        (
            "https://orch.example.com/gms/rest/gms/topologyConfig/linkInfo/v2",
            {"overlayId": "all"},
        ),
        ("https://orch.example.com/gms/rest/routeMaps", {"nePk": "1.NE"}),
    ]


def test_edgeconnect_list_overlays_filters_keyed_map_and_compacts(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"1":{"name":"Corp"}}'

        def json(self):
            return {
                "1": {
                    "name": "Corp",
                    "mode": "mesh",
                    "status": "active",
                    "raw": "omitted",
                },
                "2": {
                    "name": "Guest",
                    "mode": "hub-spoke",
                    "status": "active",
                    "raw": "omitted",
                },
                "metadata": {"raw": "not an overlay"},
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(edgeconnect.edgeconnect_list_overlays(overlay_id=1, limit=1))

    assert called["url"] == "https://orch.example.com/gms/rest/gms/overlays/config"
    assert called["params"] == {"overlayId": 1}
    assert out["overlays"]["items"] == [
        {
            "overlayId": 1,
            "name": "Corp",
            "mode": "mesh",
            "status": "active",
        }
    ]
    assert out["overlays"]["_pagination"]["total"] == 1


def test_edgeconnect_list_overlays_compacts_single_overlay_response(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"overlayId":1}'

        def json(self):
            return {
                "overlayId": 1,
                "name": "Corp",
                "mode": "mesh",
                "status": "active",
                "raw": "omitted",
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(edgeconnect.edgeconnect_list_overlays(overlay_id=1))

    assert called["url"] == "https://orch.example.com/gms/rest/gms/overlays/config"
    assert called["params"] == {"overlayId": 1}
    assert out["overlays"]["items"] == [
        {
            "overlayId": 1,
            "name": "Corp",
            "mode": "mesh",
            "status": "active",
        }
    ]


def test_edgeconnect_list_overlays_filters_list_shape_before_paging(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"overlays":[{"overlayId":1}]}'

        def json(self):
            return {
                "overlays": [
                    {
                        "overlayId": 1,
                        "name": "Corp",
                        "status": "active",
                        "raw": "omitted",
                    },
                    {
                        "overlayId": 2,
                        "name": "Guest",
                        "status": "active",
                        "raw": "omitted",
                    },
                ]
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(edgeconnect.edgeconnect_list_overlays(overlay_id=2, limit=1))

    assert called["url"] == "https://orch.example.com/gms/rest/gms/overlays/config"
    assert called["params"] == {"overlayId": 2}
    assert out["overlays"]["items"] == [
        {
            "overlayId": 2,
            "name": "Guest",
            "status": "active",
        }
    ]


def test_edgeconnect_get_overlay_priority_preserves_pagination(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"priorities":[{"overlayId":1}]}'

        def json(self):
            return {
                "priorities": [
                    {
                        "overlayId": 1,
                        "name": "Corp",
                        "priority": 10,
                        "raw": "omitted",
                    },
                    {
                        "overlayId": 2,
                        "name": "Guest",
                        "priority": 20,
                        "raw": "omitted",
                    },
                ]
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(edgeconnect.edgeconnect_get_overlay_priority(limit=1))

    assert called["url"] == "https://orch.example.com/gms/rest/gms/overlays/priority"
    assert called["params"] == {}
    assert out["overlay_priority"]["priorities"] == [
        {"overlayId": 1, "name": "Corp", "priority": 10}
    ]
    assert out["overlay_priority"]["_pagination"]["truncated"] is True


def test_edgeconnect_list_tunnels_filters_and_compacts(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"tunnels":[{"id":"tun1"}]}'

        def json(self):
            return {
                "tunnels": [
                    {
                        "id": "tun1",
                        "alias": "branch-a",
                        "srcNePk": "1.NE",
                        "destNePk": "2.NE",
                        "operStatus": "Up",
                        "adminStatus": "Up",
                        "raw": "omitted",
                    },
                    {
                        "id": "tun2",
                        "alias": "branch-b",
                        "srcNePk": "1.NE",
                        "destNePk": "3.NE",
                        "operStatus": "Down",
                        "adminStatus": "Up",
                        "raw": "omitted",
                    },
                ]
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(
        edgeconnect.edgeconnect_list_tunnels(
            ne_pk="1.NE",
            state="up|down",
            matching_alias="branch",
            limit=1,
        )
    )

    assert called["url"] == "https://orch.example.com/gms/rest/tunnels2/physical"
    assert called["params"] == {
        "nePk": "1.NE",
        "state": "up|down",
        "matchingAlias": "branch",
        "limit": 1,
    }
    assert out["tunnels"]["tunnels"] == [
        {
            "id": "tun1",
            "alias": "branch-a",
            "srcNePk": "1.NE",
            "destNePk": "2.NE",
            "operStatus": "Up",
            "adminStatus": "Up",
        }
    ]
    assert out["tunnels"]["_pagination"]["truncated"] is True


def test_edgeconnect_get_tunnel_metadata_sets_metadata_param(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"totalTunnels":12}'

        def json(self):
            return {
                "totalTunnels": 12,
                "physicalTunnels": 8,
                "bondedTunnels": 4,
                "raw": "omitted",
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(edgeconnect.edgeconnect_get_tunnel_metadata())

    assert called["url"] == "https://orch.example.com/gms/rest/tunnels2"
    assert called["params"] == {"metaData": True}
    assert out["tunnel_metadata"] == {
        "totalTunnels": 12,
        "physicalTunnels": 8,
        "bondedTunnels": 4,
    }


def test_edgeconnect_list_vrf_segments_filters_and_compacts(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"1":{"name":"Corp"}}'

        def json(self):
            return {
                "0": {
                    "name": "Default",
                    "vrfName": "default",
                    "enabled": True,
                    "status": "active",
                    "raw": "omitted",
                },
                "1": {
                    "name": "Corp",
                    "vrfName": "corp-vrf",
                    "enabled": True,
                    "status": "active",
                    "raw": "omitted",
                },
                "metadata": {"raw": "not a segment"},
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(edgeconnect.edgeconnect_list_vrf_segments(segment_id=1, limit=1))

    assert called["url"] == "https://orch.example.com/gms/rest/vrf/config/segments"
    assert called["params"] == {"id": 1}
    assert out["vrf_segments"]["items"] == [
        {
            "id": 1,
            "name": "Corp",
            "vrfName": "corp-vrf",
            "enabled": True,
            "status": "active",
        }
    ]
    assert out["vrf_segments"]["_pagination"]["total"] == 1
    assert out["vrf_segments"]["_pagination"]["truncated"] is False


def test_edgeconnect_list_vrf_segments_filters_list_shape(monkeypatch):
    called = {}

    class _Resp:
        status_code = 200
        text = '{"segments":[{"id":1}]}'

        def json(self):
            return {
                "segments": [
                    {
                        "id": 1,
                        "name": "Corp",
                        "vrfName": "corp-vrf",
                        "status": "active",
                        "raw": "omitted",
                    },
                    {
                        "id": 2,
                        "name": "Guest",
                        "vrfName": "guest-vrf",
                        "status": "active",
                        "raw": "omitted",
                    },
                ]
            }

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(edgeconnect.edgeconnect_list_vrf_segments(segment_id=2, limit=1))

    assert called["url"] == "https://orch.example.com/gms/rest/vrf/config/segments"
    assert called["params"] == {"id": 2}
    assert out["vrf_segments"]["items"] == [
        {
            "id": 2,
            "name": "Guest",
            "vrfName": "guest-vrf",
            "status": "active",
        }
    ]


@pytest.mark.parametrize(
    ("write_func", "env_base", "env_token", "base_url", "path", "expected_url"),
    [
        (
            apstra.apstra_write,
            "APSTRA_BASE_URL",
            "APSTRA_API_TOKEN",
            "https://apstra.example.com",
            "/api/blueprints/bp1",
            "https://apstra.example.com/api/blueprints/bp1",
        ),
        (
            edgeconnect.edgeconnect_write,
            "EDGECONNECT_BASE_URL",
            "EDGECONNECT_API_TOKEN",
            "https://orch.example.com",
            "/gms/rest/appliance",
            "https://orch.example.com/gms/rest/appliance",
        ),
    ],
)
def test_optional_product_write_dry_run_previews(
    write_func,
    env_base,
    env_token,
    base_url,
    path,
    expected_url,
    monkeypatch,
):
    monkeypatch.setenv(env_base, base_url)
    monkeypatch.setenv(env_token, "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(
        write_func(
            "patch",
            path,
            params={"api_key": "abc", "reason": "lab"},
            body={"password": "secret", "enabled": True},
        )
    )

    assert out["dry_run"] is True
    assert out["method"] == "PATCH"
    assert out["url"] == expected_url
    assert out["params"] == {"api_key": "******", "reason": "lab"}
    assert out["json"] == {"password": "******", "enabled": True}
    assert "execute_hint" in out


def test_aos8_write_dry_run_preview_uses_post(monkeypatch):
    """AOS8's documented API has no native PATCH; `aos8_write` only accepts GET/POST."""
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(
        aos8.aos8_write(
            "post",
            "/v1/configuration/object",
            params={"api_key": "abc", "reason": "lab"},
            body={"password": "secret", "enabled": True},
        )
    )

    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert out["url"] == "https://mm.example.com/v1/configuration/object"
    assert out["params"] == {"api_key": "******", "reason": "lab"}
    assert out["json"] == {"password": "******", "enabled": True}
    assert "execute_hint" in out


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_aos8_write_rejects_methods_outside_get_post(monkeypatch, method):
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(aos8.aos8_write(method, "/v1/configuration/object"))

    assert out == {"error": "method must be one of: GET, POST"}


@pytest.mark.parametrize(
    ("tool_call", "expected_body"),
    [
        (
            lambda: aos8.aos8_manage_ssid_profile(
                config_path="/md/lab",
                action="create",
                payload={"profile-name": "Corp", "essid": "Corp"},
            ),
            {"ssid_prof": {"profile-name": "Corp", "essid": "Corp", "_action": "add"}},
        ),
        (
            lambda: aos8.aos8_manage_virtual_ap(
                config_path="/md/lab",
                action="update",
                payload={"profile-name": "Corp-VAP", "ssid-profile": "Corp"},
            ),
            {
                "virtual_ap": {
                    "profile-name": "Corp-VAP",
                    "ssid-profile": "Corp",
                    "_action": "modify",
                }
            },
        ),
        (
            lambda: aos8.aos8_manage_ap_group(
                config_path="/md/lab",
                action="delete",
                payload={"profile-name": "Lab-AP-Group"},
            ),
            {"ap_group": {"profile-name": "Lab-AP-Group", "_action": "delete"}},
        ),
        (
            lambda: aos8.aos8_manage_user_role(
                config_path="/md/lab",
                action="create",
                payload={"rolename": "employee", "vlan": 20},
            ),
            {"role": {"rolename": "employee", "vlan": 20, "_action": "add"}},
        ),
        (
            lambda: aos8.aos8_manage_vlan(
                config_path="/md/lab",
                action="create",
                payload={"id": 20, "description": "Corp"},
            ),
            {"vlan_id": {"id": 20, "description": "Corp", "_action": "add"}},
        ),
    ],
)
def test_aos8_typed_config_writes_dry_run_preview(
    monkeypatch,
    tool_call,
    expected_body,
):
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(tool_call())

    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert out["url"] == "https://mm.example.com/v1/configuration/object"
    assert out["params"] == {"config_path": "/md/lab"}
    assert out["json"] == expected_body
    assert out["requires_write_memory_for"] == ["/md/lab"]
    assert "execute_hint" in out


def test_aos8_typed_config_write_rejects_invalid_action_and_missing_identifier(monkeypatch):
    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, headers=None, params=None, json=None):
            raise AssertionError("invalid typed write should not execute")

    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", _FakeAsyncClient)

    bad_action = asyncio.run(
        aos8.aos8_manage_vlan(
            config_path="/md/lab",
            action="merge",
            payload={"id": 20},
        )
    )
    missing_identifier = asyncio.run(
        aos8.aos8_manage_user_role(
            config_path="/md/lab",
            action="create",
            payload={"role": "employee"},
        )
    )

    assert bad_action == {"error": "action must be one of: create, update, delete"}
    assert missing_identifier == {"error": "payload must include one of: 'rolename'"}


def test_aos8_typed_config_write_executes_and_returns_write_memory_hint(monkeypatch):
    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, headers=None, params=None, json=None):
            called["method"] = method
            called["url"] = url
            called["headers"] = headers or {}
            called["params"] = params or {}
            called["json"] = json
            return _Resp()

    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(
        aos8.aos8_manage_ssid_profile(
            config_path="/md/lab",
            action="update",
            payload={"profile-name": "Corp", "essid": "Corp"},
            dry_run=False,
            confirm=True,
        )
    )

    assert out["status_code"] == 200
    assert out["requires_write_memory_for"] == ["/md/lab"]
    assert called["method"] == "POST"
    assert called["url"] == "https://mm.example.com/v1/configuration/object"
    assert called["headers"]["Authorization"].startswith("Bearer ")
    assert called["params"] == {"config_path": "/md/lab"}
    assert called["json"] == {
        "ssid_prof": {"profile-name": "Corp", "essid": "Corp", "_action": "modify"}
    }


@pytest.mark.parametrize(
    "response",
    [
        type(
            "_AOS8HttpErrorResp",
            (),
            {
                "status_code": 400,
                "text": '{"error":"bad request"}',
                "json": lambda self: {"error": "bad request"},
            },
        )(),
        type(
            "_AOS8GlobalErrorResp",
            (),
            {
                "status_code": 200,
                "text": '{"_global_result":{"status":"1"}}',
                "json": lambda self: {"_global_result": {"status": "1", "reason": "invalid"}},
            },
        )(),
    ],
)
def test_aos8_typed_config_write_does_not_hint_write_memory_on_failure(
    monkeypatch,
    response,
):
    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, headers=None, params=None, json=None):
            return response

    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(
        aos8.aos8_manage_ssid_profile(
            config_path="/md/lab",
            action="update",
            payload={"profile-name": "Corp", "essid": "Corp"},
            dry_run=False,
            confirm=True,
        )
    )

    assert "requires_write_memory_for" not in out


def test_aos8_write_memory_uses_dedicated_endpoint(monkeypatch):
    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, headers=None, params=None, json=None):
            called["method"] = method
            called["url"] = url
            called["params"] = params or {}
            called["json"] = json
            return _Resp()

    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(aos8.aos8_write_memory("/md/lab", dry_run=False, confirm=True))

    assert out["status_code"] == 200
    assert out["config_path"] == "/md/lab"
    assert called["method"] == "POST"
    assert called["url"] == "https://mm.example.com/v1/configuration/object/write_memory"
    assert called["params"] == {"config_path": "/md/lab"}
    assert called["json"] == {}


def test_aos8_typed_write_blocks_when_product_access_read_only(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")

    out = asyncio.run(
        aos8.aos8_manage_ssid_profile(
            config_path="/md/lab",
            action="create",
            payload={"profile-name": "Corp"},
        )
    )

    assert out["status"] == "blocked"
    assert out["tool"] == "aos8_manage_ssid_profile"
    assert "HPE_MCP_PRODUCT_ACCESS=read-only" in out["error"]


@pytest.mark.parametrize(
    ("write_func", "path"),
    [
        (apstra.apstra_write, "/api/blueprints/bp1"),
        (aos8.aos8_write, "/v1/configuration/object"),
        (edgeconnect.edgeconnect_write, "/gms/rest/appliance"),
    ],
)
def test_optional_product_write_blocks_when_product_access_read_only(
    write_func,
    path,
    monkeypatch,
):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")

    out = asyncio.run(write_func("patch", path, body={"enabled": True}))

    assert out["status"] == "blocked"
    assert "HPE_MCP_PRODUCT_ACCESS=read-only" in out["error"]


@pytest.mark.parametrize(
    ("module", "write_func", "env_base", "env_token", "base_url", "path"),
    [
        (
            apstra,
            apstra.apstra_write,
            "APSTRA_BASE_URL",
            "APSTRA_API_TOKEN",
            "https://apstra.example.com",
            "/api/blueprints/bp1",
        ),
        (
            edgeconnect,
            edgeconnect.edgeconnect_write,
            "EDGECONNECT_BASE_URL",
            "EDGECONNECT_API_TOKEN",
            "https://orch.example.com",
            "/gms/rest/appliance",
        ),
    ],
)
def test_optional_product_write_requires_confirm_when_not_dry_run(
    module,
    write_func,
    env_base,
    env_token,
    base_url,
    path,
    monkeypatch,
):
    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, headers=None, params=None, json=None):
            raise AssertionError("request should not execute without confirm=True")

    monkeypatch.setenv(env_base, base_url)
    monkeypatch.setenv(env_token, "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(
        write_func("PATCH", path, body={"name": "lab"}, dry_run=False, confirm=False)
    )

    assert out["dry_run"] is True
    assert out["error"] == "confirm=True is required when dry_run=False."


def test_aos8_write_requires_confirm_when_not_dry_run(monkeypatch):
    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, headers=None, params=None, json=None):
            raise AssertionError("request should not execute without confirm=True")

    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(
        aos8.aos8_write(
            "POST",
            "/v1/configuration/object",
            body={"name": "lab"},
            dry_run=False,
            confirm=False,
        )
    )

    assert out["dry_run"] is True
    assert out["error"] == "confirm=True is required when dry_run=False."



@pytest.mark.parametrize(
    ("module", "write_func", "env_base", "env_token", "base_url", "path", "expected_url"),
    [
        (
            apstra,
            apstra.apstra_write,
            "APSTRA_BASE_URL",
            "APSTRA_API_TOKEN",
            "https://apstra.example.com",
            "/api/blueprints/bp1",
            "https://apstra.example.com/api/blueprints/bp1",
        ),
        (
            edgeconnect,
            edgeconnect.edgeconnect_write,
            "EDGECONNECT_BASE_URL",
            "EDGECONNECT_API_TOKEN",
            "https://orch.example.com",
            "/gms/rest/appliance",
            "https://orch.example.com/gms/rest/appliance",
        ),
    ],
)
def test_optional_product_write_executes_with_default_bearer_auth(
    module,
    write_func,
    env_base,
    env_token,
    base_url,
    path,
    expected_url,
    monkeypatch,
):
    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, headers=None, params=None, json=None):
            called["method"] = method
            called["url"] = url
            called["headers"] = headers or {}
            called["params"] = params or {}
            called["json"] = json
            return _Resp()

    monkeypatch.setenv(env_base, base_url)
    monkeypatch.setenv(env_token, "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.delenv("EDGECONNECT_AUTH_HEADER", raising=False)
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(
        write_func("PATCH", path, body={"name": "lab"}, dry_run=False, confirm=True)
    )

    assert out["status_code"] == 200
    assert called["method"] == "PATCH"
    assert called["url"] == expected_url
    if module is apstra:
        assert called["headers"]["AuthToken"] == "secret"
        assert "Authorization" not in called["headers"]
    else:
        assert called["headers"]["X-Auth-Token"] == "secret"
        assert "Authorization" not in called["headers"]
    assert called["json"] == {"name": "lab"}



def test_aos8_write_executes_post_with_default_bearer_auth(monkeypatch):
    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, headers=None, params=None, json=None):
            called["method"] = method
            called["url"] = url
            called["headers"] = headers or {}
            called["params"] = params or {}
            called["json"] = json
            return _Resp()

    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(
        aos8.aos8_write(
            "POST",
            "/v1/configuration/object",
            body={"name": "lab"},
            dry_run=False,
            confirm=True,
        )
    )

    assert out["status_code"] == 200
    assert called["method"] == "POST"
    assert called["url"] == "https://mm.example.com/v1/configuration/object"
    assert called["headers"]["Authorization"] == "Bearer secret"
    assert called["json"] == {"name": "lab"}


def test_edgeconnect_write_executes_with_custom_auth_header(monkeypatch):
    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, headers=None, params=None, json=None):
            called["method"] = method
            called["url"] = url
            called["headers"] = headers or {}
            called["params"] = params or {}
            called["json"] = json
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("EDGECONNECT_AUTH_HEADER", "X-Auth-Token")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(
        edgeconnect.edgeconnect_write(
            "POST",
            "/gms/rest/appliance",
            body={"name": "lab"},
            dry_run=False,
            confirm=True,
        )
    )

    assert out["status_code"] == 200
    assert out["data"] == {"ok": True}
    assert called["method"] == "POST"
    assert called["url"] == "https://orch.example.com/gms/rest/appliance"
    assert called["headers"]["X-Auth-Token"] == "secret"
    assert called["json"] == {"name": "lab"}


def test_edgeconnect_save_changes_previews_with_nepk(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(edgeconnect.edgeconnect_save_changes(ne_pk="1.NE", body={"save": True}))

    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert out["path"] == "/gms/rest/appliance/saveChanges"
    assert out["params"] == {"nePk": "1.NE"}
    assert out["json"] == {"save": True}
    assert "execute_hint" in out


def test_edgeconnect_save_changes_blocks_when_read_only(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")

    out = asyncio.run(edgeconnect.edgeconnect_save_changes(ne_pk="1.NE"))

    assert out["status"] == "blocked"
    assert out["tool"] == "edgeconnect_save_changes"
    assert "HPE_MCP_PRODUCT_ACCESS=read-only" in out["error"]


def test_edgeconnect_save_changes_executes_with_confirm(monkeypatch):
    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, headers=None, params=None, json=None):
            called["method"] = method
            called["url"] = url
            called["headers"] = headers or {}
            called["params"] = params or {}
            called["json"] = json
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(
        edgeconnect.edgeconnect_save_changes(
            ne_pk="1.NE",
            body={"save": True},
            dry_run=False,
            confirm=True,
        )
    )

    assert out["status_code"] == 200
    assert called["method"] == "POST"
    assert called["url"] == "https://orch.example.com/gms/rest/appliance/saveChanges"
    assert called["params"] == {"nePk": "1.NE"}
    assert called["json"] == {"save": True}


def test_edgeconnect_set_maintenance_mode_previews(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(
        edgeconnect.edgeconnect_set_maintenance_mode(
            {"nePks": ["1.NE"], "enabled": True, "reason": "lab change"}
        )
    )

    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert out["path"] == "/gms/rest/maintenanceMode"
    assert out["json"] == {"nePks": ["1.NE"], "enabled": True, "reason": "lab change"}
    assert "execute_hint" in out


def test_edgeconnect_set_maintenance_mode_blocks_when_read_only(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")

    out = asyncio.run(edgeconnect.edgeconnect_set_maintenance_mode({"nePks": ["1.NE"]}))

    assert out["status"] == "blocked"
    assert out["tool"] == "edgeconnect_set_maintenance_mode"
    assert "HPE_MCP_PRODUCT_ACCESS=read-only" in out["error"]


def test_edgeconnect_set_maintenance_mode_executes_with_confirm(monkeypatch):
    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, headers=None, params=None, json=None):
            called["method"] = method
            called["url"] = url
            called["headers"] = headers or {}
            called["params"] = params or {}
            called["json"] = json
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(
        edgeconnect.edgeconnect_set_maintenance_mode(
            {"nePks": ["1.NE"], "enabled": False},
            dry_run=False,
            confirm=True,
        )
    )

    assert out["status_code"] == 200
    assert called["method"] == "POST"
    assert called["url"] == "https://orch.example.com/gms/rest/maintenanceMode"
    assert called["json"] == {"nePks": ["1.NE"], "enabled": False}


def test_edgeconnect_set_appliance_network_role_site_previews(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(
        edgeconnect.edgeconnect_set_appliance_network_role_site(
            "1.NE",
            {"site": "Lab", "sitePriority": 100, "networkRole": "1"},
        )
    )

    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert out["path"] == "/gms/rest/appliance/networkRoleAndSite"
    assert out["params"] == {"nePk": "1.NE"}
    assert out["json"] == {"site": "Lab", "sitePriority": 100, "networkRole": "1"}
    assert "execute_hint" in out


def test_edgeconnect_set_appliance_network_role_site_blocks_when_read_only(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")

    out = asyncio.run(
        edgeconnect.edgeconnect_set_appliance_network_role_site(
            "1.NE",
            {"site": "Lab"},
        )
    )

    assert out["status"] == "blocked"
    assert out["tool"] == "edgeconnect_set_appliance_network_role_site"
    assert "HPE_MCP_PRODUCT_ACCESS=read-only" in out["error"]


def test_edgeconnect_set_appliance_network_role_site_executes_with_confirm(monkeypatch):
    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, headers=None, params=None, json=None):
            called["method"] = method
            called["url"] = url
            called["headers"] = headers or {}
            called["params"] = params or {}
            called["json"] = json
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(
        edgeconnect.edgeconnect_set_appliance_network_role_site(
            "1.NE",
            {"site": "Lab", "sitePriority": 100, "networkRole": "1"},
            dry_run=False,
            confirm=True,
        )
    )

    assert out["status_code"] == 200
    assert called["method"] == "POST"
    assert called["url"] == "https://orch.example.com/gms/rest/appliance/networkRoleAndSite"
    assert called["params"] == {"nePk": "1.NE"}
    assert called["json"] == {"site": "Lab", "sitePriority": 100, "networkRole": "1"}


def test_edgeconnect_set_route_labels_previews(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(
        edgeconnect.edgeconnect_set_route_labels(
            {"routeLabels": [{"id": 100, "name": "Corp", "active": True}]}
        )
    )

    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert out["path"] == "/gms/rest/routeLabels"
    assert out["json"] == {"routeLabels": [{"id": 100, "name": "Corp", "active": True}]}
    assert "execute_hint" in out


def test_edgeconnect_set_route_labels_blocks_when_read_only(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")

    out = asyncio.run(edgeconnect.edgeconnect_set_route_labels({"routeLabels": []}))

    assert out["status"] == "blocked"
    assert out["tool"] == "edgeconnect_set_route_labels"
    assert "HPE_MCP_PRODUCT_ACCESS=read-only" in out["error"]


def test_edgeconnect_set_route_labels_executes_with_confirm(monkeypatch):
    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, headers=None, params=None, json=None):
            called["method"] = method
            called["url"] = url
            called["headers"] = headers or {}
            called["params"] = params or {}
            called["json"] = json
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(
        edgeconnect.edgeconnect_set_route_labels(
            {"routeLabels": [{"id": 100, "name": "Corp", "active": True}]},
            dry_run=False,
            confirm=True,
        )
    )

    assert out["status_code"] == 200
    assert called["method"] == "POST"
    assert called["url"] == "https://orch.example.com/gms/rest/routeLabels"
    assert called["json"] == {"routeLabels": [{"id": 100, "name": "Corp", "active": True}]}


def test_edgeconnect_set_interface_labels_previews(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    body = {
        "wan": {"1": {"name": "MPLS", "active": True, "topology": 0}},
        "lan": {"4": {"name": "Voice", "active": True, "topology": 0}},
    }
    out = asyncio.run(
        edgeconnect.edgeconnect_set_interface_labels(
            body,
            delete_dependencies=False,
        )
    )

    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert out["path"] == "/gms/rest/gms/interfaceLabels"
    assert out["params"] == {"deleteDependencies": False}
    assert out["json"] == body
    assert "execute_hint" in out


def test_edgeconnect_apply_interface_labels_previews(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(edgeconnect.edgeconnect_apply_interface_labels(" 1.NE "))

    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert out["path"] == "/gms/rest/interfaceLabels"
    assert out["params"] == {"nePk": "1.NE"}
    assert out["json"] == {}
    assert "execute_hint" in out


def test_edgeconnect_apply_interface_labels_requires_nepk(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(edgeconnect.edgeconnect_apply_interface_labels(" "))

    assert out == {"error": "ne_pk is required."}


@pytest.mark.parametrize(
    "tool_call",
    [
        lambda: edgeconnect.edgeconnect_set_interface_labels({"wan": {}}),
        lambda: edgeconnect.edgeconnect_apply_interface_labels("1.NE"),
    ],
)
def test_edgeconnect_interface_label_writes_block_when_read_only(monkeypatch, tool_call):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")

    out = asyncio.run(tool_call())

    assert out["status"] == "blocked"
    assert "HPE_MCP_PRODUCT_ACCESS=read-only" in out["error"]


@pytest.mark.parametrize(
    ("tool_call", "expected_url", "expected_params", "expected_body"),
    [
        (
            lambda: edgeconnect.edgeconnect_set_interface_labels(
                {"wan": {"1": {"name": "MPLS", "active": True, "topology": 0}}},
                delete_dependencies=True,
                dry_run=False,
                confirm=True,
            ),
            "https://orch.example.com/gms/rest/gms/interfaceLabels",
            {"deleteDependencies": True},
            {"wan": {"1": {"name": "MPLS", "active": True, "topology": 0}}},
        ),
        (
            lambda: edgeconnect.edgeconnect_apply_interface_labels(
                "1.NE",
                dry_run=False,
                confirm=True,
            ),
            "https://orch.example.com/gms/rest/interfaceLabels",
            {"nePk": "1.NE"},
            {},
        ),
    ],
)
def test_edgeconnect_interface_label_writes_execute_with_confirm(
    monkeypatch,
    tool_call,
    expected_url,
    expected_params,
    expected_body,
):
    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, headers=None, params=None, json=None):
            called["method"] = method
            called["url"] = url
            called["params"] = params or {}
            called["json"] = json
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(tool_call())

    assert out["status_code"] == 200
    assert called["method"] == "POST"
    assert called["url"] == expected_url
    assert called["params"] == expected_params
    assert called["json"] == expected_body


def test_edgeconnect_set_bypass_mode_previews(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(edgeconnect.edgeconnect_set_bypass_mode(True, [" 1.NE ", "2.NE"]))

    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert out["path"] == "/gms/rest/bypass"
    assert out["json"] == {"enable": True, "nePks": ["1.NE", "2.NE"]}
    assert "execute_hint" in out


def test_edgeconnect_set_bypass_mode_blocks_when_read_only(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")

    out = asyncio.run(edgeconnect.edgeconnect_set_bypass_mode(True, ["1.NE"]))

    assert out["status"] == "blocked"
    assert out["tool"] == "edgeconnect_set_bypass_mode"
    assert "HPE_MCP_PRODUCT_ACCESS=read-only" in out["error"]


def test_edgeconnect_set_bypass_mode_requires_nepks(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(edgeconnect.edgeconnect_set_bypass_mode(False, [" "]))

    assert out == {"error": "ne_pks must include at least one appliance nePk."}


def test_edgeconnect_set_bypass_mode_executes_with_confirm(monkeypatch):
    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, headers=None, params=None, json=None):
            called["method"] = method
            called["url"] = url
            called["params"] = params or {}
            called["json"] = json
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(
        edgeconnect.edgeconnect_set_bypass_mode(
            False,
            ["1.NE"],
            dry_run=False,
            confirm=True,
        )
    )

    assert out["status_code"] == 200
    assert called["method"] == "POST"
    assert called["url"] == "https://orch.example.com/gms/rest/bypass"
    assert called["params"] == {}
    assert called["json"] == {"enable": False, "nePks": ["1.NE"]}


def _link_integrity_body() -> dict:
    return {
        "nePks": ["1.NE", "2.NE"],
        "1.NE": {
            "bandwidth": "10000",
            "path": "tunnel_1",
            "tunnelInterface": {"name": "wan0", "ip": "192.0.2.1"},
            "trafficClassId": "1",
        },
        "2.NE": {
            "bandwidth": "10000",
            "path": "tunnel_1",
            "tunnelInterface": {"name": "wan0", "ip": "192.0.2.2"},
            "trafficClassId": "1",
        },
        "duration": "30",
        "DSCP": "any",
        "testProgram": "iperf",
    }


def test_edgeconnect_run_link_integrity_test_previews(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    body = _link_integrity_body()
    out = asyncio.run(edgeconnect.edgeconnect_run_link_integrity_test(body))

    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert out["path"] == "/gms/rest/linkIntegrityTest/run"
    assert out["json"] == body
    assert "execute_hint" in out


def test_edgeconnect_run_link_integrity_test_blocks_when_read_only(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")

    out = asyncio.run(edgeconnect.edgeconnect_run_link_integrity_test(_link_integrity_body()))

    assert out["status"] == "blocked"
    assert out["tool"] == "edgeconnect_run_link_integrity_test"
    assert "HPE_MCP_PRODUCT_ACCESS=read-only" in out["error"]


def test_edgeconnect_run_link_integrity_test_requires_two_nepks(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(edgeconnect.edgeconnect_run_link_integrity_test({"nePks": ["1.NE"]}))

    assert out == {"error": "body.nePks must contain exactly two appliance nePk values."}


def test_edgeconnect_run_link_integrity_test_executes_with_confirm(monkeypatch):
    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, headers=None, params=None, json=None):
            called["method"] = method
            called["url"] = url
            called["params"] = params or {}
            called["json"] = json
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    body = _link_integrity_body()
    out = asyncio.run(
        edgeconnect.edgeconnect_run_link_integrity_test(
            body,
            dry_run=False,
            confirm=True,
        )
    )

    assert out["status_code"] == 200
    assert called["method"] == "POST"
    assert called["url"] == "https://orch.example.com/gms/rest/linkIntegrityTest/run"
    assert called["params"] == {}
    assert called["json"] == body


def test_edgeconnect_set_address_group_previews(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    body = {
        "name": "OfficeNetwork",
        "type": "AG",
        "rules": [{"includedIPs": ["192.168.1.0/24"], "excludedIPs": []}],
    }
    out = asyncio.run(edgeconnect.edgeconnect_set_address_group(body))
    replace = asyncio.run(edgeconnect.edgeconnect_set_address_group(body, replace_existing=True))

    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert out["path"] == "/gms/rest/ipObjects/addressGroup"
    assert out["json"] == body
    assert "execute_hint" in out
    assert replace["method"] == "PUT"


def test_edgeconnect_set_service_group_previews(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    body = {
        "name": "WebServices",
        "type": "SG",
        "rules": [{"protocol": "tcp", "ports": "80,443"}],
    }
    out = asyncio.run(edgeconnect.edgeconnect_set_service_group(body))

    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert out["path"] == "/gms/rest/ipObjects/serviceGroup"
    assert out["json"] == body
    assert "execute_hint" in out


@pytest.mark.parametrize(
    ("tool_call", "expected_path"),
    [
        (
            lambda: edgeconnect.edgeconnect_delete_address_group(" OfficeNetwork "),
            "/gms/rest/ipObjects/addressGroup",
        ),
        (
            lambda: edgeconnect.edgeconnect_delete_service_group(" WebServices "),
            "/gms/rest/ipObjects/serviceGroup",
        ),
    ],
)
def test_edgeconnect_delete_ip_object_groups_preview(monkeypatch, tool_call, expected_path):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(tool_call())

    assert out["dry_run"] is True
    assert out["method"] == "DELETE"
    assert out["path"] == expected_path
    assert out["json"] is None
    assert "name" in out["params"]
    assert "execute_hint" in out


@pytest.mark.parametrize(
    "tool_call",
    [
        lambda: edgeconnect.edgeconnect_delete_address_group(" "),
        lambda: edgeconnect.edgeconnect_delete_service_group(" "),
    ],
)
def test_edgeconnect_delete_ip_object_groups_require_name(monkeypatch, tool_call):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(tool_call())

    assert out == {"error": "name is required."}


@pytest.mark.parametrize(
    "tool_call",
    [
        lambda: edgeconnect.edgeconnect_set_address_group({"name": "OfficeNetwork"}),
        lambda: edgeconnect.edgeconnect_delete_address_group("OfficeNetwork"),
        lambda: edgeconnect.edgeconnect_set_service_group({"name": "WebServices"}),
        lambda: edgeconnect.edgeconnect_delete_service_group("WebServices"),
    ],
)
def test_edgeconnect_ip_object_group_writes_block_when_read_only(monkeypatch, tool_call):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")

    out = asyncio.run(tool_call())

    assert out["status"] == "blocked"
    assert "HPE_MCP_PRODUCT_ACCESS=read-only" in out["error"]


@pytest.mark.parametrize(
    ("tool_call", "expected_method", "expected_url", "expected_params", "expected_body"),
    [
        (
            lambda: edgeconnect.edgeconnect_set_address_group(
                {"name": "OfficeNetwork", "type": "AG"},
                dry_run=False,
                confirm=True,
            ),
            "POST",
            "https://orch.example.com/gms/rest/ipObjects/addressGroup",
            {},
            {"name": "OfficeNetwork", "type": "AG"},
        ),
        (
            lambda: edgeconnect.edgeconnect_set_service_group(
                {"name": "WebServices", "type": "SG"},
                replace_existing=True,
                dry_run=False,
                confirm=True,
            ),
            "PUT",
            "https://orch.example.com/gms/rest/ipObjects/serviceGroup",
            {},
            {"name": "WebServices", "type": "SG"},
        ),
        (
            lambda: edgeconnect.edgeconnect_delete_address_group(
                "OfficeNetwork",
                dry_run=False,
                confirm=True,
            ),
            "DELETE",
            "https://orch.example.com/gms/rest/ipObjects/addressGroup",
            {"name": "OfficeNetwork"},
            None,
        ),
        (
            lambda: edgeconnect.edgeconnect_delete_service_group(
                "WebServices",
                dry_run=False,
                confirm=True,
            ),
            "DELETE",
            "https://orch.example.com/gms/rest/ipObjects/serviceGroup",
            {"name": "WebServices"},
            None,
        ),
    ],
)
def test_edgeconnect_ip_object_group_writes_execute_with_confirm(
    monkeypatch,
    tool_call,
    expected_method,
    expected_url,
    expected_params,
    expected_body,
):
    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, headers=None, params=None, json=None):
            called["method"] = method
            called["url"] = url
            called["params"] = params or {}
            called["json"] = json
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(tool_call())

    assert out["status_code"] == 200
    assert called["method"] == expected_method
    assert called["url"] == expected_url
    assert called["params"] == expected_params
    assert called["json"] == expected_body


def test_edgeconnect_set_services_previews(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    body = {"zscaler_west": {"name": "Zscaler West", "enabled": True}}
    out = asyncio.run(edgeconnect.edgeconnect_set_services(body))

    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert out["path"] == "/gms/rest/gms/services"
    assert out["json"] == body
    assert "execute_hint" in out


def test_edgeconnect_set_services_blocks_when_read_only(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")

    out = asyncio.run(
        edgeconnect.edgeconnect_set_services({"zscaler_west": {"name": "Zscaler West"}})
    )

    assert out["status"] == "blocked"
    assert out["tool"] == "edgeconnect_set_services"
    assert "HPE_MCP_PRODUCT_ACCESS=read-only" in out["error"]


def test_edgeconnect_set_services_executes_with_confirm(monkeypatch):
    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, headers=None, params=None, json=None):
            called["method"] = method
            called["url"] = url
            called["params"] = params or {}
            called["json"] = json
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    body = {"zscaler_west": {"name": "Zscaler West", "enabled": True}}
    out = asyncio.run(
        edgeconnect.edgeconnect_set_services(body, dry_run=False, confirm=True)
    )

    assert out["status_code"] == 200
    assert called["method"] == "POST"
    assert called["url"] == "https://orch.example.com/gms/rest/gms/services"
    assert called["params"] == {}
    assert called["json"] == body


def test_edgeconnect_set_zones_previews(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(
        edgeconnect.edgeconnect_set_zones(
            {"1": {"name": "Voice"}, "2": {"name": "Data"}},
            delete_dependencies=False,
        )
    )

    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert out["path"] == "/gms/rest/zones"
    assert out["params"] == {"deleteDependencies": False}
    assert out["json"] == {"1": {"name": "Voice"}, "2": {"name": "Data"}}
    assert "execute_hint" in out


def test_edgeconnect_set_zone_firewall_status_previews(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(edgeconnect.edgeconnect_set_zone_firewall_status(enabled=True))

    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert out["path"] == "/gms/rest/zones/eeEnable"
    assert out["json"] == {"enable": True}
    assert "execute_hint" in out


def test_edgeconnect_set_next_zone_id_previews_and_validates(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(edgeconnect.edgeconnect_set_next_zone_id(next_id=10))
    invalid = asyncio.run(edgeconnect.edgeconnect_set_next_zone_id(next_id=0))

    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert out["path"] == "/gms/rest/zones/nextId"
    assert out["json"] == {"nextId": 10}
    assert "execute_hint" in out
    assert invalid == {"error": "next_id must be greater than 0."}


@pytest.mark.parametrize(
    "tool_call",
    [
        lambda: edgeconnect.edgeconnect_set_zones({"1": {"name": "Voice"}}),
        lambda: edgeconnect.edgeconnect_set_zone_firewall_status(enabled=True),
        lambda: edgeconnect.edgeconnect_set_next_zone_id(next_id=10),
    ],
)
def test_edgeconnect_zone_writes_block_when_read_only(monkeypatch, tool_call):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")

    out = asyncio.run(tool_call())

    assert out["status"] == "blocked"
    assert "HPE_MCP_PRODUCT_ACCESS=read-only" in out["error"]


@pytest.mark.parametrize(
    ("tool_call", "expected_url", "expected_params", "expected_body"),
    [
        (
            lambda: edgeconnect.edgeconnect_set_zones(
                {"1": {"name": "Voice"}},
                delete_dependencies=True,
                dry_run=False,
                confirm=True,
            ),
            "https://orch.example.com/gms/rest/zones",
            {"deleteDependencies": True},
            {"1": {"name": "Voice"}},
        ),
        (
            lambda: edgeconnect.edgeconnect_set_zone_firewall_status(
                enabled=False,
                dry_run=False,
                confirm=True,
            ),
            "https://orch.example.com/gms/rest/zones/eeEnable",
            {},
            {"enable": False},
        ),
        (
            lambda: edgeconnect.edgeconnect_set_next_zone_id(
                next_id=10,
                dry_run=False,
                confirm=True,
            ),
            "https://orch.example.com/gms/rest/zones/nextId",
            {},
            {"nextId": 10},
        ),
    ],
)
def test_edgeconnect_zone_writes_execute_with_confirm(
    monkeypatch,
    tool_call,
    expected_url,
    expected_params,
    expected_body,
):
    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, headers=None, params=None, json=None):
            called["method"] = method
            called["url"] = url
            called["params"] = params or {}
            called["json"] = json
            return _Resp()

    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(tool_call())

    assert out["status_code"] == 200
    assert called["method"] == "POST"
    assert called["url"] == expected_url
    assert called["params"] == expected_params
    assert called["json"] == expected_body
