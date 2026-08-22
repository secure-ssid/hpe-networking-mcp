from __future__ import annotations

import asyncio

import hpe_networking_mcp.mcp_servers.mist as mist


def test_mist_status_unconfigured(monkeypatch):
    monkeypatch.delenv("MIST_API_TOKEN", raising=False)
    monkeypatch.delenv("MIST_HOST", raising=False)
    out = mist.mist_status()
    assert out["configured"] is False
    assert out["host"] == "https://api.mist.com"
    assert out["has_token"] is False


def test_mist_get_rejects_non_api_v1_path(monkeypatch):
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    out = asyncio.run(mist.mist_get("/bad/path"))
    assert "error" in out
    assert "/api/v1/*" in out["error"]


def test_mist_get_rejects_encoded_dot_segment_bypass(monkeypatch):
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    out = asyncio.run(mist.mist_get("/api/v1/%2e%2e/admin"))
    assert "error" in out
    assert "encoded dot" in out["error"]


def test_mist_get_rejects_double_encoded_dot_segment_bypass(monkeypatch):
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    out = asyncio.run(mist.mist_get("/api/v1/%252e%252e/admin"))
    assert "error" in out
    assert "double-encoded" in out["error"]


def test_mist_get_calls_httpx(monkeypatch):
    class _Resp:
        status_code = 200
        text = '{"ok":true}'

        def json(self):
            return {"ok": True}

    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None, **_ignored):
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

    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    monkeypatch.setattr(mist.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(mist.mist_get("/api/v1/self", {"limit": 1}))
    assert out["status_code"] == 200
    assert out["data"] == {"ok": True}
    assert called["url"] == "https://api.mist.com/api/v1/self"
    assert called["headers"]["Authorization"] == "Token secret"


def test_mist_get_bounds_nested_list_payloads(monkeypatch):
    class _Resp:
        status_code = 200
        text = '{"results":[1,2,3],"ok":true}'

        def json(self):
            return {"results": [1, 2, 3], "ok": True}

    class _FakeAsyncClient:
        def __init__(self, timeout=None, **_ignored):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            return _Resp()

    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    monkeypatch.setattr(mist.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(mist.mist_get("/api/v1/sites", limit=2, offset=1))

    assert out["data"] == {
        "results": [2, 3],
        "ok": True,
        "_pagination": {
            "offset": 1,
            "limit": 2,
            "total": 3,
            "truncated": False,
            "list_key": "results",
        },
    }


def test_mist_list_sites_compacts_and_pages(monkeypatch):
    class _Resp:
        status_code = 200
        text = '[{"id":"site1"}]'

        def json(self):
            return [
                {
                    "id": "site1",
                    "name": "HQ",
                    "timezone": "America/Chicago",
                    "country_code": "US",
                    "large_field": "omitted",
                }
            ]

    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None, **_ignored):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    monkeypatch.setattr(mist.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(mist.mist_list_sites("org1", limit=25, page=2))

    assert called["url"] == "https://api.mist.com/api/v1/orgs/org1/sites"
    assert called["params"] == {"limit": 25, "page": 2}
    assert out["sites"]["items"] == [
        {
            "id": "site1",
            "name": "HQ",
            "timezone": "America/Chicago",
            "country_code": "US",
        }
    ]
    assert out["sites"]["server_page"] == 2


def test_mist_get_client_uses_stats_endpoint_and_compacts(monkeypatch):
    class _Resp:
        status_code = 200
        text = '{"mac":"001122334455"}'

        def json(self):
            return {
                "mac": "001122334455",
                "hostname": "phone",
                "ap_name": "ap-1",
                "ssid": "Corp",
                "rssi": -55,
                "snr": 32,
                "debug": "omitted",
            }

    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None, **_ignored):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            return _Resp()

    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    monkeypatch.setattr(mist.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(mist.mist_get_client("site1", "00:11-22.33:44:55"))

    assert called["url"] == "https://api.mist.com/api/v1/sites/site1/stats/clients/001122334455"
    assert out["normalized_mac"] == "001122334455"
    assert out["client"] == {
        "mac": "001122334455",
        "hostname": "phone",
        "ap_name": "ap-1",
        "ssid": "Corp",
        "rssi": -55,
        "snr": 32,
    }


def test_mist_list_wlans_compacts(monkeypatch):
    class _Resp:
        status_code = 200
        text = '[{"id":"wlan1"}]'

        def json(self):
            return [
                {
                    "id": "wlan1",
                    "name": "Corp WLAN",
                    "ssid": "Corp",
                    "enabled": True,
                    "vlan_id": 10,
                    "raw": "omitted",
                }
            ]

    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None, **_ignored):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    monkeypatch.setattr(mist.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(mist.mist_list_wlans("site1", limit=50, page=3))

    assert called["url"] == "https://api.mist.com/api/v1/sites/site1/wlans"
    assert called["params"] == {"limit": 50, "page": 3}
    assert out["wlans"]["items"] == [
        {
            "id": "wlan1",
            "name": "Corp WLAN",
            "ssid": "Corp",
            "enabled": True,
            "vlan_id": 10,
        }
    ]


def test_mist_list_alarms_strips_none_params_and_compacts(monkeypatch):
    class _Resp:
        status_code = 200
        text = '{"results":[{"id":"alarm1"}]}'

        def json(self):
            return {
                "results": [
                    {
                        "id": "alarm1",
                        "type": "bad_cable",
                        "group": "marvis",
                        "severity": "warn",
                        "timestamp": 123,
                        "details": "omitted",
                    }
                ]
            }

    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None, **_ignored):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["url"] = url
            called["params"] = params or {}
            return _Resp()

    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    monkeypatch.setattr(mist.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(mist.mist_list_alarms("site1", severity="warn"))

    assert called["url"] == "https://api.mist.com/api/v1/sites/site1/alarms/search"
    assert called["params"] == {
        "severity": "warn",
        "limit": 100,
        "duration": "1d",
        "sort": "-timestamp",
    }
    assert out["alarms"]["items"] == [
        {
            "id": "alarm1",
            "type": "bad_cable",
            "group": "marvis",
            "severity": "warn",
            "timestamp": 123,
        }
    ]


def test_mist_write_dry_run_previews_request(monkeypatch):
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(
        mist.mist_write(
            "post",
            "/api/v1/sites/site1/alarms/alarm1/ack",
            body={"note": "lab ack"},
        )
    )

    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert out["url"] == "https://api.mist.com/api/v1/sites/site1/alarms/alarm1/ack"
    assert out["json"] == {"note": "lab ack"}
    assert "execute_hint" in out


def test_mist_write_blocks_when_product_access_read_only(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")
    # Platform override wins over shared product access; clear it so this
    # test exercises the shared read-only fallback path.
    monkeypatch.delenv("HPE_MCP_MIST_WRITES", raising=False)

    out = asyncio.run(
        mist.mist_write(
            "post",
            "/api/v1/sites/site1/alarms/alarm1/ack",
            body={"note": "lab ack"},
        )
    )

    assert out["status"] == "blocked"
    assert "HPE_MCP_PRODUCT_ACCESS=read-only" in out["error"]


def test_mist_write_preview_redacts_sensitive_values(monkeypatch):
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(
        mist.mist_write(
            "put",
            "/api/v1/sites/site1/wlans/wlan1",
            params={"apikey": "abc", "limit": 1},
            body={"psk": "lab-secret", "ssid": "Lab"},
        )
    )

    assert out["params"] == {"apikey": "******", "limit": 1}
    assert out["json"] == {"psk": "******", "ssid": "Lab"}


def test_mist_write_requires_confirm_when_not_dry_run(monkeypatch):
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(
        mist.mist_write(
            "delete",
            "/api/v1/sites/site1/wlans/wlan1",
            dry_run=False,
        )
    )

    assert out["dry_run"] is True
    assert out["error"] == "confirm=True is required when dry_run=False."


def test_mist_write_executes_with_confirm(monkeypatch):
    class _Resp:
        status_code = 200
        text = '{"ok":true}'

        def json(self):
            return {"ok": True}

    called = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None, **_ignored):
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

    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(mist.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(
        mist.mist_write(
            "POST",
            "/api/v1/sites/site1/alarms/alarm1/ack",
            body={"note": "lab ack"},
            dry_run=False,
            confirm=True,
        )
    )

    assert out["status_code"] == 200
    assert out["data"] == {"ok": True}
    assert called["method"] == "POST"
    assert called["url"] == "https://api.mist.com/api/v1/sites/site1/alarms/alarm1/ack"
    assert called["headers"]["Authorization"] == "Token secret"
    assert called["json"] == {"note": "lab ack"}


def test_mist_ack_alarm_builds_preview(monkeypatch):
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(mist.mist_ack_alarm("site 1", "alarm 1", note="checked"))

    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert out["path"] == "/api/v1/sites/site%201/alarms/alarm%201/ack"
    assert out["json"] == {"note": "checked"}


def test_mist_unack_alarm_builds_preview(monkeypatch):
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(mist.mist_unack_alarm("site1", "alarm1"))

    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert out["path"] == "/api/v1/sites/site1/alarms/alarm1/unack"
    assert out["json"] is None


def test_mist_delete_wlan_builds_preview(monkeypatch):
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(mist.mist_delete_wlan("site1", "wlan1"))

    assert out["dry_run"] is True
    assert out["method"] == "DELETE"
    assert out["path"] == "/api/v1/sites/site1/wlans/wlan1"
