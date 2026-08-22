"""MCP server — optional HPE Juniper Apstra backend.

No authoritative distributable full Apstra OpenAPI spec is available, so the
committed manifest is a *derived operation manifest* from pinned official
Juniper SDK endpoint mappings (NOT full OpenAPI coverage). Auth endpoints are
documented for provenance only and are never exposed as tools.

Enabled via tool router env:
  HPE_MCP_PRODUCTS=apstra

Auth/env:
  APSTRA_BASE_URL   e.g. https://apstra.example.com
  APSTRA_USERNAME   session login username (preferred — see apstra_login)
  APSTRA_PASSWORD   session login password (preferred — see apstra_login)
  APSTRA_API_TOKEN  pre-issued static AuthToken (skips session login when set)

Auth model: current Apstra's documented login flow is `POST /api/aaa/login` with a
JSON `{"username", "password"}` body, returning a token that must be sent as
an `AuthToken` header (not `Authorization: Bearer`) on every subsequent call.
Older supported releases can expose `/api/user/login`; this module falls back
to that path only when the current endpoint responds 404/405. The token is
cached per base URL and refreshed on a 401 response. Set `APSTRA_API_TOKEN` to
bypass session login with a token obtained out of band.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.mcpserver import MCPServer

from hpe_networking_mcp.mcp_servers.shared import (
    DESTRUCTIVE,
    DIAGNOSTIC,
    IDEMPOTENT_WRITE,
    READ_ONLY,
    bound_collection_response,
    bounded_response_payload,
    clamp_limit,
    compact_http_error,
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
from hpe_networking_mcp.pipeline.clients.http_retry import get_with_retry
from hpe_networking_mcp.pipeline.clients.pooled_clients import pooled_client

mcp = MCPServer("apstra-core")


def optional_product_writes_allowed() -> bool:
    return _platform_writes_allowed("apstra")


def optional_product_write_blocked(tool_name: str) -> dict[str, str]:
    return _platform_write_blocked("apstra", tool_name)


_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_EXECUTE_HINT = "Review the request, then call again with dry_run=False and confirm=True."

# Current Apstra 6.1 SDK endpoint first; older SDK releases use /api/user/login.
_LOGIN_ENDPOINTS = ("/api/aaa/login", "/api/user/login")

# base_url -> {"token", "endpoint", "legacy_fallback", "obtained_at"}
_TOKEN_CACHE: dict[str, dict[str, Any]] = {}
_BLUEPRINT_FIELDS = (
    "id",
    "label",
    "name",
    "status",
    "state",
    "role",
    "version",
    "design",
    "reference_design",
)
_TEMPLATE_FIELDS = (
    "id",
    "label",
    "name",
    "display_name",
    "type",
    "design",
    "reference_design",
    "version",
    "description",
    "tags",
)
_ANOMALY_FIELDS = (
    "id",
    "identity",
    "type",
    "severity",
    "role",
    "count",
    "description",
    "acknowledged",
)
_RACK_FIELDS = (
    "id",
    "label",
    "name",
    "description",
    "rack_type",
    "template_name",
    "fabric_connectivity_design",
    "leaf_count",
    "spine_count",
    "systems_count",
    "tags",
)
_ROUTING_ZONE_FIELDS = (
    "id",
    "label",
    "name",
    "description",
    "vni",
    "vlan_id",
    "vrf_name",
    "sz_type",
    "routing_policy",
    "rt_policy",
)
_VIRTUAL_NETWORK_FIELDS = (
    "id",
    "label",
    "name",
    "vn_type",
    "security_zone_id",
    "virtual_gateway_ipv4",
    "ipv4_subnet",
    "virtual_gateway_ipv6",
    "ipv6_subnet",
    "vni",
    "vni_id",
    "reserved_vlan_id",
    "dhcp_service",
    "bound_to",
)
_REMOTE_GATEWAY_FIELDS = (
    "id",
    "label",
    "name",
    "gw_name",
    "gw_ip",
    "gw_asn",
    "local_gw_nodes",
    "evpn_route_types",
    "evpn_interconnect_group_id",
    "status",
    "state",
)
_CONNECTIVITY_TEMPLATE_FIELDS = (
    "id",
    "label",
    "name",
    "description",
    "policy_type",
    "visible",
    "used",
    "assigned",
    "tags",
    "status",
    "state",
)
_APPLICATION_ENDPOINT_FIELDS = (
    "id",
    "label",
    "name",
    "system_id",
    "system_label",
    "node_id",
    "interface_id",
    "interface_name",
    "if_name",
    "port_channel_id",
    "lag_id",
    "policy_id",
    "policy_label",
    "assigned",
    "tags",
)
_DIFF_STATUS_FIELDS = (
    "status",
    "state",
    "staging_version",
    "active_version",
    "deployed",
    "has_uncommitted_changes",
    "uncommitted_changes",
    "diff_summary",
    "warnings",
    "errors",
)
_PROTOCOL_SESSION_FIELDS = (
    "id",
    "label",
    "name",
    "protocol",
    "role",
    "local_system_id",
    "remote_system_id",
    "local_asn",
    "remote_asn",
    "local_ip",
    "remote_ip",
    "status",
    "state",
    "established",
    "last_change",
)
_SYSTEM_FIELDS = (
    "id",
    "system_id",
    "label",
    "name",
    "hostname",
    "role",
    "system_type",
    "device_key",
    "status",
    "state",
    "deploy_status",
    "management_ip",
    "ip_address",
    "asn",
    "model",
    "serial_number",
)


def _apstra_credentials() -> tuple[str | None, str | None, str | None, str | None]:
    """Return (base_url, username, password, static_token) from the environment."""
    import os

    base_url = os.getenv("APSTRA_BASE_URL", "").strip().rstrip("/")
    username = os.getenv("APSTRA_USERNAME", "").strip()
    password = os.getenv("APSTRA_PASSWORD", "").strip()
    static_token = os.getenv("APSTRA_API_TOKEN", "").strip()
    return (base_url or None, username or None, password or None, static_token or None)


def _path_segment(value: str) -> str:
    return quote(value, safe="")


def _extract_apstra_token(payload: Any) -> str | None:
    """Pull the AuthToken value out of an Apstra login response body.

    Current `/api/aaa/login` responses are JSON objects with a `token` field.
    Older releases may return a bare JSON token string.
    """
    if isinstance(payload, str):
        text = payload.strip().strip('"')
        return text or None
    if isinstance(payload, dict):
        for key in ("token", "auth_token", "authtoken", "AuthToken"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return None


async def _apstra_login(
    client: httpx.AsyncClient, base_url: str, username: str, password: str
) -> dict[str, Any]:
    """Authenticate against Apstra, preferring the current login endpoint.

    Tries `POST /api/aaa/login` first. Only falls back to the older
    `POST /api/user/login` when the current instance answers 404/405 there.
    """
    body = {"username": username, "password": password}
    last_error: str | None = None
    for index, endpoint in enumerate(_LOGIN_ENDPOINTS):
        url = f"{base_url}{endpoint}"
        try:
            resp = await client.post(url, json=body, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            last_error = str(exc)
            continue
        if resp.status_code in (200, 201):
            token = _extract_apstra_token(response_payload(resp))
            if token:
                return {"token": token, "endpoint": endpoint, "legacy_fallback": index > 0}
            last_error = (
                f"Login succeeded ({resp.status_code}) "
                f"but no token in response at {endpoint}"
            )
            continue
        is_last = index == len(_LOGIN_ENDPOINTS) - 1
        if resp.status_code in (404, 405) and not is_last:
            last_error = compact_http_error(resp, endpoint)
            continue
        return {"error": compact_http_error(resp, endpoint), "endpoint": endpoint}
    return {"error": last_error or "Apstra login failed", "endpoint": _LOGIN_ENDPOINTS[-1]}


async def _get_apstra_token(
    base_url: str, username: str, password: str, *, force: bool = False
) -> dict[str, Any]:
    """Return a cached Apstra AuthToken, refreshing on expiry or when `force=True`."""
    cached = _TOKEN_CACHE.get(base_url)
    now = time.monotonic()
    if not force and cached:
        return {
            "token": cached["token"],
            "endpoint": cached["endpoint"],
            "legacy_fallback": cached["legacy_fallback"],
            "cached": True,
        }
    client = pooled_client("apstra")
    result = await _apstra_login(client, base_url, username, password)
    if "error" in result:
        return result
    _TOKEN_CACHE[base_url] = {
        "token": result["token"],
        "endpoint": result["endpoint"],
        "legacy_fallback": result["legacy_fallback"],
        "obtained_at": now,
    }
    return {**result, "cached": False}


async def _apstra_authenticated_request(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
) -> dict[str, Any]:
    """Perform one Apstra API call using the `AuthToken` header, with a single
    forced re-login retry on a 401 response (expired/invalidated token)."""
    base_url, username, password, static_token = _apstra_credentials()
    if not base_url:
        return {"error": "Apstra not configured. Set APSTRA_BASE_URL.", "url": url}
    if not static_token and not (username and password):
        return {
            "error": (
                "Apstra not configured. Set APSTRA_USERNAME and APSTRA_PASSWORD for "
                "session login, or APSTRA_API_TOKEN for a pre-issued AuthToken."
            ),
            "url": url,
        }

    async def _resolve_token(*, force: bool) -> tuple[str | None, dict[str, Any]]:
        if static_token:
            return static_token, {"endpoint": None, "legacy_fallback": False, "cached": True}
        result = await _get_apstra_token(base_url, username or "", password or "", force=force)
        if "error" in result:
            return None, result
        return result["token"], result

    token, meta = await _resolve_token(force=False)
    if token is None:
        return {"error": meta.get("error", "Apstra login failed"), "url": url}

    async def _do_request(auth_token: str) -> httpx.Response:
        client = pooled_client("apstra")
        request_kwargs = {
            "headers": {"AuthToken": auth_token, "Accept": "application/json"},
            "params": params or {},
        }
        if json_body is not None:
            request_kwargs["json"] = json_body
        if method.upper() == "GET" and json_body is None:
            return await get_with_retry(client, url, **request_kwargs)
        method_handler = getattr(client, method.lower(), None)
        if method_handler is not None:
            return await method_handler(url, **request_kwargs)
        return await client.request(method, url, **request_kwargs)

    try:
        resp = await _do_request(token)
    except httpx.HTTPError as exc:
        return {"error": str(exc), "url": url}

    if resp.status_code == 401 and not static_token:
        # Cached token expired or was invalidated server-side — force one
        # fresh login and retry exactly once before surfacing the failure.
        token, meta = await _resolve_token(force=True)
        if token is None:
            return {
                "error": meta.get("error", "Apstra re-login failed"),
                "url": url,
                "status_code": 401,
            }
        try:
            resp = await _do_request(token)
        except httpx.HTTPError as exc:
            return {"error": str(exc), "url": url}

    return {
        "status_code": resp.status_code,
        "data": redact_sensitive(bounded_response_payload(resp)),
        "url": url,
        "auth": {
            "mode": "static_token" if static_token else "session",
            "login_endpoint": meta.get("endpoint"),
            "legacy_login_fallback": meta.get("legacy_fallback", False),
        },
    }


def _compact_record(item: Any, fields: tuple[str, ...]) -> Any:
    if not isinstance(item, dict):
        return item
    compacted = {key: item[key] for key in fields if key in item}
    return compacted or item


def _compact_collection(data: Any, fields: tuple[str, ...], list_keys: tuple[str, ...] = ()) -> Any:
    if isinstance(data, list):
        return [_compact_record(item, fields) for item in data]
    if not isinstance(data, dict):
        return data
    out = dict(data)
    for key in (*list_keys, "items", "results", "data"):
        if isinstance(out.get(key), list):
            out[key] = [_compact_record(item, fields) for item in out[key]]
            break
    return out


@mcp.tool(annotations=READ_ONLY)
def apstra_status() -> dict[str, Any]:
    """Report whether the Apstra backend is configured and how it authenticates.

    `auth_mode` is `session` when `APSTRA_USERNAME`/`APSTRA_PASSWORD` are set
    (documented `/api/aaa/login` flow, with automatic `/api/user/login`
    fallback only if a live instance requires it), `static_token` when
    `APSTRA_API_TOKEN` is set instead, or `unconfigured`. Reports cached
    session-token age/endpoint without ever revealing the token value.
    """
    base_url, username, password, static_token = _apstra_credentials()
    if static_token:
        auth_mode = "static_token"
    elif username and password:
        auth_mode = "session"
    else:
        auth_mode = "unconfigured"
    cached = _TOKEN_CACHE.get(base_url or "")
    token_status: dict[str, Any] = {"cached": bool(cached)}
    if cached:
        token_status["age_seconds"] = round(time.monotonic() - cached["obtained_at"], 1)
        token_status["login_endpoint"] = cached["endpoint"]
        token_status["legacy_login_fallback"] = cached["legacy_fallback"]
    return {
        "configured": bool(base_url) and auth_mode != "unconfigured",
        "base_url": base_url,
        "auth_mode": auth_mode,
        "has_username": bool(username),
        "has_password": bool(password),
        "has_static_token": bool(static_token),
        "has_token": bool(static_token),
        "token": token_status,
    }


@mcp.tool(annotations=DIAGNOSTIC)
async def apstra_login(force: bool = False) -> dict[str, Any]:
    """Authenticate to Apstra and cache the resulting `AuthToken`.

    Tries the current `POST /api/aaa/login` endpoint first and falls
    back to the older `POST /api/user/login` only if the live instance
    responds 404/405 there. Set `force=True` to discard any cached token and
    re-authenticate immediately (e.g. after a password rotation). Never
    returns the token value itself — use this to diagnose login failures.
    """
    base_url, username, password, static_token = _apstra_credentials()
    if not base_url:
        return {"error": "Apstra not configured. Set APSTRA_BASE_URL."}
    try:
        base_url = validate_product_base_url(base_url, product="Apstra")
    except ValueError as exc:
        return {"error": str(exc)}
    if static_token:
        return {
            "auth_mode": "static_token",
            "note": "APSTRA_API_TOKEN is set; session login is skipped.",
        }
    if not (username and password):
        return {"error": "Set APSTRA_USERNAME and APSTRA_PASSWORD to use session login."}
    result = await _get_apstra_token(base_url, username, password, force=force)
    if "error" in result:
        return {"error": result["error"], "endpoint": result.get("endpoint")}
    return {
        "authenticated": True,
        "login_endpoint": result["endpoint"],
        "legacy_login_fallback": result["legacy_fallback"],
        "cached": result.get("cached", False),
    }


@mcp.tool(annotations=READ_ONLY)
async def apstra_get(
    path: str,
    params: dict[str, Any] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Perform a read-only GET request to Apstra API.

    Safety guard: only allows paths beginning with `/api/`. Authenticates
    with a cached `AuthToken` from session login (or `APSTRA_API_TOKEN` when
    set), retrying once on a 401 after a forced re-login. List payloads are
    bounded with `limit` and `offset`.
    """
    base_url, username, password, static_token = _apstra_credentials()
    if not base_url:
        return {"error": "Apstra not configured. Set APSTRA_BASE_URL."}
    try:
        path = safe_api_path(path, ("/api/",))
    except ValueError as exc:
        return {"error": f"Invalid path. {exc}"}
    path = quote(path, safe="/")

    try:
        base_url = validate_product_base_url(base_url, product="Apstra")
    except ValueError as exc:
        return {"error": str(exc)}
    url = f"{base_url}{path}"
    out = await _apstra_authenticated_request("GET", url, params=params)
    if "data" in out:
        out["data"] = bound_collection_response(out["data"], limit=limit, offset=offset)
    return out


async def _apstra_read_post(
    path: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """POST to a read-only Apstra endpoint with a fixed, typed wrapper."""
    base_url, username, password, static_token = _apstra_credentials()
    if not base_url:
        return {"error": "Apstra not configured. Set APSTRA_BASE_URL."}
    try:
        path = safe_api_path(path, ("/api/",))
    except ValueError as exc:
        return {"error": f"Invalid path. {exc}"}
    path = quote(path, safe="/")

    try:
        base_url = validate_product_base_url(base_url, product="Apstra")
    except ValueError as exc:
        return {"error": str(exc)}
    url = f"{base_url}{path}"
    out = await _apstra_authenticated_request("POST", url)
    if "data" in out:
        out["data"] = bound_collection_response(out["data"], limit=limit, offset=offset)
    return out


@mcp.tool(annotations=READ_ONLY)
async def apstra_list_blueprints(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """List Apstra blueprints with compact ID/name/status fields."""
    out = await apstra_get("/api/blueprints", limit=limit, offset=offset)
    if "data" in out:
        out["blueprints"] = _compact_collection(out.pop("data"), _BLUEPRINT_FIELDS)
    return out


@mcp.tool(annotations=READ_ONLY)
async def apstra_list_templates(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """List Apstra design templates available for blueprint creation."""
    out = await apstra_get("/api/design/templates", limit=limit, offset=offset)
    if "data" in out:
        out["templates"] = _compact_collection(out.pop("data"), _TEMPLATE_FIELDS)
    return out


@mcp.tool(annotations=READ_ONLY)
async def apstra_list_anomalies(
    blueprint_id: str,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List anomalies for one Apstra blueprint with compact health fields."""
    path = f"/api/blueprints/{_path_segment(blueprint_id)}/anomalies"
    out = await apstra_get(path, limit=limit, offset=offset)
    if "data" in out:
        out["anomalies"] = _compact_collection(out.pop("data"), _ANOMALY_FIELDS)
        out["blueprint_id"] = blueprint_id
    return out


@mcp.tool(annotations=READ_ONLY)
async def apstra_list_racks(
    blueprint_id: str,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List racks in one Apstra blueprint with compact topology fields."""
    path = f"/api/blueprints/{_path_segment(blueprint_id)}/racks"
    out = await apstra_get(path, limit=limit, offset=offset)
    if "data" in out:
        out["racks"] = _compact_collection(out.pop("data"), _RACK_FIELDS, ("racks",))
        out["blueprint_id"] = blueprint_id
    return out


@mcp.tool(annotations=READ_ONLY)
async def apstra_list_routing_zones(
    blueprint_id: str,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List routing/security zones in one Apstra blueprint with compact fields."""
    path = f"/api/blueprints/{_path_segment(blueprint_id)}/security-zones"
    out = await apstra_get(path, limit=limit, offset=offset)
    if "data" in out:
        out["routing_zones"] = _compact_collection(
            out.pop("data"),
            _ROUTING_ZONE_FIELDS,
            ("security_zones", "securityZones", "routing_zones"),
        )
        out["blueprint_id"] = blueprint_id
    return out


@mcp.tool(annotations=READ_ONLY)
async def apstra_list_virtual_networks(
    blueprint_id: str,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List virtual networks in one Apstra blueprint with compact bindings."""
    path = f"/api/blueprints/{_path_segment(blueprint_id)}/virtual-networks"
    out = await apstra_get(path, limit=limit, offset=offset)
    if "data" in out:
        out["virtual_networks"] = _compact_collection(
            out.pop("data"),
            _VIRTUAL_NETWORK_FIELDS,
            ("virtual_networks", "virtualNetworks"),
        )
        out["blueprint_id"] = blueprint_id
    return out


@mcp.tool(annotations=READ_ONLY)
async def apstra_list_remote_gateways(
    blueprint_id: str,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List remote EVPN gateways in one Apstra blueprint with compact fields."""
    path = f"/api/blueprints/{_path_segment(blueprint_id)}/remote_gateways"
    out = await apstra_get(path, limit=limit, offset=offset)
    if "data" in out:
        out["remote_gateways"] = _compact_collection(
            out.pop("data"),
            _REMOTE_GATEWAY_FIELDS,
            ("remote_gateways", "remoteGateways"),
        )
        out["blueprint_id"] = blueprint_id
    return out


@mcp.tool(annotations=READ_ONLY)
async def apstra_list_connectivity_templates(
    blueprint_id: str,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List connectivity templates visible in one Apstra blueprint.

    Uses the current `GET /api/blueprints/{id}/endpoint-policies` endpoint
    from the official Apstra SDK.
    """
    path = f"/api/blueprints/{_path_segment(blueprint_id)}/endpoint-policies"
    out = await apstra_get(path, limit=limit, offset=offset)
    if "data" in out:
        out["connectivity_templates"] = _compact_collection(
            out.pop("data"),
            _CONNECTIVITY_TEMPLATE_FIELDS,
            ("policies", "templates", "connectivity_templates", "obj_policies"),
        )
        out["blueprint_id"] = blueprint_id
    return out


@mcp.tool(annotations=READ_ONLY)
async def apstra_get_connectivity_template(
    blueprint_id: str,
    connectivity_template_id: str,
) -> dict[str, Any]:
    """Get one Apstra connectivity template by ID with compact fields.

    Uses `GET /api/blueprints/{id}/endpoint-policies/{policy_id}`.
    """
    path = (
        f"/api/blueprints/{_path_segment(blueprint_id)}/endpoint-policies/"
        f"{_path_segment(connectivity_template_id)}"
    )
    out = await apstra_get(path)
    if "data" in out:
        out["connectivity_template"] = _compact_record(
            out.pop("data"), _CONNECTIVITY_TEMPLATE_FIELDS
        )
        out["blueprint_id"] = blueprint_id
    return out


@mcp.tool(annotations=READ_ONLY)
async def apstra_list_application_endpoints(
    blueprint_id: str,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List interfaces that can receive connectivity-template assignments.

    Uses `POST /api/blueprints/{id}/obj-policy-application-points`, which
    remains the confirmed working shape for this query-style read across
    recent Apstra releases.
    """
    path = f"/api/blueprints/{_path_segment(blueprint_id)}/obj-policy-application-points"
    out = await _apstra_read_post(path, limit=limit, offset=offset)
    if "data" in out:
        out["application_endpoints"] = _compact_collection(
            out.pop("data"),
            _APPLICATION_ENDPOINT_FIELDS,
            ("application_points", "applicationPoints", "endpoints", "interfaces"),
        )
        out["blueprint_id"] = blueprint_id
    return out


@mcp.tool(annotations=READ_ONLY)
async def apstra_get_diff_status(blueprint_id: str) -> dict[str, Any]:
    """Get compact staging-vs-active diff status for one Apstra blueprint."""
    path = f"/api/blueprints/{_path_segment(blueprint_id)}/diff-status"
    out = await apstra_get(path)
    if "data" in out:
        out["diff_status"] = _compact_record(out.pop("data"), _DIFF_STATUS_FIELDS)
        out["blueprint_id"] = blueprint_id
    return out


@mcp.tool(annotations=READ_ONLY)
async def apstra_list_protocol_sessions(
    blueprint_id: str,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List protocol sessions in one Apstra blueprint with compact status fields."""
    path = f"/api/blueprints/{_path_segment(blueprint_id)}/protocol-sessions"
    out = await apstra_get(path, limit=limit, offset=offset)
    if "data" in out:
        out["protocol_sessions"] = _compact_collection(
            out.pop("data"),
            _PROTOCOL_SESSION_FIELDS,
            ("protocol_sessions", "protocolSessions", "sessions"),
        )
        out["blueprint_id"] = blueprint_id
    return out


@mcp.tool(annotations=READ_ONLY)
async def apstra_get_system_info(
    blueprint_id: str,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Get compact system/device information for one Apstra blueprint."""
    path = f"/api/blueprints/{_path_segment(blueprint_id)}/experience/web/system-info"
    out = await apstra_get(path, limit=limit, offset=offset)
    if "data" in out:
        out["systems"] = _compact_collection(
            out.pop("data"),
            _SYSTEM_FIELDS,
            ("systems", "nodes"),
        )
        out["blueprint_id"] = blueprint_id
    return out


@mcp.tool(annotations=READ_ONLY)
async def apstra_get_task(blueprint_id: str, task_id: str) -> dict[str, Any]:
    """Get one Apstra asynchronous blueprint task."""
    path = (
        f"/api/blueprints/{_path_segment(blueprint_id)}/tasks/"
        f"{_path_segment(task_id)}"
    )
    out = await apstra_get(path)
    if "data" in out:
        out["task"] = out.pop("data")
        out["blueprint_id"] = blueprint_id
        out["task_id"] = task_id
    return out


@mcp.tool(annotations=DIAGNOSTIC)
async def apstra_wait_for_task(
    blueprint_id: str,
    task_id: str,
    timeout_seconds: int = 120,
    poll_interval_seconds: float = 2.0,
) -> dict[str, Any]:
    """Poll an Apstra blueprint task until it reaches a terminal state."""
    timeout = max(1, min(timeout_seconds, 600))
    interval = max(0.5, min(poll_interval_seconds, 30.0))
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while True:
        last = await apstra_get_task(blueprint_id, task_id)
        task = last.get("task")
        status = task.get("status") if isinstance(task, dict) else None
        if status in {"succeeded", "failed", "timeout"}:
            return last
        if "error" in last or time.monotonic() >= deadline:
            return {
                **last,
                "timed_out": "error" not in last,
                "timeout_seconds": timeout,
            }
        await asyncio.sleep(interval)


@mcp.tool(annotations=DESTRUCTIVE)
async def apstra_write(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | list[Any] | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Perform a lab write request to Apstra with a preview-first guard.

    Allows `POST`, `PUT`, `PATCH`, and `DELETE` against `/api/*` paths on the
    configured Apstra host. Authenticates with a cached `AuthToken` from
    session login (or `APSTRA_API_TOKEN` when set), retrying once on a 401
    after a forced re-login. Defaults to `dry_run=True`; execution requires
    `dry_run=False` and `confirm=True`.
    """
    if not optional_product_writes_allowed():
        return optional_product_write_blocked("apstra_write")

    method = method.upper()
    if method not in _WRITE_METHODS:
        return {"error": f"method must be one of: {', '.join(sorted(_WRITE_METHODS))}"}

    base_url, username, password, static_token = _apstra_credentials()
    if not base_url:
        return {"error": "Apstra not configured. Set APSTRA_BASE_URL."}
    if not static_token and not (username and password):
        return {
            "error": (
                "Apstra not configured. Set APSTRA_USERNAME and APSTRA_PASSWORD for "
                "session login, or APSTRA_API_TOKEN for a pre-issued AuthToken."
            )
        }
    try:
        safe_path = safe_api_path(path, ("/api/",))
    except ValueError as exc:
        return {"error": f"Invalid path. {exc}"}
    safe_path = quote(safe_path, safe="/")

    try:
        base_url = validate_product_base_url(base_url, product="Apstra")
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

    out = await _apstra_authenticated_request(method, url, params=params, json_body=body)
    if "data" in out:
        out["data"] = redact_sensitive(out["data"])
    return out


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def apstra_create_connectivity_template(
    blueprint_id: str,
    connectivity_template: dict[str, Any],
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Guarded create/update for one Apstra connectivity template.

    Uses the current `PUT /api/blueprints/{blueprint_id}/obj-policy-import`
    endpoint with `connectivity_template` as the request body.
    Defaults to `dry_run=True`; execution requires `dry_run=False` and
    `confirm=True`.
    """
    path = f"/api/blueprints/{_path_segment(blueprint_id)}/obj-policy-import"
    return await apstra_write(
        "PUT", path, body=connectivity_template, dry_run=dry_run, confirm=confirm
    )


@mcp.tool(annotations=DESTRUCTIVE)
async def apstra_delete_connectivity_template(
    blueprint_id: str,
    connectivity_template_id: str,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Delete one Apstra connectivity template by ID.

    Uses `DELETE /api/blueprints/{blueprint_id}/endpoint-policies/{policy_id}`.
    Defaults to `dry_run=True`; execution requires `dry_run=False` and
    `confirm=True`.
    """
    path = (
        f"/api/blueprints/{_path_segment(blueprint_id)}/endpoint-policies/"
        f"{_path_segment(connectivity_template_id)}"
    )
    return await apstra_write("DELETE", path, dry_run=dry_run, confirm=confirm)


@mcp.tool(annotations=DESTRUCTIVE)
async def apstra_set_application_point_assignment(
    blueprint_id: str,
    application_points: list[dict[str, Any]],
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Guarded batch assignment of connectivity templates to application points.

    PATCHes `/api/blueprints/{blueprint_id}/obj-policy-batch-apply`
    with a caller-supplied `application_points` batch payload. Apstra's exact
    assignment schema for this call has changed across releases — use
    `apstra_list_application_endpoints` (or the Apstra Swagger UI under
    Platform > Developers) to confirm the exact shape for your instance
    before executing. Defaults to `dry_run=True`; execution requires
    `dry_run=False` and `confirm=True`.
    """
    path = f"/api/blueprints/{_path_segment(blueprint_id)}/obj-policy-batch-apply"
    return await apstra_write(
        "PATCH",
        path,
        body={"application_points": application_points},
        dry_run=dry_run,
        confirm=confirm,
    )


# ---------------------------------------------------------------------------
# Generated operation tools (see src/hpe_networking_mcp/mcp_servers/openapi_gen). No distributable
# full Apstra OpenAPI spec is available, so the committed manifest is derived
# from pinned official Juniper SDK endpoint mappings — NOT full OpenAPI
# coverage. Every generated call reuses the AuthToken session layer
# (`_apstra_authenticated_request`, with 401 re-login) and preserves the curated
# connectivity-template workflow endpoints. The two documented login endpoints
# are auth-only and are skipped at registration (session auth is injected, never
# a model argument). Registration is guarded by HPE_MCP_APSTRA_GENERATED_TOOLS
# (defaults ON when the manifest exists).
# ---------------------------------------------------------------------------


def _apstra_generated_prepare(path: str) -> tuple[str | None, str | None]:
    """Return (base_url, error) for a generated Apstra request path."""
    base_url, username, password, static_token = _apstra_credentials()
    if not base_url:
        return None, "Apstra not configured. Set APSTRA_BASE_URL."
    if not path.startswith("/api/"):
        return None, "Generated path must begin with /api/."
    try:
        base_url = validate_product_base_url(base_url, product="Apstra")
    except ValueError as exc:
        return None, str(exc)
    return base_url, None


async def _apstra_generated_read(
    method: str,
    path: str,
    query: dict[str, Any],
    headers: dict[str, str],
    body: Any = None,
    content_type: str = "application/json",
) -> dict[str, Any]:
    """Read executor for generated Apstra tools (AuthToken session, bounded)."""
    base_url, error = _apstra_generated_prepare(path)
    if error:
        return {"error": error}
    url = f"{base_url}{path}"
    clean_params = {k: v for k, v in query.items() if v is not None}
    if body is not None and content_type != "application/json":
        return {"error": f"Unsupported Apstra read request body type: {content_type}"}
    out = await _apstra_authenticated_request(
        method,
        url,
        params=clean_params,
        json_body=body,
    )
    if isinstance(out, dict) and "data" in out:
        out["data"] = redact_sensitive(
            bound_collection_response(out["data"], limit=clamp_limit(None), offset=0)
        )
    return out


async def _apstra_generated_write(
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
    """Write executor for generated Apstra tools (gate + dry-run/confirm)."""
    if not optional_product_writes_allowed():
        return optional_product_write_blocked(name)
    base_url, error = _apstra_generated_prepare(path)
    if error:
        return {"error": error}
    url = f"{base_url}{path}"
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
    out = await _apstra_authenticated_request(method, url, params=clean_params, json_body=body)
    if isinstance(out, dict) and "data" in out:
        out["data"] = redact_sensitive(out["data"])
    return out


def _register_generated_apstra_tools() -> list[str]:
    """Register generated Apstra tools at import time.

    The login endpoints (tagged ``Auth`` in the derived manifest) are filtered
    out so the AuthToken session layer stays the sole credential path — they are
    never exposed as model-visible tools.
    """
    from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import load_manifest, manifest_exists
    from hpe_networking_mcp.mcp_servers.openapi_gen.runtime import (
        generated_tools_enabled,
        register_generated_tools,
    )

    if not generated_tools_enabled("apstra") or not manifest_exists("apstra"):
        return []
    manifest = load_manifest("apstra")
    filtered = {
        **manifest,
        "operations": [
            op for op in manifest.get("operations", []) if "Auth" not in (op.get("tags") or [])
        ],
    }
    return register_generated_tools(
        mcp,
        "apstra",
        read_executor=_apstra_generated_read,
        write_executor=_apstra_generated_write,
        manifest=filtered,
    )


GENERATED_APSTRA_TOOLS = _register_generated_apstra_tools()


if __name__ == "__main__":
    from hpe_networking_mcp.mcp_servers._cache_hygiene import stable_list_tools
    from hpe_networking_mcp.mcp_servers._middleware import (
        NullStripMiddleware,
        RateLimitMiddleware,
        ResponseEnvelopeMiddleware,
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
            ResponseEnvelopeMiddleware(),
            SecretTokenizeMiddleware(),
        ],
    )
    run_server(mcp)
