"""MCP server — optional ClearPass backend (30 curated + 815 generated API tools).

The committed manifest contains 816 operations; the unauthenticated `/oauth`
bootstrap operation is intentionally not model-visible because access and
refresh tokens must remain server-side credentials.

Enabled via tool router env:
  HPE_MCP_PRODUCTS=clearpass

Auth/env:
  CLEARPASS_BASE_URL   e.g. https://clearpass.example.com
  CLEARPASS_API_TOKEN  static bearer token

Covers endpoint/session/NAD/guest reads and writes plus documented Insight
endpoint and OnGuard activity APIs.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.mcpserver import MCPServer

from hpe_networking_mcp.mcp_servers.openapi_gen.http_exec import apply_request_body
from hpe_networking_mcp.mcp_servers.shared import (
    DESTRUCTIVE,
    IDEMPOTENT_WRITE,
    READ_ONLY,
    bound_collection_response,
    bounded_response_payload,
    clamp_limit,
    redact_sensitive,
    response_payload,
    safe_api_path,
    validate_product_base_url,
)
from hpe_networking_mcp.mcp_servers.shared import (
    platform_write_blocked as _platform_write_blocked,
)
from hpe_networking_mcp.mcp_servers.shared import (
    platform_writes_allowed as _platform_writes_allowed,
)

mcp = MCPServer("clearpass-core")


def optional_product_writes_allowed() -> bool:
    return _platform_writes_allowed("clearpass")


def optional_product_write_blocked(tool_name: str) -> dict[str, str]:
    return _platform_write_blocked("clearpass", tool_name)


_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_EXECUTE_HINT = "Review the request, then call again with dry_run=False and confirm=True."


def _clearpass_config() -> tuple[str | None, str | None]:
    import os

    base_url = os.getenv("CLEARPASS_BASE_URL", "").strip().rstrip("/")
    token = os.getenv("CLEARPASS_API_TOKEN", "").strip()
    return (base_url or None, token or None)


def _auth_header(token: str) -> str:
    if token.lower().startswith("bearer "):
        return token
    return "Bearer " + token


def _normalize_mac(mac_address: str) -> str:
    normalized = re.sub(r"[^0-9A-Fa-f]", "", mac_address).lower()
    if len(normalized) != 12:
        raise ValueError("MAC address must contain exactly 12 hex characters")
    return normalized


def _path_segment(value: str) -> str:
    return quote(value, safe="")


async def _clearpass_get_request(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    limit: int = 50,
    offset: int = 0,
    bound: bool = True,
) -> dict[str, Any]:
    base_url, token = _clearpass_config()
    if not base_url or not token:
        return {
            "error": "ClearPass not configured. Set CLEARPASS_BASE_URL and CLEARPASS_API_TOKEN."
        }
    try:
        path = safe_api_path(path, ("/api/",))
    except ValueError as exc:
        return {"error": f"Invalid path. {exc}"}
    path = quote(path, safe="/")

    try:
        base_url = validate_product_base_url(base_url, product="ClearPass")
    except ValueError as exc:
        return {"error": str(exc)}
    url = f"{base_url}{path}"
    headers = {"Authorization": _auth_header(token), "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=headers, params=params or {})
        payload = response_payload(resp)
        if bound:
            payload = bound_collection_response(payload, limit=limit, offset=offset)
        return {"status_code": resp.status_code, "data": payload, "url": url}
    except httpx.HTTPError as exc:
        return {"error": str(exc), "url": url}


async def _clearpass_write_request(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | list[Any] | None = None,
    *,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    if not optional_product_writes_allowed():
        return optional_product_write_blocked("clearpass_write")
    method = method.upper()
    if method not in _WRITE_METHODS:
        return {"error": f"method must be one of: {', '.join(sorted(_WRITE_METHODS))}"}

    base_url, token = _clearpass_config()
    if not base_url or not token:
        return {
            "error": "ClearPass not configured. Set CLEARPASS_BASE_URL and CLEARPASS_API_TOKEN."
        }
    try:
        safe_path = safe_api_path(path, ("/api/",))
    except ValueError as exc:
        return {"error": f"Invalid path. {exc}"}
    safe_path = quote(safe_path, safe="/")

    try:
        base_url = validate_product_base_url(base_url, product="ClearPass")
    except ValueError as exc:
        return {"error": str(exc)}

    url = f"{base_url}{safe_path}"
    preview: dict[str, Any] = {
        "method": method,
        "path": safe_path,
        "url": url,
        "params": redact_sensitive(params or {}),
        "json": redact_sensitive(body),
    }
    if dry_run:
        return {
            "dry_run": True,
            **preview,
            "execute_hint": _EXECUTE_HINT,
        }
    if not confirm:
        return {
            "error": "confirm=True is required when dry_run=False.",
            "dry_run": True,
            **preview,
        }

    headers = {"Authorization": _auth_header(token), "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method,
                url,
                headers=headers,
                params=params or {},
                json=body,
            )
        return {
            "status_code": resp.status_code,
            "data": redact_sensitive(response_payload(resp)),
            "url": url,
        }
    except httpx.HTTPError as exc:
        return {"error": str(exc), "url": url}


def _extract_items(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("items", "results", "data", "_embedded"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def _first_item(data: Any) -> Any:
    if isinstance(data, dict) and not any(
        isinstance(data.get(key), list) for key in ("items", "results", "data")
    ):
        return data
    items = _extract_items(data)
    return items[0] if items else None


def _pick(data: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {
        field: data[field]
        for field in fields
        if field in data and data[field] not in (None, "")
    }


def _compact_endpoint(endpoint: Any) -> Any:
    if not isinstance(endpoint, dict):
        return endpoint
    return _pick(
        endpoint,
        (
            "id",
            "mac_address",
            "status",
            "enabled",
            "profile_status",
            "profile",
            "profile_name",
            "device_category",
            "device_family",
            "device_name",
            "hostname",
            "ip_address",
            "username",
            "description",
            "updated_at",
            "created_at",
            "last_seen",
        ),
    )


def _compact_session(session: Any) -> Any:
    if not isinstance(session, dict):
        return session
    out = _pick(
        session,
        (
            "id",
            "session_id",
            "username",
            "user_name",
            "mac_address",
            "calling_station_id",
            "endpoint_mac_address",
            "nasipaddress",
            "nas_ip",
            "nas_identifier",
            "nas_port_id",
            "auth_status",
            "service_name",
            "enforcement_profile",
            "acctstarttime",
            "timestamp",
        ),
    )
    for key in (
        "reason",
        "auth_error",
        "error_message",
        "reply_message",
        "alert_message",
        "result",
    ):
        if session.get(key):
            out["reason"] = session[key]
            break
    return out


def _compact_network_device(device: Any) -> Any:
    if not isinstance(device, dict):
        return device
    return _pick(
        device,
        (
            "id",
            "name",
            "ip_address",
            "vendor_name",
            "coa_capable",
            "coa_port",
            "status",
            "enabled",
            "description",
        ),
    )


def _compact_guest(guest: Any) -> Any:
    if not isinstance(guest, dict):
        return guest
    return _pick(
        guest,
        (
            "id",
            "username",
            "email",
            "visitor_name",
            "name",
            "role_name",
            "sponsor_name",
            "sponsor_email",
            "enabled",
            "expire_time",
            "created_at",
            "updated_at",
        ),
    )


@mcp.tool(annotations=READ_ONLY)
def clearpass_status() -> dict[str, Any]:
    """Report whether ClearPass backend is configured."""
    base_url, token = _clearpass_config()

    return {
        "configured": bool(base_url and token),
        "base_url": base_url,
        "has_token": bool(token),
    }


@mcp.tool(annotations=READ_ONLY)
async def clearpass_get(
    path: str,
    params: dict[str, Any] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Perform a read-only GET request to ClearPass REST API.

    Safety guard: only allows paths beginning with `/api/`.
    List payloads are bounded with `limit` and `offset`.
    """
    out = await _clearpass_get_request(path, params, bound=False)
    if "data" in out:
        out["data"] = redact_sensitive(
            bound_collection_response(out["data"], limit=limit, offset=offset)
        )
    return redact_sensitive(out)


@mcp.tool(annotations=READ_ONLY)
async def clearpass_get_endpoint_by_mac(mac_address: str) -> dict[str, Any]:
    """Look up one ClearPass endpoint by MAC address.

    Accepts colon, dash, dotted, or compact MAC input and queries
    `/api/endpoint/mac-address/{mac}`. Returns compact endpoint/profile/status
    fields instead of the full endpoint record.
    """
    try:
        normalized = _normalize_mac(mac_address)
    except ValueError as exc:
        return {"error": str(exc)}
    out = await _clearpass_get_request(f"/api/endpoint/mac-address/{normalized}")
    if "data" in out:
        out["normalized_mac"] = normalized
        out["endpoint"] = _compact_endpoint(_first_item(out["data"]))
        del out["data"]
    return out


@mcp.tool(annotations=READ_ONLY)
async def clearpass_list_auth_failures(
    limit: int = 25,
    offset: int = 0,
    auth_status: str = "FAILED",
) -> dict[str, Any]:
    """List recent ClearPass authentication failures.

    Queries `/api/session` with a bounded page and an `auth_status` filter
    (default `FAILED`). Returns compact username, MAC, NAD, status, and reason
    fields for troubleshooting.
    """
    safe_limit = clamp_limit(limit, default=25)
    params = {
        "filter": json.dumps({"auth_status": auth_status}, separators=(",", ":")),
        "sort": "-acctstarttime",
        "offset": max(0, offset),
        "limit": safe_limit,
        "calculate_count": "false",
    }
    out = await _clearpass_get_request(
        "/api/session",
        params,
        limit=safe_limit,
        offset=0,
    )
    if "data" in out:
        items = [_compact_session(item) for item in _extract_items(out["data"])]
        out["data"] = bound_collection_response(items, limit=safe_limit, offset=0)
        if isinstance(out["data"], dict):
            out["data"]["filter"] = {"auth_status": auth_status}
            out["data"]["server_offset"] = max(0, offset)
    return out


@mcp.tool(annotations=READ_ONLY)
async def clearpass_get_network_device(
    name: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    """Get a ClearPass network device (NAD) by name or numeric ID.

    Uses `/api/network-device/name/{name}` when `name` is provided, otherwise
    `/api/network-device/{device_id}`. Returns compact RADIUS/TACACS status
    fields useful for NAD troubleshooting.
    """
    if bool(name) == bool(device_id):
        return {"error": "Provide exactly one of name or device_id."}
    if name:
        path = f"/api/network-device/name/{_path_segment(name)}"
    else:
        path = f"/api/network-device/{_path_segment(device_id or '')}"
    out = await _clearpass_get_request(path)
    if "data" in out:
        out["network_device"] = _compact_network_device(_first_item(out["data"]))
        del out["data"]
    return out


@mcp.tool(annotations=READ_ONLY)
async def clearpass_find_guest(
    query: str,
    field: str = "username",
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """Find ClearPass guest accounts by username, email, visitor_name, or name.

    `username` uses the direct `/api/guest/username/{username}` lookup. Other
    fields query `/api/guest` with a JSON filter. Results are compact and
    bounded by `limit` / `offset`.
    """
    allowed_fields = {"username", "email", "visitor_name", "name"}
    if field not in allowed_fields:
        return {"error": f"field must be one of: {', '.join(sorted(allowed_fields))}"}
    if field == "username":
        out = await _clearpass_get_request(f"/api/guest/username/{_path_segment(query)}")
        if "data" in out:
            out["guests"] = bound_collection_response(
                [_compact_guest(_first_item(out["data"]))],
                limit=1,
                offset=0,
            )
            del out["data"]
        return out

    safe_limit = clamp_limit(limit, default=25)
    params = {
        "filter": json.dumps({field: query}, separators=(",", ":")),
        "offset": max(0, offset),
        "limit": safe_limit,
        "calculate_count": "false",
    }
    out = await _clearpass_get_request("/api/guest", params, limit=safe_limit, offset=0)
    if "data" in out:
        out["guests"] = bound_collection_response(
            [_compact_guest(item) for item in _extract_items(out["data"])],
            limit=safe_limit,
            offset=0,
        )
        if isinstance(out["guests"], dict):
            out["guests"]["filter"] = {field: query}
            out["guests"]["server_offset"] = max(0, offset)
        del out["data"]
    return out


@mcp.tool(annotations=READ_ONLY)
async def clearpass_get_insight_endpoint(mac_address: str) -> dict[str, Any]:
    """Get documented ClearPass Insight endpoint data by MAC address."""
    try:
        normalized = _normalize_mac(mac_address)
    except ValueError as exc:
        return {"error": str(exc)}
    out = await _clearpass_get_request(f"/api/insight/endpoint/mac/{normalized}")
    if "data" in out:
        out["normalized_mac"] = normalized
        out["endpoint"] = redact_sensitive(_first_item(out.pop("data")))
    return out


@mcp.tool(annotations=READ_ONLY)
async def clearpass_list_onguard_activity(
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """List documented ClearPass OnGuard activity records."""
    safe_limit = clamp_limit(limit, default=25)
    params = {
        "offset": max(0, offset),
        "limit": safe_limit,
        "calculate_count": "false",
    }
    out = await _clearpass_get_request(
        "/api/onguard-activity",
        params,
        limit=safe_limit,
        offset=0,
        bound=False,
    )
    if "data" in out:
        out["activity"] = redact_sensitive(
            bound_collection_response(_extract_items(out.pop("data")), limit=safe_limit)
        )
    return out


@mcp.tool(annotations=READ_ONLY)
async def clearpass_get_onguard_activity_by_mac(mac_address: str) -> dict[str, Any]:
    """Get documented ClearPass OnGuard activity for one endpoint MAC."""
    try:
        normalized = _normalize_mac(mac_address)
    except ValueError as exc:
        return {"error": str(exc)}
    out = await _clearpass_get_request(
        f"/api/onguard-activity/host_mac/{normalized}"
    )
    if "data" in out:
        out["normalized_mac"] = normalized
        out["activity"] = redact_sensitive(out.pop("data"))
    return out


@mcp.tool(annotations=DESTRUCTIVE)
async def clearpass_write(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | list[Any] | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Perform a lab write request to ClearPass with a preview-first guard.

    Allows `POST`, `PUT`, `PATCH`, and `DELETE` against `/api/*` paths on the
    configured ClearPass host. Defaults to `dry_run=True`; execution requires
    `dry_run=False` and `confirm=True`.
    """
    return await _clearpass_write_request(
        method,
        path,
        params=params,
        body=body,
        dry_run=dry_run,
        confirm=confirm,
    )


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def clearpass_update_endpoint_attributes(
    mac_address: str,
    attributes: dict[str, Any],
    change_of_authorization: bool = False,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Patch ClearPass endpoint attributes by MAC for lab workflows.

    Uses `PATCH /api/endpoint/mac-address/{mac}` with an `attributes` object.
    Defaults to `dry_run=True`; execution requires `dry_run=False` and
    `confirm=True`.
    """
    try:
        normalized = _normalize_mac(mac_address)
    except ValueError as exc:
        return {"error": str(exc)}
    out = await _clearpass_write_request(
        "PATCH",
        f"/api/endpoint/mac-address/{normalized}",
        params={"change_of_authorization": str(change_of_authorization).lower()},
        body={"attributes": attributes},
        dry_run=dry_run,
        confirm=confirm,
    )
    out["normalized_mac"] = normalized
    return out


@mcp.tool(annotations=DESTRUCTIVE)
async def clearpass_delete_endpoint(
    mac_address: str,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Delete a ClearPass endpoint by MAC address.

    Uses `DELETE /api/endpoint/mac-address/{mac}`. Defaults to `dry_run=True`;
    execution requires `dry_run=False` and `confirm=True`.
    """
    try:
        normalized = _normalize_mac(mac_address)
    except ValueError as exc:
        return {"error": str(exc)}
    out = await _clearpass_write_request(
        "DELETE",
        f"/api/endpoint/mac-address/{normalized}",
        dry_run=dry_run,
        confirm=confirm,
    )
    out["normalized_mac"] = normalized
    return out


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def clearpass_set_guest_enabled(
    enabled: bool,
    username: str | None = None,
    guest_id: str | None = None,
    change_of_authorization: bool = False,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Enable or disable a ClearPass guest account by username or ID.

    Uses `PATCH /api/guest/username/{username}` or `/api/guest/{guest_id}` with
    `{"enabled": ...}`. Defaults to `dry_run=True`; execution requires
    `dry_run=False` and `confirm=True`.
    """
    if bool(username) == bool(guest_id):
        return {"error": "Provide exactly one of username or guest_id."}
    if username:
        path = f"/api/guest/username/{_path_segment(username)}"
    else:
        path = f"/api/guest/{_path_segment(guest_id or '')}"
    return await _clearpass_write_request(
        "PATCH",
        path,
        params={"change_of_authorization": str(change_of_authorization).lower()},
        body={"enabled": enabled},
        dry_run=dry_run,
        confirm=confirm,
    )


@mcp.tool(annotations=DESTRUCTIVE)
async def clearpass_delete_guest(
    username: str | None = None,
    guest_id: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Delete a ClearPass guest account by username or ID.

    Uses `DELETE /api/guest/username/{username}` or `/api/guest/{guest_id}`.
    Defaults to `dry_run=True`; execution requires `dry_run=False` and
    `confirm=True`.
    """
    if bool(username) == bool(guest_id):
        return {"error": "Provide exactly one of username or guest_id."}
    if username:
        path = f"/api/guest/username/{_path_segment(username)}"
    else:
        path = f"/api/guest/{_path_segment(guest_id or '')}"
    return await _clearpass_write_request(
        "DELETE",
        path,
        dry_run=dry_run,
        confirm=confirm,
    )


# ---------------------------------------------------------------------------
# Access Tracker (session monitoring)
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def clearpass_list_access_tracker_sessions(
    status: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """List ClearPass Access Tracker sessions with optional status filter.

    Uses `GET /api/session` with a bounded page. Pass `status` to filter by
    ``auth_status`` (e.g. ``FAILED``, ``REJECT``, ``ALLOW``, ``DISCONNECT``);
    omit to return sessions of any status. For failures-only, prefer
    `clearpass_list_auth_failures` which defaults to ``FAILED``. Results are
    compact (username, MAC, NAD, status, reason fields).
    """
    safe_limit = clamp_limit(limit, default=25)
    params: dict[str, Any] = {
        "sort": "-acctstarttime",
        "offset": max(0, offset),
        "limit": safe_limit,
        "calculate_count": "false",
    }
    if status is not None:
        params["filter"] = json.dumps({"auth_status": status}, separators=(",", ":"))
    out = await _clearpass_get_request(
        "/api/session",
        params,
        limit=safe_limit,
        offset=0,
    )
    if "data" in out:
        items = [_compact_session(item) for item in _extract_items(out["data"])]
        out["data"] = bound_collection_response(items, limit=safe_limit, offset=0)
        if isinstance(out["data"], dict):
            if status is not None:
                out["data"]["filter"] = {"auth_status": status}
            out["data"]["server_offset"] = max(0, offset)
    return out


@mcp.tool(annotations=READ_ONLY)
async def clearpass_get_access_tracker_session(session_id: str) -> dict[str, Any]:
    """Get one ClearPass Access Tracker session by ID.

    Uses `GET /api/session/{id}`. Returns compact session fields (username,
    MAC, NAD, status, reason).
    """
    out = await _clearpass_get_request(
        f"/api/session/{_path_segment(session_id)}",
        bound=False,
    )
    if "data" in out:
        out["session"] = _compact_session(out.pop("data"))
    return out


@mcp.tool(annotations=DESTRUCTIVE)
async def clearpass_disconnect_session(
    session_id: str,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Disconnect an active ClearPass session via Change of Authorization.

    Uses `POST /api/session/{id}/disconnect`. Defaults to `dry_run=True`;
    execution requires `dry_run=False` and `confirm=True`.
    """
    return await _clearpass_write_request(
        "POST",
        f"/api/session/{_path_segment(session_id)}/disconnect",
        dry_run=dry_run,
        confirm=confirm,
    )


# ---------------------------------------------------------------------------
# Endpoint management
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def clearpass_list_endpoints(
    limit: int = 25,
    offset: int = 0,
    status: str | None = None,
) -> dict[str, Any]:
    """List ClearPass endpoints with optional status filter and bounded pagination.

    Uses `GET /api/endpoint`. Pass `status` to filter (e.g. ``Known``,
    ``Unknown``, ``Disabled``). Returns compact profile/status fields.
    Use `clearpass_get_endpoint_by_mac` for a single MAC lookup.
    """
    safe_limit = clamp_limit(limit, default=25)
    params: dict[str, Any] = {
        "offset": max(0, offset),
        "limit": safe_limit,
        "calculate_count": "false",
    }
    if status is not None:
        params["filter"] = json.dumps({"status": status}, separators=(",", ":"))
    out = await _clearpass_get_request(
        "/api/endpoint",
        params,
        limit=safe_limit,
        offset=0,
    )
    if "data" in out:
        items = [_compact_endpoint(item) for item in _extract_items(out["data"])]
        out["endpoints"] = bound_collection_response(items, limit=safe_limit, offset=0)
        if isinstance(out["endpoints"], dict):
            out["endpoints"]["server_offset"] = max(0, offset)
        del out["data"]
    return out


# ---------------------------------------------------------------------------
# Guest management
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def clearpass_list_guests(
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """List ClearPass guest accounts with bounded pagination.

    Uses `GET /api/guest`. Returns compact guest identity fields.
    Use `clearpass_find_guest` to search by username, email, or name.
    """
    safe_limit = clamp_limit(limit, default=25)
    params: dict[str, Any] = {
        "offset": max(0, offset),
        "limit": safe_limit,
        "calculate_count": "false",
    }
    out = await _clearpass_get_request(
        "/api/guest",
        params,
        limit=safe_limit,
        offset=0,
    )
    if "data" in out:
        items = [_compact_guest(item) for item in _extract_items(out["data"])]
        out["guests"] = bound_collection_response(items, limit=safe_limit, offset=0)
        if isinstance(out["guests"], dict):
            out["guests"]["server_offset"] = max(0, offset)
        del out["data"]
    return out


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def clearpass_create_guest(
    username: str,
    password: str | None = None,
    role_name: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create a ClearPass guest account.

    Uses `POST /api/guest`. The `password` field is redacted in any dry-run
    preview or echoed response. Defaults to `dry_run=True`; execution requires
    `dry_run=False` and `confirm=True`.
    """
    body: dict[str, Any] = {"username": username}
    if password is not None:
        body["password"] = password
    if role_name is not None:
        body["role_name"] = role_name
    return await _clearpass_write_request(
        "POST",
        "/api/guest",
        body=body,
        dry_run=dry_run,
        confirm=confirm,
    )


# ---------------------------------------------------------------------------
# Policy (roles, enforcement policies)
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def clearpass_list_roles(
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """List ClearPass roles with bounded pagination.

    Uses `GET /api/role`. Returns id and name for each role.
    """
    safe_limit = clamp_limit(limit, default=25)
    params: dict[str, Any] = {
        "offset": max(0, offset),
        "limit": safe_limit,
        "calculate_count": "false",
    }
    out = await _clearpass_get_request(
        "/api/role",
        params,
        limit=safe_limit,
        offset=0,
    )
    if "data" in out:
        items = _extract_items(out["data"])
        out["roles"] = bound_collection_response(items, limit=safe_limit, offset=0)
        if isinstance(out["roles"], dict):
            out["roles"]["server_offset"] = max(0, offset)
        del out["data"]
    return out


@mcp.tool(annotations=READ_ONLY)
async def clearpass_list_enforcement_policies(
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """List ClearPass enforcement policies with bounded pagination.

    Uses `GET /api/enforcement-policy`. Returns id, name, type, and default
    action for each policy.
    """
    safe_limit = clamp_limit(limit, default=25)
    params: dict[str, Any] = {
        "offset": max(0, offset),
        "limit": safe_limit,
        "calculate_count": "false",
    }
    out = await _clearpass_get_request(
        "/api/enforcement-policy",
        params,
        limit=safe_limit,
        offset=0,
    )
    if "data" in out:
        items = _extract_items(out["data"])
        out["enforcement_policies"] = bound_collection_response(
            items, limit=safe_limit, offset=0
        )
        if isinstance(out["enforcement_policies"], dict):
            out["enforcement_policies"]["server_offset"] = max(0, offset)
        del out["data"]
    return out


@mcp.tool(annotations=READ_ONLY)
async def clearpass_get_enforcement_policy(name: str) -> dict[str, Any]:
    """Get one ClearPass enforcement policy by name.

    Uses `GET /api/enforcement-policy/name/{name}`.
    """
    out = await _clearpass_get_request(
        f"/api/enforcement-policy/name/{_path_segment(name)}",
        bound=False,
    )
    if "data" in out:
        out["enforcement_policy"] = out.pop("data")
    return out


# ---------------------------------------------------------------------------
# Service management
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def clearpass_list_services(
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """List ClearPass authentication services with bounded pagination.

    Uses `GET /api/config/service`. Returns id, name, type, status, and
    order for each service.
    """
    safe_limit = clamp_limit(limit, default=25)
    params: dict[str, Any] = {
        "offset": max(0, offset),
        "limit": safe_limit,
        "calculate_count": "false",
    }
    out = await _clearpass_get_request(
        "/api/config/service",
        params,
        limit=safe_limit,
        offset=0,
    )
    if "data" in out:
        items = _extract_items(out["data"])
        out["services"] = bound_collection_response(items, limit=safe_limit, offset=0)
        if isinstance(out["services"], dict):
            out["services"]["server_offset"] = max(0, offset)
        del out["data"]
    return out


@mcp.tool(annotations=READ_ONLY)
async def clearpass_get_service(name: str) -> dict[str, Any]:
    """Get one ClearPass authentication service by name.

    Uses `GET /api/config/service/name/{services_name}`.
    """
    out = await _clearpass_get_request(
        f"/api/config/service/name/{_path_segment(name)}",
        bound=False,
    )
    if "data" in out:
        out["service"] = out.pop("data")
    return out


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def clearpass_set_service_enabled(
    name: str,
    enabled: bool,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Enable or disable a ClearPass authentication service by name.

    Uses `PATCH /api/config/service/name/{services_name}/enable` or
    `.../disable` based on `enabled`. Defaults to `dry_run=True`;
    execution requires `dry_run=False` and `confirm=True`.
    """
    action = "enable" if enabled else "disable"
    return await _clearpass_write_request(
        "PATCH",
        f"/api/config/service/name/{_path_segment(name)}/{action}",
        dry_run=dry_run,
        confirm=confirm,
    )


# ---------------------------------------------------------------------------
# Syslog / export
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def clearpass_list_syslog_targets(
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """List ClearPass syslog targets with bounded pagination.

    Uses `GET /api/syslog-target`. Returns id, host-address, port, and
    protocol for each syslog target.
    """
    safe_limit = clamp_limit(limit, default=25)
    params: dict[str, Any] = {
        "offset": max(0, offset),
        "limit": safe_limit,
        "calculate_count": "false",
    }
    out = await _clearpass_get_request(
        "/api/syslog-target",
        params,
        limit=safe_limit,
        offset=0,
    )
    if "data" in out:
        items = _extract_items(out["data"])
        out["syslog_targets"] = bound_collection_response(items, limit=safe_limit, offset=0)
        if isinstance(out["syslog_targets"], dict):
            out["syslog_targets"]["server_offset"] = max(0, offset)
        del out["data"]
    return out


@mcp.tool(annotations=READ_ONLY)
async def clearpass_list_syslog_export_filters(
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """List ClearPass syslog export filters with bounded pagination.

    Uses `GET /api/syslog-export-filter`. Returns id, name, and enabled
    status for each export filter.
    """
    safe_limit = clamp_limit(limit, default=25)
    params: dict[str, Any] = {
        "offset": max(0, offset),
        "limit": safe_limit,
        "calculate_count": "false",
    }
    out = await _clearpass_get_request(
        "/api/syslog-export-filter",
        params,
        limit=safe_limit,
        offset=0,
    )
    if "data" in out:
        items = _extract_items(out["data"])
        out["syslog_export_filters"] = bound_collection_response(
            items, limit=safe_limit, offset=0
        )
        if isinstance(out["syslog_export_filters"], dict):
            out["syslog_export_filters"]["server_offset"] = max(0, offset)
        del out["data"]
    return out


# ---------------------------------------------------------------------------
# Diagnostic / server info
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def clearpass_get_server_version() -> dict[str, Any]:
    """Get the ClearPass server version.

    Uses `GET /api/server/version`. Returns the server version string and
    any additional metadata reported by the API.
    """
    out = await _clearpass_get_request("/api/server/version", bound=False)
    if "data" in out:
        out["version"] = out.pop("data")
    return out


@mcp.tool(annotations=READ_ONLY)
async def clearpass_list_cluster_servers(
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """List ClearPass cluster server nodes with bounded pagination.

    Uses `GET /api/cluster/server`. Returns compact server UUID, name,
    and role information for each node in the cluster.
    """
    safe_limit = clamp_limit(limit, default=25)
    params: dict[str, Any] = {
        "offset": max(0, offset),
        "limit": safe_limit,
        "calculate_count": "false",
    }
    out = await _clearpass_get_request(
        "/api/cluster/server",
        params,
        limit=safe_limit,
        offset=0,
    )
    if "data" in out:
        items = _extract_items(out["data"])
        out["cluster_servers"] = bound_collection_response(items, limit=safe_limit, offset=0)
        if isinstance(out["cluster_servers"], dict):
            out["cluster_servers"]["server_offset"] = max(0, offset)
        del out["data"]
    return out


# ---------------------------------------------------------------------------
# Generated OpenAPI tools (see src/hpe_networking_mcp/mcp_servers/openapi_gen). The
# committed manifest
# at src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/clearpass.json is a derived operation
# manifest built from the current ClearPass 6.12.x OpenAPI definitions published
# on the Aruba developer portal (ReadMe api-registry). It exposes every
# documented CPPM operation as a directly-callable, typed MCPServer tool. Reads
# reuse the static-bearer auth; writes/destructive ops stay behind the product
# write gate + dry-run/confirm. Registration is guarded by
# HPE_MCP_CLEARPASS_GENERATED_TOOLS (defaults ON when the manifest exists).
# ---------------------------------------------------------------------------

# ClearPass REST is served under /api; committed manifest paths omit that prefix.
_CLEARPASS_API_PREFIX = "/api"


def _clearpass_generated_prepare(path: str) -> tuple[str | None, str | None, str | None]:
    """Return (base_url, token, error) for a generated ClearPass request path."""
    base_url, token = _clearpass_config()
    if not base_url or not token:
        return None, None, (
            "ClearPass not configured. Set CLEARPASS_BASE_URL and CLEARPASS_API_TOKEN."
        )
    if not path.startswith("/"):
        return None, None, "Generated path must be absolute."
    try:
        base_url = validate_product_base_url(base_url, product="ClearPass")
    except ValueError as exc:
        return None, None, str(exc)
    return base_url, token, None


def _clearpass_generated_headers(token: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/json"}
    for key, value in (extra or {}).items():
        if key.strip().lower() in {"authorization", "cookie"}:
            continue
        headers[key] = value
    headers["Authorization"] = _auth_header(token)
    return headers


async def _clearpass_generated_read(
    method: str,
    path: str,
    query: dict[str, Any],
    headers: dict[str, str],
    body: Any = None,
    content_type: str = "application/json",
) -> dict[str, Any]:
    """Read executor for generated ClearPass tools (bounded, direct)."""
    base_url, token, error = _clearpass_generated_prepare(path)
    if error:
        return {"error": error}
    url = f"{base_url}{_CLEARPASS_API_PREFIX}{path}"
    req_headers = _clearpass_generated_headers(token, headers)
    clean_params = {k: v for k, v in query.items() if v is not None}
    request_kwargs: dict[str, Any] = {
        "headers": req_headers,
        "params": clean_params,
    }
    body_error = apply_request_body(request_kwargs, req_headers, body, content_type)
    if body_error is not None:
        return body_error
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(method, url, **request_kwargs)
        payload = redact_sensitive(bound_collection_response(
            bounded_response_payload(resp), limit=clamp_limit(None), offset=0
        ))
        return {"status_code": resp.status_code, "data": payload, "url": url}
    except httpx.HTTPError as exc:
        return {"error": str(exc), "url": url}


async def _clearpass_generated_write(
    name: str,
    method: str,
    path: str,
    query: dict[str, Any],
    headers: dict[str, str],
    body: Any,
    content_type: str,
    dry_run: bool,
    confirm: bool,
) -> dict[str, Any]:
    """Write executor for generated ClearPass tools (gate + dry-run/confirm)."""
    if not optional_product_writes_allowed():
        return optional_product_write_blocked(name)
    base_url, token, error = _clearpass_generated_prepare(path)
    if error:
        return {"error": error}
    url = f"{base_url}{_CLEARPASS_API_PREFIX}{path}"
    clean_params = {k: v for k, v in query.items() if v is not None}
    preview: dict[str, Any] = {
        "method": method,
        "path": f"{_CLEARPASS_API_PREFIX}{path}",
        "url": url,
        "params": redact_sensitive(clean_params),
        "json": redact_sensitive(body),
        "content_type": content_type,
    }
    if dry_run:
        return {"dry_run": True, **preview, "execute_hint": _EXECUTE_HINT}
    if not confirm:
        return {
            "error": "confirm=True is required when dry_run=False.",
            "dry_run": True,
            **preview,
        }
    req_headers = _clearpass_generated_headers(token, headers)
    kwargs: dict[str, Any] = {"headers": req_headers, "params": clean_params}
    if body is not None:
        if content_type == "application/json":
            kwargs["json"] = body
        elif content_type.endswith("json"):
            kwargs["content"] = json.dumps(body)
            req_headers["Content-Type"] = content_type
        elif content_type == "application/x-www-form-urlencoded":
            if not isinstance(body, dict):
                return {"error": "form-urlencoded body must be an object of form fields"}
            kwargs["data"] = body
        else:
            kwargs["content"] = body if isinstance(body, (bytes, str)) else str(body)
            req_headers.setdefault("Content-Type", content_type)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(method, url, **kwargs)
        return {
            "status_code": resp.status_code,
            "data": redact_sensitive(bounded_response_payload(resp)),
            "url": url,
        }
    except httpx.HTTPError as exc:
        return {"error": str(exc), "url": url}


def _register_generated_clearpass_tools() -> list[str]:
    """Register generated ClearPass tools at import time, failing on manifest errors."""
    from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import load_manifest
    from hpe_networking_mcp.mcp_servers.openapi_gen.runtime import register_generated_tools

    manifest = load_manifest("clearpass")
    filtered = {
        **manifest,
        "operations": [
            operation
            for operation in manifest.get("operations", [])
            if operation.get("path") != "/oauth"
        ],
    }
    return register_generated_tools(
        mcp,
        "clearpass",
        read_executor=_clearpass_generated_read,
        write_executor=_clearpass_generated_write,
        manifest=filtered,
    )


GENERATED_CLEARPASS_TOOLS = _register_generated_clearpass_tools()


if __name__ == "__main__":
    from hpe_networking_mcp.mcp_servers._cache_hygiene import stable_list_tools
    from hpe_networking_mcp.mcp_servers._middleware import (
        NullStripMiddleware,
        PIITokenizeMiddleware,
        RateLimitMiddleware,
        SecretTokenizeMiddleware,
        install_middleware,
    )
    from hpe_networking_mcp.mcp_servers.shared import run_server

    stable_list_tools(mcp)
    install_middleware(
        mcp,
        [
            NullStripMiddleware(),
            RateLimitMiddleware(rate=8.0),
            SecretTokenizeMiddleware(),
            # PII tokenization: this backend's visitor/guest tools carry
            # email/phone/company_name -- see pii_tokenizer.py docstring.
            PIITokenizeMiddleware(),
        ],
    )
    run_server(mcp)
