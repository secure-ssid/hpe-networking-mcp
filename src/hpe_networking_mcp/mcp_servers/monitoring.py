"""MCP server — Aruba Central monitoring and operational health tools (90 tools).

Covers: sites, devices, clients, alerts, events, scopes, inventory,
audit logs, device health/trends, switch ports/VLANs/PoE, AP radios/ports,
SLE metrics, WLANs, gateway clusters, anomaly detection (client flapping,
SSH brute force), site health summary, client roaming history, switch stacking,
rogue APs, AP neighbors, channel utilization, client signal history, air quality,
SSID clients, client location, topology, BSSID inventory, swarm inventory, AP tunnel telemetry,
application visibility, reporting (reports/report-runs/metadata/health, plus
manifest-confirmed report create/get/update/delete and report-run
delete/download-link execution), client onboarding events, best-effort
notification-rule CRUD, and a guarded read-only `central_get` escape hatch
for the Monitoring/Notifications/Reporting/Services/Troubleshooting
registries in the committed Central manifest, a bounded config-health
remediation workflow (plan_config_health_remediation +
execute_config_health_remediation: chunked resync with per-chunk read-back
and partial-failure reporting), and a bounded per-device
telemetry-to-remediation planner (plan_device_troubleshooting) that maps
live health/alerts/events onto existing read, diagnostic, and gated
remediation tools without executing writes, plus a bounded site planner
(plan_site_troubleshooting) that ranks devices from inventory and alerts.

Cursor pagination note: clients/radios/BSSIDs/gateways/WLANs/alerts/device-inventory
paginate with a `next` cursor (not offset) per the v1 reference docs — list
tools accept both `next_cursor` (preferred) and a legacy `offset` that is
translated to an approximate starting cursor.
"""

import time
from typing import Any
from urllib.parse import quote

from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel

from hpe_networking_mcp.mcp_servers.shared import (
    DESTRUCTIVE,
    IDEMPOTENT_WRITE,
    READ_ONLY,
    WRITE,
    WriteResultError,
    bound_collection_response,
    clamp_limit,
    compact_http_error,
    enforce_platform_write,
    get_client,
    get_mcp_client,
    maybe_bound,
    safe_api_path,
    validate_write_result,
)
from hpe_networking_mcp.pipeline.scope_ids import normalize_scope_id

mcp = MCPServer("central-monitoring")

_SCOPE_PAGE_SIZE = 100
_SCOPE_MAX_PAGES = 50


def _scope_field(raw: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, str):
            value = value.strip()
        if value not in (None, ""):
            return str(value)
    return None


def _global_scope_field(raw: dict[str, Any]) -> str | None:
    for key in ("scopeId", "id"):
        try:
            return normalize_scope_id(raw.get(key), field_name=key)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# AP reboot reason translation
# ---------------------------------------------------------------------------

_REBOOT_REASON_MAP: dict[str, str] = {
    "UNKNOWN": "Unknown",
    "AP_RELOAD": "Reload",
    "USER_REBOOT": "User reboot",
    "WRITE_ERASE_REBOOT": "Write erase reboot",
    "WRITE_ERASE_ALL_REBOOT": "Write erase all reboot",
    "IMAGE_SYNC_FAILED": "Image sync failed",
    "IMAGE_SYNC_SUCCESSFUL": "Image sync successful",
    "IMAGE_UPGRADE": "Image upgrade successful",
    "IMAGE_DOWNLOAD_FAILURE": "Image download failure",
    "OUT_OF_MEMORY": "Reboot caused by out of memory",
    "DOWN_UPLINK": "Current uplink down, no useable uplink",
    "CONDUCTOR_TO_LOCAL": "Conductor transitioned to local",
    "NETWORK_DISCONNECT_USB_RESET": "Internet connection lost, reset USB modem",
    "NETWORK_DISCONNECT": "Internet connection lost",
    "UNREACHABLE_GATEWAY": "Gateway unreachable",
    "FATAL_EXCEPTION": "Kernel panic: fatal exception",
    "FATAL_EXCEPTION_IN_INTERRUPT": "Kernel panic: fatal exception in interrupt",
    "SOFTLOCKUP": "Kernel panic: softlockup/hung tasks",
    "NTP_SYNC": "System clock too far ahead of NTP sync",
    "BAD_MESH_LINK": "Mesh link bad — rebooting mesh point",
}


def _translate_reboot_reason(device: dict) -> dict:
    """Translate raw reboot reason code to human-readable string in-place."""
    reason = device.get("lastRebootReason") or device.get("last_reboot_reason")
    if reason and reason in _REBOOT_REASON_MAP:
        if "lastRebootReason" in device:
            device["lastRebootReason"] = _REBOOT_REASON_MAP[reason]
        elif "last_reboot_reason" in device:
            device["last_reboot_reason"] = _REBOOT_REASON_MAP[reason]
    return device


# ── Guarded generic GET escape hatch ─────────────────────────────────────────
#
# Modeled on glp.py's `glp_get`: a bounded, allow-listed read-only GET for
# the Monitoring, Notifications, Reporting, Services, and Troubleshooting
# registries in the committed Central manifest
# (src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/central.json) that don't yet have a
# dedicated curated tool in this module. `src/hpe_networking_mcp/mcp_servers/central_generated.py`
# already exposes every manifest operation as a typed `central_*` tool when
# enabled; this tool exists for callers of the curated central-monitoring
# backend who want the same documented-path exploration without depending
# on that separate, much larger generated server.

_CENTRAL_GET_PREFIXES = (
    "/network-monitoring/v1/",
    "/network-notifications/v1/",
    "/network-reporting/v1/",
    "/network-services/v1/",
    "/network-troubleshooting/v1/",
)


@mcp.tool(annotations=READ_ONLY)
def central_get(
    path: str,
    params: dict[str, Any] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Perform a guarded read-only GET against reviewed Central API families.

    Path must be relative and begin with one of the Monitoring,
    Notifications, Reporting, Services, or Troubleshooting prefixes below
    (see `list_central_get_prefixes`). List payloads are bounded with
    `limit` (clamped to the shared MCP list-limit ceiling, currently 200)
    and `offset`.
    """
    try:
        safe_path = safe_api_path(path, _CENTRAL_GET_PREFIXES)
    except ValueError as exc:
        return {"error": f"Invalid path. {exc}"}
    try:
        data = get_client().get(safe_path, params=params or {})
        data = bound_collection_response(data, limit=clamp_limit(limit), offset=max(0, offset))
        return {"data": data, "endpoint_used": safe_path}
    except Exception as exc:
        return {"error": str(exc), "endpoint_used": safe_path}


@mcp.tool(annotations=READ_ONLY)
def list_central_get_prefixes() -> dict[str, Any]:
    """List the guarded path-prefixes reachable via `central_get`.

    Prefer the dedicated curated tools in this module for common workflows
    (e.g. list_reports/list_report_runs/get_reports_metadata/get_report/
    create_report/update_report/delete_report/delete_report_run/
    get_report_run_download_link for reporting, list_active_alerts/
    list_alert_configs for notifications). `central_get` covers documented
    resources under these prefixes that don't have a named tool yet.
    """
    return {
        "guarded_get_prefixes": list(_CENTRAL_GET_PREFIXES),
        "manifest_source": "src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/central.json",
        "manifest_registries": [
            "Monitoring",
            "Notifications",
            "Reporting",
            "Services",
            "Troubleshooting",
        ],
        "note": (
            "For the full set of ~1.7k generated operations across every "
            "reviewed registry (including network-config), see the separate "
            "central-generated backend (src/hpe_networking_mcp/mcp_servers/central_generated.py)."
        ),
    }


# ── Sites ────────────────────────────────────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
def list_sites(
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Return sites with IDs, names, and location fields (paginated)."""
    off = max(0, offset)
    sites = get_mcp_client().get_sites(limit=clamp_limit(limit), offset=off)
    # get_sites already returned the paginated slice — pass offset=0 so
    # maybe_bound doesn't re-slice it; report the true offset instead.
    wrapped = maybe_bound(sites, limit=limit, offset=0)
    if isinstance(wrapped, dict) and "_pagination" in wrapped:
        wrapped["_pagination"]["offset"] = off
    return wrapped


@mcp.tool(annotations=READ_ONLY)
def get_site(name: str) -> dict[str, Any] | None:
    """Find a site by name (case-insensitive). Returns None if not found."""
    name_lower = name.lower()
    page_size = 100
    offset = 0
    for _ in range(50):
        sites = get_mcp_client().get_sites(limit=page_size, offset=offset)
        if not sites:
            break
        for site in sites:
            site_name = site.get("scopeName") or site.get("siteName") or site.get("name", "")
            if site_name.lower() == name_lower:
                return site
        if len(sites) < page_size:
            break
        offset += page_size
    return None


# ── Devices ──────────────────────────────────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
def list_devices(
    device_type: str | None = None,
    site_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    next_cursor: str | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """List devices, optionally filtered by device_type (SWITCH/AP) or site_id.

    device-inventory paginates with a `next` cursor, not offset — pass
    next_cursor from a prior response's _pagination.next_cursor to page
    forward. offset is accepted for backward compatibility and translated
    to an approximate starting cursor when next_cursor is omitted.
    """
    filters: dict[str, Any] = {}
    if device_type:
        filters["deviceType"] = device_type
    if site_id:
        filters["siteId"] = site_id
    off = max(0, offset)
    cursor = next_cursor or (str(off + 1) if off > 0 else None)
    devices, returned_cursor = get_mcp_client().get_devices_page(
        filters or None, limit=clamp_limit(limit), next_cursor=cursor
    )
    # deviceType query param is ignored server-side; apply client-side post-filter.
    if device_type:
        want = device_type.upper()
        if want == "AP":
            want = "ACCESS_POINT"
        devices = [d for d in devices if want in (d.get("deviceType") or "").upper()]
    # Translate AP reboot reason codes
    for d in devices:
        dtype_upper = (d.get("deviceType") or "").upper()
        if dtype_upper in ("AP", "ACCESS_POINT") or "AP" in dtype_upper:
            _translate_reboot_reason(d)

    wrapped = maybe_bound(devices, limit=limit, offset=0)
    if isinstance(wrapped, dict) and "_pagination" in wrapped:
        wrapped["_pagination"]["offset"] = off
        wrapped["_pagination"]["next_cursor"] = returned_cursor
    return wrapped


@mcp.tool(annotations=READ_ONLY)
def find_device(serial_number: str) -> dict[str, Any] | None:
    """Find a single device by serial number. Returns the device record or None."""
    result = get_mcp_client().get_device_by_serial(serial_number)
    if result:
        dt = (result.get("deviceType") or "").upper()
        if "ACCESS_POINT" in dt or dt == "AP":
            _translate_reboot_reason(result)
    return result


# ── Clients ──────────────────────────────────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
def list_clients(
    site_id: str | None = None,
    serial_number: str | None = None,
    ssid: str | None = None,
    connection_type: str | None = None,
    hostname_contains: str | None = None,
    os_contains: str | None = None,
    device_type_contains: str | None = None,
    ssid_contains: str | None = None,
    site_contains: str | None = None,
    limit: int = 100,
    offset: int = 0,
    next_cursor: str | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """List connected clients. ALWAYS filter — unfiltered returns all clients.

    Server-side: site_id, serial_number, ssid, connection_type (Wireless/Wired).
    Client-side substring (case-insensitive, prefer for natural-language queries):
    hostname_contains, os_contains, device_type_contains, ssid_contains, site_contains.

    /network-monitoring/v1/clients paginates with a `next` cursor, not
    offset. Pass next_cursor from a prior response's _pagination.next_cursor
    to page forward; offset is accepted for backward compatibility and
    translated to an approximate starting cursor when next_cursor is omitted.
    Cursor pagination is applied before client-side substring filters.
    """
    off = max(0, offset)
    cursor = next_cursor or (str(off + 1) if off > 0 else None)
    clients, returned_cursor = get_mcp_client().get_clients_page(
        site_id=site_id,
        serial_number=serial_number,
        ssid=ssid,
        connection_type=connection_type,
        limit=clamp_limit(limit),
        next_cursor=cursor,
    )

    # Client-side substring filters. Each filter checks multiple possible field
    # names because Central's v1 and v1alpha1 responses differ.
    def _match(client: dict[str, Any], needle: str, fields: tuple[str, ...]) -> bool:
        n = needle.lower()
        for f in fields:
            v = client.get(f)
            if v and n in str(v).lower():
                return True
        return False

    filters: list[tuple[str, tuple[str, ...]]] = []
    if hostname_contains:
        filters.append((hostname_contains, ("hostName", "clientName", "hostname", "name")))
    if os_contains:
        filters.append((os_contains, ("clientOperatingSystem", "osType")))
    if device_type_contains:
        filters.append(
            (device_type_contains, ("connectedDeviceType", "clientFunction", "clientCategory"))
        )
    if ssid_contains:
        filters.append((ssid_contains, ("network", "wlanName", "ssid", "SSID")))
    if site_contains:
        filters.append((site_contains, ("siteName", "site_name", "site", "scopeName")))

    if filters and isinstance(clients, list):
        clients = [
            c for c in clients if all(_match(c, needle, fields) for needle, fields in filters)
        ]

    wrapped = maybe_bound(clients, limit=limit, offset=0)
    if isinstance(wrapped, dict) and "_pagination" in wrapped:
        wrapped["_pagination"]["offset"] = off
        wrapped["_pagination"]["next_cursor"] = returned_cursor
    return wrapped


@mcp.tool(annotations=READ_ONLY)
def find_client(mac_or_ip: str) -> dict[str, Any] | None:
    """Find a connected client by MAC address or IP address."""
    return get_mcp_client().find_client(mac_or_ip)


@mcp.tool(annotations=READ_ONLY)
def get_client_details(mac_address: str) -> dict[str, Any]:
    """Fetch detailed info (usage, bandwidth, auth) for a single client by MAC address."""
    client = get_client()
    errors: list[str] = []
    mac = mac_address.replace(":", "").replace("-", "").lower()

    for endpoint in [
        f"/network-monitoring/v1/clients/{mac_address}",
        f"/network-monitoring/v1/clients/details?macAddress={mac_address}",
        f"/network-monitoring/v1alpha1/clients/{mac}",
    ]:
        try:
            response = client._request("GET", endpoint)
            if response.status_code == 404:
                errors.append(f"404 at {endpoint}")
                continue
            if response.status_code not in (200, 201, 202):
                errors.append(f"HTTP {response.status_code} at {endpoint}")
                continue
            return {
                "mac_address": mac_address,
                "details": response.json(),
                "endpoint_used": endpoint,
                "errors": errors,
            }
        except Exception as exc:
            errors.append(str(exc))

    return {"mac_address": mac_address, "details": None, "endpoint_used": None, "errors": errors}


# ── Alerts & Events ───────────────────────────────────────────────────────────

_ALERT_ACTIONS_BASE = "/network-notifications/v1/alerts"
_ALERT_CLEAR_REASONS = {
    "Problem was resolved",
    "False Positive",
    "Insufficient information for troubleshooting",
    "Alert is not important",
    "Other",
}
_ALERT_PRIORITIES = {"Very High", "High", "Medium", "Low", "Very Low"}
_SEARCH_MIN_CHARS = 3
_SEARCH_MAX_CHARS = 128
_ALERT_CLASSIFICATIONS = {
    "severity",
    "status",
    "priority",
    "category",
    "device_type",
    "impacted_devices",
}
_ALERT_CONFIG_SCOPE_TYPES = {"GLOBAL", "SITE", "DEVICE"}


class _ConfirmAction(BaseModel):
    confirm: bool = False


def _require_non_empty_strings(values: list[str], field: str) -> list[str]:
    cleaned = [str(value).strip() for value in values if str(value).strip()]
    if not cleaned:
        raise ValueError(f"{field} must contain at least one non-empty value")
    return cleaned


def _json_response(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {"items": data}


def _odata_string(value: str) -> str:
    return value.replace("'", "''")


def _items_from_collection(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("items", "scopes", "devices"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


async def _confirm_alert_action(
    ctx: Context, action: str, keys: list[str]
) -> dict[str, Any] | None:
    try:
        result = await ctx.elicit(
            message=f"Confirm alert {action} for {len(keys)} alert key(s): {keys}",
            schema=_ConfirmAction,
        )
    except Exception as exc:
        return {
            "status": "CONFIRMATION_UNAVAILABLE",
            "error": f"client does not support elicitation; operation NOT performed: {exc}",
        }
    if result.action != "accept" or not result.data.confirm:
        return {"status": "CANCELLED", "detail": "user declined confirmation"}
    return None


def _alert_action(action: str, body: dict[str, Any], submitted_message: str) -> dict[str, Any]:
    endpoint = f"{_ALERT_ACTIONS_BASE}/{action}"
    response = get_client()._request("POST", endpoint, json=body)
    if response.status_code not in (200, 201, 202):
        return {"error": compact_http_error(response, endpoint), "endpoint_used": endpoint}
    data = _json_response(response)
    if not data:
        data = {"submitted": True, "message": submitted_message}
    data.setdefault("endpoint_used", endpoint)
    return data


@mcp.tool(annotations=READ_ONLY)
def list_alerts(
    site_id: str | None = None,
    severity: str | None = None,
    limit: int = 50,
    offset: int = 0,
    next_cursor: str | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """List active alerts. severity: CRITICAL/MAJOR/MINOR.

    /network-notifications/v1/alerts paginates with a `next` cursor, not
    offset. Pass next_cursor from a prior response's _pagination.next_cursor
    to page forward; offset is translated to an approximate starting cursor
    when next_cursor is omitted.
    """
    off = max(0, offset)
    alerts = get_mcp_client().get_alerts(
        site_id=site_id,
        severity=severity,
        limit=clamp_limit(limit),
        offset=off,
        next_cursor=next_cursor,
    )
    wrapped = maybe_bound(alerts, limit=limit, offset=0)
    if isinstance(wrapped, dict) and "_pagination" in wrapped:
        wrapped["_pagination"]["offset"] = off
    return wrapped


@mcp.tool(annotations=READ_ONLY)
def list_active_alerts(
    site_id: str | None = None,
    severity: str | None = None,
    limit: int = 50,
    offset: int = 0,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    """List active alerts from network-notifications/v1/alerts using OData status filter.

    Paginates with a `next` cursor (getalertlistv1), not offset. Pass
    next_cursor from a prior response's `next` field to page forward;
    offset is translated to an approximate starting cursor when
    next_cursor is omitted.
    """
    client = get_client()
    filters = ["status eq 'Active'"]
    if site_id:
        filters.append(f"siteId eq '{_odata_string(site_id)}'")
    if severity:
        filters.append(f"severity eq '{_odata_string(severity)}'")
    params: dict[str, Any] = {
        "limit": clamp_limit(limit),
        "filter": " and ".join(filters),
        "sort": "severity desc",
    }
    off = max(0, offset)
    cursor = next_cursor or (str(off + 1) if off > 0 else None)
    if cursor:
        params["next"] = cursor
    try:
        return client.get("/network-notifications/v1/alerts", params=params)
    except Exception as exc:
        return {"error": str(exc), "endpoint_used": "/network-notifications/v1/alerts"}


@mcp.tool(annotations=READ_ONLY)
def list_alert_classifications(
    classify_by: str = "severity",
    filter: str | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    """List alert classification metadata from network-notifications/v1/alerts/classification."""
    if classify_by not in _ALERT_CLASSIFICATIONS:
        allowed = ", ".join(sorted(_ALERT_CLASSIFICATIONS))
        raise ValueError(f"classify_by must be one of: {allowed}")
    client = get_client()
    params = {"type": classify_by}
    if filter:
        params["filter"] = filter
    if search:
        params["search"] = search
    try:
        return client.get("/network-notifications/v1/alerts/classification", params=params)
    except Exception as exc:
        return {
            "error": str(exc),
            "endpoint_used": "/network-notifications/v1/alerts/classification",
        }


@mcp.tool(annotations=READ_ONLY)
def list_alert_configs(scope_id: str, scope_type: str = "GLOBAL") -> dict[str, Any]:
    """List alert configuration definitions for a Central scope."""
    scope = scope_id.strip()
    scope_kind = scope_type.strip().upper()
    if not scope:
        raise ValueError("scope_id must be a non-empty string")
    if scope_kind not in _ALERT_CONFIG_SCOPE_TYPES:
        allowed = ", ".join(sorted(_ALERT_CONFIG_SCOPE_TYPES))
        raise ValueError(f"scope_type must be one of: {allowed}")
    return get_client().get(
        "/network-notifications/v1/alert-config",
        params={"scope-id": scope, "scope-type": scope_kind},
    )


@mcp.tool(annotations=READ_ONLY)
def list_insights(
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """List Central Insights recommendation-style observations."""
    params: dict[str, Any] = {"limit": clamp_limit(limit, default=100), "offset": max(0, offset)}
    return get_client().get("/network-notifications/v1/insights", params=params)


@mcp.tool(annotations=READ_ONLY)
def get_alert_action_status(task_id: str) -> dict[str, Any]:
    """Return async status for clear/defer/reactivate/priority alert actions."""
    task = quote(task_id.strip(), safe="")
    if not task:
        raise ValueError("task_id must be a non-empty string")
    endpoint = f"{_ALERT_ACTIONS_BASE}/async-operations/{task}"
    response = get_client()._request("GET", endpoint)
    if response.status_code not in (200, 201, 202):
        return {"error": compact_http_error(response, endpoint), "endpoint_used": endpoint}
    data = _json_response(response)
    data.setdefault("endpoint_used", endpoint)
    return data


@mcp.tool(annotations=DESTRUCTIVE)
async def clear_alerts(
    ctx: Context,
    keys: list[str],
    reason: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Clear one or more alerts by key. Returns the Central async task payload."""
    alert_keys = _require_non_empty_strings(keys, "keys")
    if reason not in _ALERT_CLEAR_REASONS:
        allowed = ", ".join(sorted(_ALERT_CLEAR_REASONS))
        raise ValueError(f"reason must be one of: {allowed}")
    body: dict[str, Any] = {"keys": alert_keys, "reason": reason}
    if notes:
        body["notes"] = notes
    cancelled = await _confirm_alert_action(ctx, "clear", alert_keys)
    if cancelled:
        return cancelled
    return _alert_action("clear", body, f"Clear request submitted for {len(alert_keys)} alert(s).")


@mcp.tool(annotations=DESTRUCTIVE)
async def defer_alerts(ctx: Context, keys: list[str], defer_until: str) -> dict[str, Any]:
    """Defer one or more alerts until an absolute ISO-8601 timestamp."""
    alert_keys = _require_non_empty_strings(keys, "keys")
    defer_value = defer_until.strip()
    if not defer_value:
        raise ValueError("defer_until must be a non-empty ISO-8601 timestamp")
    body = {"keys": alert_keys, "deferUntil": defer_value}
    cancelled = await _confirm_alert_action(ctx, "defer", alert_keys)
    if cancelled:
        return cancelled
    return _alert_action("defer", body, f"Defer request submitted for {len(alert_keys)} alert(s).")


@mcp.tool(annotations=DESTRUCTIVE)
async def reactivate_alerts(ctx: Context, keys: list[str]) -> dict[str, Any]:
    """Reactivate cleared or deferred alerts by key."""
    alert_keys = _require_non_empty_strings(keys, "keys")
    body = {"keys": alert_keys}
    cancelled = await _confirm_alert_action(ctx, "reactivate", alert_keys)
    if cancelled:
        return cancelled
    return _alert_action(
        "active",
        body,
        f"Reactivate request submitted for {len(alert_keys)} alert(s).",
    )


@mcp.tool(annotations=DESTRUCTIVE)
async def set_alert_priority(ctx: Context, keys: list[str], priority: str) -> dict[str, Any]:
    """Set operator priority for one or more alerts."""
    alert_keys = _require_non_empty_strings(keys, "keys")
    if priority not in _ALERT_PRIORITIES:
        allowed = ", ".join(sorted(_ALERT_PRIORITIES))
        raise ValueError(f"priority must be one of: {allowed}")
    body = {"keys": alert_keys, "priority": priority}
    cancelled = await _confirm_alert_action(ctx, f"set priority to {priority}", alert_keys)
    if cancelled:
        return cancelled
    return _alert_action(
        "priority",
        body,
        f"Priority update submitted for {len(alert_keys)} alert(s).",
    )


@mcp.tool(annotations=READ_ONLY)
def list_events(
    serial_number: str,
    hours: int = 24,
    limit: int = 50,
    offset: int = 0,
    full_list: bool = False,
) -> dict[str, Any]:
    """List bounded device events and auto-resolve the device type and site."""
    events = get_mcp_client().get_events(serial_number, hours=hours)
    if full_list:
        return {
            "items": events,
            "_pagination": {
                "offset": 0,
                "limit": len(events),
                "total": len(events),
                "truncated": False,
            },
        }
    return bound_collection_response(events, limit=limit, offset=offset)


@mcp.tool(annotations=READ_ONLY)
def get_events_count(serial_number: str, hours: int = 24) -> dict[str, Any]:
    """Count events for a device over the past N hours (default 24).

    KNOWN ISSUE: events endpoint unstable. Legacy /events/count 404s;
    /event-filters 400s with unknown param shape. Tries both; surfaces errors.
    """
    client = get_client()
    errors: list[str] = []
    now_ms = int(time.time() * 1000)
    params = {
        "serialNumber": serial_number,
        "startTime": now_ms - hours * 3_600_000,
        "endTime": now_ms,
    }
    # Try peer-consensus path first, then legacy fallback.
    for endpoint in (
        "/network-troubleshooting/v1/event-filters",
        "/network-monitoring/v1/events/count",
    ):
        try:
            result = client.get(endpoint, params=params)
            count = result.get("count")
            if count is None:
                items = result.get("items", [])
                count = sum(i.get("count", 0) for i in items) if items else 0
            return {
                "serial_number": serial_number,
                "count": count,
                "endpoint_used": endpoint,
                "errors": errors,
            }
        except Exception as exc:
            errors.append(f"{endpoint}: {exc}")
    return {"serial_number": serial_number, "count": 0, "endpoint_used": None, "errors": errors}


@mcp.tool(annotations=READ_ONLY)
def list_radios(
    site_id: str | None = None,
    serial_number: str | None = None,
    limit: int = 100,
    offset: int = 0,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    """List radios from network-monitoring/v1/radios with optional site/device filters.

    Paginates with a `next` cursor (getradiolistv1), not offset. Pass
    next_cursor from a prior response's `next` field to page forward;
    offset is translated to an approximate starting cursor when
    next_cursor is omitted.
    """
    client = get_client()
    params: dict[str, Any] = {"limit": clamp_limit(limit)}
    off = max(0, offset)
    cursor = next_cursor or (str(off + 1) if off > 0 else None)
    if cursor:
        params["next"] = cursor
    if site_id:
        params["siteId"] = site_id
    if serial_number:
        params["serialNumber"] = serial_number
    try:
        return client.get("/network-monitoring/v1/radios", params=params)
    except Exception as exc:
        return {"error": str(exc), "endpoint_used": "/network-monitoring/v1/radios"}


@mcp.tool(annotations=READ_ONLY)
def list_bssids(
    site_id: str | None = None,
    site_name: str | None = None,
    serial_number: str | None = None,
    mac_address: str | None = None,
    radio_mac_address: str | None = None,
    filter: str | None = None,
    sort: str | None = None,
    limit: int = 20,
    offset: int = 0,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    """List BSSID-to-AP/radio/WLAN mappings from network-monitoring/v1/bssids.

    Structured filters use exact OData equality matches for siteId, siteName,
    serialNumber, macAddress, and radioMacAddress. Use ``filter`` for documented
    ``in`` expressions or to combine other supported clauses; Central supports
    only ``and`` conjunctions for this endpoint. Supported sort fields are
    siteId, siteName, serialNumber, deviceName, and wlanName.

    Paginates with a ``next`` cursor (getBssidsV1), not offset. Pass
    next_cursor from a prior response's ``next`` field to page forward;
    offset is translated to an approximate starting cursor when
    next_cursor is omitted.
    """
    params: dict[str, Any] = {"limit": clamp_limit(limit, default=20)}
    off = max(0, offset)
    cursor = next_cursor or (str(off + 1) if off > 0 else None)
    if cursor:
        params["next"] = cursor

    filter_parts: list[str] = []
    if filter and filter.strip():
        filter_parts.append(filter.strip())
    for field, value in (
        ("siteId", site_id),
        ("siteName", site_name),
        ("serialNumber", serial_number),
        ("macAddress", mac_address),
        ("radioMacAddress", radio_mac_address),
    ):
        if value and value.strip():
            filter_parts.append(f"{field} eq '{_odata_string(value.strip())}'")
    if filter_parts:
        params["filter"] = " and ".join(filter_parts)
    if sort and sort.strip():
        params["sort"] = sort.strip()

    try:
        return get_client().get("/network-monitoring/v1/bssids", params=params)
    except Exception as exc:
        return {"error": str(exc), "endpoint_used": "/network-monitoring/v1/bssids"}


@mcp.tool(annotations=READ_ONLY)
def list_gateways(
    site_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    """List gateway inventory from network-monitoring/v1/gateways.

    Paginates with a `next` cursor (getgatewaylistv1), not offset. Pass
    next_cursor from a prior response's `next` field to page forward;
    offset is translated to an approximate starting cursor when
    next_cursor is omitted.
    """
    client = get_client()
    params: dict[str, Any] = {"limit": clamp_limit(limit)}
    off = max(0, offset)
    cursor = next_cursor or (str(off + 1) if off > 0 else None)
    if cursor:
        params["next"] = cursor
    if site_id:
        params["siteId"] = site_id
    try:
        return client.get("/network-monitoring/v1/gateways", params=params)
    except Exception as exc:
        return {"error": str(exc), "endpoint_used": "/network-monitoring/v1/gateways"}


@mcp.tool(annotations=READ_ONLY)
def list_sites_client_health(
    limit: int = 100,
    offset: int = 0,
    site_id: str | None = None,
    site_name: str | None = None,
    filter: str | None = None,
    sort: str | None = None,
) -> dict[str, Any]:
    """List per-site wired/wireless client health summaries.

    GET /network-monitoring/v1/sites-client-health uses true limit/offset
    pagination. Structured filters use exact OData equality matches for
    siteId and siteName. Use ``filter`` for documented ``in`` expressions
    or to combine other supported clauses; only ``and`` conjunctions are
    supported. Sort fields are siteName, clientHealth, wirelessClientHealth,
    and wiredClientHealth.
    """
    client = get_client()
    params: dict[str, Any] = {"limit": clamp_limit(limit), "offset": max(0, offset)}
    filter_parts: list[str] = []
    if filter and filter.strip():
        filter_parts.append(filter.strip())
    for field, value in (("siteId", site_id), ("siteName", site_name)):
        if value and value.strip():
            filter_parts.append(f"{field} eq '{_odata_string(value.strip())}'")
    if filter_parts:
        params["filter"] = " and ".join(filter_parts)
    if sort and sort.strip():
        params["sort"] = sort.strip()
    try:
        return client.get("/network-monitoring/v1/sites-client-health", params=params)
    except Exception as exc:
        return {"error": str(exc), "endpoint_used": "/network-monitoring/v1/sites-client-health"}


@mcp.tool(annotations=READ_ONLY)
def get_tenant_health() -> dict[str, Any]:
    """Return tenant-wide device and client health summaries."""
    client = get_client()
    out: dict[str, Any] = {"device_health": None, "client_health": None, "errors": []}
    try:
        out["device_health"] = client.get("/network-monitoring/v1/tenant-device-health")
    except Exception as exc:
        out["errors"].append(f"tenant-device-health: {exc}")
    try:
        out["client_health"] = client.get("/network-monitoring/v1/tenant-client-health")
    except Exception as exc:
        out["errors"].append(f"tenant-client-health: {exc}")
    return out


# ── Scopes ────────────────────────────────────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
def list_scopes(
    limit: int = 100,
    offset: int = 0,
    full_list: bool = False,
) -> dict[str, Any]:
    """List global, site, and device-group scopes from the official v1 APIs.

    Results are normalized to ``scope_id``, ``scope_name``, and ``scope_type``.
    Site and device-group APIs are paged up to 5,000 records each. Partial
    source failures are returned as warnings; if every source fails, the tool
    returns an explicit failed result instead of an empty success.
    """
    client = get_client()
    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    source_outcomes: list[tuple[bool, int | None]] = []
    source_truncated = False

    def warning(endpoint: str, exc: Exception | str) -> str:
        detail = str(exc).strip().replace("\n", " ")
        if len(detail) > 240:
            detail = f"{detail[:237]}..."
        return f"{endpoint}: {detail or 'unknown failure'}"

    def failure_status(exc: Exception) -> int | None:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        return status_code if isinstance(status_code, int) and status_code >= 400 else None

    def normalize(raw: Any, scope_type: str) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        scope_id = _scope_field(raw, "scopeId", "scope_id", "id")
        scope_name = _scope_field(raw, "scopeName", "scope_name", "name")
        if scope_id is None or scope_name is None:
            return None
        return {
            "scope_id": scope_id,
            "scope_name": scope_name,
            "scope_type": scope_type,
        }

    global_endpoint = "/network-config/v1/global"
    try:
        global_data = client.get(global_endpoint)
        global_id = _global_scope_field(global_data) if isinstance(global_data, dict) else None
        if global_id is None:
            raise RuntimeError("response omitted a valid numeric scopeId")
        items.append(
            {
                "scope_id": global_id,
                "scope_name": "Global",
                "scope_type": "GLOBAL",
            }
        )
        source_outcomes.append((True, None))
    except Exception as exc:
        warnings.append(warning(global_endpoint, exc))
        source_outcomes.append((False, failure_status(exc)))
        source_truncated = True

    for endpoint, scope_type in (
        ("/network-config/v1/sites", "SITE"),
        ("/network-config/v1/device-groups", "DEVICE_GROUP"),
    ):
        source_items: list[dict[str, Any]] = []
        invalid_items = 0
        raw_items_seen = 0
        source_succeeded = False
        source_status: int | None = None
        page_offset = 0
        seen_offsets = {page_offset}
        for _ in range(_SCOPE_MAX_PAGES):
            try:
                data = client.get(
                    endpoint,
                    params={"limit": _SCOPE_PAGE_SIZE, "offset": page_offset},
                )
            except Exception as exc:
                warnings.append(warning(endpoint, exc))
                source_status = failure_status(exc)
                source_truncated = True
                break

            page = data.get("items") if isinstance(data, dict) else None
            if not isinstance(page, list):
                warnings.append(warning(endpoint, "response omitted the items list"))
                source_truncated = True
                break
            source_succeeded = True
            raw_items_seen += len(page)

            for raw in page:
                normalized = normalize(raw, scope_type)
                if normalized is None:
                    invalid_items += 1
                else:
                    source_items.append(normalized)

            if isinstance(data, dict) and "offset" in data:
                continuation = data.get("offset")
                if continuation is None:
                    total = data.get("total")
                    if isinstance(total, int) and total != raw_items_seen:
                        warnings.append(
                            warning(
                                endpoint,
                                f"terminal page reported total {total} after {raw_items_seen} records",
                            )
                        )
                        source_truncated = True
                    break
                if continuation == "":
                    warnings.append(warning(endpoint, "empty continuation offset"))
                    source_truncated = True
                    break
                try:
                    next_offset = int(continuation)
                except (TypeError, ValueError):
                    warnings.append(
                        warning(endpoint, f"invalid continuation offset {continuation!r}")
                    )
                    source_truncated = True
                    break
                if next_offset <= page_offset or next_offset in seen_offsets:
                    warnings.append(
                        warning(endpoint, f"non-increasing continuation offset {next_offset}")
                    )
                    source_truncated = True
                    break
                page_offset = next_offset
                seen_offsets.add(page_offset)
                continue

            total = data.get("total") if isinstance(data, dict) else None
            if isinstance(total, int):
                if total > raw_items_seen:
                    warnings.append(
                        warning(
                            endpoint,
                            f"response omitted continuation offset with {total - raw_items_seen} records remaining",
                        )
                    )
                    source_truncated = True
                elif total < raw_items_seen:
                    warnings.append(
                        warning(
                            endpoint,
                            f"response total {total} is smaller than {raw_items_seen} records received",
                        )
                    )
                    source_truncated = True
                break

            if len(page) < _SCOPE_PAGE_SIZE:
                break
            page_offset += _SCOPE_PAGE_SIZE
            seen_offsets.add(page_offset)
        else:
            source_truncated = True
            warnings.append(
                f"{endpoint}: stopped after {_SCOPE_MAX_PAGES * _SCOPE_PAGE_SIZE} records"
            )

        if invalid_items:
            warnings.append(f"{endpoint}: skipped {invalid_items} malformed scope records")
            source_truncated = True
        items.extend(source_items)
        source_outcomes.append((source_succeeded, source_status))

    if not items and warnings:
        all_sources_refused = len(source_outcomes) == 3 and all(
            not succeeded and status in {401, 403}
            for succeeded, status in source_outcomes
        )
        status = (
            next(status for _, status in source_outcomes if status in {401, 403})
            if all_sources_refused
            else "failed"
        )
        return {
            "status": status,
            "error": "Central scope discovery failed; no authoritative source returned data.",
            "warnings": warnings,
        }

    result: dict[str, Any] = {
        "items": items,
        "_pagination": {
            "offset": 0,
            "limit": len(items),
            "total": len(items),
            "truncated": source_truncated,
            "list_key": "items",
        },
    }
    if warnings:
        result["warnings"] = warnings

    if full_list:
        return result
    return bound_collection_response(result, limit=limit, offset=offset, list_key="items")


@mcp.tool(annotations=READ_ONLY)
def get_global_scope_id() -> dict[str, Any]:
    """Return the org-wide scope ID from ``GET /network-config/v1/global``."""
    endpoint = "/network-config/v1/global"
    try:
        data = get_client().get(endpoint)
        scope_id = _global_scope_field(data) if isinstance(data, dict) else None
        if scope_id is None:
            raise RuntimeError("response omitted a valid numeric scopeId")
        return {"global_scope_id": scope_id, "errors": []}
    except Exception as exc:
        detail = str(exc).strip().replace("\n", " ")
        if len(detail) > 240:
            detail = f"{detail[:237]}..."
        return {
            "global_scope_id": None,
            "errors": [f"{endpoint}: {detail or 'unknown failure'}"],
        }


@mcp.tool(annotations=READ_ONLY)
def find_scope(
    query: str,
    scope_type: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Find scopes by name or ID substring, optionally narrowed by scope_type."""
    needle = query.strip().lower()
    if not needle:
        raise ValueError("query must be a non-empty string")
    wanted_type = scope_type.strip().upper() if scope_type else None
    scope_result = list_scopes(full_list=True)
    if isinstance(scope_result, dict) and scope_result.get("error"):
        return scope_result
    scopes = _items_from_collection(scope_result)
    matches: list[dict[str, Any]] = []
    for scope in scopes:
        sid = str(
            scope.get("scope_id")
            or scope.get("scopeId")
            or scope.get("siteId")
            or scope.get("id")
            or ""
        )
        name = str(
            scope.get("scope_name")
            or scope.get("scopeName")
            or scope.get("siteName")
            or scope.get("name")
            or ""
        )
        kind = str(scope.get("scope_type") or scope.get("scopeType") or scope.get("type") or "")
        if wanted_type and kind.upper() != wanted_type:
            continue
        if needle in sid.lower() or needle in name.lower():
            matches.append(
                {
                    "scope_id": sid,
                    "scope_name": name,
                    "scope_type": kind,
                    "raw": scope,
                }
            )
    result = bound_collection_response(matches, limit=limit, offset=0)
    if (
        isinstance(result, dict)
        and isinstance(scope_result, dict)
        and isinstance(scope_result.get("warnings"), list)
    ):
        result["warnings"] = scope_result["warnings"]
    if (
        isinstance(result, dict)
        and isinstance(result.get("_pagination"), dict)
        and isinstance(scope_result, dict)
        and isinstance(scope_result.get("_pagination"), dict)
        and scope_result["_pagination"].get("truncated")
    ):
        result["_pagination"]["truncated"] = True
    return result


@mcp.tool(annotations=READ_ONLY)
def list_scope_devices(
    scope_id: str,
    device_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """List devices associated with a site/scope ID using known Central scope fields.

    Sweeps device-inventory pages using the server-issued `next` cursor
    (device-inventory does not support offset-based paging) and filters
    client-side; the returned limit/offset only bound the final result page.
    """
    scope = scope_id.strip()
    if not scope:
        raise ValueError("scope_id must be a non-empty string")
    page_size = 200
    found: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(50):
        page, cursor_next = get_mcp_client().get_devices_page(
            {"siteId": scope},
            limit=page_size,
            next_cursor=cursor,
        )
        if not page:
            break
        for device in page:
            fields = (
                device.get("scopeId"),
                device.get("scope_id"),
                device.get("siteId"),
                device.get("site_id"),
                device.get("groupId"),
                device.get("deviceGroupId"),
            )
            if scope not in {str(value) for value in fields if value is not None}:
                continue
            if device_type:
                want = device_type.upper()
                raw = str(device.get("deviceType") or device.get("type") or "").upper()
                if want == "AP":
                    want = "ACCESS_POINT"
                if want not in raw:
                    continue
            found.append(device)
        if not cursor_next:
            break
        cursor = cursor_next
    return bound_collection_response(found, limit=limit, offset=offset)


# ── Inventory ─────────────────────────────────────────────────────────────────


# Device-inventory (getDeviceInventoryV1) supports server-side OData filtering
# on deviceType/isProvisioned (both `eq` and `in`); the bare `deviceType=`
# query param this tool used to send was silently ignored, so filtering only
# ever happened client-side over a single cursor page (incomplete results).
# The canonical values come straight from the Monitoring OpenAPI schema:
# DeviceType enum = ACCESS_POINT|SWITCH|GATEWAY, IsProvisioned = YES|NO.
_DEVICE_TYPE_FILTER_ALIASES = {
    "AP": "ACCESS_POINT",
    "ACCESS_POINT": "ACCESS_POINT",
    "ACCESS-POINT": "ACCESS_POINT",
    "ACCESSPOINT": "ACCESS_POINT",
    "ACCESS_POINTS": "ACCESS_POINT",
    "SWITCH": "SWITCH",
    "SWITCHES": "SWITCH",
    "GATEWAY": "GATEWAY",
    "GATEWAYS": "GATEWAY",
    "GW": "GATEWAY",
}
_PROVISIONED_TRUE = {"YES", "Y", "TRUE", "PROVISIONED", "1"}
_PROVISIONED_FALSE = {"NO", "N", "FALSE", "CLAIMED", "UNPROVISIONED", "0"}


def _odata_quote(value: Any) -> str:
    """Escape a value for interpolation inside a single-quoted OData string."""
    return str(value).replace("'", "''")


def _normalize_device_type_filter(device_type: str) -> str:
    """Normalize a caller device_type to the DeviceType enum value.

    Maps the common ``AP`` alias to ``ACCESS_POINT`` and folds friendly
    plurals/abbreviations onto the documented enum; anything unrecognized is
    passed through upper-cased so a future enum value still forms a filter.
    """
    key = device_type.strip().upper()
    return _DEVICE_TYPE_FILTER_ALIASES.get(key, key)


def _normalize_provisioned_filter(status: str) -> str:
    """Normalize a caller status to the IsProvisioned value (YES/NO)."""
    key = status.strip().upper()
    if key in _PROVISIONED_TRUE:
        return "YES"
    if key in _PROVISIONED_FALSE:
        return "NO"
    return key

@mcp.tool(annotations=READ_ONLY)
def list_inventory(
    status: str | None = None,
    device_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    """List claimed/unprovisioned devices via server-side filtering.

    status: "YES"=provisioned, "NO"=claimed-only (case-insensitive; common
    aliases accepted). device_type: ACCESS_POINT/SWITCH/GATEWAY ("AP" is
    normalized to ACCESS_POINT). Both are translated into a getDeviceInventoryV1
    OData `filter` so results span the whole inventory rather than one page.

    Uses device-inventory v1 (getdeviceinventoryv1), falling back to
    v1alpha1 automatically if v1 is unavailable on this tenant. Both
    versions paginate with a `next` cursor, not offset — pass next_cursor
    from a prior response's _pagination.next_cursor to page forward;
    offset is translated to an approximate starting cursor when
    next_cursor is omitted.
    """
    errors: list[str] = []
    off = max(0, offset)
    cursor = next_cursor or (str(off + 1) if off > 0 else None)

    # Build a server-side OData filter (getDeviceInventoryV1 supports only the
    # `and` conjunction) so filtering happens across the whole inventory, not
    # just the current cursor page. No filter is sent when neither argument is
    # given, preserving the unfiltered listing.
    clauses: list[str] = []
    if device_type:
        normalized_type = _normalize_device_type_filter(device_type)
        clauses.append(f"deviceType eq '{_odata_quote(normalized_type)}'")
    if status:
        normalized_status = _normalize_provisioned_filter(status)
        clauses.append(f"isProvisioned eq '{_odata_quote(normalized_status)}'")
    filters: dict[str, Any] | None = (
        {"filter": " and ".join(clauses)} if clauses else None
    )

    try:
        items, returned_cursor = get_mcp_client().get_devices_page(
            filters, limit=clamp_limit(limit), next_cursor=cursor
        )
    except Exception as exc:
        errors.append(str(exc))
        return {"items": [], "total": 0, "errors": errors}
    if not isinstance(items, list):
        items = []
    # Server-side filtering already narrowed the page; `total` reports this
    # page's size (cursor pagination exposes no full-collection count).
    return {
        "items": items,
        "total": len(items),
        "next_cursor": returned_cursor,
        "errors": errors,
    }


# ── Audit Logs ────────────────────────────────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
def list_audit_logs(
    start_at: int | None = None,
    end_at: int | None = None,
    limit: int = 100,
    offset: int = 0,
    filter: str | None = None,
    sort: str | None = None,
) -> dict[str, Any]:
    """Audit logs are not available on New Central instances.

    The audit-log endpoint 404s and is absent from all OpenAPI specs. Use
    list_glp_audit_logs (glp-core) for GreenLake Platform audit trails instead.
    """
    return {
        "items": [],
        "errors": [
            "audit-log endpoint not available on New Central instances — "
            "use list_glp_audit_logs (glp-core) instead"
        ],
    }


@mcp.tool(annotations=READ_ONLY)
def get_audit_log(audit_id: str) -> dict[str, Any]:
    """Audit logs are not available on New Central instances.

    The audit-log endpoint 404s and is absent from all OpenAPI specs. Use
    list_glp_audit_logs (glp-core) for GreenLake Platform audit trails instead.
    """
    return {
        "items": [],
        "errors": [
            "audit-log endpoint not available on New Central instances — "
            "use list_glp_audit_logs (glp-core) instead"
        ],
    }


# ── Device Health & Trends ────────────────────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
def get_device_trends(
    serial_number: str,
    metric: str,
    start_time: str,
    end_time: str,
    site_id: str | None = None,
    device_type: str | None = None,
) -> dict[str, Any]:
    """Time-series utilization trends for an AP or switch.

    metric: cpu/memory/throughput. start_time/end_time: ISO 8601.
    device_type AP/SWITCH auto-detected if omitted.
    """
    client = get_client()
    errors: list[str] = []

    if not device_type:
        device = get_mcp_client().get_device_by_serial(serial_number)
        if device:
            raw = device.get("deviceType", "")
            if "ACCESS_POINT" in raw or raw == "AP":
                device_type = "AP"
            elif "SWITCH" in raw:
                device_type = "SWITCH"
            elif "GATEWAY" in raw:
                device_type = "GATEWAY"

    filter_str = f"timestamp gt {start_time} and timestamp lt {end_time}"
    params: dict[str, Any] = {"filter": filter_str}
    if site_id:
        params["site-id"] = site_id

    dt = (device_type or "").upper()
    m = metric.lower()
    if dt in ("AP", "ACCESS_POINT"):
        metric_segment = "throughput-trends" if m == "throughput" else f"{m}-utilization-trends"
        candidates = [f"/network-monitoring/v1/aps/{serial_number}/{metric_segment}"]
        if m == "throughput":
            params.setdefault("interface-type", "WIRELESS")
    elif dt in ("SWITCH", "CX"):
        if m in ("cpu", "memory", "hardware"):
            metric_segment = "hardware-trends"
        elif m == "throughput":
            metric_segment = "interface-trends"
        else:
            metric_segment = f"{m}-utilization-trends"
        candidates = [
            f"/network-monitoring/v1/switches/{serial_number}/{metric_segment}",
            f"/network-monitoring/v1alpha1/switch/{serial_number}/{metric_segment}",
        ]
    else:
        if m == "throughput":
            candidates = [
                f"/network-monitoring/v1/aps/{serial_number}/throughput-trends",
                f"/network-monitoring/v1/switches/{serial_number}/interface-trends",
            ]
        elif m in ("cpu", "memory", "hardware"):
            candidates = [
                f"/network-monitoring/v1/aps/{serial_number}/{m}-utilization-trends",
                f"/network-monitoring/v1/switches/{serial_number}/hardware-trends",
            ]
        else:
            candidates = [
                f"/network-monitoring/v1/aps/{serial_number}/{m}-utilization-trends",
                f"/network-monitoring/v1/switches/{serial_number}/{m}-utilization-trends",
            ]

    for endpoint in candidates:
        try:
            response = client._request("GET", endpoint, params=params)
            if response.status_code == 404:
                errors.append(f"404 at {endpoint}")
                continue
            if response.status_code not in (200, 201, 202):
                errors.append(f"HTTP {response.status_code} at {endpoint}")
                continue
            return {
                "serial_number": serial_number,
                "metric": metric,
                "trends": response.json(),
                "endpoint_used": endpoint,
                "errors": errors,
            }
        except Exception as exc:
            errors.append(str(exc))

    return {
        "serial_number": serial_number,
        "metric": metric,
        "trends": None,
        "endpoint_used": None,
        "errors": errors,
    }


@mcp.tool(annotations=READ_ONLY)
def get_device_health(
    serial_number: str | None = None,
    device_scope_id: str | None = None,
) -> dict[str, Any]:
    """Fetch config-health or monitoring health state for a device."""
    client = get_client()
    errors: list[str] = []

    try:
        params: dict[str, Any] = {}
        if device_scope_id:
            params["scope-id"] = device_scope_id
        response = client._request(
            "GET", "/network-config/v1alpha1/config-health/devices", params=params or None
        )
        if response.status_code == 200:
            data = response.json()
            items = data.get("items", data.get("devices", [data] if data else []))
            if serial_number and isinstance(items, list):
                matches = [
                    i
                    for i in items
                    if (i.get("serial") or i.get("serialNumber") or "").lower()
                    == serial_number.lower()
                ]
                items = matches if matches else items
            return {
                "serial_number": serial_number,
                "health": items,
                "endpoint_used": "/network-config/v1alpha1/config-health/devices",
                "errors": errors,
            }
        errors.append(f"config-health: HTTP {response.status_code}")
    except Exception as exc:
        errors.append(f"config-health: {exc}")

    if serial_number:
        for endpoint in [
            f"/network-monitoring/v1/devices/{serial_number}",
            f"/network-monitoring/v1alpha1/devices/{serial_number}",
        ]:
            try:
                response = client._request("GET", endpoint)
                if response.status_code == 404:
                    errors.append(f"404 at {endpoint}")
                    continue
                if response.status_code == 200:
                    return {
                        "serial_number": serial_number,
                        "health": response.json(),
                        "endpoint_used": endpoint,
                        "errors": errors,
                    }
                errors.append(f"HTTP {response.status_code} at {endpoint}")
            except Exception as exc:
                errors.append(str(exc))

    return {"serial_number": serial_number, "health": None, "endpoint_used": None, "errors": errors}


@mcp.tool(annotations=READ_ONLY)
def get_device_config_issues(serial_number: str) -> dict[str, Any]:
    """Return active configuration issues and recommended actions for one device."""
    serial = serial_number.strip()
    if not serial:
        raise ValueError("serial_number must be a non-empty string")
    endpoint = "/network-config/v1alpha1/config-health/active-issue"
    return get_client().get(endpoint, params={"serial": serial})


@mcp.tool(annotations=READ_ONLY)
def list_devices_config_health(
    limit: int = 100,
    offset: int = 0,
    sort: str | None = None,
    filter: str | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    """List fleet config-health summaries, optionally sorted/filtered/searched."""
    if search is not None and not (_SEARCH_MIN_CHARS <= len(search) <= _SEARCH_MAX_CHARS):
        raise ValueError(
            f"search must be {_SEARCH_MIN_CHARS}-{_SEARCH_MAX_CHARS} characters, got {len(search)}"
        )
    params: dict[str, Any] = {"limit": clamp_limit(limit, default=100), "offset": max(0, offset)}
    if sort:
        params["sort"] = sort
    if filter:
        params["filter"] = filter
    if search:
        params["search"] = search
    return get_client().get("/network-config/v1alpha1/config-health/devices", params=params)


# Schema max for one devices-resync call (resyncCfgDevices: maxItems 50,
# manifest-confirmed: ingestion/sources/openapi_specs/configuration-health-
# b374ik1cmq.json). execute_config_health_remediation chunks larger batches
# into calls this size.
_CONFIG_HEALTH_RESYNC_MAX_PER_CALL = 50
_MAX_REMEDIATION_SERIALS = 200


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def resync_device_config(serial_numbers: list[str]) -> dict[str, Any]:
    """Trigger full Central config resync for one or more device serial numbers.

    Bounded to 50 serials per call — the schema max for
    POST /network-config/v1alpha1/config-health/devices-resync
    (resyncCfgDevices: maxItems 50). Use execute_config_health_remediation
    for a bounded, chunked, dry_run+confirm+gate remediation workflow over
    more than 50 devices.
    """
    serials = _require_non_empty_strings(serial_numbers, "serial_numbers")
    if len(serials) > _CONFIG_HEALTH_RESYNC_MAX_PER_CALL:
        raise ValueError(
            f"serial_numbers cannot exceed {_CONFIG_HEALTH_RESYNC_MAX_PER_CALL} entries per "
            "call (schema max); chunk the request or use "
            "execute_config_health_remediation"
        )
    return get_client().post(
        "/network-config/v1alpha1/config-health/devices-resync",
        data={"serials": serials},
    )


@mcp.tool(annotations=READ_ONLY)
def plan_config_health_remediation(
    limit: int = 50,
    offset: int = 0,
    filter: str | None = None,
    search: str | None = None,
    max_devices_scanned: int = 50,
) -> dict[str, Any]:
    """Build a bounded, read-only config-health remediation plan.

    Calls list_devices_config_health (same limit/offset/filter/search
    params) to find devices, skips any whose reported status already looks
    compliant/healthy, then calls get_device_config_issues for up to
    max_devices_scanned (<=200) of the remaining devices to attach
    Central's active-issue detail for each. This tool makes no config
    changes — the only remediation action the config-health API exposes is
    a full resync, so the plan's next_step points at
    execute_config_health_remediation with the collected serial numbers.
    """
    if not (1 <= max_devices_scanned <= _MAX_REMEDIATION_SERIALS):
        raise ValueError(f"max_devices_scanned must be between 1 and {_MAX_REMEDIATION_SERIALS}")

    summary = list_devices_config_health(limit=limit, offset=offset, filter=filter, search=search)
    devices: list[Any] = []
    if isinstance(summary, dict):
        raw = summary.get("items")
        devices = raw if isinstance(raw, list) else summary.get("devices", [])
        if not isinstance(devices, list):
            devices = []

    healthy_statuses = {"SYNCHRONIZED", "COMPLIANT", "IN_SYNC", "HEALTHY", "SUCCESS"}
    unhealthy: list[dict[str, Any]] = []
    for device in devices:
        if not isinstance(device, dict):
            continue
        status = str(
            device.get("configStatus")
            or device.get("status")
            or device.get("configHealthStatus")
            or ""
        ).upper()
        if not status:
            continue
        if status in healthy_statuses:
            continue
        serial = device.get("serial") or device.get("serialNumber") or device.get("serial_number")
        if not serial:
            continue
        unhealthy.append({"serial_number": serial, "status": status or None})
        if len(unhealthy) >= max_devices_scanned:
            break

    plan: list[dict[str, Any]] = []
    errors: list[str] = []
    for entry in unhealthy:
        try:
            issues = get_device_config_issues(entry["serial_number"])
        except Exception as exc:
            errors.append(f"{entry['serial_number']}: {exc}")
            issues = None
        plan.append({**entry, "recommended_action": "resync_device_config", "issues": issues})

    return {
        "scanned": len(devices),
        "unhealthy_count": len(unhealthy),
        "plan": plan,
        "errors": errors,
        "next_step": (
            "Call execute_config_health_remediation(serial_numbers=[...], dry_run=False, "
            "confirm=True) with the serial numbers you want to resync."
        ),
    }


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def execute_config_health_remediation(
    serial_numbers: list[str],
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Execute a bounded config-health remediation: chunked resync + per-chunk read-back.

    serial_numbers (<=200 total) is split into chunks of at most 50 (the
    devices-resync schema max) and each chunk is resynced independently —
    one chunk's failure does not abort the remaining chunks (see
    result["chunks_failed"] and each chunk's "error"). dry_run=True
    (default) returns the chunk plan with no network calls. Set
    dry_run=False and confirm=True to execute — requires full-read-write or
    custom access with HPE_MCP_CENTRAL_WRITES=1 (Central writes are denied by
    default). After each successful chunk,
    get_device_config_issues is read back for every serial in that chunk;
    resync is asynchronous device-side, so a read_back entry may still show
    the prior issue if the device has not yet reported back — re-run
    plan_config_health_remediation later to confirm resolution.
    """
    serials = _require_non_empty_strings(serial_numbers, "serial_numbers")
    if len(serials) > _MAX_REMEDIATION_SERIALS:
        raise ValueError(f"serial_numbers cannot exceed {_MAX_REMEDIATION_SERIALS} entries")

    chunks = [
        serials[i : i + _CONFIG_HEALTH_RESYNC_MAX_PER_CALL]
        for i in range(0, len(serials), _CONFIG_HEALTH_RESYNC_MAX_PER_CALL)
    ]

    if dry_run:
        return {"dry_run": True, "chunks": chunks, "chunk_count": len(chunks)}

    blocked = enforce_platform_write("central", "execute_config_health_remediation")
    if blocked:
        return blocked
    if not confirm:
        return {
            "error": "confirm=True is required when dry_run=False.",
            "dry_run": True, "chunks": chunks, "chunk_count": len(chunks),
        }

    results: list[dict[str, Any]] = []
    for chunk in chunks:
        try:
            write_result = resync_device_config(chunk)
        except Exception as exc:
            results.append({"serials": chunk, "error": str(exc)})
            continue
        read_back: dict[str, Any] = {}
        for serial in chunk:
            try:
                read_back[serial] = get_device_config_issues(serial)
            except Exception as exc:
                read_back[serial] = {"error": str(exc)}
        results.append({"serials": chunk, "write_result": write_result, "read_back": read_back})

    succeeded = sum(1 for entry in results if "error" not in entry)
    return {
        "dry_run": False,
        "chunks_attempted": len(chunks),
        "chunks_succeeded": succeeded,
        "chunks_failed": len(chunks) - succeeded,
        "results": results,
    }


_MAX_TROUBLESHOOT_ALERTS = 50
_MAX_TROUBLESHOOT_EVENTS = 50
_MAX_TROUBLESHOOT_HOURS = 168
_MAX_SITE_DEVICES = 10
_MAX_SITE_DEVICE_SCAN = 200
_MAX_SITE_ALERTS = 50
_ALERT_SEVERITY_SCORES = {
    "CRITICAL": 50,
    "HIGH": 50,
    "MAJOR": 30,
    "MINOR": 10,
    "WARNING": 10,
}
_DEVICE_FAMILY_AP = "ap"
_DEVICE_FAMILY_SWITCH = "switch"
_DEVICE_FAMILY_GATEWAY = "gateway"
_OFFLINE_STATUSES = {
    "DOWN",
    "OFFLINE",
    "DISCONNECTED",
    "UNREACHABLE",
    "INACTIVE",
    "NOT_CONNECTED",
}
_CONFIG_HEALTHY_STATUSES = {"SYNCHRONIZED", "COMPLIANT", "IN_SYNC", "HEALTHY", "SUCCESS"}


def _device_family(device: dict[str, Any] | None) -> str:
    raw = ""
    if isinstance(device, dict):
        raw = str(
            device.get("deviceType")
            or device.get("deviceFunction")
            or device.get("persona")
            or ""
        )
    key = raw.upper()
    if "ACCESS_POINT" in key or key in {"AP", "CAMPUS_AP"}:
        return _DEVICE_FAMILY_AP
    if "GATEWAY" in key or "MOBILITY" in key:
        return _DEVICE_FAMILY_GATEWAY
    if "SWITCH" in key or key in {"CX", "AOS-S", "AOS_S"}:
        return _DEVICE_FAMILY_SWITCH
    return "unknown"


def _switch_bundle_device_type(device: dict[str, Any] | None) -> str | None:
    if not isinstance(device, dict):
        return None
    raw = " ".join(
        str(device.get(key) or "")
        for key in ("deviceType", "model", "firmwareVersion", "osVersion", "softwareVersion")
    ).upper()
    if "AOS-S" in raw or "AOS_S" in raw:
        return "aos-s"
    if "AOS-CX" in raw or "AOS_CX" in raw or " CX" in f" {raw}":
        return "cx"
    return None


def _record_serial(record: dict[str, Any]) -> str:
    for key in ("serialNumber", "serial", "serial_number", "deviceSerial"):
        value = record.get(key)
        if value not in (None, ""):
            return str(value)
    nested = record.get("device")
    if isinstance(nested, dict):
        return _record_serial(nested)
    return ""


def _compact_text(value: Any, limit: int = 160) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _status_token(*values: Any) -> str:
    for value in values:
        if value in (None, ""):
            continue
        return str(value).strip().upper()
    return ""


def _plan_action(
    name: str,
    capability: str,
    reason: str,
    arguments: dict[str, Any],
    *,
    requires_confirmation: bool = False,
) -> dict[str, Any]:
    dispatcher = "invoke_read_tool" if capability == "read" else "invoke_tool"
    action: dict[str, Any] = {
        "name": name,
        "capability": capability,
        "dispatcher": dispatcher,
        "reason": reason,
        "arguments": arguments,
        "execute": False,
    }
    if requires_confirmation:
        action["requires_confirmation"] = True
    return action


def _extend_unique(bucket: list[dict[str, Any]], action: dict[str, Any]) -> None:
    if any(existing["name"] == action["name"] for existing in bucket):
        return
    bucket.append(action)


@mcp.tool(annotations=READ_ONLY)
def plan_device_troubleshooting(
    serial_number: str,
    hours: int = 24,
    max_alerts: int = 20,
    max_events: int = 20,
    site_id: str | None = None,
) -> dict[str, Any]:
    """Build a bounded, read-only telemetry-to-remediation plan for one device.

    Composes existing Central tools only: find_device, get_device_health,
    get_device_config_issues, list_events, and list_active_alerts. It never
    executes diagnostics or writes. Recommended next steps point at already
    registered read, diagnostic, and gated remediation tools. Destructive
    suggestions always set execute=False and requires_confirmation=True.
    """
    serial = serial_number.strip()
    if not serial:
        raise ValueError("serial_number must be a non-empty string")
    if not (1 <= hours <= _MAX_TROUBLESHOOT_HOURS):
        raise ValueError(f"hours must be between 1 and {_MAX_TROUBLESHOOT_HOURS}")
    if not (1 <= max_alerts <= _MAX_TROUBLESHOOT_ALERTS):
        raise ValueError(f"max_alerts must be between 1 and {_MAX_TROUBLESHOOT_ALERTS}")
    if not (1 <= max_events <= _MAX_TROUBLESHOOT_EVENTS):
        raise ValueError(f"max_events must be between 1 and {_MAX_TROUBLESHOOT_EVENTS}")

    errors: list[str] = []
    observations: list[str] = []
    recommended_reads: list[dict[str, Any]] = []
    recommended_diagnostics: list[dict[str, Any]] = []
    recommended_writes: list[dict[str, Any]] = []
    recommended_destructive: list[dict[str, Any]] = []

    device: dict[str, Any] | None = None
    try:
        found = find_device(serial)
        device = found if isinstance(found, dict) else None
    except Exception as exc:
        errors.append(f"find_device: {exc}")
        device = None

    family = _device_family(device)
    device_status = _status_token(
        (device or {}).get("status"),
        (device or {}).get("deviceStatus"),
        (device or {}).get("connectionStatus"),
    )
    resolved_site = site_id or (
        str(
            (device or {}).get("siteId")
            or (device or {}).get("site_id")
            or (device or {}).get("scopeId")
            or ""
        ).strip()
        or None
    )
    if device is None:
        observations.append("Device inventory lookup returned no record.")
    else:
        observations.append(
            f"Inventory status={device_status or 'UNKNOWN'} family={family}."
        )

    health: dict[str, Any] | None = None
    try:
        health = get_device_health(serial)
    except Exception as exc:
        errors.append(f"get_device_health: {exc}")
    health_status = ""
    if isinstance(health, dict):
        if health.get("error"):
            errors.append(f"get_device_health: {health['error']}")
        payload = health.get("health")
        first = payload[0] if isinstance(payload, list) and payload else payload
        if isinstance(first, dict):
            health_status = _status_token(
                first.get("configStatus"),
                first.get("status"),
                first.get("configHealthStatus"),
                first.get("healthStatus"),
            )
        observations.append(
            f"Health endpoint={health.get('endpoint_used') or 'none'} "
            f"status={health_status or 'UNKNOWN'}."
        )

    issues: dict[str, Any] | None = None
    try:
        issues = get_device_config_issues(serial)
    except Exception as exc:
        errors.append(f"get_device_config_issues: {exc}")
    issue_items = _items_from_collection(issues)
    if isinstance(issues, dict) and issues.get("error"):
        errors.append(f"get_device_config_issues: {issues['error']}")
        issue_items = []
    if issue_items:
        observations.append(f"Config-health active issues: {len(issue_items)}.")

    events_payload: dict[str, Any] | list[Any] | None = None
    try:
        events_payload = list_events(serial, hours=hours, limit=max_events)
    except Exception as exc:
        errors.append(f"list_events: {exc}")
    event_items = _items_from_collection(events_payload)
    compact_events = []
    event_blob = ""
    for event in event_items[:max_events]:
        name = str(event.get("eventName") or event.get("name") or event.get("type") or "")
        description = _compact_text(event.get("description") or event.get("message"))
        compact_events.append(
            {
                "name": name or None,
                "description": description,
                "time": event.get("timeAt") or event.get("timestamp") or event.get("time"),
            }
        )
        event_blob += f" {name} {description or ''}"
    event_blob = event_blob.lower()
    if compact_events:
        observations.append(f"Recent events in last {hours}h: {len(compact_events)}.")

    alerts_payload: dict[str, Any] | list[Any] | None = None
    try:
        alerts_payload = list_active_alerts(site_id=resolved_site, limit=max_alerts)
    except Exception as exc:
        errors.append(f"list_active_alerts: {exc}")
    alert_items = _items_from_collection(alerts_payload)
    if isinstance(alerts_payload, dict) and alerts_payload.get("error"):
        errors.append(f"list_active_alerts: {alerts_payload['error']}")
        alert_items = []
    compact_alerts = []
    alert_blob = ""
    for alert in alert_items:
        alert_serial = _record_serial(alert)
        haystack = " ".join(
            str(alert.get(key) or "")
            for key in ("name", "title", "description", "alertType", "type")
        ).lower()
        if alert_serial:
            if alert_serial.lower() != serial.lower():
                continue
        elif serial.lower() not in haystack:
            continue
        title = str(
            alert.get("name")
            or alert.get("title")
            or alert.get("alertType")
            or alert.get("type")
            or ""
        )
        compact_alerts.append(
            {
                "key": alert.get("key") or alert.get("id"),
                "severity": alert.get("severity"),
                "name": title or None,
                "status": alert.get("status"),
            }
        )
        alert_blob += f" {title} {alert.get('severity') or ''}"
        if len(compact_alerts) >= max_alerts:
            break
    alert_blob = alert_blob.lower()
    if compact_alerts:
        observations.append(f"Active alerts matching this serial: {len(compact_alerts)}.")

    evidence = f"{device_status} {health_status} {event_blob} {alert_blob}".lower()
    config_unhealthy = bool(issue_items) or (
        bool(health_status) and health_status not in _CONFIG_HEALTHY_STATUSES
    )
    offline = device_status in _OFFLINE_STATUSES or "down" in evidence or "offline" in evidence

    args = {"serial_number": serial}
    _extend_unique(
        recommended_reads,
        _plan_action(
            "get_device_health",
            "read",
            "Re-check live inventory/config-health after the current snapshot.",
            args,
        ),
    )
    if family == _DEVICE_FAMILY_AP or "radio" in evidence or "wlan" in evidence:
        _extend_unique(
            recommended_reads,
            _plan_action(
                "get_wireless_metrics",
                "read",
                "Inspect RF/client/utilization metrics for this AP.",
                args,
            ),
        )
        _extend_unique(
            recommended_reads,
            _plan_action("list_radios", "read", "Inspect radio state for this AP.", args),
        )
    if family == _DEVICE_FAMILY_SWITCH or any(
        token in evidence for token in ("interface", "poe", "cable", "link")
    ):
        _extend_unique(
            recommended_reads,
            _plan_action(
                "list_switch_ports",
                "read",
                "Inspect switch interface/PoE state before any port action.",
                args,
            ),
        )
    if "tunnel" in evidence or "gre" in evidence or "ipsec" in evidence:
        _extend_unique(
            recommended_reads,
            _plan_action(
                "list_ap_tunnels",
                "read",
                "Inspect AP tunnel telemetry mentioned by recent events/alerts.",
                args,
            ),
        )
    if any(token in evidence for token in ("onboarding", "roam", "flap", "client")):
        _extend_unique(
            recommended_reads,
            _plan_action(
                "detect_client_flapping",
                "read",
                "Check whether client onboarding/roaming events look like flapping.",
                {"serial_number": serial, "hours": hours},
            ),
        )
        _extend_unique(
            recommended_reads,
            _plan_action(
                "list_clients",
                "read",
                "List clients currently or recently on this device.",
                {"serial_number": serial, "limit": 50},
            ),
        )
    if "ssh" in evidence:
        _extend_unique(
            recommended_reads,
            _plan_action(
                "detect_ssh_brute_force",
                "read",
                "Recent SSH failure events were present; inspect clustered sources.",
                {"serial_number": serial, "hours": hours},
            ),
        )

    if family == _DEVICE_FAMILY_SWITCH:
        bundle_type = _switch_bundle_device_type(device)
        bundle_args: dict[str, Any] = {"serial_number": serial}
        if bundle_type:
            bundle_args["device_type"] = bundle_type
        _extend_unique(
            recommended_diagnostics,
            _plan_action(
                "run_troubleshooting_bundle",
                "diagnostic",
                (
                    "Run the bounded CX/AOS-S LLDP/ARP/ping/show bundle."
                    if bundle_type
                    else (
                        "Switch family detected; confirm device_type=cx or "
                        "aos-s before the bundle."
                    )
                ),
                bundle_args,
            ),
        )
    if any(token in evidence for token in ("cable", "tdr", "interface", "link")):
        _extend_unique(
            recommended_diagnostics,
            _plan_action(
                "cable_test",
                "diagnostic",
                "Interface/cable symptoms were present; run TDR after the user supplies ports.",
                {"serial_number": serial},
            ),
        )

    if config_unhealthy:
        _extend_unique(
            recommended_writes,
            _plan_action(
                "execute_config_health_remediation",
                "write",
                "Config-health is not synchronized; preview a resync with dry_run=True first.",
                {
                    "serial_numbers": [serial],
                    "dry_run": True,
                    "confirm": False,
                },
                requires_confirmation=True,
            ),
        )
    if any(token in evidence for token in ("poe",)):
        _extend_unique(
            recommended_destructive,
            _plan_action(
                "poe_bounce",
                "destructive",
                "PoE symptoms were present. Do not execute unless the user confirms the port list.",
                {"serial_number": serial},
                requires_confirmation=True,
            ),
        )
    if (
        any(token in evidence for token in ("link", "interface"))
        and family == _DEVICE_FAMILY_SWITCH
    ):
        _extend_unique(
            recommended_destructive,
            _plan_action(
                "port_bounce",
                "destructive",
                "Link/interface symptoms were present. Bounce only after confirming the port.",
                {"serial_number": serial},
                requires_confirmation=True,
            ),
        )
    if offline:
        _extend_unique(
            recommended_destructive,
            _plan_action(
                "reboot_device",
                "destructive",
                "Device appears down/offline. Reboot is last-resort and needs confirmation.",
                args,
                requires_confirmation=True,
            ),
        )

    return {
        "serial_number": serial,
        "site_id": resolved_site,
        "device_family": family,
        "device_status": device_status or None,
        "health_status": health_status or None,
        "hours": hours,
        "observations": observations,
        "alerts": compact_alerts,
        "events": compact_events,
        "config_issue_count": len(issue_items),
        "recommended_reads": recommended_reads,
        "recommended_diagnostics": recommended_diagnostics,
        "recommended_writes": recommended_writes,
        "recommended_destructive": recommended_destructive,
        "errors": errors,
        "next_step": (
            "Call the recommended_reads / recommended_diagnostics tools first. "
            "Writes and destructive tools stay execute=False until the user confirms; "
            "config resync must use dry_run=True before confirm=True."
        ),
    }


def _alert_severity_score(severity: Any) -> int:
    return _ALERT_SEVERITY_SCORES.get(str(severity or "").strip().upper(), 0)


@mcp.tool(annotations=READ_ONLY)
def plan_site_troubleshooting(
    site_id: str | None = None,
    site_name: str | None = None,
    max_devices: int = 5,
    max_alerts: int = 20,
    include_device_plans: bool = False,
) -> dict[str, Any]:
    """Build a bounded, read-only troubleshooting plan for one site.

    Composes get_site_health_summary, list_devices, and list_active_alerts.
    Devices are ranked by offline/down status and matching alert severity.
    This tool never executes diagnostics or writes. By default it only
    recommends plan_device_troubleshooting for the top devices; set
    include_device_plans=True to attach those nested read-only plans
    (still execute=False) for up to max_devices (1-10).
    """
    if not (1 <= max_devices <= _MAX_SITE_DEVICES):
        raise ValueError(f"max_devices must be between 1 and {_MAX_SITE_DEVICES}")
    if not (1 <= max_alerts <= _MAX_SITE_ALERTS):
        raise ValueError(f"max_alerts must be between 1 and {_MAX_SITE_ALERTS}")
    if not (site_id and site_id.strip()) and not (site_name and site_name.strip()):
        raise ValueError("Provide site_id or site_name")

    errors: list[str] = []
    observations: list[str] = []

    summary: dict[str, Any] | None = None
    try:
        summary = get_site_health_summary(site_id=site_id, site_name=site_name)
    except Exception as exc:
        errors.append(f"get_site_health_summary: {exc}")
        summary = None
    if isinstance(summary, dict) and summary.get("error"):
        return {
            "error": summary["error"],
            "site_id": site_id,
            "site_name": site_name,
            "priority_devices": [],
            "recommended_reads": [],
            "errors": errors,
            "next_step": "Resolve the site with list_sites or get_site_health_summary.",
        }

    resolved_id = None
    resolved_name = site_name
    if isinstance(summary, dict):
        resolved_id = summary.get("site_id") or site_id
        resolved_name = summary.get("site") or site_name
        devices_meta = summary.get("devices") if isinstance(summary.get("devices"), dict) else {}
        alerts_meta = summary.get("alerts") if isinstance(summary.get("alerts"), dict) else {}
        observations.append(
            f"Site inventory devices={devices_meta.get('total', 0)} "
            f"alerts={alerts_meta.get('total', 0)}."
        )

    devices_payload: list[dict[str, Any]] | dict[str, Any] | None = None
    try:
        devices_payload = list_devices(
            site_id=str(resolved_id) if resolved_id else None,
            limit=_MAX_SITE_DEVICE_SCAN,
        )
    except Exception as exc:
        errors.append(f"list_devices: {exc}")
    device_items = _items_from_collection(devices_payload)

    alerts_payload: dict[str, Any] | list[Any] | None = None
    try:
        alerts_payload = list_active_alerts(
            site_id=str(resolved_id) if resolved_id else None,
            limit=max_alerts,
        )
    except Exception as exc:
        errors.append(f"list_active_alerts: {exc}")
    if isinstance(alerts_payload, dict) and alerts_payload.get("error"):
        errors.append(f"list_active_alerts: {alerts_payload['error']}")
        alert_items: list[dict[str, Any]] = []
    else:
        alert_items = _items_from_collection(alerts_payload)

    alerts_by_serial: dict[str, list[dict[str, Any]]] = {}
    unscoped_alerts = 0
    for alert in alert_items:
        serial = _record_serial(alert)
        compact = {
            "key": alert.get("key") or alert.get("id"),
            "severity": alert.get("severity"),
            "name": alert.get("name") or alert.get("title") or alert.get("alertType"),
        }
        if serial:
            alerts_by_serial.setdefault(serial.upper(), []).append(compact)
        else:
            unscoped_alerts += 1
    if unscoped_alerts:
        observations.append(f"Active alerts without a device serial: {unscoped_alerts}.")

    ranked: list[dict[str, Any]] = []
    for device in device_items:
        serial = _record_serial(device)
        if not serial:
            continue
        status = _status_token(device.get("status"), device.get("deviceStatus"))
        family = _device_family(device)
        matched = alerts_by_serial.get(serial.upper(), [])
        score = 0
        if status in _OFFLINE_STATUSES:
            score += 100
        score += sum(_alert_severity_score(item.get("severity")) for item in matched)
        if score <= 0:
            continue
        ranked.append(
            {
                "serial_number": serial,
                "name": device.get("deviceName") or device.get("name"),
                "device_family": family,
                "status": status or None,
                "score": score,
                "alert_count": len(matched),
                "alerts": matched[:5],
                "next_tool": _plan_action(
                    "plan_device_troubleshooting",
                    "read",
                    "Build a per-device read-only remediation plan for this serial.",
                    {"serial_number": serial, "site_id": resolved_id},
                ),
            }
        )
    ranked.sort(key=lambda item: (-int(item["score"]), str(item["serial_number"])))
    priority = ranked[:max_devices]
    observations.append(
        f"Ranked {len(ranked)} symptomatic devices; returning top {len(priority)}."
    )

    recommended_reads = [
        _plan_action(
            "get_site_health_summary",
            "read",
            "Re-check the site-wide health snapshot.",
            {"site_id": resolved_id} if resolved_id else {"site_name": resolved_name},
        )
    ]
    if priority:
        recommended_reads.append(priority[0]["next_tool"])

    device_plans: list[dict[str, Any]] = []
    if include_device_plans:
        for item in priority:
            serial = item["serial_number"]
            try:
                plan = plan_device_troubleshooting(
                    serial,
                    site_id=str(resolved_id) if resolved_id else None,
                )
            except Exception as exc:
                errors.append(f"plan_device_troubleshooting {serial}: {exc}")
                continue
            device_plans.append({"serial_number": serial, "plan": plan})

    return {
        "site_id": resolved_id,
        "site": resolved_name,
        "scanned_devices": len(device_items),
        "symptomatic_devices": len(ranked),
        "observations": observations,
        "priority_devices": priority,
        "device_plans": device_plans,
        "recommended_reads": recommended_reads,
        "errors": errors,
        "next_step": (
            "Call plan_device_troubleshooting for the highest-score serials. "
            "Those nested plans are read-only; do not run destructive tools "
            "unless the user confirms."
            if priority
            else "No symptomatic devices were ranked. Re-check get_site_health_summary."
        ),
    }


@mcp.tool(annotations=READ_ONLY)
def get_wireless_metrics(serial_number: str) -> dict[str, Any]:
    """Fetch AP wireless metrics: RF stats, client count, utilization, channel."""
    client = get_client()
    errors: list[str] = []

    for endpoint in [
        f"/network-monitoring/v1/aps/{serial_number}",
        f"/network-monitoring/v1/devices/{serial_number}/wireless-stats",
        f"/network-monitoring/v1alpha1/aps/{serial_number}/rf-stats",
    ]:
        try:
            response = client._request("GET", endpoint)
            if response.status_code == 404:
                errors.append(f"404 at {endpoint}")
                continue
            if response.status_code not in (200, 201, 202):
                errors.append(f"HTTP {response.status_code} at {endpoint}")
                continue
            return {
                "serial_number": serial_number,
                "metrics": response.json(),
                "endpoint_used": endpoint,
                "errors": errors,
            }
        except Exception as exc:
            errors.append(str(exc))

    return {
        "serial_number": serial_number,
        "metrics": None,
        "endpoint_used": None,
        "errors": errors,
    }


# ── Switch Monitoring ─────────────────────────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
def list_switch_ports(
    serial_number: str,
    limit: int = 100,
    offset: int = 0,
    filter: str | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    """List switch interfaces with optional OData filtering."""
    client = get_client()
    errors: list[str] = []
    params: dict[str, Any] = {
        "limit": clamp_limit(limit),
        "offset": max(0, offset),
    }
    if filter:
        params["filter"] = filter
    if search:
        params["search"] = search

    for endpoint in [
        f"/network-monitoring/v1/switches/{serial_number}/interfaces",
        f"/network-monitoring/v1alpha1/switch/{serial_number}/interfaces",
    ]:
        try:
            response = client._request("GET", endpoint, params=params)
            if response.status_code == 404:
                errors.append(f"404 at {endpoint}")
                continue
            if response.status_code not in (200, 201, 202):
                errors.append(f"HTTP {response.status_code} at {endpoint}")
                continue
            data = response.json()
            interfaces = data.get("interfaces", data.get("items", data))
            return {
                "serial_number": serial_number,
                "interfaces": interfaces,
                "endpoint_used": endpoint,
                "errors": errors,
            }
        except Exception as exc:
            errors.append(str(exc))

    return {
        "serial_number": serial_number,
        "interfaces": None,
        "endpoint_used": None,
        "errors": errors,
    }


@mcp.tool(annotations=READ_ONLY)
def get_switch_details(serial_number: str) -> dict[str, Any]:
    """Fetch full monitoring details for a switch (status, uptime, CPU, memory, VLANs)."""
    client = get_client()
    errors: list[str] = []

    for endpoint in [
        f"/network-monitoring/v1/switches/{serial_number}",
        f"/network-monitoring/v1alpha1/switch/{serial_number}",
    ]:
        try:
            response = client._request("GET", endpoint)
            if response.status_code == 404:
                errors.append(f"404 at {endpoint}")
                continue
            if response.status_code not in (200, 201, 202):
                errors.append(f"HTTP {response.status_code} at {endpoint}")
                continue
            return {
                "serial_number": serial_number,
                "details": response.json(),
                "endpoint_used": endpoint,
                "errors": errors,
            }
        except Exception as exc:
            errors.append(str(exc))

    return {
        "serial_number": serial_number,
        "details": None,
        "endpoint_used": None,
        "errors": errors,
    }


@mcp.tool(annotations=READ_ONLY)
def get_switch_vlans(
    serial_number: str,
    limit: int = 100,
    offset: int = 0,
    filter: str | None = None,
) -> dict[str, Any]:
    """List VLANs active on a switch (status, membership). filter: OData e.g. "status in ('Up')"."""
    client = get_client()
    errors: list[str] = []
    params: dict[str, Any] = {
        "limit": clamp_limit(limit),
        "offset": max(0, offset),
    }
    if filter:
        params["filter"] = filter

    for endpoint in [
        f"/network-monitoring/v1/switches/{serial_number}/vlans",
        f"/network-monitoring/v1alpha1/switch/{serial_number}/vlans",
    ]:
        try:
            response = client._request("GET", endpoint, params=params)
            if response.status_code == 404:
                errors.append(f"404 at {endpoint}")
                continue
            if response.status_code not in (200, 201, 202):
                errors.append(f"HTTP {response.status_code} at {endpoint}")
                continue
            data = response.json()
            vlans = data.get("vlans", data.get("items", data))
            return {
                "serial_number": serial_number,
                "vlans": vlans,
                "endpoint_used": endpoint,
                "errors": errors,
            }
        except Exception as exc:
            errors.append(str(exc))

    return {"serial_number": serial_number, "vlans": None, "endpoint_used": None, "errors": errors}


@mcp.tool(annotations=READ_ONLY)
def get_switch_interface_poe(
    serial_number: str,
    site_id: str | None = None,
) -> dict[str, Any]:
    """Fetch PoE state and power draw for all ports on a switch."""
    client = get_client()
    errors: list[str] = []
    params: dict[str, Any] = {}
    if site_id:
        params["site-id"] = site_id

    for endpoint in [
        f"/network-monitoring/v1/switches/{serial_number}/interface-poe",
        f"/network-monitoring/v1alpha1/switch/{serial_number}/interface-poe",
    ]:
        try:
            response = client._request("GET", endpoint, params=params or None)
            if response.status_code == 404:
                errors.append(f"404 at {endpoint}")
                continue
            if response.status_code not in (200, 201, 202):
                errors.append(f"HTTP {response.status_code} at {endpoint}")
                continue
            data = response.json()
            poe = data.get("interfaces", data.get("items", data))
            return {
                "serial_number": serial_number,
                "poe": poe,
                "endpoint_used": endpoint,
                "errors": errors,
            }
        except Exception as exc:
            errors.append(str(exc))

    return {"serial_number": serial_number, "poe": None, "endpoint_used": None, "errors": errors}


@mcp.tool(annotations=READ_ONLY)
def get_switch_interface_trends(
    serial_number: str,
    start_time: str,
    end_time: str,
    site_id: str | None = None,
    interface_id: str | None = None,
    uplink: bool | None = None,
) -> dict[str, Any]:
    """Throughput trends for switch interfaces over a time window.

    start_time/end_time ISO 8601. interface_id e.g. "7" or "1/1/6".
    """
    client = get_client()
    errors: list[str] = []
    params: dict[str, Any] = {"filter": f"timestamp gt {start_time} and timestamp lt {end_time}"}
    if site_id:
        params["site-id"] = site_id
    if interface_id:
        params["interface-id"] = interface_id
    if uplink is not None:
        params["uplink"] = str(uplink).lower()

    for endpoint in [
        f"/network-monitoring/v1/switches/{serial_number}/interface-trends",
        f"/network-monitoring/v1alpha1/switch/{serial_number}/interface-trends",
    ]:
        try:
            response = client._request("GET", endpoint, params=params)
            if response.status_code == 404:
                errors.append(f"404 at {endpoint}")
                continue
            if response.status_code not in (200, 201, 202):
                errors.append(f"HTTP {response.status_code} at {endpoint}")
                continue
            return {
                "serial_number": serial_number,
                "trends": response.json(),
                "endpoint_used": endpoint,
                "errors": errors,
            }
        except Exception as exc:
            errors.append(str(exc))

    return {"serial_number": serial_number, "trends": None, "endpoint_used": None, "errors": errors}


# ── AP Sub-Resources ──────────────────────────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
def get_ap_radios(serial_number: str) -> dict[str, Any]:
    """List radios on an AP with band, channel, power, utilization, and mode."""
    client = get_client()
    errors: list[str] = []

    for endpoint in [
        f"/network-monitoring/v1/aps/{serial_number}/radios",
        f"/network-monitoring/v1alpha1/aps/{serial_number}/radios",
    ]:
        try:
            response = client._request("GET", endpoint)
            if response.status_code == 404:
                errors.append(f"404 at {endpoint}")
                continue
            if response.status_code not in (200, 201, 202):
                errors.append(f"HTTP {response.status_code} at {endpoint}")
                continue
            data = response.json()
            radios = data.get("radios", data.get("items", data))
            return {
                "serial_number": serial_number,
                "radios": radios,
                "endpoint_used": endpoint,
                "errors": errors,
            }
        except Exception as exc:
            errors.append(str(exc))

    return {"serial_number": serial_number, "radios": None, "endpoint_used": None, "errors": errors}


@mcp.tool(annotations=READ_ONLY)
def get_ap_ports(serial_number: str) -> dict[str, Any]:
    """List wired ports on an AP with link state, speed, VLAN, and duplex."""
    client = get_client()
    errors: list[str] = []

    for endpoint in [
        f"/network-monitoring/v1/aps/{serial_number}/ports",
        f"/network-monitoring/v1alpha1/aps/{serial_number}/ports",
    ]:
        try:
            response = client._request("GET", endpoint)
            if response.status_code == 404:
                errors.append(f"404 at {endpoint}")
                continue
            if response.status_code not in (200, 201, 202):
                errors.append(f"HTTP {response.status_code} at {endpoint}")
                continue
            data = response.json()
            ports = data.get("ports", data.get("items", data))
            return {
                "serial_number": serial_number,
                "ports": ports,
                "endpoint_used": endpoint,
                "errors": errors,
            }
        except Exception as exc:
            errors.append(str(exc))

    return {"serial_number": serial_number, "ports": None, "endpoint_used": None, "errors": errors}


# ── SLE ───────────────────────────────────────────────────────────────────────
#
# get_sle_metrics was removed: neither /network-monitoring/v1/sle nor
# /network-monitoring/v1alpha1/sle nor any sibling variant
# (/service-level, /wireless-service-level, /connectivity/sle) exist in the
# New Central API. No reviewed peer MCP wraps SLE either. Bring it back
# here only when the official API exposes a real path.


# ── WLANs ─────────────────────────────────────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
def list_wlans(limit: int = 100, offset: int = 0, next_cursor: str | None = None) -> dict[str, Any]:
    """List all WLANs visible in New Central monitoring.

    Paginates with a `next` cursor (getwlanlistv1), not offset. Pass
    next_cursor from a prior response's `next` field to page forward;
    offset is translated to an approximate starting cursor when
    next_cursor is omitted.
    """
    client = get_client()
    lim = clamp_limit(limit)
    off = max(0, offset)
    cursor = next_cursor or (str(off + 1) if off > 0 else None)
    params: dict[str, Any] = {"limit": lim}
    if cursor:
        params["next"] = cursor
    try:
        return client.get("/network-monitoring/v1/wlans", params=params)
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool(annotations=READ_ONLY)
def get_wlan(wlan_name: str) -> dict[str, Any]:
    """Fetch monitoring details for a single WLAN by name."""
    client = get_client()
    try:
        return client.get(f"/network-monitoring/v1/wlans/{wlan_name}")
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool(annotations=READ_ONLY)
def list_ap_wlans(serial_number: str) -> dict[str, Any]:
    """List WLANs currently active on a specific AP."""
    client = get_client()
    try:
        return client.get(f"/network-monitoring/v1/aps/{serial_number}/wlans")
    except Exception as exc:
        return {"error": str(exc)}


# ── Gateway Clusters ──────────────────────────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
def get_cluster_members(cluster_name: str) -> dict[str, Any]:
    """List members of a gateway cluster."""
    client = get_client()
    try:
        return client.get(f"/network-monitoring/v1/clusters/{cluster_name}/members")
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool(annotations=READ_ONLY)
def get_cluster_tunnels(cluster_name: str) -> dict[str, Any]:
    """List tunnels for a gateway cluster."""
    client = get_client()
    try:
        return client.get(f"/network-monitoring/v1/clusters/{cluster_name}/tunnels")
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool(annotations=READ_ONLY)
def get_cluster_tunnel_health(cluster_name: str) -> dict[str, Any]:
    """Get tunnel health summary (up/down counts) for a gateway cluster."""
    client = get_client()
    try:
        return client.get(f"/network-monitoring/v1/clusters/{cluster_name}/tunnels-health-summary")
    except Exception as exc:
        return {"error": str(exc)}


# ── Switch Extended Monitoring ───────────────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
def get_switch_stacking_info(serial_number: str) -> dict[str, Any]:
    """Get stacking status for a CX switch stack.

    Returns stack members, roles (conductor/standby/member), serial numbers,
    MAC addresses, and forwarding-plane health. Returns a not-applicable
    response for standalone switches. Note: stacking sub-path endpoints
    are not yet exposed in New Central — use get_switch_details which
    includes stackId and switchRole fields.
    """
    client = get_client()
    errors: list[str] = []
    for endpoint in [
        f"/network-monitoring/v1/switches/{serial_number}/stack",
        f"/network-monitoring/v1alpha1/switch/{serial_number}/stack",
        f"/network-monitoring/v1/switches/{serial_number}/stack-members",
    ]:
        try:
            resp = client._request("GET", endpoint)
            if resp.status_code in (400, 404):
                errors.append(f"HTTP {resp.status_code} at {endpoint}")
                continue
            if resp.status_code not in (200, 201, 202):
                errors.append(compact_http_error(resp, endpoint))
                continue
            data = resp.json()
            return {
                "serial_number": serial_number,
                "stack": data,
                "endpoint_used": endpoint,
                "errors": errors,
            }
        except Exception as exc:
            errors.append(f"{endpoint}: {exc}")
    return {
        "serial_number": serial_number,
        "stack": None,
        "errors": errors,
        "_note": (
            "Stack endpoint not found — switch may be standalone or the "
            "endpoint may not be available"
        ),
    }


# ── Wireless Extended Monitoring ─────────────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
def get_channel_utilization(serial_number: str) -> dict[str, Any]:
    """Get per-radio channel utilization and noise floor for an AP.

    Returns busy percentage, noise floor (dBm), channel number, and
    interference score for each radio. The first metric to check when
    clients are slow but signal is good.
    """
    client = get_client()
    errors: list[str] = []
    for endpoint in [
        f"/network-monitoring/v1/aps/{serial_number}/radios",
        f"/network-monitoring/v1alpha1/aps/{serial_number}/rf-stats",
        f"/network-monitoring/v1/aps/{serial_number}/channel-utilization",
    ]:
        try:
            resp = client._request("GET", endpoint)
            if resp.status_code in (400, 404):
                errors.append(f"HTTP {resp.status_code} at {endpoint}")
                continue
            if resp.status_code not in (200, 201, 202):
                errors.append(compact_http_error(resp, endpoint))
                continue
            data = resp.json()
            radios = data.get(
                "radios", data.get("items", data if isinstance(data, list) else [data])
            )
            summary = []
            for r in radios if isinstance(radios, list) else []:
                summary.append(
                    {
                        "band": r.get("band") or r.get("radio_band"),
                        "channel": r.get("channel") or r.get("primary_channel"),
                        "utilization_pct": r.get("utilization") or r.get("channel_utilization"),
                        "noise_floor_dbm": r.get("noise") or r.get("noise_floor"),
                        "tx_power_dbm": r.get("txPower") or r.get("tx_power"),
                        "client_count": r.get("clientCount") or r.get("client_count"),
                    }
                )
            return {
                "serial_number": serial_number,
                "endpoint_used": endpoint,
                "radios": summary,
                "raw": data,
                "errors": errors,
            }
        except Exception as exc:
            errors.append(f"{endpoint}: {exc}")
    return {"serial_number": serial_number, "radios": None, "errors": errors}


@mcp.tool(annotations=READ_ONLY)
def list_rogue_aps(
    site_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """List rogue and interfering APs detected by the wireless infrastructure.

    Returns rogue BSSID, SSID, channel, RSSI, classification (rogue/interfering/
    neighbour), and detecting AP. Note: rogue AP endpoints (/rogues, /rogue-aps)
    are not yet exposed in New Central — this tool will return an empty result
    with an explanatory note until the endpoint is available.
    """
    client = get_client()
    errors: list[str] = []
    params: dict[str, Any] = {"limit": clamp_limit(limit)}
    if site_id:
        params["site-id"] = site_id
    for endpoint in [
        "/network-monitoring/v1/rogues",
        "/network-monitoring/v1alpha1/rogues",
        "/network-monitoring/v1/rogue-aps",
    ]:
        try:
            resp = client._request("GET", endpoint, params=params)
            if resp.status_code in (400, 404):
                errors.append(f"HTTP {resp.status_code} at {endpoint}")
                continue
            if resp.status_code not in (200, 201, 202):
                errors.append(compact_http_error(resp, endpoint))
                continue
            data = resp.json()
            items = data.get("rogues", data.get("items", data if isinstance(data, list) else []))
            return bound_collection_response(items, limit=limit, offset=0)
        except Exception as exc:
            errors.append(f"{endpoint}: {exc}")
    return {
        "items": [],
        "errors": errors,
        "_note": "Rogue AP endpoint not found or no rogues detected",
    }


@mcp.tool(annotations=READ_ONLY)
def get_ap_neighbors(serial_number: str) -> dict[str, Any]:
    """Get neighboring APs visible to this AP with RSSI and channel.

    Returns BSSIDs, SSIDs, channels, and signal strength of APs heard by
    this AP. Useful for coverage overlap analysis and co-channel interference
    identification.
    """
    client = get_client()
    errors: list[str] = []
    for endpoint in [
        f"/network-monitoring/v1/aps/{serial_number}/neighbors",
        f"/network-monitoring/v1alpha1/aps/{serial_number}/neighbors",
        f"/network-monitoring/v1/aps/{serial_number}/rf-neighbors",
    ]:
        try:
            resp = client._request("GET", endpoint)
            if resp.status_code in (400, 404):
                errors.append(f"HTTP {resp.status_code} at {endpoint}")
                continue
            if resp.status_code not in (200, 201, 202):
                errors.append(compact_http_error(resp, endpoint))
                continue
            data = resp.json()
            neighbors = data.get(
                "neighbors", data.get("items", data if isinstance(data, list) else [])
            )
            return {
                "serial_number": serial_number,
                "neighbors": neighbors,
                "endpoint_used": endpoint,
                "errors": errors,
            }
        except Exception as exc:
            errors.append(f"{endpoint}: {exc}")
    return {
        "serial_number": serial_number,
        "neighbors": None,
        "errors": errors,
        "_note": "Neighbor endpoint not found — may not be available in New Central yet",
    }


@mcp.tool(annotations=READ_ONLY)
def get_client_signal_history(
    mac_address: str,
    hours: int = 24,
) -> dict[str, Any]:
    """Get RSSI and SNR history for a wireless client over the past N hours.

    Returns signal strength trends showing whether a client's poor performance
    is due to degrading signal or is intermittent. Note: client sub-path
    endpoints (signal-history, trends) are not yet exposed in New Central.
    Use get_client_roaming_history for event-based connection history instead.
    """
    client = get_client()
    errors: list[str] = []
    mac_clean = mac_address.replace(":", "").replace("-", "").lower()

    for endpoint in [
        f"/network-monitoring/v1/clients/{mac_clean}/signal-history",
        f"/network-monitoring/v1/clients/{mac_address}/signal-history",
        f"/network-monitoring/v1alpha1/clients/{mac_clean}/signal-history",
        f"/network-monitoring/v1/clients/{mac_clean}/trends",
    ]:
        try:
            resp = client._request("GET", endpoint)
            if resp.status_code in (400, 404):
                errors.append(f"HTTP {resp.status_code} at {endpoint}")
                continue
            if resp.status_code not in (200, 201, 202):
                errors.append(compact_http_error(resp, endpoint))
                continue
            data = resp.json()
            return {
                "mac_address": mac_address,
                "history": data,
                "endpoint_used": endpoint,
                "errors": errors,
            }
        except Exception as exc:
            errors.append(f"{endpoint}: {exc}")
    return {
        "mac_address": mac_address,
        "history": None,
        "errors": errors,
        "_note": (
            "Signal history endpoint not found — use "
            "get_client_roaming_history for event-based history"
        ),
    }


@mcp.tool(annotations=READ_ONLY)
def list_ssid_clients(
    ssid_name: str,
    site_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """List all clients currently connected to a specific SSID.

    Returns client MAC, IP, hostname, signal, band, and connected AP.
    Useful for per-SSID capacity checks and isolating SSID-specific issues.
    """
    clients = get_mcp_client().get_clients(
        site_id=site_id,
        ssid=ssid_name,
        limit=clamp_limit(limit),
    )
    return bound_collection_response(clients, limit=limit, offset=0)


@mcp.tool(annotations=READ_ONLY)
def locate_client(mac_address: str) -> dict[str, Any]:
    """Get the approximate physical location of a client.

    Returns floor plan coordinates, building, floor, and nearest AP where
    available. Note: location endpoints (/location/v1) require a separate
    location services licence and are not available on all Central instances.
    """
    client = get_client()
    errors: list[str] = []
    mac_clean = mac_address.replace(":", "").replace("-", "").lower()

    for endpoint in [
        f"/network-monitoring/v1/clients/{mac_clean}/location",
        f"/network-monitoring/v1/clients/{mac_address}/location",
        f"/network-monitoring/v1alpha1/clients/{mac_clean}/location",
        f"/location/v1/clients/{mac_clean}",
    ]:
        try:
            resp = client._request("GET", endpoint)
            if resp.status_code in (400, 404):
                errors.append(f"HTTP {resp.status_code} at {endpoint}")
                continue
            if resp.status_code not in (200, 201, 202):
                errors.append(compact_http_error(resp, endpoint))
                continue
            return {
                "mac_address": mac_address,
                "location": resp.json(),
                "endpoint_used": endpoint,
                "errors": errors,
            }
        except Exception as exc:
            errors.append(f"{endpoint}: {exc}")
    return {
        "mac_address": mac_address,
        "location": None,
        "errors": errors,
        "_note": "Location endpoint not found — location services may require a separate licence",
    }


@mcp.tool(annotations=READ_ONLY)
def get_air_quality(serial_number: str) -> dict[str, Any]:
    """Get air quality and interference metrics for an AP.

    Returns interference score, non-Wi-Fi interference sources, duty cycle,
    and air quality index per radio. Note: air-quality and rf-health sub-paths
    are not yet exposed in New Central — use get_channel_utilization (AP radios
    endpoint) for available RF metrics in the meantime.
    """
    client = get_client()
    errors: list[str] = []
    for endpoint in [
        f"/network-monitoring/v1/aps/{serial_number}/air-quality",
        f"/network-monitoring/v1alpha1/aps/{serial_number}/air-quality",
        f"/network-monitoring/v1/aps/{serial_number}/rf-health",
    ]:
        try:
            resp = client._request("GET", endpoint)
            if resp.status_code in (400, 404):
                errors.append(f"HTTP {resp.status_code} at {endpoint}")
                continue
            if resp.status_code not in (200, 201, 202):
                errors.append(compact_http_error(resp, endpoint))
                continue
            return {
                "serial_number": serial_number,
                "air_quality": resp.json(),
                "endpoint_used": endpoint,
                "errors": errors,
            }
        except Exception as exc:
            errors.append(f"{endpoint}: {exc}")
    return {
        "serial_number": serial_number,
        "air_quality": None,
        "errors": errors,
        "_note": "Air quality endpoint not found — may not be available in New Central yet",
    }


# ── Client History ───────────────────────────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
def get_client_roaming_history(
    mac_address: str,
    hours: int = 24,
) -> dict[str, Any]:
    """Show where a client has roamed across switches/APs over the past N hours.

    Queries Client Onboarding events across all switches and APs in the
    environment, filtered to the given MAC. Returns a chronological list of
    connections showing which device/port/VLAN the client was seen on and when.
    Useful for tracing connectivity issues that follow a user around.
    """
    client = get_mcp_client()

    devices = client.get_devices(limit=200)
    switches = [d for d in devices if "SWITCH" in (d.get("deviceType") or "").upper()]
    aps = [d for d in devices if d.get("deviceType") in ("AP", "ACCESS_POINT")]

    mac_lower = mac_address.lower()
    history: list[dict[str, Any]] = []

    for device in switches + aps:
        serial = device.get("serialNumber") or device.get("id", "")
        if not serial:
            continue
        events = client.get_events(serial, hours=hours, api_limit=500)
        for e in events:
            if (e.get("clientMacAddress") or "").lower() == mac_lower:
                history.append(
                    {
                        "time": e.get("timeAt"),
                        "event": e.get("eventName"),
                        "device_name": device.get("deviceName") or serial,
                        "device_serial": serial,
                        "device_type": device.get("deviceType"),
                        "description": e.get("description"),
                    }
                )

    history.sort(key=lambda x: x.get("time") or "", reverse=True)

    return {
        "mac_address": mac_address,
        "hours_analyzed": hours,
        "devices_scanned": len(switches) + len(aps),
        "event_count": len(history),
        "history": history,
    }


# ── Intelligence / Anomaly Detection ─────────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
def detect_client_flapping(
    serial_number: str,
    hours: int = 24,
    min_events: int = 5,
) -> dict[str, Any]:
    """Detect wired/wireless clients re-onboarding abnormally often on a switch.

    Fetches Client Onboarding events for the device over the past N hours and
    flags any MAC address that appears >= min_events times. Useful for catching
    VMware NIC resets, 802.1X re-auth loops, or flapping endpoints that Central
    does not surface as a port-flapping alert.

    Returns flagged clients sorted by event count descending, plus a summary.
    """
    events = get_mcp_client().get_events(serial_number, hours=hours, api_limit=1000)

    onboard_events = [
        e for e in events if e.get("eventName") == "Client Onboarding" and e.get("clientMacAddress")
    ]

    counts: dict[str, list[str]] = {}
    for e in onboard_events:
        mac = e["clientMacAddress"]
        ts = e.get("timeAt", "")
        counts.setdefault(mac, []).append(ts)

    flagged = [
        {
            "mac": mac,
            "event_count": len(timestamps),
            "first_seen": min(timestamps) if timestamps else None,
            "last_seen": max(timestamps) if timestamps else None,
            "source_name": next(
                (e.get("sourceName") for e in onboard_events if e.get("clientMacAddress") == mac),
                None,
            ),
        }
        for mac, timestamps in counts.items()
        if len(timestamps) >= min_events
    ]
    flagged.sort(key=lambda x: x["event_count"], reverse=True)

    return {
        "serial_number": serial_number,
        "hours_analyzed": hours,
        "min_events_threshold": min_events,
        "total_onboard_events": len(onboard_events),
        "flagged_clients": flagged,
        "flagged_count": len(flagged),
    }


@mcp.tool(annotations=READ_ONLY)
def detect_ssh_brute_force(
    serial_number: str,
    hours: int = 24,
    min_failures: int = 3,
) -> dict[str, Any]:
    """Detect SSH brute-force or misconfigured clients targeting a switch.

    Scans switch events for SSH login failures (eventId 5210) and SSH session
    denials (eventId 5214), groups by source IP, and flags any IP that hits
    >= min_failures within the time window (min_failures is clamped to >= 1 so
    a zero/negative threshold can't flag every source).

    Events whose description carries no parseable IPv4 source are counted
    separately as ``unattributed_failures`` rather than aggregated under a
    single ``"unknown"`` pseudo-attacker (which would fabricate a false
    positive from unrelated events). Returns flagged IPs sorted by failure
    count descending.
    """
    import re

    min_failures = max(1, min_failures)
    events = get_mcp_client().get_events(serial_number, hours=hours, api_limit=1000)

    ssh_events = [e for e in events if str(e.get("eventId", "")) in ("5210", "5214")]

    _ip_re = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")

    ip_failures: dict[str, list[dict[str, Any]]] = {}
    unattributed = 0
    for e in ssh_events:
        desc = e.get("description", "")
        match = _ip_re.search(desc)
        if match is None:
            # No parseable IPv4 source (IPv6/hostname-only description) —
            # aggregating these into one pseudo-attacker would inflate a
            # false positive from unrelated events.
            unattributed += 1
            continue
        ip_failures.setdefault(match.group(1), []).append(
            {
                "event_id": e.get("eventId"),
                "event_name": e.get("eventName"),
                "description": desc,
                "time": e.get("timeAt"),
            }
        )

    flagged = []
    for ip, evts in ip_failures.items():
        if len(evts) < min_failures:
            continue
        times = [e["time"] for e in evts if e["time"]]
        flagged.append(
            {
                "source_ip": ip,
                "failure_count": len(evts),
                "first_seen": min(times) if times else None,
                "last_seen": max(times) if times else None,
                "event_types": list({e["event_name"] for e in evts}),
            }
        )
    flagged.sort(key=lambda x: x["failure_count"], reverse=True)

    return {
        "serial_number": serial_number,
        "hours_analyzed": hours,
        "min_failures_threshold": min_failures,
        "total_ssh_failure_events": len(ssh_events),
        "unattributed_failures": unattributed,
        "flagged_sources": flagged,
        "flagged_count": len(flagged),
    }


@mcp.tool(annotations=READ_ONLY)
def get_site_health_summary(
    site_id: str | None = None,
    site_name: str | None = None,
) -> dict[str, Any]:
    """Return a single-view health summary for a site.

    Aggregates: device status counts, client count, active alert counts by
    severity, and recent notable switch/AP events (last 24h). Either site_id
    or site_name must be provided.
    """
    client = get_mcp_client()

    if not site_id and site_name:
        site = client.get_site_by_name(site_name)
        if not site:
            return {"error": f"Site not found: {site_name}"}
        site_id = site.get("scopeId") or site.get("siteId") or site.get("id")
        resolved_name = site.get("scopeName") or site.get("siteName") or site_name
    elif site_id:
        resolved_name = site_id
    else:
        return {"error": "Provide site_id or site_name"}

    devices = client.get_devices(filters={"siteId": site_id} if site_id else {}, limit=200)
    clients = client.get_clients(site_id=site_id, limit=200)
    alerts = client.get_alerts(site_id=site_id, limit=200)

    device_status: dict[str, int] = {}
    device_type_counts: dict[str, int] = {}
    for d in devices:
        status = (d.get("status") or "UNKNOWN").upper()
        device_status[status] = device_status.get(status, 0) + 1
        dtype = (d.get("deviceType") or "UNKNOWN").upper()
        device_type_counts[dtype] = device_type_counts.get(dtype, 0) + 1

    alert_severity: dict[str, int] = {}
    for a in alerts:
        sev = (a.get("severity") or "UNKNOWN").upper()
        alert_severity[sev] = alert_severity.get(sev, 0) + 1

    notable_event_names = {
        "INTERFACE",
        "Device Down",
        "Device Up",
        "Client Onboarding",
        "SSH User Login Failure",
        "SSH Failure",
        "PoE",
    }
    recent_events: list[dict[str, Any]] = []
    switches = [d for d in devices if "SWITCH" in (d.get("deviceType") or "").upper()]
    for sw in switches[:5]:
        serial = sw.get("serialNumber") or sw.get("id", "")
        if not serial:
            continue
        evts = client.get_events(serial, hours=24, api_limit=200)
        for e in evts:
            if e.get("eventName") in notable_event_names:
                recent_events.append(
                    {
                        "device": sw.get("deviceName") or serial,
                        "event": e.get("eventName"),
                        "description": e.get("description"),
                        "time": e.get("timeAt"),
                    }
                )
    recent_events.sort(key=lambda x: x.get("time") or "", reverse=True)

    return {
        "site": resolved_name,
        "site_id": site_id,
        "devices": {
            "total": len(devices),
            "by_status": device_status,
            "by_type": device_type_counts,
        },
        "clients": {
            "total": len(clients),
        },
        "alerts": {
            "total": len(alerts),
            "by_severity": alert_severity,
        },
        "recent_notable_events": recent_events[:20],
    }


# ── Topology ──────────────────────────────────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
def get_topology(site_id: str) -> dict[str, Any]:
    """Fetch the network topology (nodes + links) for a site.

    GET /network-monitoring/v1/topology/{site-id}.
    """
    site = site_id.strip()
    if not site:
        raise ValueError("site_id must be a non-empty string")
    endpoint = f"/network-monitoring/v1/topology/{quote(site, safe='')}"
    try:
        return get_client().get(endpoint)
    except Exception as exc:
        return {"error": str(exc), "endpoint_used": endpoint}


# ── Swarm Inventory ───────────────────────────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
def list_swarms(
    filter: str | None = None,
    sort: str | None = None,
    limit: int = 20,
    offset: int = 0,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    """List AP swarms/clusters (conductor + members) from network-monitoring/v1/swarms.

    Paginates with a `next` cursor (getswarmsv1), not offset. Pass
    next_cursor from a prior response's `next` field to page forward;
    offset is translated to an approximate starting cursor when
    next_cursor is omitted.
    """
    params: dict[str, Any] = {"limit": clamp_limit(limit, default=20)}
    off = max(0, offset)
    cursor = next_cursor or (str(off + 1) if off > 0 else None)
    if cursor:
        params["next"] = cursor
    if filter:
        params["filter"] = filter
    if sort:
        params["sort"] = sort
    try:
        return get_client().get("/network-monitoring/v1/swarms", params=params)
    except Exception as exc:
        return {"error": str(exc), "endpoint_used": "/network-monitoring/v1/swarms"}


@mcp.tool(annotations=READ_ONLY)
def get_swarm(cluster_id: str) -> dict[str, Any]:
    """Fetch a single AP swarm/cluster by ID from network-monitoring/v1/swarms/{cluster-id}."""
    cluster = cluster_id.strip()
    if not cluster:
        raise ValueError("cluster_id must be a non-empty string")
    endpoint = f"/network-monitoring/v1/swarms/{quote(cluster, safe='')}"
    try:
        return get_client().get(endpoint)
    except Exception as exc:
        return {"error": str(exc), "endpoint_used": endpoint}


# ── AP Tunnel Telemetry ───────────────────────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
def list_ap_tunnels(
    serial_number: str,
    site_id: str | None = None,
    filter: str | None = None,
    sort: str | None = None,
    limit: int = 20,
    offset: int = 0,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    """List GRE/IPsec tunnels reported by an AP (network-monitoring/v1/aps/{serial}/tunnels).

    Paginates with a `next` cursor (getaccesspointtunnellistv1), not offset.
    Pass next_cursor from a prior response's `next` field to page forward;
    offset is translated to an approximate starting cursor when
    next_cursor is omitted.
    """
    params: dict[str, Any] = {"limit": clamp_limit(limit, default=20)}
    off = max(0, offset)
    cursor = next_cursor or (str(off + 1) if off > 0 else None)
    if cursor:
        params["next"] = cursor
    if site_id:
        params["site-id"] = site_id
    if filter:
        params["filter"] = filter
    if sort:
        params["sort"] = sort
    endpoint = f"/network-monitoring/v1/aps/{quote(serial_number, safe='')}/tunnels"
    try:
        return get_client().get(endpoint, params=params)
    except Exception as exc:
        return {"error": str(exc), "endpoint_used": endpoint}


@mcp.tool(annotations=READ_ONLY)
def get_ap_tunnel(serial_number: str, tunnel_id: str) -> dict[str, Any]:
    """Fetch detail for a single AP tunnel by ID.

    GET /network-monitoring/v1/aps/{serial}/tunnels/{tunnel-id}.
    """
    endpoint = (
        f"/network-monitoring/v1/aps/{quote(serial_number, safe='')}"
        f"/tunnels/{quote(tunnel_id, safe='')}"
    )
    try:
        return get_client().get(endpoint)
    except Exception as exc:
        return {"error": str(exc), "endpoint_used": endpoint}


@mcp.tool(annotations=READ_ONLY)
def get_ap_tunnel_throughput(
    serial_number: str,
    tunnel_id: str,
    start_time: str,
    end_time: str,
) -> dict[str, Any]:
    """Time-series throughput for a single AP tunnel over a time window.

    start_time/end_time: ISO 8601, applied as an OData `timestamp` filter
    (matches get_device_trends convention elsewhere in this module).
    """
    endpoint = (
        f"/network-monitoring/v1/aps/{quote(serial_number, safe='')}"
        f"/tunnels/{quote(tunnel_id, safe='')}/throughput"
    )
    params = {"filter": f"timestamp gt {start_time} and timestamp lt {end_time}"}
    try:
        return get_client().get(endpoint, params=params)
    except Exception as exc:
        return {"error": str(exc), "endpoint_used": endpoint}


# ── Application Visibility ────────────────────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
def list_applications(
    site_id: str,
    start_time: str,
    end_time: str,
    client_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """List application-visibility records for a site over a time window.

    GET /network-monitoring/v1/applications. start_time/end_time must be
    RFC 3339 timestamps within 7 days of each other. Unlike most other
    network-monitoring v1 list endpoints, this one uses true limit/offset
    pagination (applicationsv1), not a `next` cursor.
    """
    params: dict[str, Any] = {
        "site-id": site_id,
        "start-at": start_time,
        "end-at": end_time,
        "limit": clamp_limit(limit),
        "offset": max(0, offset),
    }
    if client_id:
        params["client-id"] = client_id
    try:
        return get_client().get("/network-monitoring/v1/applications", params=params)
    except Exception as exc:
        return {"error": str(exc), "endpoint_used": "/network-monitoring/v1/applications"}


# ── Reporting ─────────────────────────────────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
def list_reports(
    search: str | None = None,
    filter: str | None = None,
    sort: str | None = None,
    limit: int = 10,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    """List saved Central reports from network-reporting/v1/reports.

    Cursor-paginated (`next`, per the reporting v1 reference) — pass
    next_cursor from a prior response's `next` field to page forward.
    """
    params: dict[str, Any] = {"limit": max(1, min(limit, 100))}
    if next_cursor:
        params["next"] = next_cursor
    if search:
        params["search"] = search
    if filter:
        params["filter"] = filter
    if sort:
        params["sort"] = sort
    try:
        return get_client().get("/network-reporting/v1/reports", params=params)
    except Exception as exc:
        return {"error": str(exc), "endpoint_used": "/network-reporting/v1/reports"}


@mcp.tool(annotations=READ_ONLY)
def list_report_runs(
    report_id: str,
    sort: str | None = None,
    limit: int = 10,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    """List report-run history for a saved report.

    GET /network-reporting/v1alpha1/reports/{report_id}/report-runs.
    """
    params: dict[str, Any] = {"limit": max(1, min(limit, 100))}
    if next_cursor:
        params["next"] = next_cursor
    if sort:
        params["sort"] = sort
    endpoint = f"/network-reporting/v1alpha1/reports/{quote(str(report_id), safe='')}/report-runs"
    try:
        return get_client().get(endpoint, params=params)
    except Exception as exc:
        return {"error": str(exc), "endpoint_used": endpoint}


@mcp.tool(annotations=READ_ONLY)
def get_reports_metadata() -> dict[str, Any]:
    """Fetch reporting metadata (available report types/fields).

    GET /network-reporting/v1alpha1/reports-metadata.
    """
    try:
        return get_client().get("/network-reporting/v1alpha1/reports-metadata")
    except Exception as exc:
        return {"error": str(exc), "endpoint_used": "/network-reporting/v1alpha1/reports-metadata"}


@mcp.tool(annotations=READ_ONLY)
def get_reporting_service_health() -> dict[str, Any]:
    """Fetch reporting-service health status from network-reporting/v1alpha1/reports/health."""
    try:
        return get_client().get("/network-reporting/v1alpha1/reports/health")
    except Exception as exc:
        return {"error": str(exc), "endpoint_used": "/network-reporting/v1alpha1/reports/health"}


# ── Report Lifecycle & Report-Run Execution (manifest-confirmed) ────────────
#
# Endpoint shapes below are read directly from the committed Central
# manifest (src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/central.json, source_file
# reporting-11dzpz39mq.json: createUserReportV1, getUserReportV1,
# updateUserReportV1, deleteUserReportV1, deleteReportRunV1,
# downloadReportLinkV1) -- confirmed request/response shapes, unlike the
# best-effort notification-rule CRUD group below. listReportsV1,
# listReportRunsV1, and getReportsMetadataV1 are already covered by
# list_reports/list_report_runs/get_reports_metadata above; the tools below
# add the remaining report lifecycle and report-run execution operations
# from that same manifest entry.

_REPORTS_BASE = "/network-reporting/v1/reports"


async def _confirm_report_action(
    ctx: Context, action: str, report_id: str | None
) -> dict[str, Any] | None:
    try:
        result = await ctx.elicit(
            message=f"Confirm report {action}{f' for {report_id}' if report_id else ''}?",
            schema=_ConfirmAction,
        )
    except Exception as exc:
        return {
            "status": "CONFIRMATION_UNAVAILABLE",
            "error": f"client does not support elicitation; operation NOT performed: {exc}",
        }
    if result.action != "accept" or not result.data.confirm:
        return {"status": "CANCELLED", "detail": "user declined confirmation"}
    return None


def _validated_write_result(response: Any, endpoint: str) -> dict[str, Any]:
    """Validate an executed write response and body, returning a JSON dict or an error envelope.

    Fails closed (via `validate_write_result`) on a non-2xx response or an
    error-shaped envelope instead of returning a success-shaped result with
    the failure buried in an `errors` list; unlike `validate_write_result`'s
    default behavior of raising, both failure modes are caught here and
    returned as `{"error": ..., "endpoint_used": ...}` to match this
    module's existing non-raising tool contract.
    """
    try:
        validate_write_result(response, context=endpoint)
    except WriteResultError as exc:
        return {"error": str(exc), "endpoint_used": endpoint}
    data = _json_response(response)
    try:
        validate_write_result(data, context=endpoint)
    except WriteResultError as exc:
        return {"error": str(exc), "endpoint_used": endpoint}
    return data | {"endpoint_used": endpoint}


@mcp.tool(annotations=READ_ONLY)
def get_report(report_id: str) -> dict[str, Any]:
    """Fetch a single saved report by ID.

    GET /network-reporting/v1/reports/{report-id} (manifest-confirmed).
    Report ID can be obtained from list_reports.
    """
    endpoint = f"{_REPORTS_BASE}/{quote(str(report_id), safe='')}"
    try:
        data = get_client().get(endpoint)
        if isinstance(data, dict):
            return data | {"endpoint_used": endpoint}
        return {"data": data, "endpoint_used": endpoint}
    except Exception as exc:
        return {"error": str(exc), "endpoint_used": endpoint}


@mcp.tool(annotations=WRITE)
async def create_report(
    ctx: Context,
    body: dict[str, Any],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create a saved report.

    POST /network-reporting/v1/reports (manifest-confirmed). `body` must
    match the Create Report schema: required `name`, `type`, `timeZone`
    (IANA), `filters`, `reportPeriod`, and `reportSchedule`, plus optional
    `email`. See get_reports_metadata for the report-type/KPI/filter catalog
    and the createUserReportV1 manifest entry for the full field reference.
    dry_run returns the payload without sending; otherwise requires
    elicited confirmation.
    """
    if dry_run:
        return {"dry_run": True, "endpoint": _REPORTS_BASE, "payload": body}
    cancelled = await _confirm_report_action(ctx, "create", None)
    if cancelled:
        return cancelled
    response = await get_client()._arequest("POST", _REPORTS_BASE, json=body)
    return _validated_write_result(response, _REPORTS_BASE)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def update_report(
    ctx: Context,
    report_id: str,
    updates: dict[str, Any],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Update a saved report's name and/or email settings.

    PUT /network-reporting/v1/reports/{report-id} (manifest-confirmed;
    documented request body properties are `name` and `email`). Report ID
    can be obtained from list_reports. dry_run returns the payload without
    sending; otherwise requires elicited confirmation.
    """
    endpoint = f"{_REPORTS_BASE}/{quote(str(report_id), safe='')}"
    if dry_run:
        return {"dry_run": True, "endpoint": endpoint, "payload": updates}
    cancelled = await _confirm_report_action(ctx, "update", report_id)
    if cancelled:
        return cancelled
    response = await get_client()._arequest("PUT", endpoint, json=updates)
    return _validated_write_result(response, endpoint)


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_report(
    ctx: Context,
    report_id: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete a saved report and its associated report runs.

    DELETE /network-reporting/v1/reports/{report-id} (manifest-confirmed;
    deletion cascades to the report's report runs per the manifest
    description). Report ID can be obtained from list_reports. dry_run
    returns the endpoint without sending; otherwise requires elicited
    confirmation.
    """
    endpoint = f"{_REPORTS_BASE}/{quote(str(report_id), safe='')}"
    if dry_run:
        return {"dry_run": True, "endpoint": endpoint}
    cancelled = await _confirm_report_action(ctx, "delete", report_id)
    if cancelled:
        return cancelled
    response = await get_client()._arequest("DELETE", endpoint)
    result = _validated_write_result(response, endpoint)
    if "error" in result:
        return result
    return result | {"deleted": True}


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_report_run(
    ctx: Context,
    report_id: str,
    report_run_id: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete a single report-run by ID.

    DELETE /network-reporting/v1/reports/{report-id}/report-runs/{report-run-id}
    (manifest-confirmed). Report ID/report-run ID can be obtained from
    list_reports/list_report_runs. dry_run returns the endpoint without
    sending; otherwise requires elicited confirmation.
    """
    endpoint = (
        f"{_REPORTS_BASE}/{quote(str(report_id), safe='')}"
        f"/report-runs/{quote(str(report_run_id), safe='')}"
    )
    if dry_run:
        return {"dry_run": True, "endpoint": endpoint}
    cancelled = await _confirm_report_action(ctx, "delete report-run", report_run_id)
    if cancelled:
        return cancelled
    response = await get_client()._arequest("DELETE", endpoint)
    result = _validated_write_result(response, endpoint)
    if "error" in result:
        return result
    return result | {"deleted": True}


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def get_report_run_download_link(
    ctx: Context,
    report_id: str,
    report_run_id: str,
    export_type: str = "CSV",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Get a downloadable link for a completed report run.

    POST .../reports/{report-id}/report-runs/{report-run-id}/download-link
    (manifest-confirmed). `export_type` must be "CSV" or "PDF" (maps to the
    required `exportType` request-body field); the response carries the
    download URL, MIME type (application/zip for CSV, application/pdf for
    PDF), and suggested file name. Report ID/report-run ID can be obtained
    from list_reports/list_report_runs. dry_run returns the payload without
    sending; otherwise requires elicited confirmation.
    """
    export_type_norm = str(export_type or "").strip().upper()
    if export_type_norm not in {"CSV", "PDF"}:
        return {"error": "export_type must be 'CSV' or 'PDF'"}
    endpoint = (
        f"{_REPORTS_BASE}/{quote(str(report_id), safe='')}"
        f"/report-runs/{quote(str(report_run_id), safe='')}/download-link"
    )
    payload = {"exportType": export_type_norm}
    if dry_run:
        return {"dry_run": True, "endpoint": endpoint, "payload": payload}
    cancelled = await _confirm_report_action(
        ctx, f"generate a {export_type_norm} download link", report_run_id
    )
    if cancelled:
        return cancelled
    response = await get_client()._arequest("POST", endpoint, json=payload)
    return _validated_write_result(response, endpoint)


# ── Client Onboarding ─────────────────────────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
def list_client_onboarding_events(
    serial_number: str,
    hours: int = 24,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List 'Client Onboarding' events for a device over the past N hours (bounded by default).

    Filters get_events() to eventName == "Client Onboarding" — the same
    event name already relied on by detect_client_flapping and
    get_client_roaming_history elsewhere in this module.
    """
    events = get_mcp_client().get_events(serial_number, hours=hours, api_limit=1000)
    onboarding = [e for e in events if e.get("eventName") == "Client Onboarding"]
    return bound_collection_response(onboarding, limit=limit, offset=offset)


# ── Notification Rule CRUD (alert-config) ────────────────────────────────────
#
# GOTCHA: the New Central developer-docs corpus documents only GET for
# alert-config (backing list_alert_configs above) — no POST/PATCH/DELETE
# reference page was found for this resource. The Central UI's
# "Notification Rules" page does support create/edit/delete/enable/disable
# (see techdocs notify-dash-brd.htm), so a REST counterpart plausibly
# exists at the same base path, but its exact shape is NOT independently
# confirmed. These tools follow the same defensive "try the documented
# REST convention, surface a clear 404 instead of guessing again" pattern
# already used by acknowledge_alert (ops.py) and list_config_templates
# (config.py) for endpoints in the same state.

_ALERT_CONFIG_BASE = "/network-notifications/v1/alert-config"


async def _confirm_notification_rule_action(
    ctx: Context, action: str, rule_id: str | None
) -> dict[str, Any] | None:
    try:
        result = await ctx.elicit(
            message=f"Confirm notification-rule {action}{f' for {rule_id}' if rule_id else ''}?",
            schema=_ConfirmAction,
        )
    except Exception as exc:
        return {
            "status": "CONFIRMATION_UNAVAILABLE",
            "error": f"client does not support elicitation; operation NOT performed: {exc}",
        }
    if result.action != "accept" or not result.data.confirm:
        return {"status": "CANCELLED", "detail": "user declined confirmation"}
    return None


@mcp.tool(annotations=DESTRUCTIVE)
async def create_notification_rule(
    ctx: Context,
    body: dict[str, Any],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create a notification rule (alert-config).

    UNCONFIRMED endpoint shape — see module note above.

    body must match the alert-config schema returned by list_alert_configs
    (scopeId, scopeType, category, destination, etc.). dry_run returns the
    payload without sending; otherwise requires elicited confirmation.
    """
    if dry_run:
        return {"dry_run": True, "endpoint": _ALERT_CONFIG_BASE, "payload": body}
    cancelled = await _confirm_notification_rule_action(ctx, "create", None)
    if cancelled:
        return cancelled
    response = await get_client()._arequest("POST", _ALERT_CONFIG_BASE, json=body)
    if response.status_code not in (200, 201, 202):
        return {
            "error": compact_http_error(response, _ALERT_CONFIG_BASE),
            "endpoint_used": _ALERT_CONFIG_BASE,
        }
    return _json_response(response) | {"endpoint_used": _ALERT_CONFIG_BASE}


@mcp.tool(annotations=DESTRUCTIVE)
async def update_notification_rule(
    ctx: Context,
    rule_id: str,
    updates: dict[str, Any],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Update a notification rule by ID (alert-config).

    UNCONFIRMED endpoint shape — see module note above.
    """
    endpoint = f"{_ALERT_CONFIG_BASE}/{quote(rule_id, safe='')}"
    if dry_run:
        return {"dry_run": True, "endpoint": endpoint, "payload": updates}
    cancelled = await _confirm_notification_rule_action(ctx, "update", rule_id)
    if cancelled:
        return cancelled
    response = await get_client()._arequest("PATCH", endpoint, json=updates)
    if response.status_code not in (200, 201, 202, 204):
        return {"error": compact_http_error(response, endpoint), "endpoint_used": endpoint}
    return _json_response(response) | {"endpoint_used": endpoint}


@mcp.tool(annotations=DESTRUCTIVE)
async def set_notification_rule_enabled(
    ctx: Context,
    rule_id: str,
    enabled: bool,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Enable or disable a notification rule by ID.

    UNCONFIRMED endpoint shape — see module note above.

    Central UI disallows enabling a rule with no destination configured;
    the API is expected to enforce the same constraint (surfaced as a 400).
    """
    endpoint = f"{_ALERT_CONFIG_BASE}/{quote(rule_id, safe='')}"
    payload = {"enable": enabled}
    if dry_run:
        return {"dry_run": True, "endpoint": endpoint, "payload": payload}
    cancelled = await _confirm_notification_rule_action(
        ctx, "enable" if enabled else "disable", rule_id
    )
    if cancelled:
        return cancelled
    response = await get_client()._arequest("PATCH", endpoint, json=payload)
    if response.status_code not in (200, 201, 202, 204):
        return {"error": compact_http_error(response, endpoint), "endpoint_used": endpoint}
    return _json_response(response) | {"endpoint_used": endpoint}


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_notification_rule(
    ctx: Context,
    rule_id: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete a notification rule by ID. UNCONFIRMED endpoint shape — see module note above."""
    endpoint = f"{_ALERT_CONFIG_BASE}/{quote(rule_id, safe='')}"
    if dry_run:
        return {"dry_run": True, "endpoint": endpoint}
    cancelled = await _confirm_notification_rule_action(ctx, "delete", rule_id)
    if cancelled:
        return cancelled
    response = await get_client()._arequest("DELETE", endpoint)
    if response.status_code not in (200, 202, 204):
        return {"error": compact_http_error(response, endpoint), "endpoint_used": endpoint}
    return _json_response(response) | {"endpoint_used": endpoint, "deleted": True}


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
