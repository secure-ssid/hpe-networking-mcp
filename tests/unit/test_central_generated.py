"""Unit tests for the generated Aruba Central backend
(hpe_networking_mcp.mcp_servers.central_generated).

Covers: committed manifest counts, direct registration on a dedicated server,
representative read/write dispatch, the Central write gate + dry_run/confirm,
content-type handling (JSON / multipart), auth isolation (auth injected last,
never model-visible), response bounding, and source drift/determinism.
"""

from __future__ import annotations

import asyncio

import pytest

import hpe_networking_mcp.mcp_servers.central_generated as cg
import hpe_networking_mcp.mcp_servers.openapi_gen.http_exec as http_exec
from hpe_networking_mcp.mcp_servers.openapi_gen import manifest_operation_count

EXPECTED_OPERATION_COUNT = 1677

READ_TOOL = "central_read_bcn_rpt_req_profiles"
POST_TOOL = "central_create_bcn_rpt_req_profiles_profile_by_id"
DELETE_TOOL = "central_delete_bcn_rpt_req_profiles_profile_by_id"
MULTIPART_TOOL = "central_upload_image_file"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeTokenManager:
    def __init__(self, token: str) -> None:
        self._token = token
        self.calls = 0

    def get_access_token(self, *a, **kw) -> str:
        self.calls += 1
        return self._token


class _FakeCentralClient:
    def __init__(self, base_url="https://api.central.test", token="TESTTOKEN"):
        self.base_url = base_url
        self.token_manager = _FakeTokenManager(token)


def _patch_client(monkeypatch):
    client = _FakeCentralClient()
    monkeypatch.setattr(cg, "get_client", lambda: client)
    return client


def _fake_httpx(monkeypatch, captured, *, payload=None, resp_cls=None):
    class Resp:
        status_code = 200
        text = "{}"
        headers = {"content-type": "application/json"}

        def json(self):
            return payload if payload is not None else {"ok": True}

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
            return (resp_cls or Resp)()

    monkeypatch.setattr(http_exec.httpx, "AsyncClient", FakeClient)


def _tool_fn(name):
    return cg.mcp._tool_manager._tools[name].fn


# ---------------------------------------------------------------------------
# Counts + direct registration
# ---------------------------------------------------------------------------

def test_committed_manifest_and_registration_counts():
    assert manifest_operation_count("central") == EXPECTED_OPERATION_COUNT
    assert len(cg.GENERATED_CENTRAL_TOOLS) == EXPECTED_OPERATION_COUNT
    assert len(cg.mcp._tool_manager._tools) == EXPECTED_OPERATION_COUNT


def test_direct_registration_exposes_typed_tools_on_dedicated_server():
    tools = cg.mcp._tool_manager._tools
    assert READ_TOOL in tools
    assert POST_TOOL in tools
    assert DELETE_TOOL in tools
    # Dedicated server, not the curated Central backends.
    assert cg.mcp.name == "central-generated"
    # Read is read-only; delete is destructive/non-idempotent-visible.
    assert tools[READ_TOOL].annotations.read_only_hint is True
    assert tools[DELETE_TOOL].annotations.read_only_hint is not True


def test_broader_central_api_families_are_registered():
    tools = cg.mcp._tool_manager._tools

    assert "central_get_access_points_v1" in tools
    assert "central_get_alert_list_v1" in tools
    assert "central_list_reports_v1" in tools
    assert "central_get_device_locations_v1" in tools
    assert "central_initiate_cx_ping_v1" in tools
    assert cg._central_allowed_prefixes() == (
        "/as/",
        "/network-config/",
        "/network-monitoring/",
        "/network-msp/",
        "/network-notifications/",
        "/network-reporting/",
        "/network-services/",
        "/network-troubleshooting/",
    )


def test_read_tool_signature_hides_auth_and_exposes_params():
    props = cg.mcp._tool_manager._tools[READ_TOOL].parameters.get("properties") or {}
    # Query params are typed named args.
    assert "view_type" in props and "limit" in props
    # No auth argument is ever exposed.
    assert "authorization" not in props and "cookie" not in props


def test_write_tool_signature_has_body_dry_run_confirm():
    props = cg.mcp._tool_manager._tools[POST_TOOL].parameters.get("properties") or {}
    assert {"name", "body", "dry_run", "confirm"} <= set(props)


# ---------------------------------------------------------------------------
# Read dispatch + bounding + auth
# ---------------------------------------------------------------------------

def test_read_dispatch_bounds_injects_auth_and_preserves_false(monkeypatch):
    _patch_client(monkeypatch)
    cap: dict = {}
    _fake_httpx(monkeypatch, cap, payload={"items": [1, 2, 3, 4, 5]})
    out = asyncio.run(_tool_fn(READ_TOOL)(view_type="committed", effective=False, limit=2))
    assert cap["url"] == "https://api.central.test/network-config/v1alpha1/bcn-rpt-req-profiles"
    # Auth injected last, from the client's own header format.
    assert cap["headers"]["Authorization"] == "Bearer TESTTOKEN"
    # False preserved, None omitted.
    assert cap["params"]["effective"] is False
    assert cap["params"]["view-type"] == "committed"
    # Response bounding applied.
    assert "_pagination" in out["data"]


def test_read_retries_after_forced_token_refresh(monkeypatch):
    client = _patch_client(monkeypatch)
    attempts = {"count": 0}

    class Resp:
        text = "{}"
        content = b"{}"
        headers = {"content-type": "application/json"}

        def __init__(self, status_code):
            self.status_code = status_code

        def json(self):
            return {"ok": self.status_code == 200}

    class FakeClient:
        def __init__(self, timeout=None, **_ignored):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, **kwargs):
            attempts["count"] += 1
            return Resp(401 if attempts["count"] == 1 else 200)

    monkeypatch.setattr(http_exec.httpx, "AsyncClient", FakeClient)

    out = asyncio.run(_tool_fn(READ_TOOL)())

    assert out["status_code"] == 200
    assert attempts["count"] == 2
    assert client.token_manager.calls >= 3


def test_read_path_traversal_rejected(monkeypatch):
    _patch_client(monkeypatch)
    fn = _tool_fn("central_read_bcn_rpt_req_profiles_profile_by_id")
    out = asyncio.run(fn(name="a/b"))
    assert "error" in out


def test_auth_header_param_cannot_shadow_injected_auth(monkeypatch):
    _patch_client(monkeypatch)
    cap: dict = {}
    _fake_httpx(monkeypatch, cap)
    # Even if a caller smuggles a header dict, auth stays the trusted value.
    # (Header params are stripped by the runtime; verify executor-level guard.)
    base, headers = asyncio.run(
        cg._central_auth_headers({"Authorization": "Bearer EVIL", "X-Trace": "t1"})
    )
    assert headers["Authorization"] == "Bearer TESTTOKEN"
    assert headers["X-Trace"] == "t1"


# ---------------------------------------------------------------------------
# Write gate + dry_run/confirm + content types
# ---------------------------------------------------------------------------

def test_write_blocked_when_central_writes_disabled(monkeypatch):
    monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "0")
    _patch_client(monkeypatch)
    out = asyncio.run(
        _tool_fn(POST_TOOL)(name="p1", body={"name": "p1"}, dry_run=False, confirm=True)
    )
    assert out["status"] == "blocked"
    assert out["platform"] == "central"


def test_write_dry_run_redacts_and_previews(monkeypatch):
    monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
    _patch_client(monkeypatch)
    out = asyncio.run(_tool_fn(POST_TOOL)(name="p1", body={"name": "p1"}, dry_run=True))
    assert out["dry_run"] is True
    assert out["url"] == (
        "https://api.central.test/network-config/v1alpha1/bcn-rpt-req-profiles/p1"
    )
    assert "execute_hint" in out


def test_write_requires_confirm_when_not_dry_run(monkeypatch):
    monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
    _patch_client(monkeypatch)
    out = asyncio.run(
        _tool_fn(POST_TOOL)(name="p1", body={"name": "p1"}, dry_run=False, confirm=False)
    )
    assert out["dry_run"] is True
    assert "confirm=True is required" in out["error"]


def test_write_executes_json_body_with_confirm(monkeypatch):
    monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
    _patch_client(monkeypatch)
    cap: dict = {}
    _fake_httpx(monkeypatch, cap)
    out = asyncio.run(
        _tool_fn(POST_TOOL)(name="p1", body={"name": "p1"}, dry_run=False, confirm=True)
    )
    assert out["status_code"] == 200
    assert cap["method"] == "POST"
    assert cap["kw"]["json"] == {"name": "p1"}
    assert cap["headers"]["Authorization"] == "Bearer TESTTOKEN"


def test_multipart_write_uses_httpx_files(monkeypatch):
    monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
    _patch_client(monkeypatch)
    cap: dict = {}
    _fake_httpx(monkeypatch, cap)
    fn = _tool_fn(MULTIPART_TOOL)
    out = asyncio.run(fn(body={"file": "image-bytes"}, dry_run=False, confirm=True))
    assert out["status_code"] == 200
    files = cap["kw"]["files"]
    assert files["file"] == (None, "image-bytes")
    # multipart must not carry an explicit JSON content-type header.
    assert "Content-Type" not in cap["headers"]


def test_write_gate_default_disabled(monkeypatch):
    monkeypatch.delenv("HPE_MCP_CENTRAL_WRITES", raising=False)
    _patch_client(monkeypatch)
    out = asyncio.run(_tool_fn(DELETE_TOOL)(name="p1", dry_run=True))
    # 0.9.1: Central is deny-by-default
    assert out["status"] == "blocked"
    assert out["platform"] == "central"

    # Opting in restores the dry-run preview.
    monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
    out = asyncio.run(_tool_fn(DELETE_TOOL)(name="p1", dry_run=True))
    assert out["dry_run"] is True
    assert out["method"] == "DELETE"


# ---------------------------------------------------------------------------
# Binary/text response bounding
# ---------------------------------------------------------------------------

def test_binary_read_response_is_bounded(monkeypatch):
    _patch_client(monkeypatch)

    class BinResp:
        status_code = 200
        headers = {"content-type": "application/octet-stream"}
        content = b"x" * 200_000
        text = ""

        def json(self):
            raise ValueError("not json")

    cap: dict = {}
    _fake_httpx(monkeypatch, cap, resp_cls=BinResp)
    out = asyncio.run(_tool_fn(READ_TOOL)())
    payload = out["data"]
    assert payload["size_bytes"] == 200_000
    assert payload["truncated"] is True
    assert len(payload["base64"]) < 200_000


# ---------------------------------------------------------------------------
# Path allow-list guard
# ---------------------------------------------------------------------------

def test_path_allow_list_rejects_foreign_prefix(monkeypatch):
    _patch_client(monkeypatch)
    out = asyncio.run(cg._central_generated_read("GET", "/monitoring/v1/aps", {}, {}))
    assert "error" in out
    assert cg._central_path_ok("/network-config/v1alpha1/bgp") is True


# ---------------------------------------------------------------------------
# Source drift / determinism
# ---------------------------------------------------------------------------

def test_committed_central_manifest_matches_fresh_build():
    from pathlib import Path

    from hpe_networking_mcp.mcp_servers.openapi_gen import manifest as manifest_mod
    from scripts import generate_central_tools as cli

    if not cli.DEFAULT_SPEC_DIR.exists() or not any(cli.DEFAULT_SPEC_DIR.glob("*.json")):
        pytest.skip("local Central OpenAPI specs not present (gitignored)")
    fresh = manifest_mod.dumps(cli.build_central_manifest(Path(cli.DEFAULT_SPEC_DIR)))
    committed = manifest_mod.manifest_path("central").read_text()
    assert fresh == committed, (
        "committed Central manifest is stale; re-run generate_central_tools.py"
    )


def test_merged_build_is_deterministic():
    from pathlib import Path

    from hpe_networking_mcp.mcp_servers.openapi_gen import manifest as manifest_mod
    from scripts import generate_central_tools as cli

    if not cli.DEFAULT_SPEC_DIR.exists() or not any(cli.DEFAULT_SPEC_DIR.glob("*.json")):
        pytest.skip("local Central OpenAPI specs not present (gitignored)")
    a = manifest_mod.dumps(cli.build_central_manifest(Path(cli.DEFAULT_SPEC_DIR)))
    b = manifest_mod.dumps(cli.build_central_manifest(Path(cli.DEFAULT_SPEC_DIR)))
    assert a == b


def test_normalize_prepends_relative_server_base():
    from scripts.generate_central_tools import normalize_central_spec, server_base_path

    rel = {"servers": [{"url": "/network-config/v1alpha1"}], "paths": {"/bgp": {"get": {}}}}
    assert server_base_path(rel) == "/network-config/v1alpha1"
    out = normalize_central_spec(rel)
    assert "/network-config/v1alpha1/bgp" in out["paths"]
    # Absolute host servers are left untouched.
    abs_spec = {"servers": [{"url": "https://api.central.test"}], "paths": {"/x": {"get": {}}}}
    assert server_base_path(abs_spec) == ""
    assert normalize_central_spec(abs_spec)["paths"] == {"/x": {"get": {}}}
