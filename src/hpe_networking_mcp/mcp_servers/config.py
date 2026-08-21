"""MCP server — Aruba Central configuration and provisioning tools (80 tools).

Covers: VLANs, SSIDs, overlay WLANs, port profiles, firmware compliance, device management,
webhooks, device groups, gateway clusters, interface and static route config, and
generic bounded network-profile helpers (get/set/delete_network_profile) backing
typed build_* workflows for routing overlays (BGP/OSPF/VRF), high availability
(VSX+VRRP), telemetry, application experience (bandwidth contracts + ARC), and
configuration checkpoint policy (see get_config_rollback_status for what New
Central's checkpoint API does and does not support). Also covers the v0.7
stricter dry_run+confirm+gate+read-back workflows: VSF stacking-template
lifecycle (build_vsf_template/delete_vsf_template), bulk site/site-collection
delete, and multi-target firmware-compliance campaigns.
"""
import os
import uuid
from typing import Any
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer

from hpe_networking_mcp.mcp_servers.shared import (
    DESTRUCTIVE,
    IDEMPOTENT_WRITE,
    READ_ONLY,
    bound_collection_response,
    clamp_limit,
    compact_http_error,
    enforce_platform_write,
    get_client,
    get_mcp_client,
    resp_json,
    validate_write_result,
)
from hpe_networking_mcp.pipeline.config import build_account_contexts
from hpe_networking_mcp.pipeline.create_ssid import (
    build_overlay_ssid as _build_overlay,
)
from hpe_networking_mcp.pipeline.create_ssid import (
    build_underlay_ssid as _build,
)
from hpe_networking_mcp.pipeline.create_ssid import (
    create_allow_all_role as _create_role,
)
from hpe_networking_mcp.pipeline.create_ssid import (
    delete_underlay_ssid as _delete,
)
from hpe_networking_mcp.pipeline.create_ssid import (
    get_underlay_ssid as _get,
)
from hpe_networking_mcp.pipeline.create_ssid import (
    list_underlay_ssids as _list,
)
from hpe_networking_mcp.pipeline.stages.s6_configure import (
    ARUBA_DEVICE_PROFILES,
    _ensure_device_profiles,
    _fetch_global_scope_id,
    _post_scope_map,
    _push_vlan_interface,
)

mcp = MCPServer("central-config")

_WEBHOOKS_BASE = "/network-services/v1/webhooks"
_DEVICE_GROUPS_BASE = "/network-config/v1/device-groups"


def _exc_resp_text(exc: Exception) -> str:
    """Extract response body text from an HTTP exception, or '' if unavailable."""
    return getattr(getattr(exc, "response", None), "text", "") or ""


# Name-spacing rules differ by target platform:
#  - AOS-CX switches and Gateways FORBID spaces in role/policy/rule names.
#  - Central NAC authz policies and their referenced roles REQUIRE spaces
#    (no-condition catch-all pattern needs spaced names to match reliably).
# See audit/FIX_PLAN.md Tier 2.1.
_TARGETS_FORBIDDING_SPACES = {"SWITCH", "GATEWAY", "AOS_CX", "AOS_S"}


def _validate_name_for_target(name: str, target: str | None) -> None:
    """Raise ValueError when a name contains spaces but target forbids them."""
    if target and target.upper() in _TARGETS_FORBIDDING_SPACES and " " in name:
        raise ValueError(
            f"Name '{name}' contains spaces but target={target} forbids them "
            "(AOS-CX switches and Gateways reject spaces in role/policy/rule names). "
            "Use dashes or underscores, or set target=NAC if this is a NAC policy/role."
        )


# ── VLANs ─────────────────────────────────────────────────────────────────────

@mcp.tool(annotations=IDEMPOTENT_WRITE)
def create_vlan(
    vlan_id: int,
    vlan_name: str | None = None,
    scope_id: str | None = None,
    persona: str = "ACCESS_SWITCH",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create an L2 VLAN and scope-map it (org-wide if scope_id omitted)."""
    if dry_run:
        return {"vlan_id": vlan_id, "dry_run": True, "created": False}

    client = get_client()
    if scope_id is None:
        scope_id = _fetch_global_scope_id(client)

    name = vlan_name or str(vlan_id)
    body = {"vlan": vlan_id, "name": name, "enable": True}
    errors: list[str] = []

    try:
        client.post(f"/network-config/v1/layer2-vlan/{vlan_id}", data=body)
    except Exception as exc:
        resp_text = _exc_resp_text(exc)
        if "duplicate" in resp_text.lower():
            try:
                client.put(f"/network-config/v1/layer2-vlan/{vlan_id}", data=body)
            except Exception as exc2:
                errors.append(f"upsert: {exc2}")
        else:
            errors.append(f"create: {exc}")

    try:
        _post_scope_map(client, scope_id, persona, f"layer2-vlan/{vlan_id}")
    except Exception as exc:
        resp_text = _exc_resp_text(exc)
        if "already exists" not in resp_text.lower():
            errors.append(f"scope_map: {exc}")

    return {"vlan_id": vlan_id, "vlan_name": name, "scope_id": scope_id, "errors": errors}


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def create_vlan_interface(
    vlan_id: int,
    device_scope_id: str,
    ip_address: str | None = None,
    helper_address: str | None = None,
    dhcp: bool = False,
    persona: str = "ACCESS_SWITCH",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create an L3 SVI at device scope (global L2 VLAN shell is auto-confirmed).

    ip_address: CIDR e.g. "10.1.200.1/24". Omit for DHCP.
    """
    if dry_run:
        return {"vlan_id": vlan_id, "device_scope_id": device_scope_id, "dry_run": True}

    client = get_client()
    global_scope_id = _fetch_global_scope_id(client)
    vi = {"vlan": vlan_id, "ip_address": ip_address, "helper_address": helper_address, "dhcp": dhcp}

    try:
        _push_vlan_interface(client, vi, device_scope_id, global_scope_id, persona)
        return {"vlan_id": vlan_id, "device_scope_id": device_scope_id, "pushed": True, "errors": []}
    except Exception as exc:
        return {"vlan_id": vlan_id, "device_scope_id": device_scope_id, "pushed": False, "errors": [str(exc)]}


# ── Hostname & Device Profiles ─────────────────────────────────────────────────

@mcp.tool(annotations=IDEMPOTENT_WRITE)
def set_hostname(
    device_scope_id: str,
    hostname: str,
    device_function: str = "CAMPUS_AP",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Set the hostname alias on a device."""
    if dry_run:
        return {"device_scope_id": device_scope_id, "hostname": hostname,
                "device_function": device_function, "dry_run": True}

    client = get_client()
    params = {"object-type": "LOCAL", "scope-id": device_scope_id,
              "device-function": device_function}
    alias_payload = {"default-value": {"hostname-value": {"hostname": hostname}}}
    sysinfo_payload = {"hostname-alias": "sys_host_name"}
    errors: list[str] = []
    try:
        # Step 1: set the alias value (create or update)
        try:
            client.post("/network-config/v1alpha1/aliases/sys_host_name",
                        params=params, data=alias_payload)
        except Exception:
            client.patch("/network-config/v1alpha1/aliases/sys_host_name",
                         params=params, data=alias_payload)
        # Step 2: link the alias in system-info (try PATCH first, then POST)
        try:
            client.patch("/network-config/v1alpha1/system-info",
                         params=params, data=sysinfo_payload)
        except Exception:
            client.post("/network-config/v1alpha1/system-info",
                        params=params, data=sysinfo_payload)
        return {"device_scope_id": device_scope_id, "hostname": hostname,
                "device_function": device_function, "set": True, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"device_scope_id": device_scope_id, "hostname": hostname,
                "device_function": device_function, "set": False, "errors": errors}


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def push_aruba_device_profiles(dry_run: bool = False) -> dict[str, Any]:
    """Ensure the four standard Aruba LLDP device profiles exist at library level (idempotent)."""
    if dry_run:
        return {"profiles": [p["name"] for p in ARUBA_DEVICE_PROFILES], "dry_run": True}

    client = get_client()
    creds_path = os.environ.get("CREDS_PATH", "config/credentials.yaml")
    _, target_ctx = build_account_contexts(creds_path)
    target_ctx.central_client = client
    # Scope IDs are resolved at runtime now, so this can fail before anything
    # is written (e.g. the org scope is undiscoverable). Surface that as a
    # normal tool error rather than an exception, and pass through non-fatal
    # per-profile failures instead of dropping them.
    try:
        profile_errors = _ensure_device_profiles(client, target_ctx)
    except Exception as exc:
        return {
            "profiles": [p["name"] for p in ARUBA_DEVICE_PROFILES],
            "pushed": False,
            "errors": [str(exc)],
        }
    return {
        "profiles": [p["name"] for p in ARUBA_DEVICE_PROFILES],
        "pushed": True,
        "errors": profile_errors,
    }


# ── Firmware ──────────────────────────────────────────────────────────────────

@mcp.tool(annotations=READ_ONLY)
def get_firmware(serial_number: str) -> dict[str, Any]:
    """Fetch current firmware details (version, compliance status, upgrades available) for a device."""
    client = get_client()
    errors: list[str] = []
    try:
        result = client.get(
            "/network-services/v1alpha1/firmware-details",
            params={"serialNumber": serial_number},
        )
        return {"serial_number": serial_number, "items": result.get("items", []), "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"serial_number": serial_number, "items": [], "errors": errors}


@mcp.tool(annotations=READ_ONLY)
def get_firmware_compliance(
    scope_id: str,
    device_function: str,
) -> dict[str, Any]:
    """Read the current firmware compliance policy at a given scope."""
    client = get_client()
    errors: list[str] = []
    params = {"scope-id": scope_id, "object-type": "LOCAL", "device-function": device_function}
    try:
        response = client._request("GET", "/network-config/v1alpha1/firmware-compliance", params=params)
        if response.status_code not in (200, 201, 202):
            errors.append(f"HTTP {response.status_code}")
            return {"scope_id": scope_id, "device_function": device_function, "policy": None, "errors": errors}
        return {"scope_id": scope_id, "device_function": device_function, "policy": response.json(), "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"scope_id": scope_id, "device_function": device_function, "policy": None, "errors": errors}


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def set_firmware_compliance(
    scope_id: str,
    device_function: str,
    firmware_version: str,
    upgrade_mode: str = "REGULAR",
    reboot_schedule_mode: str = "IMMEDIATE",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create or update a firmware compliance policy (triggers upgrade).

    firmware_version e.g. "10.16.1030". upgrade_mode: REGULAR or LIVE.
    """
    client = get_client()
    errors: list[str] = []
    payload: dict[str, Any] = {
        "name": f"compliance-{device_function.lower()}",
        "enable": True,
        "version-chart": {"version": firmware_version},
        "upgrade-mode": upgrade_mode,
        "enforcement-schedule": {
            "upgrade-schedule": {"upgrade-schedule-mode": "IMMEDIATE"},
            "reboot-schedule": {"reboot-schedule-mode": reboot_schedule_mode},
        },
    }
    params = {"scope-id": scope_id, "object-type": "LOCAL", "device-function": device_function}

    if dry_run:
        return {"dry_run": True, "action": "would POST or PATCH", "scope_id": scope_id,
                "device_function": device_function, "firmware_version": firmware_version,
                "payload": payload, "errors": []}

    action = "created"
    try:
        response = client._request("POST", "/network-config/v1alpha1/firmware-compliance", json=payload, params=params)
        if response.status_code == 412:
            action = "updated"
            response = client._request("PATCH", "/network-config/v1alpha1/firmware-compliance", json=payload, params=params)
        if response.status_code not in (200, 201, 202):
            errors.append(compact_http_error(response))
            return {"action": None, "scope_id": scope_id, "device_function": device_function,
                    "firmware_version": firmware_version, "response": None, "errors": errors}
        try:
            resp_body = response.json()
        except Exception:
            resp_body = {}
        return {"action": action, "scope_id": scope_id, "device_function": device_function,
                "firmware_version": firmware_version, "response": resp_body, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"action": None, "scope_id": scope_id, "device_function": device_function,
                "firmware_version": firmware_version, "response": None, "errors": errors}


@mcp.tool(annotations=READ_ONLY)
def list_firmware_upgrades(
    serial_number: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List devices with firmware upgrade activity (in-progress or recent, bounded by default).

    Sourced from GET /network-services/v1alpha1/firmware-details (the same endpoint
    get_firmware uses) — the legacy /firmware/v1/upgrade endpoint 404s on New Central.
    That endpoint ignores serialNumber server-side, so serial_number is filtered
    client-side. By default only devices whose upgradeStatus is set are returned,
    each surfacing upgradeStatus, recommendedVersion, firmwareClassification,
    lastUpgradedTimeAt, deviceName, and serialNumber.
    """
    client = get_client()
    errors: list[str] = []
    try:
        result = client.get("/network-services/v1alpha1/firmware-details")
        items = result.get("items", []) if isinstance(result, dict) else []
        if serial_number:
            items = [
                it for it in items
                if str(it.get("serialNumber", "")).lower() == serial_number.lower()
            ]
        upgrades = [
            {
                "serialNumber": it.get("serialNumber"),
                "deviceName": it.get("deviceName"),
                "upgradeStatus": it.get("upgradeStatus"),
                "recommendedVersion": it.get("recommendedVersion"),
                "firmwareClassification": it.get("firmwareClassification"),
                "lastUpgradedTimeAt": it.get("lastUpgradedTimeAt"),
            }
            for it in items
            if it.get("upgradeStatus") is not None
        ]
        return bound_collection_response(
            {"items": upgrades, "errors": errors}, limit=limit, offset=offset, list_key="items"
        )
    except Exception as exc:
        errors.append(str(exc))
        return {"items": [], "errors": errors}


@mcp.tool(annotations=DESTRUCTIVE)
def trigger_device_upgrade(
    serial_number: str,
    firmware_version: str,
    device_function: str | None = None,
    reboot_schedule_mode: str = "IMMEDIATE",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Trigger a per-device firmware upgrade at the device's local scope.

    POSTs the version-chart body to /network-config/v1alpha1/device-firmware with
    object-type=LOCAL plus the device's scope-id and device-function (the per-device
    local-object equivalent of set_firmware_compliance). The scope-id and persona are
    resolved from device inventory; device_function is auto-detected if omitted.
    """
    client = get_client()
    errors: list[str] = []
    mcp_client = get_mcp_client()

    if not device_function:
        device = mcp_client.get_device_by_serial(serial_number)
        if device:
            raw = device.get("deviceType", "")
            if "ACCESS_POINT" in raw or raw == "AP":
                device_function = "CAMPUS_AP"
            elif "SWITCH" in raw:
                device_function = "ACCESS_SWITCH"
            elif "GATEWAY" in raw:
                device_function = "MOBILITY_GW"

    if not device_function:
        errors.append(f"Could not determine device_function for {serial_number}.")
        return {"serial_number": serial_number, "firmware_version": firmware_version, "response": None, "errors": errors}

    scope_id = mcp_client.get_device_scope_id(serial_number)
    if not scope_id:
        errors.append(f"Could not resolve scope-id for {serial_number}.")
        return {"serial_number": serial_number, "firmware_version": firmware_version,
                "device_function": device_function, "response": None, "errors": errors}

    endpoint = "/network-config/v1alpha1/device-firmware"
    params = {"object-type": "LOCAL", "scope-id": scope_id, "device-function": device_function}
    # Version-chart body mirrors set_firmware_compliance (see CFG note below): the
    # device-firmware spec schema only formally declares issu/site-distribution, but the
    # firmware version is driven through the version-chart enforcement payload.
    payload: dict[str, Any] = {
        "version-chart": {"version": firmware_version},
        "enforcement-schedule": {
            "upgrade-schedule": {"upgrade-schedule-mode": "IMMEDIATE"},
            "reboot-schedule": {"reboot-schedule-mode": reboot_schedule_mode},
        },
    }

    if dry_run:
        return {"dry_run": True, "serial_number": serial_number, "firmware_version": firmware_version,
                "device_function": device_function, "scope_id": scope_id, "endpoint": endpoint,
                "params": params, "payload": payload, "errors": []}

    try:
        response = client._request("POST", endpoint, json=payload, params=params)
        if response.status_code == 412:
            response = client._request("PATCH", endpoint, json=payload, params=params)
        if response.status_code not in (200, 201, 202):
            errors.append(compact_http_error(response, endpoint=endpoint))
            return {"serial_number": serial_number, "firmware_version": firmware_version,
                    "device_function": device_function, "scope_id": scope_id, "response": None, "errors": errors}
        try:
            resp_body = response.json()
        except Exception:
            resp_body = {}
        return {"serial_number": serial_number, "firmware_version": firmware_version,
                "device_function": device_function, "scope_id": scope_id, "endpoint_used": endpoint,
                "response": resp_body, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"serial_number": serial_number, "firmware_version": firmware_version,
                "device_function": device_function, "scope_id": scope_id, "response": None, "errors": errors}


# ── SSIDs ─────────────────────────────────────────────────────────────────────

@mcp.tool(annotations=READ_ONLY)
def list_ssids(
    limit: int = 50,
    offset: int = 0,
    full_list: bool = False,
) -> dict[str, Any]:
    """Return wlan-ssid objects from Aruba New Central (bounded by default)."""
    items = _list(get_client())
    data: dict[str, Any] = {"wlan-ssid": items}
    if full_list:
        return data
    return bound_collection_response(data, limit=limit, offset=offset, list_key="wlan-ssid")


@mcp.tool(annotations=READ_ONLY)
def get_ssid(ssid_name: str) -> dict[str, Any] | None:
    """Fetch an existing SSID config by name. Returns None if not found."""
    return _get(get_client(), ssid_name)


@mcp.tool(annotations=READ_ONLY)
def get_scope_maps(
    resource_filter: str | None = None,
    limit: int = 100,
    offset: int = 0,
    full_list: bool = False,
) -> dict[str, Any]:
    """Return scope-map entries, optionally filtered by resource name (bounded by default)."""
    client = get_client()
    maps_resp = client.get("/network-config/v1/scope-maps")
    if isinstance(maps_resp, list):
        maps = maps_resp
    elif isinstance(maps_resp, dict):
        # New Central currently returns {"scope-map": [...]} for this endpoint.
        maps = maps_resp.get("scope-map", maps_resp.get("items", []))
    else:
        maps = []

    if not isinstance(maps, list):
        maps = []
    if resource_filter:
        needle = resource_filter.lower()
        maps = [m for m in maps if needle in str(m.get("resource", "")).lower()]
    data: dict[str, Any] = {"scope_maps": maps}
    if full_list:
        return data
    return bound_collection_response(data, limit=limit, offset=offset, list_key="scope_maps")


def _passpoint_read_params(
    *,
    view_type: str | None = None,
    object_type: str | None = None,
    scope_id: str | None = None,
    device_function: str | None = None,
    effective: bool | None = None,
    detailed: bool | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> dict[str, Any]:
    """Build query params for Passpoint read endpoints."""
    params: dict[str, Any] = {}
    if view_type is not None:
        params["view-type"] = view_type
    if object_type is not None:
        params["object-type"] = object_type
    if scope_id is not None:
        params["scope-id"] = scope_id
    if device_function is not None:
        params["device-function"] = device_function
    if effective is not None:
        # Query encoders serialize Python bools as 'True'/'False'; the API expects 'true'/'false'.
        params["effective"] = str(effective).lower()
    if detailed is not None:
        params["detailed"] = str(detailed).lower()
    if limit is not None:
        params["limit"] = clamp_limit(limit)
    if offset is not None:
        params["offset"] = max(0, offset)
    return params


def _config_list_response(
    endpoint: str,
    *,
    list_key: str,
    limit: int,
    offset: int,
    full_list: bool,
    **read_params: Any,
) -> dict[str, Any]:
    """Fetch a config collection with bounded output by default."""
    client = get_client()
    params = _passpoint_read_params(limit=limit, offset=offset, **read_params)
    data = client.get(endpoint, params=params or None)
    if isinstance(data, list):
        payload = {list_key: data}
    elif isinstance(data, dict):
        items = data.get(list_key)
        if not isinstance(items, list):
            items = data.get("items", [])
        if not isinstance(items, list):
            items = []
        payload = {**data, list_key: items}
    else:
        payload = {list_key: []}
    if full_list:
        return payload
    # The server already applied the offset via query params; slice from 0 so
    # the page isn't offset twice, but report the true offset to the caller.
    bounded = bound_collection_response(payload, limit=limit, offset=0, list_key=list_key)
    if isinstance(bounded, dict) and "_pagination" in bounded:
        bounded["_pagination"]["offset"] = max(0, offset)
    return bounded


@mcp.tool(annotations=READ_ONLY)
def list_passpoint_profiles(
    limit: int = 50,
    offset: int = 0,
    full_list: bool = False,
    view_type: str | None = None,
    object_type: str | None = None,
    scope_id: str | None = None,
    device_function: str | None = None,
    effective: bool | None = None,
    detailed: bool | None = None,
) -> dict[str, Any]:
    """List Passpoint / 802.11u provider profiles (bounded by default)."""
    return _config_list_response(
        "/network-config/v1alpha1/passpoint",
        list_key="profile",
        limit=limit,
        offset=offset,
        full_list=full_list,
        view_type=view_type,
        object_type=object_type,
        scope_id=scope_id,
        device_function=device_function,
        effective=effective,
        detailed=detailed,
    )


@mcp.tool(annotations=READ_ONLY)
def get_passpoint_profile(
    name: str,
    view_type: str | None = None,
    object_type: str | None = None,
    scope_id: str | None = None,
    device_function: str | None = None,
    effective: bool | None = None,
    detailed: bool | None = None,
) -> dict[str, Any]:
    """Fetch a Passpoint / 802.11u provider profile by name."""
    client = get_client()
    params = _passpoint_read_params(
        view_type=view_type,
        object_type=object_type,
        scope_id=scope_id,
        device_function=device_function,
        effective=effective,
        detailed=detailed,
    )
    resp = client._request("GET", f"/network-config/v1alpha1/passpoint/{quote(name, safe='')}", params=params or None)
    return resp_json(resp)


@mcp.tool(annotations=READ_ONLY)
def list_passpoint_identity_profiles(
    limit: int = 50,
    offset: int = 0,
    full_list: bool = False,
    view_type: str | None = None,
    object_type: str | None = None,
    scope_id: str | None = None,
    device_function: str | None = None,
    effective: bool | None = None,
    detailed: bool | None = None,
) -> dict[str, Any]:
    """List Passpoint identity / ANQP NAI realm profiles (bounded by default)."""
    return _config_list_response(
        "/network-config/v1alpha1/passpoint-identity",
        list_key="profile",
        limit=limit,
        offset=offset,
        full_list=full_list,
        view_type=view_type,
        object_type=object_type,
        scope_id=scope_id,
        device_function=device_function,
        effective=effective,
        detailed=detailed,
    )


@mcp.tool(annotations=READ_ONLY)
def get_passpoint_identity_profile(
    name: str,
    view_type: str | None = None,
    object_type: str | None = None,
    scope_id: str | None = None,
    device_function: str | None = None,
    effective: bool | None = None,
    detailed: bool | None = None,
) -> dict[str, Any]:
    """Fetch a Passpoint identity / ANQP NAI realm profile by name."""
    client = get_client()
    params = _passpoint_read_params(
        view_type=view_type,
        object_type=object_type,
        scope_id=scope_id,
        device_function=device_function,
        effective=effective,
        detailed=detailed,
    )
    resp = client._request(
        "GET",
        f"/network-config/v1alpha1/passpoint-identity/{quote(name, safe='')}",
        params=params or None,
    )
    return resp_json(resp)


@mcp.tool(annotations=READ_ONLY)
def list_gw_clusters(
    limit: int = 50,
    offset: int = 0,
    full_list: bool = False,
) -> dict[str, Any]:
    """List gateway clusters for overlay/tunneled SSIDs (bounded by default)."""
    clusters = get_mcp_client().get_gw_clusters()
    if full_list:
        return {"items": clusters}
    return bound_collection_response(clusters, limit=limit, offset=offset)


_CNAC_BASE = "/network-config/v1alpha1"
_AUTH_PROFILE_BASE = f"{_CNAC_BASE}/auth-profiles"
_MAC_ADDRESS_STORE_ID = "4c6c406a-7c1f-442a-8e43-c627090e8624"
_CENTRAL_ORG_NAME = "SecureSSID-LAB"


def _provision_nac_mac_auth(client, ssid_name: str, default_role: str | None, result: dict) -> dict:
    """Create Central NAC MAB auth profile + catch-all authz policy for an SSID.

    Called automatically by build_underlay_ssid and build_overlay_ssid when
    mac_auth_server_group is set. Skipped silently if a profile for the SSID
    already exists (idempotent).
    """
    effective_role = default_role if default_role is not None else ssid_name
    # Add SSID to an existing wireless MAB allow-all profile (UI-created profiles have hidden
    # internal bindings that API-created ones lack — patching a working profile is more reliable).
    try:
        existing = client.get(_AUTH_PROFILE_BASE).get("profile", [])

        # Already registered — nothing to do
        for p in existing:
            if ssid_name in p.get("networks", []):
                result["nac_auth_profile"] = {"skipped": "already exists"}
                break
        else:
            # Find an existing wireless MAB allow-all profile to append to
            target = next(
                (p for p in existing
                 if p.get("auth-type") == "MAB"
                 and not p.get("wired", False)
                 and p.get("mab", {}).get("allow-all")
                 and p.get("organization-name")),
                None,
            )
            if target:
                updated_networks = target.get("networks", []) + [ssid_name]
                profile_id = target["auth-profile-id"]
                resp = client._request("PATCH", f"{_AUTH_PROFILE_BASE}/{profile_id}", json={
                    "networks": updated_networks,
                })
                result["nac_auth_profile"] = {"profile_id": profile_id, "action": "patched", "status": resp.status_code}
            else:
                # Fallback: create new profile
                profile_id = str(uuid.uuid4())
                resp = client._request("POST", f"{_AUTH_PROFILE_BASE}/{profile_id}", json={
                    "auth-profile-id": profile_id,
                    "name": ssid_name,
                    "description": "",
                    "auth-type": "MAB",
                    "networks": [ssid_name],
                    "wired": False,
                    "organization-name": _CENTRAL_ORG_NAME,
                    "identity-stores": [_MAC_ADDRESS_STORE_ID],
                    "mab": {"allow-all": True},
                })
                result["nac_auth_profile"] = {"profile_id": profile_id, "action": "created", "status": resp.status_code}
    except Exception as exc:
        result.setdefault("errors", []).append(f"nac_auth_profile: {exc}")

    # Create a catch-all authz policy with NO conditions — conditions (e.g. NAS-Port-Type)
    # prevent NAC from matching. A plain no-condition policy at a low position works correctly.
    try:
        existing_policies = client.get(f"{_CNAC_BASE}/authz-policies").get("policy", [])
        policy_names = [p.get("name") for p in existing_policies]
        policy_name = f"{ssid_name} Allow"
        if policy_name in policy_names:
            result["nac_authz_policy"] = {"skipped": "already exists"}
        else:
            existing_positions = {p.get("position", 0) for p in existing_policies}
            position = 0 if 0 not in existing_positions else min(existing_positions) - 1
            policy_id = str(uuid.uuid4())
            resp = client._request("POST", f"{_CNAC_BASE}/authz-policies/{policy_id}", json={
                "name": policy_name,
                "position": position,
                "enable": True,
                "policy-type": "CUSTOM",
                "rule": [{
                    "position": 1,
                    "rule-id": str(uuid.uuid4()),
                    "rule-name": "Allow All",
                    "enable": True,
                    "enf-profile": [{
                        "profile-id": str(uuid.uuid4()),
                        "type": "ENF_RADIUS",
                        "radius-profile": {
                            "defined-attr": [
                                {"attr-name": "ATTR_POLICY_ACTION", "value": "Accept"},
                                {"attr-name": "ATTR_ARUBA_ROLE", "value": effective_role},
                            ]
                        },
                    }],
                }],
            })
            result["nac_authz_policy"] = {"policy_id": policy_id, "name": policy_name, "status": resp.status_code}
    except Exception as exc:
        result.setdefault("errors", []).append(f"nac_authz_policy: {exc}")

    return result


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def build_underlay_ssid(
    ssid_name: str,
    scope_id: str,
    persona: str = "CAMPUS_AP",
    opmode: str = "OPEN",
    passphrase: str | None = None,
    vlan_id: int | None = None,
    vlan_ids: list[int] | None = None,
    mac_auth_server_group: str | None = "sys_central_nac",
    default_role: str | None = None,
    dry_run: bool = False,
    *,
    wpa3_transition: bool = False,
) -> dict[str, Any]:
    """Create a bridge-mode (underlay) SSID and scope-map it.

    Args:
        scope_id: Use get_global_scope_id() for org-wide, or list_scopes() for site/group.
        persona: CAMPUS_AP (default), MOBILITY_GW, ACCESS_SWITCH, etc.
        opmode: ALWAYS confirm — never assume. OPEN for MAC-auth only (do NOT use
                ENHANCED_OPEN — causes NAC to reject with "Unexpected Client Data").
                WPA3_SAE / WPA2_PERSONAL for PSK (ask user for passphrase).
                NOT valid: OPEN_NETWORK, WPA3_SAE_AES.
        passphrase: Required for PSK modes — always ask, never generate.
        vlan_id / vlan_ids: Single VLAN or list of VLAN IDs.
        mac_auth_server_group: Central NAC server-group for MAC auth. None to skip.
        default_role: Override default MAC-auth role (omit to use SSID name).
        dry_run: Return payload without sending. Keeps its 0.4.0 positional index
                (10th positional argument) -- an old positional call that passed
                True here still only previews the payload and never writes.
        wpa3_transition: Keyword-only, added in 0.5.0 after dry_run so it cannot
                shift any pre-existing positional argument. Explicit opt-in for a
                WPA3-transition-mode SSID. Defaults to False for every currently
                supported pure opmode (OPEN, WPA2_PERSONAL, WPA3_SAE, ENHANCED_OPEN)
                -- never silently inherited as True. Only set True when you
                specifically intend a WPA3-transition SSID.
    """
    client = get_client()
    resolved_vlan_ids = vlan_ids or ([vlan_id] if vlan_id is not None else [1])
    result = _build(
        central_client=client,
        ssid_name=ssid_name,
        vlan_ids=[str(v) for v in resolved_vlan_ids],
        scope_id=scope_id,
        persona=persona,
        opmode=opmode,
        wpa_passphrase=passphrase,
        wpa3_transition=wpa3_transition,
        dry_run=dry_run,
    )
    scope_id = str(result.get("scope_id", scope_id))
    if dry_run:
        if mac_auth_server_group:
            result["will_also_create"] = [
                f"Central NAC MAB auth profile: add '{ssid_name}' to existing wireless allow-all profile",
                f"Central NAC authz policy: '{ssid_name} Allow' (no conditions, assigns role '{default_role or ssid_name}')",
            ]
        return result
    if mac_auth_server_group is None:
        return result

    if result.get("errors"):
        return result

    url_name = quote(ssid_name, safe="")
    updates = {
        "mac-authentication": True,
        "primary-auth-server": mac_auth_server_group,
        "cloud-auth": True,
        "radius-accounting": True,
        "radius-interim-accounting-interval": 10,
        "denylist": False,
        "called-station-id": {
            "type": "MAC_ADDRESS",
            "include-ssid": True,
        },
    }
    if default_role is not None:
        updates["default-role"] = default_role
    try:
        response = client._request("PATCH", f"/network-config/v1/wlan-ssids/{url_name}", json=updates)
        if response.status_code not in (200, 201, 202, 204):
            result.setdefault("errors", []).append(f"post_configure_macauth: {compact_http_error(response)}")
            return result
        result["mac_auth_configured"] = True
        result["mac_auth_updates"] = updates
    except Exception as exc:
        result.setdefault("errors", []).append(f"post_configure_macauth: {exc}")
        return result

    # Central auto-creates a role named after the SSID with no policies — add allowall so
    # clients assigned this role (pre-auth or RADIUS-returned) aren't silently denied.
    effective_default_role = default_role if default_role is not None else ssid_name
    try:
        resp = client._request(
            "PUT",
            f"/network-config/v1/roles/{effective_default_role}",
            json={"policies": [{"name": "sys_allow_all", "position": 1}]},
        )
        result["role_allowall"] = {"role": effective_default_role, "status": resp.status_code}
    except Exception as exc:
        result.setdefault("errors", []).append(f"role_allowall: {exc}")

    # Scope-map macauth-allow to this site/scope so APs here can enforce it.
    # The role lives in the shared library but must be visible at the SSID's scope.
    try:
        for resource in ["roles/macauth-allow", "role-gpids/macauth-allow"]:
            client._request("POST", "/network-config/v1/scope-maps", json={
                "scope-map": [{
                    "scope-name": scope_id,
                    "scope-id": int(scope_id),
                    "persona": persona,
                    "resource": resource,
                }]
            })
        result["macauth_allow_scope_map"] = {"scope_id": scope_id, "status": "ok"}
    except Exception as exc:
        result.setdefault("errors", []).append(f"macauth_allow_scope_map: {exc}")

    # Auto-create Central NAC auth profile and catch-all authz policy
    result = _provision_nac_mac_auth(client, ssid_name, default_role, result)
    return result


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def build_overlay_ssid(
    ssid_name: str,
    scope_id: str,
    cluster_name: str,
    cluster_scope_id: str,
    vlan_ids: list[int],
    opmode: str = "OPEN",
    passphrase: str | None = None,
    mac_auth_server_group: str | None = None,
    policy_name: str | None = None,
    dry_run: bool = False,
    *,
    wpa3_transition: bool = False,
) -> dict[str, Any]:
    """Create a tunneled (overlay/GRE) SSID via a gateway cluster.

    Args:
        scope_id: Device group scope-id (overlay WLANs cannot use global scope).
        cluster_name: Gateway cluster name (use list_gw_clusters).
        cluster_scope_id: Scope-id of the gateway cluster (use list_gw_clusters).
        vlan_ids: List of VLAN IDs (e.g. [200]).
        opmode: ALWAYS confirm — never assume. OPEN for MAC-auth only (do NOT use
                ENHANCED_OPEN — causes NAC to reject with "Unexpected Client Data").
                WPA3_SAE / WPA2_PERSONAL for PSK. NOT valid: OPEN_NETWORK, WPA3_SAE_AES.
        passphrase: Required for PSK opmodes — always ask, never generate.
        mac_auth_server_group: If set, creates an AAA profile and enables MAC auth.
        policy_name: Existing GW security policy to attach; auto-creates allow-all if omitted.
        dry_run: Return payload without sending. Keeps its 0.4.0 positional index
                (9th positional argument) -- an old positional call that passed
                True here still only previews the payload and never writes.
        wpa3_transition: Keyword-only, added in 0.5.0 after dry_run so it cannot
                shift any pre-existing positional argument. Explicit opt-in for a
                WPA3-transition-mode SSID. Defaults to False for every currently
                supported pure opmode (OPEN, WPA2_PERSONAL, WPA3_SAE, ENHANCED_OPEN)
                -- never silently inherited as True.
    """
    client = get_client()
    result = _build_overlay(
        central_client=client,
        ssid_name=ssid_name,
        vlan_ids=[str(v) for v in vlan_ids],
        scope_id=scope_id,
        cluster_name=cluster_name,
        cluster_scope_id=cluster_scope_id,
        opmode=opmode,
        wpa_passphrase=passphrase,
        wpa3_transition=wpa3_transition,
        mac_auth_server_group=mac_auth_server_group,
        policy_name=policy_name,
        dry_run=dry_run,
    )
    if not dry_run and mac_auth_server_group and not result.get("errors"):
        result = _provision_nac_mac_auth(client, ssid_name, "macauth-allow", result)
    return result


@mcp.tool(annotations=READ_ONLY)
def list_overlay_wlans(
    limit: int = 50,
    offset: int = 0,
    full_list: bool = False,
) -> dict[str, Any]:
    """List overlay (tunneled/GRE) WLAN profiles (bounded by default)."""
    data = get_client().get("/network-config/v1alpha1/overlay-wlan")
    if full_list:
        return data
    return bound_collection_response(data, limit=limit, offset=offset)


@mcp.tool(annotations=DESTRUCTIVE)
def delete_overlay_ssid(
    profile_name: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete an overlay (tunneled/GRE) WLAN profile. Must precede underlay SSID deletion.

    profile_name visible in gw-profile field or via list_overlay_wlans.

    Raises `WriteResultError` (via `validate_write_result`) on a non-2xx response
    or an error-shaped envelope instead of returning a success-shaped result with
    the failure buried in an `errors` list.
    """
    if dry_run:
        return {"dry_run": True, "profile_name": profile_name, "endpoint": f"/network-config/v1alpha1/overlay-wlan/{profile_name}"}

    endpoint = f"/network-config/v1alpha1/overlay-wlan/{profile_name}"
    client = get_client()
    resp = client._request("DELETE", endpoint)
    validate_write_result(resp, context=f"DELETE {endpoint}")
    result = resp_json(resp)
    validate_write_result(result, context=f"DELETE {endpoint}")
    return result


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def create_allow_all_role(
    role_name: str,
    scope_id: str,
    persona: str = "CAMPUS_AP",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create a permit-all wireless role and scope-map it.

    persona CAMPUS_AP allows spaces in name (NAC-adjacent);
    SWITCH/GW personas reject spaces.
    """
    persona_upper = (persona or "").upper()
    if "SWITCH" in persona_upper or persona_upper.endswith("_GW"):
        _validate_name_for_target(role_name, "SWITCH" if "SWITCH" in persona_upper else "GATEWAY")
    client = get_client()
    return _create_role(
        client,
        role_name=role_name,
        scope_id=scope_id,
        persona=persona,
        dry_run=dry_run,
    )


@mcp.tool(annotations=DESTRUCTIVE)
def delete_underlay_ssid(
    ssid_name: str,
    scope_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete an underlay SSID and its scope-map."""
    client = get_client()
    return _delete(client, ssid_name=ssid_name, dry_run=dry_run)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def update_ssid(
    ssid_name: str,
    updates: dict[str, Any],
    scope_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """PATCH an existing SSID — only provided fields change. scope_id for LOCAL override."""
    client = get_client()
    errors: list[str] = []
    url_name = quote(ssid_name, safe="")
    endpoint = f"/network-config/v1/wlan-ssids/{url_name}"
    params: dict[str, Any] = {}
    if scope_id:
        params["scope-id"] = scope_id
        params["view-type"] = "LOCAL"

    if dry_run:
        return {"dry_run": True, "ssid_name": ssid_name, "scope_id": scope_id, "updates": updates, "response": None, "errors": []}

    try:
        response = client._request("PATCH", endpoint, json=updates, params=params or None)
        if response.status_code not in (200, 201, 202, 204):
            errors.append(compact_http_error(response))
            return {"ssid_name": ssid_name, "scope_id": scope_id, "updates": updates, "response": None, "errors": errors}
        try:
            resp_body = response.json()
        except Exception:
            resp_body = {}
        return {"ssid_name": ssid_name, "scope_id": scope_id, "updates": updates, "response": resp_body, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"ssid_name": ssid_name, "scope_id": scope_id, "updates": updates, "response": None, "errors": errors}


# ── Switch Port Profiles & Interface Config ────────────────────────────────────

@mcp.tool(annotations=IDEMPOTENT_WRITE)
def create_port_profile(
    profile_name: str,
    body: dict[str, Any],
    description: str = "",
    scope_ids: list[str] | None = None,
    persona: str = "ACCESS_SWITCH",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create or update a switch port profile and scope-map it.

    Two-step: POST (shell) then PUT (full config body). Pass the nested config dict as body.
    """
    if dry_run:
        return {"dry_run": True, "profile_name": profile_name, "description": description,
                "body": body, "scope_ids": scope_ids or [], "errors": []}

    client = get_client()
    errors: list[str] = []
    encoded_name = quote(profile_name, safe="")

    try:
        client.post(f"/network-config/v1/sw-port-profiles/{encoded_name}", data={"description": description})
    except Exception as exc:
        resp_text = _exc_resp_text(exc)
        if "duplicate" not in resp_text.lower() and "already exists" not in resp_text.lower():
            errors.append(f"POST (shell): {exc}")

    try:
        client.put(f"/network-config/v1/sw-port-profiles/{encoded_name}", data=body)
    except Exception as exc:
        errors.append(f"PUT (body): {exc}")

    for sid in (scope_ids or []):
        try:
            _post_scope_map(client, sid, persona, f"sw-port-profiles/{profile_name}")
        except Exception as exc:
            resp_text = _exc_resp_text(exc)
            if "already exists" not in resp_text.lower():
                errors.append(f"scope_map(scope={sid}): {exc}")

    return {"profile_name": profile_name, "body": body, "scope_ids": scope_ids or [], "errors": errors}


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def create_aaa_macauth_profile(
    name: str,
    body: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create a device-side MAC-auth (MAB) profile via POST /macauth/{name}.

    Device-enforcement profile referenced by sw-port-profiles. Distinct from
    create_mac_auth_profile (Central NAC cloud-side). Omit body for shell.

    Raises `WriteResultError` (via `validate_write_result`) on a non-2xx response
    or an error-shaped envelope instead of returning a success-shaped result with
    the failure buried in an `errors` list.
    """
    endpoint = f"/network-config/v1alpha1/macauth/{quote(name, safe='')}"
    payload = body or {}
    if dry_run:
        return {"dry_run": True, "endpoint": endpoint, "payload": payload}
    client = get_client()
    resp = client._request("POST", endpoint, json=payload)
    validate_write_result(resp, context=f"POST {endpoint}")
    result = resp_json(resp)
    validate_write_result(result, context=f"POST {endpoint}")
    return result


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def create_aaa_dot1xauth_profile(
    name: str,
    body: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create a device-side 802.1X profile via POST /dot1xauth/{name}.

    Device-enforcement counterpart to create_dot1x_auth_profile (cloud-side).
    Referenced by sw-port-profiles. Omit body for shell.

    Raises `WriteResultError` (via `validate_write_result`) on a non-2xx response
    or an error-shaped envelope instead of returning a success-shaped result with
    the failure buried in an `errors` list.
    """
    endpoint = f"/network-config/v1alpha1/dot1xauth/{quote(name, safe='')}"
    payload = body or {}
    if dry_run:
        return {"dry_run": True, "endpoint": endpoint, "payload": payload}
    client = get_client()
    resp = client._request("POST", endpoint, json=payload)
    validate_write_result(resp, context=f"POST {endpoint}")
    result = resp_json(resp)
    validate_write_result(result, context=f"POST {endpoint}")
    return result


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def set_port_auth(
    profile_name: str,
    port_access_body: dict[str, Any],
    dry_run: bool = False,
) -> dict[str, Any]:
    """PATCH a sw-port-profile to bind mac-auth / dot1x / server-group / role.

    GET the current profile first — port_access_body shape varies. Typical keys:
    authenticator (dot1x ref), mac-auth (macauth ref), aaa-server-group,
    initial-role, auth-vlan-id. Profile must already exist.
    """
    endpoint = f"/network-config/v1/sw-port-profiles/{quote(profile_name, safe='')}"
    payload = {"port-access": port_access_body}
    if dry_run:
        return {"dry_run": True, "endpoint": endpoint, "payload": payload}
    resp = get_client()._request("PATCH", endpoint, json=payload)
    return resp_json(resp)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def update_port_config(
    serial_number: str,
    interface_name: str,
    updates: dict[str, Any],
    device_scope_id: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """PATCH ethernet interface config on a CX switch at device scope.

    interface_name e.g. "1/1/6". updates e.g. {"port-profile": "ap-uplink"}.
    """
    if dry_run:
        return {"dry_run": True, "serial_number": serial_number, "interface_name": interface_name,
                "updates": updates, "device_scope_id": device_scope_id, "response": None, "errors": []}

    client = get_client()
    errors: list[str] = []
    encoded_iface = quote(interface_name, safe="")
    endpoint = f"/network-config/v1/ethernet-interfaces/{encoded_iface}"
    params = {"object-type": "LOCAL", "scope-id": device_scope_id}

    try:
        response = client._request("PATCH", endpoint, json=updates, params=params)
        if response.status_code not in (200, 201, 202, 204):
            errors.append(compact_http_error(response))
            return {"serial_number": serial_number, "interface_name": interface_name,
                    "updates": updates, "response": None, "errors": errors}
        try:
            resp_body = response.json()
        except Exception:
            resp_body = {}
        return {"serial_number": serial_number, "interface_name": interface_name,
                "updates": updates, "response": resp_body, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"serial_number": serial_number, "interface_name": interface_name,
                "updates": updates, "response": None, "errors": errors}


# ── Gateway Interface & Routing Config ────────────────────────────────────────

@mcp.tool(annotations=IDEMPOTENT_WRITE)
def gateway_config_interface(
    serial_number: str,
    interface_name: str,
    updates: dict[str, Any],
    device_scope_id: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """PATCH ethernet interface config on an Aruba gateway at device scope.

    interface_name e.g. 'GE 0/0/1' (auto URL-encoded).
    """
    if dry_run:
        return {
            "dry_run": True, "serial_number": serial_number,
            "interface_name": interface_name, "updates": updates,
            "device_scope_id": device_scope_id, "response": None, "errors": [],
        }

    client = get_client()
    errors: list[str] = []
    encoded_iface = quote(interface_name, safe="")
    endpoint = f"/network-config/v1/ethernet-interfaces/{encoded_iface}"
    params = {"object-type": "LOCAL", "scope-id": device_scope_id}

    try:
        response = client._request("PATCH", endpoint, json=updates, params=params)
        if response.status_code not in (200, 201, 202, 204):
            errors.append(compact_http_error(response, endpoint=endpoint))
            return {"serial_number": serial_number, "interface_name": interface_name,
                    "updates": updates, "response": None, "errors": errors}
        try:
            resp_body = response.json()
        except Exception:
            resp_body = {}
        return {"serial_number": serial_number, "interface_name": interface_name,
                "updates": updates, "response": resp_body, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"serial_number": serial_number, "interface_name": interface_name,
                "updates": updates, "response": None, "errors": errors}


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def gateway_config_static_route(
    serial_number: str,
    destination: str,
    nexthop: str,
    device_scope_id: str,
    admin_distance: int = 1,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create or replace a static route on an Aruba gateway at device scope.

    destination: CIDR e.g. '0.0.0.0/0' (slashes become underscores in URL).
    admin_distance: default 1; use 50 for a default route.
    """
    route_name = destination.replace("/", "_")
    payload = {
        "nexthop": [{"ip-address": nexthop, "admin-distance": admin_distance}],
        "network": destination,
    }

    if dry_run:
        return {
            "dry_run": True, "serial_number": serial_number,
            "destination": destination, "nexthop": nexthop,
            "admin_distance": admin_distance, "device_scope_id": device_scope_id,
            "payload": payload, "errors": [],
        }

    client = get_client()
    errors: list[str] = []
    endpoint = f"/network-config/v1/static-route/{route_name}"
    params = {"object-type": "LOCAL", "scope-id": device_scope_id}

    try:
        response = client._request("PUT", endpoint, json=payload, params=params)
        if response.status_code not in (200, 201, 202, 204):
            errors.append(compact_http_error(response, endpoint=endpoint))
            return {"serial_number": serial_number, "destination": destination,
                    "nexthop": nexthop, "response": None, "errors": errors}
        try:
            resp_body = response.json()
        except Exception:
            resp_body = {}
        return {"serial_number": serial_number, "destination": destination,
                "nexthop": nexthop, "response": resp_body, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"serial_number": serial_number, "destination": destination,
                "nexthop": nexthop, "response": None, "errors": errors}


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def create_gw_cluster(
    cluster_name: str,
    scope_id: str,
    description: str = "",
    ipv6_enable: bool = False,
    auto_cluster: bool = False,
    device_function: str = "MOBILITY_GW",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create an empty gateway cluster profile shell (add members via gateway_join_cluster).

    Name must not start with 'auto_' and must not contain spaces.
    ipv6_enable cannot be toggled after creation. device_function:
    MOBILITY_GW or BRANCH_GW.
    """
    # As in gateway_join_cluster: the name is addressed in the URL path, and
    # `gateway-clusters` has no `name` child in Central's schema.
    payload: dict[str, Any] = {
        "description": description,
        "ipv6-enable": ipv6_enable,
        "auto-cluster": auto_cluster,
    }
    endpoint = f"/network-config/v1alpha1/gateway-clusters/{cluster_name}"
    params = {"object-type": "LOCAL", "scope-id": scope_id, "device-function": device_function}

    if dry_run:
        return {"dry_run": True, "endpoint": endpoint, "params": params, "payload": payload}

    client = get_client()
    resp = client._request("POST", endpoint, params=params, json=payload)
    return resp_json(resp)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def gateway_join_cluster(
    cluster_name: str,
    scope_id: str,
    gateways: list[dict[str, Any]],
    coa_vrrp_vlan: int | None = None,
    coa_vrrp_id: str | None = None,
    coa_vrrp_passphrase: str | None = None,
    device_function: str = "MOBILITY_GW",
    auto_cluster: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Add/update gateway cluster membership (POST to create, PATCH if duplicate).

    gateways: dicts with keys ip, mac, priority (higher=VRRP master), coa_vrrp_ip.
    coa_vrrp_vlan required when coa_vrrp_ip is set. Call create_gw_cluster first
    if the cluster shell doesn't exist.
    """
    # Central's gateway-clusters schema has no `name` child — the cluster name is
    # addressed in the URL path. Including it in the body fails the data-tree parse:
    #   400 "Node 'name' not found as a child of 'gateway-clusters' node."
    payload: dict[str, Any] = {
        "auto-cluster": auto_cluster,
        "ipv4-gateways": [
            {
                "ip": gw["ip"],
                "mac": gw["mac"],
                "priority": gw["priority"],
                "coa-vrrp-ip": gw.get("coa_vrrp_ip"),
            }
            for gw in gateways
        ],
    }
    if coa_vrrp_vlan is not None:
        coa_vrrp: dict[str, Any] = {"vlan": coa_vrrp_vlan}
        if coa_vrrp_id is not None:
            coa_vrrp["id"] = coa_vrrp_id
        if coa_vrrp_passphrase is not None:
            coa_vrrp["passphrase"] = coa_vrrp_passphrase
        payload["coa-vrrp"] = coa_vrrp

    # If all gateways share the same VRRP VIP, one-to-one-redundancy is required.
    vrrp_ips = {gw.get("coa_vrrp_ip") for gw in gateways}
    if len(vrrp_ips) == 1 and len(gateways) > 1:
        payload["one-to-one-redundancy"] = True

    if dry_run:
        return {
            "dry_run": True, "cluster_name": cluster_name,
            "scope_id": scope_id, "device_function": device_function,
            "payload": payload, "errors": [],
        }

    client = get_client()
    errors: list[str] = []
    params = {"object-type": "LOCAL", "scope-id": scope_id, "device-function": device_function}

    def _resp_err(resp: Any) -> str:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        return f"HTTP {resp.status_code}: {body}"

    endpoint = f"/network-config/v1alpha1/gateway-clusters/{cluster_name}"

    try:
        # Both verbs address the resource URL. POST creates; when the cluster already
        # exists, fall through to PATCH on ANY non-success rather than sniffing the
        # response for "duplicate"/"already exists" — tenants word that case
        # differently, and an existing cluster can surface as a schema-shaped 400
        # that string-matching silently misreads as a hard failure.
        response = client._request("POST", endpoint, json=payload, params=params)
        if response.status_code in (200, 201, 202, 204):
            return {"cluster_name": cluster_name, "action": "created", "payload": payload,
                    "response": response.json() if response.text else {}, "errors": errors}
        post_err = _resp_err(response)

        response = client._request("PATCH", endpoint, json=payload, params=params)
        if response.status_code in (200, 201, 202, 204):
            return {"cluster_name": cluster_name, "action": "updated", "payload": payload,
                    "response": response.json() if response.text else {}, "errors": errors}

        errors.append(f"POST failed: {post_err}")
        errors.append(f"PATCH failed: {_resp_err(response)}")
        return {"cluster_name": cluster_name, "payload": payload, "response": None, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"cluster_name": cluster_name, "payload": payload, "response": None, "errors": errors}


# ── Device Management ─────────────────────────────────────────────────────────

@mcp.tool(annotations=IDEMPOTENT_WRITE)
def create_site(
    name: str,
    address: str | None = None,
    city: str | None = None,
    state: str | None = None,
    country: str | None = None,
    zipcode: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create a new site (mandatory geographic scope). Only `name` is required; name must be unique."""
    payload: dict[str, Any] = {"name": name}
    for key, val in [
        ("address", address), ("city", city), ("state", state), ("country", country),
        ("zipcode", zipcode), ("latitude", latitude), ("longitude", longitude),
    ]:
        if val is not None:
            payload[key] = val

    endpoint = "/network-monitoring/v1/sites"
    if dry_run:
        return {"dry_run": True, "endpoint": endpoint, "payload": payload}

    client = get_client()
    resp = client._request("POST", endpoint, json=payload)
    return resp_json(resp)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def assign_device_to_site(
    serial_number: str,
    site_id: str,
    device_type: str | None = None,
) -> dict[str, Any]:
    """Assign or move a device to a site. device_type hint: SWITCH/AP/GATEWAY."""
    client = get_client()
    errors: list[str] = []

    # The legacy fallbacks require a *numeric* site_id; building their payloads
    # with int(site_id) eagerly used to raise ValueError before the primary
    # string-id candidate was ever tried, crashing the tool for scope-name /
    # non-numeric site ids. Resolve the legacy payload lazily and skip those
    # candidates (rather than crash) when the id isn't numeric.
    def _legacy_payload() -> dict[str, Any] | None:
        try:
            numeric_site_id = int(site_id)
        except (TypeError, ValueError):
            return None
        return {
            "site_id": numeric_site_id,
            "device_id": [serial_number],
            **({"device_type": device_type} if device_type else {}),
        }

    legacy_payload = _legacy_payload()
    candidates: list[tuple[str, str, dict[str, Any] | None]] = [
        ("POST", f"/network-monitoring/v1/sites/{site_id}/devices", {"serials": [serial_number]}),
        ("POST", "/central/v2/sites/associate", legacy_payload),
        ("POST", "/monitoring/v1/site/assign", legacy_payload),
    ]

    for method, endpoint, payload in candidates:
        if payload is None:
            errors.append(f"skipped {endpoint}: site_id {site_id!r} is not numeric")
            continue
        try:
            response = client._request(method, endpoint, json=payload)
            if response.status_code == 404:
                errors.append(f"404 at {endpoint}")
                continue
            if response.status_code not in (200, 201, 202):
                errors.append(compact_http_error(response, endpoint=endpoint))
                continue
            try:
                resp_body = response.json()
            except Exception:
                resp_body = {}
            return {"serial_number": serial_number, "site_id": site_id, "endpoint_used": endpoint,
                    "response": resp_body, "errors": errors}
        except Exception as exc:
            errors.append(str(exc))

    return {"serial_number": serial_number, "site_id": site_id, "response": None, "errors": errors}


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def update_device_settings(
    serial_number: str,
    settings: dict[str, Any],
    device_scope_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Update device metadata (name, location, notes, banner). device_scope_id required for switch-system path."""
    if dry_run:
        return {"dry_run": True, "serial_number": serial_number, "settings": settings,
                "device_scope_id": device_scope_id, "response": None, "errors": []}

    client = get_client()
    errors: list[str] = []
    candidates: list[tuple[str, str, dict[str, Any], dict[str, Any] | None]] = [
        ("PATCH", f"/network-monitoring/v1/devices/{serial_number}", settings, None),
    ]
    if device_scope_id:
        candidates.append((
            "PATCH", f"/network-config/v1/switch-system/{serial_number}", settings,
            {"object-type": "LOCAL", "scope-id": device_scope_id},
        ))

    for method, endpoint, payload, params in candidates:
        try:
            response = client._request(method, endpoint, json=payload, params=params)
            if response.status_code == 404:
                errors.append(f"404 at {endpoint}")
                continue
            if response.status_code not in (200, 201, 202, 204):
                errors.append(compact_http_error(response, endpoint=endpoint))
                continue
            try:
                resp_body = response.json()
            except Exception:
                resp_body = {}
            return {"serial_number": serial_number, "settings": settings, "endpoint_used": endpoint,
                    "response": resp_body, "errors": errors}
        except Exception as exc:
            errors.append(str(exc))

    return {"serial_number": serial_number, "settings": settings, "endpoint_used": None, "response": None, "errors": errors}


# ── Roles ─────────────────────────────────────────────────────────────────────

@mcp.tool(annotations=READ_ONLY)
def list_roles(
    limit: int = 50,
    offset: int = 0,
    full_list: bool = False,
) -> dict[str, Any]:
    """List wireless/gateway roles (bounded by default)."""
    data = get_client().get("/network-config/v1alpha1/roles")
    if full_list:
        return data
    return bound_collection_response(data, limit=limit, offset=offset)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def create_role(
    name: str,
    description: str | None = None,
    allow_all: bool = True,
    vlan_id: int | None = None,
    target: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create a wireless role in the Central Library (object-type=SHARED).

    NAC note: insufficient alone for Central NAC wireless roles — also needs
    scope-map (roles/ + role-gpids/) at global+site and a security policy in
    the policy group. Prefer build_underlay_ssid for NAC MAB wireless.

    allow_all=True attaches 'sys_allow_all'. target: NAC allows spaces;
    SWITCH/GATEWAY/AOS_CX/AOS_S reject them; omit to skip validation.

    Raises `WriteResultError` (via `validate_write_result`) on a non-2xx response
    or an error-shaped envelope instead of returning a success-shaped result with
    the failure buried in an `errors` list.
    """
    _validate_name_for_target(name, target)
    payload: dict[str, Any] = {}
    if description:
        payload["description"] = description
    if allow_all:
        payload["policies"] = [{"name": "sys_allow_all", "position": 1}]
    if vlan_id is not None:
        payload["vlan-parameters"] = {"access-vlan": vlan_id}

    if dry_run:
        return {"dry_run": True, "name": name, "payload": payload}

    client = get_client()
    resp = client._request(
        "POST",
        f"/network-config/v1/roles/{name}",
        json=payload,
    )
    validate_write_result(resp, context=f"POST /network-config/v1/roles/{name}")
    result = resp_json(resp)
    validate_write_result(result, context=f"POST /network-config/v1/roles/{name}")
    return result


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def update_role(
    name: str,
    description: str | None = None,
    allow_all: bool = True,
    vlan_id: int | None = None,
    target: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """PUT-update an existing wireless role in the Central Library. target: see create_role.

    Raises `WriteResultError` (via `validate_write_result`) on a non-2xx response
    or an error-shaped envelope instead of returning a success-shaped result with
    the failure buried in an `errors` list.
    """
    _validate_name_for_target(name, target)
    payload: dict[str, Any] = {}
    if description:
        payload["description"] = description
    if allow_all:
        payload["policies"] = [{"name": "sys_allow_all", "position": 1}]
    if vlan_id is not None:
        payload["vlan-parameters"] = {"access-vlan": vlan_id}

    if dry_run:
        return {"dry_run": True, "name": name, "payload": payload}

    client = get_client()
    resp = client._request(
        "PUT",
        f"/network-config/v1/roles/{name}",
        json=payload,
    )
    validate_write_result(resp, context=f"PUT /network-config/v1/roles/{name}")
    result = resp_json(resp)
    validate_write_result(result, context=f"PUT /network-config/v1/roles/{name}")
    return result


@mcp.tool(annotations=READ_ONLY)
def list_role_acls(
    limit: int = 50,
    offset: int = 0,
    full_list: bool = False,
) -> dict[str, Any]:
    """List role ACL policies (bounded by default)."""
    data = get_client().get("/network-config/v1alpha1/role-acls")
    if full_list:
        return data
    return bound_collection_response(data, limit=limit, offset=offset)


@mcp.tool(annotations=DESTRUCTIVE)
def delete_role_acl(
    name: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete a role ACL policy by name. Must precede delete_role."""
    if dry_run:
        return {"dry_run": True, "name": name}

    client = get_client()
    endpoint = f"/network-config/v1alpha1/role-acls/{quote(name, safe='')}"
    resp = client._request("DELETE", endpoint)
    validate_write_result(resp, context=f"DELETE {endpoint}")
    result = resp_json(resp)
    validate_write_result(result, context=f"DELETE {endpoint}")
    return result


@mcp.tool(annotations=READ_ONLY)
def list_gw_policies(
    limit: int = 50,
    offset: int = 0,
    full_list: bool = False,
) -> dict[str, Any]:
    """List GW security policies (bounded by default)."""
    data = get_client().get("/network-config/v1alpha1/policies")
    if full_list:
        return data
    return bound_collection_response(data, limit=limit, offset=offset)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def create_gw_policy(
    name: str,
    rules: list[dict[str, Any]] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create a POLICY_TYPE_SECURITY GW policy and register it in the policy group.

    Policy/rule names cannot contain spaces. GW source types: ADDRESS_ROLE,
    ADDRESS_NETWORK, ADDRESS_HOST, ADDRESS_FQDN, ADDRESS_RANGE, ADDRESS_VLAN,
    ADDRESS_PORT, ADDRESS_ANY. (AOS-CX supports only ADDRESS_ROLE — different endpoint.)
    Omit rules to auto-create allow-all sourced from role `name`.
    """
    _validate_name_for_target(name, "GATEWAY")
    if rules is None:
        rules = [{
            "position": 1,
            "description": "Allow All",
            "condition": {
                "type": "CONDITION_DEFAULT",
                "rule-type": "RULE_ANY",
                "source": {"type": "ADDRESS_ROLE", "role": name},
                "destination": {"type": "ADDRESS_ANY"},
            },
            "action": {"type": "ACTION_ALLOW"},
        }]

    payload = {
        "name": name,
        "type": "POLICY_TYPE_SECURITY",
        "security-policy": {
            "type": "SECURITY_POLICY_TYPE_DEFAULT",
            "policy-rule": rules,
        },
    }

    if dry_run:
        return {"dry_run": True, "name": name, "payload": payload}

    client = get_client()
    endpoint = f"/network-config/v1alpha1/policies/{quote(name, safe='')}"
    resp = client._request("POST", endpoint, json=payload)
    validate_write_result(resp, context=f"POST {endpoint}")
    policy_result = resp_json(resp)
    validate_write_result(policy_result, context=f"POST {endpoint}")
    result: dict[str, Any] = {"policy": policy_result}

    # Add to policy group (required before scope-mapping)
    policy_group_endpoint = "/network-config/v1alpha1/policy-groups"
    pg_resp = client._request(
        "PATCH",
        policy_group_endpoint,
        json={"policy-group": {"policy-group-list": [{"name": name, "position": 3}]}},
    )
    validate_write_result(pg_resp, context=f"PATCH {policy_group_endpoint}")
    policy_group_result = resp_json(pg_resp)
    validate_write_result(
        policy_group_result,
        context=f"PATCH {policy_group_endpoint}",
    )
    result["policy_group"] = policy_group_result

    return result


@mcp.tool(annotations=DESTRUCTIVE)
def delete_gw_policy(
    name: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete a GW security policy by name."""
    if dry_run:
        return {"dry_run": True, "name": name}

    client = get_client()
    endpoint = f"/network-config/v1alpha1/policies/{quote(name, safe='')}"
    resp = client._request("DELETE", endpoint)
    validate_write_result(resp, context=f"DELETE {endpoint}")
    result = resp_json(resp)
    validate_write_result(result, context=f"DELETE {endpoint}")
    return result


@mcp.tool(annotations=DESTRUCTIVE)
def delete_config_assignment(
    scope_id: str,
    device_function: str,
    profile_type: str,
    profile_instance: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Unassign a profile from a device function at a scope (required before delete_role).

    profile_type e.g. 'roles'; profile_instance is the profile name.

    Raises `WriteResultError` (via `validate_write_result`) on a non-2xx response
    or an error-shaped envelope instead of returning a success-shaped result with
    the failure buried in an `errors` list.
    """
    endpoint = f"/network-config/v1alpha1/config-assignments/{scope_id}/{device_function}/{profile_type}/{profile_instance}"

    if dry_run:
        return {"dry_run": True, "endpoint": endpoint}

    client = get_client()
    resp = client._request("DELETE", endpoint)
    validate_write_result(resp, context=f"DELETE {endpoint}")
    result = resp_json(resp)
    validate_write_result(result, context=f"DELETE {endpoint}")
    return result


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def create_config_assignment(
    scope_id: str,
    device_function: str,
    profile_type: str,
    profile_instance: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Assign a library profile to a scope + device-function.

    Binds library profile (role, ssid, vlan, policy, port-profile, etc.) to where
    it applies. Config-authoring tools produce orphans without this.
    profile_type: API endpoint segment ('roles', 'wlan-ssids', 'named-vlans',
    'sw-port-profiles', 'policies', 'auth-servers', 'server-groups',
    'aaa-profile', 'dot1xauth', 'macauth', etc.). profile_instance: profile
    name/ID.

    Raises `WriteResultError` (via `validate_write_result`) on a non-2xx response
    or an error-shaped envelope instead of returning a success-shaped result with
    the failure buried in an `errors` list.
    """
    endpoint = "/network-config/v1alpha1/config-assignments"
    payload = {
        "config-assignment": [
            {
                "scope-id": scope_id,
                "device-function": device_function,
                "profile-type": profile_type,
                "profile-instance": profile_instance,
            }
        ]
    }

    if dry_run:
        return {"dry_run": True, "endpoint": endpoint, "payload": payload}

    client = get_client()
    resp = client._request("POST", endpoint, json=payload)
    validate_write_result(resp, context=f"POST {endpoint}")
    result = resp_json(resp)
    validate_write_result(result, context=f"POST {endpoint}")
    return result


@mcp.tool(annotations=READ_ONLY)
def list_config_assignments(
    scope_id: str | None = None,
    device_function: str | None = None,
    profile_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
    full_list: bool = False,
) -> dict[str, Any]:
    """List config assignments (library profiles bound to scopes, bounded by default).

    scope_id and device_function are server-side query filters
    (GET /config-assignments?scope-id=...&device-function=...) per the
    config-assignment spec. The API does not support a profile-type query
    filter, so profile_type is applied client-side to the returned
    "config-assignment" list. Paginated client-side via limit/offset;
    full_list=True returns everything (post profile_type filtering).
    """
    params: dict[str, Any] = {}
    if scope_id is not None:
        params["scope-id"] = scope_id
    if device_function is not None:
        params["device-function"] = device_function
    client = get_client()
    resp = client._request("GET", "/network-config/v1alpha1/config-assignments", params=params or None)
    data = resp_json(resp)
    if profile_type is not None and isinstance(data, dict):
        assignments = data.get("config-assignment")
        if isinstance(assignments, list):
            data = {
                **data,
                "config-assignment": [
                    item
                    for item in assignments
                    if isinstance(item, dict) and item.get("profile-type") == profile_type
                ],
            }
    if full_list:
        return data
    return bound_collection_response(data, limit=limit, offset=offset)


@mcp.tool(annotations=DESTRUCTIVE)
def delete_role(
    name: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete a wireless/gateway role by name.

    Pre-reqs: delete_role_acl then delete_config_assignment for all scopes.

    Raises `WriteResultError` (via `validate_write_result`) on a non-2xx response
    or an error-shaped envelope instead of returning a success-shaped result with
    the failure buried in an `errors` list.
    """
    if dry_run:
        return {"dry_run": True, "name": name}

    endpoint = f"/network-config/v1alpha1/roles/{name}"
    client = get_client()
    resp = client._request("DELETE", endpoint)
    validate_write_result(resp, context=f"DELETE {endpoint}")
    result = resp_json(resp)
    validate_write_result(result, context=f"DELETE {endpoint}")
    return result


# ── Device Fingerprinting ─────────────────────────────────────────────────────

@mcp.tool(annotations=READ_ONLY)
def list_fingerprinting_profiles(
    limit: int = 50,
    offset: int = 0,
    full_list: bool = False,
) -> dict[str, Any]:
    """List device-fingerprinting wireless profiles (bounded by default)."""
    data = get_client().get("/network-config/v1alpha1/devicefingerprinting")
    if full_list:
        return data
    return bound_collection_response(data, limit=limit, offset=offset)


@mcp.tool(annotations=READ_ONLY)
def list_fingerprinting_switch_profiles(
    limit: int = 50,
    offset: int = 0,
    full_list: bool = False,
) -> dict[str, Any]:
    """List device-fingerprinting switch profiles (bounded by default)."""
    data = get_client().get("/network-config/v1alpha1/devicefingerprinting-profile")
    if full_list:
        return data
    return bound_collection_response(data, limit=limit, offset=offset)


# ── Webhooks ──────────────────────────────────────────────────────────────────

@mcp.tool(annotations=READ_ONLY)
def list_webhooks(
    limit: int = 50,
    offset: int = 0,
    full_list: bool = False,
) -> dict[str, Any]:
    """List configured webhooks (bounded by default)."""
    data = get_client().get(_WEBHOOKS_BASE)
    if full_list:
        return data
    return bound_collection_response(data, limit=limit, offset=offset)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def create_webhook(
    name: str,
    endpoint_url: str,
    auth_mechanism: str = "API_KEY",
    api_key: str | None = None,
    oidc_client_id: str | None = None,
    oidc_client_secret: str | None = None,
    oidc_well_known_url: str | None = None,
) -> dict[str, Any]:
    """Create a new webhook.

    Args:
        auth_mechanism: "API_KEY" or "OIDC". (Central UI exposes both;
                        the current API reference only documents OIDC as the
                        enum value — pass API_KEY if the default API-key flow
                        is what the tenant uses. "HMAC" is not valid.)
        api_key: Required when auth_mechanism="API_KEY".
        oidc_client_id / oidc_client_secret / oidc_well_known_url:
            Required when auth_mechanism="OIDC" (sent as the `oidcDetails`
            object per the developer-docs schema).
    """
    body: dict[str, Any] = {"name": name, "endpoint": endpoint_url, "authMechanism": auth_mechanism}
    if auth_mechanism == "OIDC":
        if not (oidc_client_id and oidc_client_secret and oidc_well_known_url):
            return {"errors": ["OIDC requires oidc_client_id, oidc_client_secret, and oidc_well_known_url"]}
        body["oidcDetails"] = {
            "clientId": oidc_client_id,
            "clientSecret": oidc_client_secret,
            "wellKnownUrl": oidc_well_known_url,
        }
    elif api_key:
        body["apiKey"] = api_key
    resp = get_client()._request("POST", _WEBHOOKS_BASE, json={"input": body})
    return resp_json(resp)


@mcp.tool(annotations=READ_ONLY)
def get_webhook(webhook_id: str) -> dict[str, Any]:
    """Fetch details for a single webhook by ID."""
    return get_client().get(f"{_WEBHOOKS_BASE}/{webhook_id}")


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def update_webhook(
    webhook_id: str,
    name: str | None = None,
    endpoint_url: str | None = None,
    auth_mechanism: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """PATCH an existing webhook — only provided fields are changed."""
    client = get_client()
    patch: dict[str, Any] = {}
    if name is not None:
        patch["name"] = name
    if endpoint_url is not None:
        patch["endpoint"] = endpoint_url
    if auth_mechanism is not None:
        patch["authMechanism"] = auth_mechanism
    if api_key is not None:
        patch["apiKey"] = api_key
    resp = client._request("PATCH", f"{_WEBHOOKS_BASE}/{webhook_id}", json={"input": patch})
    return resp_json(resp)


@mcp.tool(annotations=DESTRUCTIVE)
def delete_webhook(webhook_id: str) -> dict[str, Any]:
    """Delete a webhook by ID."""
    client = get_client()
    resp = client._request("DELETE", f"{_WEBHOOKS_BASE}/{webhook_id}")
    try:
        body = resp.json()
    except Exception:
        body = resp.text
    return {"status_code": resp.status_code, "response": body}


@mcp.tool(annotations=DESTRUCTIVE)
def rotate_webhook_key(webhook_id: str) -> dict[str, Any]:
    """Rotate the HMAC key for a webhook."""
    client = get_client()
    resp = client._request("POST", f"{_WEBHOOKS_BASE}/{webhook_id}/rotate-hmac-key")
    return resp_json(resp)


# ── Device Groups ─────────────────────────────────────────────────────────────

@mcp.tool(annotations=READ_ONLY)
def list_device_groups(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """List all device groups (scopeId, scopeName, description)."""
    lim = clamp_limit(limit)
    off = max(0, offset)
    return get_client().get(f"{_DEVICE_GROUPS_BASE}?limit={lim}&offset={off}")


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def create_device_group(
    name: str,
    description: str = "",
    devices: list[str] | None = None,
) -> dict[str, Any]:
    """Create a device group, optionally pre-populated with device serial numbers."""
    client = get_client()
    if devices:
        payload = {"scopeName": name, "description": description, "devices": devices}
        resp = client._request("POST", "/network-config/v1/device-groups-create-and-add-devices", json=payload)
    else:
        payload = {"scopeName": name, "description": description}
        resp = client._request("POST", _DEVICE_GROUPS_BASE, json=payload)
    return resp_json(resp)


@mcp.tool(annotations=DESTRUCTIVE)
def delete_device_groups(scope_ids: list[str]) -> dict[str, Any]:
    """Bulk-delete device groups by scope ID list."""
    client = get_client()
    payload = {"items": [{"id": sid} for sid in scope_ids]}
    resp = client._request("DELETE", f"{_DEVICE_GROUPS_BASE}/bulk", json=payload)
    try:
        body = resp.json()
    except Exception:
        body = resp.text
    return {"status_code": resp.status_code, "response": body}


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def add_devices_to_group(scope_id: str, serial_numbers: list[str]) -> dict[str, Any]:
    """Add devices to an existing device group by scope ID."""
    client = get_client()
    payload = {"desScopeId": scope_id, "devices": serial_numbers}
    resp = client._request("POST", "/network-config/v1/device-groups-add-devices", json=payload)
    try:
        body = resp.json()
    except Exception:
        body = resp.text
    return {"status_code": resp.status_code, "response": body}


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def remove_devices_from_group(serial_numbers: list[str]) -> dict[str, Any]:
    """Remove devices from their current device group."""
    client = get_client()
    resp = client._request("POST", "/network-config/v1/device-groups-remove-devices", json={"devices": serial_numbers})
    try:
        body = resp.json()
    except Exception:
        body = resp.text
    return {"status_code": resp.status_code, "response": body}


# ── Config Templates ──────────────────────────────────────────────────────────

@mcp.tool(annotations=READ_ONLY)
def list_config_templates(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """List configuration templates defined in Central.

    Returns template names, types, and scope assignments. Tries multiple
    known New Central endpoints and surfaces whichever responds.

    v0.7 note: none of the endpoints probed below are confirmed in the
    committed New Central network-config manifest — they are speculative
    Classic-Central-shaped guesses kept only for back-compat on tenants
    where they happen to respond. The one schema-confirmed, generic
    "template" resource in New Central today is the VSF stacking template
    (see build_vsf_template / delete_vsf_template / get_network_profile
    with profile_type="vsf-template"). There is no New Central endpoint for
    Classic-style templates with %variable% substitution or a separate
    template-group assignment step; do not expect one from this tool.
    """
    client = get_client()
    lim = clamp_limit(limit)
    off = max(0, offset)
    errors: list[str] = []
    for endpoint in (
        "/network-config/v1/templates",
        "/network-config/v1alpha1/templates",
        "/configuration/v1/templates",
        "/configuration/v2/templates",
    ):
        try:
            resp = client._request("GET", endpoint, params={"limit": lim, "offset": off})
            if resp.status_code in (400, 404):
                errors.append(f"HTTP {resp.status_code} at {endpoint}")
                continue
            if resp.status_code not in (200, 201, 202):
                errors.append(compact_http_error(resp, endpoint))
                continue
            data = resp.json()
            items = data if isinstance(data, list) else data.get("items", data.get("templates", []))
            # The endpoint already paged via limit/offset params; slice from 0
            # so the page isn't offset twice, but report the true offset.
            bounded = bound_collection_response(items, limit=lim, offset=0)
            if isinstance(bounded, dict) and "_pagination" in bounded:
                bounded["_pagination"]["offset"] = off
            return bounded
        except Exception as exc:
            errors.append(f"{endpoint}: {exc}")
    return {"items": [], "errors": errors, "_note": "No template endpoint responded — may not be exposed in New Central yet"}


@mcp.tool(annotations=READ_ONLY)
def get_device_running_config(serial_number: str) -> dict[str, Any]:
    """Download the running configuration for a device.

    Tries multiple Central endpoints for device config backup/export.
    Returns the raw config text when available.
    """
    client = get_client()
    errors: list[str] = []
    for endpoint in (
        f"/network-config/v1/devices/{serial_number}/configuration",
        f"/network-config/v1alpha1/devices/{serial_number}/configuration",
        f"/configuration/v1/devices/{serial_number}/configuration",
        f"/configuration/v1/devices/template/{serial_number}/config",
    ):
        try:
            resp = client._request("GET", endpoint)
            if resp.status_code in (400, 404):
                errors.append(f"HTTP {resp.status_code} at {endpoint}")
                continue
            if resp.status_code not in (200, 201, 202):
                errors.append(compact_http_error(resp, endpoint))
                continue
            try:
                data = resp.json()
            except Exception:
                data = {"config": resp.text}
            return {"serial_number": serial_number, "endpoint_used": endpoint, "config": data, "errors": errors}
        except Exception as exc:
            errors.append(f"{endpoint}: {exc}")
    return {"serial_number": serial_number, "config": None, "errors": errors, "_note": "Config export may not be available in New Central"}


@mcp.tool(annotations=READ_ONLY)
def list_named_vlans(scope_id: str | None = None) -> dict[str, Any]:
    """List named VLANs configured at the group/scope level in Central.

    These are the config-layer VLAN definitions (name + ID mappings) as
    distinct from the live VLANs reported by monitoring on a specific switch.
    scope_id defaults to global scope when omitted.
    """
    client = get_client()
    errors: list[str] = []

    if not scope_id:
        try:
            global_resp = client.get("/network-monitoring/v1/globalScopeId")
            scope_id = global_resp.get("scopeId") or global_resp.get("id")
        except Exception as exc:
            errors.append(f"Could not resolve global scope: {exc}")

    for endpoint in (
        "/network-config/v1/named-vlan",
        f"/network-config/v1/node_list/{scope_id}/config/aruba_wired_cx/vlans/config",
        "/network-config/v1alpha1/named-vlan",
        f"/network-config/v1/scopes/{scope_id}/named-vlans",
    ):
        try:
            resp = client._request("GET", endpoint)
            if resp.status_code in (400, 404):
                errors.append(f"HTTP {resp.status_code} at {endpoint}")
                continue
            if resp.status_code not in (200, 201, 202):
                errors.append(compact_http_error(resp, endpoint))
                continue
            data = resp.json()
            items = data if isinstance(data, list) else data.get("items", data.get("vlans", data.get("named_vlans", [])))
            return {"scope_id": scope_id, "endpoint_used": endpoint, "vlans": items, "errors": errors}
        except Exception as exc:
            errors.append(f"{endpoint}: {exc}")
    return {"scope_id": scope_id, "vlans": [], "errors": errors, "_note": "Named VLAN endpoint not found — use get_switch_vlans for live switch VLANs"}


# ── Network Profiles: Routing Overlays, HA, Telemetry, App Experience, ──────
# ── Config Checkpoint (generic bounded helpers + typed build_* workflows) ───
#
# All of the resources below share one REST convention confirmed against the
# network-config v1alpha1 OpenAPI specs (bgp.json, ospfv2.json, ospfv3.json,
# vrf.json, vsx.json, vrrp.json, telemetry.json, app-bandwidth-contract.json,
# app-recog-control.json, config-checkpoint.json — see
# developer.arubanetworks.com/new-central-config/reference/create<name>profilebyid):
#
#   GET    /{base}                 list library (SHARED) or LOCAL profiles
#   GET    /{base}/{name}          read one profile (view-type/object-type/
#                                   scope-id/device-function/effective/detailed)
#   POST   /{base}/{name}          create (object-type/scope-id/device-function)
#   PATCH  /{base}/{name}          update
#   DELETE /{base}/{name}          delete
#
# Rather than hand-write 5 near-identical tools per resource (35+ for the 7
# domains below), _profile_base/_get_network_profile/_upsert_network_profile/
# _delete_network_profile implement the convention once. Each domain then
# gets one clear verb_noun "build_*" workflow tool that creates a
# LOCAL-scoped, scope-bound profile in a single call (object-type=LOCAL
# requires scope-id + device-function directly on the create call — no
# separate config-assignment step is needed for LOCAL objects, unlike
# SHARED library profiles such as roles/wlan-ssids).

_NETWORK_PROFILE_TYPES: dict[str, str] = {
    "bgp": "bgp",
    "ospfv2": "ospfv2",
    "ospfv3": "ospfv3",
    "vrf": "vrfs",
    "vsx": "vsx-profiles",
    "vsx-pair": "vsx-config",
    "vrrp": "vrrp-global",
    "telemetry": "telemetry",
    "app-bandwidth-contract": "app-bandwidth-contracts",
    "app-recognition": "arc",
    "config-checkpoint": "config-checkpoint",
    # VSF stacking templates (manifest-confirmed: vsf-template.json) — the
    # only schema-backed generic "template" resource in the New Central
    # network-config API. See build_vsf_template/delete_vsf_template below
    # for the stricter dry_run+confirm+gate+read-back lifecycle contract;
    # get_network_profile/set_network_profile/delete_network_profile also
    # work generically against profile_type="vsf-template".
    "vsf-template": "vsf-templates",
}

_READ_ONLY_NETWORK_PROFILE_TYPES: dict[str, str] = {
    **_NETWORK_PROFILE_TYPES,
    # Read-only evidence surfaces for unresolved AOS8 migration contracts.
    # Keep these out of _NETWORK_PROFILE_TYPES so set/delete_network_profile
    # cannot write them until their live payload and attachment semantics are
    # proven (docs/aos8-migration-contract-matrix.md SS6.12-SS6.13).
    "static-route": "static-route",
    "vrrp-interface": "vrrp",
}


def _profile_base(profile_type: str, *, read_only: bool = False) -> str:
    key = profile_type.strip().lower()
    profile_types = (
        _READ_ONLY_NETWORK_PROFILE_TYPES if read_only else _NETWORK_PROFILE_TYPES
    )
    if key not in profile_types:
        allowed = ", ".join(sorted(profile_types))
        raise ValueError(f"profile_type must be one of: {allowed}")
    return f"/network-config/v1alpha1/{profile_types[key]}"


def _profile_write_params(
    object_type: str | None, scope_id: str | None, device_function: str | None
) -> dict[str, Any]:
    if (object_type or "").upper() == "LOCAL" and not scope_id:
        raise ValueError("scope_id is required when object_type='LOCAL'")
    params: dict[str, Any] = {}
    if object_type is not None:
        params["object-type"] = object_type
    if scope_id is not None:
        params["scope-id"] = scope_id
    if device_function is not None:
        params["device-function"] = device_function
    return params


def _upsert_network_profile(
    profile_type: str,
    name: str,
    body: dict[str, Any],
    *,
    object_type: str | None,
    scope_id: str | None,
    device_function: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    base = _profile_base(profile_type)
    endpoint = f"{base}/{quote(name, safe='')}"
    params = _profile_write_params(object_type, scope_id, device_function)
    if dry_run:
        return {
            "dry_run": True, "profile_type": profile_type, "name": name,
            "endpoint": endpoint, "params": params, "payload": body,
        }
    client = get_client()
    response = client._request("POST", endpoint, json=body, params=params or None)
    action = "created"
    if response.status_code == 412:
        action = "updated"
        response = client._request("PATCH", endpoint, json=body, params=params or None)
    result = resp_json(response)
    result["action"] = action if response.is_success else None
    if not response.is_success:
        result["errors"] = [compact_http_error(response, endpoint)]
    return result


def _delete_network_profile(
    profile_type: str,
    name: str,
    *,
    object_type: str | None,
    scope_id: str | None,
    device_function: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    base = _profile_base(profile_type)
    endpoint = f"{base}/{quote(name, safe='')}"
    params = {
        k: v for k, v in {
            "object-type": object_type, "scope-id": scope_id, "device-function": device_function,
        }.items() if v is not None
    }
    if dry_run:
        return {
            "dry_run": True, "profile_type": profile_type, "name": name,
            "endpoint": endpoint, "params": params,
        }
    client = get_client()
    response = client._request("DELETE", endpoint, params=params or None)
    result = resp_json(response)
    if not response.is_success:
        result["errors"] = [compact_http_error(response, endpoint)]
    return result


@mcp.tool(annotations=READ_ONLY)
def get_network_profile(
    profile_type: str,
    name: str | None = None,
    view_type: str | None = None,
    object_type: str | None = None,
    scope_id: str | None = None,
    device_function: str | None = None,
    effective: bool | None = None,
    detailed: bool | None = None,
    limit: int = 50,
    offset: int = 0,
    full_list: bool = False,
) -> dict[str, Any]:
    """Read a network-config library profile: list all, or fetch one by name.

    profile_type: bgp | ospfv2 | ospfv3 | vrf | vsx | vsx-pair | vrrp |
    telemetry | app-bandwidth-contract | app-recognition | config-checkpoint |
    static-route | vrrp-interface. The last two are read-only evidence
    surfaces; set/delete_network_profile intentionally reject them until their
    AOS8 migration write contracts are verified live.
    Backs the routing-overlay (BGP/OSPF/VRF), high-availability (vsx/vrrp),
    telemetry, application-experience, and config-checkpoint domains.
    Omit `name` to list (bounded by default); provide `name` to fetch one.
    view_type/object_type/scope_id/device_function/effective/detailed mirror
    the read query params documented for every resource in this family
    (see list_passpoint_profiles for the same pattern).
    """
    base = _profile_base(profile_type, read_only=True)
    if name:
        client = get_client()
        params = _passpoint_read_params(
            view_type=view_type, object_type=object_type, scope_id=scope_id,
            device_function=device_function, effective=effective, detailed=detailed,
        )
        response = client._request("GET", f"{base}/{quote(name, safe='')}", params=params or None)
        return resp_json(response)
    return _config_list_response(
        base, list_key="profile", limit=limit, offset=offset, full_list=full_list,
        view_type=view_type, object_type=object_type, scope_id=scope_id,
        device_function=device_function, effective=effective, detailed=detailed,
    )


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def set_network_profile(
    profile_type: str,
    name: str,
    body: dict[str, Any],
    object_type: str | None = None,
    scope_id: str | None = None,
    device_function: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create or update (upsert) a network-config library profile by name.

    See get_network_profile for the profile_type enum. object_type=LOCAL
    requires scope_id + device_function and creates a scope-bound profile
    directly (no separate config-assignment needed); omit object_type (or
    pass SHARED) to write to the library — SHARED library profiles need a
    separate create_config_assignment call to bind them to a scope.
    Prefer the domain-specific build_* workflow tools below for the common
    "create + bind to this scope" case.
    """
    return _upsert_network_profile(
        profile_type, name, body,
        object_type=object_type, scope_id=scope_id, device_function=device_function,
        dry_run=dry_run,
    )


@mcp.tool(annotations=DESTRUCTIVE)
def delete_network_profile(
    profile_type: str,
    name: str,
    object_type: str | None = None,
    scope_id: str | None = None,
    device_function: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete a network-config library profile by name. See get_network_profile for profile_type."""
    return _delete_network_profile(
        profile_type, name,
        object_type=object_type, scope_id=scope_id, device_function=device_function,
        dry_run=dry_run,
    )


# ── Routing Overlays: BGP / OSPF / VRF ────────────────────────────────────────

@mcp.tool(annotations=IDEMPOTENT_WRITE)
def build_bgp_overlay(
    name: str,
    router: list[dict[str, Any]],
    scope_id: str,
    device_function: str = "GATEWAY",
    description: str | None = None,
    platform_type: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create a LOCAL-scoped BGP routing-overlay profile bound to scope_id + device_function.

    router: list with exactly one BGP router-instance dict (AS number,
    neighbors, route policies, timers, address families) — a BGP profile
    supports only one Autonomous System per profile; pass the router-instance
    body through as-is per the BGP profile schema. device_function:
    GATEWAY | CX_SWITCH | PVOS_SWITCH | AP | BRIDGE.
    """
    body: dict[str, Any] = {"name": name, "router": router}
    if description:
        body["description"] = description
    if platform_type:
        body["platform-type"] = platform_type
    return _upsert_network_profile(
        "bgp", name, body,
        object_type="LOCAL", scope_id=scope_id, device_function=device_function, dry_run=dry_run,
    )


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def build_ospf_overlay(
    name: str,
    body: dict[str, Any],
    scope_id: str,
    device_function: str = "GATEWAY",
    version: str = "v2",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create a LOCAL-scoped OSPF routing-overlay profile bound to scope_id + device_function.

    version: "v2" (OSPFv2/IPv4) or "v3" (OSPFv3/IPv6). body is the full OSPF
    profile document (areas, interfaces, redistribution, timers) — pass
    through as-is per the ospfv2/ospfv3 profile schema; `name` is injected.
    """
    if version not in ("v2", "v3"):
        raise ValueError("version must be 'v2' or 'v3'")
    payload = {**body, "name": name}
    return _upsert_network_profile(
        f"ospf{version}", name, payload,
        object_type="LOCAL", scope_id=scope_id, device_function=device_function, dry_run=dry_run,
    )


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def build_vrf_overlay(
    name: str,
    body: dict[str, Any],
    scope_id: str,
    device_function: str = "GATEWAY",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create a LOCAL-scoped VRF bound to scope_id + device_function.

    body is the full VRF profile document (route-distinguisher, route
    targets, interfaces) — pass through as-is per the vrf profile schema;
    `name` is injected.
    """
    payload = {**body, "name": name}
    return _upsert_network_profile(
        "vrf", name, payload,
        object_type="LOCAL", scope_id=scope_id, device_function=device_function, dry_run=dry_run,
    )


# ── High Availability: VSX + VRRP ─────────────────────────────────────────────

@mcp.tool(annotations=IDEMPOTENT_WRITE)
def configure_high_availability(
    vsx_name: str,
    vsx_body: dict[str, Any],
    vrrp_name: str,
    vrrp_body: dict[str, Any],
    scope_id: str,
    device_function: str = "CX_SWITCH",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Configure switch/gateway HA: a VSX profile plus a VRRP redundancy profile, both LOCAL-scoped.

    vsx_body / vrrp_body are the full profile documents per the vsx-profiles
    and vrrp-global schemas respectively — pass through as-is; `name` is
    injected into each. Both profiles are bound to the same scope_id +
    device_function. Either half can fail independently — check
    result["vsx"]["errors"] / result["vrrp"]["errors"].
    """
    vsx_payload = {**vsx_body, "name": vsx_name}
    vrrp_payload = {**vrrp_body, "name": vrrp_name}
    vsx_result = _upsert_network_profile(
        "vsx", vsx_name, vsx_payload,
        object_type="LOCAL", scope_id=scope_id, device_function=device_function, dry_run=dry_run,
    )
    vrrp_result = _upsert_network_profile(
        "vrrp", vrrp_name, vrrp_payload,
        object_type="LOCAL", scope_id=scope_id, device_function=device_function, dry_run=dry_run,
    )
    return {"vsx": vsx_result, "vrrp": vrrp_result}


# ── Telemetry ─────────────────────────────────────────────────────────────────

@mcp.tool(annotations=IDEMPOTENT_WRITE)
def enable_telemetry(
    name: str,
    body: dict[str, Any],
    scope_id: str,
    device_function: str = "CAMPUS_AP",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create a LOCAL-scoped telemetry profile (streaming/export destinations)
    bound to scope_id + device_function.

    body is the full telemetry profile document — pass through as-is per
    the telemetry profile schema; `name` is injected.
    """
    payload = {**body, "name": name}
    return _upsert_network_profile(
        "telemetry", name, payload,
        object_type="LOCAL", scope_id=scope_id, device_function=device_function, dry_run=dry_run,
    )


# ── Application Experience ────────────────────────────────────────────────────

@mcp.tool(annotations=IDEMPOTENT_WRITE)
def configure_application_experience(
    bandwidth_contract_name: str,
    bandwidth_contract_body: dict[str, Any],
    arc_name: str,
    arc_body: dict[str, Any],
    scope_id: str,
    device_function: str = "GATEWAY",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Configure application experience: an app-bandwidth-contract profile plus an
    Application Recognition & Control (ARC) profile, both LOCAL-scoped.

    bandwidth_contract_body / arc_body are the full profile documents per
    the app-bandwidth-contracts and arc schemas respectively — pass through
    as-is; `name` is injected into each. Either half can fail independently
    — check result["app_bandwidth_contract"]["errors"] /
    result["app_recognition"]["errors"].
    """
    bw_payload = {**bandwidth_contract_body, "name": bandwidth_contract_name}
    arc_payload = {**arc_body, "name": arc_name}
    bw_result = _upsert_network_profile(
        "app-bandwidth-contract", bandwidth_contract_name, bw_payload,
        object_type="LOCAL", scope_id=scope_id, device_function=device_function, dry_run=dry_run,
    )
    arc_result = _upsert_network_profile(
        "app-recognition", arc_name, arc_payload,
        object_type="LOCAL", scope_id=scope_id, device_function=device_function, dry_run=dry_run,
    )
    return {"app_bandwidth_contract": bw_result, "app_recognition": arc_result}


# ── Configuration Checkpoint / Rollback ───────────────────────────────────────

@mcp.tool(annotations=IDEMPOTENT_WRITE)
def build_config_checkpoint_policy(
    name: str,
    scope_id: str,
    device_function: str,
    post_checkpoint: bool = True,
    post_checkpoint_delay: int = 300,
    description: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create/update a LOCAL-scoped config-checkpoint policy bound to scope_id + device_function.

    post_checkpoint: when True (default), the device generates a new
    configuration checkpoint post_checkpoint_delay seconds (5-600, default
    300) after a config change is applied. This is the *only* checkpoint
    control New Central exposes via API — there is no "create a checkpoint
    now" or "roll back to checkpoint N" endpoint. See
    get_config_rollback_status for why, and use dry_run=True to preview.
    """
    if not (5 <= post_checkpoint_delay <= 600):
        raise ValueError("post_checkpoint_delay must be between 5 and 600 seconds")
    body: dict[str, Any] = {
        "name": name,
        "post-checkpoint": post_checkpoint,
        "post-checkpoint-delay": post_checkpoint_delay,
    }
    if description:
        body["description"] = description
    return _upsert_network_profile(
        "config-checkpoint", name, body,
        object_type="LOCAL", scope_id=scope_id, device_function=device_function, dry_run=dry_run,
    )


@mcp.tool(annotations=READ_ONLY)
def get_config_rollback_status() -> dict[str, Any]:
    """Explain New Central's configuration checkpoint/rollback behavior.

    New Central has NO manual "roll back to checkpoint" API. Rollback is
    automatic and device-side: when a device fails to apply a newly pushed
    configuration, it reverts to its last-known-good checkpoint on its own.
    build_config_checkpoint_policy only controls whether/how quickly Central
    asks the device to snapshot a *post*-change checkpoint — it does not let
    you trigger a rollback, list prior checkpoints, or pick which one to
    restore.

    Sources:
    - developer.arubanetworks.com/new-central-config/reference/config-checkpoint
      (profile schema is limited to name/description/post-checkpoint/
      post-checkpoint-delay — no snapshot list or restore operation).
    - New Central tech-docs FAQ "Do access points and switches typically
      support Rollback feature?" — confirms rollback is automatic
      device-side behavior, not an operator-triggered action.

    Use list_devices_config_health / get_device_config_issues (central-monitoring)
    plus GLP audit logs to confirm whether an automatic rollback already
    occurred on a device.
    """
    return {
        "manual_rollback_supported": False,
        "automatic_rollback_supported": True,
        "checkpoint_profile_tool": "build_config_checkpoint_policy",
        "citation": [
            "developer.arubanetworks.com/new-central-config/reference/config-checkpoint",
            "New Central tech-docs FAQ: 'Do access points and switches typically support "
            "Rollback feature?'",
        ],
        "guidance": (
            "There is no endpoint to list checkpoints, trigger a rollback, or choose which "
            "checkpoint to restore. Rollback happens automatically on the device when a "
            "pushed config fails to apply. Use build_config_checkpoint_policy to control "
            "post-checkpoint generation, and list_devices_config_health / "
            "get_device_config_issues to see current config-health state."
        ),
    }


# ── VSF Template Lifecycle (v0.7 bounded workflow) ────────────────────────────
#
# Stricter write contract than the build_*/set_network_profile pair above:
# dry_run=True by default, an explicit confirm=True is required to execute,
# the write is gated behind the existing Central write gate
# (HPE_MCP_CENTRAL_WRITES, enforce_platform_write), the executed result is
# validated, and a read-back GET confirms what Central actually stored.
# Schema: GET/POST/PATCH/DELETE /network-config/v1alpha1/vsf-templates[/{name}]
# (manifest-confirmed: ingestion/sources/openapi_specs/vsf-template.json,
# also central.json operations central_read_vsf_templates /
# central_create_vsf_templates / central_update_vsf_templates /
# central_delete_vsf_templates). number-of-members is the only
# schema-required body field (1-10); name/description/conductor-serials are
# optional. There is no New Central endpoint for Classic-style config
# templates with %variable% substitution or a separate template-assignment
# step — object-type=LOCAL on create *is* the assignment (bound directly to
# scope_id + device_function). Do not probe for a Classic-shaped
# variables/assignment endpoint; none exists in this manifest.

_VSF_TEMPLATE_PROFILE_TYPE = "vsf-template"


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def build_vsf_template(
    name: str,
    number_of_members: int,
    scope_id: str,
    device_function: str = "ACCESS_SWITCH",
    description: str | None = None,
    conductor_serials: list[str] | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create a LOCAL-scoped VSF (Virtual Switching Framework) stacking template, then read it back.

    number_of_members must be 1-10 (schema minimum/maximum). The template
    is bound directly to scope_id + device_function (object-type=LOCAL) —
    this *is* the assignment step; there is no separate assignment call.
    dry_run=True (default) returns the request preview with no network
    call. Set dry_run=False and confirm=True to execute — this also requires
    full-read-write or custom access with HPE_MCP_CENTRAL_WRITES=1 (Central
    writes are denied by default). On success the response is
    validated (raises WriteResultError on a non-2xx or error-shaped result)
    and a GET read-back is attached under
    result["read_back"] to confirm what Central actually stored.
    Cleanup: call delete_vsf_template(name, scope_id=..., device_function=...,
    dry_run=False, confirm=True) to remove a disposable/lab template.
    """
    if not (1 <= number_of_members <= 10):
        raise ValueError("number_of_members must be between 1 and 10")
    if not scope_id:
        raise ValueError("scope_id is required (VSF templates are always LOCAL-scoped)")

    body: dict[str, Any] = {"number-of-members": number_of_members}
    if description:
        body["description"] = description
    if conductor_serials:
        body["conductor-serials"] = conductor_serials
    params = _profile_write_params("LOCAL", scope_id, device_function)
    endpoint = f"{_profile_base(_VSF_TEMPLATE_PROFILE_TYPE)}/{quote(name, safe='')}"

    if dry_run:
        return {
            "dry_run": True, "name": name, "endpoint": endpoint,
            "params": params, "payload": body,
        }
    blocked = enforce_platform_write("central", "build_vsf_template")
    if blocked:
        return blocked
    if not confirm:
        return {
            "error": "confirm=True is required when dry_run=False.",
            "dry_run": True, "name": name, "endpoint": endpoint,
            "params": params, "payload": body,
        }

    client = get_client()
    response = client._request("POST", endpoint, json=body, params=params or None)
    action = "created"
    if response.status_code == 412:
        action = "updated"
        response = client._request("PATCH", endpoint, json=body, params=params or None)
    validate_write_result(response, context=f"{action} {endpoint}")
    result = resp_json(response)
    validate_write_result(result, context=f"{action} {endpoint}")

    read_back_resp = client._request("GET", endpoint, params=params or None)
    read_back = resp_json(read_back_resp) if read_back_resp.is_success else {
        "error": compact_http_error(read_back_resp, endpoint)
    }
    return {"action": action, "name": name, "response": result, "read_back": read_back}


@mcp.tool(annotations=DESTRUCTIVE)
def delete_vsf_template(
    name: str,
    scope_id: str,
    device_function: str = "ACCESS_SWITCH",
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Delete a LOCAL-scoped VSF stacking template, then read back to confirm removal.

    dry_run=True (default) returns the request preview with no network
    call. Set dry_run=False and confirm=True to execute — this also requires
    full-read-write or custom access with HPE_MCP_CENTRAL_WRITES=1 (Central
    writes are denied by default). On success the response is
    validated (raises WriteResultError on a non-2xx or error-shaped result)
    and a read-back GET is attached under
    result["read_back"]; an HTTP 404 on read-back confirms the template no
    longer exists (surfaced as read_back={"deleted_confirmed": True}).
    """
    params = _profile_write_params("LOCAL", scope_id, device_function)
    endpoint = f"{_profile_base(_VSF_TEMPLATE_PROFILE_TYPE)}/{quote(name, safe='')}"

    if dry_run:
        return {"dry_run": True, "name": name, "endpoint": endpoint, "params": params}
    blocked = enforce_platform_write("central", "delete_vsf_template")
    if blocked:
        return blocked
    if not confirm:
        return {
            "error": "confirm=True is required when dry_run=False.",
            "dry_run": True, "name": name, "endpoint": endpoint, "params": params,
        }

    client = get_client()
    response = client._request("DELETE", endpoint, params=params or None)
    validate_write_result(response, context=f"DELETE {endpoint}")
    result = resp_json(response)
    validate_write_result(result, context=f"DELETE {endpoint}")

    read_back_resp = client._request("GET", endpoint, params=params or None)
    if read_back_resp.status_code == 404:
        read_back = {"deleted_confirmed": True}
    elif read_back_resp.is_success:
        read_back = {"deleted_confirmed": False, "still_present": resp_json(read_back_resp)}
    else:
        read_back = {
            "deleted_confirmed": None,
            "error": compact_http_error(read_back_resp, endpoint),
        }
    return {"name": name, "response": result, "read_back": read_back}


# ── Bulk Site / Site-Collection Delete (v0.7 bounded workflow) ───────────────
#
# Schema: DELETE /network-config/v1/sites/bulk and
# /network-config/v1/site-collections/bulk (manifest-confirmed:
# scope-management-1fkx1wmq0s.json — same request shape as the existing
# delete_device_groups: {"items": [{"id": ...}, ...]}). The v1alpha1
# equivalents are deprecated in favor of these v1 paths. Same
# dry_run+confirm+gate contract as build_vsf_template above; these are
# irreversible so no read-back GET is attempted for a deleted site/
# site-collection.

_MAX_BULK_DELETE_ITEMS = 100


def _bulk_delete_ids(ids: list[str], field_name: str) -> list[str]:
    if not ids:
        raise ValueError(f"{field_name} must be a non-empty list")
    if len(ids) > _MAX_BULK_DELETE_ITEMS:
        raise ValueError(
            f"{field_name} cannot exceed {_MAX_BULK_DELETE_ITEMS} entries (got {len(ids)})"
        )
    cleaned = [str(i).strip() for i in ids]
    if any(not i for i in cleaned):
        raise ValueError(f"{field_name} entries must be non-empty strings")
    return cleaned


@mcp.tool(annotations=DESTRUCTIVE)
def delete_sites_bulk(
    site_ids: list[str],
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Bulk-delete sites by scope ID list (up to 100 per call).

    DELETE /network-config/v1/sites/bulk (manifest-confirmed). dry_run=True
    (default) returns the payload preview with no network call. Set
    dry_run=False and confirm=True to execute — requires full-read-write or
    custom access with HPE_MCP_CENTRAL_WRITES=1 (Central writes are denied by
    default). Irreversible: there is no undo/read-back
    for a deleted site — verify site_ids with list_sites first.
    """
    ids = _bulk_delete_ids(site_ids, "site_ids")
    endpoint = "/network-config/v1/sites/bulk"
    payload = {"items": [{"id": sid} for sid in ids]}
    if dry_run:
        return {"dry_run": True, "endpoint": endpoint, "payload": payload}
    blocked = enforce_platform_write("central", "delete_sites_bulk")
    if blocked:
        return blocked
    if not confirm:
        return {
            "error": "confirm=True is required when dry_run=False.",
            "dry_run": True, "endpoint": endpoint, "payload": payload,
        }
    client = get_client()
    response = client._request("DELETE", endpoint, json=payload)
    validate_write_result(response, context=f"DELETE {endpoint}")
    result = resp_json(response)
    validate_write_result(result, context=f"DELETE {endpoint}")
    return result


@mcp.tool(annotations=DESTRUCTIVE)
def delete_site_collections_bulk(
    site_collection_ids: list[str],
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Bulk-delete site collections by scope ID list (up to 100 per call).

    DELETE /network-config/v1/site-collections/bulk (manifest-confirmed).
    dry_run=True (default) returns the payload preview with no network
    call. Set dry_run=False and confirm=True to execute — requires
    full-read-write or custom access with HPE_MCP_CENTRAL_WRITES=1 (Central
    writes are denied by default). Irreversible: there is no
    undo/read-back for a deleted site collection.
    """
    ids = _bulk_delete_ids(site_collection_ids, "site_collection_ids")
    endpoint = "/network-config/v1/site-collections/bulk"
    payload = {"items": [{"id": sid} for sid in ids]}
    if dry_run:
        return {"dry_run": True, "endpoint": endpoint, "payload": payload}
    blocked = enforce_platform_write("central", "delete_site_collections_bulk")
    if blocked:
        return blocked
    if not confirm:
        return {
            "error": "confirm=True is required when dry_run=False.",
            "dry_run": True, "endpoint": endpoint, "payload": payload,
        }
    client = get_client()
    response = client._request("DELETE", endpoint, json=payload)
    validate_write_result(response, context=f"DELETE {endpoint}")
    result = resp_json(response)
    validate_write_result(result, context=f"DELETE {endpoint}")
    return result


# ── Firmware Compliance Campaign (v0.7 bounded workflow) ─────────────────────
#
# Orchestrates the existing manifest-confirmed set_firmware_compliance /
# get_firmware_compliance calls (POST-then-PATCH-on-409 against
# /network-config/v1alpha1/firmware-compliance) across multiple
# scope_id/device_function targets in one bounded call. There is no bulk
# firmware-compliance endpoint in the New Central schema — this composes
# the single-target endpoint per target rather than inventing one. Every
# target is attempted independently: one target's failure never aborts the
# rest of the campaign (partial failure is reported per-target, not
# swallowed).

_MAX_CAMPAIGN_TARGETS = 25


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def run_firmware_compliance_campaign(
    targets: list[dict[str, str]],
    firmware_version: str,
    upgrade_mode: str = "REGULAR",
    reboot_schedule_mode: str = "IMMEDIATE",
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Apply one firmware-compliance policy across multiple scopes/personas in a bounded campaign.

    targets: list of {"scope_id": ..., "device_function": ...} dicts, up to
    25 entries. Each target calls the same manifest-confirmed
    /network-config/v1alpha1/firmware-compliance POST-then-PATCH-on-409
    flow as set_firmware_compliance — one HTTP call per target; this tool
    does not invent a bulk-firmware endpoint. dry_run=True (default) returns
    every target's payload preview with no network calls. Set dry_run=False
    and confirm=True to execute — requires full-read-write or custom access
    with HPE_MCP_CENTRAL_WRITES=1 (Central writes are denied by default).
    Every target is attempted even if an earlier one fails (see
    result["targets_failed"] / each entry's "error");
    after a successful write each target is read back via
    get_firmware_compliance into that entry's "read_back". Cleanup/rollback: call
    set_firmware_compliance again per target with the prior firmware
    version.
    """
    if not targets:
        raise ValueError("targets must be a non-empty list")
    if len(targets) > _MAX_CAMPAIGN_TARGETS:
        raise ValueError(
            f"targets cannot exceed {_MAX_CAMPAIGN_TARGETS} entries (got {len(targets)})"
        )
    for index, target in enumerate(targets):
        if (
            not isinstance(target, dict)
            or not str(target.get("scope_id", "")).strip()
            or not str(target.get("device_function", "")).strip()
        ):
            raise ValueError(
                f"targets[{index}] must be a dict with non-empty 'scope_id' and 'device_function'"
            )

    if not dry_run:
        blocked = enforce_platform_write("central", "run_firmware_compliance_campaign")
        if blocked:
            return blocked
        if not confirm:
            return {
                "error": "confirm=True is required when dry_run=False.",
                "dry_run": True,
                "targets": targets,
                "firmware_version": firmware_version,
            }

    results: list[dict[str, Any]] = []
    for target in targets:
        scope_id = str(target["scope_id"]).strip()
        device_function = str(target["device_function"]).strip()
        if dry_run:
            results.append(
                set_firmware_compliance(
                    scope_id, device_function, firmware_version,
                    upgrade_mode, reboot_schedule_mode, dry_run=True,
                )
            )
            continue
        try:
            write_result = set_firmware_compliance(
                scope_id, device_function, firmware_version,
                upgrade_mode, reboot_schedule_mode, dry_run=False,
            )
        except Exception as exc:
            results.append({
                "scope_id": scope_id, "device_function": device_function, "error": str(exc),
            })
            continue
        if write_result.get("errors"):
            results.append({
                "scope_id": scope_id, "device_function": device_function,
                "error": "; ".join(write_result["errors"]), "write_result": write_result,
            })
            continue
        read_back = get_firmware_compliance(scope_id, device_function)
        results.append({
            "scope_id": scope_id, "device_function": device_function,
            "write_result": write_result, "read_back": read_back,
        })

    succeeded = sum(1 for entry in results if "error" not in entry)
    return {
        "dry_run": dry_run,
        "firmware_version": firmware_version,
        "targets_attempted": len(targets),
        "targets_succeeded": succeeded,
        "targets_failed": len(targets) - succeeded,
        "results": results,
    }


if __name__ == "__main__":
    from hpe_networking_mcp.mcp_servers._cache_hygiene import stable_list_tools
    from hpe_networking_mcp.mcp_servers._middleware import (
        NullStripMiddleware,
        RateLimitMiddleware,
        SecretTokenizeMiddleware,
        install_middleware,
    )
    stable_list_tools(mcp)
    install_middleware(
        mcp,
        [
            NullStripMiddleware(),
            RateLimitMiddleware(rate=8.0),
            SecretTokenizeMiddleware(),
        ],
    )
    from hpe_networking_mcp.mcp_servers.shared import run_server
    run_server(mcp)
