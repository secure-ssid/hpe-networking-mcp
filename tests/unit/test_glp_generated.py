"""Unit tests for the generated GreenLake (GLP) tools on hpe_networking_mcp.mcp_servers.glp.

Covers: committed manifest count + provenance/digests, direct registration on
the curated glp-core server (without disturbing curated tools), representative
read/write dispatch, the GLP write gate (HPE_MCP_GLP_V2BETA1_WRITES, fail
closed) + dry_run/confirm, content-type handling (JSON / merge-patch / multipart),
auth isolation + workspace reuse, response bounding, source drift/determinism,
and account isolation from the Central generated backend.
"""

from __future__ import annotations

import asyncio
import os

import pytest

import hpe_networking_mcp.mcp_servers.glp as glp
import hpe_networking_mcp.mcp_servers.openapi_gen.http_exec as http_exec
from hpe_networking_mcp.mcp_servers.openapi_gen import manifest_operation_count

EXPECTED_OPERATION_COUNT = 920
EXPECTED_REGISTERED_OPERATION_COUNT = 906

READ_LIST = "glp_get_v1beta1_user_preferences"
READ_PATH = "glp_get_audit_log"
POST_JSON = "glp_create_role_assignment_v1beta1"
MULTIPART = "glp_create_location_csv"
DELETE_TOOL = "glp_delete_role_assignment_v1beta1"
DRYRUN_COLLIDER = "glp_post_devices_v1"
CONTENT_TYPE_HEADER = "glp_create_v1_group"
PROXY_HEADERS = "glp_add_secret_v1"
BUSINESS_HEADER = "glp_get_v1_alerts"


@pytest.fixture(scope="module", autouse=True)
def _register_generated_glp_tools():
    """Enable + register the opt-in generated GLP tools for this module only.

    The generated tools default OFF so the curated ``glp-core`` catalog stays
    lean (and the public README/catalog counts stay valid). We register them on
    the shared ``glp.mcp`` for these tests, then fully remove them on teardown
    so later count/catalog tests see the untouched 62-tool curated server.
    """
    prev = os.environ.get("HPE_MCP_GLP_GENERATED_TOOLS")
    os.environ["HPE_MCP_GLP_GENERATED_TOOLS"] = "1"
    names = glp._register_generated_glp_tools()
    try:
        yield names
    finally:
        for name in names:
            glp.mcp._tool_manager._tools.pop(name, None)
        glp.GENERATED_GLP_TOOLS = []
        if prev is None:
            os.environ.pop("HPE_MCP_GLP_GENERATED_TOOLS", None)
        else:
            os.environ["HPE_MCP_GLP_GENERATED_TOOLS"] = prev


class _FakeTokenManager:
    def __init__(self, token):
        self._token = token
        self.calls = 0

    def get_access_token(self, *a, **kw):
        self.calls += 1
        return self._token


class _FakeCentralClient:
    def __init__(self, base_url="https://global.api.greenlake.hpe.com", token="GLPTOKEN"):
        self.base_url = base_url
        self.token_manager = _FakeTokenManager(token)


class _FakeGLPClient:
    def __init__(self):
        self._client = _FakeCentralClient()
        self.workspace_id = "ws-123"


def _patch_glp(monkeypatch):
    fake = _FakeGLPClient()
    monkeypatch.setattr(glp, "get_glp_client", lambda: fake)
    return fake


def _fake_httpx(monkeypatch, captured, *, payload=None, resp_cls=None):
    class Resp:
        status_code = 200
        text = "{}"
        headers = {"content-type": "application/json"}

        def json(self):
            return payload if payload is not None else {"ok": True}

    class FakeClient:
        def __init__(self, timeout=None):
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


def _fn(name):
    return glp.mcp._tool_manager._tools[name].fn


# ---------------------------------------------------------------------------
# Counts + provenance + registration
# ---------------------------------------------------------------------------

def test_committed_manifest_count_and_registration():
    assert manifest_operation_count("glp") == EXPECTED_OPERATION_COUNT
    assert len(glp.GENERATED_GLP_TOOLS) == EXPECTED_REGISTERED_OPERATION_COUNT


def test_generated_glp_schemas_hide_transport_headers_only():
    content_props = glp.mcp._tool_manager._tools[
        CONTENT_TYPE_HEADER
    ].parameters["properties"]
    proxy_props = glp.mcp._tool_manager._tools[
        PROXY_HEADERS
    ].parameters["properties"]
    business_props = glp.mcp._tool_manager._tools[
        BUSINESS_HEADER
    ].parameters["properties"]

    assert "content_type" not in content_props
    assert {"x_forwarded_for", "x_envoy_external_address"}.isdisjoint(proxy_props)
    assert "tenant_acid" in business_props


def test_manifest_provenance_and_digests():
    from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import load_manifest

    man = load_manifest("glp")
    prov = man["provenance"]
    assert prov["upstream_license"] == "MIT"
    assert "hpe-networking-mcp" in prov["upstream_repo"]
    # Pinned ref (a 40-char commit SHA) for reproducible digests.
    assert len(prov["upstream_ref"]) == 40
    # Per-source digests recorded for every included spec.
    files = man["source"]["files"]
    assert files and all(len(f["sha256"]) == 64 for f in files)
    # sources.json (spec-index, not an OpenAPI doc) is reported as excluded.
    excluded = {e["file"] for e in prov["excluded_sources"]}
    assert "sources.json" in excluded


def test_generated_tools_do_not_disturb_curated_glp_tools():
    tools = glp.mcp._tool_manager._tools
    for curated in ("glp_get", "list_glp_devices", "glp_write_status", "glp_assign_subscription"):
        assert curated in tools
    # Generated tools live on the same curated server, additive only.
    assert len(tools) >= EXPECTED_REGISTERED_OPERATION_COUNT + 62


def test_read_tool_hides_auth_and_write_tool_has_controls():
    read_props = glp.mcp._tool_manager._tools[READ_PATH].parameters.get("properties") or {}
    assert "id" in read_props and "authorization" not in read_props
    write_props = glp.mcp._tool_manager._tools[POST_JSON].parameters.get("properties") or {}
    assert {"body", "dry_run", "confirm"} <= set(write_props)


def test_dry_run_query_param_collision_preserved_as_suffixed_arg():
    # A real GreenLake ``dry-run`` query param must survive alongside our
    # injected dry_run control (renamed to dry_run_2, original name preserved).
    props = glp.mcp._tool_manager._tools[DRYRUN_COLLIDER].parameters.get("properties") or {}
    assert "dry_run" in props and "dry_run_2" in props


# ---------------------------------------------------------------------------
# Read dispatch: bounding + auth + workspace reuse
# ---------------------------------------------------------------------------

def test_read_dispatch_reuses_glp_client_auth_and_bounds(monkeypatch):
    fake = _patch_glp(monkeypatch)
    monkeypatch.setenv("GLP_GENERATED_REGION", "us-west")
    cap: dict = {}
    _fake_httpx(monkeypatch, cap, payload={"items": [1, 2, 3]})
    out = asyncio.run(_fn(READ_LIST)())
    assert cap["url"] == (
        "https://us-west.api.greenlake.hpe.com/compute-ops-mgmt/v1beta1/user-preferences"
    )
    assert cap["headers"]["Authorization"] == "Bearer GLPTOKEN"
    assert fake._client.token_manager.calls == 1  # token acquired off the event loop
    assert "_pagination" in out["data"]


def test_read_path_param_escaping(monkeypatch):
    _patch_glp(monkeypatch)
    cap: dict = {}
    _fake_httpx(monkeypatch, cap)
    asyncio.run(_fn(READ_PATH)(id="a b"))
    assert cap["url"].endswith("/audit-log/v2beta1/logs/a%20b")
    out = asyncio.run(_fn(READ_PATH)(id="a/b"))
    assert "error" in out


# ---------------------------------------------------------------------------
# Write gate + dry_run/confirm + content types
# ---------------------------------------------------------------------------

def test_write_blocked_when_glp_writes_disabled(monkeypatch):
    monkeypatch.delenv("HPE_MCP_GLP_V2BETA1_WRITES", raising=False)
    _patch_glp(monkeypatch)
    out = asyncio.run(_fn(POST_JSON)(body={"x": 1}, dry_run=False, confirm=True))
    assert out["status"] == "blocked"
    assert out["platform"] == "glp"


def test_write_dry_run_default_previews(monkeypatch):
    monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
    _patch_glp(monkeypatch)
    out = asyncio.run(_fn(POST_JSON)(body={"x": 1}))
    assert out["dry_run"] is True
    assert out["url"].endswith("/authorization/v1beta1/role-assignments")
    assert "execute_hint" in out


def test_write_executes_json_with_confirm(monkeypatch):
    monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
    _patch_glp(monkeypatch)
    cap: dict = {}
    _fake_httpx(monkeypatch, cap)
    out = asyncio.run(_fn(POST_JSON)(body={"x": 1}, dry_run=False, confirm=True))
    assert out["status_code"] == 200
    assert cap["method"] == "POST"
    assert cap["kw"]["json"] == {"x": 1}
    assert cap["headers"]["Authorization"] == "Bearer GLPTOKEN"


def test_multipart_write_uses_files(monkeypatch):
    monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
    _patch_glp(monkeypatch)
    cap: dict = {}
    _fake_httpx(monkeypatch, cap)
    out = asyncio.run(_fn(MULTIPART)(body={"file": "csv,data"}, dry_run=False, confirm=True))
    assert out["status_code"] == 200
    assert cap["kw"]["files"]["file"] == (None, "csv,data")
    assert "Content-Type" not in cap["headers"]
    body_schema = glp.mcp._tool_manager._tools[MULTIPART].parameters["properties"]["body"]
    assert body_schema["type"] == "object"


def test_dry_run_query_param_forwarded_with_original_name(monkeypatch):
    monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
    _patch_glp(monkeypatch)
    cap: dict = {}
    _fake_httpx(monkeypatch, cap)
    # dry_run_2 is the server-side GreenLake ``dry-run`` query param.
    asyncio.run(
        _fn(DRYRUN_COLLIDER)(body={"n": []}, dry_run_2=True, dry_run=False, confirm=True)
    )
    assert cap["params"].get("dry-run") is True


# ---------------------------------------------------------------------------
# Auth isolation between Central and GLP backends
# ---------------------------------------------------------------------------

def test_auth_isolation_uses_glp_not_central(monkeypatch):
    # The GLP resolver must never reach the Central source-account client.
    def _boom():
        raise AssertionError("GLP backend must not call get_client() (Central account)")

    monkeypatch.setattr(glp, "get_glp_client", lambda: _FakeGLPClient())
    import hpe_networking_mcp.mcp_servers.central_generated as cg

    monkeypatch.setattr(cg, "get_client", _boom)
    base, headers = asyncio.run(glp._glp_generated_auth_headers(None))
    assert base == "https://global.api.greenlake.hpe.com"
    assert headers["Authorization"] == "Bearer GLPTOKEN"


def test_auth_header_param_cannot_shadow_injected_auth(monkeypatch):
    _patch_glp(monkeypatch)
    _, headers = asyncio.run(
        glp._glp_generated_auth_headers({"Authorization": "Bearer EVIL", "X-Trace": "t"})
    )
    assert headers["Authorization"] == "Bearer GLPTOKEN"
    assert headers["X-Trace"] == "t"


def test_glp_generated_server_requires_and_maps_region(monkeypatch):
    path = "/block-storage/v1alpha1/devtype4-storage-systems/s1/application-summary"
    monkeypatch.delenv("GLP_GENERATED_REGION", raising=False)
    with pytest.raises(ValueError, match="GLP_GENERATED_REGION"):
        glp._glp_generated_server(path, "https://global.api.greenlake.hpe.com")

    monkeypatch.setenv("GLP_GENERATED_REGION", "eu-central")
    assert glp._glp_generated_server(
        path, "https://global.api.greenlake.hpe.com"
    ) == "https://eu1.data.cloud.hpe.com"


# ---------------------------------------------------------------------------
# Response bounding
# ---------------------------------------------------------------------------

def test_binary_read_response_bounded(monkeypatch):
    _patch_glp(monkeypatch)
    monkeypatch.setenv("GLP_GENERATED_REGION", "us-west")

    class BinResp:
        status_code = 200
        headers = {"content-type": "application/octet-stream"}
        content = b"y" * 200_000
        text = ""

        def json(self):
            raise ValueError("not json")

    cap: dict = {}
    _fake_httpx(monkeypatch, cap, resp_cls=BinResp)
    out = asyncio.run(_fn(READ_LIST)())
    assert out["data"]["size_bytes"] == 200_000
    assert out["data"]["truncated"] is True


# ---------------------------------------------------------------------------
# Source drift / determinism
# ---------------------------------------------------------------------------

def test_committed_glp_manifest_matches_fresh_build():
    from hpe_networking_mcp.mcp_servers.openapi_gen import manifest as manifest_mod
    from scripts import generate_glp_tools as cli

    spec_dir = cli.DEFAULT_SPEC_DIR
    missing = [f for f in cli.VENDOR_SPECS if not (spec_dir / f).exists()]
    if missing:
        pytest.skip("local vendored GreenLake specs not present (gitignored)")
    fresh = manifest_mod.dumps(cli.build_glp_manifest(spec_dir))
    committed = manifest_mod.manifest_path("glp").read_text()
    assert fresh == committed, "committed GLP manifest is stale; re-run generate_glp_tools.py"
