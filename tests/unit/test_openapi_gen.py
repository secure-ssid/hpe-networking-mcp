"""Unit tests for the shared generated-OpenAPI tool foundation."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
from mcp.server.mcpserver import MCPServer

import hpe_networking_mcp.mcp_servers.mist as mist
import hpe_networking_mcp.mcp_servers.openapi_gen.http_exec as http_exec
from hpe_networking_mcp.mcp_servers.openapi_gen import manifest_operation_count
from hpe_networking_mcp.mcp_servers.openapi_gen.classify import classify
from hpe_networking_mcp.mcp_servers.openapi_gen.http_exec import make_read_executor
from hpe_networking_mcp.mcp_servers.openapi_gen.ir import SpecParser, UnresolvedRefError
from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import (
    build_manifest,
    build_merged_manifest,
    dumps,
    sha256_bytes,
)
from hpe_networking_mcp.mcp_servers.openapi_gen.naming import (
    DuplicateNameError,
    NameAllocator,
    base_name,
    snake,
)
from hpe_networking_mcp.mcp_servers.openapi_gen.runtime import (
    _py_type,
    is_transport_header,
    register_generated_tools,
)
from hpe_networking_mcp.mcp_servers.shared import (
    bound_collection_response,
    bounded_response_payload,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "Demo", "version": "1.0", "license": {"name": "MIT"}},
    "components": {
        "parameters": {
            "org_id": {
                "name": "org_id",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            },
            "verbose": {
                "name": "verbose",
                "in": "query",
                "schema": {"type": "boolean", "default": False},
            },
        },
        "schemas": {
            "base": {"type": "object", "properties": {"a": {"type": "string"}}},
            "widget": {
                "allOf": [
                    {"$ref": "#/components/schemas/base"},
                    {"type": "object", "properties": {"b": {"type": "integer"}}},
                ]
            },
            "claim_codes": {"type": "array", "items": {"type": "string"}},
            "mode": {"type": "string", "enum": ["fast", "slow"]},
        },
    },
    "paths": {
        "/api/v1/orgs/{org_id}/widgets": {
            "get": {
                "operationId": "listWidgets",
                "summary": "List widgets",
                "parameters": [
                    {"$ref": "#/components/parameters/org_id"},
                    {"$ref": "#/components/parameters/verbose"},
                    {
                        "name": "mode",
                        "in": "query",
                        "schema": {"$ref": "#/components/schemas/mode"},
                    },
                    {
                        "name": "site_ids",
                        "in": "query",
                        "schema": {"type": "array", "items": {"type": "string"}},
                    },
                    {"name": "X-Trace", "in": "header", "schema": {"type": "string"}},
                    {"name": "Authorization", "in": "header", "schema": {"type": "string"}},
                    {
                        "name": "Content-Type",
                        "in": "header",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "X-Forwarded-For",
                        "in": "header",
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "X-Envoy-External-Address",
                        "in": "header",
                        "schema": {"type": "string"},
                    },
                    {"name": "Host", "in": "header", "schema": {"type": "string"}},
                    {"name": "If-Match", "in": "header", "schema": {"type": "string"}},
                    {"name": "Tenant-Acid", "in": "header", "schema": {"type": "string"}},
                    {
                        "name": "Idempotency-Key",
                        "in": "header",
                        "schema": {"type": "string"},
                    },
                ],
            },
            "post": {
                "operationId": "createWidget",
                "summary": "Create widget",
                "parameters": [
                    {"$ref": "#/components/parameters/org_id"},
                    {
                        "name": "mode",
                        "in": "query",
                        "schema": {"$ref": "#/components/schemas/mode"},
                    },
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/widget"}}
                    },
                },
            },
            "delete": {
                "operationId": "deleteWidgets",
                "summary": "Delete widgets",
                "parameters": [{"$ref": "#/components/parameters/org_id"}],
            },
        },
    },
}


def _manifest():
    return build_manifest(
        SPEC,
        platform="demo",
        source_file="demo.json",
        source_sha256="deadbeef",
    )


# ---------------------------------------------------------------------------
# Parsing / IR
# ---------------------------------------------------------------------------

def test_parser_resolves_refs_params_and_bodies():
    ops = SpecParser(SPEC).operations()
    # Deterministic walk: methods in canonical order get,put,post,delete,...
    assert [o.method for o in ops] == ["GET", "POST", "DELETE"]
    get = ops[0]
    params = {p.name: p for p in get.parameters}
    assert params["org_id"].location == "path" and params["org_id"].required
    assert params["verbose"].schema_type == "boolean" and params["verbose"].default is False
    assert params["mode"].enum == ["fast", "slow"]
    assert params["site_ids"].schema_type == "array"
    assert params["site_ids"].item_type == "string"
    # allOf request body resolves to an object
    post = ops[1]
    assert post.request_body.schema_type == "object"
    assert post.request_body.content_type == "application/json"
    assert post.request_body.required is True


def test_parser_array_body_item_type():
    spec = {
        "openapi": "3.1.0",
        "paths": {
            "/api/v1/x": {
                "post": {
                    "operationId": "claim",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/claim_codes"}
                            }
                        }
                    },
                }
            }
        },
        "components": SPEC["components"],
    }
    op = SpecParser(spec).operations()[0]
    assert op.request_body.schema_type == "array"
    assert op.request_body.item_type == "string"


def test_parser_raises_on_unresolved_ref():
    spec = {
        "openapi": "3.1.0",
        "paths": {
            "/api/v1/x": {
                "get": {
                    "operationId": "g",
                    "parameters": [{"$ref": "#/components/parameters/missing"}],
                }
            }
        },
    }
    with pytest.raises(UnresolvedRefError):
        SpecParser(spec).operations()


def test_parser_rejects_unsupported_version():
    with pytest.raises(Exception):
        SpecParser({"swagger": "1.2", "paths": {}})


# ---------------------------------------------------------------------------
# Naming / classification
# ---------------------------------------------------------------------------

def test_snake_and_base_name():
    assert snake("listOrgSites") == "list_org_sites"
    assert base_name("demo", "GET", "/x", "listOrgSites") == "demo_list_org_sites"


def test_name_allocator_fails_on_unresolved_duplicate():
    alloc = NameAllocator()
    alloc.allocate("demo", "GET", "/api/v1/x", "dup")
    with pytest.raises(DuplicateNameError):
        # Same method+path+operationId → base collides and digest collides too.
        alloc.allocate("demo", "GET", "/api/v1/x", "dup")


def test_name_allocator_disambiguates_distinct_paths():
    alloc = NameAllocator()
    a = alloc.allocate("demo", "GET", "/api/v1/a", "same")
    b = alloc.allocate("demo", "GET", "/api/v1/b", "same")
    assert a != b


def test_classification_defaults_and_override():
    assert classify("GET", "GET /x") == "read"
    assert classify("DELETE", "DELETE /x") == "destructive"
    assert classify("POST", "POST /x") == "write"
    assert classify("POST", "POST /x", {"POST /x": "read"}) == "read"


# ---------------------------------------------------------------------------
# Manifest determinism
# ---------------------------------------------------------------------------

def test_manifest_is_deterministic_and_records_source():
    m1 = _manifest()
    m2 = _manifest()
    assert dumps(m1) == dumps(m2)
    assert m1["source"]["sha256"] == "deadbeef"
    assert m1["source"]["operation_count"] == 3
    caps = {o["key"]: o["capability"] for o in m1["operations"]}
    assert caps["GET /api/v1/orgs/{org_id}/widgets"] == "read"
    assert caps["DELETE /api/v1/orgs/{org_id}/widgets"] == "destructive"


def test_sha256_bytes_stable():
    assert sha256_bytes(b"abc") == sha256_bytes(b"abc")


def test_large_json_response_is_bounded_before_return():
    payload = {"value": "x" * 200_000}

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = json.dumps(payload).encode()

        def json(self):
            return payload

    out = bounded_response_payload(Response())

    assert out["truncated"] is True
    assert out["content_type"] == "application/json"
    assert len(out["text"]) < 200_000


def test_large_json_collection_is_parsed_and_paged_before_byte_bounding():
    payload = [{"id": index, "value": "x" * 1_000} for index in range(5_000)]

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = json.dumps(payload).encode()

        def json(self):
            return payload

    out = bounded_response_payload(Response())
    rebound = bound_collection_response(out, limit=10, offset=0)

    assert len(out["items"]) == 50
    assert out["_pagination"]["total"] == 5_000
    assert out["_response_bounds"]["truncated"] is True
    assert len(rebound["items"]) == 10
    assert rebound["_pagination"]["total"] == 5_000


def test_disabled_generated_mist_tools_return_empty_without_manifest_zip_error(monkeypatch):
    from hpe_networking_mcp.mcp_servers.openapi_gen import runtime

    monkeypatch.setattr(runtime, "register_generated_tools", lambda *args, **kwargs: [])

    assert mist._register_generated_mist_tools() == []


def test_merged_manifest_is_deterministic_and_deduplicates_operations():
    second = {
        "openapi": "3.0.3",
        "info": {"title": "Second", "version": "2"},
        "paths": {
            "/api/v1/orgs/{org_id}/widgets": {
                "get": {
                    "operationId": "duplicateListWidgets",
                    "parameters": [
                        {"$ref": "#/components/parameters/org_id"},
                        {
                            "name": "scope-id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                }
            },
            "/api/v1/health": {"get": {"operationId": "getHealth"}},
        },
        "components": {"parameters": SPEC["components"]["parameters"]},
    }
    docs = [
        ("b.json", "bbb", second),
        ("a.json", "aaa", SPEC),
    ]
    merged = build_merged_manifest(docs, platform="demo")
    assert merged["source"]["operation_count"] == 4
    assert merged["source"]["duplicate_operation_count"] == 1
    assert merged["duplicate_operations"][0]["kept_source"] == "a.json"
    duplicate = next(
        op
        for op in merged["operations"]
        if op["key"] == "GET /api/v1/orgs/{org_id}/widgets"
    )
    assert any(
        parameter["name"] == "scope-id" and parameter["required"]
        for parameter in duplicate["parameters"]
    )
    assert dumps(merged) == dumps(build_merged_manifest(list(reversed(docs)), platform="demo"))


# ---------------------------------------------------------------------------
# MCPServer registration + schema
# ---------------------------------------------------------------------------

def _fake_read_executor(captured):
    async def _exec(
        method, path, query, headers, body=None, content_type="application/json"
    ):
        captured.update(
            method=method,
            path=path,
            query=query,
            headers=headers,
            body=body,
            content_type=content_type,
        )
        return {"status_code": 200, "data": {"ok": True}}
    return _exec


def _fake_write_executor(captured):
    async def _exec(name, method, path, query, headers, body, content_type, dry_run, confirm):
        captured.update(
            name=name, method=method, path=path, query=query, headers=headers,
            body=body, content_type=content_type, dry_run=dry_run, confirm=confirm,
        )
        return {"dry_run": dry_run, "name": name}
    return _exec


def _register_demo(monkeypatch):
    server = MCPServer("demo-core")
    read_cap: dict = {}
    write_cap: dict = {}
    monkeypatch.setenv("HPE_MCP_DEMO_GENERATED_TOOLS", "1")
    names = register_generated_tools(
        server,
        "demo",
        read_executor=_fake_read_executor(read_cap),
        write_executor=_fake_write_executor(write_cap),
        manifest=_manifest(),
    )
    return server, names, read_cap, write_cap


def test_registration_exposes_typed_params_without_auth(monkeypatch):
    server, names, _, _ = _register_demo(monkeypatch)
    assert len(names) == 3
    tools = server._tool_manager._tools
    get_tool = tools["demo_list_widgets"]
    props = (get_tool.parameters.get("properties") or {})
    # Typed named params exposed, not an opaque kwargs blob.
    assert {"org_id", "verbose", "mode", "site_ids"} <= set(props)
    site_id_variants = props["site_ids"].get("anyOf", [props["site_ids"]])
    site_ids_schema = next(
        variant for variant in site_id_variants if variant.get("type") == "array"
    )
    assert site_ids_schema["items"] == {"type": "string"}
    mode_variants = props["mode"].get("anyOf", [props["mode"]])
    mode_schema = next(variant for variant in mode_variants if "enum" in variant)
    assert mode_schema == {"enum": ["fast", "slow"], "type": "string"}
    # Business headers stay exposed; auth and transport headers are stripped.
    assert {"x_trace", "if_match", "tenant_acid", "idempotency_key"} <= set(props)
    assert {
        "authorization",
        "content_type",
        "x_forwarded_for",
        "x_envoy_external_address",
        "host",
    }.isdisjoint(props)
    assert get_tool.annotations.read_only_hint is True
    # Write tool exposes body/dry_run/confirm.
    post_tool = tools["demo_create_widget"]
    post_props = (post_tool.parameters.get("properties") or {})
    assert {"org_id", "mode", "body", "dry_run", "confirm"} <= set(post_props)
    assert post_tool.annotations.read_only_hint is not True


@pytest.mark.parametrize(
    "name",
    [
        "Accept",
        "Content-Type",
        "Content-Length",
        "Host",
        "Connection",
        "Keep-Alive",
        "TE",
        "Trailer",
        "Transfer-Encoding",
        "Upgrade",
        "Forwarded",
        "Via",
        "X-Real-IP",
        "Proxy-Authorization",
        "X-Forwarded-For",
        "X-Envoy-External-Address",
    ],
)
def test_transport_headers_are_recognized_case_insensitively(name):
    assert is_transport_header(name)


@pytest.mark.parametrize(
    "name",
    [
        "Accept-Language",
        "If-Match",
        "Idempotency-Key",
        "Tenant-Acid",
        "Hpe-workspace-id",
        "X-Trace",
    ],
)
def test_business_headers_are_not_treated_as_transport_headers(name):
    assert not is_transport_header(name)


def test_direct_read_dispatch(monkeypatch):
    server, _, read_cap, _ = _register_demo(monkeypatch)
    fn = server._tool_manager._tools["demo_list_widgets"].fn
    out = asyncio.run(
        fn(
            org_id="o1",
            verbose=False,
            mode="fast",
            site_ids=["site-1", "site-2"],
            if_match='"version-1"',
            tenant_acid="tenant-1",
            idempotency_key="request-1",
        )
    )
    assert out["status_code"] == 200
    assert read_cap["path"] == "/api/v1/orgs/o1/widgets"
    # False preserved (not dropped), None omitted.
    assert read_cap["query"] == {
        "verbose": False,
        "mode": "fast",
        "site_ids": ["site-1", "site-2"],
    }
    assert read_cap["headers"] == {
        "If-Match": '"version-1"',
        "Tenant-Acid": "tenant-1",
        "Idempotency-Key": "request-1",
    }


def test_form_nonexploded_query_array_is_comma_separated(monkeypatch):
    manifest = _manifest()
    read = manifest["operations"][0]
    site_ids = next(
        parameter
        for parameter in read["parameters"]
        if parameter["name"] == "site_ids"
    )
    site_ids.update(style="form", explode=False)
    server = MCPServer("demo-form-array")
    captured: dict = {}
    monkeypatch.setenv("HPE_MCP_DEMO_GENERATED_TOOLS", "1")
    register_generated_tools(
        server,
        "demo",
        read_executor=_fake_read_executor(captured),
        write_executor=_fake_write_executor({}),
        manifest=manifest,
    )

    out = asyncio.run(
        server._tool_manager._tools["demo_list_widgets"].fn(
            org_id="o1",
            verbose=False,
            site_ids=["site-1", "site-2"],
        )
    )

    assert out["status_code"] == 200
    assert captured["query"] == {
        "verbose": False,
        "site_ids": "site-1,site-2",
    }


def test_form_nonexploded_boolean_array_uses_openapi_casing(monkeypatch):
    manifest = _manifest()
    read = manifest["operations"][0]
    read["parameters"].append(
        {
            "name": "flags",
            "in": "query",
            "required": False,
            "type": "array",
            "item_type": "boolean",
            "style": "form",
            "explode": False,
        }
    )
    server = MCPServer("demo-form-boolean-array")
    captured: dict = {}
    monkeypatch.setenv("HPE_MCP_DEMO_GENERATED_TOOLS", "1")
    register_generated_tools(
        server,
        "demo",
        read_executor=_fake_read_executor(captured),
        write_executor=_fake_write_executor({}),
        manifest=manifest,
    )

    out = asyncio.run(
        server._tool_manager._tools["demo_list_widgets"].fn(
            org_id="o1",
            flags=[False, True],
        )
    )

    assert out["status_code"] == 200
    assert captured["query"]["flags"] == "false,true"


def test_form_exploded_query_array_retains_repeated_key_values(monkeypatch):
    manifest = _manifest()
    read = manifest["operations"][0]
    site_ids = next(
        parameter
        for parameter in read["parameters"]
        if parameter["name"] == "site_ids"
    )
    site_ids.update(style="form", explode=True)
    server = MCPServer("demo-exploded-array")
    captured: dict = {}
    monkeypatch.setenv("HPE_MCP_DEMO_GENERATED_TOOLS", "1")
    register_generated_tools(
        server,
        "demo",
        read_executor=_fake_read_executor(captured),
        write_executor=_fake_write_executor({}),
        manifest=manifest,
    )

    out = asyncio.run(
        server._tool_manager._tools["demo_list_widgets"].fn(
            org_id="o1",
            site_ids=["site-1", "site-2"],
        )
    )

    assert out["status_code"] == 200
    assert captured["query"]["site_ids"] == ["site-1", "site-2"]


def test_array_python_type_preserves_known_item_types():
    assert _py_type("array", "string") == list[str]
    assert _py_type("array", "integer") == list[int]
    assert _py_type("array", "number") == list[float]
    assert _py_type("array", "boolean") == list[bool]
    assert _py_type("array", None) is list
    assert _py_type("array", "any") is list


def test_invalid_read_enum_is_rejected_before_dispatch(monkeypatch):
    server, _, read_cap, _ = _register_demo(monkeypatch)
    fn = server._tool_manager._tools["demo_list_widgets"].fn

    out = asyncio.run(fn(org_id="o1", mode="turbo"))

    assert out == {"error": "parameter 'mode' must be one of: 'fast', 'slow'"}
    assert read_cap == {}


def test_invalid_write_enum_is_rejected_before_dispatch(monkeypatch):
    server, _, _, write_cap = _register_demo(monkeypatch)
    fn = server._tool_manager._tools["demo_create_widget"].fn

    out = asyncio.run(fn(org_id="o1", mode="turbo", body={"a": "x"}))

    assert out == {"error": "parameter 'mode' must be one of: 'fast', 'slow'"}
    assert write_cap == {}


def test_invalid_diagnostic_enum_is_rejected_before_dispatch(monkeypatch):
    manifest = _manifest()
    diagnostic = dict(manifest["operations"][0])
    diagnostic.update(
        name="demo_probe_widgets",
        key="POST /api/v1/orgs/{org_id}/widgets/probe",
        method="POST",
        path="/api/v1/orgs/{org_id}/widgets/probe",
        operation_id="probeWidgets",
        capability="diagnostic",
        summary="Probe widgets",
    )
    manifest["operations"] = [diagnostic]
    server = MCPServer("demo-diagnostic-enum")
    captured: dict = {}
    monkeypatch.setenv("HPE_MCP_DEMO_GENERATED_TOOLS", "1")
    register_generated_tools(
        server,
        "demo",
        read_executor=_fake_read_executor({}),
        write_executor=_fake_write_executor(captured),
        manifest=manifest,
    )

    out = asyncio.run(
        server._tool_manager._tools["demo_probe_widgets"].fn(
            org_id="o1",
            mode="turbo",
        )
    )

    assert out == {"error": "parameter 'mode' must be one of: 'fast', 'slow'"}
    assert captured == {}


def test_incompatible_enum_metadata_keeps_declared_parameter_type(monkeypatch):
    manifest = _manifest()
    read = manifest["operations"][0]
    read["parameters"] = [
        *read["parameters"],
        {
            "name": "legacy-flag",
            "in": "query",
            "required": False,
            "type": "boolean",
            "enum": ["false", "true"],
            "default": False,
        },
    ]
    server = MCPServer("demo-incompatible-enum")
    captured: dict = {}
    monkeypatch.setenv("HPE_MCP_DEMO_GENERATED_TOOLS", "1")
    register_generated_tools(
        server,
        "demo",
        read_executor=_fake_read_executor(captured),
        write_executor=_fake_write_executor({}),
        manifest=manifest,
    )

    tool = server._tool_manager._tools["demo_list_widgets"]
    schema = (tool.parameters.get("properties") or {})["legacy_flag"]
    variants = schema.get("anyOf", [schema])
    assert any(variant.get("type") == "boolean" for variant in variants)
    assert not any("enum" in variant for variant in variants)

    out = asyncio.run(tool.fn(org_id="o1", legacy_flag=False))

    assert out["status_code"] == 200
    assert captured["query"]["legacy-flag"] is False


def test_large_enum_is_enforced_without_expanding_tool_schema(monkeypatch):
    manifest = _manifest()
    read = manifest["operations"][0]
    choices = [f"choice-{index}" for index in range(21)]
    read["parameters"] = [
        *read["parameters"],
        {
            "name": "large-mode",
            "in": "query",
            "required": False,
            "type": "string",
            "enum": choices,
        },
    ]
    server = MCPServer("demo-large-enum")
    captured: dict = {}
    monkeypatch.setenv("HPE_MCP_DEMO_GENERATED_TOOLS", "1")
    register_generated_tools(
        server,
        "demo",
        read_executor=_fake_read_executor(captured),
        write_executor=_fake_write_executor({}),
        manifest=manifest,
    )

    tool = server._tool_manager._tools["demo_list_widgets"]
    schema = (tool.parameters.get("properties") or {})["large_mode"]
    variants = schema.get("anyOf", [schema])
    assert any(variant.get("type") == "string" for variant in variants)
    assert not any("enum" in variant for variant in variants)

    out = asyncio.run(tool.fn(org_id="o1", large_mode="invalid"))

    assert out["error"].startswith(
        "parameter 'large-mode' must be one of: 'choice-0', 'choice-1'"
    )
    assert out["error"].endswith(", ... (21 total)")
    assert captured == {}


def test_read_post_exposes_required_body_without_write_controls(monkeypatch):
    manifest = _manifest()
    read_post = dict(manifest["operations"][1])
    read_post.update(
        name="demo_search_widgets",
        operation_id="searchWidgets",
        capability="read",
        summary="Search widgets",
    )
    manifest["operations"].append(read_post)
    server = MCPServer("demo-read-body")
    captured: dict = {}
    monkeypatch.setenv("HPE_MCP_DEMO_GENERATED_TOOLS", "1")
    register_generated_tools(
        server,
        "demo",
        read_executor=_fake_read_executor(captured),
        write_executor=_fake_write_executor({}),
        manifest=manifest,
    )

    tool = server._tool_manager._tools["demo_search_widgets"]
    props = tool.parameters.get("properties") or {}
    assert tool.annotations.read_only_hint is True
    assert "body" in (tool.parameters.get("required") or [])
    assert "dry_run" not in props
    assert "confirm" not in props

    out = asyncio.run(tool.fn(org_id="o1", body={"name": "leaf"}))
    assert out["status_code"] == 200
    assert captured["method"] == "POST"
    assert captured["body"] == {"name": "leaf"}
    assert captured["content_type"] == "application/json"


def test_http_read_executor_applies_json_body(monkeypatch):
    captured: dict = {}

    class Response:
        status_code = 200
        content = b'{"ok": true}'
        text = '{"ok": true}'
        headers = {"content-type": "application/json"}

        def json(self):
            return {"ok": True}

    class FakeClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, **kwargs):
            captured.update(method=method, url=url, kwargs=kwargs)
            return Response()

    async def resolve(path, headers):
        return "https://example.test", {
            "Authorization": "Bearer secret",
            **headers,
        }

    monkeypatch.setattr(http_exec.httpx, "AsyncClient", FakeClient)
    executor = make_read_executor(
        resolve=resolve,
        allowed_prefixes=lambda: ("/api/",),
        not_configured="not configured",
    )
    out = asyncio.run(
        executor(
            "POST",
            "/api/search",
            {"limit": 5},
            {"X-Trace": "trace"},
            {"name": "leaf"},
            "application/json",
        )
    )

    assert out["status_code"] == 200
    assert captured["kwargs"]["json"] == {"name": "leaf"}
    assert captured["kwargs"]["params"] == {"limit": 5}
    assert captured["kwargs"]["headers"]["X-Trace"] == "trace"


def test_read_path_escaping_and_traversal_rejection(monkeypatch):
    server, _, read_cap, _ = _register_demo(monkeypatch)
    fn = server._tool_manager._tools["demo_list_widgets"].fn
    asyncio.run(fn(org_id="a b"))
    assert read_cap["path"] == "/api/v1/orgs/a%20b/widgets"
    out = asyncio.run(fn(org_id="a/b"))
    assert "error" in out


def test_write_dispatch_passes_body_and_flags(monkeypatch):
    server, _, _, write_cap = _register_demo(monkeypatch)
    fn = server._tool_manager._tools["demo_create_widget"].fn
    asyncio.run(fn(org_id="o1", body={"a": "x"}, dry_run=True))
    assert write_cap["name"] == "demo_create_widget"
    assert write_cap["body"] == {"a": "x"}
    assert write_cap["dry_run"] is True
    assert write_cap["content_type"] == "application/json"


# ---------------------------------------------------------------------------
# Mist integration proof
# ---------------------------------------------------------------------------

def test_mist_manifest_committed_and_counts():
    assert manifest_operation_count("mist") == 1050
    assert len(mist.GENERATED_MIST_TOOLS) == 1050


def test_mist_generated_tools_registered_on_backend():
    tools = mist.mcp._tool_manager._tools
    # curated + generated
    assert "mist_status" in tools  # curated
    assert "mist_list_ap_channels" in tools  # generated read
    assert len(tools) >= 1076


def _fake_httpx(monkeypatch, captured, payload=None):
    class Resp:
        status_code = 200
        text = "{}"

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
            return Resp()

    monkeypatch.setattr(mist.httpx, "AsyncClient", FakeClient)


def test_mist_generated_read_dispatch_bounds_and_injects_auth(monkeypatch):
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    cap: dict = {}
    _fake_httpx(monkeypatch, cap, payload={"results": [1, 2, 3, 4]})
    fn = mist.mcp._tool_manager._tools["mist_list_ap_channels"].fn
    out = asyncio.run(fn(country_code="US"))
    assert cap["url"] == "https://api.mist.com/api/v1/const/ap_channels"
    assert cap["headers"]["Authorization"] == "Token secret"
    assert cap["params"] == {"country_code": "US"}
    # Response bounding applied.
    assert "_pagination" in out["data"]


def test_mist_generated_read_honors_requested_output_limit(monkeypatch):
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    cap: dict = {}
    _fake_httpx(monkeypatch, cap, payload=list(range(150)))
    fn = mist.mcp._tool_manager._tools[
        "mist_list_installer_list_of_recently_claimed_devices"
    ].fn

    out = asyncio.run(fn(org_id="o1", limit=120))

    assert len(out["data"]["items"]) == 120
    assert out["data"]["_pagination"]["limit"] == 120


def test_mist_public_and_session_auth_modes(monkeypatch):
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.delenv("MIST_API_TOKEN", raising=False)
    monkeypatch.delenv("MIST_SESSION_COOKIE", raising=False)
    cap: dict = {}
    _fake_httpx(monkeypatch, cap)

    public = mist.mcp._tool_manager._tools["mist_get_admin_registration_info"].fn
    out = asyncio.run(public())
    assert out["status_code"] == 200
    assert "Authorization" not in cap["headers"]
    assert "Cookie" not in cap["headers"]

    monkeypatch.setenv("MIST_SESSION_COOKIE", "csrftoken=cookie-token; sessionid=session")
    monkeypatch.setenv("MIST_CSRF_TOKEN", "csrf-token")
    protected = mist.mcp._tool_manager._tools["mist_list_ap_channels"].fn
    out = asyncio.run(protected(country_code="US"))
    assert out["status_code"] == 200
    assert cap["headers"]["Cookie"].startswith("csrftoken=")
    assert cap["headers"]["X-CSRFToken"] == "csrf-token"
    assert "Authorization" not in cap["headers"]


def test_mist_generated_write_blocked_by_default(monkeypatch):
    monkeypatch.delenv("HPE_MCP_MIST_WRITES", raising=False)
    monkeypatch.delenv("HPE_MCP_PRODUCT_ACCESS", raising=False)
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    fn = mist.mcp._tool_manager._tools["mist_claim_installer_devices"].fn
    out = asyncio.run(fn(org_id="o1", body=["CODE"]))
    assert out["status"] == "blocked"


def test_mist_generated_write_dry_run_redacts(monkeypatch):
    monkeypatch.setenv("HPE_MCP_MIST_WRITES", "1")
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    fn = mist.mcp._tool_manager._tools["mist_claim_installer_devices"].fn
    out = asyncio.run(fn(org_id="o1", body=["CODE"], dry_run=True))
    assert out["dry_run"] is True
    assert out["url"] == "https://api.mist.com/api/v1/installer/orgs/o1/devices"


def test_side_effecting_mist_get_uses_write_gate(monkeypatch):
    monkeypatch.delenv("HPE_MCP_MIST_WRITES", raising=False)
    monkeypatch.delenv("HPE_MCP_PRODUCT_ACCESS", raising=False)
    tool = mist.mcp._tool_manager._tools["mist_optimize_installer_rrm"]
    assert tool.annotations.read_only_hint is not True
    assert tool.annotations.idempotent_hint is False
    out = asyncio.run(tool.fn(site_name="lab"))
    assert out["status"] == "blocked"


def test_mist_diagnostic_post_executes_without_write_gate(monkeypatch):
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")
    cap: dict = {}
    _fake_httpx(monkeypatch, cap)
    tool = mist.mcp._tool_manager._tools["mist_ping_from_device"]

    assert "dry_run" not in tool.parameters["properties"]
    assert tool.annotations.title == "Diagnostic"
    out = asyncio.run(
        tool.fn(site_id="s1", device_id="d1", body={"host": "192.0.2.1"})
    )

    assert out["status_code"] == 200
    assert cap["method"] == "POST"


def test_generated_post_is_not_marked_idempotent():
    tool = mist.mcp._tool_manager._tools["mist_claim_installer_devices"]
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.idempotent_hint is False


def test_mist_generated_multipart_uses_httpx_files(monkeypatch):
    monkeypatch.setenv("HPE_MCP_MIST_WRITES", "1")
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    cap: dict = {}
    _fake_httpx(monkeypatch, cap)
    fn = mist.mcp._tool_manager._tools["mist_import_org_maps"].fn
    out = asyncio.run(
        fn(
            org_id="o1",
            body={"file": "map-data", "json": {"site_name": "lab"}},
            dry_run=False,
            confirm=True,
        )
    )
    assert out["status_code"] == 200
    files = cap["kw"]["files"]
    assert files["file"] == (None, "map-data")
    assert files["json"] == (None, '{"site_name": "lab"}', "application/json")
    assert "Content-Type" not in cap["headers"]

    asyncio.run(
        fn(
            org_id="o1",
            body={
                "file": {
                    "filename": "map.png",
                    "content_base64": base64.b64encode(b"image").decode(),
                    "content_type": "image/png",
                }
            },
            dry_run=False,
            confirm=True,
        )
    )
    assert cap["kw"]["files"]["file"] == ("map.png", b"image", "image/png")


def test_mist_generated_binary_download_is_bounded(monkeypatch):
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")

    class Resp:
        status_code = 200
        headers = {"content-type": "application/octet-stream"}
        content = b"x" * 200_000
        text = ""

        def json(self):
            raise ValueError("not json")

    class FakeClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, headers=None, params=None, **kwargs):
            return Resp()

    monkeypatch.setattr(mist.httpx, "AsyncClient", FakeClient)
    fn = mist.mcp._tool_manager._tools["mist_download_site_rfdiag_recording"].fn
    out = asyncio.run(fn(site_id="s1", rfdiag_id="r1"))
    payload = out["data"]
    assert payload["size_bytes"] == 200_000
    assert payload["truncated"] is True
    assert len(payload["base64"]) < 200_000


# ---------------------------------------------------------------------------
# Generation CLI drift/check mode (skipped when the local spec is absent)
# ---------------------------------------------------------------------------

def test_committed_mist_manifest_matches_fresh_build():
    from pathlib import Path

    from hpe_networking_mcp.mcp_servers.openapi_gen import manifest as manifest_mod
    from scripts import generate_openapi_tools as cli

    spec_path = Path(cli._REPO_ROOT) / cli._DEFAULT_SPECS["mist"]
    if not spec_path.exists():
        pytest.skip("local Mist spec not present (gitignored)")
    fresh = manifest_mod.dumps(cli.build("mist", spec_path))
    committed = manifest_mod.manifest_path("mist").read_text()
    assert fresh == committed, "committed Mist manifest is stale; re-run generate_openapi_tools.py"


def test_manifest_preserves_openapi_lifecycle_and_schema_metadata():
    from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import build_manifest

    spec = {
        "openapi": "3.1.0",
        "info": {"title": "demo", "version": "1"},
        "security": [{"oauth": ["read"]}],
        "paths": {
            "/items": {
                "post": {
                    "operationId": "createItem",
                    "deprecated": True,
                    "x-sunset-date": "2027-01-01",
                    "parameters": [
                        {
                            "name": "fields",
                            "in": "query",
                            "style": "form",
                            "explode": False,
                            "schema": {
                                "type": "array",
                                "items": {"type": "string", "format": "hostname"},
                            },
                        }
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["name"],
                                    "properties": {
                                        "name": {"type": "string"},
                                        "created": {
                                            "type": "string",
                                            "format": "date-time",
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"201": {"description": "created"}},
                }
            }
        },
    }

    operation = build_manifest(
        spec,
        platform="demo",
        source_file="demo.json",
        source_sha256="0" * 64,
    )["operations"][0]

    assert operation["deprecated"] is True
    assert operation["sunset"] == "2027-01-01"
    assert operation["security"] == [{"oauth": ["read"]}]
    assert operation["response_codes"] == ["201"]
    assert operation["parameters"][0]["style"] == "form"
    assert operation["parameters"][0]["explode"] is False
    assert operation["request_body"]["required_properties"] == ["name"]
    assert operation["request_body"]["property_formats"] == {
        "created": "date-time"
    }


def test_generated_write_retry_does_not_replay_post(monkeypatch):
    import asyncio

    import hpe_networking_mcp.mcp_servers.openapi_gen.http_exec as http_exec

    calls: list[str] = []

    class Response:
        status_code = 503
        headers = {}
        content = b"{}"
        text = "{}"

        def json(self):
            return {"error": "unavailable"}

    class Client:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, **kwargs):
            calls.append(method)
            return Response()

    async def resolve(path, headers):
        return "https://api.example.com", headers

    monkeypatch.setattr(http_exec.httpx, "AsyncClient", Client)
    execute = http_exec.make_write_executor(
        resolve=resolve,
        allowed_prefixes=lambda: ("/api/",),
        writes_allowed=lambda: True,
        blocked_response=lambda name: {"status": "blocked"},
        execute_hint="confirm",
    )

    result = asyncio.run(
        execute(
            "create_item",
            "POST",
            "/api/items",
            {},
            {},
            {"name": "one"},
            "application/json",
            False,
            True,
        )
    )

    assert result["status_code"] == 503
    assert calls == ["POST"]


def test_generated_write_auth_refresh_does_not_replay_post(monkeypatch):
    calls: list[str] = []
    refreshes: list[bool] = []

    class Response:
        status_code = 401
        headers = {"content-type": "application/json"}
        content = b"{}"
        text = "{}"

        def json(self):
            return {"error": "unauthorized"}

    class Client:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, **kwargs):
            calls.append(method)
            return Response()

    async def resolve(path, headers):
        return "https://api.example.com", headers

    async def refresh_auth():
        refreshes.append(True)

    monkeypatch.setattr(http_exec.httpx, "AsyncClient", Client)
    execute = http_exec.make_write_executor(
        resolve=resolve,
        allowed_prefixes=lambda: ("/api/",),
        writes_allowed=lambda: True,
        blocked_response=lambda name: {"status": "blocked"},
        execute_hint="confirm",
        refresh_auth=refresh_auth,
    )

    result = asyncio.run(
        execute(
            "create_item",
            "POST",
            "/api/items",
            {},
            {},
            {"name": "one"},
            "application/json",
            False,
            True,
        )
    )

    assert result["status_code"] == 401
    assert calls == ["POST"]
    assert refreshes == []


def test_generated_read_retry_honors_short_retry_after(monkeypatch):
    calls: list[str] = []
    sleeps: list[float] = []

    class Response:
        content = b"{}"
        text = "{}"

        def __init__(self, status_code, headers):
            self.status_code = status_code
            self.headers = headers

        def json(self):
            return {"ok": self.status_code == 200}

    responses = [
        Response(429, {"Retry-After": "2"}),
        Response(200, {"content-type": "application/json"}),
    ]

    class Client:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, **kwargs):
            calls.append(method)
            return responses.pop(0)

    async def resolve(path, headers):
        return "https://api.example.com", headers

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(http_exec.httpx, "AsyncClient", Client)
    monkeypatch.setattr(http_exec.asyncio, "sleep", sleep)
    execute = make_read_executor(
        resolve=resolve,
        allowed_prefixes=lambda: ("/api/",),
    )

    result = asyncio.run(execute("GET", "/api/items", {}, {}))

    assert result["status_code"] == 200
    assert calls == ["GET", "GET"]
    assert sleeps == [2.0]


def test_generated_read_does_not_retry_before_long_retry_after(monkeypatch):
    calls: list[str] = []

    class Response:
        status_code = 429
        headers = {
            "Retry-After": "60",
            "content-type": "application/json",
        }
        content = b"{}"
        text = "{}"

        def json(self):
            return {"error": "rate limited"}

    class Client:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, **kwargs):
            calls.append(method)
            return Response()

    async def resolve(path, headers):
        return "https://api.example.com", headers

    async def unexpected_sleep(delay):
        raise AssertionError(f"must not sleep for long Retry-After hint: {delay}")

    monkeypatch.setattr(http_exec.httpx, "AsyncClient", Client)
    monkeypatch.setattr(http_exec.asyncio, "sleep", unexpected_sleep)
    execute = make_read_executor(
        resolve=resolve,
        allowed_prefixes=lambda: ("/api/",),
    )

    result = asyncio.run(execute("GET", "/api/items", {}, {}))

    assert result["status_code"] == 429
    assert calls == ["GET"]
