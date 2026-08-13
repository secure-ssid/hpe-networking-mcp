"""Read-only adapter for Central monitoring and configuration APIs.

All reads that go through this client use the shared CentralClient HTTP layer,
keeping read operations isolated from write-path pipeline code.

In unit tests, replace this class with a mock — all public methods
take simple Python types and return plain dicts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from hpe_networking_mcp.pipeline.clients.central_client import CentralClient

logger = logging.getLogger(__name__)
_DEFAULT_LIST_LIMIT = 50
_MAX_LIST_LIMIT = 200
# Client-side search sweeps (get_device_by_serial / find_client) stop after
# this many 100-item pages. The loop breaks early on a short page, so a
# higher cap costs nothing on small fleets — it only bounds the worst case.
_MAX_SEARCH_PAGES = 50


def _bounded_limit(limit: int) -> int:
    return max(1, min(limit, _MAX_LIST_LIMIT))


def _inventory_device_type_to_event_context(device_type: str | None) -> str:
    """Map device inventory type to troubleshooting event context type."""
    raw = (device_type or "").upper()
    if "ACCESS_POINT" in raw or raw == "AP":
        return "ACCESS_POINT"
    if "GATEWAY" in raw:
        return "GATEWAY"
    if "SWITCH" in raw or "CX" in raw:
        return "SWITCH"
    return "SWITCH"


def _rfc3339_utc_ms(dt: datetime) -> str:
    """Format UTC datetime for Central ``start-at`` / ``end-at`` query params."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _odata_string(value: str) -> str:
    return value.replace("'", "''")


class MCPClient:
    """Thin read-only wrapper around New Central monitoring APIs.

    Uses the same CentralClient HTTP layer as the MCP tools but only calls read
    endpoints.
    """

    def __init__(self, central_client: CentralClient):
        self._client = central_client

    # ------------------------------------------------------------------
    # Devices
    # ------------------------------------------------------------------

    # Device-inventory API versions. Both v1 and v1alpha1 use `next`/`limit`
    # cursor pagination (NOT offset) per the official reference docs
    # (getdeviceinventoryv1 / getdeviceinventory) — offset is not a
    # documented query param on either version.
    _INVENTORY_V1 = "/network-monitoring/v1/device-inventory"
    _INVENTORY_V1ALPHA1 = "/network-monitoring/v1alpha1/device-inventory"

    def _device_inventory_page(
        self, params: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        """GET one device-inventory page, preferring v1 and falling back to
        v1alpha1 when v1 is unavailable on this tenant (404/error).

        Returns ``(raw_response, endpoint_used)``.
        """
        try:
            result = self._client.get(self._INVENTORY_V1, params=params)
            return result, self._INVENTORY_V1
        except Exception as exc:
            logger.debug(
                "device-inventory v1 failed (%s); falling back to v1alpha1", exc
            )
        result = self._client.get(self._INVENTORY_V1ALPHA1, params=params)
        return result, self._INVENTORY_V1ALPHA1

    def get_device_by_serial(self, serial_number: str) -> Optional[dict[str, Any]]:
        """Return the device inventory record for a given serial, or None.

        Tries a server-side ``serialNumber eq '...'`` filter first (supported
        by the device-inventory API), then falls back to a cursor-paginated
        scan using the `next` token returned by each page (not offset — the
        API doesn't support it). Prefers v1, falls back to v1alpha1.
        """
        serial_escaped = _odata_string(serial_number)
        try:
            result, _ = self._device_inventory_page(
                {"filter": f"serialNumber eq '{serial_escaped}'", "limit": 1}
            )
            items = result.get("devices", result.get("items", []))
            for item in items:
                if item.get("serialNumber", "").lower() == serial_number.lower():
                    return item
        except Exception as exc:
            logger.debug(
                "MCPClient.get_device_by_serial(%s): server-side filter failed (%s), "
                "falling back to paginated scan",
                serial_number,
                exc,
            )

        try:
            limit = 100
            cursor: Optional[str] = None
            for _ in range(_MAX_SEARCH_PAGES):
                params: dict[str, Any] = {"limit": limit}
                if cursor:
                    params["next"] = cursor
                result, _ = self._device_inventory_page(params)
                items = result.get("devices", result.get("items", []))
                for item in items:
                    if item.get("serialNumber", "").lower() == serial_number.lower():
                        return item
                cursor = result.get("next")
                if not cursor:
                    break
            else:
                logger.warning(
                    "MCPClient.get_device_by_serial(%s): searched %d devices without "
                    "finding the serial or exhausting the inventory — result is "
                    "inconclusive, not 'not found'. Raise _MAX_SEARCH_PAGES for "
                    "larger fleets.",
                    serial_number,
                    _MAX_SEARCH_PAGES * limit,
                )
            return None
        except Exception as exc:
            logger.warning("MCPClient.get_device_by_serial(%s) failed: %s", serial_number, exc)
            return None

    def get_devices_page(
        self,
        filters: Optional[dict[str, Any]] = None,
        limit: int = _DEFAULT_LIST_LIMIT,
        next_cursor: Optional[str] = None,
    ) -> tuple[list[dict[str, Any]], Optional[str]]:
        """Return one cursor-paginated page of device-inventory records.

        Returns ``(items, next_cursor)`` — pass the returned cursor back in as
        ``next_cursor`` to fetch the following page. ``next_cursor`` is None
        when there is no further page.
        """
        try:
            params = dict(filters or {})
            params["limit"] = _bounded_limit(limit)
            if next_cursor:
                params["next"] = next_cursor
            result, _ = self._device_inventory_page(params)
            items = result.get("devices", result.get("items", []))
            return items, result.get("next")
        except Exception as exc:
            logger.warning("MCPClient.get_devices_page failed: %s", exc)
            return [], None

    def get_devices(
        self,
        filters: Optional[dict[str, Any]] = None,
        limit: int = _DEFAULT_LIST_LIMIT,
        offset: int = 0,
        next_cursor: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Return a single bounded page of device-inventory records.

        The device-inventory API paginates with a `next` cursor, not offset.
        Pass ``next_cursor`` (from a prior call's cursor) to page forward.
        ``offset`` is accepted for backward compatibility only: when
        ``next_cursor`` is omitted and ``offset`` is positive, it is
        translated to an approximate starting cursor (``offset + 1``) — this
        is best-effort, not an exact seek, because `next` is an opaque
        server-issued token on some tenants. Prefer ``get_devices_page`` for
        exact multi-page traversal.
        """
        cursor = next_cursor or (str(offset + 1) if offset > 0 else None)
        items, _ = self.get_devices_page(filters, limit=limit, next_cursor=cursor)
        return items

    # ------------------------------------------------------------------
    # Sites
    # ------------------------------------------------------------------

    def _all_sites(self) -> list[dict[str, Any]]:
        """Return the full, unsliced site list (the API has no paging)."""
        try:
            result = self._client.get("/network-config/v1/sites")
            sites = result.get("items", result.get("sites", []))
            return sites if isinstance(sites, list) else []
        except Exception as exc:
            logger.warning("MCPClient.get_sites failed: %s", exc)
            return []

    def get_sites(self, limit: int = _DEFAULT_LIST_LIMIT, offset: int = 0) -> list[dict[str, Any]]:
        """Return a bounded page of sites with their IDs."""
        # The sites config API does not support limit/offset query params;
        # slice client-side.
        off = max(0, offset)
        lim = _bounded_limit(limit)
        return self._all_sites()[off : off + lim]

    def get_device_scope_id(self, serial_number: str) -> Optional[str]:
        """Return the New Central config-layer scope-id for a device by serial.

        Uses the monitoring device-inventory API (see developer docs
        how-to-get-scope-ids) — match on serialNumber, read scopeId from
        the inventory record.
        """
        try:
            device = self.get_device_by_serial(serial_number)
            if not device:
                return None
            return (
                device.get("scopeId")
                or device.get("scopeID")
                or device.get("scope_id")
            )
        except Exception as exc:
            logger.warning("MCPClient.get_device_scope_id(%s) failed: %s", serial_number, exc)
            return None

    def get_site_by_name(self, name: str) -> Optional[dict[str, Any]]:
        """Return a site record by name, or None if not found.

        New Central sites use 'scopeName' as the human-readable name field.
        Searches the full site list — a paged default here made every site
        past #50 invisible to migration validation and site creation.
        """
        sites = self._all_sites()
        for site in sites:
            site_name = site.get("scopeName") or site.get("siteName") or site.get("name", "")
            if site_name.lower() == name.lower():
                return site
        return None

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    def get_alerts(
        self,
        site_id: Optional[str] = None,
        severity: Optional[str] = None,
        limit: int = _DEFAULT_LIST_LIMIT,
        offset: int = 0,
        next_cursor: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Return active alerts, optionally filtered by site or severity.

        Severity must be passed via OData filter expression, not a bare param.
        /network-notifications/v1/alerts paginates with a `next` cursor, not
        offset (per getalertlistv1). ``offset`` is kept for backward
        compatibility and translated to an approximate starting cursor
        (``offset + 1``) when ``next_cursor`` is omitted.
        """
        filters = ["status eq 'Active'"]
        if site_id:
            filters.append(f"siteId eq '{site_id}'")
        if severity:
            filters.append(f"severity eq '{severity.capitalize()}'")
        params: dict[str, Any] = {
            "filter": " and ".join(filters),
            "limit": _bounded_limit(limit),
        }
        cursor = next_cursor or (str(offset + 1) if offset > 0 else None)
        if cursor:
            params["next"] = cursor
        try:
            result = self._client.get("/network-notifications/v1/alerts", params=params)
            return result.get("alerts", result.get("items", []))
        except Exception as exc:
            logger.warning("MCPClient.get_alerts failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def get_events(
        self,
        serial_number: str,
        hours: int = 24,
        site_id: Optional[str] = None,
        context_type: Optional[str] = None,
        api_limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Return events for a device within the last N hours.

        Uses GET ``/network-troubleshooting/v1/events`` with ``context-type``,
        ``context-identifier``, ``start-at``, ``end-at``, ``site-id``, and ``limit``.
        If ``site_id`` or ``context_type`` is omitted, they are resolved from
        device inventory (serial → site + device type).
        """
        end = datetime.now(timezone.utc)
        span_h = min(max(1, hours), 24 * 30)
        start = end - timedelta(hours=span_h)

        device: Optional[dict[str, Any]] = None
        if site_id is None or context_type is None:
            device = self.get_device_by_serial(serial_number)
        if site_id is None:
            site_id = (device or {}).get("siteId")
        if context_type is None:
            context_type = _inventory_device_type_to_event_context((device or {}).get("deviceType"))
        if not site_id:
            logger.warning("MCPClient.get_events(%s): could not resolve site_id", serial_number)
            return []

        lim = max(1, min(1000, api_limit))
        params = {
            "context-type": context_type,
            "context-identifier": serial_number,
            "start-at": _rfc3339_utc_ms(start),
            "end-at": _rfc3339_utc_ms(end),
            "site-id": site_id,
            "limit": lim,
        }
        try:
            result = self._client.get("/network-troubleshooting/v1/events", params=params)
            return result.get("events", result.get("items", []))
        except Exception as exc:
            logger.warning("MCPClient.get_events(%s) failed: %s", serial_number, exc)
            return []

    # ------------------------------------------------------------------
    # Clients
    # ------------------------------------------------------------------

    def get_clients_page(
        self,
        site_id: Optional[str] = None,
        serial_number: Optional[str] = None,
        ssid: Optional[str] = None,
        connection_type: Optional[str] = None,
        limit: int = 100,
        next_cursor: Optional[str] = None,
    ) -> tuple[list[dict[str, Any]], Optional[str]]:
        """Return one cursor-paginated page of connected clients.

        /network-monitoring/v1/clients paginates with a `next` cursor, not
        offset (per the clients v1 reference). Returns ``(items, next_cursor)``.
        """
        params: dict[str, Any] = {"limit": _bounded_limit(limit)}
        if next_cursor:
            params["next"] = next_cursor
        if site_id:
            params["site-id"] = site_id
        if serial_number:
            params["serial-number"] = serial_number
        odata: list[str] = []
        if ssid:
            odata.append(f"wlanName eq '{ssid}'")
        if connection_type:
            odata.append(f"clientConnectionType eq '{connection_type}'")
        if odata:
            params["filter"] = " and ".join(odata)
        try:
            result = self._client.get("/network-monitoring/v1/clients", params=params)
            items = result.get("clients", result.get("items", []))
            return items, result.get("next")
        except Exception as exc:
            logger.warning("MCPClient.get_clients_page failed: %s", exc)
            return [], None

    def get_clients(
        self,
        site_id: Optional[str] = None,
        serial_number: Optional[str] = None,
        ssid: Optional[str] = None,
        connection_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        next_cursor: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Return one bounded page of connected clients.

        ``offset`` is accepted for backward compatibility only and translated
        to an approximate starting cursor (``offset + 1``) when
        ``next_cursor`` is omitted — the API paginates with `next`, not
        offset. Prefer ``get_clients_page`` for exact multi-page traversal.
        """
        cursor = next_cursor or (str(offset + 1) if offset > 0 else None)
        items, _ = self.get_clients_page(
            site_id=site_id,
            serial_number=serial_number,
            ssid=ssid,
            connection_type=connection_type,
            limit=limit,
            next_cursor=cursor,
        )
        return items

    def find_client(self, mac_or_ip: str) -> Optional[dict[str, Any]]:
        """Find a single client by MAC address or IP address, or None if not found.

        Note: the clients API does not filter server-side by macAddress or
        ipAddress, so we sweep pages using the server-issued `next` cursor
        (not a computed offset — offset is not a supported query param on
        this endpoint) and filter client-side.
        """
        try:
            limit = 100
            cursor: Optional[str] = None
            normalized = mac_or_ip.lower()
            for _ in range(_MAX_SEARCH_PAGES):
                items, cursor_next = self.get_clients_page(limit=limit, next_cursor=cursor)
                for client in items:
                    if (client.get("macAddress") or "").lower() == normalized:
                        return client
                    if (client.get("ipv4") or "").lower() == normalized:
                        return client
                if not cursor_next:
                    break
                cursor = cursor_next
            else:
                logger.warning(
                    "MCPClient.find_client(%s): searched %d clients without finding "
                    "a match or exhausting the list — result is inconclusive, not "
                    "'not found'.",
                    mac_or_ip,
                    _MAX_SEARCH_PAGES * limit,
                )
            return None
        except Exception as exc:
            logger.warning("MCPClient.find_client(%s) failed: %s", mac_or_ip, exc)
            return None

    # ------------------------------------------------------------------
    # Gateway Clusters
    # ------------------------------------------------------------------

    def get_gw_clusters(self) -> list[dict[str, Any]]:
        """Return unique gateway clusters by scanning /network-config/v1/overlay-wlan."""
        try:
            result = self._client.get("/network-config/v1/overlay-wlan")
            seen: dict[str, dict[str, Any]] = {}
            for profile in result.get("ssid-cluster", []):
                for entry in profile.get("gw-cluster-list", []):
                    cluster_name = entry.get("cluster")
                    if cluster_name and cluster_name not in seen:
                        seen[cluster_name] = {
                            "cluster": cluster_name,
                            "cluster-scope-id": entry.get("cluster-scope-id"),
                            "cluster-type": entry.get("cluster-type"),
                            "tunnel-type": entry.get("tunnel-type"),
                        }
            return list(seen.values())
        except Exception as exc:
            logger.warning("MCPClient.get_gw_clusters failed: %s", exc)
            return []
