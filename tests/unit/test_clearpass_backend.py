from __future__ import annotations

import asyncio
import json

import hpe_networking_mcp.mcp_servers.clearpass as clearpass


def test_clearpass_status_unconfigured(monkeypatch):
    monkeypatch.delenv("CLEARPASS_BASE_URL", raising=False)
    monkeypatch.delenv("CLEARPASS_API_TOKEN", raising=False)
    out = clearpass.clearpass_status()
    assert out["configured"] is False
    assert out["has_token"] is False


def test_clearpass_get_rejects_non_api_path(monkeypatch):
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "secret")
    out = asyncio.run(clearpass.clearpass_get("/bad/path"))
    assert "error" in out
    assert "/api/*" in out["error"]


def test_clearpass_get_rejects_dot_segment_bypass(monkeypatch):
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "secret")
    out = asyncio.run(clearpass.clearpass_get("/api/../admin"))
    assert "error" in out
    assert "dot segments" in out["error"]


def test_clearpass_get_rejects_double_encoded_dot_segment_bypass(monkeypatch):
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "secret")
    out = asyncio.run(clearpass.clearpass_get("/api/%252e%252e/admin"))
    assert "error" in out
    assert "double-encoded" in out["error"]


def test_clearpass_get_calls_httpx(monkeypatch):
    class _Resp:
        status_code = 200
        text = '{"ok":true}'

        def json(self):
            return {"ok": True}

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

    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "secret")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(clearpass.clearpass_get("/api/session", {"limit": 1}))
    assert out["status_code"] == 200
    assert out["data"] == {"ok": True}
    assert called["url"] == "https://cp.example.com/api/session"
    assert called["headers"]["Authorization"] == "Bearer secret"


def test_clearpass_get_bounds_list_payloads(monkeypatch):
    class _Resp:
        status_code = 200
        text = '[{"id":1},{"id":2},{"id":3}]'

        def json(self):
            return [{"id": 1}, {"id": 2}, {"id": 3}]

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            return _Resp()

    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "secret")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(clearpass.clearpass_get("/api/session", limit=2))

    assert out["data"]["items"] == [{"id": 1}, {"id": 2}]
    assert out["data"]["_pagination"] == {
        "offset": 0,
        "limit": 2,
        "total": 3,
        "truncated": True,
    }


def test_clearpass_get_endpoint_by_mac_normalizes_and_compacts(monkeypatch):
    class _Resp:
        status_code = 200
        text = '{"id":1}'

        def json(self):
            return {
                "id": 1,
                "mac_address": "001122334455",
                "profile_name": "Printer",
                "status": "Known",
                "large_field": "omitted",
            }

    called = {}

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

    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "secret")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(clearpass.clearpass_get_endpoint_by_mac("00:11-22.33:44:55"))

    assert called["url"] == "https://cp.example.com/api/endpoint/mac-address/001122334455"
    assert out["normalized_mac"] == "001122334455"
    assert out["endpoint"] == {
        "id": 1,
        "mac_address": "001122334455",
        "status": "Known",
        "profile_name": "Printer",
    }


def test_clearpass_list_auth_failures_filters_and_compacts(monkeypatch):
    class _Resp:
        status_code = 200
        text = '[{"username":"bob"}]'

        def json(self):
            return [
                {
                    "username": "bob",
                    "calling_station_id": "00-11-22-33-44-55",
                    "nasipaddress": "192.0.2.10",
                    "auth_status": "FAILED",
                    "error_message": "bad password",
                    "debug_blob": "omitted",
                }
            ]

    called = {}

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

    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "secret")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(clearpass.clearpass_list_auth_failures(limit=10, offset=5))

    assert called["url"] == "https://cp.example.com/api/session"
    assert json.loads(called["params"]["filter"]) == {"auth_status": "FAILED"}
    assert called["params"]["offset"] == 5
    assert out["data"]["items"] == [
        {
            "username": "bob",
            "calling_station_id": "00-11-22-33-44-55",
            "nasipaddress": "192.0.2.10",
            "auth_status": "FAILED",
            "reason": "bad password",
        }
    ]
    assert out["data"]["server_offset"] == 5


def test_clearpass_get_network_device_by_name_compacts(monkeypatch):
    class _Resp:
        status_code = 200
        text = '{"id":7}'

        def json(self):
            return {
                "id": 7,
                "name": "Branch Switch",
                "ip_address": "192.0.2.20",
                "status": "Enabled",
                "secret": "omitted",
            }

    called = {}

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

    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "secret")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(clearpass.clearpass_get_network_device(name="Branch Switch"))

    assert called["url"] == "https://cp.example.com/api/network-device/name/Branch%20Switch"
    assert out["network_device"] == {
        "id": 7,
        "name": "Branch Switch",
        "ip_address": "192.0.2.20",
        "status": "Enabled",
    }


def test_clearpass_find_guest_by_email_uses_filter(monkeypatch):
    class _Resp:
        status_code = 200
        text = '{"results":[{"username":"guest"}]}'

        def json(self):
            return {
                "results": [
                    {
                        "username": "guest",
                        "email": "guest@example.com",
                        "visitor_name": "Guest User",
                        "password": "omitted",
                    }
                ]
            }

    called = {}

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

    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "secret")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(clearpass.clearpass_find_guest("guest@example.com", field="email"))

    assert called["url"] == "https://cp.example.com/api/guest"
    assert json.loads(called["params"]["filter"]) == {"email": "guest@example.com"}
    assert out["guests"]["items"] == [
        {
            "username": "guest",
            "email": "guest@example.com",
            "visitor_name": "Guest User",
        }
    ]


def test_clearpass_write_dry_run_previews_request(monkeypatch):
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(
        clearpass.clearpass_write(
            "patch",
            "/api/endpoint/mac-address/001122334455",
            body={"attributes": {"Lab": "true"}},
        )
    )

    assert out["dry_run"] is True
    assert out["method"] == "PATCH"
    assert out["url"] == "https://cp.example.com/api/endpoint/mac-address/001122334455"
    assert out["json"] == {"attributes": {"Lab": "true"}}
    assert "execute_hint" in out


def test_clearpass_write_blocks_when_product_access_read_only(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")
    monkeypatch.delenv("HPE_MCP_CLEARPASS_WRITES", raising=False)

    out = asyncio.run(
        clearpass.clearpass_write(
            "patch",
            "/api/endpoint/mac-address/001122334455",
            body={"attributes": {"Lab": "true"}},
        )
    )

    assert out["status"] == "blocked"
    assert "HPE_MCP_PRODUCT_ACCESS=read-only" in out["error"]


def test_clearpass_write_preview_redacts_sensitive_values(monkeypatch):
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(
        clearpass.clearpass_write(
            "patch",
            "/api/guest/username/lab-user",
            params={"api_key": "abc", "reason": "lab"},
            body={"password": "guest-pass", "secretKey": "abc", "enabled": True},
        )
    )

    assert out["params"] == {"api_key": "******", "reason": "lab"}
    assert out["json"] == {"password": "******", "secretKey": "******", "enabled": True}


def test_clearpass_write_requires_confirm_when_not_dry_run(monkeypatch):
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(
        clearpass.clearpass_write(
            "delete",
            "/api/guest/username/lab-user",
            dry_run=False,
        )
    )

    assert out["dry_run"] is True
    assert out["error"] == "confirm=True is required when dry_run=False."


def test_clearpass_write_executes_with_confirm(monkeypatch):
    class _Resp:
        status_code = 202
        text = '{"ok":true}'

        def json(self):
            return {"ok": True}

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

    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(
        clearpass.clearpass_write(
            "PATCH",
            "/api/guest/username/lab-user",
            params={"change_of_authorization": "false"},
            body={"enabled": False},
            dry_run=False,
            confirm=True,
        )
    )

    assert out["status_code"] == 202
    assert out["data"] == {"ok": True}
    assert called["method"] == "PATCH"
    assert called["url"] == "https://cp.example.com/api/guest/username/lab-user"
    assert called["headers"]["Authorization"] == "Bearer secret"
    assert called["params"] == {"change_of_authorization": "false"}
    assert called["json"] == {"enabled": False}


def test_clearpass_update_endpoint_attributes_builds_patch_preview(monkeypatch):
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(
        clearpass.clearpass_update_endpoint_attributes(
            "00:11:22:33:44:55",
            {"Lab-Isolated": "true"},
            change_of_authorization=True,
        )
    )

    assert out["dry_run"] is True
    assert out["normalized_mac"] == "001122334455"
    assert out["method"] == "PATCH"
    assert out["path"] == "/api/endpoint/mac-address/001122334455"
    assert out["params"] == {"change_of_authorization": "true"}
    assert out["json"] == {"attributes": {"Lab-Isolated": "true"}}


def test_clearpass_set_guest_enabled_requires_one_identifier(monkeypatch):
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "secret")

    out = asyncio.run(clearpass.clearpass_set_guest_enabled(False))

    assert out == {"error": "Provide exactly one of username or guest_id."}


def test_clearpass_delete_guest_by_username_previews(monkeypatch):
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(clearpass.clearpass_delete_guest(username="lab user"))

    assert out["dry_run"] is True
    assert out["method"] == "DELETE"
    assert out["path"] == "/api/guest/username/lab%20user"


def test_clearpass_documented_insight_and_onguard_paths(monkeypatch):
    class _Resp:
        status_code = 200
        text = "{}"

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    calls = []

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            calls.append((url, params))
            if "/insight/" in url:
                return _Resp({"mac_address": "001122334455", "username": "lab"})
            return _Resp([{"host_mac": "001122334455", "status": "healthy"}])

    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "secret")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _FakeAsyncClient)

    insight = asyncio.run(clearpass.clearpass_get_insight_endpoint("00:11:22:33:44:55"))
    activity = asyncio.run(clearpass.clearpass_list_onguard_activity(limit=10))
    activity_by_mac = asyncio.run(
        clearpass.clearpass_get_onguard_activity_by_mac("00:11:22:33:44:55")
    )

    assert insight["normalized_mac"] == "001122334455"
    assert insight["endpoint"]["username"] == "lab"
    assert activity["activity"]["items"][0]["status"] == "healthy"
    assert activity_by_mac["normalized_mac"] == "001122334455"
    assert calls[0][0].endswith("/api/insight/endpoint/mac/001122334455")
    assert calls[1][0].endswith("/api/onguard-activity")
    assert calls[2][0].endswith("/api/onguard-activity/host_mac/001122334455")
