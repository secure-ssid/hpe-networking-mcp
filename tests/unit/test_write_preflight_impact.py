"""Unit tests for dry-run preflight impact analysis.

Regression coverage for a real near-miss: Mist's ``PUT`` merges top-level keys
but *replaces* any nested object it receives. Sending a single port profile
under ``port_usages`` therefore deletes every other profile in that object --
including switch uplinks and inter-switch links -- while the dry-run preview
looks completely benign, because it only echoes the request body back.

These tests pin the diff semantics and the fail-open guarantees.
"""

from __future__ import annotations

import asyncio

from hpe_networking_mcp.mcp_servers.openapi_gen.preflight import (
    build_write_impact,
    extract_resource,
    nested_replace_impact,
)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# nested_replace_impact
# ---------------------------------------------------------------------------

def test_top_level_omission_is_preserved_not_removed():
    """Top-level keys merge, so omitting one is not a deletion."""
    current = {"networks": {"vlan_5": {"vlan_id": 5}}, "port_usages": {"a": {}}}
    proposed = {"port_usages": {"a": {}}}
    impact = nested_replace_impact(current, proposed)
    assert "would_remove" not in impact


def test_nested_omission_is_reported_as_removal():
    """The real hazard: a partial nested object silently drops its siblings."""
    current = {
        "port_usages": {
            "ap_port": {"mode": "trunk"},
            "client_data": {"mode": "access"},
            "sw_trunk": {"mode": "trunk"},
            "cx_trunk_native_1": {"mode": "trunk"},
        }
    }
    proposed = {"port_usages": {"client_data": {"mode": "access"}}}
    impact = nested_replace_impact(current, proposed)
    assert impact["would_remove"] == [
        "port_usages.ap_port",
        "port_usages.cx_trunk_native_1",
        "port_usages.sw_trunk",
    ]
    assert "warning" not in impact  # warning is added by build_write_impact


def test_complete_nested_object_reports_no_removal():
    """Re-sending every sibling is the correct fix and must come back clean."""
    current = {"port_usages": {"a": {"mode": "access"}, "b": {"mode": "trunk"}}}
    proposed = {
        "port_usages": {
            "a": {"mode": "access", "enable_mac_auth": True},
            "b": {"mode": "trunk"},
        }
    }
    impact = nested_replace_impact(current, proposed)
    assert "would_remove" not in impact
    assert impact["would_add"] == ["port_usages.a.enable_mac_auth"]


def test_changes_and_additions_are_reported():
    current = {"cfg": {"enabled": False, "timeout": 5}}
    proposed = {"cfg": {"enabled": True, "timeout": 5, "retries": 3}}
    impact = nested_replace_impact(current, proposed)
    assert impact["would_change"] == ["cfg.enabled"]
    assert impact["would_add"] == ["cfg.retries"]
    assert "would_remove" not in impact


def test_masked_secrets_do_not_produce_false_changes():
    """Redaction middleware masks secrets; a mask must not read as a change."""
    current = {"switch_mgmt": {"root_password": "******", "timer": 10}}
    proposed = {"switch_mgmt": {"root_password": "hunter2", "timer": 10}}
    impact = nested_replace_impact(current, proposed)
    assert impact == {}


def test_server_managed_fields_are_ignored():
    current = {"cfg": {"a": 1}, "id": "x", "modified_time": 1, "org_id": "o"}
    proposed = {"cfg": {"a": 1}}
    assert nested_replace_impact(current, proposed) == {}


def test_large_diffs_are_truncated():
    current = {"m": {f"k{i}": i for i in range(120)}}
    proposed = {"m": {"k0": 0}}
    impact = nested_replace_impact(current, proposed)
    assert len(impact["would_remove"]) == 50
    assert impact["would_remove_truncated"] == 69


# ---------------------------------------------------------------------------
# extract_resource
# ---------------------------------------------------------------------------

def test_extract_resource_unwraps_data_envelope():
    assert extract_resource({"status_code": 200, "data": {"a": 1}}) == {"a": 1}


def test_extract_resource_rejects_collections_and_errors():
    assert extract_resource({"status_code": 200, "data": {"items": [1]}}) is None
    assert extract_resource({"error": "boom"}) is None
    assert extract_resource({"status_code": 404, "data": {"a": 1}}) is None
    assert extract_resource("not-a-dict") is None


# ---------------------------------------------------------------------------
# build_write_impact
# ---------------------------------------------------------------------------

def test_build_write_impact_flags_the_port_usages_regression():
    """End-to-end replay of the near-miss that motivated this module."""
    current = {
        "status_code": 200,
        "data": {
            "port_usages": {
                "ap_port": {"mode": "trunk"},
                "client_data": {"mode": "access"},
                "sw_trunk": {"mode": "trunk"},
            },
            "networks": {"vlan_5": {"vlan_id": 5}},
        },
    }

    async def read_executor(method, path, query, headers):
        assert method == "GET"
        return current

    impact = _run(
        build_write_impact(
            read_executor,
            "PUT",
            "/api/v1/orgs/o/networktemplates/t",
            {},
            {"port_usages": {"client_data": {"mode": "access"}}},
        )
    )
    assert impact["would_remove"] == ["port_usages.ap_port", "port_usages.sw_trunk"]
    assert "DELETED" in impact["warning"]
    assert impact["source"] == "GET /api/v1/orgs/o/networktemplates/t"


def test_build_write_impact_skips_non_merge_methods():
    async def read_executor(*_args):  # pragma: no cover - must not be called
        raise AssertionError("POST must not trigger a preflight read")

    assert _run(build_write_impact(read_executor, "POST", "/p", {}, {"a": {}})) is None


def test_build_write_impact_fails_open_on_read_error():
    """A broken preflight must never obstruct or annotate the write."""

    async def read_executor(*_args):
        raise RuntimeError("network down")

    assert _run(build_write_impact(read_executor, "PUT", "/p", {}, {"a": {"b": 1}})) is None


def test_build_write_impact_returns_none_for_non_dict_body():
    async def read_executor(*_args):  # pragma: no cover - must not be called
        raise AssertionError("non-dict body must not trigger a preflight read")

    assert _run(build_write_impact(read_executor, "PUT", "/p", {}, "raw")) is None
    assert _run(build_write_impact(read_executor, "PUT", "/p", {}, {})) is None


# ---------------------------------------------------------------------------
# runtime wiring
# ---------------------------------------------------------------------------

class _FakeToolManager:
    def __init__(self):
        self._tools = {}


class _FakeMCP:
    """Minimal stand-in for the MCPServer surface register_generated_tools uses."""

    def __init__(self):
        self._tool_manager = _FakeToolManager()
        self.tools = {}

    def add_tool(self, fn, *, name, description, annotations):
        self._tool_manager._tools[name] = fn
        self.tools[name] = fn


_MANIFEST = {
    "operations": [
        {
            "name": "get_thing",
            "method": "GET",
            "path": "/api/v1/things/{thing_id}",
            "capability": "read",
            "parameters": [
                {"name": "thing_id", "in": "path", "required": True, "schema_type": "string"}
            ],
        },
        {
            "name": "update_thing",
            "method": "PUT",
            "path": "/api/v1/things/{thing_id}",
            "capability": "write",
            "parameters": [
                {"name": "thing_id", "in": "path", "required": True, "schema_type": "string"}
            ],
            "request_body": {"content_type": "application/json", "schema_type": "object"},
        },
        {
            "name": "make_thing",
            "method": "POST",
            "path": "/api/v1/things",
            "capability": "write",
            "parameters": [],
            "request_body": {"content_type": "application/json", "schema_type": "object"},
        },
    ]
}


def _register(monkeypatch_env=None):
    from hpe_networking_mcp.mcp_servers.openapi_gen.runtime import register_generated_tools

    stored = {
        "status_code": 200,
        "data": {"port_usages": {"a": {"m": 1}, "b": {"m": 2}, "c": {"m": 3}}},
    }

    async def read_executor(method, path, query, headers, *rest):
        return stored

    async def write_executor(
        name, method, path, query, headers, body, content_type, dry_run, confirm
    ):
        return {"dry_run": True, "method": method, "path": path, "json": body}

    mcp = _FakeMCP()
    register_generated_tools(
        mcp,
        "testplat",
        read_executor=read_executor,
        write_executor=write_executor,
        manifest=_MANIFEST,
        flag_env="HPE_MCP_TEST_PREFLIGHT_FLAG",
    )
    return mcp


def test_generated_put_dry_run_is_annotated_with_impact(monkeypatch):
    monkeypatch.setenv("HPE_MCP_TEST_PREFLIGHT_FLAG", "1")
    mcp = _register()
    result = _run(
        mcp.tools["update_thing"](thing_id="t1", body={"port_usages": {"a": {"m": 1}}})
    )
    assert result["dry_run"] is True
    assert result["impact"]["would_remove"] == ["port_usages.b", "port_usages.c"]


def test_generated_post_dry_run_has_no_impact(monkeypatch):
    """POST creates; there is no prior state to clobber."""
    monkeypatch.setenv("HPE_MCP_TEST_PREFLIGHT_FLAG", "1")
    mcp = _register()
    result = _run(mcp.tools["make_thing"](body={"port_usages": {"a": {"m": 1}}}))
    assert "impact" not in result
