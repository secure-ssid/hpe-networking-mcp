"""MCP server - optional HPE Aruba UXI backend (24 curated + 25 generated OpenAPI tools).

Enabled via tool router env:
  HPE_MCP_PRODUCTS=uxi

Auth/env:
  UXI_CLIENT_ID      GreenLake OAuth2 client ID
  UXI_CLIENT_SECRET  GreenLake OAuth2 client secret
  UXI_BASE_URL       optional, defaults to HPE UXI v1alpha1 API
  UXI_TOKEN_URL      optional, defaults to HPE GreenLake SSO token URL

Rate limiting: the UXI v1alpha1 API is documented as a 5 requests/second
ceiling per tenant. Every outbound HTTP call this module makes (token fetch,
reads, and writes) is throttled through a dedicated 5 req/s token-bucket
limiter (`_UXI_RATE_LIMITER`), independent of whatever MCP-tool-call rate
limit the hosting server (direct stdio run or the shared tool router) also
applies — so this cap holds even when the router's own limiter is looser.

Writes: create/update/delete for groups, sensors, and agents, plus group
assignment/unassignment for sensors, agents, networks, and service tests, are
all gated behind `HPE_MCP_ACCESS_PROFILE=full-read-write` (or custom with
`HPE_MCP_PRODUCT_ACCESS=read-write`) and a `dry_run`
(default `True`) + `confirm` (required for execution) preview-first contract,
matching every other optional product backend in this repo. Write path/ID
shapes extend the already-confirmed read collection paths with the standard
REST `{collection}/{id}` convention; verify against a live UXI tenant before
relying on them for anything beyond a lab.
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.mcpserver import MCPServer

from hpe_networking_mcp.mcp_servers._middleware import RateLimitMiddleware
from hpe_networking_mcp.mcp_servers.openapi_gen.http_exec import apply_request_body
from hpe_networking_mcp.mcp_servers.shared import (
    DESTRUCTIVE,
    IDEMPOTENT_WRITE,
    READ_ONLY,
    WRITE,
    bound_collection_response,
    bounded_response_payload,
    clamp_limit,
    platform_write_blocked as _platform_write_blocked,
    platform_writes_allowed as _platform_writes_allowed,
    redact_sensitive,
    response_payload,
    safe_api_path,
    validate_product_base_url,
)

mcp = MCPServer("uxi-core")


def optional_product_writes_allowed() -> bool:
    return _platform_writes_allowed("uxi")


def optional_product_write_blocked(tool_name: str) -> dict[str, str]:
    return _platform_write_blocked("uxi", tool_name)


_DEFAULT_UXI_BASE_URL = "https://api.capenetworks.com/networking-uxi/v1alpha1"
_DEFAULT_TOKEN_URL = "https://sso.common.cloud.hpe.com/as/token.oauth2"
_LIST_PATHS = {
    "/agents",
    "/agent-group-assignments",
    "/groups",
    "/network-group-assignments",
    "/sensors",
    "/sensor-group-assignments",
    "/service-test-group-assignments",
    "/service-tests",
    "/wired-networks",
    "/wireless-networks",
}
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_EXECUTE_HINT = "Review the request, then call again with dry_run=False and confirm=True."
_TOKEN_CACHE: dict[str, Any] = {}
# Documented UXI API ceiling: 5 requests/second per tenant.
_UXI_RATE_LIMITER = RateLimitMiddleware(rate=5.0)


async def _uxi_throttle() -> None:
    """Block until the shared 5 req/s UXI token bucket has capacity."""
    await _UXI_RATE_LIMITER.before_call("uxi_http", {})


def _uxi_config() -> tuple[str | None, str | None, str, str]:
    import os

    client_id = os.getenv("UXI_CLIENT_ID", "").strip()
    client_secret = os.getenv("UXI_CLIENT_SECRET", "").strip()
    base_url = os.getenv("UXI_BASE_URL", _DEFAULT_UXI_BASE_URL).strip().rstrip("/")
    token_url = os.getenv("UXI_TOKEN_URL", _DEFAULT_TOKEN_URL).strip()
    return client_id or None, client_secret or None, base_url, token_url


def _uxi_limit(limit: int | None) -> int:
    return min(clamp_limit(limit, default=50), 100)


def _cursor_params(next_cursor: str | None, page_size: int) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": _uxi_limit(page_size)}
    if next_cursor:
        params["next"] = next_cursor
    return params


def _path_segment(value: str) -> str:
    text = str(value).strip()
    if not text or len(text) > 128 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in text):
        raise ValueError("UXI resource IDs must be 1-128 ASCII letters, numbers, underscores, or dashes.")
    return quote(text, safe="")


def _safe_uxi_path(path: str) -> str:
    safe_path = safe_api_path(path, ("/",))
    if safe_path in _LIST_PATHS:
        return quote(safe_path, safe="/")
    parts = safe_path.strip("/").split("/")
    if len(parts) == 3 and parts[0] == "sensors" and parts[2] == "status":
        return f"/sensors/{_path_segment(parts[1])}/status"
    allowed = ", ".join(sorted(_LIST_PATHS))
    raise ValueError(f"path must be one of: {allowed}, or /sensors/{{id}}/status")


def _safe_uxi_write_path(method: str, path: str) -> str:
    """Validate an exact method/path combination from the current UXI spec."""
    safe_path = safe_api_path(path, ("/",))
    assignment_collections = {
        "/agent-group-assignments",
        "/network-group-assignments",
        "/sensor-group-assignments",
        "/service-test-group-assignments",
    }
    if method == "POST" and safe_path in {"/groups", *assignment_collections}:
        return quote(safe_path, safe="/")
    parts = safe_path.strip("/").split("/")
    collection = f"/{parts[0]}" if len(parts) == 2 else ""
    if method == "PATCH" and collection in {"/agents", "/groups", "/sensors"}:
        return f"{collection}/{_path_segment(parts[1])}"
    if method == "DELETE" and collection in {
        "/agents",
        "/groups",
        *assignment_collections,
    }:
        return f"{collection}/{_path_segment(parts[1])}"
    raise ValueError(
        "path must be one of the method/path combinations documented by the "
        f"UXI v1alpha1 specification; {method} {safe_path} is not"
    )


def _pick(record: Any, fields: tuple[str, ...]) -> Any:
    if not isinstance(record, dict):
        return record
    return {field: record[field] for field in fields if field in record and record[field] not in (None, "")}


def _compact_items(data: Any, fields: tuple[str, ...] | None) -> Any:
    if not fields:
        return data
    if isinstance(data, list):
        return [_pick(item, fields) for item in data]
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        out = dict(data)
        out["items"] = [_pick(item, fields) for item in data["items"]]
        return out
    return _pick(data, fields)


async def _uxi_access_token(client_id: str, client_secret: str, token_url: str) -> str:
    token_url = validate_product_base_url(token_url, product="UXI token URL")
    now = time.time()
    cache_key = f"{token_url}:{client_id}"
    if (
        _TOKEN_CACHE.get("key") == cache_key
        and _TOKEN_CACHE.get("token")
        and float(_TOKEN_CACHE.get("expires_at", 0)) > now + 60
    ):
        return str(_TOKEN_CACHE["token"])

    await _uxi_throttle()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    payload = response_payload(resp)
    if resp.status_code >= 400:
        raise RuntimeError(f"UXI token request failed with HTTP {resp.status_code}: {redact_sensitive(payload)}")
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise RuntimeError("UXI token response did not include access_token.")
    expires_in = int(payload.get("expires_in") or 3600)
    _TOKEN_CACHE.update(
        {
            "key": cache_key,
            "token": payload["access_token"],
            "expires_at": now + max(60, expires_in),
        }
    )
    return str(payload["access_token"])


async def _uxi_get_request(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    limit: int = 50,
    offset: int = 0,
    fields: tuple[str, ...] | None = None,
    bound: bool = True,
) -> dict[str, Any]:
    client_id, client_secret, base_url, token_url = _uxi_config()
    if not client_id or not client_secret:
        return {"error": "UXI not configured. Set UXI_CLIENT_ID and UXI_CLIENT_SECRET."}
    try:
        base_url = validate_product_base_url(base_url, product="UXI")
        safe_path = _safe_uxi_path(path)
    except ValueError as exc:
        return {"error": str(exc)}

    try:
        token = await _uxi_access_token(client_id, client_secret, token_url)
        url = f"{base_url}{safe_path}"
        clean_params = {key: value for key, value in (params or {}).items() if value is not None}
        await _uxi_throttle()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                url,
                headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
                params=clean_params,
            )
        payload = response_payload(resp)
        payload = _compact_items(payload, fields)
        if bound:
            payload = bound_collection_response(payload, limit=limit, offset=offset)
        return {"status_code": resp.status_code, "data": payload, "url": url}
    except httpx.HTTPError as exc:
        return {"error": f"{type(exc).__name__}: connection or protocol error", "url": f"{base_url}{path}"}
    except Exception as exc:
        return {"error": str(exc), "url": f"{base_url}{path}"}


async def _uxi_write_request(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | list[Any] | None = None,
    *,
    dry_run: bool = True,
    confirm: bool = False,
    tool_name: str = "uxi_write",
) -> dict[str, Any]:
    if not optional_product_writes_allowed():
        return optional_product_write_blocked(tool_name)

    method = method.upper()
    if method not in _WRITE_METHODS:
        return {"error": f"method must be one of: {', '.join(sorted(_WRITE_METHODS))}"}

    client_id, client_secret, base_url, token_url = _uxi_config()
    if not client_id or not client_secret:
        return {"error": "UXI not configured. Set UXI_CLIENT_ID and UXI_CLIENT_SECRET."}
    try:
        base_url = validate_product_base_url(base_url, product="UXI")
        safe_path = _safe_uxi_write_path(method, path)
    except ValueError as exc:
        return {"error": str(exc)}

    url = f"{base_url}{safe_path}"
    preview = {
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

    try:
        token = await _uxi_access_token(client_id, client_secret, token_url)
        await _uxi_throttle()
        request_headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
        }
        if method == "PATCH":
            request_headers["Content-Type"] = "application/merge-patch+json"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method,
                url,
                headers=request_headers,
                params=params or {},
                json=body,
            )
        return {
            "status_code": resp.status_code,
            "data": redact_sensitive(bounded_response_payload(resp)),
            "url": url,
        }
    except httpx.HTTPError as exc:
        return {"error": f"{type(exc).__name__}: connection or protocol error", "url": url}
    except Exception as exc:
        return {"error": str(exc), "url": url}


@mcp.tool(annotations=READ_ONLY)
def uxi_status() -> dict[str, Any]:
    """Report whether the optional UXI backend has OAuth credentials configured."""
    client_id, client_secret, base_url, token_url = _uxi_config()
    return {
        "configured": bool(client_id and client_secret),
        "has_client_id": bool(client_id),
        "has_client_secret": bool(client_secret),
        "base_url": base_url,
        "token_url": token_url,
        "writes_allowed": optional_product_writes_allowed(),
        "rate_limit_per_second": _UXI_RATE_LIMITER.rate,
        "tools": [
            "uxi_get",
            "uxi_list_sensors",
            "uxi_get_sensor_status",
            "uxi_list_agents",
            "uxi_list_groups",
            "uxi_list_wired_networks",
            "uxi_list_wireless_networks",
            "uxi_list_service_tests",
            "uxi_list_agent_group_assignments",
            "uxi_list_sensor_group_assignments",
            "uxi_list_network_group_assignments",
            "uxi_list_service_test_group_assignments",
            "uxi_write",
            "uxi_create_group",
            "uxi_update_group",
            "uxi_delete_group",
            "uxi_update_sensor",
            "uxi_update_agent",
            "uxi_delete_agent",
            "uxi_assign_sensor_to_group",
            "uxi_assign_agent_to_group",
            "uxi_assign_network_to_group",
            "uxi_assign_service_test_to_group",
        ],
    }


@mcp.tool(annotations=READ_ONLY)
async def uxi_get(
    path: str,
    params: dict[str, Any] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Perform a guarded read-only GET against selected UXI v1alpha1 paths.

    List payloads are bounded with `limit` and `offset`.
    """
    return await _uxi_get_request(path, params, limit=limit, offset=offset)


@mcp.tool(annotations=READ_ONLY)
async def uxi_list_sensors(next_cursor: str | None = None, page_size: int = 50) -> dict[str, Any]:
    """List UXI sensors with compact identity, model, MAC, group, and location fields."""
    return await _uxi_get_request(
        "/sensors",
        _cursor_params(next_cursor, page_size),
        limit=page_size,
        fields=(
            "id",
            "name",
            "serial",
            "type",
            "modelNumber",
            "ethernetMacAddress",
            "wifiMacAddress",
            "groupName",
            "groupPath",
            "latitude",
            "longitude",
            "addressNote",
            "notes",
            "pcapMode",
        ),
    )


@mcp.tool(annotations=READ_ONLY)
async def uxi_get_sensor_status(sensor_id: str) -> dict[str, Any]:
    """Get online/testing status and active issues for a UXI sensor."""
    try:
        path = f"/sensors/{_path_segment(sensor_id)}/status"
    except ValueError as exc:
        return {"error": str(exc)}
    return await _uxi_get_request(path, fields=("isOnline", "isTesting", "issues"), bound=False)


@mcp.tool(annotations=READ_ONLY)
async def uxi_list_agents(next_cursor: str | None = None, page_size: int = 50) -> dict[str, Any]:
    """List UXI agents with compact identity, model, MAC, group, and notes fields."""
    return await _uxi_get_request(
        "/agents",
        _cursor_params(next_cursor, page_size),
        limit=page_size,
        fields=(
            "id",
            "name",
            "serial",
            "type",
            "modelNumber",
            "ethernetMacAddress",
            "wifiMacAddress",
            "groupName",
            "groupPath",
            "notes",
            "pcapMode",
        ),
    )


@mcp.tool(annotations=READ_ONLY)
async def uxi_list_groups(next_cursor: str | None = None, page_size: int = 50) -> dict[str, Any]:
    """List UXI groups with id, name, path, and parent group."""
    return await _uxi_get_request(
        "/groups",
        _cursor_params(next_cursor, page_size),
        limit=page_size,
        fields=("id", "name", "path", "parent"),
    )


@mcp.tool(annotations=READ_ONLY)
async def uxi_list_wired_networks(next_cursor: str | None = None, page_size: int = 50) -> dict[str, Any]:
    """List UXI wired networks."""
    return await _uxi_get_request(
        "/wired-networks",
        _cursor_params(next_cursor, page_size),
        limit=page_size,
        fields=(
            "id",
            "name",
            "type",
            "security",
            "vLanId",
            "ipVersion",
            "externalConnectivity",
            "dnsLookupDomain",
            "disableEdns",
            "useDns64",
            "createdAt",
            "updatedAt",
        ),
    )


@mcp.tool(annotations=READ_ONLY)
async def uxi_list_wireless_networks(next_cursor: str | None = None, page_size: int = 50) -> dict[str, Any]:
    """List UXI wireless networks."""
    return await _uxi_get_request(
        "/wireless-networks",
        _cursor_params(next_cursor, page_size),
        limit=page_size,
        fields=(
            "id",
            "name",
            "type",
            "ssid",
            "security",
            "hidden",
            "ipVersion",
            "externalConnectivity",
            "dnsLookupDomain",
            "disableEdns",
            "useDns64",
            "createdAt",
            "updatedAt",
        ),
    )


@mcp.tool(annotations=READ_ONLY)
async def uxi_list_service_tests(next_cursor: str | None = None, page_size: int = 50) -> dict[str, Any]:
    """List UXI service tests."""
    return await _uxi_get_request(
        "/service-tests",
        _cursor_params(next_cursor, page_size),
        limit=page_size,
        fields=("id", "name", "type", "category", "target", "template", "isEnabled"),
    )


async def _uxi_list_assignments(path: str, next_cursor: str | None, page_size: int) -> dict[str, Any]:
    return await _uxi_get_request(
        path,
        _cursor_params(next_cursor, page_size),
        limit=page_size,
        fields=("id", "type", "agent", "sensor", "network", "serviceTest", "group"),
    )


@mcp.tool(annotations=READ_ONLY)
async def uxi_list_agent_group_assignments(
    next_cursor: str | None = None,
    page_size: int = 50,
) -> dict[str, Any]:
    """List UXI agent-to-group assignments."""
    return await _uxi_list_assignments("/agent-group-assignments", next_cursor, page_size)


@mcp.tool(annotations=READ_ONLY)
async def uxi_list_sensor_group_assignments(
    next_cursor: str | None = None,
    page_size: int = 50,
) -> dict[str, Any]:
    """List UXI sensor-to-group assignments."""
    return await _uxi_list_assignments("/sensor-group-assignments", next_cursor, page_size)


@mcp.tool(annotations=READ_ONLY)
async def uxi_list_network_group_assignments(
    next_cursor: str | None = None,
    page_size: int = 50,
) -> dict[str, Any]:
    """List UXI network-to-group assignments."""
    return await _uxi_list_assignments("/network-group-assignments", next_cursor, page_size)


@mcp.tool(annotations=READ_ONLY)
async def uxi_list_service_test_group_assignments(
    next_cursor: str | None = None,
    page_size: int = 50,
) -> dict[str, Any]:
    """List UXI service-test-to-group assignments."""
    return await _uxi_list_assignments("/service-test-group-assignments", next_cursor, page_size)


@mcp.tool(annotations=DESTRUCTIVE)
async def uxi_write(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | list[Any] | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Perform a guarded write request against a writable UXI v1alpha1 path.

    Allows only method/path combinations present in the current UXI v1alpha1
    specification. Defaults to
    `dry_run=True`; execution requires `dry_run=False` and `confirm=True`,
    and is throttled to the documented 5 req/s UXI API ceiling.
    """
    return await _uxi_write_request(
        method,
        path,
        params,
        body,
        dry_run=dry_run,
        confirm=confirm,
        tool_name="uxi_write",
    )


@mcp.tool(annotations=WRITE)
async def uxi_create_group(
    name: str,
    parent: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create a UXI group; requires full-read-write or custom UXI write access."""
    body: dict[str, Any] = {"name": name}
    if parent is not None:
        body["parentId"] = parent
    return await _uxi_write_request(
        "POST", "/groups", body=body, dry_run=dry_run, confirm=confirm, tool_name="uxi_create_group"
    )


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def uxi_update_group(
    group_id: str,
    name: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Update a UXI group's name by ID; requires read-write access."""
    try:
        path = f"/groups/{_path_segment(group_id)}"
    except ValueError as exc:
        return {"error": str(exc)}
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if not body:
        return {"error": "Provide name to update."}
    return await _uxi_write_request(
        "PATCH", path, body=body, dry_run=dry_run, confirm=confirm, tool_name="uxi_update_group"
    )


@mcp.tool(annotations=DESTRUCTIVE)
async def uxi_delete_group(
    group_id: str,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Delete a UXI group by ID; requires read-write access."""
    try:
        path = f"/groups/{_path_segment(group_id)}"
    except ValueError as exc:
        return {"error": str(exc)}
    return await _uxi_write_request(
        "DELETE", path, dry_run=dry_run, confirm=confirm, tool_name="uxi_delete_group"
    )


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def uxi_update_sensor(
    sensor_id: str,
    patch: dict[str, Any],
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Patch a UXI sensor's editable fields (e.g. notes, addressNote) by ID."""
    try:
        path = f"/sensors/{_path_segment(sensor_id)}"
    except ValueError as exc:
        return {"error": str(exc)}
    if not isinstance(patch, dict) or not patch:
        return {"error": "patch must be a non-empty object"}
    return await _uxi_write_request(
        "PATCH", path, body=patch, dry_run=dry_run, confirm=confirm, tool_name="uxi_update_sensor"
    )


async def uxi_delete_sensor(
    sensor_id: str,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Return an explicit error because the current UXI API has no sensor DELETE."""
    return {
        "error": (
            "The current UXI v6.7 API does not expose DELETE /sensors/{id}; "
            "update the sensor state with uxi_update_sensor instead."
        )
    }


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def uxi_update_agent(
    agent_id: str,
    patch: dict[str, Any],
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Patch a UXI agent's editable fields (e.g. notes) by ID."""
    try:
        path = f"/agents/{_path_segment(agent_id)}"
    except ValueError as exc:
        return {"error": str(exc)}
    if not isinstance(patch, dict) or not patch:
        return {"error": "patch must be a non-empty object"}
    return await _uxi_write_request(
        "PATCH", path, body=patch, dry_run=dry_run, confirm=confirm, tool_name="uxi_update_agent"
    )


@mcp.tool(annotations=DESTRUCTIVE)
async def uxi_delete_agent(
    agent_id: str,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Delete/decommission a UXI agent by ID; requires read-write access."""
    try:
        path = f"/agents/{_path_segment(agent_id)}"
    except ValueError as exc:
        return {"error": str(exc)}
    return await _uxi_write_request(
        "DELETE", path, dry_run=dry_run, confirm=confirm, tool_name="uxi_delete_agent"
    )


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def uxi_assign_sensor_to_group(
    sensor_id: str,
    group_id: str,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Assign a UXI sensor to a group; requires read-write access."""
    return await _uxi_write_request(
        "POST",
        "/sensor-group-assignments",
        body={"sensorId": sensor_id, "groupId": group_id},
        dry_run=dry_run,
        confirm=confirm,
        tool_name="uxi_assign_sensor_to_group",
    )


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def uxi_assign_agent_to_group(
    agent_id: str,
    group_id: str,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Assign a UXI agent to a group; requires read-write access."""
    return await _uxi_write_request(
        "POST",
        "/agent-group-assignments",
        body={"agentId": agent_id, "groupId": group_id},
        dry_run=dry_run,
        confirm=confirm,
        tool_name="uxi_assign_agent_to_group",
    )


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def uxi_assign_network_to_group(
    network_id: str,
    group_id: str,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Assign a UXI wired/wireless network to a group; requires read-write access."""
    return await _uxi_write_request(
        "POST",
        "/network-group-assignments",
        body={"networkId": network_id, "groupId": group_id},
        dry_run=dry_run,
        confirm=confirm,
        tool_name="uxi_assign_network_to_group",
    )


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def uxi_assign_service_test_to_group(
    service_test_id: str,
    group_id: str,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Assign a UXI service test to a group; requires read-write access."""
    return await _uxi_write_request(
        "POST",
        "/service-test-group-assignments",
        body={"serviceTestId": service_test_id, "groupId": group_id},
        dry_run=dry_run,
        confirm=confirm,
        tool_name="uxi_assign_service_test_to_group",
    )


# ---------------------------------------------------------------------------
# Generated OpenAPI tools (see src/hpe_networking_mcp/mcp_servers/openapi_gen). The committed manifest
# at src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/uxi.json is derived from the current UXI
# v6.7 OpenAPI document published on the Aruba developer portal (ReadMe) and
# exposes every documented operation as a directly-callable, typed MCPServer tool.
# Every outbound call still flows through the OAuth token layer and the shared
# 5 req/s throttle; writes stay behind the product write gate + dry-run/confirm.
# Registration is guarded by HPE_MCP_UXI_GENERATED_TOOLS (defaults ON when
# the manifest exists).
# ---------------------------------------------------------------------------

# The committed manifest paths carry the full API prefix; the configured base
# URL usually already includes it, so strip it once to avoid double-prefixing.
_UXI_API_PREFIX = "/networking-uxi/v1alpha1"


def _uxi_generated_prepare(path: str) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Return (host, client_id, client_secret, token_url, error) for a generated path."""
    client_id, client_secret, base_url, token_url = _uxi_config()
    if not client_id or not client_secret:
        return None, None, None, None, "UXI not configured. Set UXI_CLIENT_ID and UXI_CLIENT_SECRET."
    if not path.startswith(_UXI_API_PREFIX + "/"):
        return None, None, None, None, f"Generated path must begin with {_UXI_API_PREFIX}/."
    try:
        base_url = validate_product_base_url(base_url, product="UXI")
    except ValueError as exc:
        return None, None, None, None, str(exc)
    # Manifest paths already include the API prefix; drop a duplicate suffix.
    host = base_url[: -len(_UXI_API_PREFIX)] if base_url.endswith(_UXI_API_PREFIX) else base_url
    return host, client_id, client_secret, token_url, None


async def _uxi_generated_read(
    method: str,
    path: str,
    query: dict[str, Any],
    headers: dict[str, str],
    body: Any = None,
    content_type: str = "application/json",
) -> dict[str, Any]:
    """Read executor for generated UXI tools (throttled, bounded, direct)."""
    host, client_id, client_secret, token_url, error = _uxi_generated_prepare(path)
    if error:
        return {"error": error}
    url = f"{host}{path}"
    clean_params = {k: v for k, v in query.items() if v is not None}
    try:
        token = await _uxi_access_token(client_id, client_secret, token_url)
        req_headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        for key, value in (headers or {}).items():
            if key.strip().lower() not in {"authorization", "cookie"}:
                req_headers[key] = value
        request_kwargs: dict[str, Any] = {
            "headers": req_headers,
            "params": clean_params,
        }
        body_error = apply_request_body(request_kwargs, req_headers, body, content_type)
        if body_error is not None:
            return body_error
        await _uxi_throttle()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(method, url, **request_kwargs)
        payload = redact_sensitive(bound_collection_response(
            bounded_response_payload(resp), limit=clamp_limit(None), offset=0
        ))
        return {"status_code": resp.status_code, "data": payload, "url": url}
    except httpx.HTTPError:
        return {"error": "connection or protocol error", "url": url}
    except Exception as exc:
        return {"error": str(exc), "url": url}


async def _uxi_generated_write(
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
    """Write executor for generated UXI tools (gate + dry-run/confirm, throttled)."""
    if not optional_product_writes_allowed():
        return optional_product_write_blocked(name)
    host, client_id, client_secret, token_url, error = _uxi_generated_prepare(path)
    if error:
        return {"error": error}
    url = f"{host}{path}"
    clean_params = {k: v for k, v in query.items() if v is not None}
    preview: dict[str, Any] = {
        "method": method,
        "path": path,
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
    try:
        token = await _uxi_access_token(client_id, client_secret, token_url)
        req_headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        for key, value in (headers or {}).items():
            if key.strip().lower() not in {"authorization", "cookie"}:
                req_headers[key] = value
        kwargs: dict[str, Any] = {"headers": req_headers, "params": clean_params}
        if body is not None:
            if content_type == "application/x-www-form-urlencoded":
                if not isinstance(body, dict):
                    return {"error": "form-urlencoded body must be an object of form fields"}
                kwargs["data"] = body
            elif content_type == "application/json":
                kwargs["json"] = body
            elif content_type.endswith("json"):
                # e.g. application/merge-patch+json — preserve the exact type.
                kwargs["content"] = json.dumps(body)
                req_headers["Content-Type"] = content_type
            else:
                kwargs["json"] = body
        await _uxi_throttle()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(method, url, **kwargs)
        return {
            "status_code": resp.status_code,
            "data": redact_sensitive(bounded_response_payload(resp)),
            "url": url,
        }
    except httpx.HTTPError:
        return {"error": "connection or protocol error", "url": url}
    except Exception as exc:
        return {"error": str(exc), "url": url}


def _register_generated_uxi_tools() -> list[str]:
    """Register generated UXI tools at import time, failing on manifest errors."""
    from hpe_networking_mcp.mcp_servers.openapi_gen.runtime import register_generated_tools

    return register_generated_tools(
        mcp,
        "uxi",
        read_executor=_uxi_generated_read,
        write_executor=_uxi_generated_write,
    )


GENERATED_UXI_TOOLS = _register_generated_uxi_tools()


if __name__ == "__main__":
    from hpe_networking_mcp.mcp_servers._cache_hygiene import stable_list_tools
    from hpe_networking_mcp.mcp_servers._middleware import (
        NullStripMiddleware,
        SecretTokenizeMiddleware,
        install_middleware,
    )

    stable_list_tools(mcp)
    # Tool-call-rate guard for the direct stdio server; the 5 req/s outbound
    # HTTP throttle in `_uxi_throttle()` is the real UXI API ceiling and
    # applies regardless of how this server is invoked (direct or router).
    install_middleware(
        mcp,
        [
            NullStripMiddleware(),
            RateLimitMiddleware(rate=5.0),
            SecretTokenizeMiddleware(),
        ],
    )
    from hpe_networking_mcp.mcp_servers.shared import run_server

    run_server(mcp)
