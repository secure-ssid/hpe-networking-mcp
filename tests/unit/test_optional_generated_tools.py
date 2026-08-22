"""Unit tests for the direct generated OpenAPI/derived tool surface of the four
optional product backends: ClearPass, ArubaOS 8, UXI, and Apstra.

Each backend registers its committed manifest operations as typed MCPServer tools
via the shared ``hpe_networking_mcp.mcp_servers.openapi_gen`` foundation. These tests cover, per
platform: the deterministic operation count, direct registration on the backend
server, auth-parameter isolation, request-type dispatch (read vs gated write),
response bounding, and the write gate (blocked by default; dry-run/confirm).
"""

from __future__ import annotations

import asyncio
import inspect

import hpe_networking_mcp.mcp_servers.aos8 as aos8
import hpe_networking_mcp.mcp_servers.apstra as apstra
import hpe_networking_mcp.mcp_servers.clearpass as clearpass
import hpe_networking_mcp.mcp_servers.edgeconnect as edgeconnect
import hpe_networking_mcp.mcp_servers.mist as mist
import hpe_networking_mcp.mcp_servers.uxi as uxi
from hpe_networking_mcp.mcp_servers.openapi_gen import manifest_operation_count

# ---------------------------------------------------------------------------
# Shared fake httpx client
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {"ok": True}
        self.status_code = status_code
        self.text = "{}"
        self.headers = {}
        self.content = b"{}"

    def json(self):
        return self._payload


def _fake_httpx(monkeypatch, module, captured, payload=None):
    class FakeClient:
        def __init__(self, timeout=None, **_ignored):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def request(self, method, url, headers=None, params=None, **kw):
            captured.update(
                method=method, url=url, headers=headers or {}, params=params or {}, kw=kw
            )
            return _Resp(payload)

        async def get(self, url, headers=None, params=None, **kw):
            captured.update(
                method="GET", url=url, headers=headers or {}, params=params or {}, kw=kw
            )
            return _Resp(payload)

    monkeypatch.setattr(module.httpx, "AsyncClient", FakeClient)


def _tool(module, name):
    return module.mcp._tool_manager._tools[name]


def _find_tool(module, prefix, names=None):
    """Find a registered tool whose name starts with ``prefix`` (handles the
    deterministic ``_g<digest>`` suffix the runtime adds on curated collisions).

    ``names`` optionally restricts the search to a set of tool names (e.g. the
    platform's generated-tools list) so a curated tool with the same base name
    is not matched first.
    """
    candidates = names if names is not None else module.mcp._tool_manager._tools.keys()
    for name in candidates:
        if name.startswith(prefix):
            return name, module.mcp._tool_manager._tools[name]
    raise KeyError(prefix)


def _props(tool):
    return (tool.parameters.get("properties") or {})


# ---------------------------------------------------------------------------
# Deterministic counts + direct registration
# ---------------------------------------------------------------------------

def test_manifest_counts_are_deterministic():
    assert manifest_operation_count("clearpass") == 816
    assert manifest_operation_count("uxi") == 25
    assert manifest_operation_count("aos8") == 258
    assert manifest_operation_count("apstra") == 135


def test_generated_tools_registered_on_each_backend():
    assert len(clearpass.GENERATED_CLEARPASS_TOOLS) == 815
    assert len(uxi.GENERATED_UXI_TOOLS) == 25
    assert len(aos8.GENERATED_AOS8_TOOLS) == 258
    # Apstra: 135 reviewed operations (v0.7 added resource pools, device/rack
    # profiles, system agents, telemetry, and blueprint-scoped IBA), the 2 Auth
    # login endpoints are not tools.
    assert len(apstra.GENERATED_APSTRA_TOOLS) == 133
    assert "clearpass_token_endpoint_post" not in clearpass.mcp._tool_manager._tools

    assert "clearpass_certificate_chain_by_cert_id_chain_get" in clearpass.mcp._tool_manager._tools
    assert "uxi_get_sensor_status" in uxi.mcp._tool_manager._tools
    assert "aos8_get_object_aaa_prof" in aos8.mcp._tool_manager._tools
    assert "apstra_list_blueprint_anomalies" in apstra.mcp._tool_manager._tools
    # Curated tools still present alongside generated ones.
    assert "clearpass_status" in clearpass.mcp._tool_manager._tools
    assert "aos8_show_command" in aos8.mcp._tool_manager._tools


def test_apstra_login_endpoints_not_registered():
    names = apstra.mcp._tool_manager._tools
    assert not any("login" in n.lower() for n in apstra.GENERATED_APSTRA_TOOLS)
    assert not any("aaa_login" in n.lower() for n in names)


def test_custom_generated_read_executors_accept_request_bodies():
    executors = (
        aos8._aos8_generated_read,
        apstra._apstra_generated_read,
        clearpass._clearpass_generated_read,
        edgeconnect._edgeconnect_generated_read,
        mist._mist_generated_read,
        uxi._uxi_generated_read,
    )
    for executor in executors:
        parameters = inspect.signature(executor).parameters
        assert parameters["body"].default is None
        assert parameters["content_type"].default == "application/json"


def test_clearpass_disconnect_all_is_destructive():
    tool = _tool(clearpass, "clearpass_session_action_disconnect_post")
    assert tool.annotations.destructive_hint is True


# ---------------------------------------------------------------------------
# Auth-parameter isolation
# ---------------------------------------------------------------------------

def test_aos8_uidaruba_never_a_model_argument():
    # The session id is injected by _aos8_send, never exposed as a tool arg.
    tool = _tool(aos8, "aos8_get_object_aaa_prof")
    props = _props(tool)
    assert "config_path" in props
    assert "uidaruba" not in {k.lower() for k in props}
    # And no manifest operation carries a UIDARUBA parameter.
    import json
    from pathlib import Path

    man = json.loads(Path(aos8.__file__).resolve().parent.joinpath(
        "openapi_gen/manifests/aos8.json").read_text())
    assert not any(
        p["name"].lower() == "uidaruba" for o in man["operations"] for p in o["parameters"]
    )


def test_clearpass_read_injects_bearer_and_hides_auth(monkeypatch):
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "secret")
    cap: dict = {}
    _fake_httpx(
        monkeypatch,
        clearpass,
        cap,
        payload={"items": [1, 2, 3], "radius_secret": "sensitive-value"},
    )
    fn = _tool(clearpass, "clearpass_certificate_chain_by_cert_id_chain_get").fn
    out = asyncio.run(fn(cert_id="42"))
    assert cap["url"] == "https://cp.example.com/api/certificate/42/chain"
    assert cap["headers"]["Authorization"] == "Bearer secret"
    assert out["status_code"] == 200
    assert out["data"]["radius_secret"] != "sensitive-value"


# ---------------------------------------------------------------------------
# Request-type dispatch (reads execute directly; correct method/path/params)
# ---------------------------------------------------------------------------

def test_uxi_read_dispatch_bounds_and_injects_oauth(monkeypatch):
    monkeypatch.setenv("UXI_CLIENT_ID", "cid")
    monkeypatch.setenv("UXI_CLIENT_SECRET", "csec")

    async def fake_token(cid, csec, turl):
        return "tok-123"

    monkeypatch.setattr(uxi, "_uxi_access_token", fake_token)
    monkeypatch.setattr(uxi, "_uxi_throttle", lambda: asyncio.sleep(0))
    cap: dict = {}
    _fake_httpx(monkeypatch, uxi, cap, payload={"items": [1, 2, 3, 4, 5]})
    fn = _tool(uxi, "uxi_agent_group_assignments_get").fn
    out = asyncio.run(fn(limit=2))
    assert cap["url"] == "https://api.capenetworks.com/networking-uxi/v1alpha1/agent-group-assignments"
    assert cap["headers"]["Authorization"] == "Bearer tok-123"
    assert cap["params"] == {"limit": 2}
    assert "_pagination" in out["data"]


def test_aos8_generated_read_prefixes_config_base_and_uses_legacy_token(monkeypatch):
    monkeypatch.setenv("AOS8_BASE_URL", "https://mc.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "legacytok")
    monkeypatch.delenv("AOS8_USERNAME", raising=False)
    monkeypatch.delenv("AOS8_PASSWORD", raising=False)
    cap: dict = {}
    _fake_httpx(monkeypatch, aos8, cap, payload={"rows": [1, 2, 3]})
    fn = _tool(aos8, "aos8_get_object_aaa_prof").fn
    out = asyncio.run(fn(config_path="/md"))
    assert cap["method"] == "GET"
    assert cap["url"] == "https://mc.example.com/v1/configuration/object/aaa_prof"
    assert cap["params"]["config_path"] == "/md"
    assert cap["headers"]["Authorization"] == "Bearer legacytok"
    assert out["status_code"] == 200


def test_apstra_readonly_query_post_dispatches_directly(monkeypatch):
    # POST obj-policy-application-points is reclassified read via the override.
    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_API_TOKEN", "statictok")
    cap: dict = {}
    _fake_httpx(monkeypatch, apstra, cap, payload={"application_points": [1, 2]})
    name, tool = _find_tool(
        apstra, "apstra_list_application_endpoints", apstra.GENERATED_APSTRA_TOOLS
    )
    # Registered as a read tool -> readOnly hint set, no dry_run parameter.
    assert tool.annotations.read_only_hint is True
    assert "dry_run" not in _props(tool)
    out = asyncio.run(tool.fn(blueprint_id="bp1"))
    assert cap["method"] == "POST"
    assert cap["url"].endswith("/api/blueprints/bp1/obj-policy-application-points")
    assert cap["headers"]["AuthToken"] == "statictok"
    assert out["status_code"] == 200


def test_apstra_readonly_query_post_forwards_required_json_body(monkeypatch):
    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_API_TOKEN", "statictok")
    cap: dict = {}
    _fake_httpx(monkeypatch, apstra, cap, payload={"items": [{"id": "ct1"}]})
    _, tool = _find_tool(
        apstra, "apstra_search_connectivity_templates", apstra.GENERATED_APSTRA_TOOLS
    )
    props = _props(tool)
    assert tool.annotations.read_only_hint is True
    assert "body" in (tool.parameters.get("required") or [])
    assert "dry_run" not in props
    assert "confirm" not in props

    out = asyncio.run(
        tool.fn(
            blueprint_id="bp1",
            body={"search_string": "leaf", "policy_type": "batch"},
        )
    )

    assert cap["method"] == "POST"
    assert cap["url"].endswith("/api/blueprints/bp1/obj-policy-search")
    assert cap["kw"]["json"] == {"search_string": "leaf", "policy_type": "batch"}
    assert out["status_code"] == 200


def test_read_path_traversal_rejected(monkeypatch):
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "secret")
    fn = _tool(clearpass, "clearpass_certificate_chain_by_cert_id_chain_get").fn
    out = asyncio.run(fn(cert_id="a/b"))
    assert "error" in out


# ---------------------------------------------------------------------------
# Write gate: blocked by default, dry-run preview, confirm required
# ---------------------------------------------------------------------------

def test_clearpass_generated_write_blocked_by_default(monkeypatch):
    monkeypatch.delenv("HPE_MCP_CLEARPASS_WRITES", raising=False)
    monkeypatch.delenv("HPE_MCP_PRODUCT_ACCESS", raising=False)
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "secret")
    name, tool = _find_tool(clearpass, "clearpass_certificate_by_certificate_id_delete")
    out = asyncio.run(tool.fn(certificate_id="9", dry_run=False, confirm=True))
    assert out["status"] == "blocked"


def test_uxi_generated_write_dry_run_then_confirm(monkeypatch):
    monkeypatch.setenv("HPE_MCP_UXI_WRITES", "1")
    monkeypatch.setenv("UXI_CLIENT_ID", "cid")
    monkeypatch.setenv("UXI_CLIENT_SECRET", "csec")

    async def fake_token(cid, csec, turl):
        return "tok"

    monkeypatch.setattr(uxi, "_uxi_access_token", fake_token)
    monkeypatch.setattr(uxi, "_uxi_throttle", lambda: asyncio.sleep(0))
    fn = _tool(uxi, "uxi_agent_group_assignment_post").fn
    # dry_run default True -> preview, no HTTP call.
    preview = asyncio.run(fn(body={"agentId": "a1", "groupId": "g1"}))
    assert preview["dry_run"] is True
    assert preview["method"] == "POST"
    # dry_run False without confirm -> refused.
    refused = asyncio.run(fn(body={"agent": "a1"}, dry_run=False))
    assert "confirm=True is required" in refused["error"]
    # Execute with confirm.
    cap: dict = {}
    _fake_httpx(monkeypatch, uxi, cap, payload={"id": "x"})
    done = asyncio.run(
        fn(body={"agentId": "a1", "groupId": "g1"}, dry_run=False, confirm=True)
    )
    assert cap["method"] == "POST"
    assert done["status_code"] == 200


def test_aos8_destructive_action_is_write_gated_and_annotated(monkeypatch):
    tool = _tool(aos8, "aos8_post_object_reload_device")
    assert tool.annotations.destructive_hint is True
    assert tool.annotations.read_only_hint is not True
    monkeypatch.delenv("HPE_MCP_AOS8_WRITES", raising=False)
    monkeypatch.delenv("HPE_MCP_PRODUCT_ACCESS", raising=False)
    monkeypatch.setenv("AOS8_BASE_URL", "https://mc.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "legacytok")
    out = asyncio.run(
        tool.fn(config_path="/md", body={"_action": "add"}, dry_run=False, confirm=True)
    )
    assert out["status"] == "blocked"


def test_apstra_generated_write_dry_run_and_gate(monkeypatch):
    monkeypatch.delenv("HPE_MCP_APSTRA_WRITES", raising=False)
    monkeypatch.delenv("HPE_MCP_PRODUCT_ACCESS", raising=False)
    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_API_TOKEN", "statictok")
    name, tool = _find_tool(
        apstra, "apstra_import_connectivity_templates", apstra.GENERATED_APSTRA_TOOLS
    )
    # PUT -> idempotent-write, still gated + dry-run.
    assert "dry_run" in _props(tool)
    blocked = asyncio.run(tool.fn(blueprint_id="bp1", body={"x": 1}, dry_run=False, confirm=True))
    assert blocked["status"] == "blocked"
    monkeypatch.setenv("HPE_MCP_APSTRA_WRITES", "1")
    preview = asyncio.run(tool.fn(blueprint_id="bp1", body={"x": 1}))
    assert preview["dry_run"] is True
    assert preview["method"] == "PUT"


# ---------------------------------------------------------------------------
# Response bounding + write-body redaction
# ---------------------------------------------------------------------------

def test_clearpass_write_dry_run_redacts_secrets(monkeypatch):
    monkeypatch.setenv("HPE_MCP_CLEARPASS_WRITES", "1")
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "secret")
    name, tool = _find_tool(clearpass, "clearpass_certificate_by_certificate_id_delete")
    out = asyncio.run(tool.fn(certificate_id="9", body={"password": "hunter2"}))
    assert out["dry_run"] is True
    assert out["json"] != {"password": "hunter2"}
