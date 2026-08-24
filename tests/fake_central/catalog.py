"""Endpoint catalog for the fake Central API.

Declarative route table. Every route the harness (or the write-spine, or the
workflow tests) may touch is declared here with an explicit classification so
the request journal can tell reads, diagnostic task-POSTs, config writes, and
destructive actions apart without re-guessing from the URL shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Classification buckets used by the journal and by scoring.
READ = "read"
DIAGNOSTIC = "diagnostic"  # task-initiated POSTs: showCommands, aaa, disconnect…
WRITE = "write"  # state-changing but not configuration-model (e.g. notification rules)
CONFIG_WRITE = "config_write"  # /network-config/* and /network-notifications/* mutations
DESTRUCTIVE = "destructive"  # reboot, port/poe bounce, swarm actions, deletes

KIND_IS_WRITE = frozenset({WRITE, CONFIG_WRITE, DESTRUCTIVE})
KIND_IS_CONFIG_WRITE = frozenset({CONFIG_WRITE, DESTRUCTIVE})


@dataclass(frozen=True)
class Route:
    """One fake-API route.

    ``pattern`` uses ``{param}`` placeholders for path segments. ``kind`` is the
    harness classification (see module constants). ``collection`` names a key in
    the fixture bundle's ``collections`` map; ``by_id`` routes resolve a single
    item by its ``id`` field; ``paginated`` routes honor ``limit``/``offset``.
    """

    method: str
    pattern: str
    kind: str
    collection: str = ""
    by_id: bool = False
    paginated: bool = False

    @property
    def regex(self) -> re.Pattern[str]:
        expr = re.escape(self.pattern)
        expr = re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", expr)
        return re.compile(rf"^{expr}$")


def is_write(kind: str) -> bool:
    return kind in KIND_IS_WRITE


def is_config_write(kind: str) -> bool:
    return kind in KIND_IS_CONFIG_WRITE


def default_routes() -> list[Route]:
    """The route table matching the golden-scenario api_calls and the
    must_not_call families the scenarios forbid."""
    return [
        # --- auth ---
        Route("POST", "/oauth2/token", READ, collection=""),
        # --- network-monitoring (reads) ---
        Route("GET", "/network-monitoring/v1/clients/{mac}", READ, collection="clients", by_id=True),
        Route("GET", "/network-monitoring/v1/clients", READ, collection="clients", paginated=True),
        Route("GET", "/network-monitoring/v1/client-onboarding-stage/reasons", READ, collection="onboarding_reasons"),
        Route("GET", "/network-monitoring/v1/clients/{mac}/mobility-trail", READ, collection="mobility_trails", by_id=True),
        Route("GET", "/network-monitoring/v1/sites-health", READ, collection="sites_health", paginated=True),
        Route("GET", "/network-monitoring/v1/sites-client-health", READ, collection="sites_client_health", paginated=True),
        Route("GET", "/network-monitoring/v1/device-inventory", READ, collection="device_inventory", paginated=True),
        Route("GET", "/network-monitoring/v1/wlans", READ, collection="wlans", paginated=True),
        Route("GET", "/network-monitoring/v1/events", READ, collection="incident_events", paginated=True),
        Route("GET", "/network-monitoring/v1/aps/{serial}/cpu-utilization-trends", READ, collection="ap_cpu_trends"),
        Route("GET", "/network-monitoring/v1/aps/{serial}/memory-utilization-trends", READ, collection="ap_mem_trends"),
        # --- network-config (reads then writes) ---
        Route("GET", "/network-config/v1/wlan-ssids/{ssid}", READ, collection="wlans", by_id=True),
        Route("PATCH", "/network-config/v1/wlan-ssids/{ssid}", CONFIG_WRITE, collection="wlans", by_id=True),
        Route("DELETE", "/network-config/v1/wlan-ssids/{ssid}", CONFIG_WRITE, collection="wlans", by_id=True),
        Route("GET", "/network-config/v1alpha1/cnac-mac-reg", READ, collection="nac", paginated=True),
        Route("POST", "/network-config/v1alpha1/cnac-mac-reg", CONFIG_WRITE, collection="nac"),
        Route("DELETE", "/network-config/v1alpha1/cnac-mac-reg/{id}", CONFIG_WRITE, collection="nac", by_id=True),
        Route("GET", "/network-config/v1alpha1/firmware-compliance", READ, collection="firmware_compliance"),
        Route("GET", "/network-config/v1alpha1/firmware-details", READ, collection="firmware_details"),
        Route("GET", "/network-services/v1/firmware-details", READ, collection="firmware_details"),
        Route("GET", "/network-services/v1/firmware-details/{serial}", READ, collection="firmware_details", by_id=True),
        Route("POST", "/network-config/v1alpha1/device-firmware", CONFIG_WRITE, collection="device_firmware"),
        Route("PATCH", "/network-config/v1alpha1/device-firmware", CONFIG_WRITE, collection="device_firmware"),
        Route("DELETE", "/network-config/v1alpha1/device-firmware", CONFIG_WRITE, collection="device_firmware"),
        Route("GET", "/network-config/v1alpha1/config-health/devices", READ, collection="config_health", paginated=True),
        Route("GET", "/network-config/v1alpha1/config-health/active-issue", READ, collection="config_health_issues"),
        Route("POST", "/network-config/v1alpha1/config-health/devices-resync", CONFIG_WRITE, collection="config_health"),
        # --- network-services read ---
        Route("GET", "/network-services/v1/audits", READ, collection="audits", paginated=True),
        # --- network-notifications ---
        Route("GET", "/network-notifications/v1/alerts", READ, collection="alerts", paginated=True),
        Route("POST", "/network-notifications/v1/alerts/defer", WRITE, collection="alerts"),
        Route("POST", "/network-notifications/v1/alerts/clear", WRITE, collection="alerts"),
        Route("POST", "/network-notifications/v1/alerts/priority", WRITE, collection="alerts"),
        Route("POST", "/network-notifications/v1/notification-rules", WRITE, collection="notification_rules"),
        Route("DELETE", "/network-notifications/v1/notification-rules/{id}", WRITE, collection="notification_rules", by_id=True),
        # --- network-troubleshooting task endpoints ---
        # Diagnostic task-POSTs (showCommands, aaa, disconnect) are *not*
        # config writes; the disconnect is a WRITE (gated) per the manifest,
        # the aaa test and showCommands are DIAGNOSTIC.
        Route("POST", "/network-troubleshooting/v1/cx/{serial}/showCommands", DIAGNOSTIC, collection="show_command_jobs"),
        Route("GET", "/network-troubleshooting/v1/cx/{serial}/show-commands", READ, collection="show_command_jobs"),
        Route("POST", "/network-troubleshooting/v1/cx/{serial}/show-commands", DIAGNOSTIC, collection="show_command_jobs"),
        Route("POST", "/network-troubleshooting/v1/aps/{serial}/aaa", DIAGNOSTIC, collection="aaa_results"),
        Route("POST", "/network-troubleshooting/v1/aps/{serial}/disconnectUserByMacAddress", WRITE, collection="disconnect_jobs"),
        Route("POST", "/network-troubleshooting/v1/aps/{serial}/disconnectUserAll", DESTRUCTIVE, collection="disconnect_jobs"),
        # --- destructive actions ---
        Route("POST", "/network-troubleshooting/v1/cx/{serial}/portBounce", DESTRUCTIVE, collection="bounce_jobs"),
        Route("POST", "/network-troubleshooting/v1/cx/{serial}/poeBounce", DESTRUCTIVE, collection="bounce_jobs"),
        Route("POST", "/network-troubleshooting/v1/cx/{serial}/reboot", DESTRUCTIVE, collection="reboot_jobs"),
        Route("POST", "/network-troubleshooting/v1/aps/{serial}/reboot", DESTRUCTIVE, collection="reboot_jobs"),
        Route("POST", "/network-troubleshooting/v1/aps/{serial}/rebootSwarm", DESTRUCTIVE, collection="reboot_jobs"),
    ]


class EndpointCatalog:
    """Compiled route table."""

    def __init__(self, routes: list[Route] | None = None) -> None:
        self._routes = list(routes) if routes is not None else default_routes()

    def match(self, method: str, path: str) -> Route | None:
        """Highest-specificity (longest pattern) match for method+path."""
        best: Route | None = None
        for route in self._routes:
            if route.method != method:
                continue
            if route.regex.match(path) is None:
                continue
            if best is None or len(route.pattern) > len(best.pattern):
                best = route
        return best