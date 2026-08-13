# ruff: noqa: E501

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import dumps
from scripts.generate_axis_manifest import (
    AxisSourceError,
    _expected_source_parameters,
    _function_metadata,
    build_axis_manifest,
    check_manifest,
    parse_registries,
    reviewed_operations,
    validate_registry_source,
    verify_source_digests,
)

# Verbatim pinned upstream source (nowireless4u/hpe-networking-mcp@a1b2afa,
# src/hpe_networking_mcp/platforms/axis/tools/connectors.py) — embedded so the
# split-verb source-provenance check below is fully offline-verifiable.
_PINNED_CONNECTORS_SOURCE = b'''
from __future__ import annotations

from typing import Any

from fastmcp import Context
from fastmcp.exceptions import ToolError

from hpe_networking_mcp.platforms._common.annotations import Capability
from hpe_networking_mcp.platforms._common.url import path_seg
from hpe_networking_mcp.platforms.axis._registry import tool
from hpe_networking_mcp.platforms.axis.client import format_http_error, get_axis_client
from hpe_networking_mcp.platforms.axis.tools._manage import manage_entity


@tool(capability=Capability.READ)
async def axis_get_connectors(
    ctx: Context,
    connector_id: str | None = None,
    page_number: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    try:
        client = await get_axis_client()
        if connector_id:
            return await client.get_json(f"/Connectors/{path_seg(connector_id)}")
        return await client.get_paged("/Connectors", page_number=page_number, page_size=page_size)
    except Exception as e:
        detail = format_http_error(e)
        raise ToolError({"status_code": 502, "message": f"Error fetching connectors: {detail}"}) from e


@tool(capability=Capability.WRITE_DELETE)
async def axis_manage_connector(
    ctx: Context,
    action_type: str,
    payload: dict | None = None,
    connector_id: str | None = None,
    confirmed: bool = False,
) -> dict:
    return await manage_entity(
        ctx,
        base_path="/Connectors",
        label="connector",
        action_type=action_type,
        payload=payload,
        entity_id=connector_id,
        confirmed=confirmed,
    )


@tool(capability=Capability.OPERATIONAL, enable_gated=True)
async def axis_regenerate_connector(
    ctx: Context,
    connector_id: str,
    confirmed: bool = False,
) -> dict:
    try:
        client = await get_axis_client()
        return await client.post_json(f"/Connectors/{path_seg(connector_id)}/regenerate", json_body={})
    except Exception as e:
        detail = format_http_error(e)
        raise ToolError({"status_code": 502, "message": f"Error regenerating connector: {detail}"}) from e
'''


def test_axis_manifest_generation_is_deterministic_and_current():
    first = build_axis_manifest()
    second = build_axis_manifest()
    committed = Path("src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/axis.json")

    assert dumps(first) == dumps(second)
    assert dumps(first) == committed.read_text()
    assert first["schema_version"] == 2
    assert first["source"]["operation_count"] == 47


def test_axis_source_digest_mismatch_is_rejected():
    sources = {"axis.py": b"reviewed"}
    expected = {"axis.py": "0" * 64}

    with pytest.raises(AxisSourceError, match="digest mismatch"):
        verify_source_digests(sources, expected_digests=expected)


def test_axis_registry_source_changes_are_detected():
    source = b"""
TOOLS = {"status": ["axis_get_new_status"]}
_DISABLED_TOOLS = {
    "custom_ip_categories": [
        "axis_get_custom_ip_categories",
        "axis_manage_custom_ip_category",
    ],
    "ip_feed_categories": [
        "axis_get_ip_feed_categories",
        "axis_manage_ip_feed_category",
    ],
}
"""

    with pytest.raises(AxisSourceError, match="enabled TOOLS registry changed"):
        validate_registry_source(source)


def test_axis_stale_manifest_detection(tmp_path):
    path = tmp_path / "axis.json"
    stale = build_axis_manifest()
    stale["operations"][0]["summary"] = "stale"
    path.write_text(json.dumps(stale))

    with pytest.raises(AxisSourceError, match="is stale"):
        check_manifest(path)


def test_axis_registry_parser_requires_both_registries():
    source = b'TOOLS = {"status": ["axis_get_status"]}\n'

    with pytest.raises(AxisSourceError, match="_DISABLED_TOOLS is missing"):
        parse_registries(source)


def test_axis_manage_split_shares_source_name_for_provenance():
    """The generated create/update/delete operations for one entity are three
    distinct tool surfaces, but all point back at the single upstream fused
    ``axis_manage_*`` function for source-provenance validation."""
    ops = {op["name"]: op for op in reviewed_operations()}

    assert "axis_manage_connector" not in ops
    fused_signature = ["ctx", "action_type", "payload", "connector_id", "confirmed"]
    for verb, method, capability in (
        ("create", "POST", "write"),
        ("update", "PUT", "write"),
        ("delete", "DELETE", "destructive"),
    ):
        op = ops[f"axis_{verb}_connector"]
        assert op["source_name"] == "axis_manage_connector"
        assert op["method"] == method
        assert op["capability"] == capability
        assert _expected_source_parameters(op) == fused_signature


def test_axis_split_operations_validate_against_real_pinned_manage_function():
    """Regression guard for the split-verb provenance check: parse the
    verbatim pinned upstream ``connectors.py`` source and confirm every
    split create/update/delete operation's expected signature, capability,
    and path match the single fused ``axis_manage_connector`` function that
    upstream actually ships (no live network access required)."""
    metadata = _function_metadata(_PINNED_CONNECTORS_SOURCE)
    found = metadata["axis_manage_connector"]

    assert found["capability"] == "WRITE_DELETE"
    assert "/Connectors" in found["paths"]

    ops = {op["name"]: op for op in reviewed_operations()}
    for verb in ("create", "update", "delete"):
        op = ops[f"axis_{verb}_connector"]
        assert _expected_source_parameters(op) == found["parameters"]
        assert op["path"] in found["paths"]
