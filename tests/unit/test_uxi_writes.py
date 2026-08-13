"""Tests for UXI guarded writes/assignments (dry_run/confirm, read-write gate)
and the documented 5 req/s outbound throttle.
"""

from __future__ import annotations

import asyncio

import hpe_networking_mcp.mcp_servers.uxi as uxi


class _TokenResp:
    status_code = 200
    text = "{}"

    def json(self):
        return {"access_token": "tok123", "expires_in": 3600}


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.text = "{}"

    def json(self):
        return self._payload


def _fake_client(request_resp=None, calls=None):
    calls = calls if calls is not None else {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, data=None, headers=None, **kwargs):
            calls.setdefault("post_calls", []).append(url)
            return _TokenResp()

        async def request(self, method, url, headers=None, params=None, json=None):
            calls.setdefault("request_calls", []).append(
                {"method": method, "url": url, "headers": headers or {}, "json": json}
            )
            return request_resp

    return _FakeAsyncClient, calls


def _configure_uxi(monkeypatch):
    monkeypatch.setenv("UXI_CLIENT_ID", "client")
    monkeypatch.setenv("UXI_CLIENT_SECRET", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    uxi._TOKEN_CACHE.clear()


def test_uxi_status_reports_rate_limit_and_write_tools(monkeypatch):
    monkeypatch.delenv("UXI_CLIENT_ID", raising=False)
    monkeypatch.delenv("UXI_CLIENT_SECRET", raising=False)

    out = uxi.uxi_status()

    assert out["rate_limit_per_second"] == 5.0
    assert "uxi_create_group" in out["tools"]
    assert "uxi_assign_sensor_to_group" in out["tools"]


def test_uxi_write_blocked_when_read_only(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")

    out = asyncio.run(uxi.uxi_write("POST", "/groups", body={"name": "Lab"}))

    assert out["status"] == "blocked"


def test_uxi_write_rejects_unwritable_path(monkeypatch):
    _configure_uxi(monkeypatch)

    out = asyncio.run(uxi.uxi_write("POST", "/wired-networks", body={}))

    assert "error" in out
    assert "path must be one of" in out["error"]


def test_uxi_create_group_dry_run_previews(monkeypatch):
    _configure_uxi(monkeypatch)

    out = asyncio.run(uxi.uxi_create_group("Lab Sites", parent="root"))

    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert out["path"] == "/groups"
    assert out["json"] == {"name": "Lab Sites", "parentId": "root"}
    assert "execute_hint" in out


def test_uxi_create_group_requires_confirm_when_not_dry_run(monkeypatch):
    _configure_uxi(monkeypatch)

    out = asyncio.run(uxi.uxi_create_group("Lab Sites", dry_run=False, confirm=False))

    assert out["dry_run"] is True
    assert out["error"] == "confirm=True is required when dry_run=False."


def test_uxi_create_group_executes_with_confirm(monkeypatch):
    fake_cls, calls = _fake_client(request_resp=_Resp(201, {"id": "g1", "name": "Lab Sites"}))
    _configure_uxi(monkeypatch)
    monkeypatch.setattr(uxi.httpx, "AsyncClient", fake_cls)

    out = asyncio.run(uxi.uxi_create_group("Lab Sites", dry_run=False, confirm=True))

    assert out["status_code"] == 201
    request_call = calls["request_calls"][0]
    assert request_call["method"] == "POST"
    assert request_call["url"].endswith("/groups")
    assert request_call["json"] == {"name": "Lab Sites"}
    assert request_call["headers"]["Authorization"].startswith("Bearer ")


def test_uxi_update_group_requires_at_least_one_field(monkeypatch):
    _configure_uxi(monkeypatch)

    out = asyncio.run(uxi.uxi_update_group("g1"))

    assert out == {"error": "Provide name to update."}


def test_uxi_update_group_previews_patch_by_id(monkeypatch):
    _configure_uxi(monkeypatch)

    out = asyncio.run(uxi.uxi_update_group("g1", name="Renamed"))

    assert out["method"] == "PATCH"
    assert out["path"] == "/groups/g1"
    assert out["json"] == {"name": "Renamed"}


def test_uxi_delete_group_previews_delete_by_id(monkeypatch):
    _configure_uxi(monkeypatch)

    out = asyncio.run(uxi.uxi_delete_group("g1"))

    assert out["method"] == "DELETE"
    assert out["path"] == "/groups/g1"


def test_uxi_delete_group_rejects_unsafe_id(monkeypatch):
    _configure_uxi(monkeypatch)

    out = asyncio.run(uxi.uxi_delete_group("../etc"))

    assert "error" in out


def test_uxi_update_sensor_requires_nonempty_patch(monkeypatch):
    _configure_uxi(monkeypatch)

    out = asyncio.run(uxi.uxi_update_sensor("s1", {}))

    assert out == {"error": "patch must be a non-empty object"}


def test_uxi_update_sensor_previews_patch_by_id(monkeypatch):
    _configure_uxi(monkeypatch)

    out = asyncio.run(uxi.uxi_update_sensor("s1", {"notes": "Lobby AP"}))

    assert out["method"] == "PATCH"
    assert out["path"] == "/sensors/s1"
    assert out["json"] == {"notes": "Lobby AP"}


def test_uxi_delete_sensor_previews(monkeypatch):
    _configure_uxi(monkeypatch)

    out = asyncio.run(uxi.uxi_delete_sensor("s1"))

    assert "does not expose DELETE" in out["error"]


def test_uxi_update_agent_previews_patch_by_id(monkeypatch):
    _configure_uxi(monkeypatch)

    out = asyncio.run(uxi.uxi_update_agent("a1", {"notes": "Break room"}))

    assert out["method"] == "PATCH"
    assert out["path"] == "/agents/a1"


def test_uxi_delete_agent_previews(monkeypatch):
    _configure_uxi(monkeypatch)

    out = asyncio.run(uxi.uxi_delete_agent("a1"))

    assert out["method"] == "DELETE"
    assert out["path"] == "/agents/a1"


def test_uxi_assign_sensor_to_group_previews_post_body(monkeypatch):
    _configure_uxi(monkeypatch)

    out = asyncio.run(uxi.uxi_assign_sensor_to_group("s1", "g1"))

    assert out["method"] == "POST"
    assert out["path"] == "/sensor-group-assignments"
    assert out["json"] == {"sensorId": "s1", "groupId": "g1"}


def test_uxi_assign_agent_to_group_previews_post_body(monkeypatch):
    _configure_uxi(monkeypatch)

    out = asyncio.run(uxi.uxi_assign_agent_to_group("a1", "g1"))

    assert out["path"] == "/agent-group-assignments"
    assert out["json"] == {"agentId": "a1", "groupId": "g1"}


def test_uxi_assign_network_to_group_previews_post_body(monkeypatch):
    _configure_uxi(monkeypatch)

    out = asyncio.run(uxi.uxi_assign_network_to_group("n1", "g1"))

    assert out["path"] == "/network-group-assignments"
    assert out["json"] == {"networkId": "n1", "groupId": "g1"}


def test_uxi_assign_service_test_to_group_previews_post_body(monkeypatch):
    _configure_uxi(monkeypatch)

    out = asyncio.run(uxi.uxi_assign_service_test_to_group("st1", "g1"))

    assert out["path"] == "/service-test-group-assignments"
    assert out["json"] == {"serviceTestId": "st1", "groupId": "g1"}


def test_uxi_write_preview_redacts_sensitive_body_values(monkeypatch):
    _configure_uxi(monkeypatch)

    out = asyncio.run(
        uxi.uxi_write(
            "PATCH",
            "/groups/g1",
            body={"apiKey": "abc", "name": "Lab"},
        )
    )

    assert out["json"] == {"apiKey": "******", "name": "Lab"}


def test_uxi_rate_limiter_is_shared_5rps_bucket():
    assert uxi._UXI_RATE_LIMITER.rate == 5.0


def test_uxi_write_throttles_outbound_requests(monkeypatch):
    """Every real write call must pass through the shared 5 req/s limiter."""
    calls = {"count": 0}
    # ``acquire`` is the public token-bucket entry point (``_acquire`` remains
    # as a backwards-compatible alias); ``before_call`` dispatches through it.
    original_acquire = uxi._UXI_RATE_LIMITER.acquire

    async def _counting_acquire():
        calls["count"] += 1
        return await original_acquire()

    monkeypatch.setattr(uxi._UXI_RATE_LIMITER, "acquire", _counting_acquire)
    fake_cls, _ = _fake_client(request_resp=_Resp(200, {"ok": True}))
    _configure_uxi(monkeypatch)
    monkeypatch.setattr(uxi.httpx, "AsyncClient", fake_cls)

    asyncio.run(uxi.uxi_create_group("Lab", dry_run=False, confirm=True))

    # Once for the OAuth token fetch, once for the write request itself.
    assert calls["count"] == 2
