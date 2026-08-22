"""Tests for the v0.7 optional-depth curated tools added to clearpass.py."""

from __future__ import annotations

import asyncio
import json

import hpe_networking_mcp.mcp_servers.clearpass as clearpass
from hpe_networking_mcp.mcp_servers.shared import DESTRUCTIVE, IDEMPOTENT_WRITE, READ_ONLY

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


def _make_fake_client(called: dict, response_payload):
    """Return a _FakeAsyncClient class that captures GET and request calls."""

    class _Resp:
        status_code = 200
        text = json.dumps(response_payload)

        def json(self):
            return response_payload

    class _FakeAsyncClient:
        def __init__(self, timeout=None, **_ignored):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            called["method"] = "GET"
            called["url"] = url
            called["headers"] = headers or {}
            called["params"] = params or {}
            return _Resp()

        async def request(self, method, url, headers=None, params=None, json=None):
            called["method"] = method
            called["url"] = url
            called["headers"] = headers or {}
            called["params"] = params or {}
            called["json"] = json
            return _Resp()

    return _FakeAsyncClient


# ---------------------------------------------------------------------------
# Annotation classification
# ---------------------------------------------------------------------------


def test_clearpass_list_access_tracker_sessions_is_read_only():
    tool = clearpass.mcp._tool_manager._tools["clearpass_list_access_tracker_sessions"]
    assert tool.annotations is READ_ONLY


def test_clearpass_disconnect_session_is_destructive():
    tool = clearpass.mcp._tool_manager._tools["clearpass_disconnect_session"]
    assert tool.annotations is DESTRUCTIVE


def test_clearpass_create_guest_is_idempotent_write():
    tool = clearpass.mcp._tool_manager._tools["clearpass_create_guest"]
    assert tool.annotations is IDEMPOTENT_WRITE


def test_clearpass_set_service_enabled_is_idempotent_write():
    tool = clearpass.mcp._tool_manager._tools["clearpass_set_service_enabled"]
    assert tool.annotations is IDEMPOTENT_WRITE


def test_clearpass_get_server_version_is_read_only():
    tool = clearpass.mcp._tool_manager._tools["clearpass_get_server_version"]
    assert tool.annotations is READ_ONLY


# ---------------------------------------------------------------------------
# clearpass_list_access_tracker_sessions
# ---------------------------------------------------------------------------


def test_clearpass_list_access_tracker_sessions_no_filter(monkeypatch):
    """Without a status, no filter param is sent and results are compact/bounded."""
    sessions = [
        {"username": "alice", "auth_status": "ALLOW", "nasipaddress": "10.0.0.1"},
        {"username": "bob", "auth_status": "FAILED", "nasipaddress": "10.0.0.2"},
    ]
    called = {}
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "tok")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _make_fake_client(called, sessions))

    out = asyncio.run(clearpass.clearpass_list_access_tracker_sessions(limit=10))

    assert called["url"] == "https://cp.example.com/api/session"
    assert "filter" not in called["params"]
    assert out["data"]["items"][0]["username"] == "alice"
    assert out["data"]["items"][1]["auth_status"] == "FAILED"


def test_clearpass_list_access_tracker_sessions_with_status_filter(monkeypatch):
    """With a status argument the filter JSON is sent as a query param."""
    called = {}
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "tok")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _make_fake_client(called, []))

    asyncio.run(clearpass.clearpass_list_access_tracker_sessions(status="REJECT", limit=5))

    assert json.loads(called["params"]["filter"]) == {"auth_status": "REJECT"}


def test_clearpass_list_access_tracker_sessions_bounds_large_payload(monkeypatch):
    """The tool must not return more items than limit even with a huge backend payload."""
    big = [{"username": f"u{i}", "auth_status": "ALLOW"} for i in range(200)]
    called = {}
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "tok")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _make_fake_client(called, big))

    out = asyncio.run(clearpass.clearpass_list_access_tracker_sessions(limit=10))

    # The tool passes limit=10 to the server AND bounds the response; result must be <= 10.
    assert len(out["data"]["items"]) <= 10


# ---------------------------------------------------------------------------
# clearpass_get_access_tracker_session
# ---------------------------------------------------------------------------


def test_clearpass_get_access_tracker_session_calls_correct_path(monkeypatch):
    session = {"id": "sess1", "username": "carol", "auth_status": "ALLOW"}
    called = {}
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "tok")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _make_fake_client(called, session))

    out = asyncio.run(clearpass.clearpass_get_access_tracker_session("sess1"))

    assert called["url"] == "https://cp.example.com/api/session/sess1"
    assert out["session"]["username"] == "carol"
    assert "data" not in out


# ---------------------------------------------------------------------------
# clearpass_disconnect_session
# ---------------------------------------------------------------------------


def test_clearpass_disconnect_session_dry_run_preview(monkeypatch):
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "tok")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(clearpass.clearpass_disconnect_session("sess99"))

    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert out["path"] == "/api/session/sess99/disconnect"
    assert "execute_hint" in out


def test_clearpass_disconnect_session_blocked_read_only(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")
    monkeypatch.delenv("HPE_MCP_CLEARPASS_WRITES", raising=False)

    out = asyncio.run(clearpass.clearpass_disconnect_session("sess99"))

    assert out["status"] == "blocked"


def test_clearpass_disconnect_session_requires_confirm(monkeypatch):
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "tok")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(
        clearpass.clearpass_disconnect_session("sess99", dry_run=False, confirm=False)
    )

    assert "error" in out
    assert "confirm=True" in out["error"]


# ---------------------------------------------------------------------------
# clearpass_list_endpoints
# ---------------------------------------------------------------------------


def test_clearpass_list_endpoints_calls_endpoint_path(monkeypatch):
    endpoints = [{"mac_address": "aabbccddeeff", "status": "Known"}]
    called = {}
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "tok")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _make_fake_client(called, endpoints))

    out = asyncio.run(clearpass.clearpass_list_endpoints(limit=5))

    assert called["url"] == "https://cp.example.com/api/endpoint"
    assert out["endpoints"]["items"][0]["mac_address"] == "aabbccddeeff"
    assert "data" not in out


def test_clearpass_list_endpoints_bounds_large_payload(monkeypatch):
    big = [{"mac_address": f"aa{i:010d}", "status": "Known"} for i in range(100)]
    called = {}
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "tok")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _make_fake_client(called, big))

    out = asyncio.run(clearpass.clearpass_list_endpoints(limit=5))

    # The tool passes limit=5 to the server AND bounds the response; result must be <= 5.
    assert len(out["endpoints"]["items"]) <= 5


# ---------------------------------------------------------------------------
# clearpass_list_guests
# ---------------------------------------------------------------------------


def test_clearpass_list_guests_calls_guest_path(monkeypatch):
    guests = [{"username": "visitor1", "email": "v1@example.com"}]
    called = {}
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "tok")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _make_fake_client(called, guests))

    out = asyncio.run(clearpass.clearpass_list_guests(limit=10))

    assert called["url"] == "https://cp.example.com/api/guest"
    assert out["guests"]["items"][0]["username"] == "visitor1"
    assert "data" not in out


# ---------------------------------------------------------------------------
# clearpass_create_guest
# ---------------------------------------------------------------------------


def test_clearpass_create_guest_dry_run_redacts_password(monkeypatch):
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "tok")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(
        clearpass.clearpass_create_guest(
            username="lab-guest",
            password="super-secret",
            role_name="Guest",
        )
    )

    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert out["path"] == "/api/guest"
    # password must be redacted in the preview
    assert out["json"]["password"] == "******"
    assert out["json"]["username"] == "lab-guest"
    assert out["json"]["role_name"] == "Guest"


def test_clearpass_create_guest_blocked_read_only(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")
    monkeypatch.delenv("HPE_MCP_CLEARPASS_WRITES", raising=False)

    out = asyncio.run(clearpass.clearpass_create_guest("lab-guest"))

    assert out["status"] == "blocked"


# ---------------------------------------------------------------------------
# clearpass_list_roles / clearpass_list_enforcement_policies
# ---------------------------------------------------------------------------


def test_clearpass_list_roles_calls_role_path(monkeypatch):
    roles = [{"id": 1, "name": "Employee"}, {"id": 2, "name": "Guest"}]
    called = {}
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "tok")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _make_fake_client(called, roles))

    out = asyncio.run(clearpass.clearpass_list_roles(limit=10))

    assert called["url"] == "https://cp.example.com/api/role"
    assert len(out["roles"]["items"]) == 2
    assert "data" not in out


def test_clearpass_list_enforcement_policies_calls_correct_path(monkeypatch):
    policies = [{"id": 10, "name": "AllowAll"}]
    called = {}
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "tok")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _make_fake_client(called, policies))

    out = asyncio.run(clearpass.clearpass_list_enforcement_policies(limit=10))

    assert called["url"] == "https://cp.example.com/api/enforcement-policy"
    assert out["enforcement_policies"]["items"][0]["name"] == "AllowAll"
    assert "data" not in out


def test_clearpass_get_enforcement_policy_by_name(monkeypatch):
    policy = {"id": 10, "name": "AllowAll", "default_enforcement_profile": "Allow Access"}
    called = {}
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "tok")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _make_fake_client(called, policy))

    out = asyncio.run(clearpass.clearpass_get_enforcement_policy("AllowAll"))

    assert called["url"] == "https://cp.example.com/api/enforcement-policy/name/AllowAll"
    assert out["enforcement_policy"]["name"] == "AllowAll"
    assert "data" not in out


# ---------------------------------------------------------------------------
# clearpass_list_services / clearpass_get_service / clearpass_set_service_enabled
# ---------------------------------------------------------------------------


def test_clearpass_list_services_calls_correct_path(monkeypatch):
    services = [{"id": 5, "name": "802.1X Wired", "enabled": True}]
    called = {}
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "tok")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _make_fake_client(called, services))

    out = asyncio.run(clearpass.clearpass_list_services(limit=10))

    assert called["url"] == "https://cp.example.com/api/config/service"
    assert out["services"]["items"][0]["name"] == "802.1X Wired"


def test_clearpass_get_service_by_name(monkeypatch):
    service = {"id": 5, "name": "802.1X Wired"}
    called = {}
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "tok")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _make_fake_client(called, service))

    out = asyncio.run(clearpass.clearpass_get_service("802.1X Wired"))

    assert called["url"] == "https://cp.example.com/api/config/service/name/802.1X%20Wired"
    assert out["service"]["name"] == "802.1X Wired"


def test_clearpass_set_service_enabled_routes_enable_endpoint(monkeypatch):
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "tok")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(clearpass.clearpass_set_service_enabled("My Service", enabled=True))

    assert out["dry_run"] is True
    assert out["path"] == "/api/config/service/name/My%20Service/enable"
    assert out["method"] == "PATCH"


def test_clearpass_set_service_enabled_routes_disable_endpoint(monkeypatch):
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "tok")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    out = asyncio.run(clearpass.clearpass_set_service_enabled("My Service", enabled=False))

    assert out["dry_run"] is True
    assert out["path"] == "/api/config/service/name/My%20Service/disable"


def test_clearpass_set_service_enabled_blocked_read_only(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")
    monkeypatch.delenv("HPE_MCP_CLEARPASS_WRITES", raising=False)

    out = asyncio.run(clearpass.clearpass_set_service_enabled("svc", enabled=True))

    assert out["status"] == "blocked"


# ---------------------------------------------------------------------------
# clearpass_list_syslog_targets / clearpass_list_syslog_export_filters
# ---------------------------------------------------------------------------


def test_clearpass_list_syslog_targets_calls_correct_path(monkeypatch):
    targets = [{"id": 1, "host_address": "192.168.1.100", "port": 514}]
    called = {}
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "tok")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _make_fake_client(called, targets))

    out = asyncio.run(clearpass.clearpass_list_syslog_targets(limit=10))

    assert called["url"] == "https://cp.example.com/api/syslog-target"
    assert out["syslog_targets"]["items"][0]["port"] == 514


def test_clearpass_list_syslog_export_filters_calls_correct_path(monkeypatch):
    filters = [{"id": 3, "name": "Auth Events", "enabled": True}]
    called = {}
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "tok")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _make_fake_client(called, filters))

    out = asyncio.run(clearpass.clearpass_list_syslog_export_filters(limit=10))

    assert called["url"] == "https://cp.example.com/api/syslog-export-filter"
    assert out["syslog_export_filters"]["items"][0]["name"] == "Auth Events"


# ---------------------------------------------------------------------------
# clearpass_get_server_version / clearpass_list_cluster_servers
# ---------------------------------------------------------------------------


def test_clearpass_get_server_version_calls_correct_path(monkeypatch):
    version_data = {"app_major_version": "6", "app_minor_version": "12", "server_version": "6.12.1"}
    called = {}
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "tok")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _make_fake_client(called, version_data))

    out = asyncio.run(clearpass.clearpass_get_server_version())

    assert called["url"] == "https://cp.example.com/api/server/version"
    assert out["version"]["server_version"] == "6.12.1"
    assert "data" not in out


def test_clearpass_list_cluster_servers_calls_correct_path(monkeypatch):
    servers = [{"uuid": "abc-123", "name": "Publisher", "server_type": "Publisher"}]
    called = {}
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "tok")
    monkeypatch.setattr(clearpass.httpx, "AsyncClient", _make_fake_client(called, servers))

    out = asyncio.run(clearpass.clearpass_list_cluster_servers(limit=10))

    assert called["url"] == "https://cp.example.com/api/cluster/server"
    assert out["cluster_servers"]["items"][0]["name"] == "Publisher"
    assert "data" not in out
