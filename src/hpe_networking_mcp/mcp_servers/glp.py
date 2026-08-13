"""MCP server — GreenLake Platform (GLP): inventory, licensing, users, and
service catalog (105 curated + 906 active generated tools; 920 in provenance manifest).

Covers: GLP device lifecycle (v1 + v2beta1), device grouping summaries,
subscription assignment/bulk-add, auto-subscription-setting reads/updates,
audit logs (v2beta1 -- the only version in the manifest), users,
workspaces (incl. contact PATCH), reporting
statuses, service-catalog reads, and a guarded read-only GLP GET. Curated
workflows also cover RBAC role-assignment and scope-group lifecycle
(create/update/delete), identity user lifecycle (invite/update-preferences/
disassociate), event webhooks/subscriptions/deliveries, workspace tags/
locations, and SCIM user/group membership reads (see list_glp_api_families).
v0.7 adds bounded, region-aware curated reads for Compute Ops Management,
Storage Fleet, Block Storage, Virtualization, Backup & Recovery, and Data
Services (each served from regional hosts, not global.api.greenlake.hpe.com --
set GLP_GENERATED_REGION; see list_glp_api_families), a few guarded writes in
those families (VM power on/off incl. a bounded bulk composite, and
run-protection-job-now), and a read-only cross-resource reconciliation/
planning composite (plan_glp_reconciliation).
Uses the target_account (glp_account) credentials.
"""
import asyncio
import os
import re
from typing import Any, Literal
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer

from hpe_networking_mcp.mcp_servers.openapi_gen.http_exec import make_read_executor, make_write_executor
from hpe_networking_mcp.mcp_servers.shared import (
    DESTRUCTIVE,
    IDEMPOTENT_WRITE,
    READ_ONLY,
    bound_collection_response,
    clamp_limit,
    get_glp_client,
    platform_write_blocked,
    platform_writes_allowed,
    redact_sensitive,
    safe_api_path,
)
from hpe_networking_mcp.pipeline.clients.glp_client import (
    _V2BETA1_WRITES_FLAG,
    _writes_enabled,
    glp_write_gate_message,
)

mcp = MCPServer("glp-core")

_GLP_GET_PREFIXES = (
    "/devices/",
    "/subscriptions/",
    "/audit-log/",
    "/audit-logs/",
    "/identity/",
    "/service-catalog/",
    "/workspaces/",
    "/reporting/",
    # Curated typed reads below cover the common workflows in these families;
    # glp_get remains available for documented resources without a named tool.
    "/authorization/",
    "/events/",
    "/webhooks/",
    "/notifications/",
    "/tags/",
    "/locations/",
    "/scim/",
)
_SENSITIVE_QUERY_PARAMS = {"unredacted"}

# Auth header/cookie names never forwarded from model-supplied header params
# for either the opt-in generated GLP tools or the curated region-aware
# family tools below -- trusted GLP auth is always injected last.
_GLP_AUTH_HEADER_NAMES = {"authorization", "cookie"}


@mcp.tool(annotations=READ_ONLY)
def glp_write_status() -> dict[str, Any]:
    """Report whether guarded GLP v2beta1 write tools are enabled.

    The GLP gate is resolved independently of Central's write gate — flipping
    HPE_MCP_CENTRAL_WRITES neither enables nor blocks GreenLake writes.
    """
    from hpe_networking_mcp.mcp_servers.shared import platform_write_gate_state

    enabled = _writes_enabled()
    gate = platform_write_gate_state("glp")
    return {
        "enabled": enabled,
        "flag": _V2BETA1_WRITES_FLAG,
        "set_to_enable": f"{_V2BETA1_WRITES_FLAG}=1",
        "gate_state": gate["state"],
        "gate_source": gate["source"],
        "independent_of": ["HPE_MCP_CENTRAL_WRITES"],
        "guarded_tools": [
            "glp_assign_subscription",
            "glp_add_device",
            "glp_add_devices_bulk",
            "glp_archive_device",
            "update_glp_workspace_contact",
            "glp_add_subscriptions",
            "create_glp_role_assignment",
            "update_glp_role_assignment",
            "delete_glp_role_assignment",
            "create_glp_scope_group",
            "update_glp_scope_group",
            "delete_glp_scope_group",
            "add_glp_scope_group_scopes",
            "delete_glp_scope_group_scopes",
            "invite_glp_user",
            "update_glp_user_preferences",
            "disassociate_glp_user",
            "update_glp_auto_subscription_settings",
            "set_glp_virtual_machine_power",
            "set_glp_virtual_machines_power_bulk",
            "run_glp_backup_protection_job",
        ],
        "message": (
            "GLP write tools can execute."
            if enabled
            else (
                f"GLP write tools are visible but fail closed until {_V2BETA1_WRITES_FLAG}=1. "
                "Central's write gate (HPE_MCP_CENTRAL_WRITES) has no effect on GLP writes."
            )
        ),
    }


def _write_disabled(tool_name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    if _writes_enabled():
        return None
    return {
        "status": "FORBIDDEN",
        "error": glp_write_gate_message(
            tool_name,
            "Set the flag only after sandbox-validating payload and rollback.",
        ),
        "flag": _V2BETA1_WRITES_FLAG,
        "platform": "glp",
        "would_have_sent": payload,
    }


def _path_part(value: str) -> str:
    return quote(str(value), safe="")


def _params(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _safe_read_params(params: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    safe_params = dict(params or {})
    removed = [
        key
        for key in list(safe_params)
        if str(key).strip().lower() in _SENSITIVE_QUERY_PARAMS
    ]
    for key in removed:
        safe_params.pop(key, None)

    warnings = []
    if removed:
        warnings.append(
            "GLP unredacted responses are disabled; removed unredacted query parameter."
        )
    return safe_params, warnings


def _paged_params(limit: int | None = 100, offset: int | None = 0, **values: Any) -> dict[str, Any]:
    return {
        **_params(**values),
        "limit": clamp_limit(limit),
        "offset": max(0, offset or 0),
    }


_LIST_PAGINATION_KEYS = ("count", "offset", "total", "next")


def _list_result(page: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    """Shape a client ``*_page`` envelope into the tool's list response.

    Preserves the existing ``{"items", "errors"}`` contract and adds whichever
    of ``count``/``offset``/``total``/``next`` GLP returned so callers can page.
    """
    out: dict[str, Any] = {"items": page.get("items", []), "errors": errors}
    for key in _LIST_PAGINATION_KEYS:
        if key in page:
            out[key] = page[key]
    return out


def _cursor_params(
    limit: int | None = 100,
    next_cursor: str | None = None,
    **values: Any,
) -> dict[str, Any]:
    return {
        **_params(next=next_cursor, **values),
        "limit": clamp_limit(limit),
    }


def _glp_read(
    path: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    safe_params, warnings = _safe_read_params(params)
    try:
        safe_api_path(path, _GLP_GET_PREFIXES)
    except ValueError as exc:
        return {"data": None, "endpoint_used": path, "errors": [f"Invalid path. {exc}"]}
    try:
        client = get_glp_client()._client
        if headers:
            response = client._request("GET", path, params=safe_params, headers=headers)
            response.raise_for_status()
            data = response.json()
        else:
            data = client.get(path, params=safe_params)
        result = {"data": redact_sensitive(data), "endpoint_used": path, "errors": []}
        if warnings:
            result["warnings"] = warnings
        return result
    except Exception as exc:
        result = {"data": None, "endpoint_used": path, "errors": [str(exc)]}
        if warnings:
            result["warnings"] = warnings
        return result


def _glp_list_read(
    path: str,
    params: dict[str, Any],
    *,
    limit: int,
    list_key: str | None = "items",
) -> dict[str, Any]:
    result = _glp_read(path, params)
    if result.get("data") is not None:
        result["data"] = redact_sensitive(
            bound_collection_response(
                result["data"],
                limit=clamp_limit(limit),
                offset=0,
                list_key=list_key,
            )
        )
    return result


@mcp.tool(annotations=READ_ONLY)
def glp_get(
    path: str,
    params: dict[str, Any] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Perform a guarded read-only GET against selected GLP API families.

    Useful for exploring GLP service-catalog, workspaces, reporting, and
    adjacent read-only APIs before adding dedicated typed wrappers. Path must
    be relative and begin with one of the documented GLP API family prefixes.
    List payloads are bounded with `limit` and `offset`.
    """
    try:
        safe_path = safe_api_path(path, _GLP_GET_PREFIXES)
    except ValueError as exc:
        return {"error": f"Invalid path. {exc}"}
    safe_params, warnings = _safe_read_params(params)
    try:
        data = get_glp_client()._client.get(safe_path, params=safe_params)
        data = redact_sensitive(bound_collection_response(data, limit=limit, offset=offset))
        result = {"data": data, "endpoint_used": safe_path}
        if warnings:
            result["warnings"] = warnings
        return result
    except Exception as exc:
        result = {"error": str(exc), "endpoint_used": safe_path}
        if warnings:
            result["warnings"] = warnings
        return result


@mcp.tool(annotations=READ_ONLY)
def list_glp_devices(
    limit: int = 100,
    offset: int = 0,
    filter: str | None = None,
) -> dict[str, Any]:
    """List devices in the GLP workspace (warranty, subscription state, lifecycle).

    Args:
        limit: Maximum items to request; clamped to the MCP list limit.
        offset: Zero-based result offset for pagination.
        filter: OData filter, e.g. "serialNumber eq 'SG30LMR164'".
    """
    glp = get_glp_client()
    errors: list[str] = []
    try:
        page = glp.list_devices_page(
            limit=clamp_limit(limit), offset=max(0, offset), filter=filter
        )
        return _list_result(page, errors)
    except Exception as exc:
        errors.append(str(exc))
        return {"items": [], "errors": errors}


@mcp.tool(annotations=READ_ONLY)
def get_glp_device(serial_number: str) -> dict[str, Any]:
    """Fetch a single device from GLP by serial number."""
    glp = get_glp_client()
    errors: list[str] = []
    try:
        device = glp.get_device(serial_number)
        return {"device": device, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"device": None, "errors": errors}


@mcp.tool(annotations=READ_ONLY)
def get_glp_device_by_id(device_id: str) -> dict[str, Any]:
    """Fetch a GLP device by its official device resource ID."""
    return _glp_read(f"/devices/v1/devices/{_path_part(device_id)}")


@mcp.tool(annotations=READ_ONLY)
def list_glp_subscriptions(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """List subscriptions with `limit` / `offset` pagination."""
    glp = get_glp_client()
    errors: list[str] = []
    try:
        page = glp.list_subscriptions_page(limit=clamp_limit(limit), offset=max(0, offset))
        return _list_result(page, errors)
    except Exception as exc:
        errors.append(str(exc))
        return {"items": [], "errors": errors}


@mcp.tool(annotations=READ_ONLY)
def get_glp_subscription(subscription_id: str) -> dict[str, Any]:
    """Fetch a single GLP subscription by ID."""
    glp = get_glp_client()
    errors: list[str] = []
    try:
        sub = glp.get_subscription(subscription_id)
        return {"subscription": sub, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"subscription": None, "errors": errors}


@mcp.tool(annotations=READ_ONLY)
def list_glp_auto_subscription_settings() -> dict[str, Any]:
    """List all configured auto-subscription settings in the GLP workspace."""
    glp = get_glp_client()
    errors: list[str] = []
    try:
        items = glp.list_auto_subscription_settings()
        return {"items": items, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"items": [], "errors": errors}


@mcp.tool(annotations=READ_ONLY)
def get_glp_auto_subscription_setting(setting_id: str) -> dict[str, Any]:
    """Fetch one configured auto-subscription setting by ID."""
    glp = get_glp_client()
    errors: list[str] = []
    try:
        setting = glp.get_auto_subscription_setting(setting_id)
        return {"setting": setting, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"setting": None, "errors": errors}


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def update_glp_auto_subscription_settings(
    setting_id: str, settings: dict[str, Any]
) -> dict[str, Any]:
    """PATCH the configured auto-subscription settings for a workspace.

    Pass a list of deviceType/tier combinations to create or update in
    `settings`; per the spec, pass `tier` as null for a deviceType to
    remove its auto-subscription setting. The manifest's declared body
    property and required property don't agree with each other for this
    operation (not independently re-verified against the Subscriptions v1
    spec text) — `settings` is sent through as-is, so treat a 400/422 as
    "shape not confirmed on this tenant" and inspect
    list_glp_auto_subscription_settings / get_glp_auto_subscription_setting
    first. Gated behind the same guardrail as other GLP v2beta1-style
    writes — see glp_write_status.
    """
    disabled = _write_disabled(
        "update_glp_auto_subscription_settings",
        {"setting_id": setting_id, "settings": settings},
    )
    if disabled:
        return disabled
    glp = get_glp_client()
    errors: list[str] = []
    try:
        result = glp.update_auto_subscription_settings(setting_id, settings)
        return {"result": result, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"result": None, "errors": errors}


@mcp.tool(annotations=READ_ONLY)
def list_glp_users(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """List users with access to the GLP workspace using `limit` / `offset` pagination."""
    glp = get_glp_client()
    errors: list[str] = []
    try:
        page = glp.list_users_page(limit=clamp_limit(limit), offset=max(0, offset))
        return _list_result(page, errors)
    except Exception as exc:
        errors.append(str(exc))
        return {"items": [], "errors": errors}


@mcp.tool(annotations=READ_ONLY)
def get_glp_user(user_id: str) -> dict[str, Any]:
    """Fetch a single GLP identity user by ID."""
    return _glp_read(f"/identity/v1/users/{_path_part(user_id)}")




@mcp.tool(annotations=IDEMPOTENT_WRITE)
def invite_glp_user(email: str, send_welcome_email: bool | None = None) -> dict[str, Any]:
    """Invite a user to the GLP workspace by email.

    Gated behind the same guardrail as other GLP v2beta1-style writes —
    see glp_write_status.
    """
    disabled = _write_disabled(
        "invite_glp_user",
        {"email": redact_sensitive(email), "send_welcome_email": send_welcome_email},
    )
    if disabled:
        return disabled
    glp = get_glp_client()
    errors: list[str] = []
    try:
        result = glp.invite_user(email, send_welcome_email=send_welcome_email)
        return {"result": result, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"result": None, "errors": errors}


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def update_glp_user_preferences(
    user_id: str, idle_timeout: int, language: str
) -> dict[str, Any]:
    """Update a GLP user's preferences (idle timeout, language).

    This is a full PUT replace of the user's preferences — both
    `idle_timeout` and `language` are required. Gated behind the same
    guardrail as other GLP v2beta1-style writes — see glp_write_status.
    """
    disabled = _write_disabled(
        "update_glp_user_preferences",
        {"user_id": user_id, "idle_timeout": idle_timeout, "language": language},
    )
    if disabled:
        return disabled
    glp = get_glp_client()
    errors: list[str] = []
    try:
        result = glp.update_user_preferences(user_id, idle_timeout, language)
        return {"result": result, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"result": None, "errors": errors}


@mcp.tool(annotations=DESTRUCTIVE)
def disassociate_glp_user(user_id: str) -> dict[str, Any]:
    """Remove (disassociate) a user from the GLP workspace.

    Gated behind the same guardrail as other GLP v2beta1-style writes —
    see glp_write_status.
    """
    disabled = _write_disabled("disassociate_glp_user", {"user_id": user_id})
    if disabled:
        return disabled
    glp = get_glp_client()
    errors: list[str] = []
    try:
        result = glp.disassociate_user(user_id)
        return {"result": result, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"result": None, "errors": errors}
@mcp.tool(annotations=READ_ONLY)
def list_glp_audit_logs(
    limit: int = 100,
    offset: int = 0,
    category: str | None = None,
    filter: str | None = None,
    select: str | None = None,
    sort: str | None = None,
) -> dict[str, Any]:
    """List GLP audit log entries (who did what and when).

    Calls the only audit-log list operation in the committed GLP manifest,
    GET /audit-log/v2beta1/logs (getAuditLogs).

    Args:
        limit: Maximum entries to request; clamped to the MCP list limit.
        offset: Zero-based result offset for pagination.
        category: Convenience shorthand — translated into the documented
            filter expression ``category eq '<value>'`` (e.g. "User Management",
            "Device Management"); it is not a standalone query parameter.
        filter: Raw OData filter, ANDed with ``category`` when both are given.
            Supported fields include createdAt, category, description,
            ipAddress, username, workspace/name, workspace/type,
            serviceOffer/id, region and hasDetails.
        select: Comma-separated properties to return.
        sort: Sort expression, e.g. "createdAt desc".
    """
    glp = get_glp_client()
    errors: list[str] = []
    try:
        page = glp.list_audit_logs_page(
            limit=clamp_limit(limit),
            offset=max(0, offset),
            category=category,
            filter=filter,
            select=select,
            sort=sort,
        )
        return _list_result(page, errors)
    except Exception as exc:
        errors.append(str(exc))
        return {"items": [], "errors": errors}


@mcp.tool(annotations=READ_ONLY)
def get_glp_audit_log_detail(audit_log_id: str) -> dict[str, Any]:
    """Fetch official GLP audit-log details for entries with details enabled.

    Uses GET /audit-log/v2beta1/logs/{id}/details (getAuditLogDetails) — the
    only audit-log detail operation in the committed manifest. The previous
    singular ``/audit-log/v1/logs/{id}/detail`` spelling exists in neither the
    manifest nor the service.
    """
    return _glp_read(f"/audit-log/v2beta1/logs/{_path_part(audit_log_id)}/details")


@mcp.tool(annotations=READ_ONLY)
def get_glp_workspace(workspace_id: str) -> dict[str, Any]:
    """Fetch basic GreenLake workspace information by workspace ID."""
    return _glp_read(f"/workspaces/v1/workspaces/{_path_part(workspace_id)}")


@mcp.tool(annotations=READ_ONLY)
def get_glp_workspace_contact(workspace_id: str) -> dict[str, Any]:
    """Fetch detailed GreenLake workspace contact information."""
    return _glp_read(f"/workspaces/v1/workspaces/{_path_part(workspace_id)}/contact")


@mcp.tool(annotations=READ_ONLY)
def list_glp_reporting_statuses(
    filter: str | None = None,
    sort: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List GreenLake reporting status records with bounded pagination."""
    return _glp_read(
        "/reporting/v1/statuses",
        _paged_params(limit, offset, filter=filter, sort=sort),
    )


@mcp.tool(annotations=READ_ONLY)
def get_glp_reporting_status(status_id: str) -> dict[str, Any]:
    """Fetch a single GreenLake reporting status record by ID."""
    return _glp_read(f"/reporting/v1/statuses/{_path_part(status_id)}")


@mcp.tool(annotations=READ_ONLY)
def list_glp_service_offers(
    next_cursor: str | None = None,
    limit: int = 100,
    filter: str | None = None,
) -> dict[str, Any]:
    """List GreenLake service-catalog offers with cursor pagination."""
    return _glp_read(
        "/service-catalog/v1beta1/service-offers",
        _cursor_params(limit, next_cursor, filter=filter),
    )


@mcp.tool(annotations=READ_ONLY)
def get_glp_service_offer(offer_id: str) -> dict[str, Any]:
    """Fetch a GreenLake service-catalog offer by ID."""
    return _glp_read(f"/service-catalog/v1beta1/service-offers/{_path_part(offer_id)}")


@mcp.tool(annotations=READ_ONLY)
def list_glp_service_offer_regions(
    next_cursor: str | None = None,
    limit: int = 100,
    filter: str | None = None,
) -> dict[str, Any]:
    """List GreenLake service-offer regions with cursor pagination."""
    return _glp_read(
        "/service-catalog/v1beta1/service-offer-regions",
        _cursor_params(limit, next_cursor, filter=filter),
    )


@mcp.tool(annotations=READ_ONLY)
def get_glp_service_offer_region(region_id: str) -> dict[str, Any]:
    """Fetch a GreenLake service-offer region by ID."""
    return _glp_read(f"/service-catalog/v1beta1/service-offer-regions/{_path_part(region_id)}")


@mcp.tool(annotations=READ_ONLY)
def list_glp_service_provisions(
    workspace_id: str | None = None,
    next_cursor: str | None = None,
    limit: int = 100,
    filter: str | None = None,
    all_workspaces: bool | None = None,
) -> dict[str, Any]:
    """List GreenLake service provisions, optionally scoped by workspace ID."""
    headers = {"Hpe-workspace-id": workspace_id} if workspace_id else None
    return _glp_read(
        "/service-catalog/v1beta1/service-provisions",
        _cursor_params(
            limit,
            next_cursor,
            filter=filter,
            all=all_workspaces,
        ),
        headers=headers,
    )


@mcp.tool(annotations=READ_ONLY)
def get_glp_service_provision(
    provision_id: str,
) -> dict[str, Any]:
    """Fetch a GreenLake service provision by ID."""
    return _glp_read(
        f"/service-catalog/v1beta1/service-provisions/{_path_part(provision_id)}",
    )


@mcp.tool(annotations=READ_ONLY)
def list_glp_service_managers(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """List GreenLake service managers."""
    return _glp_read(
        "/service-catalog/v1/service-managers",
        _paged_params(limit, offset),
    )


@mcp.tool(annotations=READ_ONLY)
def get_glp_service_manager(manager_id: str) -> dict[str, Any]:
    """Fetch a GreenLake service manager by ID."""
    return _glp_read(f"/service-catalog/v1/service-managers/{_path_part(manager_id)}")


@mcp.tool(annotations=READ_ONLY)
def list_glp_service_manager_provisions(
    limit: int = 100,
    offset: int = 0,
    filter: str | None = None,
) -> dict[str, Any]:
    """List GreenLake service-manager provisions."""
    return _glp_read(
        "/service-catalog/v1/service-manager-provisions",
        _paged_params(limit, offset, filter=filter),
    )


@mcp.tool(annotations=READ_ONLY)
def get_glp_service_manager_provision(provision_id: str) -> dict[str, Any]:
    """Fetch a GreenLake service-manager provision by ID."""
    return _glp_read(f"/service-catalog/v1/service-manager-provisions/{_path_part(provision_id)}")


@mcp.tool(annotations=READ_ONLY)
def list_glp_per_region_service_managers(
    limit: int = 100,
    offset: int = 0,
    filter: str | None = None,
) -> dict[str, Any]:
    """List GreenLake per-region service-manager mappings."""
    return _glp_read(
        "/service-catalog/v1/per-region-service-managers",
        _paged_params(limit, offset, filter=filter),
    )


@mcp.tool(annotations=READ_ONLY)
def get_glp_service_managers_for_region(region_id: str) -> dict[str, Any]:
    """Fetch GreenLake service managers available for a region mapping ID."""
    return _glp_read(f"/service-catalog/v1/per-region-service-managers/{_path_part(region_id)}")


# ── Authorization / RBAC v1beta1 ─────────────────────────────────────────────

@mcp.tool(annotations=READ_ONLY)
def list_glp_role_assignments(
    limit: int = 100,
    offset: int = 0,
    filter: str | None = None,
) -> dict[str, Any]:
    """List RBAC role assignments with bounded offset pagination.

    The documented OData subset supports `in` and `and` on role, scope, and
    principal, with each attribute appearing at most once.
    """
    bounded_limit = clamp_limit(limit)
    return _glp_list_read(
        "/authorization/v1beta1/role-assignments",
        _paged_params(bounded_limit, offset, filter=filter),
        limit=bounded_limit,
    )


@mcp.tool(annotations=READ_ONLY)
def get_glp_role_assignment(role_assignment_id: str) -> dict[str, Any]:
    """Fetch one RBAC role assignment by ID."""
    return _glp_read(
        f"/authorization/v1beta1/role-assignments/{_path_part(role_assignment_id)}"
    )


@mcp.tool(annotations=READ_ONLY)
def list_glp_scope_groups(
    limit: int = 100,
    offset: int = 0,
    filter: str | None = None,
    sort: str | None = None,
) -> dict[str, Any]:
    """List RBAC scope groups with bounded offset pagination."""
    bounded_limit = clamp_limit(limit)
    return _glp_list_read(
        "/authorization/v1beta1/scope-groups",
        _paged_params(bounded_limit, offset, filter=filter, sort=sort),
        limit=bounded_limit,
    )


@mcp.tool(annotations=READ_ONLY)
def get_glp_scope_group(scope_group_id: str) -> dict[str, Any]:
    """Fetch one RBAC scope group by ID."""
    return _glp_read(
        f"/authorization/v1beta1/scope-groups/{_path_part(scope_group_id)}"
    )


@mcp.tool(annotations=READ_ONLY)
def list_glp_scope_group_scopes(
    scope_group_id: str,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """List scopes assigned to an RBAC scope group."""
    bounded_limit = clamp_limit(limit)
    return _glp_list_read(
        f"/authorization/v1beta1/scope-groups/{_path_part(scope_group_id)}/scopes",
        _paged_params(bounded_limit, offset),
        limit=bounded_limit,
    )



@mcp.tool(annotations=IDEMPOTENT_WRITE)
def create_glp_role_assignment(role_assignment: dict[str, Any]) -> dict[str, Any]:
    """Create an RBAC role assignment.

    `role_assignment` is passed through as-is; per the spec it must include
    `principal`, `role`, and `scope` (see get_glp_role_assignment /
    list_glp_role_assignments for the shape returned by this same API, and
    the GLP authorization developer guide for how to find those
    identifiers). Gated behind the same guardrail as other GLP v2beta1-style
    writes — see glp_write_status.
    """
    disabled = _write_disabled(
        "create_glp_role_assignment", {"role_assignment": role_assignment}
    )
    if disabled:
        return disabled
    glp = get_glp_client()
    errors: list[str] = []
    try:
        result = glp.create_role_assignment(role_assignment)
        return {"result": result, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"result": None, "errors": errors}


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def update_glp_role_assignment(
    role_assignment_id: str, role_assignment: dict[str, Any]
) -> dict[str, Any]:
    """Update the scope(s) of an existing RBAC role assignment by ID.

    Per the spec, `role_assignment` must still include the immutable `id`,
    `principal`, and `role` attributes alongside the updated `scope`. Gated
    behind the same guardrail as other GLP v2beta1-style writes — see
    glp_write_status.
    """
    disabled = _write_disabled(
        "update_glp_role_assignment",
        {"role_assignment_id": role_assignment_id, "role_assignment": role_assignment},
    )
    if disabled:
        return disabled
    glp = get_glp_client()
    errors: list[str] = []
    try:
        result = glp.update_role_assignment(role_assignment_id, role_assignment)
        return {"result": result, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"result": None, "errors": errors}


@mcp.tool(annotations=DESTRUCTIVE)
def delete_glp_role_assignment(role_assignment_id: str) -> dict[str, Any]:
    """Delete an RBAC role assignment by ID.

    Gated behind the same guardrail as other GLP v2beta1-style writes —
    see glp_write_status.
    """
    disabled = _write_disabled(
        "delete_glp_role_assignment", {"role_assignment_id": role_assignment_id}
    )
    if disabled:
        return disabled
    glp = get_glp_client()
    errors: list[str] = []
    try:
        result = glp.delete_role_assignment(role_assignment_id)
        return {"result": result, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"result": None, "errors": errors}


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def create_glp_scope_group(scope_group: dict[str, Any]) -> dict[str, Any]:
    """Create an RBAC scope group (a named collection of scopes for role assignments).

    `scope_group` is passed through as-is; per the spec it must include
    `name`, and a scope group cannot contain another scope group (no
    nesting). Gated behind the same guardrail as other GLP v2beta1-style
    writes — see glp_write_status.
    """
    disabled = _write_disabled("create_glp_scope_group", {"scope_group": scope_group})
    if disabled:
        return disabled
    glp = get_glp_client()
    errors: list[str] = []
    try:
        result = glp.create_scope_group(scope_group)
        return {"result": result, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"result": None, "errors": errors}


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def update_glp_scope_group(
    scope_group_id: str, scope_group: dict[str, Any]
) -> dict[str, Any]:
    """Update an RBAC scope group by ID.

    Per the spec, `scope_group` must still include the immutable `id`
    attribute. Gated behind the same guardrail as other GLP v2beta1-style
    writes — see glp_write_status.
    """
    disabled = _write_disabled(
        "update_glp_scope_group",
        {"scope_group_id": scope_group_id, "scope_group": scope_group},
    )
    if disabled:
        return disabled
    glp = get_glp_client()
    errors: list[str] = []
    try:
        result = glp.update_scope_group(scope_group_id, scope_group)
        return {"result": result, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"result": None, "errors": errors}


@mcp.tool(annotations=DESTRUCTIVE)
def delete_glp_scope_group(scope_group_id: str) -> dict[str, Any]:
    """Delete an RBAC scope group by ID.

    Gated behind the same guardrail as other GLP v2beta1-style writes —
    see glp_write_status.
    """
    disabled = _write_disabled(
        "delete_glp_scope_group", {"scope_group_id": scope_group_id}
    )
    if disabled:
        return disabled
    glp = get_glp_client()
    errors: list[str] = []
    try:
        result = glp.delete_scope_group(scope_group_id)
        return {"result": result, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"result": None, "errors": errors}


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def add_glp_scope_group_scopes(
    scope_group_id: str, items: list[dict[str, Any]]
) -> dict[str, Any]:
    """Add scopes to an existing RBAC scope group.

    `items` is required by the spec. This operation is synchronous and
    non-atomic per the spec. Gated behind the same guardrail as other GLP
    v2beta1-style writes — see glp_write_status.
    """
    disabled = _write_disabled(
        "add_glp_scope_group_scopes",
        {"scope_group_id": scope_group_id, "items": items},
    )
    if disabled:
        return disabled
    glp = get_glp_client()
    errors: list[str] = []
    try:
        result = glp.add_scope_group_scopes(scope_group_id, items)
        return {"result": result, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"result": None, "errors": errors}


@mcp.tool(annotations=DESTRUCTIVE)
def delete_glp_scope_group_scopes(
    scope_group_id: str, items: list[dict[str, Any]]
) -> dict[str, Any]:
    """Delete scopes from an existing RBAC scope group.

    `items` is required by the spec (the scope IDs to remove — see
    list_glp_scope_group_scopes to find them). This operation is
    synchronous and non-atomic per the spec. Gated behind the same
    guardrail as other GLP v2beta1-style writes — see glp_write_status.
    """
    disabled = _write_disabled(
        "delete_glp_scope_group_scopes",
        {"scope_group_id": scope_group_id, "items": items},
    )
    if disabled:
        return disabled
    glp = get_glp_client()
    errors: list[str] = []
    try:
        result = glp.delete_scope_group_scopes(scope_group_id, items)
        return {"result": result, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"result": None, "errors": errors}

# ── Event webhooks v1beta1 ───────────────────────────────────────────────────

@mcp.tool(annotations=READ_ONLY)
def list_glp_event_webhooks(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """List workspace event webhooks, newest first."""
    bounded_limit = clamp_limit(limit)
    return _glp_list_read(
        "/events/v1beta1/webhooks",
        _paged_params(bounded_limit, offset),
        limit=bounded_limit,
    )


@mcp.tool(annotations=READ_ONLY)
def get_glp_event_webhook(webhook_id: str) -> dict[str, Any]:
    """Fetch one workspace event webhook by ID."""
    return _glp_read(f"/events/v1beta1/webhooks/{_path_part(webhook_id)}")


@mcp.tool(annotations=READ_ONLY)
def list_glp_event_subscriptions(
    filter: str,
    limit: int = 10,
    offset: int = 0,
) -> dict[str, Any]:
    """List event subscriptions for a webhook.

    `filter` is required by the GLP v1beta1 operation; the documented
    supported filter field is `webhookId`.
    """
    bounded_limit = clamp_limit(limit)
    return _glp_list_read(
        "/events/v1beta1/subscriptions",
        _paged_params(bounded_limit, offset, filter=filter),
        limit=bounded_limit,
    )


@mcp.tool(annotations=READ_ONLY)
def list_glp_webhook_deliveries(
    webhook_id: str,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """List recent delivery attempts for a workspace event webhook."""
    bounded_limit = clamp_limit(limit)
    return _glp_list_read(
        f"/events/v1beta1/webhooks/{_path_part(webhook_id)}/recent-deliveries",
        _paged_params(bounded_limit, offset),
        limit=bounded_limit,
    )


# ── Location management v1 ───────────────────────────────────────────────────

@mcp.tool(annotations=READ_ONLY)
def list_glp_locations(
    limit: int = 100,
    offset: int = 0,
    filter: str | None = None,
) -> dict[str, Any]:
    """List workspace locations; the documented filter supports location name."""
    bounded_limit = clamp_limit(limit)
    return _glp_list_read(
        "/locations/v1/locations",
        _paged_params(bounded_limit, offset, filter=filter),
        limit=bounded_limit,
    )


@mcp.tool(annotations=READ_ONLY)
def get_glp_location(location_id: str) -> dict[str, Any]:
    """Fetch one workspace location by ID."""
    return _glp_read(f"/locations/v1/locations/{_path_part(location_id)}")


@mcp.tool(annotations=READ_ONLY)
def reverse_geocode_glp_location(
    latitude: float,
    longitude: float,
    language: str | None = None,
) -> dict[str, Any]:
    """Resolve latitude/longitude to a location, optionally using an ISO language code."""
    return _glp_read(
        "/locations/v1/locations/address/revgeocode",
        _params(latitude=latitude, longitude=longitude, language=language),
    )


@mcp.tool(annotations=READ_ONLY)
def list_glp_location_tags(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """List location-management tags for the workspace."""
    bounded_limit = clamp_limit(limit)
    return _glp_list_read(
        "/locations/v1/locations/tags",
        _paged_params(bounded_limit, offset),
        limit=bounded_limit,
    )


@mcp.tool(annotations=READ_ONLY)
def get_glp_location_tags(location_id: str) -> dict[str, Any]:
    """Fetch location-management tags assigned to one location."""
    return _glp_read(f"/locations/v1/locations/tags/{_path_part(location_id)}")


# ── Workspace tags v1 ────────────────────────────────────────────────────────

@mcp.tool(annotations=READ_ONLY)
def list_glp_tags(
    limit: int = 100,
    offset: int = 0,
    filter: str | None = None,
    sort: str | None = None,
    select: list[str] | None = None,
) -> dict[str, Any]:
    """List workspace tags with filter, sort, projection, and bounded pagination."""
    bounded_limit = clamp_limit(limit)
    return _glp_list_read(
        "/tags/v1/tags",
        _paged_params(
            bounded_limit,
            offset,
            filter=filter,
            sort=sort,
            select=select,
        ),
        limit=bounded_limit,
    )


@mcp.tool(annotations=READ_ONLY)
def list_glp_tag_resources(
    limit: int = 100,
    offset: int = 0,
    filter: str | None = None,
    filter_tags: str | None = None,
    sort: str | None = None,
    select: list[str] | None = None,
) -> dict[str, Any]:
    """List tagged workspace resources with bounded pagination."""
    bounded_limit = clamp_limit(limit)
    return _glp_list_read(
        "/tags/v1/tag-resources",
        _paged_params(
            bounded_limit,
            offset,
            filter=filter,
            sort=sort,
            select=select,
            **{"filter-tags": filter_tags},
        ),
        limit=bounded_limit,
    )


# ── Identity SCIM v2beta1 ────────────────────────────────────────────────────

@mcp.tool(annotations=READ_ONLY)
def list_glp_scim_users(
    filter: str | None = None,
    count: int = 100,
    start_index: int = 1,
    sort_by: Literal["displayName", "meta.lastLogin"] | None = None,
    sort_order: Literal["ascending", "descending"] | None = None,
) -> dict[str, Any]:
    """List SCIM users with 1-based pagination.

    Supported user filters are displayName/userName with sw, eq, or co.
    sort_by supports displayName or meta.lastLogin; sort_order supports
    ascending or descending.
    """
    bounded_count = clamp_limit(count)
    return _glp_list_read(
        "/identity/v2beta1/scim/v2/Users",
        _params(
            filter=filter,
            count=bounded_count,
            startIndex=max(1, start_index),
            sortBy=sort_by,
            sortOrder=sort_order,
        ),
        limit=bounded_count,
        list_key="Resources",
    )


@mcp.tool(annotations=READ_ONLY)
def get_glp_scim_user(user_id: str) -> dict[str, Any]:
    """Fetch one SCIM user by ID."""
    return _glp_read(f"/identity/v2beta1/scim/v2/Users/{_path_part(user_id)}")


@mcp.tool(annotations=READ_ONLY)
def list_glp_scim_groups(
    filter: str | None = None,
    count: int = 100,
    start_index: int = 1,
) -> dict[str, Any]:
    """List SCIM user groups with 1-based pagination."""
    bounded_count = clamp_limit(count)
    return _glp_list_read(
        "/identity/v2beta1/scim/v2/Groups",
        _params(
            filter=filter,
            count=bounded_count,
            startIndex=max(1, start_index),
        ),
        limit=bounded_count,
        list_key="Resources",
    )


@mcp.tool(annotations=READ_ONLY)
def get_glp_scim_group(group_id: str) -> dict[str, Any]:
    """Fetch one SCIM user group by ID."""
    return _glp_read(f"/identity/v2beta1/scim/v2/Groups/{_path_part(group_id)}")


@mcp.tool(annotations=READ_ONLY)
def list_glp_scim_group_users(
    group_id: str,
    count: int = 100,
    start_index: int = 1,
) -> dict[str, Any]:
    """List SCIM users assigned to a user group."""
    bounded_count = clamp_limit(count)
    return _glp_list_read(
        f"/identity/v2beta1/scim/v2/extensions/Groups/{_path_part(group_id)}/users",
        _params(count=bounded_count, startIndex=max(1, start_index)),
        limit=bounded_count,
        list_key="Resources",
    )


@mcp.tool(annotations=READ_ONLY)
def list_glp_scim_user_groups(
    user_id: str,
    count: int = 100,
    start_index: int = 1,
) -> dict[str, Any]:
    """List SCIM groups assigned to a user."""
    bounded_count = clamp_limit(count)
    return _glp_list_read(
        f"/identity/v2beta1/scim/v2/extensions/Users/{_path_part(user_id)}/groups",
        _params(count=bounded_count, startIndex=max(1, start_index)),
        limit=bounded_count,
        list_key="Resources",
    )


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def glp_assign_subscription(
    serial_number: str,
    subscription_key: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Assign a GLP subscription (license) to a device.

    subscription_key accepts either a subscription key string or its GLP UUID;
    a key is resolved to its UUID internally before assignment.

    dry_run=True sends the manifest-declared ``dry-run`` query parameter on
    PATCH /devices/v2beta1/devices (patchDevicesV2beta1): GLP validates the
    request and returns as if it had completed, without changing anything.
    Still requires the GLP write flag, because the validation request does
    reach the tenant.
    """
    disabled = _write_disabled(
        "glp_assign_subscription",
        {
            "serial_number": serial_number,
            "subscription_key": subscription_key,
            "dry_run": dry_run,
        },
    )
    if disabled:
        return disabled
    glp = get_glp_client()
    errors: list[str] = []
    try:
        result = glp.assign_subscription(
            serial_number, subscription_key, dry_run=dry_run
        )
        return {"result": result, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"result": None, "errors": errors}


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def glp_add_device(serial_number: str, mac_address: str | None = None) -> dict[str, Any]:
    """Add a device to the GLP workspace (async task, polls until complete, ~5min max)."""
    disabled = _write_disabled(
        "glp_add_device",
        {"serial_number": serial_number, "mac_address": mac_address},
    )
    if disabled:
        return disabled
    glp = get_glp_client()
    errors: list[str] = []
    try:
        task_id = glp.add_device(serial_number, mac_address=mac_address)
        task_result = glp.poll_task(task_id)
        return {"task_id": task_id, "task_result": task_result, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"task_id": None, "task_result": None, "errors": errors}


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def glp_add_devices_bulk(devices: list[dict[str, str]]) -> dict[str, Any]:
    """Bulk add devices to GLP. devices: dicts with 'serialNumber' and 'macAddress'.

    Returns task_id + task_result (successfulDevicesSerial / failedDevicesSerial).
    """
    disabled = _write_disabled("glp_add_devices_bulk", {"devices": devices})
    if disabled:
        return disabled
    glp = get_glp_client()
    errors: list[str] = []
    try:
        task_id = glp.add_devices(devices)
        task_result = glp.poll_task(task_id)
        return {"task_id": task_id, "task_result": task_result, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"task_id": None, "task_result": None, "errors": errors}


@mcp.tool(annotations=DESTRUCTIVE)
def glp_archive_device(serial_number: str, dry_run: bool = False) -> dict[str, Any]:
    """Archive a device in GLP (removes from Central, keeps in GLP inventory).

    dry_run=True sends the manifest-declared ``dry-run`` query parameter on
    PATCH /devices/v2beta1/devices (patchDevicesV2beta1) so GLP validates the
    archive without performing it. Still requires the GLP write flag — the
    validation request reaches the tenant.
    """
    disabled = _write_disabled(
        "glp_archive_device",
        {"serial_number": serial_number, "dry_run": dry_run},
    )
    if disabled:
        return disabled
    glp = get_glp_client()
    errors: list[str] = []
    try:
        result = glp.archive_device(serial_number, dry_run=dry_run)
        return {"result": result, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"result": None, "errors": errors}


# ── Devices v2beta1 / Device Groups v2beta1 ──────────────────────────────────

@mcp.tool(annotations=READ_ONLY)
def list_glp_devices_v2(
    limit: int = 100,
    offset: int = 0,
    filter: str | None = None,
) -> dict[str, Any]:
    """List devices via the GLP Devices v2beta1 collection.

    Prefer this over list_glp_devices when you need v2beta1-only fields
    (e.g. the fields exposed by the v2beta1 PATCH path used for archive /
    subscription-assign). Falls back with a clear error if v2beta1 isn't
    available on this tenant yet — use list_glp_devices (v1) instead.
    """
    glp = get_glp_client()
    errors: list[str] = []
    try:
        items = glp.list_devices_v2beta1(
            limit=clamp_limit(limit), offset=max(0, offset), filter=filter
        )
        return {"items": items, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"items": [], "errors": errors}


@mcp.tool(annotations=READ_ONLY)
def get_glp_device_v2(device_id: str) -> dict[str, Any]:
    """Fetch a single device via the GLP Devices v2beta1 collection by GLP device ID."""
    glp = get_glp_client()
    errors: list[str] = []
    try:
        device = glp.get_device_v2beta1(device_id)
        return {"device": device, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"device": None, "errors": errors}


@mcp.tool(annotations=READ_ONLY)
def group_glp_devices(
    group_by: str,
    limit: int = 100,
    offset: int = 0,
    filter: str | None = None,
) -> dict[str, Any]:
    """Group GLP devices by a documented v2beta1 attribute."""
    glp = get_glp_client()
    errors: list[str] = []
    try:
        items = glp.group_devices_v2beta1(
            group_by=group_by,
            limit=clamp_limit(limit),
            offset=max(0, offset),
            filter=filter,
        )
        return {"items": items, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"items": [], "errors": errors}


# ── Audit Logs v2beta1 ────────────────────────────────────────────────────────

@mcp.tool(annotations=READ_ONLY)
def list_glp_audit_logs_v2(
    limit: int = 100,
    offset: int = 0,
    category: str | None = None,
    filter: str | None = None,
    select: str | None = None,
    sort: str | None = None,
) -> dict[str, Any]:
    """List GLP audit log entries via the v2beta1 Audit Log service.

    Same manifest operation as list_glp_audit_logs (getAuditLogs); kept for
    callers that pinned the explicit-version name. ``category`` is translated
    into the documented ``category eq '<value>'`` filter expression.
    """
    glp = get_glp_client()
    errors: list[str] = []
    try:
        items = glp.list_audit_logs_v2beta1(
            limit=clamp_limit(limit),
            offset=max(0, offset),
            category=category,
            filter=filter,
            select=select,
            sort=sort,
        )
        return {"items": items, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"items": [], "errors": errors}


@mcp.tool(annotations=READ_ONLY)
def get_glp_audit_log_v2(audit_log_id: str) -> dict[str, Any]:
    """Fetch a single GLP audit-log entry by ID via the v2beta1 Audit Log service."""
    glp = get_glp_client()
    errors: list[str] = []
    try:
        entry = glp.get_audit_log_v2beta1(audit_log_id)
        return {"audit_log": entry, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"audit_log": None, "errors": errors}


@mcp.tool(annotations=READ_ONLY)
def get_glp_audit_log_v2_detail(audit_log_id: str) -> dict[str, Any]:
    """Fetch full detail for a v2beta1 GLP audit-log entry (entries with details enabled)."""
    glp = get_glp_client()
    errors: list[str] = []
    try:
        detail = glp.get_audit_log_v2beta1_detail(audit_log_id)
        return {"audit_log_detail": detail, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"audit_log_detail": None, "errors": errors}


# ── Workspace contact/location PATCH, subscription bulk-add ─────────────────

@mcp.tool(annotations=IDEMPOTENT_WRITE)
def update_glp_workspace_contact(workspace_id: str, contact: dict[str, Any]) -> dict[str, Any]:
    """PATCH the contact record for a GLP workspace.

    Endpoint mirrors the confirmed-working GET at the same path
    (get_glp_workspace_contact). Gated behind the same guardrail as the
    device v2beta1 writes — see glp_write_status.
    """
    disabled = _write_disabled(
        "update_glp_workspace_contact",
        {"workspace_id": workspace_id, "contact": redact_sensitive(contact)},
    )
    if disabled:
        return disabled
    glp = get_glp_client()
    errors: list[str] = []
    try:
        result = glp.update_workspace_contact(workspace_id, contact)
        return {"result": result, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"result": None, "errors": errors}


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def glp_add_subscriptions(subscription_keys: list[str], dry_run: bool = False) -> dict[str, Any]:
    """Add one or more subscription keys to the GLP workspace, with an optional dry-run preview.

    dry_run=True sends the request with the manifest-confirmed ``dry-run``
    query parameter (postSubscriptionsV1; validation only — no subscriptions
    are actually added), rather than a purely local no-op. The nested
    subscription-item body shape has not been independently re-verified
    against live spec text — treat a 400/404 here as "not confirmed on this
    tenant" and fall back to glp_get for exploration. Gated behind the same
    guardrail as other GLP v2beta1-style writes — see glp_write_status.
    """
    disabled = _write_disabled(
        "glp_add_subscriptions",
        {"subscription_keys": subscription_keys, "dry_run": dry_run},
    )
    if disabled:
        return disabled
    glp = get_glp_client()
    errors: list[str] = []
    try:
        result = glp.add_subscriptions(subscription_keys, dry_run=dry_run)
        return {"result": result, "errors": errors}
    except Exception as exc:
        errors.append(str(exc))
        return {"result": None, "errors": errors}


# ── Compute, Storage, Virtualization, Backup & Data Services (curated,
#    region-aware) ────────────────────────────────────────────────────────
#
# These families are declared in the committed manifest
# (src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/glp.json: compute-ops-mgmt.json,
# storage-fleet.json, block-storage.json, virtualization.json,
# backup-recovery.json, data-services.json) but -- unlike
# devices/subscriptions/identity/authorization/workspaces/reporting above --
# they are served from region-specific hosts, never
# global.api.greenlake.hpe.com. Every tool below requires
# GLP_GENERATED_REGION to be set to a region valid for that family (see
# _GLP_FAMILY_HOSTS); an unset/invalid region surfaces as a clear entry in
# `errors` rather than silently hitting the wrong host. glp_get cannot reach
# these paths (its _GLP_GET_PREFIXES intentionally excludes them for the
# same reason) -- use these dedicated tools instead.
#
# All are read-only except set_glp_virtual_machine_power (+ its bulk
# composite) and run_glp_backup_protection_job, gated behind the same
# HPE_MCP_GLP_V2BETA1_WRITES flag as the other guarded GLP writes (see
# glp_write_status).

_GLP_REGIONAL_API_HOSTS: dict[str, str] = {
    "us-west": "https://us-west.api.greenlake.hpe.com",
    "eu-west": "https://eu-west.api.greenlake.hpe.com",
    "eu-central": "https://eu-central.api.greenlake.hpe.com",
    "ap-northeast": "https://ap-northeast.api.greenlake.hpe.com",
}
_GLP_REGIONAL_DATA_HOSTS: dict[str, str] = {
    "us-west": "https://us1.data.cloud.hpe.com",
    "us1": "https://us1.data.cloud.hpe.com",
    "eu-west": "https://eu1.data.cloud.hpe.com",
    "eu-central": "https://eu1.data.cloud.hpe.com",
    "eu1": "https://eu1.data.cloud.hpe.com",
    "ap-northeast": "https://jp1.data.cloud.hpe.com",
    "jp1": "https://jp1.data.cloud.hpe.com",
}

# Manifest-declared server_urls per source_file (every operation within one
# source_file shares the same host tuple in the committed manifest --
# verified in tests/unit/test_glp_v07_depth.py against the manifest itself
# so upstream host-list drift fails loudly instead of silently mis-routing).
_GLP_FAMILY_HOSTS: dict[str, tuple[str, ...]] = {
    "compute-ops-mgmt": (
        "https://us-west.api.greenlake.hpe.com",
        "https://eu-central.api.greenlake.hpe.com",
        "https://ap-northeast.api.greenlake.hpe.com",
    ),
    "storage-fleet": (
        "https://eu1.data.cloud.hpe.com",
        "https://us1.data.cloud.hpe.com",
        "https://jp1.data.cloud.hpe.com",
    ),
    "block-storage": (
        "https://eu1.data.cloud.hpe.com",
        "https://us1.data.cloud.hpe.com",
        "https://jp1.data.cloud.hpe.com",
    ),
    "virtualization": (
        "https://us-west.api.greenlake.hpe.com",
        "https://eu-west.api.greenlake.hpe.com",
        "https://eu-central.api.greenlake.hpe.com",
        "https://ap-northeast.api.greenlake.hpe.com",
    ),
    "backup-recovery": (
        "https://us-west.api.greenlake.hpe.com",
        "https://eu-west.api.greenlake.hpe.com",
        "https://eu-central.api.greenlake.hpe.com",
        "https://ap-northeast.api.greenlake.hpe.com",
    ),
    "data-services": (
        "https://us-west.api.greenlake.hpe.com",
        "https://eu-west.api.greenlake.hpe.com",
        "https://eu-central.api.greenlake.hpe.com",
        "https://ap-northeast.api.greenlake.hpe.com",
    ),
}
_GLP_FAMILY_PREFIXES: dict[str, tuple[str, ...]] = {
    family: (f"/{family}/",) for family in _GLP_FAMILY_HOSTS
}


def _glp_family_server(family: str) -> str:
    """Resolve the region-specific host for a curated GLP service family.

    Raises ValueError (surfaced in the tool's `errors` list, never raised
    through to the MCP transport) when GLP_GENERATED_REGION is unset or not
    valid for this family.
    """
    hosts = _GLP_FAMILY_HOSTS[family]
    region = os.environ.get("GLP_GENERATED_REGION", "").strip().lower()
    lookup = (
        _GLP_REGIONAL_DATA_HOSTS
        if any(".data.cloud.hpe.com" in host for host in hosts)
        else _GLP_REGIONAL_API_HOSTS
    )
    requested = lookup.get(region)
    if requested in hosts:
        return requested
    valid_regions = sorted({key for key, url in lookup.items() if url in hosts})
    raise ValueError(
        f"GLP {family} operations are region-specific. Set GLP_GENERATED_REGION "
        f"to one of {valid_regions}."
    )


def _glp_family_resolver(family: str):
    """Build an auth resolver bound to one curated region-aware GLP family.

    Reuses the target-account GLPClient's token manager (its workspace-scoped
    bearer token), fixing the base URL to _glp_family_server(family) rather
    than the opt-in generated-tool route table (_GLP_GENERATED_ROUTES, only
    populated when HPE_MCP_GLP_GENERATED_TOOLS is set) -- these curated
    tools resolve the correct regional host even on the default, curated-only
    glp-core server.
    """

    async def _resolve(
        path: str, extra: dict[str, str] | None = None
    ) -> tuple[str, dict[str, str]]:
        client = get_glp_client()._client
        token = await asyncio.to_thread(client.token_manager.get_access_token)
        headers: dict[str, str] = {"Accept": "application/json"}
        for key, value in (extra or {}).items():
            if key.strip().lower() in _GLP_AUTH_HEADER_NAMES:
                continue
            headers[key] = str(value)
        headers["Authorization"] = f"Bearer {token}"
        return _glp_family_server(family), headers

    return _resolve


async def _glp_family_refresh_auth() -> None:
    client = get_glp_client()._client
    await asyncio.to_thread(client.token_manager.get_access_token, True)


_GLP_FAMILY_READ_EXECUTORS: dict[str, Any] = {}
_GLP_FAMILY_WRITE_EXECUTORS: dict[str, Any] = {}


def _glp_family_read_executor(family: str):
    executor = _GLP_FAMILY_READ_EXECUTORS.get(family)
    if executor is None:
        executor = make_read_executor(
            resolve=_glp_family_resolver(family),
            allowed_prefixes=lambda f=family: _GLP_FAMILY_PREFIXES[f],
            not_configured="GLP not configured",
            refresh_auth=_glp_family_refresh_auth,
        )
        _GLP_FAMILY_READ_EXECUTORS[family] = executor
    return executor


def _glp_family_write_executor(family: str):
    executor = _GLP_FAMILY_WRITE_EXECUTORS.get(family)
    if executor is None:
        executor = make_write_executor(
            resolve=_glp_family_resolver(family),
            allowed_prefixes=lambda f=family: _GLP_FAMILY_PREFIXES[f],
            writes_allowed=lambda: platform_writes_allowed("glp"),
            blocked_response=lambda name: platform_write_blocked("glp", name),
            execute_hint=(
                "Re-run with dry_run=False and confirm=True to execute this GLP "
                f"write (requires {_V2BETA1_WRITES_FLAG}=1)."
            ),
            not_configured="GLP not configured",
            refresh_auth=_glp_family_refresh_auth,
        )
        _GLP_FAMILY_WRITE_EXECUTORS[family] = executor
    return executor


async def _glp_family_get(
    family: str,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    limit: int | None = None,
    list_key: str | None = "items",
) -> dict[str, Any]:
    """Bounded, region-aware GET for one curated GLP service family."""
    try:
        safe_path = safe_api_path(path, _GLP_FAMILY_PREFIXES[family])
    except ValueError as exc:
        return {"data": None, "endpoint_used": path, "errors": [f"Invalid path. {exc}"]}
    safe_params, warnings = _safe_read_params(params)
    result = await _glp_family_read_executor(family)("GET", safe_path, safe_params, {})
    if "error" in result:
        payload: dict[str, Any] = {
            "data": None,
            "endpoint_used": safe_path,
            "errors": [result["error"]],
        }
        if warnings:
            payload["warnings"] = warnings
        return payload
    status_code = result.get("status_code")
    data = result.get("data")
    errors: list[str] = []
    if isinstance(status_code, int) and not (200 <= status_code < 300):
        errors.append(f"GLP {family} request returned HTTP {status_code}: {data}"[:400])
    if limit is not None:
        data = bound_collection_response(
            data, limit=clamp_limit(limit), offset=0, list_key=list_key
        )
    payload = {
        "data": data,
        "endpoint_used": safe_path,
        "status_code": status_code,
        "errors": errors,
    }
    if warnings:
        payload["warnings"] = warnings
    return payload


# ── Compute Ops Management ───────────────────────────────────────────────────

@mcp.tool(annotations=READ_ONLY)
async def list_glp_compute_servers(
    limit: int = 100,
    offset: int = 0,
    filter: str | None = None,
) -> dict[str, Any]:
    """List HPE Compute Ops Management servers (iLO-managed compute inventory).

    Region-specific -- set GLP_GENERATED_REGION to us-west, eu-central, or
    ap-northeast. `filter` is an OData-subset expression per the manifest
    (e.g. "name eq 'server1'").
    """
    bounded_limit = clamp_limit(limit)
    return await _glp_family_get(
        "compute-ops-mgmt",
        "/compute-ops-mgmt/v1/servers",
        _paged_params(bounded_limit, offset, filter=filter),
        limit=bounded_limit,
    )


@mcp.tool(annotations=READ_ONLY)
async def get_glp_compute_server(server_id: str) -> dict[str, Any]:
    """Fetch one HPE Compute Ops Management server by ID."""
    return await _glp_family_get(
        "compute-ops-mgmt",
        f"/compute-ops-mgmt/v1/servers/{_path_part(server_id)}",
        list_key=None,
    )


@mcp.tool(annotations=READ_ONLY)
async def list_glp_compute_server_alerts(
    server_id: str, limit: int = 100, offset: int = 0
) -> dict[str, Any]:
    """List active alerts for one Compute Ops Management server."""
    bounded_limit = clamp_limit(limit)
    return await _glp_family_get(
        "compute-ops-mgmt",
        f"/compute-ops-mgmt/v1/servers/{_path_part(server_id)}/alerts",
        _paged_params(bounded_limit, offset),
        limit=bounded_limit,
    )


@mcp.tool(annotations=READ_ONLY)
async def list_glp_compute_groups(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """List Compute Ops Management server groups."""
    bounded_limit = clamp_limit(limit)
    return await _glp_family_get(
        "compute-ops-mgmt",
        "/compute-ops-mgmt/v1/groups",
        _paged_params(bounded_limit, offset),
        limit=bounded_limit,
    )


@mcp.tool(annotations=READ_ONLY)
async def list_glp_compute_jobs(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """List Compute Ops Management jobs (firmware/config actions) and their status."""
    bounded_limit = clamp_limit(limit)
    return await _glp_family_get(
        "compute-ops-mgmt",
        "/compute-ops-mgmt/v1/jobs",
        _paged_params(bounded_limit, offset),
        limit=bounded_limit,
    )


# ── Storage Fleet ─────────────────────────────────────────────────────────────

@mcp.tool(annotations=READ_ONLY)
async def list_glp_storage_systems(
    limit: int = 100,
    offset: int = 0,
    filter: str | None = None,
) -> dict[str, Any]:
    """List HPE GreenLake Storage Fleet systems (cross-device-type inventory).

    Region-specific -- set GLP_GENERATED_REGION to us-west, eu-west, or
    ap-northeast (routed to the us1/eu1/jp1 data hosts).
    """
    bounded_limit = clamp_limit(limit)
    return await _glp_family_get(
        "storage-fleet",
        "/storage-fleet/v1alpha1/storage-systems",
        _paged_params(bounded_limit, offset, filter=filter),
        limit=bounded_limit,
    )


@mcp.tool(annotations=READ_ONLY)
async def get_glp_storage_system(system_id: str) -> dict[str, Any]:
    """Fetch one Storage Fleet system by ID."""
    return await _glp_family_get(
        "storage-fleet",
        f"/storage-fleet/v1alpha1/storage-systems/{_path_part(system_id)}",
        list_key=None,
    )


@mcp.tool(annotations=READ_ONLY)
async def list_glp_storage_system_types() -> dict[str, Any]:
    """List the storage device types supported by Storage Fleet."""
    return await _glp_family_get(
        "storage-fleet", "/storage-fleet/v1alpha1/storage-types", limit=100
    )


# ── Block Storage ─────────────────────────────────────────────────────────────

@mcp.tool(annotations=READ_ONLY)
async def list_glp_block_storage_volumes(
    limit: int = 100,
    offset: int = 0,
    filter: str | None = None,
) -> dict[str, Any]:
    """List HPE GreenLake Block Storage volumes across the fleet."""
    bounded_limit = clamp_limit(limit)
    return await _glp_family_get(
        "block-storage",
        "/block-storage/v1alpha1/volumes",
        _paged_params(bounded_limit, offset, filter=filter),
        limit=bounded_limit,
    )


@mcp.tool(annotations=READ_ONLY)
async def get_glp_block_storage_volume(volume_id: str) -> dict[str, Any]:
    """Fetch one Block Storage volume by ID."""
    return await _glp_family_get(
        "block-storage",
        f"/block-storage/v1alpha1/volumes/{_path_part(volume_id)}",
        list_key=None,
    )


@mcp.tool(annotations=READ_ONLY)
async def list_glp_block_storage_hosts(
    limit: int = 100,
    offset: int = 0,
    filter: str | None = None,
) -> dict[str, Any]:
    """List Block Storage host initiators (registered application hosts)."""
    bounded_limit = clamp_limit(limit)
    return await _glp_family_get(
        "block-storage",
        "/block-storage/v1alpha1/host-initiators",
        _paged_params(bounded_limit, offset, filter=filter),
        limit=bounded_limit,
    )


# ── Virtualization ────────────────────────────────────────────────────────────

@mcp.tool(annotations=READ_ONLY)
async def list_glp_virtual_machines(
    limit: int = 100,
    offset: int = 0,
    filter: str | None = None,
) -> dict[str, Any]:
    """List virtual machines managed via the GLP Virtualization service."""
    bounded_limit = clamp_limit(limit)
    return await _glp_family_get(
        "virtualization",
        "/virtualization/v1beta1/virtual-machines",
        _paged_params(bounded_limit, offset, filter=filter),
        limit=bounded_limit,
    )


@mcp.tool(annotations=READ_ONLY)
async def get_glp_virtual_machine(vm_id: str) -> dict[str, Any]:
    """Fetch one GLP-managed virtual machine by ID."""
    return await _glp_family_get(
        "virtualization",
        f"/virtualization/v1beta1/virtual-machines/{_path_part(vm_id)}",
        list_key=None,
    )


@mcp.tool(annotations=READ_ONLY)
async def list_glp_hypervisor_managers(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """List registered hypervisor managers (e.g. vCenter instances)."""
    bounded_limit = clamp_limit(limit)
    return await _glp_family_get(
        "virtualization",
        "/virtualization/v1beta1/hypervisor-managers",
        _paged_params(bounded_limit, offset),
        limit=bounded_limit,
    )


@mcp.tool(annotations=READ_ONLY)
async def list_glp_hypervisor_clusters(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """List hypervisor clusters visible to GLP Virtualization."""
    bounded_limit = clamp_limit(limit)
    return await _glp_family_get(
        "virtualization",
        "/virtualization/v1beta1/hypervisor-clusters",
        _paged_params(bounded_limit, offset),
        limit=bounded_limit,
    )


@mcp.tool(annotations=READ_ONLY)
async def list_glp_datastores(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """List datastores visible to GLP Virtualization."""
    bounded_limit = clamp_limit(limit)
    return await _glp_family_get(
        "virtualization",
        "/virtualization/v1beta1/datastores",
        _paged_params(bounded_limit, offset),
        limit=bounded_limit,
    )


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def set_glp_virtual_machine_power(
    vm_id: str,
    action: Literal["power-on", "power-off"],
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Power a single GLP-managed virtual machine on or off.

    No request body per the manifest -- `vm_id`/`action` alone select the
    endpoint. Defaults to a dry-run preview; re-run with dry_run=False and
    confirm=True to execute. Gated behind the same guardrail as other GLP
    v2beta1-style writes -- see glp_write_status. Prefer
    set_glp_virtual_machines_power_bulk for more than one VM (adds per-VM
    partial-failure reporting instead of one all-or-nothing call).
    """
    path = f"/virtualization/v1beta1/virtual-machines/{_path_part(vm_id)}/{action}"
    executor = _glp_family_write_executor("virtualization")
    return await executor(
        f"set_glp_virtual_machine_power:{action}",
        "POST",
        path,
        {},
        {},
        None,
        "application/json",
        dry_run,
        confirm,
    )


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def set_glp_virtual_machines_power_bulk(
    vm_ids: list[str],
    action: Literal["power-on", "power-off"],
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Power on/off up to 20 GLP-managed virtual machines, one request per VM.

    Composite over set_glp_virtual_machine_power: runs sequentially and
    reports a per-VM outcome plus succeeded/failed counts, so one VM
    rejecting the action (e.g. already powered off) never masks the
    outcome of the others. Defaults to a dry-run preview for every VM;
    re-run with dry_run=False and confirm=True to execute. Gated behind the
    same guardrail as other GLP v2beta1-style writes -- see
    glp_write_status.
    """
    if not vm_ids:
        return {
            "results": [],
            "succeeded": 0,
            "failed": 0,
            "errors": ["vm_ids must not be empty"],
        }
    if len(vm_ids) > 20:
        return {
            "results": [],
            "succeeded": 0,
            "failed": 0,
            "errors": [f"vm_ids exceeds the 20-item safety bound (got {len(vm_ids)})"],
        }
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for vm_id in vm_ids:
        outcome = await set_glp_virtual_machine_power(
            vm_id, action, dry_run=dry_run, confirm=confirm
        )
        if outcome.get("dry_run"):
            status = "dry_run"
        elif "error" in outcome:
            status = "failed"
        elif isinstance(outcome.get("status_code"), int) and outcome["status_code"] >= 300:
            status = "failed"
        else:
            status = "ok"
        if status == "failed":
            errors.append(f"{vm_id}: {outcome.get('error') or outcome.get('status_code')}")
        results.append({"vm_id": vm_id, "status": status, "detail": outcome})
    return {
        "action": action,
        "dry_run": dry_run,
        "requested": len(vm_ids),
        "succeeded": sum(1 for r in results if r["status"] == "ok"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "results": results,
        "errors": errors,
    }


# ── Backup & Recovery ─────────────────────────────────────────────────────────

@mcp.tool(annotations=READ_ONLY)
async def list_glp_backup_protection_jobs(
    limit: int = 100,
    offset: int = 0,
    filter: str | None = None,
) -> dict[str, Any]:
    """List Backup & Recovery protection jobs and their run status."""
    bounded_limit = clamp_limit(limit)
    return await _glp_family_get(
        "backup-recovery",
        "/backup-recovery/v1beta1/protection-jobs",
        _paged_params(bounded_limit, offset, filter=filter),
        limit=bounded_limit,
    )


@mcp.tool(annotations=READ_ONLY)
async def get_glp_backup_protection_job(job_id: str) -> dict[str, Any]:
    """Fetch one Backup & Recovery protection job by ID."""
    return await _glp_family_get(
        "backup-recovery",
        f"/backup-recovery/v1beta1/protection-jobs/{_path_part(job_id)}",
        list_key=None,
    )


@mcp.tool(annotations=READ_ONLY)
async def list_glp_backup_protection_stores(
    limit: int = 100, offset: int = 0, filter: str | None = None
) -> dict[str, Any]:
    """List Backup & Recovery protection stores (backup target capacity/health)."""
    bounded_limit = clamp_limit(limit)
    return await _glp_family_get(
        "backup-recovery",
        "/backup-recovery/v1beta1/protection-stores",
        _paged_params(bounded_limit, offset, filter=filter),
        limit=bounded_limit,
    )


@mcp.tool(annotations=READ_ONLY)
async def list_glp_backup_storeonces(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """List registered HPE StoreOnce appliances under Backup & Recovery."""
    bounded_limit = clamp_limit(limit)
    return await _glp_family_get(
        "backup-recovery",
        "/backup-recovery/v1beta1/storeonces",
        _paged_params(bounded_limit, offset),
        limit=bounded_limit,
    )


@mcp.tool(annotations=READ_ONLY)
async def list_glp_backup_vm_protection_groups(
    limit: int = 100, offset: int = 0, filter: str | None = None
) -> dict[str, Any]:
    """List virtual-machine protection groups under Backup & Recovery."""
    bounded_limit = clamp_limit(limit)
    return await _glp_family_get(
        "backup-recovery",
        "/backup-recovery/v1beta1/virtual-machine-protection-groups",
        _paged_params(bounded_limit, offset, filter=filter),
        limit=bounded_limit,
    )


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def run_glp_backup_protection_job(
    job_id: str,
    full_backup: bool = False,
    include_resources: list[str] | None = None,
    schedule_ids: list[str] | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Trigger an existing Backup & Recovery protection job to run now.

    Per the manifest, the request body declares `fullBackup`/
    `includeResources`/`scheduleIds` -- pass `include_resources`/
    `schedule_ids` as empty lists (the default) to run the job's full
    default scope. See list_glp_backup_protection_jobs /
    get_glp_backup_protection_job for the job shape and its configured
    schedule IDs. Defaults to a dry-run preview; re-run with dry_run=False
    and confirm=True to execute. Gated behind the same guardrail as other
    GLP v2beta1-style writes -- see glp_write_status.
    """
    body = {
        "fullBackup": full_backup,
        "includeResources": include_resources or [],
        "scheduleIds": schedule_ids or [],
    }
    path = f"/backup-recovery/v1beta1/protection-jobs/{_path_part(job_id)}/run"
    executor = _glp_family_write_executor("backup-recovery")
    return await executor(
        "run_glp_backup_protection_job",
        "POST",
        path,
        {},
        {},
        body,
        "application/json",
        dry_run,
        confirm,
    )


# ── Data Services ─────────────────────────────────────────────────────────────

@mcp.tool(annotations=READ_ONLY)
async def list_glp_data_services_issues(
    limit: int = 100, offset: int = 0, filter: str | None = None
) -> dict[str, Any]:
    """List open Data Services issues (cross-resource health/status feed).

    `filter` supports issueType/severity/category/state/createdAt/services/
    sourceResourceId/sourceResourceType per the manifest.
    """
    bounded_limit = clamp_limit(limit)
    return await _glp_family_get(
        "data-services",
        "/data-services/v1beta1/issues",
        _paged_params(bounded_limit, offset, filter=filter),
        limit=bounded_limit,
    )


@mcp.tool(annotations=READ_ONLY)
async def get_glp_data_services_issue(issue_id: str) -> dict[str, Any]:
    """Fetch one Data Services issue by ID."""
    return await _glp_family_get(
        "data-services",
        f"/data-services/v1beta1/issues/{_path_part(issue_id)}",
        list_key=None,
    )


@mcp.tool(annotations=READ_ONLY)
async def list_glp_data_services_async_operations(
    limit: int = 100, offset: int = 0, filter: str | None = None
) -> dict[str, Any]:
    """List Data Services async-operation status (job tracking)."""
    bounded_limit = clamp_limit(limit)
    return await _glp_family_get(
        "data-services",
        "/data-services/v1beta1/async-operations",
        _paged_params(bounded_limit, offset, filter=filter),
        limit=bounded_limit,
    )


@mcp.tool(annotations=READ_ONLY)
async def list_glp_data_services_storage_locations(filter: str | None = None) -> dict[str, Any]:
    """List Data Services storage locations (no server-side pagination per the manifest)."""
    return await _glp_family_get(
        "data-services",
        "/data-services/v1beta1/storage-locations",
        _params(filter=filter),
        limit=100,
    )


# ── Reconciliation / planning (read-only) ────────────────────────────────────

_GLP_RECONCILIATION_FAILURE_STATUS_MARKERS = {"failed", "failure", "error"}


def _glp_extract_items(result: Any) -> list[Any]:
    if not isinstance(result, dict):
        return []
    if isinstance(result.get("items"), list):
        return result["items"]
    data = result.get("data")
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    if isinstance(data, list):
        return data
    return []


def _glp_extract_errors(result: Any) -> list[str]:
    if not isinstance(result, dict):
        return []
    errors = result.get("errors")
    if isinstance(errors, list):
        return [str(item) for item in errors]
    return []


def _glp_has_failure_status(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    for key in ("status", "state"):
        value = item.get(key)
        if isinstance(value, str) and value.strip().lower() in (
            _GLP_RECONCILIATION_FAILURE_STATUS_MARKERS
        ):
            return True
    return False


@mcp.tool(annotations=READ_ONLY)
def plan_glp_reconciliation(sample_size: int = 100) -> dict[str, Any]:
    """Read-only cross-resource reconciliation/planning aid. Never writes.

    Fetches one bounded page each of devices, subscriptions, users, RBAC
    role assignments, scope groups, recent audit logs, and reporting
    statuses, then flags likely drift for a human to plan around (e.g.
    devices with no subscription, RBAC role assignments whose scope-group
    isn't in this sample, reporting statuses reporting failure). This is a
    planning aid over one sample page per resource, not an exhaustive
    audit -- page through list_glp_devices / list_glp_subscriptions / etc.
    directly with `offset` for a full workspace sweep.

    Args:
        sample_size: bounded page size for every underlying fetch (clamped
            to the MCP list limit).

    A `sections` block always reports whether each underlying fetch
    actually succeeded, so one API returning e.g. 403 shows up as a failed
    section instead of silently narrowing the findings.
    """
    bounded = clamp_limit(sample_size)
    sections: dict[str, Any] = {}
    errors: list[str] = []

    def _fetch(name: str, fn: Any, **kwargs: Any) -> list[Any]:
        try:
            result = fn(**kwargs)
        except Exception as exc:  # defensive: a single family must not abort the plan
            sections[name] = {"ok": False, "count": 0}
            errors.append(f"{name}: {exc}")
            return []
        items = _glp_extract_items(result)
        section_errors = _glp_extract_errors(result)
        sections[name] = {"ok": not section_errors, "count": len(items)}
        errors.extend(f"{name}: {item}" for item in section_errors)
        return items

    device_items = _fetch("devices", list_glp_devices, limit=bounded, offset=0)
    subscription_items = _fetch("subscriptions", list_glp_subscriptions, limit=bounded, offset=0)
    user_items = _fetch("users", list_glp_users, limit=bounded, offset=0)
    role_assignment_items = _fetch(
        "role_assignments", list_glp_role_assignments, limit=bounded, offset=0
    )
    scope_group_items = _fetch("scope_groups", list_glp_scope_groups, limit=bounded, offset=0)
    _fetch("audit_logs", list_glp_audit_logs, limit=min(bounded, 50), offset=0)
    reporting_items = _fetch(
        "reporting_statuses", list_glp_reporting_statuses, limit=min(bounded, 50), offset=0
    )

    findings: list[dict[str, Any]] = []

    unlicensed_devices = [
        d.get("serialNumber") or d.get("id")
        for d in device_items
        if isinstance(d, dict)
        and not (d.get("subscription") or d.get("subscriptionId") or d.get("subscriptionKey"))
    ]
    if unlicensed_devices:
        findings.append(
            {
                "type": "device_without_subscription",
                "severity": "warning",
                "count": len(unlicensed_devices),
                "sample": unlicensed_devices[:10],
                "recommendation": (
                    "Assign a subscription with glp_assign_subscription "
                    "(dry-run first)."
                ),
            }
        )

    if scope_group_items:
        scope_group_ids = {
            str(sg.get("id")) for sg in scope_group_items if isinstance(sg, dict) and sg.get("id")
        }
        dangling_role_assignments = [
            ra.get("id")
            for ra in role_assignment_items
            if isinstance(ra, dict)
            and isinstance(ra.get("scope"), dict)
            and ra["scope"].get("type") == "scope-group"
            and str(ra["scope"].get("id")) not in scope_group_ids
        ]
        if dangling_role_assignments:
            findings.append(
                {
                    "type": "role_assignment_scope_group_not_in_sample",
                    "severity": "info",
                    "count": len(dangling_role_assignments),
                    "sample": dangling_role_assignments[:10],
                    "recommendation": (
                        "Confirm with get_glp_scope_group -- this may just be "
                        "outside this bounded sample page rather than actually "
                        "missing."
                    ),
                }
            )

    if user_items and not role_assignment_items:
        findings.append(
            {
                "type": "users_present_without_any_sampled_role_assignment",
                "severity": "info",
                "count": len(user_items),
                "recommendation": "Review RBAC coverage with list_glp_role_assignments.",
            }
        )

    failed_reports = [r for r in reporting_items if _glp_has_failure_status(r)]
    if failed_reports:
        findings.append(
            {
                "type": "reporting_status_failure",
                "severity": "warning",
                "count": len(failed_reports),
                "sample": [
                    r.get("id") for r in failed_reports[:10] if isinstance(r, dict)
                ],
                "recommendation": (
                    "Inspect with get_glp_reporting_status -- this heuristic reads "
                    "a status/state field that is not independently confirmed "
                    "against the reporting response schema."
                ),
            }
        )

    return {
        "sample_size": bounded,
        "sections": sections,
        "counts": {
            "devices": len(device_items),
            "subscriptions": len(subscription_items),
            "users": len(user_items),
            "role_assignments": len(role_assignment_items),
            "scope_groups": len(scope_group_items),
            "reporting_statuses": len(reporting_items),
        },
        "findings": findings,
        "errors": errors,
        "note": (
            "Read-only planning over one bounded sample page per resource -- "
            "never writes. Re-run the underlying list_glp_* tools with "
            "offset to page through a full workspace before acting on a "
            "finding."
        ),
    }


# ── API family discovery ──────────────────────────────────────────────────────

@mcp.tool(annotations=READ_ONLY)
def list_glp_api_families() -> dict[str, Any]:
    """List guarded GLP GET path-prefixes reachable via glp_get, and note which
    dedicated typed tools are manifest-backed vs. best-effort/unconfirmed.
    """
    manifest_backed_tools = [
        "list_glp_devices", "get_glp_device", "get_glp_device_by_id",
        "list_glp_devices_v2", "get_glp_device_v2",
        "glp_add_device", "glp_add_devices_bulk", "glp_archive_device",
        "list_glp_subscriptions", "get_glp_subscription", "glp_assign_subscription",
        "list_glp_auto_subscription_settings", "get_glp_auto_subscription_setting",
        "update_glp_auto_subscription_settings",
        "list_glp_users", "get_glp_user",
        "invite_glp_user", "update_glp_user_preferences", "disassociate_glp_user",
        "list_glp_audit_logs", "get_glp_audit_log_detail",
        "list_glp_audit_logs_v2", "get_glp_audit_log_v2", "get_glp_audit_log_v2_detail",
        "get_glp_workspace", "get_glp_workspace_contact", "update_glp_workspace_contact",
        "list_glp_reporting_statuses", "get_glp_reporting_status",
        "list_glp_service_offers", "get_glp_service_offer",
        "list_glp_service_offer_regions", "get_glp_service_offer_region",
        "list_glp_service_provisions", "get_glp_service_provision",
        "list_glp_service_managers", "get_glp_service_manager",
        "list_glp_service_manager_provisions", "get_glp_service_manager_provision",
        "list_glp_per_region_service_managers", "get_glp_service_managers_for_region",
        "list_glp_role_assignments", "get_glp_role_assignment",
        "create_glp_role_assignment", "update_glp_role_assignment", "delete_glp_role_assignment",
        "list_glp_scope_groups", "get_glp_scope_group", "list_glp_scope_group_scopes",
        "create_glp_scope_group", "update_glp_scope_group", "delete_glp_scope_group",
        "add_glp_scope_group_scopes", "delete_glp_scope_group_scopes",
        "list_glp_event_webhooks", "get_glp_event_webhook",
        "list_glp_event_subscriptions", "list_glp_webhook_deliveries",
        "list_glp_locations", "get_glp_location", "reverse_geocode_glp_location",
        "list_glp_location_tags", "get_glp_location_tags",
        "list_glp_tags", "list_glp_tag_resources",
        "list_glp_scim_users", "get_glp_scim_user",
        "list_glp_scim_groups", "get_glp_scim_group",
        "list_glp_scim_group_users", "list_glp_scim_user_groups",
    ]
    region_aware_family_tools = [
        "list_glp_compute_servers", "get_glp_compute_server", "list_glp_compute_server_alerts",
        "list_glp_compute_groups", "list_glp_compute_jobs",
        "list_glp_storage_systems", "get_glp_storage_system", "list_glp_storage_system_types",
        "list_glp_block_storage_volumes", "get_glp_block_storage_volume",
        "list_glp_block_storage_hosts",
        "list_glp_virtual_machines", "get_glp_virtual_machine", "list_glp_hypervisor_managers",
        "list_glp_hypervisor_clusters", "list_glp_datastores",
        "set_glp_virtual_machine_power", "set_glp_virtual_machines_power_bulk",
        "list_glp_backup_protection_jobs", "get_glp_backup_protection_job",
        "list_glp_backup_protection_stores", "list_glp_backup_storeonces",
        "list_glp_backup_vm_protection_groups", "run_glp_backup_protection_job",
        "list_glp_data_services_issues", "get_glp_data_services_issue",
        "list_glp_data_services_async_operations", "list_glp_data_services_storage_locations",
    ]
    return {
        "guarded_get_prefixes": list(_GLP_GET_PREFIXES),
        "confirmed_typed_tools": manifest_backed_tools,
        "curated_manifest_backed_tools": manifest_backed_tools,
        "region_aware_family_tools": region_aware_family_tools,
        "region_aware_families": {
            family: {
                "hosts": list(hosts),
                "path_prefix": _GLP_FAMILY_PREFIXES[family][0],
            }
            for family, hosts in _GLP_FAMILY_HOSTS.items()
        },
        "best_effort_typed_tools": [
            "group_glp_devices",
            "glp_add_subscriptions",
        ],
        "explore_only_families": {
            "notifications": "/notifications/...",
            "API client credentials": "no confirmed path — not exposed via glp_get yet",
        },
        "reconciliation_tools": ["plan_glp_reconciliation"],
        "note": (
            "Named RBAC, event-webhook, tag, location, and SCIM reads are backed by "
            "the committed GLP OpenAPI manifest. RBAC role-assignment/scope-group "
            "lifecycle, identity user lifecycle, auto-subscription-setting writes, "
            "device add/bulk-add/archive (POST and PATCH /devices/v1|v2beta1/devices), "
            "and subscription assignment (PATCH /devices/v2beta1/devices) are also "
            "manifest-backed and gated behind glp_write_status. Use glp_get "
            "only for other documented resources under an allowed prefix. "
            "region_aware_family_tools (compute/storage-fleet/block-storage/"
            "virtualization/backup-recovery/data-services) are NOT reachable via "
            "glp_get — they are served from region-specific hosts (never "
            "global.api.greenlake.hpe.com); set GLP_GENERATED_REGION to a region "
            "valid for that family (see region_aware_families) before calling them."
        ),
    }



# ---------------------------------------------------------------------------
# Generated GreenLake (GLP) tools (see src/hpe_networking_mcp/mcp_servers/openapi_gen). The committed
# manifest at src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/glp.json is derived from the
# MIT-licensed nowireless4u/hpe-networking-mcp project's vendored HPE GreenLake
# OpenAPI specs (raw specs are proprietary and NOT committed — see the manifest
# "provenance" block and scripts/generate_glp_tools.py). Every unique documented
# GLP operation becomes a directly-callable, typed MCPServer tool that reuses the
# target-account GLPClient auth/workspace/retry behavior. Registration is guarded
# by HPE_MCP_GLP_GENERATED_TOOLS and defaults OFF (see
# _glp_generated_enabled below) except in `direct` router mode with the
# `glp`/`all` toolset, so the default curated glp-core catalog stays small.
#
# The 105 curated GLP tools above are the confirmed-working, hand-tuned surface;
# the generated glp_* tools broaden coverage to the full workspace/inventory/
# licensing/service-catalog/storage/compute surface. Generated writes stay
# fail-closed behind the same HPE_MCP_GLP_V2BETA1_WRITES gate and default to
# dry_run=True.
# ---------------------------------------------------------------------------

# _GLP_AUTH_HEADER_NAMES is shared with the curated region-aware family
# tools above (see _glp_family_resolver) -- defined once near the top of
# this module.

# Populated at registration time from the committed manifest (defense-in-depth
# path allow-list). The shared runtime already URL-escapes path values and
# rejects traversal segments, so this is belt-and-suspenders.
_GLP_GENERATED_PREFIXES: tuple[str, ...] = ("/devices/", "/subscriptions/", "/workspaces/")
_GLP_GENERATED_ROUTES: list[tuple[re.Pattern[str], tuple[str, ...]]] = []
_GLP_SUNSET_OPERATION_PREFIXES = (
    "/devices/v1beta1/",
    "/subscriptions/v1alpha1/",
    "/subscriptions/v1beta1/",
)

_GLP_GENERATED_EXECUTE_HINT = (
    "Re-run with dry_run=False and confirm=True to execute this GLP write "
    f"(requires {_V2BETA1_WRITES_FLAG}=1)."
)


def _glp_generated_prefixes() -> tuple[str, ...]:
    return _GLP_GENERATED_PREFIXES


def _glp_route_pattern(path_template: str) -> re.Pattern[str]:
    parts = re.split(r"(\{[^}]+\})", path_template)
    pattern = "".join("[^/]+" if part.startswith("{") else re.escape(part) for part in parts)
    return re.compile(f"^{pattern}$")


def _glp_generated_server(path: str, configured_base_url: str) -> str:
    server_urls: tuple[str, ...] = ()
    for pattern, candidates in _GLP_GENERATED_ROUTES:
        if pattern.fullmatch(path):
            server_urls = candidates
            break
    if not server_urls:
        return configured_base_url.rstrip("/")
    configured = configured_base_url.rstrip("/")
    if configured in server_urls:
        return configured
    global_url = "https://global.api.greenlake.hpe.com"
    if global_url in server_urls:
        return global_url
    if len(server_urls) == 1:
        return server_urls[0]

    region = os.environ.get("GLP_GENERATED_REGION", "").strip().lower()
    # Shared with the curated region-aware family tools' _glp_family_server
    # (see _GLP_REGIONAL_API_HOSTS / _GLP_REGIONAL_DATA_HOSTS above).
    requested = (
        _GLP_REGIONAL_DATA_HOSTS.get(region)
        if any(".data.cloud.hpe.com" in url for url in server_urls)
        else _GLP_REGIONAL_API_HOSTS.get(region)
    )
    if requested in server_urls:
        return requested
    raise ValueError(
        "This generated GLP operation is region-specific. Set GLP_GENERATED_REGION "
        "to one of us-west, eu-west, eu-central, or ap-northeast."
    )


async def _glp_generated_auth_headers(
    path: str | dict[str, str] | None,
    extra: dict[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    """Return ``(base_url, headers)`` with trusted GLP auth injected last.

    Reuses the target-account GLPClient's underlying token manager (its
    workspace-scoped bearer token) and GLP base URL, injecting the
    Authorization header last. Non-auth header params are preserved; the
    client's httpx session is never touched here.
    """
    if not isinstance(path, str):
        extra = path
        path = ""
    client = get_glp_client()._client
    # Acquire the workspace-scoped GLP bearer token off the event loop via the
    # GLPClient's underlying token manager; never touch the client's httpx
    # session (that boundary is owned exclusively by CentralClient).
    token = await asyncio.to_thread(client.token_manager.get_access_token)
    headers: dict[str, str] = {"Accept": "application/json"}
    for key, value in (extra or {}).items():
        if key.strip().lower() in _GLP_AUTH_HEADER_NAMES:
            continue
        headers[key] = str(value)
    headers["Authorization"] = f"Bearer {token}"  # trusted auth injected last
    base_url = (
        _glp_generated_server(path, client.base_url)
        if path
        else client.base_url.rstrip("/")
    )
    return base_url, headers


async def _glp_generated_refresh_auth() -> None:
    client = get_glp_client()._client
    await asyncio.to_thread(client.token_manager.get_access_token, True)


def _glp_generated_enabled() -> bool:
    """Whether the generated GLP tools should register.

    Opt-in and **default OFF**: unlike the optional-product starter backends,
    the ~920 generated GLP tools are a very large surface, so we keep the
    default ``glp-core`` catalog to the 105 curated tools and only expand when
    an operator sets ``HPE_MCP_GLP_GENERATED_TOOLS`` truthy. (Central's
    generated tools live on a separate ``central-generated`` server, so
    they can default on without inflating a shared catalog; the GLP generated
    tools share the curated ``glp-core`` server, hence the opt-in default.)
    """
    raw = os.environ.get("HPE_MCP_GLP_GENERATED_TOOLS")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    router_mode = os.environ.get("HPE_MCP_ROUTER_MODE", "").strip().lower()
    toolsets = {
        item.strip().lower()
        for item in os.environ.get("HPE_MCP_TOOLSETS", "").split(",")
        if item.strip()
    }
    return router_mode == "direct" and bool({"glp", "all"} & toolsets)


def _register_generated_glp_tools() -> list[str]:
    """Register generated GLP tools (idempotent).

    No-op (returns ``[]``) when the opt-in flag is off or the manifest is
    missing, so a stripped checkout never breaks import. Safe to call again
    after enabling the flag (e.g. from tests); already-registered tools are
    returned as-is.
    """
    global GENERATED_GLP_TOOLS
    if GENERATED_GLP_TOOLS:
        return GENERATED_GLP_TOOLS
    from hpe_networking_mcp.mcp_servers.openapi_gen.http_exec import make_read_executor, make_write_executor
    from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import load_manifest, manifest_exists
    from hpe_networking_mcp.mcp_servers.openapi_gen.runtime import register_generated_tools

    if not _glp_generated_enabled() or not manifest_exists("glp"):
        return []
    manifest = load_manifest("glp")
    active_manifest = {
        **manifest,
        "operations": [
            operation
            for operation in manifest.get("operations", [])
            if not operation["path"].startswith(_GLP_SUNSET_OPERATION_PREFIXES)
        ],
    }
    global _GLP_GENERATED_PREFIXES, _GLP_GENERATED_ROUTES
    prefixes = sorted(
        {
            "/" + op["path"].split("/", 2)[1] + "/"
            for op in manifest.get("operations", [])
            if isinstance(op.get("path"), str) and op["path"].startswith("/")
        }
    )
    if prefixes:
        _GLP_GENERATED_PREFIXES = tuple(prefixes)
    _GLP_GENERATED_ROUTES = [
        (
            _glp_route_pattern(operation["path"]),
            tuple(operation.get("server_urls") or ()),
        )
        for operation in active_manifest.get("operations", [])
    ]

    read_executor = make_read_executor(
        resolve=_glp_generated_auth_headers,
        allowed_prefixes=_glp_generated_prefixes,
        not_configured="GLP not configured",
        refresh_auth=_glp_generated_refresh_auth,
    )
    write_executor = make_write_executor(
        resolve=_glp_generated_auth_headers,
        allowed_prefixes=_glp_generated_prefixes,
        writes_allowed=lambda: platform_writes_allowed("glp"),
        blocked_response=lambda name: platform_write_blocked("glp", name),
        execute_hint=_GLP_GENERATED_EXECUTE_HINT,
        not_configured="GLP not configured",
        refresh_auth=_glp_generated_refresh_auth,
    )
    GENERATED_GLP_TOOLS = register_generated_tools(
        mcp,
        "glp",
        read_executor=read_executor,
        write_executor=write_executor,
        manifest=active_manifest,
    )
    return GENERATED_GLP_TOOLS


# Module global, populated by _register_generated_glp_tools(). Declared before
# the (opt-in) registration call so the idempotency guard has a value to read.
GENERATED_GLP_TOOLS: list[str] = []
GENERATED_GLP_TOOLS = _register_generated_glp_tools()


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
