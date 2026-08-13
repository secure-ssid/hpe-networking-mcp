from __future__ import annotations

import asyncio
import base64
import json

import hpe_networking_mcp.mcp_servers.edgeconnect as edgeconnect
from hpe_networking_mcp.mcp_servers.openapi_gen import manifest_operation_count
from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import load_manifest


class _Response:
    def __init__(self, payload=None, *, content=b"{}", content_type="application/json"):
        self.status_code = 200
        self._payload = payload
        self.content = content
        self.text = content.decode(errors="replace")
        self.headers = {"content-type": content_type}

    def json(self):
        if self._payload is None:
            raise ValueError
        return self._payload


def _fake_http(monkeypatch, captured, response):
    class Client:
        def __init__(self, timeout=None):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, **kwargs):
            captured.update(method=method, url=url, kwargs=kwargs)
            return response

    monkeypatch.setattr(edgeconnect.httpx, "AsyncClient", Client)


def _configure(monkeypatch):
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "secret")
    monkeypatch.delenv("EDGECONNECT_AUTH_HEADER", raising=False)
    monkeypatch.delenv("EDGECONNECT_ALLOW_LEGACY_API", raising=False)


def test_edgeconnect_manifest_count_registration_and_capabilities():
    manifest = load_manifest("edgeconnect")
    assert manifest_operation_count("edgeconnect") == 1216
    assert len(edgeconnect.GENERATED_EDGECONNECT_TOOLS) == 1216
    assert manifest["source"]["sha256"] == (
        "8f7d90cbd7777e3fac0dc2458249174068f4c373b400d1224f3c3dcc77e34c46"
    )
    assert manifest["reviewed_capability_counts"] == {
        "read": 652,
        "diagnostic": 121,
        "write": 357,
        "destructive": 86,
    }


def test_generated_read_bypasses_legacy_gate_and_injects_source_and_auth(monkeypatch):
    _configure(monkeypatch)
    captured = {}
    _fake_http(monkeypatch, captured, _Response({"items": [1, 2, 3]}))

    fn = edgeconnect.mcp._tool_manager._tools["edgeconnect_get_ac_ls"].fn
    out = asyncio.run(fn(ne_pk="10.NE", cached=False))

    assert out["status_code"] == 200
    assert captured["url"] == "https://orch.example.com/gms/rest/acls"
    assert captured["kwargs"]["params"] == {
        "nePk": "10.NE",
        "cached": False,
        "source": "menu_rest_apis_id",
    }
    headers = captured["kwargs"]["headers"]
    assert headers["X-Auth-Token"] == "secret"
    assert "Authorization" not in headers


def test_generated_diagnostic_post_executes_without_write_gate_or_guard_args(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.delenv("HPE_MCP_EDGECONNECT_WRITES", raising=False)
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")
    captured = {}
    _fake_http(monkeypatch, captured, _Response({"results": [{"value": 1}]}))
    tool = edgeconnect.mcp._tool_manager._tools["edgeconnect_get_aggregate_boost_stats"]

    assert "dry_run" not in tool.parameters["properties"]
    assert "confirm" not in tool.parameters["properties"]
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is False
    out = asyncio.run(
        tool.fn(
            start_time=1,
            end_time=2,
            granularity="minute",
            body={"nePks": ["10.NE"]},
        )
    )

    assert out["status_code"] == 200
    assert captured["method"] == "POST"
    assert captured["kwargs"]["json"] == {"nePks": ["10.NE"]}


def test_side_effecting_get_is_guarded_and_defaults_to_dry_run(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("HPE_MCP_EDGECONNECT_WRITES", "1")
    fn = edgeconnect.mcp._tool_manager._tools["edgeconnect_idle_time415"].fn

    out = asyncio.run(fn())

    assert out["dry_run"] is True
    assert out["method"] == "GET"
    assert out["path"] == "/idle/clear"
    assert out["params"]["source"] == "menu_rest_apis_id"

    cancel = edgeconnect.mcp._tool_manager._tools[
        "edgeconnect_appliance_cp_ustats_cancel_fetch_get"
    ].fn
    out = asyncio.run(cancel(ne_pk="10.NE"))
    assert out["dry_run"] is True
    assert out["path"] == "/appliance/cpustat/historical/cancelfetch"


def test_generated_request_supports_form_multipart_and_text(monkeypatch):
    _configure(monkeypatch)
    captured = {}
    _fake_http(monkeypatch, captured, _Response({"ok": True}))

    asyncio.run(
        edgeconnect._edgeconnect_generated_request(
            "POST", "/form", {}, {}, {"a": "b"}, "application/x-www-form-urlencoded"
        )
    )
    assert captured["kwargs"]["data"] == {"a": "b"}

    asyncio.run(
        edgeconnect._edgeconnect_generated_request(
            "POST", "/upload", {}, {}, {"file": b"abc"}, "multipart/form-data"
        )
    )
    assert captured["kwargs"]["files"]["file"] == ("file", b"abc", "application/octet-stream")

    asyncio.run(
        edgeconnect._edgeconnect_generated_request(
            "POST",
            "/upload",
            {},
            {},
            {
                "qqfile": {
                    "filename": "logo.png",
                    "content_base64": base64.b64encode(b"png").decode(),
                    "content_type": "image/png",
                }
            },
            "multipart/form-data",
        )
    )
    assert captured["kwargs"]["files"]["qqfile"] == ("logo.png", b"png", "image/png")

    asyncio.run(
        edgeconnect._edgeconnect_generated_request(
            "POST", "/text", {}, {}, "hello", "text/plain"
        )
    )
    assert captured["kwargs"]["content"] == "hello"
    assert captured["kwargs"]["headers"]["Content-Type"] == "text/plain"


def test_generated_auth_cannot_be_shadowed_and_responses_are_bounded(monkeypatch):
    _configure(monkeypatch)
    captured = {}
    payload = {"value": "x" * 140_000}
    _fake_http(monkeypatch, captured, _Response(payload, content=json.dumps(payload).encode()))

    out = asyncio.run(
        edgeconnect._edgeconnect_generated_request(
            "GET",
            "/large",
            {},
            {"Authorization": "attacker", "X-Auth-Token": "attacker", "X-Trace": "ok"},
        )
    )

    assert captured["kwargs"]["headers"]["X-Auth-Token"] == "secret"
    assert "Authorization" not in captured["kwargs"]["headers"]
    assert captured["kwargs"]["headers"]["X-Trace"] == "ok"
    assert out["data"]["truncated"] is True
    assert out["data"]["content_type"] == "application/json"


def test_generated_authorization_mode_uses_bearer_token(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("EDGECONNECT_AUTH_HEADER", "Authorization")
    captured = {}
    _fake_http(monkeypatch, captured, _Response({"ok": True}))

    asyncio.run(edgeconnect._edgeconnect_generated_request("GET", "/status", {}, {}))

    assert captured["kwargs"]["headers"]["Authorization"] == "Bearer secret"
    assert "X-Auth-Token" not in captured["kwargs"]["headers"]


def test_sdwan_ai_feedback_uses_separate_session_authorization(monkeypatch):
    _configure(monkeypatch)
    captured = {}
    _fake_http(monkeypatch, captured, _Response({"ok": True}))

    missing = asyncio.run(
        edgeconnect._edgeconnect_generated_request(
            "POST",
            "/sdwanai/feedback",
            {},
            {},
            {"msg_id": "1", "feedback_type": "positive"},
        )
    )
    assert "EDGECONNECT_AI_SESSION_AUTHORIZATION" in missing["error"]

    monkeypatch.setenv(
        "EDGECONNECT_AI_SESSION_AUTHORIZATION", "Bearer active-session"
    )
    out = asyncio.run(
        edgeconnect._edgeconnect_generated_request(
            "POST",
            "/sdwanai/feedback",
            {},
            {},
            {"msg_id": "1", "feedback_type": "positive"},
        )
    )

    assert out["status_code"] == 200
    assert captured["kwargs"]["headers"]["Authorization"] == "Bearer active-session"
