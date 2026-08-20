"""Regression coverage for `list_glp_api_families()`'s tool inventory.

Guards against the inventory silently drifting out of sync with the actual
curated `@mcp.tool` surface in `glp.py` (as happened for the 10 service-catalog
reads and 4 device/subscription writes fixed here): every curated tool must
be discoverable through exactly one of `curated_manifest_backed_tools`,
`region_aware_family_tools`, or `best_effort_typed_tools`, and the four
guarded device/subscription writes must keep their write-gate semantics
(fail-closed without HPE_MCP_GLP_V2BETA1_WRITES=1) regardless of which
inventory bucket they are listed under.
"""

from __future__ import annotations

import re
from pathlib import Path

from hpe_networking_mcp.mcp_servers import glp
from hpe_networking_mcp.pipeline.clients.glp_client import _V2BETA1_WRITES_FLAG

# Tool decorators followed by a `def name(...)` line, mirroring how the
# inventory itself is authored (module source, not the live tool registry,
# so this also catches drift if generated tools were ever mixed in).
_TOOL_DEF_RE = re.compile(r"@mcp\.tool\([^\n]*\)\n(?:async )?def (\w+)")

# Meta/introspection tools that describe the inventory rather than being
# entries within it.
_META_TOOLS = {"glp_preflight", "glp_write_status", "list_glp_api_families"}

# The 10 service-catalog reads added to close the Reporting/Service-Catalog
# coverage gap (list_glp_api_families previously omitted these even though
# the underlying tools/manifest operations already existed).
NEW_SERVICE_CATALOG_TOOLS = (
    "list_glp_service_offer_regions",
    "get_glp_service_offer_region",
    "list_glp_service_provisions",
    "get_glp_service_provision",
    "list_glp_service_managers",
    "get_glp_service_manager",
    "list_glp_service_manager_provisions",
    "get_glp_service_manager_provision",
    "list_glp_per_region_service_managers",
    "get_glp_service_managers_for_region",
)

# The 4 curated device/subscription write tools reclassified from "missing
# entirely" into curated_manifest_backed_tools (manifest-backed: POST
# /devices/v1/devices and PATCH /devices/v2beta1/devices are both real,
# manifest-conformance-tested operations -- see
# tests/unit/test_glp_client_manifest_conformance.py -- unlike
# glp_add_subscriptions, whose nested body shape is explicitly flagged
# unconfirmed and stays in best_effort_typed_tools).
DEVICE_SUBSCRIPTION_WRITE_TOOLS = (
    "glp_add_device",
    "glp_add_devices_bulk",
    "glp_archive_device",
    "glp_assign_subscription",
)


def _all_curated_tool_names() -> set[str]:
    src = Path(glp.__file__).read_text(encoding="utf-8")
    return set(_TOOL_DEF_RE.findall(src))


def test_families_inventory_lists_new_service_catalog_reads():
    result = glp.list_glp_api_families()
    curated = result["curated_manifest_backed_tools"]
    for tool_name in NEW_SERVICE_CATALOG_TOOLS:
        assert tool_name in curated, tool_name


def test_families_inventory_lists_device_subscription_writes_as_manifest_backed():
    result = glp.list_glp_api_families()
    curated = result["curated_manifest_backed_tools"]
    best_effort = result["best_effort_typed_tools"]
    for tool_name in DEVICE_SUBSCRIPTION_WRITE_TOOLS:
        assert tool_name in curated, tool_name
        # Manifest-backed and best-effort are mutually exclusive classifications.
        assert tool_name not in best_effort, tool_name


def test_families_inventory_has_no_missing_or_stale_curated_tools():
    """Every curated `@mcp.tool` must appear in exactly one inventory bucket,
    and every inventory entry must correspond to a real curated tool."""
    result = glp.list_glp_api_families()
    covered = (
        set(result["curated_manifest_backed_tools"])
        | set(result["region_aware_family_tools"])
        | set(result["best_effort_typed_tools"])
        | {"glp_get", "plan_glp_reconciliation"}
        | _META_TOOLS
    )
    actual = _all_curated_tool_names()

    missing = sorted(actual - covered)
    stale = sorted(covered - actual)
    assert missing == [], f"curated tools missing from list_glp_api_families(): {missing}"
    assert stale == [], f"list_glp_api_families() references non-existent tools: {stale}"


def test_device_subscription_writes_stay_gated_without_writes_flag(monkeypatch):
    """Reclassifying these tools into curated_manifest_backed_tools must not
    change their fail-closed write-gate behavior."""
    monkeypatch.delenv(_V2BETA1_WRITES_FLAG, raising=False)

    add_device_result = glp.glp_add_device("SERIAL1")
    assert add_device_result["status"] == "FORBIDDEN"
    assert add_device_result["flag"] == _V2BETA1_WRITES_FLAG

    bulk_result = glp.glp_add_devices_bulk([{"serialNumber": "SERIAL1", "macAddress": "AA:BB"}])
    assert bulk_result["status"] == "FORBIDDEN"

    archive_result = glp.glp_archive_device("SERIAL1")
    assert archive_result["status"] == "FORBIDDEN"

    assign_result = glp.glp_assign_subscription("SERIAL1", "SUB-KEY-1")
    assert assign_result["status"] == "FORBIDDEN"

    status = glp.glp_write_status()
    assert status["enabled"] is False
    for tool_name in DEVICE_SUBSCRIPTION_WRITE_TOOLS:
        assert tool_name in status["guarded_tools"]
