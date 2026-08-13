"""Apstra derived operation set for the generated-tools manifest.

No distributable full Apstra OpenAPI document exists. This module records
method/path mappings verified against the pinned official Juniper
``aos-sdk-api`` package, then runs them through the shared manifest builder.
Auth endpoints are provenance-only and never become model-visible tools.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from hpe_networking_mcp.mcp_servers.openapi_gen import manifest as M  # noqa: E402

_BP = {
    "name": "blueprint_id",
    "in": "path",
    "required": True,
    "schema": {"type": "string"},
    "description": "Apstra blueprint ID.",
}
_CT = {
    "name": "policy_id",
    "in": "path",
    "required": True,
    "schema": {"type": "string"},
    "description": "Connectivity template ID.",
}
_TASK = {
    "name": "task_id",
    "in": "path",
    "required": True,
    "schema": {"type": "string"},
    "description": "Apstra blueprint task ID.",
}


def _op(operation_id: str, summary: str, tags: list[str] | None = None) -> dict:
    return {"operationId": operation_id, "summary": summary, "tags": tags or ["Apstra"]}


def _json_body() -> dict:
    return {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}}


def _resource_family(
    *,
    path: str,
    id_arg: str,
    resource_name: str,
    operation_prefix: str,
    tags: list[str],
    create: bool = True,
    update: bool = True,
    delete: bool = True,
) -> dict:
    """Build list/get/create/update/delete path entries for one top-level entity.

    Mirrors one ``aos_sdk_api._client.resources(...)`` call: every family here
    is confirmed (by inspecting the pinned wheel's ``_client.py``) to expose an
    explicit ``get_schema``/``collection_schema`` (list+get, always present)
    plus ``post_schema`` (create, only when ``create=True``) and
    ``put_schema`` (update, only when ``update=True``). ``delete`` defaults to
    True for entities the pinned SDK models with an explicit create/update
    schema pair (ordinary user-managed config objects); set it False only
    where the source has no writable schema at all (e.g. computed digests).
    """
    id_param = {
        "name": id_arg,
        "in": "path",
        "required": True,
        "schema": {"type": "string"},
        "description": f"Apstra {resource_name} ID.",
    }
    item_path = f"{path}/{{{id_arg}}}"
    collection: dict = {
        "get": _op(f"list{operation_prefix}", f"List Apstra {resource_name}s.", tags),
    }
    if create:
        collection["post"] = {
            **_op(f"create{operation_prefix}", f"Create an Apstra {resource_name}.", tags),
            "requestBody": _json_body(),
        }
    item: dict = {
        "parameters": [id_param],
        "get": _op(f"get{operation_prefix}", f"Get one Apstra {resource_name} by ID.", tags),
    }
    if update:
        item["put"] = {
            **_op(f"update{operation_prefix}", f"Update one Apstra {resource_name}.", tags),
            "requestBody": _json_body(),
        }
    if delete:
        item["delete"] = _op(
            f"delete{operation_prefix}", f"Delete one Apstra {resource_name}.", tags
        )
    return {path: collection, item_path: item}


def apstra_spec() -> dict:
    paths = {
        "/api/blueprints": {
            "get": _op("listBlueprints", "List Apstra blueprints (ID/name/status)."),
            "post": {
                **_op("createBlueprint", "Create an Apstra blueprint.", ["Blueprints"]),
                "requestBody": _json_body(),
            },
        },
        "/api/blueprints/{blueprint_id}": {
            "parameters": [_BP],
            "get": _op("getBlueprint", "Get one Apstra blueprint.", ["Blueprints"]),
            "patch": {
                **_op("updateBlueprint", "Update one Apstra blueprint.", ["Blueprints"]),
                "requestBody": _json_body(),
            },
            "delete": _op("deleteBlueprint", "Delete one Apstra blueprint.", ["Blueprints"]),
        },
        "/api/design/templates": {"get": _op("listDesignTemplates", "List Apstra design templates.")},
        "/api/blueprints/{blueprint_id}/anomalies": {
            "parameters": [_BP],
            "get": _op("listBlueprintAnomalies", "List anomalies for one blueprint."),
        },
        "/api/blueprints/{blueprint_id}/racks": {
            "parameters": [_BP],
            "get": _op("listBlueprintRacks", "List racks in one blueprint."),
        },
        "/api/blueprints/{blueprint_id}/security-zones": {
            "parameters": [_BP],
            "get": _op("listRoutingZones", "List routing zones (security-zones) in one blueprint."),
        },
        "/api/blueprints/{blueprint_id}/virtual-networks": {
            "parameters": [_BP],
            "get": _op("listVirtualNetworks", "List virtual networks in one blueprint."),
        },
        "/api/blueprints/{blueprint_id}/remote_gateways": {
            "parameters": [_BP],
            "get": _op("listRemoteGateways", "List remote gateways in one blueprint."),
        },
        "/api/blueprints/{blueprint_id}/deploy": {
            "parameters": [_BP],
            "get": _op("getBlueprintDeployStatus", "Get blueprint deployment status.", ["Blueprints"]),
            "put": {
                **_op("deployBlueprint", "Deploy a blueprint.", ["Blueprints"]),
                "requestBody": _json_body(),
            },
        },
        "/api/blueprints/{blueprint_id}/configuration": {
            "parameters": [_BP],
            "get": _op("getBlueprintConfigurationStatus", "Get blueprint configuration deployment status."),
        },
        "/api/blueprints/{blueprint_id}/preview-config-summary": {
            "parameters": [_BP],
            "get": _op("previewBlueprintConfiguration", "Preview and summarize generated device configurations."),
        },
        "/api/blueprints/{blueprint_id}/diff": {
            "parameters": [_BP],
            "get": _op("getBlueprintDiff", "Get the staged-versus-deployed blueprint diff."),
        },
        "/api/blueprints/{blueprint_id}/diff-status": {
            "parameters": [_BP],
            "get": _op("getBlueprintDiffStatus", "Get staged-vs-committed diff status for one blueprint."),
        },
        "/api/blueprints/{blueprint_id}/lock-status": {
            "parameters": [_BP],
            "get": _op("getBlueprintLockStatus", "Get blueprint lock status."),
        },
        "/api/blueprints/{blueprint_id}/lock-blueprint": {
            "parameters": [_BP],
            "put": _op("lockBlueprint", "Lock a blueprint.", ["Blueprints"]),
        },
        "/api/blueprints/{blueprint_id}/unlock-blueprint": {
            "parameters": [_BP],
            "put": _op("unlockBlueprint", "Unlock a blueprint.", ["Blueprints"]),
        },
        "/api/blueprints/{blueprint_id}/revert": {
            "parameters": [_BP],
            "post": _op("revertBlueprint", "Revert a blueprint to its latest backup.", ["Blueprints"]),
        },
        "/api/blueprints/{blueprint_id}/rollback": {
            "parameters": [_BP],
            "post": {
                **_op("rollbackBlueprint", "Rollback a blueprint to a selected revision.", ["Blueprints"]),
                "requestBody": _json_body(),
            },
        },
        "/api/blueprints/{blueprint_id}/revisions": {
            "parameters": [_BP],
            "get": _op("listBlueprintRevisions", "List blueprint revisions.", ["Blueprints"]),
        },
        "/api/blueprints/{blueprint_id}/tasks": {
            "parameters": [_BP],
            "get": _op("listBlueprintTasks", "List asynchronous blueprint tasks.", ["Tasks"]),
        },
        "/api/blueprints/{blueprint_id}/tasks/{task_id}": {
            "parameters": [_BP, _TASK],
            "get": _op("getBlueprintTask", "Get asynchronous blueprint task details.", ["Tasks"]),
        },
        "/api/blueprints/{blueprint_id}/acknowledge-tasks": {
            "parameters": [_BP],
            "post": {
                **_op("acknowledgeBlueprintTasks", "Acknowledge blueprint tasks.", ["Tasks"]),
                "requestBody": _json_body(),
            },
        },
        "/api/blueprints/{blueprint_id}/policy-types": {
            "parameters": [_BP],
            "get": _op("listConnectivityTemplateTypes", "List connectivity-template types.", ["Connectivity"]),
        },
        "/api/blueprints/{blueprint_id}/endpoint-policies": {
            "parameters": [_BP],
            "get": _op("listConnectivityTemplates", "List connectivity templates in one blueprint.", ["Connectivity"]),
            "post": {
                **_op("createConnectivityTemplate", "Create a connectivity template.", ["Connectivity"]),
                "requestBody": _json_body(),
            },
        },
        "/api/blueprints/{blueprint_id}/endpoint-policies/{policy_id}": {
            "parameters": [_BP, _CT],
            "get": _op("getConnectivityTemplate", "Get one connectivity template by ID.", ["Connectivity"]),
            "patch": {
                **_op("updateConnectivityTemplate", "Update a connectivity template.", ["Connectivity"]),
                "requestBody": _json_body(),
            },
            "delete": _op("deleteConnectivityTemplate", "Delete one connectivity template by ID.", ["Connectivity"]),
        },
        "/api/blueprints/{blueprint_id}/endpoint-policies/{policy_id}/application-points": {
            "parameters": [_BP, _CT],
            "get": _op(
                "getConnectivityTemplateApplicationPoints",
                "Get valid application points for one connectivity template.",
                ["Connectivity"],
            ),
            "patch": {
                **_op(
                    "setConnectivityTemplateApplicationPoints",
                    "Update one connectivity template's application points.",
                    ["Connectivity"],
                ),
                "requestBody": _json_body(),
            },
        },
        "/api/blueprints/{blueprint_id}/obj-policy-export": {
            "parameters": [_BP],
            "get": _op(
                "exportObjPolicy",
                "Export all connectivity-template definitions.",
                ["Connectivity"],
            ),
        },
        "/api/blueprints/{blueprint_id}/obj-policy-export/{policy_id}": {
            "parameters": [_BP, _CT],
            "get": _op("exportConnectivityTemplate", "Export one connectivity-template definition.", ["Connectivity"]),
        },
        "/api/blueprints/{blueprint_id}/obj-policy-import": {
            "parameters": [_BP],
            "put": {
                **_op("importConnectivityTemplates", "Import connectivity-template definitions.", ["Connectivity"]),
                "requestBody": _json_body(),
            },
        },
        "/api/blueprints/{blueprint_id}/obj-policy-application-points": {
            "parameters": [_BP],
            "post": _op(
                "listApplicationEndpoints",
                "List application endpoints (policy application points) - read-only query POST.",
                ["Connectivity"],
            ),
        },
        "/api/blueprints/{blueprint_id}/obj-policy-batch-apply": {
            "parameters": [_BP],
            "patch": {
                **_op(
                    "setApplicationPointAssignment",
                    "Batch-apply connectivity-template application point assignments.",
                    ["Connectivity"],
                ),
                "requestBody": _json_body(),
            },
        },
        "/api/blueprints/{blueprint_id}/obj-policy-batch-delete": {
            "parameters": [_BP],
            "post": {
                **_op("deleteConnectivityTemplates", "Delete a batch of top-level connectivity templates.", ["Connectivity"]),
                "requestBody": _json_body(),
            },
        },
        "/api/blueprints/{blueprint_id}/obj-policy-search": {
            "parameters": [_BP],
            "post": {
                **_op("searchConnectivityTemplates", "Search connectivity templates.", ["Connectivity"]),
                "requestBody": _json_body(),
            },
        },
        "/api/blueprints/{blueprint_id}/obj-policy-locations-schema": {
            "parameters": [_BP],
            "get": _op("getConnectivityLocationsSchema", "Get application-point location node types.", ["Connectivity"]),
        },
        "/api/blueprints/{blueprint_id}/experience/web/endpoint-policies": {
            "parameters": [_BP],
            "get": _op("getConnectivityTemplateStatus", "Get UI-oriented connectivity-template status.", ["Connectivity"]),
        },
        "/api/blueprints/{blueprint_id}/experience/web/obj-policies-by-application-points": {
            "parameters": [_BP],
            "post": {
                **_op(
                    "listConnectivityTemplatesByApplicationPoints",
                    "List connectivity templates for supplied application points.",
                    ["Connectivity"],
                ),
                "requestBody": _json_body(),
            },
        },
        "/api/blueprints/{blueprint_id}/protocol-sessions": {
            "parameters": [_BP],
            "get": _op("listProtocolSessions", "List protocol (BGP) sessions in one blueprint."),
        },
        "/api/blueprints/{blueprint_id}/experience/web/system-info": {
            "parameters": [_BP],
            "get": _op("getBlueprintSystemInfo", "Get managed system info for one blueprint."),
        },
        # ------------------------------------------------------------------
        # Resource pools (top-level, not blueprint-scoped). Confirmed via
        # aos_sdk_api._client.py `resources('/resources/<kind>-pools', ...,
        # post_schema=..., put_schema=...)` -- every one of the seven pool
        # kinds below has both post_schema and put_schema pinned in the SDK,
        # so create/update/delete are source-confirmed, not guessed.
        # ------------------------------------------------------------------
        **_resource_family(
            path="/api/resources/ip-pools",
            id_arg="pool_id",
            resource_name="IPv4 resource pool",
            operation_prefix="IpPool",
            tags=["Resources"],
        ),
        **_resource_family(
            path="/api/resources/ipv6-pools",
            id_arg="pool_id",
            resource_name="IPv6 resource pool",
            operation_prefix="Ipv6Pool",
            tags=["Resources"],
        ),
        **_resource_family(
            path="/api/resources/vlan-pools",
            id_arg="pool_id",
            resource_name="VLAN resource pool",
            operation_prefix="VlanPool",
            tags=["Resources"],
        ),
        **_resource_family(
            path="/api/resources/asn-pools",
            id_arg="pool_id",
            resource_name="ASN resource pool",
            operation_prefix="AsnPool",
            tags=["Resources"],
        ),
        **_resource_family(
            path="/api/resources/vni-pools",
            id_arg="pool_id",
            resource_name="VNI resource pool",
            operation_prefix="VniPool",
            tags=["Resources"],
        ),
        **_resource_family(
            path="/api/resources/integer-pools",
            id_arg="pool_id",
            resource_name="integer resource pool",
            operation_prefix="IntegerPool",
            tags=["Resources"],
        ),
        **_resource_family(
            path="/api/resources/device-pools",
            id_arg="pool_id",
            resource_name="device resource pool",
            operation_prefix="DevicePool",
            tags=["Resources"],
        ),
        # ------------------------------------------------------------------
        # Device / rack profiles (top-level). device-profiles/linecard-
        # profiles/chassis-profiles each pin post_schema+put_schema in the
        # SDK (full CRUD confirmed). device-profile-digests pins only
        # get_schema/collection_schema (computed data -- read-only, no
        # create/update/delete: explicit coverage gap, not a guess).
        # rack-types pins get_schema/collection_schema/put_schema but no
        # post_schema -- update/delete confirmed, create is an explicit,
        # documented coverage gap (see provenance note).
        # ------------------------------------------------------------------
        **_resource_family(
            path="/api/device-profiles",
            id_arg="device_profile_id",
            resource_name="device profile",
            operation_prefix="DeviceProfile",
            tags=["DeviceProfiles"],
        ),
        "/api/device-profile-clone": {
            "post": {
                **_op(
                    "cloneDeviceProfile",
                    "Clone an existing Apstra device profile.",
                    ["DeviceProfiles"],
                ),
                "requestBody": _json_body(),
            },
        },
        "/api/device-profile-digests": {
            "get": _op(
                "listDeviceProfileDigests",
                "List Apstra device profile digests (computed, read-only).",
                ["DeviceProfiles"],
            ),
        },
        "/api/device-profile-digests/{device_profile_id}": {
            "parameters": [
                {
                    "name": "device_profile_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                    "description": "Apstra device profile ID.",
                }
            ],
            "get": _op(
                "getDeviceProfileDigest",
                "Get one Apstra device profile digest (computed, read-only).",
                ["DeviceProfiles"],
            ),
        },
        **_resource_family(
            path="/api/linecard-profiles",
            id_arg="linecard_profile_id",
            resource_name="linecard profile",
            operation_prefix="LinecardProfile",
            tags=["DeviceProfiles"],
        ),
        **_resource_family(
            path="/api/chassis-profiles",
            id_arg="chassis_profile_id",
            resource_name="chassis profile",
            operation_prefix="ChassisProfile",
            tags=["DeviceProfiles"],
        ),
        **_resource_family(
            path="/api/design/rack-types",
            id_arg="rack_type_id",
            resource_name="rack type",
            operation_prefix="RackType",
            tags=["DeviceProfiles"],
            create=False,
        ),
        # ------------------------------------------------------------------
        # System agents (top-level). system-agents/system-agent-profiles pin
        # explicit create/update schemas in the SDK -- full CRUD confirmed.
        # system-agent (singleton manager-config) and system-agent-jobs
        # expose only the confirmed custom GET/PUT actions modeled by the
        # SDK, not a generic resource collection.
        # ------------------------------------------------------------------
        **_resource_family(
            path="/api/system-agents",
            id_arg="agent_id",
            resource_name="system agent",
            operation_prefix="SystemAgent",
            tags=["SystemAgents"],
        ),
        "/api/system-agent/manager-config": {
            "get": _op(
                "getSystemAgentManagerConfig",
                "Get the local system agent manager configuration.",
                ["SystemAgents"],
            ),
            "put": {
                **_op(
                    "updateSystemAgentManagerConfig",
                    "Update the local system agent manager configuration.",
                    ["SystemAgents"],
                ),
                "requestBody": _json_body(),
            },
        },
        "/api/system-agent-jobs/pending-jobs": {
            "get": _op(
                "listSystemAgentPendingJobs", "List pending system agent jobs.", ["SystemAgents"]
            ),
        },
        "/api/system-agent-jobs/active-jobs": {
            "get": _op(
                "listSystemAgentActiveJobs", "List active system agent jobs.", ["SystemAgents"]
            ),
        },
        **_resource_family(
            path="/api/system-agent-profiles",
            id_arg="profile_id",
            resource_name="system agent profile",
            operation_prefix="SystemAgentProfile",
            tags=["SystemAgents"],
        ),
        "/api/system-agent-profiles/{profile_id}/assign": {
            "parameters": [
                {
                    "name": "profile_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                    "description": "Apstra system agent profile ID.",
                }
            ],
            "post": {
                **_op(
                    "assignSystemAgentProfile",
                    "Assign a system agent profile to one or more system agents.",
                    ["SystemAgents"],
                ),
                "requestBody": _json_body(),
            },
        },
        # ------------------------------------------------------------------
        # Telemetry (top-level). telemetry-service-registry pins explicit
        # create/update schemas (full CRUD confirmed). telemetry-collectors
        # pins list/create/get/update schemas (confirmed) but no delete
        # schema was found for the per-collector resource -- delete is an
        # explicit, documented coverage gap rather than a guess.
        # ------------------------------------------------------------------
        **_resource_family(
            path="/api/telemetry-service-registry",
            id_arg="service_name",
            resource_name="telemetry service registry entry",
            operation_prefix="TelemetryServiceRegistryEntry",
            tags=["Telemetry"],
        ),
        **_resource_family(
            path="/api/telemetry/collectors",
            id_arg="service_name",
            resource_name="telemetry collector",
            operation_prefix="TelemetryCollector",
            tags=["Telemetry"],
            delete=False,
        ),
        # ------------------------------------------------------------------
        # IBA (Intent-Based Analytics), blueprint-scoped. Confirmed via the
        # pinned SDK's nested `.../blueprints/{id}/iba/...` classes. Widgets,
        # dashboard import/export, and predefined-probe instantiation are
        # deliberately NOT modeled here (documented coverage gap; see
        # provenance) -- only the plain list/get/create read+create surface
        # the SDK exposes unconditionally is included.
        # ------------------------------------------------------------------
        "/api/blueprints/{blueprint_id}/iba/dashboards": {
            "parameters": [_BP],
            "get": _op(
                "listBlueprintIbaDashboards", "List IBA dashboards in one blueprint.", ["IBA"]
            ),
            "post": {
                **_op(
                    "createBlueprintIbaDashboard",
                    "Create an IBA dashboard in one blueprint.",
                    ["IBA"],
                ),
                "requestBody": _json_body(),
            },
        },
        "/api/blueprints/{blueprint_id}/iba/dashboards/{dashboard_id}": {
            "parameters": [
                _BP,
                {
                    "name": "dashboard_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                    "description": "Apstra IBA dashboard ID.",
                },
            ],
            "get": _op(
                "getBlueprintIbaDashboard", "Get one IBA dashboard in one blueprint.", ["IBA"]
            ),
        },
        "/api/blueprints/{blueprint_id}/iba/anomalous-stages": {
            "parameters": [_BP],
            "get": _op(
                "getBlueprintIbaAnomalousStages",
                "Get IBA stages with anomalies in one blueprint.",
                ["IBA"],
            ),
        },
        "/api/blueprints/{blueprint_id}/iba/probes": {
            "parameters": [_BP],
            "get": _op("listBlueprintIbaProbes", "List IBA probes in one blueprint.", ["IBA"]),
        },
        "/api/blueprints/{blueprint_id}/iba/predefined-probes": {
            "parameters": [_BP],
            "get": _op(
                "listBlueprintIbaPredefinedProbes",
                "List IBA predefined probe templates available to one blueprint.",
                ["IBA"],
            ),
        },
        # Auth/login endpoints - documented for provenance; tagged Auth and
        # skipped at registration so the AuthToken session layer stays the sole
        # credential path.
        "/api/aaa/login": {
            "post": {
                **_op("apstraLogin", "Current session login (returns AuthToken).", ["Auth"]),
                "requestBody": _json_body(),
            }
        },
        "/api/user/login": {
            "post": {
                **_op("apstraLoginLegacy", "Older-release session login (returns AuthToken).", ["Auth"]),
                "requestBody": _json_body(),
            }
        },
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "HPE Juniper Apstra (derived operation set)",
            "version": "aos-sdk-api-6.1.2.post1",
            "license": {"name": "Apache-2.0 OR MIT (official Juniper SDK source mapping)"},
        },
        "servers": [{"url": "/"}],
        "paths": paths,
    }


def build_apstra_manifest() -> dict:
    spec = apstra_spec()
    sha = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    man = M.build_manifest(
        spec,
        platform="apstra",
        source_file="apstra-derived-operations.json",
        source_sha256=sha,
        overrides=M.load_overrides("apstra"),
    )
    man["provenance"] = {
        "acquired_from": (
            "Pinned official Juniper aos-sdk-api 6.1.2.post1 endpoint mappings."
        ),
        "note": (
            "No distributable full Apstra OpenAPI spec is available; this reviewed SDK-derived "
            "operation set is reproducible but is not full API coverage."
        ),
        "source_url": "https://pypi.org/project/aos-sdk-api/6.1.2.post1/",
        "source_sha256": sha,
        "reviewed_operation_count": len(man["operations"]),
        "auth_endpoints_not_registered": ["POST /api/aaa/login", "POST /api/user/login"],
        "auth_model": "AuthToken header session (see src/hpe_networking_mcp/mcp_servers/apstra.py _get_apstra_token).",
        "v07_optional_depth_additions": (
            "Added top-level resource pools (ip/ipv6/vlan/asn/vni/integer/device), "
            "device/rack profiles (device/linecard/chassis profiles, device-profile "
            "digests+clone, rack-types), system agents (agents, manager-config, jobs, "
            "profiles+assign), telemetry (service registry, collectors), and "
            "blueprint-scoped IBA (dashboards, anomalous-stages, probes, "
            "predefined-probes), all confirmed against the same pinned aos-sdk-api "
            "6.1.2.post1 wheel (`RestResources`/`RestResource` class definitions in "
            "`aos/sdk/api/_client.py`)."
        ),
        "coverage_gaps": [
            "Device profile digests are computed/read-only (get_schema/"
            "collection_schema only, no post_schema/put_schema in the pinned SDK): "
            "no create/update/delete operations are modeled.",
            "Rack-type creation has no post_schema in the pinned SDK: only "
            "list/get/update/delete are modeled for rack-types.",
            "Telemetry collector deletion has no confirmed schema for the "
            "per-collector resource in the pinned SDK: only list/create/get/update "
            "are modeled.",
            "IBA dashboard import/export and widget/predefined-probe instantiation "
            "are not modeled (the SDK exposes them as bespoke sub-actions rather "
            "than the plain list/get/create surface reviewed here); only "
            "dashboards (list/get/create), anomalous-stages, probes, and "
            "predefined-probes (list) are modeled.",
            "The streaming-telemetry-schema endpoint returns a binary protobuf "
            "descriptor, not JSON, and is not modeled as a tool.",
        ],
    }
    return man


if __name__ == "__main__":
    M.write_manifest("apstra", build_apstra_manifest())
    print("Wrote apstra manifest.")
