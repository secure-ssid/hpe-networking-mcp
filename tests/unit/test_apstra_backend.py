from __future__ import annotations

import asyncio

import pytest

import hpe_networking_mcp.mcp_servers.apstra as apstra


@pytest.fixture(autouse=True)
def _clear_token_cache():
    apstra._TOKEN_CACHE.clear()
    yield
    apstra._TOKEN_CACHE.clear()


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = payload if isinstance(payload, str) else str(payload)

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Records every post()/request() call and dispatches to a test-provided responder."""

    calls: list[tuple[str, str, dict]] = []
    responder = None

    def __init__(self, timeout=None, **_ignored):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, json=None, headers=None):
        _FakeAsyncClient.calls.append(("POST", url, {"json": json, "headers": headers}))
        return _FakeAsyncClient.responder("POST", url, json, headers, None)

    async def request(self, method, url, headers=None, params=None, json=None):
        _FakeAsyncClient.calls.append(
            (method, url, {"headers": headers, "params": params, "json": json})
        )
        return _FakeAsyncClient.responder(method, url, json, headers, params)

    async def get(self, url, headers=None, params=None):
        return await self.request("GET", url, headers=headers, params=params)


def _install_fake_client(monkeypatch, responder):
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.responder = responder
    monkeypatch.setattr(apstra.httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


def _configure_session(monkeypatch):
    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_USERNAME", "admin")
    monkeypatch.setenv("APSTRA_PASSWORD", "admin-pass")
    monkeypatch.delenv("APSTRA_API_TOKEN", raising=False)


def test_apstra_status_unconfigured(monkeypatch):
    monkeypatch.delenv("APSTRA_BASE_URL", raising=False)
    monkeypatch.delenv("APSTRA_USERNAME", raising=False)
    monkeypatch.delenv("APSTRA_PASSWORD", raising=False)
    monkeypatch.delenv("APSTRA_API_TOKEN", raising=False)
    out = apstra.apstra_status()
    assert out["configured"] is False
    assert out["auth_mode"] == "unconfigured"


def test_apstra_status_session_mode(monkeypatch):
    _configure_session(monkeypatch)
    out = apstra.apstra_status()
    assert out["configured"] is True
    assert out["auth_mode"] == "session"
    assert out["has_username"] is True
    assert out["token"]["cached"] is False


def test_apstra_status_static_token_mode(monkeypatch):
    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_API_TOKEN", "static-token")
    monkeypatch.delenv("APSTRA_USERNAME", raising=False)
    monkeypatch.delenv("APSTRA_PASSWORD", raising=False)
    out = apstra.apstra_status()
    assert out["configured"] is True
    assert out["auth_mode"] == "static_token"


def test_apstra_login_uses_aaa_login_first_and_caches(monkeypatch):
    _configure_session(monkeypatch)

    def responder(method, url, json_body, headers, params):
        assert method == "POST"
        assert url == "https://apstra.example.com/api/aaa/login"
        assert json_body == {"username": "admin", "password": "admin-pass"}
        return _FakeResp(201, {"token": "abc123"})

    client = _install_fake_client(monkeypatch, responder)

    out = asyncio.run(apstra.apstra_login())
    assert out["authenticated"] is True
    assert out["login_endpoint"] == "/api/aaa/login"
    assert out["legacy_login_fallback"] is False
    assert len(client.calls) == 1

    # Second call should hit the cache and not perform any network calls.
    out2 = asyncio.run(apstra.apstra_login())
    assert out2["cached"] is True
    assert len(client.calls) == 1


def test_apstra_login_falls_back_to_older_user_login_on_404(monkeypatch):
    _configure_session(monkeypatch)

    def responder(method, url, json_body, headers, params):
        if url.endswith("/api/aaa/login"):
            return _FakeResp(404, {"detail": "not found"})
        assert url.endswith("/api/user/login")
        return _FakeResp(200, "legacy-token")

    client = _install_fake_client(monkeypatch, responder)

    out = asyncio.run(apstra.apstra_login())
    assert out["authenticated"] is True
    assert out["login_endpoint"] == "/api/user/login"
    assert out["legacy_login_fallback"] is True
    assert [call[1] for call in client.calls] == [
        "https://apstra.example.com/api/aaa/login",
        "https://apstra.example.com/api/user/login",
    ]


def test_apstra_get_sends_authtoken_header_not_bearer(monkeypatch):
    _configure_session(monkeypatch)

    def responder(method, url, json_body, headers, params):
        if method == "POST" and url.endswith("/api/aaa/login"):
            return _FakeResp(201, {"token": "tok-1"})
        assert method == "GET"
        assert headers["AuthToken"] == "tok-1"
        assert "Authorization" not in headers
        return _FakeResp(200, {"items": [{"id": "bp1"}]})

    _install_fake_client(monkeypatch, responder)

    out = asyncio.run(apstra.apstra_get("/api/blueprints"))
    assert out["status_code"] == 200
    assert out["auth"]["mode"] == "session"
    assert out["auth"]["login_endpoint"] == "/api/aaa/login"


def test_apstra_get_retries_once_after_401_with_fresh_login(monkeypatch):
    _configure_session(monkeypatch)
    state = {"logins": 0, "requests": 0}

    def responder(method, url, json_body, headers, params):
        if method == "POST" and url.endswith("/api/aaa/login"):
            state["logins"] += 1
            return _FakeResp(201, {"token": f"tok-{state['logins']}"})
        state["requests"] += 1
        if state["requests"] == 1:
            assert headers["AuthToken"] == "tok-1"
            return _FakeResp(401, {"error": "expired"})
        assert headers["AuthToken"] == "tok-2"
        return _FakeResp(200, {"items": []})

    _install_fake_client(monkeypatch, responder)

    out = asyncio.run(apstra.apstra_get("/api/blueprints"))
    assert out["status_code"] == 200
    assert state["logins"] == 2
    assert state["requests"] == 2


def test_apstra_write_dry_run_previews_without_network_calls(monkeypatch):
    _configure_session(monkeypatch)
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    client = _install_fake_client(monkeypatch, lambda *a: pytest.fail("unexpected network call"))

    out = asyncio.run(
        apstra.apstra_write("post", "/api/blueprints/bp1/anomalies", body={"foo": "bar"})
    )
    assert out["dry_run"] is True
    assert out["method"] == "POST"
    assert client.calls == []


def test_apstra_write_blocked_when_read_only(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")
    out = asyncio.run(apstra.apstra_write("post", "/api/blueprints/bp1/anomalies"))
    assert out["status"] == "blocked"


def test_apstra_write_executes_with_authtoken_on_confirm(monkeypatch):
    _configure_session(monkeypatch)
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

    def responder(method, url, json_body, headers, params):
        if method == "POST" and url.endswith("/api/aaa/login"):
            return _FakeResp(201, {"token": "tok-1"})
        assert method == "PATCH"
        assert headers["AuthToken"] == "tok-1"
        return _FakeResp(200, {"ok": True})

    _install_fake_client(monkeypatch, responder)

    out = asyncio.run(
        apstra.apstra_write(
            "patch",
            "/api/blueprints/bp1/anomalies",
            body={"acknowledged": True},
            dry_run=False,
            confirm=True,
        )
    )
    assert out["status_code"] == 200
    assert out["data"] == {"ok": True}


def test_apstra_list_connectivity_templates_uses_endpoint_policies(monkeypatch):
    _configure_session(monkeypatch)

    def responder(method, url, json_body, headers, params):
        if method == "POST" and url.endswith("/api/aaa/login"):
            return _FakeResp(201, {"token": "tok-1"})
        assert url.endswith("/api/blueprints/bp1/endpoint-policies")
        return _FakeResp(200, [{"id": "ct1", "label": "Server-Link"}])

    _install_fake_client(monkeypatch, responder)

    out = asyncio.run(apstra.apstra_list_connectivity_templates("bp1"))
    assert out["connectivity_templates"]["items"] == [{"id": "ct1", "label": "Server-Link"}]


def test_apstra_create_connectivity_template_preview(monkeypatch):
    _configure_session(monkeypatch)
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    client = _install_fake_client(monkeypatch, lambda *a: pytest.fail("unexpected network call"))

    out = asyncio.run(
        apstra.apstra_create_connectivity_template(
            "bp1", {"label": "New-CT", "policy_type": "junos"}
        )
    )
    assert out["dry_run"] is True
    assert out["method"] == "PUT"
    assert out["path"] == "/api/blueprints/bp1/obj-policy-import"
    assert client.calls == []


def test_apstra_set_application_point_assignment_preview(monkeypatch):
    _configure_session(monkeypatch)
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    _install_fake_client(monkeypatch, lambda *a: pytest.fail("unexpected network call"))

    out = asyncio.run(
        apstra.apstra_set_application_point_assignment(
            "bp1", [{"id": "ap1", "policies": [{"policy": "ct1", "used": True}]}]
        )
    )
    assert out["dry_run"] is True
    assert out["method"] == "PATCH"
    assert out["path"] == "/api/blueprints/bp1/obj-policy-batch-apply"
    assert out["json"] == {
        "application_points": [{"id": "ap1", "policies": [{"policy": "ct1", "used": True}]}]
    }


def test_apstra_wait_for_task_reaches_terminal_state(monkeypatch):
    _configure_session(monkeypatch)
    state = {"task_reads": 0}

    def responder(method, url, json_body, headers, params):
        if method == "POST" and url.endswith("/api/aaa/login"):
            return _FakeResp(200, {"token": "tok-1"})
        assert url.endswith("/api/blueprints/bp1/tasks/task1")
        state["task_reads"] += 1
        status = "in_progress" if state["task_reads"] == 1 else "succeeded"
        return _FakeResp(200, {"status": status})

    async def no_sleep(_seconds):
        return None

    _install_fake_client(monkeypatch, responder)
    monkeypatch.setattr(apstra.asyncio, "sleep", no_sleep)

    out = asyncio.run(
        apstra.apstra_wait_for_task(
            "bp1", "task1", timeout_seconds=10, poll_interval_seconds=0.5
        )
    )

    assert out["task"]["status"] == "succeeded"
    assert state["task_reads"] == 2
