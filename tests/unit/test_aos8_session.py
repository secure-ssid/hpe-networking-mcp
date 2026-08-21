"""Tests for AOS8 session-based auth (login/logout, 401 retry, legacy fallback).

Complements the broader AOS8 coverage in `test_optional_product_backends.py`,
which is shared with other optional product backends and focuses on the
existing static-bearer-token read/write tools. This file is dedicated to the
newer session-login flow (`AOS8_USERNAME`/`AOS8_PASSWORD`), export tools, and
the migration-plan tool.
"""

from __future__ import annotations

import asyncio

import pytest

import hpe_networking_mcp.mcp_servers.aos8 as aos8


@pytest.fixture(autouse=True)
def _clear_aos8_session_cache():
    aos8._SESSION_CACHE.clear()
    yield
    aos8._SESSION_CACHE.clear()


class _LoginResp:
    def __init__(self, status_code=200, uidaruba="UID123", status="0", csrf="csrf-token-1"):
        self.status_code = status_code
        self._uidaruba = uidaruba
        self._status = status
        self.headers = {"set-cookie": "SESSION=session-cookie; Path=/; Secure"}
        self.text = "{}"

    def json(self):
        return {
            "_global_result": {
                "UIDARUBA": self._uidaruba,
                "X-CSRF-Token": "csrf-token-1",
                "status": self._status,
            }
        }


class _JsonResp:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.headers = headers or {}
        self.text = "{}"

    def json(self):
        return self._payload


def _fake_client(login_resp=None, get_resp=None, request_resp=None, calls=None):
    calls = calls if calls is not None else {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, params=None, data=None, headers=None, **kwargs):
            calls.setdefault("post_calls", []).append(
                {
                    "url": url,
                    "params": params,
                    "data": data,
                    "headers": headers or {},
                }
            )
            return login_resp

        async def get(self, url, headers=None, params=None):
            calls.setdefault("get_calls", []).append(
                {"url": url, "headers": headers or {}, "params": params or {}}
            )
            return get_resp

        async def request(self, method, url, headers=None, params=None, json=None):
            calls.setdefault("request_calls", []).append(
                {
                    "method": method,
                    "url": url,
                    "headers": headers or {},
                    "params": params or {},
                    "json": json,
                }
            )
            return request_resp

    return _FakeAsyncClient, calls


def test_aos8_status_reports_session_mode_when_username_and_password_set(monkeypatch):
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.delenv("AOS8_API_TOKEN", raising=False)
    monkeypatch.setenv("AOS8_USERNAME", "admin")
    monkeypatch.setenv("AOS8_PASSWORD", "hunter2")

    out = aos8.aos8_status()

    assert out["configured"] is True
    assert out["auth_mode"] == "session"
    assert out["has_username"] is True
    assert out["has_password"] is True
    assert out["session_active"] is False
    assert out["allowed_methods"] == ["GET", "POST"]


def test_aos8_status_reports_legacy_mode_when_only_token_set(monkeypatch):
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.delenv("AOS8_USERNAME", raising=False)
    monkeypatch.delenv("AOS8_PASSWORD", raising=False)

    out = aos8.aos8_status()

    assert out["configured"] is True
    assert out["auth_mode"] == "legacy_static_token"
    assert out["has_legacy_token"] is True


def test_aos8_status_unconfigured_when_nothing_set(monkeypatch):
    monkeypatch.delenv("AOS8_BASE_URL", raising=False)
    monkeypatch.delenv("AOS8_API_TOKEN", raising=False)
    monkeypatch.delenv("AOS8_USERNAME", raising=False)
    monkeypatch.delenv("AOS8_PASSWORD", raising=False)

    out = aos8.aos8_status()

    assert out["configured"] is False
    assert out["auth_mode"] == "unconfigured"


def test_aos8_login_success_caches_uidaruba_and_csrf_token(monkeypatch):
    fake_cls, calls = _fake_client(login_resp=_LoginResp())
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_USERNAME", "admin")
    monkeypatch.setenv("AOS8_PASSWORD", "hunter2")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", fake_cls)

    out = asyncio.run(aos8.aos8_login())

    assert out["status"] == "logged_in"
    assert calls["post_calls"][0]["url"] == "https://mm.example.com/v1/api/login"
    assert calls["post_calls"][0]["params"] is None
    assert calls["post_calls"][0]["data"] == {"username": "admin", "password": "hunter2"}
    entry = aos8._SESSION_CACHE["https://mm.example.com"]
    assert entry["uidaruba"] == "UID123"
    assert entry["csrf_token"] == "csrf-token-1"
    assert entry["session_cookie"] == "SESSION=session-cookie"


def test_aos8_login_sends_client_ip_when_configured(monkeypatch):
    fake_cls, calls = _fake_client(login_resp=_LoginResp())
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_USERNAME", "admin")
    monkeypatch.setenv("AOS8_PASSWORD", "hunter2")
    monkeypatch.setenv("AOS8_CLIENT_IP", "203.0.113.5")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", fake_cls)

    asyncio.run(aos8.aos8_login())

    assert calls["post_calls"][0]["data"]["client_ip"] == "203.0.113.5"


def test_aos8_login_failure_returns_error_and_does_not_cache(monkeypatch):
    fake_cls, _ = _fake_client(login_resp=_LoginResp(status_code=200, uidaruba=None, status="1"))
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_USERNAME", "admin")
    monkeypatch.setenv("AOS8_PASSWORD", "wrong")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", fake_cls)

    out = asyncio.run(aos8.aos8_login())

    assert "error" in out
    assert "https://mm.example.com" not in aos8._SESSION_CACHE


def test_aos8_login_without_credentials_returns_error(monkeypatch):
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.delenv("AOS8_USERNAME", raising=False)
    monkeypatch.delenv("AOS8_PASSWORD", raising=False)
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")

    out = asyncio.run(aos8.aos8_login())

    assert "error" in out


def test_aos8_login_noop_when_already_logged_in(monkeypatch):
    aos8._SESSION_CACHE["https://mm.example.com"] = {
        "uidaruba": "UID999",
        "csrf_token": "csrf",
        "logged_in_at": aos8.time.time(),
        "expires_at": aos8.time.time() + 600,
    }
    fake_cls, calls = _fake_client(login_resp=_LoginResp())
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_USERNAME", "admin")
    monkeypatch.setenv("AOS8_PASSWORD", "hunter2")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", fake_cls)

    out = asyncio.run(aos8.aos8_login())

    assert out["status"] == "already_logged_in"
    assert "post_calls" not in calls


def test_aos8_login_force_relogs_in_even_when_cached(monkeypatch):
    aos8._SESSION_CACHE["https://mm.example.com"] = {
        "uidaruba": "UID999",
        "csrf_token": "csrf",
        "logged_in_at": aos8.time.time(),
        "expires_at": aos8.time.time() + 600,
    }
    fake_cls, calls = _fake_client(login_resp=_LoginResp(uidaruba="UID_NEW"))
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_USERNAME", "admin")
    monkeypatch.setenv("AOS8_PASSWORD", "hunter2")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", fake_cls)

    out = asyncio.run(aos8.aos8_login(force=True))

    assert out["status"] == "logged_in"
    assert aos8._SESSION_CACHE["https://mm.example.com"]["uidaruba"] == "UID_NEW"


def test_aos8_logout_clears_cache_and_calls_logout_endpoint(monkeypatch):
    aos8._SESSION_CACHE["https://mm.example.com"] = {
        "uidaruba": "UID123",
        "csrf_token": "csrf",
        "logged_in_at": aos8.time.time(),
        "expires_at": aos8.time.time() + 600,
    }
    fake_cls, calls = _fake_client(login_resp=_JsonResp())
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", fake_cls)

    out = asyncio.run(aos8.aos8_logout())

    assert out["status"] == "logged_out"
    assert "https://mm.example.com" not in aos8._SESSION_CACHE
    assert calls["post_calls"][0]["url"] == "https://mm.example.com/v1/api/logout"
    assert calls["post_calls"][0]["params"] == {"UIDARUBA": "UID123"}


def test_aos8_logout_when_no_active_session(monkeypatch):
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")

    out = asyncio.run(aos8.aos8_logout())

    assert out["status"] == "no_active_session"


def test_aos8_get_uses_session_uidaruba_param_not_bearer_header(monkeypatch):
    fake_cls, calls = _fake_client(
        login_resp=_LoginResp(),
        get_resp=_JsonResp(payload={"items": [1, 2, 3]}),
    )
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_USERNAME", "admin")
    monkeypatch.setenv("AOS8_PASSWORD", "hunter2")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", fake_cls)

    out = asyncio.run(aos8.aos8_get("/v1/configuration/object"))

    assert out["status_code"] == 200
    get_call = calls["get_calls"][0]
    assert get_call["params"]["UIDARUBA"] == "UID123"
    assert "Authorization" not in get_call["headers"]


def test_aos8_write_post_sends_csrf_token_header_in_session_mode(monkeypatch):
    fake_cls, calls = _fake_client(
        login_resp=_LoginResp(),
        request_resp=_JsonResp(),
    )
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_USERNAME", "admin")
    monkeypatch.setenv("AOS8_PASSWORD", "hunter2")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", fake_cls)

    out = asyncio.run(
        aos8.aos8_write(
            "POST", "/v1/configuration/object", body={"a": 1}, dry_run=False, confirm=True
        )
    )

    assert out["status_code"] == 200
    request_call = calls["request_calls"][0]
    assert request_call["params"]["UIDARUBA"] == "UID123"
    assert request_call["headers"]["X-CSRF-Token"] == "csrf-token-1"


def test_aos8_get_retries_once_after_401_with_fresh_login(monkeypatch):
    aos8._SESSION_CACHE["https://mm.example.com"] = {
        "uidaruba": "STALE",
        "csrf_token": "stale-csrf",
        "logged_in_at": aos8.time.time(),
        "expires_at": aos8.time.time() + 600,
    }
    get_responses = [
        _JsonResp(status_code=401, payload={"error": "expired"}), _JsonResp(payload={"ok": True})
    ]

    calls = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, params=None, **kwargs):
            calls.setdefault("post_calls", []).append(params)
            return _LoginResp(uidaruba="FRESH")

        async def get(self, url, headers=None, params=None):
            calls.setdefault("get_calls", []).append(dict(params or {}))
            return get_responses.pop(0)

    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_USERNAME", "admin")
    monkeypatch.setenv("AOS8_PASSWORD", "hunter2")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(aos8.aos8_get("/v1/configuration/object"))

    assert out["status_code"] == 200
    assert len(calls["get_calls"]) == 2
    assert calls["get_calls"][0]["UIDARUBA"] == "STALE"
    assert calls["get_calls"][1]["UIDARUBA"] == "FRESH"
    assert aos8._SESSION_CACHE["https://mm.example.com"]["uidaruba"] == "FRESH"


def test_aos8_legacy_token_mode_unaffected_by_session_helpers(monkeypatch):
    """With no username/password, behavior must match the original static-token flow."""
    fake_cls, calls = _fake_client(get_resp=_JsonResp(payload={"ok": True}))
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.delenv("AOS8_USERNAME", raising=False)
    monkeypatch.delenv("AOS8_PASSWORD", raising=False)
    monkeypatch.setattr(aos8.httpx, "AsyncClient", fake_cls)

    out = asyncio.run(aos8.aos8_get("/v1/configuration/object"))

    assert out["status_code"] == 200
    get_call = calls["get_calls"][0]
    assert "UIDARUBA" not in get_call["params"]
    assert get_call["headers"]["Authorization"].startswith("Bearer ")
