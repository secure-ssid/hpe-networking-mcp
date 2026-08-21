"""MCP server — optional Juniper Mist backend (30 curated + 1050 generated OpenAPI tools).

Enabled via tool router env:
  HPE_MCP_PRODUCTS=mist

Auth/env:
  MIST_HOST            e.g. https://api.mist.com
  MIST_API_TOKEN       Mist API token
  MIST_SESSION_COOKIE  optional browser-session Cookie header value
  MIST_CSRF_TOKEN      optional session CSRF token

Covers the generic passthrough/WLAN/alarm tools plus typed, bounded workflow
tools for: NAC/Access Assurance (nactags/nacportals/usermacs, plus NAC IDP
realm mappings read from org settings), Marvis AI (client telemetry, client
experience insights, device event search, org Marvis settings), org
inventory and device claims, Wired Assurance switch/port stats, WAN
Assurance gateway (SRX/SSR) stats, a composite read-only site assurance
snapshot workflow (`mist_get_site_assurance_snapshot`, concurrently
combining the switch/gateway/alarm reads above for one site), and bounded
authenticated regional WebSocket diagnostic-result collection
(`mist_collect_diagnostic_results`, requires the `websockets` dependency).
Endpoints and field names verified
directly against the mistsys/mist_openapi spec (mist.openapi.yaml) at commit
f374cffdd5a275c7954645a306fcab7f1227e7a3 (OpenAPI version 2606.1.1,
2026-07-10).
See individual tool docstrings for the underlying `/api/v1/*` endpoints and
any remaining live-instance verification caveats.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from mcp.server.mcpserver import MCPServer
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import PayloadTooBig

from hpe_networking_mcp.mcp_servers.openapi_gen.http_exec import (
    apply_request_body,
    build_multipart_files,
)
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

mcp = MCPServer("mist-core")


def optional_product_writes_allowed() -> bool:
    return _platform_writes_allowed("mist")


def optional_product_write_blocked(tool_name: str) -> dict[str, str]:
    return _platform_write_blocked("mist", tool_name)


_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_EXECUTE_HINT = "Review the request, then call again with dry_run=False and confirm=True."


def _mist_config() -> tuple[str | None, str | None]:
    import os

    host = os.getenv("MIST_HOST", "https://api.mist.com").strip().rstrip("/")
    token = os.getenv("MIST_API_TOKEN", "").strip()
    return (host or None, token or None)


def _normalize_mac(mac_address: str) -> str:
    normalized = re.sub(r"[^0-9A-Fa-f]", "", mac_address).lower()
    if len(normalized) != 12:
        raise ValueError("MAC address must contain exactly 12 hex characters")
    return normalized


def _path_segment(value: str) -> str:
    return quote(value, safe="")


_MIST_WEBSOCKET_HOSTS = {
    "api.mist.com": "api-ws.mist.com",
    "api.gc1.mist.com": "api-ws.gc1.mist.com",
    "api.ac2.mist.com": "api-ws.ac2.mist.com",
    "api.gc2.mist.com": "api-ws.gc2.mist.com",
    "api.gc4.mist.com": "api-ws.gc4.mist.com",
    "api.eu.mist.com": "api-ws.eu.mist.com",
    "api.gc3.mist.com": "api-ws.gc3.mist.com",
    "api.ac6.mist.com": "api-ws.ac6.mist.com",
    "api.gc6.mist.com": "api-ws.gc6.mist.com",
    "api.ac5.mist.com": "api-ws.ac5.mist.com",
    "api.gc5.mist.com": "api-ws.gc5.mist.com",
    "api.gc7.mist.com": "api-ws.gc7.mist.com",
}
_MIST_DIAGNOSTIC_CHANNEL = re.compile(
    r"^/sites/(?P<site_id>[^/]+)/devices/(?P<device_id>[^/]+)/cmd$"
)
_mist_websocket_connect = websocket_connect


def _mist_websocket_url() -> tuple[str | None, str | None]:
    host, _ = _mist_config()
    if not host:
        return None, "Mist not configured. Set MIST_HOST."
    try:
        trusted_host = validate_product_base_url(host, product="Mist")
    except ValueError as exc:
        return None, str(exc)
    parsed = urlsplit(trusted_host)
    try:
        port = parsed.port
    except ValueError:
        return None, "Mist WebSocket diagnostics received an invalid MIST_HOST port."
    if (
        parsed.scheme != "https"
        or port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        return None, (
            "Mist WebSocket diagnostics require a documented HTTPS Mist API origin "
            "without a custom port, path, query, or fragment."
        )
    websocket_host = _MIST_WEBSOCKET_HOSTS.get((parsed.hostname or "").lower())
    if not websocket_host:
        supported = ", ".join(sorted(_MIST_WEBSOCKET_HOSTS))
        return None, (
            f"Mist WebSocket diagnostics do not support configured host "
            f"{parsed.hostname!r}. Supported documented Mist API hosts: {supported}."
        )
    return f"wss://{websocket_host}/api-ws/v1/stream", None


def _mist_websocket_headers() -> tuple[dict[str, str] | None, str | None]:
    import os

    _, token = _mist_config()
    session_cookie = os.getenv("MIST_SESSION_COOKIE", "").strip()
    csrf_token = os.getenv("MIST_CSRF_TOKEN", "").strip()
    if token:
        return {"Authorization": "Token " + token}, None
    if session_cookie and csrf_token:
        return {"Cookie": session_cookie, "X-CSRFToken": csrf_token}, None
    if session_cookie or csrf_token:
        return None, (
            "Mist session authentication requires both MIST_SESSION_COOKIE and "
            "MIST_CSRF_TOKEN."
        )
    return None, (
        "Mist WebSocket authentication requires MIST_API_TOKEN or both "
        "MIST_SESSION_COOKIE and MIST_CSRF_TOKEN."
    )


def _decode_diagnostic_event(message: str | bytes) -> tuple[str, dict[str, Any]] | None:
    if isinstance(message, bytes):
        message = message.decode("utf-8", errors="strict")
    value: Any = json.loads(message)
    if not isinstance(value, dict):
        return None

    for _ in range(3):
        if value.get("event") != "data" or not isinstance(value.get("channel"), str):
            return None
        data = value.get("data")
        if isinstance(data, str):
            try:
                decoded = json.loads(data)
            except json.JSONDecodeError:
                return None
            if (
                isinstance(decoded, dict)
                and decoded.get("event") == "data"
                and isinstance(decoded.get("channel"), str)
            ):
                value = decoded
                continue
            data = decoded
        if isinstance(data, dict):
            return value["channel"], data
        return None
    return None


def _diagnostic_event_finished(data: dict[str, Any]) -> bool:
    if data.get("finished") is True:
        return True
    raw = data.get("raw")
    if not isinstance(raw, str):
        return False
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return isinstance(decoded, dict) and decoded.get("finished") is True


def _mist_diagnostic_secrets() -> tuple[str, ...]:
    import os

    values = {
        os.getenv("MIST_API_TOKEN", "").strip(),
        os.getenv("MIST_CSRF_TOKEN", "").strip(),
        os.getenv("MIST_SESSION_COOKIE", "").strip(),
    }
    cookie = os.getenv("MIST_SESSION_COOKIE", "")
    for part in cookie.split(";"):
        _, separator, value = part.partition("=")
        if separator:
            values.add(value.strip())
    return tuple(sorted((value for value in values if value), key=len, reverse=True))


def _redact_diagnostic_data(value: Any, secrets: tuple[str, ...]) -> Any:
    redacted = redact_sensitive(value)
    if isinstance(redacted, dict):
        return {key: _redact_diagnostic_data(item, secrets) for key, item in redacted.items()}
    if isinstance(redacted, list):
        return [_redact_diagnostic_data(item, secrets) for item in redacted]
    if isinstance(redacted, str):
        for secret in secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


async def _mist_get_request(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    limit: int = 50,
    offset: int = 0,
    bound: bool = True,
) -> dict[str, Any]:
    host, token = _mist_config()
    if not host or not token:
        return {"error": "Mist not configured. Set MIST_HOST and MIST_API_TOKEN."}
    try:
        path = safe_api_path(path, ("/api/v1/",))
    except ValueError as exc:
        return {"error": f"Invalid path. {exc}"}
    path = quote(path, safe="/")

    try:
        host = validate_product_base_url(host, product="Mist")
    except ValueError as exc:
        return {"error": str(exc)}
    url = f"{host}{path}"
    headers = {"Authorization": "Token " + token, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            clean_params = {k: v for k, v in (params or {}).items() if v is not None}
            resp = await client.get(url, headers=headers, params=clean_params)
        payload = response_payload(resp)
        if bound:
            payload = bound_collection_response(payload, limit=limit, offset=offset)
        return {"status_code": resp.status_code, "data": payload, "url": url}
    except httpx.HTTPError as exc:
        return {"error": str(exc), "url": url}


async def _mist_write_request(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | list[Any] | None = None,
    *,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    if not optional_product_writes_allowed():
        return optional_product_write_blocked("mist_write")
    method = method.upper()
    if method not in _WRITE_METHODS:
        return {"error": f"method must be one of: {', '.join(sorted(_WRITE_METHODS))}"}

    host, token = _mist_config()
    if not host or not token:
        return {"error": "Mist not configured. Set MIST_HOST and MIST_API_TOKEN."}
    try:
        safe_path = safe_api_path(path, ("/api/v1/",))
    except ValueError as exc:
        return {"error": f"Invalid path. {exc}"}
    safe_path = quote(safe_path, safe="/")

    try:
        host = validate_product_base_url(host, product="Mist")
    except ValueError as exc:
        return {"error": str(exc)}

    url = f"{host}{safe_path}"
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    preview: dict[str, Any] = {
        "method": method,
        "path": safe_path,
        "url": url,
        "params": redact_sensitive(clean_params),
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

    headers = {"Authorization": "Token " + token, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method,
                url,
                headers=headers,
                params=clean_params,
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
    for key in ("items", "results", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def _pick(data: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {
        field: data[field]
        for field in fields
        if field in data and data[field] not in (None, "")
    }


def _compact_site(site: Any) -> Any:
    if not isinstance(site, dict):
        return site
    return _pick(
        site,
        (
            "id",
            "name",
            "timezone",
            "country_code",
            "address",
            "latlng",
            "sitegroup_ids",
            "wifi_enabled",
        ),
    )


def _compact_client(client: Any) -> Any:
    if not isinstance(client, dict):
        return client
    return _pick(
        client,
        (
            "mac",
            "hostname",
            "ip",
            "username",
            "ap",
            "ap_id",
            "ap_name",
            "site_id",
            "ssid",
            "wlan_id",
            "vlan",
            "rssi",
            "snr",
            "band",
            "channel",
            "tx_rate",
            "rx_rate",
            "tx_bps",
            "rx_bps",
            "uptime",
            "last_seen",
            "health",
            "score",
            "connected",
            "assoc_time",
            "device",
            "os",
            "model",
        ),
    )


def _compact_wlan(wlan: Any) -> Any:
    if not isinstance(wlan, dict):
        return wlan
    return _pick(
        wlan,
        (
            "id",
            "name",
            "ssid",
            "enabled",
            "auth",
            "auth_servers",
            "vlan_id",
            "wlan_id",
            "template_id",
            "site_id",
        ),
    )


def _compact_alarm(alarm: Any) -> Any:
    if not isinstance(alarm, dict):
        return alarm
    return _pick(
        alarm,
        (
            "id",
            "type",
            "group",
            "severity",
            "timestamp",
            "last_seen",
            "count",
            "acked",
            "text",
            "reason",
            "device",
            "device_name",
            "ap",
            "client",
            "site_id",
        ),
    )


def _compact_nac_tag(tag: Any) -> Any:
    if not isinstance(tag, dict):
        return tag
    # Verified against mist_openapi `nac_tag` schema — the VLAN field is
    # named `vlan` (string), not `vlan_id`.
    return _pick(tag, ("id", "name", "type", "match", "match_all", "values", "vlan", "org_id"))


def _compact_nac_portal(portal: Any) -> Any:
    if not isinstance(portal, dict):
        return portal
    # Verified against mist_openapi `nac_portal` schema — there is no
    # `enabled`/`auth_type`/`portal_url`/`sso_url` field; the real
    # read-only URLs are `portal_sso_url`, `portal_authorize_url`, `ui_url`.
    return _pick(
        portal,
        (
            "id",
            "name",
            "type",
            "ssid",
            "portal_sso_url",
            "portal_authorize_url",
            "ui_url",
            "org_id",
        ),
    )


def _compact_nac_idp(idp: Any) -> Any:
    if not isinstance(idp, dict):
        return idp
    # Verified against mist_openapi `org_setting_mist_nac_idp` schema — this
    # is a realm mapping to an externally-defined identity provider `id`,
    # not a standalone IDP resource with name/type/issuer fields.
    return _pick(idp, ("id", "user_realms", "exclude_realms"))


def _compact_user_mac(entry: Any) -> Any:
    if not isinstance(entry, dict):
        return entry
    # Verified against mist_openapi `user_mac` schema — the VLAN field is
    # named `vlan` (string), not `vlan_id`.
    return _pick(
        entry,
        ("id", "mac", "name", "labels", "vlan", "radius_group", "notes"),
    )


def _compact_inventory_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    # Verified against mist_openapi `inventory` schema. Deliberately
    # excludes `magic` — the real field name for the claim code used to add
    # this device (a single-use onboarding secret); there is no `status` or
    # `site_name` field on this schema.
    return _pick(
        item,
        (
            "id",
            "mac",
            "serial",
            "model",
            "type",
            "sku",
            "hw_rev",
            "name",
            "hostname",
            "site_id",
            "org_id",
            "adopted",
            "connected",
            "last_disconnected",
            "vc_mac",
        ),
    )


def _compact_switch(switch: Any) -> Any:
    if not isinstance(switch, dict):
        return switch
    # Verified against mist_openapi `stats_switch` schema — there is no
    # `num_ports` field; per-port detail lives under `ports`/port search.
    return _pick(
        switch,
        (
            "id",
            "mac",
            "name",
            "model",
            "serial",
            "version",
            "status",
            "ip",
            "uptime",
            "site_id",
            "last_seen",
        ),
    )


def _compact_switch_port(port: Any) -> Any:
    if not isinstance(port, dict):
        return port
    # Verified against mist_openapi `searchSiteSwOrGwPorts` response fields.
    return _pick(
        port,
        (
            "port_id",
            "up",
            "full_duplex",
            "speed",
            "poe_disabled",
            "poe_mode",
            "poe_on",
            "mac",
            "neighbor_mac",
            "neighbor_port_desc",
            "neighbor_system_name",
            "stp_state",
            "stp_role",
        ),
    )


def _compact_gateway(gateway: Any) -> Any:
    if not isinstance(gateway, dict):
        return gateway
    # Verified against mist_openapi `stats_gateway` schema — HA/cluster
    # fields are `is_ha`/`cluster_config`, not `ha_config`; `tunnels` and
    # `vpn_peers` carry WAN Edge (SRX/SSR) tunnel/VPN status.
    return _pick(
        gateway,
        (
            "id",
            "mac",
            "name",
            "model",
            "serial",
            "version",
            "status",
            "ip",
            "uptime",
            "is_ha",
            "cluster_config",
            "tunnels",
            "vpn_peers",
            "site_id",
            "last_seen",
        ),
    )


def _compact_marvis_client(entry: Any) -> Any:
    if not isinstance(entry, dict):
        return entry
    # Verified against mist_openapi `stats_marvis_client` schema (Marvis
    # Client Android app telemetry) — there is no org-level "list Marvis AI
    # action suggestions" resource in the public spec (only an MSP-scoped
    # count exists at `/msps/{msp_id}/suggestion/count`).
    return _pick(
        entry,
        (
            "device_id",
            "hostname",
            "model",
            "mfg",
            "serial",
            "os_type",
            "os_version",
            "wifi_mac",
            "wifi_ip",
            "wifi_ssid",
            "wifi_rssi",
            "timestamp",
        ),
    )


def _compact_event(event: Any) -> Any:
    if not isinstance(event, dict):
        return event
    # Verified against mist_openapi `device_event` schema.
    return _pick(
        event,
        (
            "type",
            "ev_type",
            "timestamp",
            "site_id",
            "site_name",
            "mac",
            "model",
            "device_name",
            "device_type",
            "ap",
            "ap_name",
            "port_id",
            "text",
            "reason",
        ),
    )


@mcp.tool(annotations=READ_ONLY)
def mist_status() -> dict[str, Any]:
    """Report whether Mist backend is configured."""
    host, token = _mist_config()
    return {
        "configured": bool(host and token),
        "host": host,
        "has_token": bool(token),
    }


@mcp.tool(annotations=READ_ONLY)
async def mist_get(
    path: str,
    params: dict[str, Any] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Perform a read-only GET request to Mist API.

    Safety guard: only allows paths beginning with `/api/v1/`.
    List payloads are bounded with `limit` and `offset`.
    """
    out = await _mist_get_request(path, params, bound=False)
    if "data" in out:
        out["data"] = bound_collection_response(out["data"], limit=limit, offset=offset)
    return out


@mcp.tool(annotations=READ_ONLY)
async def mist_list_sites(
    org_id: str,
    limit: int = 100,
    page: int = 1,
) -> dict[str, Any]:
    """List Mist org sites with compact ID, name, timezone, and location fields.

    Uses `GET /api/v1/orgs/{org_id}/sites`. Mist uses page-based pagination,
    so pass `limit` and `page` to move through larger orgs.
    """
    safe_limit = clamp_limit(limit, default=100)
    out = await _mist_get_request(
        f"/api/v1/orgs/{_path_segment(org_id)}/sites",
        {"limit": safe_limit, "page": max(1, page)},
        limit=safe_limit,
        offset=0,
    )
    if "data" in out:
        out["sites"] = bound_collection_response(
            [_compact_site(item) for item in _extract_items(out["data"])],
            limit=safe_limit,
            offset=0,
        )
        if isinstance(out["sites"], dict):
            out["sites"]["server_page"] = max(1, page)
        del out["data"]
    return out


@mcp.tool(annotations=READ_ONLY)
async def mist_get_client(site_id: str, mac_address: str) -> dict[str, Any]:
    """Look up Mist wireless client health by site ID and MAC address.

    Uses `GET /api/v1/sites/{site_id}/stats/clients/{client_mac}` and returns
    compact health, AP, WLAN, RSSI, SNR, and identity fields.
    """
    try:
        normalized = _normalize_mac(mac_address)
    except ValueError as exc:
        return {"error": str(exc)}
    out = await _mist_get_request(
        f"/api/v1/sites/{_path_segment(site_id)}/stats/clients/{normalized}"
    )
    if "data" in out:
        out["normalized_mac"] = normalized
        out["client"] = _compact_client(out["data"])
        del out["data"]
    return out


@mcp.tool(annotations=READ_ONLY)
async def mist_list_wlans(
    site_id: str,
    limit: int = 100,
    page: int = 1,
) -> dict[str, Any]:
    """List Mist site WLANs with compact SSID, status, auth, and VLAN fields.

    Uses `GET /api/v1/sites/{site_id}/wlans`. Mist uses page-based pagination,
    so pass `limit` and `page` to move through larger sites.
    """
    safe_limit = clamp_limit(limit, default=100)
    out = await _mist_get_request(
        f"/api/v1/sites/{_path_segment(site_id)}/wlans",
        {"limit": safe_limit, "page": max(1, page)},
        limit=safe_limit,
        offset=0,
    )
    if "data" in out:
        out["wlans"] = bound_collection_response(
            [_compact_wlan(item) for item in _extract_items(out["data"])],
            limit=safe_limit,
            offset=0,
        )
        if isinstance(out["wlans"], dict):
            out["wlans"]["server_page"] = max(1, page)
        del out["data"]
    return out


@mcp.tool(annotations=READ_ONLY)
async def mist_list_alarms(
    site_id: str,
    severity: str | None = None,
    duration: str = "1d",
    limit: int = 100,
    start: str | None = None,
    end: str | None = None,
    search_after: str | None = None,
) -> dict[str, Any]:
    """List recent Mist site alarms with compact severity/time fields.

    Uses `GET /api/v1/sites/{site_id}/alarms/search`. Bound with `limit`;
    pass Mist `search_after` from a previous response to continue.
    """
    safe_limit = clamp_limit(limit, default=100)
    params = {
        "severity": severity,
        "limit": safe_limit,
        "start": start,
        "end": end,
        "duration": duration,
        "sort": "-timestamp",
        "search_after": search_after,
    }
    out = await _mist_get_request(
        f"/api/v1/sites/{_path_segment(site_id)}/alarms/search",
        params,
        limit=safe_limit,
        offset=0,
    )
    if "data" in out:
        out["alarms"] = bound_collection_response(
            [_compact_alarm(item) for item in _extract_items(out["data"])],
            limit=safe_limit,
            offset=0,
        )
        del out["data"]
    return out


# ---------------------------------------------------------------------------
# NAC / Access Assurance
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def mist_list_nac_tags(
    org_id: str,
    limit: int = 100,
    page: int = 1,
) -> dict[str, Any]:
    """List Mist org NAC tags used by Access Assurance policy rules.

    Uses `GET /api/v1/orgs/{org_id}/nactags`. Mist uses page-based
    pagination, so pass `limit` and `page` to move through larger orgs.
    """
    safe_limit = clamp_limit(limit, default=100)
    out = await _mist_get_request(
        f"/api/v1/orgs/{_path_segment(org_id)}/nactags",
        {"limit": safe_limit, "page": max(1, page)},
        limit=safe_limit,
        offset=0,
    )
    if "data" in out:
        out["nac_tags"] = bound_collection_response(
            [_compact_nac_tag(item) for item in _extract_items(out["data"])],
            limit=safe_limit,
            offset=0,
        )
        del out["data"]
    return out


@mcp.tool(annotations=READ_ONLY)
async def mist_list_nac_portals(
    org_id: str,
    limit: int = 100,
    page: int = 1,
) -> dict[str, Any]:
    """List Mist org NAC (guest/BYOD) portals.

    Uses `GET /api/v1/orgs/{org_id}/nacportals`. Mist uses page-based
    pagination, so pass `limit` and `page` to move through larger orgs.
    """
    safe_limit = clamp_limit(limit, default=100)
    out = await _mist_get_request(
        f"/api/v1/orgs/{_path_segment(org_id)}/nacportals",
        {"limit": safe_limit, "page": max(1, page)},
        limit=safe_limit,
        offset=0,
    )
    if "data" in out:
        out["nac_portals"] = bound_collection_response(
            [_compact_nac_portal(item) for item in _extract_items(out["data"])],
            limit=safe_limit,
            offset=0,
        )
        del out["data"]
    return out


@mcp.tool(annotations=READ_ONLY)
async def mist_list_nac_idps(org_id: str) -> dict[str, Any]:
    """List Mist NAC identity-provider realm mappings backing Access Assurance cloud RADIUS.

    There is no standalone `/nacidps` REST resource in the current Mist
    OpenAPI spec — NAC identity-provider realm mappings live at
    `GET /api/v1/orgs/{org_id}/setting` under `mist_nac.idps` (a list of
    `{id, user_realms, exclude_realms}` entries referencing externally
    defined identity providers). This tool fetches org settings and
    extracts that list.
    """
    out = await _mist_get_request(f"/api/v1/orgs/{_path_segment(org_id)}/setting", bound=False)
    if "data" in out and isinstance(out["data"], dict):
        mist_nac = out["data"].get("mist_nac") or {}
        idps = mist_nac.get("idps") if isinstance(mist_nac, dict) else None
        out["nac_idps"] = bound_collection_response(
            [_compact_nac_idp(item) for item in (idps or [])],
            limit=100,
            offset=0,
        )
        del out["data"]
    return out


@mcp.tool(annotations=READ_ONLY)
async def mist_list_user_macs(
    org_id: str,
    limit: int = 100,
    page: int = 1,
) -> dict[str, Any]:
    """List Mist org user-MAC entries (known-client MAC-to-label/VLAN mappings).

    Uses `GET /api/v1/orgs/{org_id}/usermacs/search` (there is no GET on the
    `/usermacs` collection root — only `POST`/`PUT`). These entries are
    commonly referenced by NAC rules for static classification (e.g. IoT
    allowlists). Mist uses page-based pagination, so pass `limit`/`page`.
    """
    safe_limit = clamp_limit(limit, default=100)
    out = await _mist_get_request(
        f"/api/v1/orgs/{_path_segment(org_id)}/usermacs/search",
        {"limit": safe_limit, "page": max(1, page)},
        limit=safe_limit,
        offset=0,
    )
    if "data" in out:
        out["user_macs"] = bound_collection_response(
            [_compact_user_mac(item) for item in _extract_items(out["data"])],
            limit=safe_limit,
            offset=0,
        )
        del out["data"]
    return out


# ---------------------------------------------------------------------------
# Marvis AI
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def mist_search_marvis_clients(
    org_id: str,
    hostname: str | None = None,
    model: str | None = None,
    serial: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Search Marvis Client (Android app) telemetry for one Mist org.

    Uses `GET /api/v1/orgs/{org_id}/stats/marvisclients/search`. The public
    Mist OpenAPI spec has no org-level "list Marvis AI action suggestions"
    resource — Marvis Action suggestion data is only exposed as an
    MSP-scoped count (`/api/v1/msps/{msp_id}/suggestion/count`), not a
    listable org resource — so this tool covers verified Marvis *client*
    stats (device/Wi-Fi telemetry from the Marvis mobile app) instead.
    """
    safe_limit = clamp_limit(limit, default=50)
    params = {"hostname": hostname, "model": model, "serial": serial, "limit": safe_limit}
    out = await _mist_get_request(
        f"/api/v1/orgs/{_path_segment(org_id)}/stats/marvisclients/search",
        params,
        limit=safe_limit,
        offset=offset,
    )
    if "data" in out:
        out["marvis_clients"] = bound_collection_response(
            [_compact_marvis_client(item) for item in _extract_items(out["data"])],
            limit=safe_limit,
            offset=offset,
        )
        del out["data"]
    return out


@mcp.tool(annotations=READ_ONLY)
async def mist_get_client_insights(
    site_id: str,
    client_mac: str,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Get Marvis client experience insights/metrics for one wireless client.

    Uses `GET /api/v1/sites/{site_id}/insights/client/{client_mac}`. Returns
    the Mist-summarized metric series (already compact) bounded to a
    reasonable size; pass `start`/`end` as epoch seconds to scope the window.
    """
    try:
        normalized = _normalize_mac(client_mac)
    except ValueError as exc:
        return {"error": str(exc)}
    out = await _mist_get_request(
        f"/api/v1/sites/{_path_segment(site_id)}/insights/client/{normalized}",
        {"start": start, "end": end},
    )
    if "data" in out:
        out["normalized_mac"] = normalized
        out["insights"] = out.pop("data")
    return out


@mcp.tool(annotations=READ_ONLY)
async def mist_search_events(
    site_id: str,
    event_type: str | None = None,
    mac: str | None = None,
    model: str | None = None,
    text: str | None = None,
    duration: str = "1d",
    limit: int = 100,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Search recent Mist site device events with compact fields.

    Uses `GET /api/v1/sites/{site_id}/devices/events/search`. There is no
    generic unified `/events/search` endpoint — device, client, NAC-client,
    and other-device events each have their own search path; this tool
    covers device events (AP/switch/gateway). Filter with `event_type`
    (maps to the `type` query param), `mac`, `model`, and/or `text`.
    """
    safe_limit = clamp_limit(limit, default=100)
    params = {
        "type": event_type,
        "mac": mac,
        "model": model,
        "text": text,
        "limit": safe_limit,
        "start": start,
        "end": end,
        "duration": duration,
        "sort": "-timestamp",
    }
    out = await _mist_get_request(
        f"/api/v1/sites/{_path_segment(site_id)}/devices/events/search",
        params,
        limit=safe_limit,
        offset=0,
    )
    if "data" in out:
        out["events"] = bound_collection_response(
            [_compact_event(item) for item in _extract_items(out["data"])],
            limit=safe_limit,
            offset=0,
        )
        del out["data"]
    return out


@mcp.tool(annotations=READ_ONLY)
async def mist_get_marvis_settings(org_id: str) -> dict[str, Any]:
    """Get org-level Marvis AI settings from org settings.

    Uses `GET /api/v1/orgs/{org_id}/setting` and returns the nested `marvis`
    object directly (`disable_proactive_monitoring`, `self_driving`) — the
    real `org_setting` schema nests Marvis config under a `marvis` key
    rather than exposing flat top-level `marvis_*`/`vna_*` fields.
    """
    out = await _mist_get_request(f"/api/v1/orgs/{_path_segment(org_id)}/setting", bound=False)
    if "data" in out and isinstance(out["data"], dict):
        out["marvis_settings"] = out["data"].get("marvis") or {}
        del out["data"]
    return out



# ---------------------------------------------------------------------------
# Org inventory and device claims
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def mist_list_org_inventory(
    org_id: str,
    device_type: str | None = None,
    unassigned: bool | None = None,
    limit: int = 100,
    page: int = 1,
) -> dict[str, Any]:
    """List devices in one Mist org's inventory (claimed but possibly unassigned).

    Uses `GET /api/v1/orgs/{org_id}/inventory`. `device_type` maps to Mist's
    `type` filter (`ap`, `switch`, `gateway`). Omits `magic` (the claim code
    used to add the device) from the compact output since it is a
    single-use onboarding secret. Mist uses page-based pagination, so pass
    `limit` and `page`.
    """
    safe_limit = clamp_limit(limit, default=100)
    params = {
        "type": device_type,
        "unassigned": str(unassigned).lower() if unassigned is not None else None,
        "limit": safe_limit,
        "page": max(1, page),
    }
    out = await _mist_get_request(
        f"/api/v1/orgs/{_path_segment(org_id)}/inventory",
        params,
        limit=safe_limit,
        offset=0,
    )
    if "data" in out:
        out["inventory"] = bound_collection_response(
            [_compact_inventory_item(item) for item in _extract_items(out["data"])],
            limit=safe_limit,
            offset=0,
        )
        del out["data"]
    return out


# ---------------------------------------------------------------------------
# Wired Assurance
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def mist_list_switches(
    site_id: str,
    status: str = "all",
    limit: int = 100,
    page: int = 1,
) -> dict[str, Any]:
    """List Mist site switches with compact status/version/uptime fields.

    Uses `GET /api/v1/sites/{site_id}/stats/devices?type=switch` — Mist has
    no separate `/stats/switches` endpoint; all device types share the
    unified `stats/devices` resource filtered by `type`. `status` maps to
    Mist's `all`/`connected`/`disconnected` filter. Mist uses page-based
    pagination, so pass `limit` and `page` to move through larger sites.
    """
    safe_limit = clamp_limit(limit, default=100)
    out = await _mist_get_request(
        f"/api/v1/sites/{_path_segment(site_id)}/stats/devices",
        {"type": "switch", "status": status, "limit": safe_limit, "page": max(1, page)},
        limit=safe_limit,
        offset=0,
    )
    if "data" in out:
        out["switches"] = bound_collection_response(
            [_compact_switch(item) for item in _extract_items(out["data"])],
            limit=safe_limit,
            offset=0,
        )
        del out["data"]
    return out


@mcp.tool(annotations=READ_ONLY)
async def mist_list_switch_ports(
    site_id: str,
    switch_mac: str,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """List port stats for one Mist switch with compact link/PoE fields.

    Uses `GET /api/v1/sites/{site_id}/stats/ports/search` filtered by the
    switch's `mac` and `device_type=switch` — Mist has no nested
    `/stats/switches/{device_id}/ports` path; port search is a unified,
    site-wide resource covering both switch and gateway ports and supports
    `limit`/`sort`/`search_after` pagination (no `page`/`offset`).
    """
    try:
        normalized = _normalize_mac(switch_mac)
    except ValueError as exc:
        return {"error": str(exc)}
    out = await _mist_get_request(
        f"/api/v1/sites/{_path_segment(site_id)}/stats/ports/search",
        {"mac": normalized, "device_type": "switch", "limit": clamp_limit(limit, default=100)},
        bound=False,
    )
    if "data" in out:
        out["normalized_mac"] = normalized
        out["ports"] = bound_collection_response(
            [_compact_switch_port(item) for item in _extract_items(out["data"])],
            limit=limit,
            offset=offset,
        )
        del out["data"]
    return out


# ---------------------------------------------------------------------------
# WAN Assurance (gateways: SRX and SSR)
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def mist_list_gateways(
    site_id: str,
    status: str = "all",
    limit: int = 100,
    page: int = 1,
) -> dict[str, Any]:
    """List Mist site WAN Edge gateways (SRX or SSR) with compact status fields.

    Uses `GET /api/v1/sites/{site_id}/stats/devices?type=gateway` — Mist has
    no separate `/stats/gateways` endpoint; all device types share the
    unified `stats/devices` resource filtered by `type`. Mist represents
    both SRX and Session Smart Router (SSR) WAN Edge devices as the
    `gateway` device type — the `model` field distinguishes them; no
    separate `/srx` or `/ssr` REST namespace exists. `is_ha`/`cluster_config`
    and `tunnels`/`vpn_peers` carry HA and WAN tunnel status. Mist uses
    page-based pagination, so pass `limit` and `page`.
    """
    safe_limit = clamp_limit(limit, default=100)
    out = await _mist_get_request(
        f"/api/v1/sites/{_path_segment(site_id)}/stats/devices",
        {"type": "gateway", "status": status, "limit": safe_limit, "page": max(1, page)},
        limit=safe_limit,
        offset=0,
    )
    if "data" in out:
        out["gateways"] = bound_collection_response(
            [_compact_gateway(item) for item in _extract_items(out["data"])],
            limit=safe_limit,
            offset=0,
        )
        del out["data"]
    return out


@mcp.tool(annotations=READ_ONLY)
async def mist_get_gateway(site_id: str, device_id: str) -> dict[str, Any]:
    """Get one Mist WAN Edge gateway (SRX or SSR) with compact status fields.

    Uses `GET /api/v1/sites/{site_id}/stats/devices/{device_id}` — the same
    unified per-device endpoint used for any device type. See
    `mist_list_gateways` for the SRX/SSR model-field caveat.
    """
    out = await _mist_get_request(
        f"/api/v1/sites/{_path_segment(site_id)}/stats/devices/{_path_segment(device_id)}"
    )
    if "data" in out:
        out["gateway"] = _compact_gateway(out.pop("data"))
    return out


@mcp.tool(annotations=READ_ONLY)
async def mist_get_site_assurance_snapshot(
    site_id: str,
    include_switches: bool = True,
    include_gateways: bool = True,
    include_alarms: bool = True,
    alarm_duration: str = "1d",
    limit: int = 50,
) -> dict[str, Any]:
    """Curated read workflow: one-call site health snapshot.

    Concurrently composes three already-verified, curated read endpoints
    for one site — no new endpoints are introduced:
      - `GET /api/v1/sites/{site_id}/stats/devices?type=switch` (see
        `mist_list_switches`)
      - `GET /api/v1/sites/{site_id}/stats/devices?type=gateway` (see
        `mist_list_gateways`)
      - `GET /api/v1/sites/{site_id}/alarms/search` (see `mist_list_alarms`)

    Each section is fetched independently, so a failure in one section
    (e.g. an unsupported device type for the site) does not block the
    others — check each section's own `error` key. Use the narrower
    `mist_list_switches`/`mist_list_gateways`/`mist_list_alarms` tools for
    paging beyond `limit` or for switch port-level detail.
    """
    safe_limit = clamp_limit(limit, default=50)
    section_calls: dict[str, Any] = {}
    if include_switches:
        section_calls["switches"] = mist_list_switches(site_id, limit=safe_limit)
    if include_gateways:
        section_calls["gateways"] = mist_list_gateways(site_id, limit=safe_limit)
    if include_alarms:
        section_calls["alarms"] = mist_list_alarms(
            site_id, duration=alarm_duration, limit=safe_limit
        )
    if not section_calls:
        return {
            "error": (
                "At least one of include_switches/include_gateways/"
                "include_alarms must be true."
            )
        }

    results = await asyncio.gather(*section_calls.values())
    snapshot: dict[str, Any] = {"site_id": site_id, "sections": {}}
    for name, result in zip(section_calls.keys(), results, strict=True):
        snapshot["sections"][name] = result
    snapshot["degraded"] = any(
        "error" in result for result in snapshot["sections"].values()
    )
    return snapshot


@mcp.tool(annotations=DESTRUCTIVE)
async def mist_write(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | list[Any] | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Perform a lab write request to Mist with a preview-first guard.

    Allows `POST`, `PUT`, `PATCH`, and `DELETE` against `/api/v1/*` paths on
    the configured Mist host. Defaults to `dry_run=True`; execution requires
    `dry_run=False` and `confirm=True`.
    """
    return await _mist_write_request(
        method,
        path,
        params=params,
        body=body,
        dry_run=dry_run,
        confirm=confirm,
    )


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def mist_ack_alarm(
    site_id: str,
    alarm_id: str,
    note: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Acknowledge one Mist site alarm.

    Uses `POST /api/v1/sites/{site_id}/alarms/{alarm_id}/ack`. Defaults to
    `dry_run=True`; execution requires `dry_run=False` and `confirm=True`.
    """
    body = {"note": note} if note else None
    return await _mist_write_request(
        "POST",
        f"/api/v1/sites/{_path_segment(site_id)}/alarms/{_path_segment(alarm_id)}/ack",
        body=body,
        dry_run=dry_run,
        confirm=confirm,
    )


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def mist_unack_alarm(
    site_id: str,
    alarm_id: str,
    note: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Unacknowledge one Mist site alarm.

    Uses `POST /api/v1/sites/{site_id}/alarms/{alarm_id}/unack`. Defaults to
    `dry_run=True`; execution requires `dry_run=False` and `confirm=True`.
    """
    body = {"note": note} if note else None
    return await _mist_write_request(
        "POST",
        f"/api/v1/sites/{_path_segment(site_id)}/alarms/{_path_segment(alarm_id)}/unack",
        body=body,
        dry_run=dry_run,
        confirm=confirm,
    )


@mcp.tool(annotations=DESTRUCTIVE)
async def mist_delete_wlan(
    site_id: str,
    wlan_id: str,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Delete one Mist site WLAN.

    Uses `DELETE /api/v1/sites/{site_id}/wlans/{wlan_id}`. Defaults to
    `dry_run=True`; execution requires `dry_run=False` and `confirm=True`.
    """
    return await _mist_write_request(
        "DELETE",
        f"/api/v1/sites/{_path_segment(site_id)}/wlans/{_path_segment(wlan_id)}",
        dry_run=dry_run,
        confirm=confirm,
    )


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def mist_upsert_user_mac(
    org_id: str,
    mac_address: str,
    labels: list[str] | None = None,
    vlan: str | None = None,
    radius_group: str | None = None,
    notes: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create one Mist org user-MAC entry for NAC classification.

    Uses `POST /api/v1/orgs/{org_id}/usermacs` with `mac` plus optional
    `labels`, `vlan` (Mist's `user_mac` schema stores VLAN as a string, not
    an integer `vlan_id`), `radius_group`, and `notes`. Defaults to
    `dry_run=True`; execution requires `dry_run=False` and `confirm=True`.
    """
    try:
        normalized = _normalize_mac(mac_address)
    except ValueError as exc:
        return {"error": str(exc)}
    body: dict[str, Any] = {"mac": normalized}
    if labels is not None:
        body["labels"] = labels
    if vlan is not None:
        body["vlan"] = vlan
    if radius_group is not None:
        body["radius_group"] = radius_group
    if notes is not None:
        body["notes"] = notes
    out = await _mist_write_request(
        "POST",
        f"/api/v1/orgs/{_path_segment(org_id)}/usermacs",
        body=body,
        dry_run=dry_run,
        confirm=confirm,
    )
    out["normalized_mac"] = normalized
    return out


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def mist_claim_devices(
    org_id: str,
    claim_codes: list[str],
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Claim one or more devices into a Mist org's inventory by claim code.

    Uses `POST /api/v1/orgs/{org_id}/inventory` (`addOrgInventory`) with a
    bare JSON array of claim-code strings as the body — there is no
    `{"op": "claim", ...}` wrapper and no `type` query filter on this
    endpoint (device type is inferred from each claim code). Claim codes are
    masked in the preview since they are single-use onboarding secrets.
    Defaults to `dry_run=True`; execution requires `dry_run=False` and
    `confirm=True`.
    """
    if not claim_codes:
        return {"error": "claim_codes must contain at least one claim code."}
    out = await _mist_write_request(
        "POST",
        f"/api/v1/orgs/{_path_segment(org_id)}/inventory",
        body=claim_codes,
        dry_run=dry_run,
        confirm=confirm,
    )
    if isinstance(out.get("json"), list):
        out["json"] = [
            f"...{code[-4:]}" if len(code) > 4 else "****" for code in claim_codes
        ]
    return out


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def mist_set_marvis_settings(
    org_id: str,
    settings: dict[str, Any],
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Update org-level Marvis AI settings.

    Uses `PUT /api/v1/orgs/{org_id}/setting` with `settings` nested under
    the `marvis` key (`org_setting.marvis`, e.g.
    `{"disable_proactive_monitoring": true}`) — the real `org_setting`
    schema requires the full settings object as the PUT body, so this
    wraps `settings` correctly but still risks clobbering unrelated org
    settings if your Mist release does not merge partial PUT bodies;
    confirm against a live instance before relying on this for production
    orgs. Defaults to `dry_run=True`; execution requires `dry_run=False`
    and `confirm=True`.
    """
    return await _mist_write_request(
        "PUT",
        f"/api/v1/orgs/{_path_segment(org_id)}/setting",
        body={"marvis": settings},
        dry_run=dry_run,
        confirm=confirm,
    )


# ---------------------------------------------------------------------------
# WebSocket diagnostic result collection
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def mist_collect_diagnostic_results(
    site_id: str,
    device_id: str,
    session_id: str,
    timeout_seconds: float = 30.0,
    max_events: int = 50,
    max_bytes: int = 131_072,
) -> dict[str, Any]:
    """Collect bounded output for a previously started Mist device diagnostic.

    Connects to the documented regional `WS /api-ws/v1/stream` endpoint,
    subscribes with `{"subscribe": "/sites/{site_id}/devices/{device_id}/cmd"}`,
    and returns only `event=data` payloads whose documented `session` correlation
    identifier matches `session_id`. The connection URL is derived from the
    configured documented `MIST_HOST`; callers cannot supply a WebSocket URL.

    Authentication reuses `MIST_API_TOKEN` (preferred) or the complete
    `MIST_SESSION_COOKIE` + `MIST_CSRF_TOKEN` login-session configuration.
    Collection is bounded by total received event count, bytes, and elapsed
    time. Timeout, unmatched/bound exhaustion, malformed streams, and premature
    connection closure are explicit non-success results. Connections close on
    success, error, timeout, or cancellation.
    """
    if any(not value.strip() or "/" in value for value in (site_id, device_id, session_id)):
        return {
            "status": "validation_error",
            "completed": False,
            "error": "site_id, device_id, and session_id must be non-empty single segments.",
        }
    if not 0 < timeout_seconds <= 120:
        return {
            "status": "validation_error",
            "completed": False,
            "error": "timeout_seconds must be greater than 0 and at most 120.",
        }
    if not 1 <= max_events <= 200:
        return {
            "status": "validation_error",
            "completed": False,
            "error": "max_events must be between 1 and 200.",
        }
    if not 1_024 <= max_bytes <= 1_048_576:
        return {
            "status": "validation_error",
            "completed": False,
            "error": "max_bytes must be between 1024 and 1048576.",
        }

    websocket_url, url_error = _mist_websocket_url()
    headers, auth_error = _mist_websocket_headers()
    if url_error or auth_error:
        return {
            "status": "configuration_error",
            "completed": False,
            "error": url_error or auth_error,
        }

    channel = f"/sites/{site_id}/devices/{device_id}/cmd"
    subscription = json.dumps({"subscribe": channel}, separators=(",", ":"))
    started = time.monotonic()
    received_events = 0
    received_bytes = 0
    unrelated_events = 0
    malformed_events = 0
    events: list[dict[str, Any]] = []
    status = "connection_error"
    error = "Mist WebSocket closed before a terminal diagnostic event was received."
    secrets = _mist_diagnostic_secrets()

    try:
        async with _mist_websocket_connect(
            websocket_url,
            additional_headers=headers,
            open_timeout=min(timeout_seconds, 10.0),
            close_timeout=5.0,
            max_size=max_bytes,
            max_queue=4,
        ) as websocket:
            await websocket.send(subscription)
            while received_events < max_events:
                remaining = timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    status = "timeout"
                    error = "Timed out before receiving a terminal diagnostic event."
                    break
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=remaining)
                except (TimeoutError, asyncio.TimeoutError):
                    status = "timeout"
                    error = "Timed out before receiving a terminal diagnostic event."
                    break

                received_events += 1
                message_size = len(message) if isinstance(message, bytes) else len(message.encode())
                received_bytes += message_size
                if received_bytes > max_bytes:
                    status = "byte_limit"
                    error = "Diagnostic WebSocket byte limit reached before completion."
                    break

                try:
                    decoded = _decode_diagnostic_event(message)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    malformed_events += 1
                    continue
                if decoded is None:
                    try:
                        control = json.loads(
                            message.decode("utf-8") if isinstance(message, bytes) else message
                        )
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        malformed_events += 1
                    else:
                        if isinstance(control, dict) and control.get("event") != "data":
                            unrelated_events += 1
                        else:
                            malformed_events += 1
                    continue

                event_channel, data = decoded
                match = _MIST_DIAGNOSTIC_CHANNEL.fullmatch(event_channel)
                if (
                    not match
                    or match.group("site_id") != site_id
                    or match.group("device_id") != device_id
                    or data.get("session") != session_id
                ):
                    unrelated_events += 1
                    continue

                safe_data = _redact_diagnostic_data(data, secrets)
                events.append({"channel": event_channel, "data": safe_data})
                if _diagnostic_event_finished(data):
                    status = "completed"
                    error = ""
                    break
            else:
                status = "event_limit"
                error = "Diagnostic WebSocket event limit reached before completion."
    except asyncio.CancelledError:
        raise
    except PayloadTooBig:
        status = "byte_limit"
        error = "A diagnostic WebSocket message exceeded max_bytes before completion."
    except Exception as exc:
        safe_error = _redact_diagnostic_data(str(exc), secrets)
        error = f"Mist WebSocket failed before completion: {safe_error}"

    result: dict[str, Any] = {
        "status": status,
        "completed": status == "completed",
        "site_id": site_id,
        "device_id": device_id,
        "session_id": session_id,
        "channel": channel,
        "events": events,
        "received_events": received_events,
        "matched_events": len(events),
        "unrelated_events": unrelated_events,
        "malformed_events": malformed_events,
        "received_bytes": received_bytes,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    if error:
        result["error"] = error
    return result


# ---------------------------------------------------------------------------
# SLE / assurance convenience wrappers
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def mist_get_org_sle_overview(
    org_id: str,
    metric: str = "throughput",
    start: str | None = None,
    end: str | None = None,
    duration: str = "1d",
) -> dict[str, Any]:
    """Get Mist org-level SLE (Service Level Experience) metric insight.

    Uses `GET /api/v1/orgs/{org_id}/insights/{metric}`. Common `metric`
    values: ``throughput``, ``wifi-success-connecting``, ``time-to-connect``,
    ``roam-success``, ``coverage``. Pass `start`/`end` as epoch seconds or
    `duration` (e.g. ``1d``, ``7d``) to scope the time window.
    The response payload is bounded to prevent oversized context consumption.
    """
    if not org_id:
        return {"error": "org_id is required."}
    params: dict[str, Any] = {
        "duration": duration,
        "start": start,
        "end": end,
    }
    out = await _mist_get_request(
        f"/api/v1/orgs/{_path_segment(org_id)}/insights/{_path_segment(metric)}",
        params,
        bound=False,
    )
    if "data" in out:
        out["metric"] = metric
        out["org_id"] = org_id
        out["sle"] = bound_collection_response(out.pop("data"), limit=clamp_limit(None), offset=0)
    return out


@mcp.tool(annotations=READ_ONLY)
async def mist_get_site_sle_metric_summary(
    site_id: str,
    scope: str,
    scope_id: str,
    metric: str = "wifi",
    start: str | None = None,
    end: str | None = None,
    duration: str = "1d",
) -> dict[str, Any]:
    """Get Mist site SLE metric summary for a given scope and scope entity.

    Uses `GET /api/v1/sites/{site_id}/sle/{scope}/{scope_id}/metric/{metric}/summary`.
    `scope` should be one of ``ap``, ``band``, ``client``, ``device-os``,
    ``device-type``, ``wlan``, ``gateway``, ``switch``. `scope_id` is the
    identifier of the scoped entity (e.g. an AP MAC). `metric` selects the
    SLE metric to summarise. Pass `start`/`end` as epoch seconds or
    `duration` to scope the time window.
    The response payload is bounded to prevent oversized context consumption.
    """
    if not site_id or not scope or not scope_id:
        return {"error": "site_id, scope, and scope_id are all required."}
    params: dict[str, Any] = {
        "duration": duration,
        "start": start,
        "end": end,
    }
    out = await _mist_get_request(
        (
            f"/api/v1/sites/{_path_segment(site_id)}/sle"
            f"/{_path_segment(scope)}/{_path_segment(scope_id)}"
            f"/metric/{_path_segment(metric)}/summary"
        ),
        params,
        bound=False,
    )
    if "data" in out:
        out["metric"] = metric
        out["scope"] = scope
        out["scope_id"] = scope_id
        out["site_id"] = site_id
        out["summary"] = bound_collection_response(
            out.pop("data"), limit=clamp_limit(None), offset=0
        )
    return out


# ---------------------------------------------------------------------------
# Generated OpenAPI tools (see src/hpe_networking_mcp/mcp_servers/openapi_gen). The
# committed manifest at
# src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/mist.json is derived from
# the MIT-licensed
# mistsys OpenAPI spec and exposes every documented /api/v1 operation as a
# directly-callable, typed MCPServer tool. Registration is guarded by
# HPE_MCP_MIST_GENERATED_TOOLS and defaults ON when the manifest exists.
# ---------------------------------------------------------------------------

_MIST_ALLOWED_PREFIXES = ("/api/v1/",)
_MIST_DIAGNOSTIC_TOOL_NAMES: set[str] = set()
_MIST_PUBLIC_PATH_PATTERNS = (
    re.compile(r"^/api/v1/invite/verify/[^/]+$"),
    re.compile(r"^/api/v1/recover$"),
    re.compile(r"^/api/v1/recover/verify/[^/]+$"),
    re.compile(r"^/api/v1/register$"),
    re.compile(r"^/api/v1/register/recaptcha$"),
    re.compile(r"^/api/v1/register/verify/[^/]+$"),
)


def _mist_is_public_path(path: str) -> bool:
    return any(pattern.fullmatch(path) for pattern in _MIST_PUBLIC_PATH_PATTERNS)


def _mist_prepare(path: str) -> tuple[str | None, str | None, str | None]:
    """Return (host, token, error) after validating config and a runtime path.

    The generated runtime already URL-escapes path values and rejects traversal
    segments, so we validate the prefix/host here without re-quoting (which would
    double-encode the escaped segments).
    """
    import os

    host, token = _mist_config()
    session_cookie = os.getenv("MIST_SESSION_COOKIE", "").strip()
    if not host:
        return None, None, "Mist not configured. Set MIST_HOST."
    if not token and not session_cookie and not _mist_is_public_path(path):
        return (
            None,
            None,
            "Mist not configured. Set MIST_API_TOKEN or MIST_SESSION_COOKIE.",
        )
    if not path.startswith(_MIST_ALLOWED_PREFIXES):
        return None, None, "Generated path must begin with /api/v1/."
    try:
        host = validate_product_base_url(host, product="Mist")
    except ValueError as exc:
        return None, None, str(exc)
    return host, token, None


def _mist_auth_headers(
    token: str | None,
    extra: dict[str, str] | None = None,
    *,
    include_session: bool = True,
) -> dict[str, str]:
    """Build request headers, injecting the trusted auth header last."""
    import os

    headers: dict[str, str] = {"Accept": "application/json"}
    if extra:
        # Non-auth header params from the model; auth params are filtered by the
        # runtime, but strip any that would shadow the credential just in case.
        for key, value in extra.items():
            if key.strip().lower() in {"authorization", "cookie"}:
                continue
            headers[key] = value
    if token:
        headers["Authorization"] = "Token " + token
    elif include_session:
        session_cookie = os.getenv("MIST_SESSION_COOKIE", "").strip()
        csrf_token = os.getenv("MIST_CSRF_TOKEN", "").strip()
        if session_cookie:
            headers["Cookie"] = session_cookie
        if csrf_token:
            headers["X-CSRFToken"] = csrf_token
    return headers


async def _mist_generated_read(
    method: str,
    path: str,
    query: dict[str, Any],
    headers: dict[str, str],
    body: Any = None,
    content_type: str = "application/json",
) -> dict[str, Any]:
    """Read executor for generated Mist tools (bounded, direct)."""
    host, token, error = _mist_prepare(path)
    if error:
        return {"error": error}
    url = f"{host}{path}"
    req_headers = _mist_auth_headers(
        token, headers, include_session=not _mist_is_public_path(path)
    )
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
        requested_limit = query.get("limit")
        output_limit = clamp_limit(requested_limit if isinstance(requested_limit, int) else None)
        payload = redact_sensitive(bound_collection_response(
            bounded_response_payload(resp), limit=output_limit, offset=0
        ))
        return {"status_code": resp.status_code, "data": payload, "url": url}
    except httpx.HTTPError as exc:
        return {"error": str(exc), "url": url}


async def _mist_generated_write(
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
    """Write executor for generated Mist tools (gate + dry-run/confirm)."""
    if name not in _MIST_DIAGNOSTIC_TOOL_NAMES and not optional_product_writes_allowed():
        return optional_product_write_blocked(name)
    host, token, error = _mist_prepare(path)
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
    req_headers = _mist_auth_headers(
        token, headers, include_session=not _mist_is_public_path(path)
    )
    kwargs: dict[str, Any] = {"headers": req_headers, "params": clean_params}
    if body is not None:
        if content_type == "application/json":
            kwargs["json"] = body
        elif content_type == "multipart/form-data":
            files, body_error = build_multipart_files(body)
            if body_error is not None:
                return body_error
            kwargs["files"] = files
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


def _register_generated_mist_tools() -> list[str]:
    """Register generated Mist tools at import time, failing on manifest errors."""
    from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import load_manifest
    from hpe_networking_mcp.mcp_servers.openapi_gen.runtime import register_generated_tools

    manifest = load_manifest("mist")
    registered = register_generated_tools(
        mcp,
        "mist",
        read_executor=_mist_generated_read,
        write_executor=_mist_generated_write,
        manifest=manifest,
    )
    if not registered:
        return []
    for operation, registered_name in zip(
        manifest.get("operations", []), registered, strict=True
    ):
        if operation.get("capability") == "diagnostic":
            _MIST_DIAGNOSTIC_TOOL_NAMES.add(registered_name)
    return registered


GENERATED_MIST_TOOLS = _register_generated_mist_tools()


if __name__ == "__main__":
    from hpe_networking_mcp.mcp_servers._cache_hygiene import stable_list_tools
    from hpe_networking_mcp.mcp_servers._middleware import (
        NullStripMiddleware,
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
        ],
    )
    run_server(mcp)
