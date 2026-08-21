"""Stage 2 — Validate: pre-flight checks via read-only MCP tools."""

from __future__ import annotations

import logging
from typing import Any

from hpe_networking_mcp.pipeline.models import (
    AccountContext,
    DeviceRecord,
    FirmwareAction,
    HardwareSeries,
    StageResult,
)
from hpe_networking_mcp.pipeline.stages.base import Stage
from hpe_networking_mcp.pipeline.state_store import StateStore

logger = logging.getLogger(__name__)


# GET /network-config/v1/device-groups (and its deprecated /v1alpha1/ sibling)
# both *require* limit and offset and cap limit at 100 per the committed
# Central manifest (getDeviceGroupsV1 / getDeviceGroups). A single unpaged
# limit=100 request therefore silently hid every group past the first page,
# which made validation reject perfectly valid target groups.
DEVICE_GROUP_PATHS = (
    "/network-config/v1/device-groups",
    "/network-config/v1alpha1/device-groups",
)
_GROUP_PAGE_SIZE = 100
_MAX_GROUP_PAGES = 200

# Response envelope keys seen for this collection, in priority order. A
# response carrying *none* of these is an unrecognized shape (e.g. an error
# envelope returned with a 200) and must trigger the fallback path rather
# than be read as "this account has no device groups".
_GROUP_COLLECTION_KEYS = ("items", "data", "device-groups", "deviceGroups", "groups")


class DeviceGroupLookupError(RuntimeError):
    """Raised when device groups could not be listed from any known path."""


def _extract_group_items(payload: Any) -> list[dict[str, Any]] | None:
    """Return the group list from a response, or ``None`` if unrecognized.

    Distinguishes "recognized envelope, zero groups" (``[]``) from "this is
    not a device-group collection response" (``None``) so only the latter
    falls through to the next API version.
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return None
    for key in _GROUP_COLLECTION_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if value is None:
            return []
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        return None
    return None


def _group_name(group: dict[str, Any]) -> str:
    """Human-readable name of a device group across response shapes.

    Uses ``or`` chaining rather than ``dict.get(key, fallback)`` — the API
    returns explicit ``null`` for absent fields, and the old default-argument
    form propagated that ``None`` straight into the name set.
    """
    for key in ("scopeName", "scope_name", "group", "name", "groupName"):
        value = group.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _fetch_device_groups(central_client: Any) -> list[dict[str, Any]]:
    """Return every device group in the account, paging until exhausted.

    Tries the current ``/v1/`` path first and falls back to the deprecated
    ``/v1alpha1/`` path only when ``/v1/`` errors or answers with a shape that
    is not a device-group collection. Raises :class:`DeviceGroupLookupError`
    when no path yields a usable response — callers must not treat an
    unreachable API as "the group does not exist".
    """
    failures: list[str] = []
    for path in DEVICE_GROUP_PATHS:
        groups: list[dict[str, Any]] = []
        offset = 0
        unrecognized = False
        errored: str | None = None
        for _ in range(_MAX_GROUP_PAGES):
            try:
                result = central_client.get(
                    path, params={"limit": _GROUP_PAGE_SIZE, "offset": offset}
                )
            except Exception as exc:
                errored = f"{path}: {exc}"
                break
            page = _extract_group_items(result)
            if page is None:
                unrecognized = True
                break
            groups.extend(page)
            if len(page) < _GROUP_PAGE_SIZE:
                break
            offset += _GROUP_PAGE_SIZE
        else:
            logger.warning(
                "Device-group paging stopped at the %d-page cap for %s — "
                "results may be incomplete.",
                _MAX_GROUP_PAGES,
                path,
            )
        if errored:
            failures.append(errored)
            continue
        if unrecognized:
            failures.append(f"{path}: unrecognized response shape")
            continue
        logger.debug("Fetched %d device group(s) from %s", len(groups), path)
        return groups
    raise DeviceGroupLookupError(
        "could not list device groups from any known endpoint (" + "; ".join(failures) + ")"
    )


def _get_group_names(central_client: Any) -> set[str]:
    """Fetch every device group name in the account.

    Raises:
        DeviceGroupLookupError: no known device-group endpoint returned a
            usable response.
    """
    names = {_group_name(group) for group in _fetch_device_groups(central_client)}
    names.discard("")
    return names


class ValidateStage(Stage):
    name = "s2_validate"

    def _execute(
        self,
        record: DeviceRecord,
        run_id: str,
        source_ctx: AccountContext,
        target_ctx: AccountContext,
        state: StateStore,
        dry_run: bool,
    ) -> StageResult:
        mcp = target_ctx.mcp_client

        warnings: list[str] = []

        # 1. Confirm device is not already provisioned in target
        existing = mcp.get_device_by_serial(record.serial_number)
        if existing and str(existing.get("isProvisioned", "")).lower() == "yes":
            return StageResult.failed(
                f"VALIDATION_FAILED: device {record.serial_number} is already provisioned "
                "in the target account — skipping to avoid duplicate migration."
            )

        # 2. Check if target_site exists
        site = mcp.get_site_by_name(record.target_site)
        if site:
            record.site_id = site.get("id") or site.get("siteId") or site.get("site_id")
            record.needs_site_create = False
            logger.debug("Site '%s' exists → site_id=%s", record.target_site, record.site_id)
        else:
            record.needs_site_create = True
            logger.info("Site '%s' does not exist — will create in S6", record.target_site)

        # 3. Check if target_group exists (blocking)
        try:
            group_names = _get_group_names(target_ctx.central_client)
        except DeviceGroupLookupError as exc:
            return StageResult.failed(
                f"VALIDATION_FAILED: {exc}. Cannot confirm target_group "
                f"'{record.target_group}' exists — refusing to proceed on an "
                "unverified group."
            )
        if record.target_group not in group_names:
            return StageResult.failed(
                f"VALIDATION_FAILED: target_group '{record.target_group}' does not exist "
                "in the target Central account. Create it before running the hpe_networking_mcp.pipeline."
            )

        # 4. Check for active critical alerts (warn only)
        if record.site_id:
            alerts = mcp.get_alerts(site_id=record.site_id, severity="Critical")
            if alerts:
                warnings.append(
                    f"{len(alerts)} active critical alert(s) on site '{record.target_site}'"
                )
                logger.warning(
                    "Serial %s: %d critical alert(s) on target site — proceeding anyway",
                    record.serial_number,
                    len(alerts),
                )

        # 5. Config-health pre-check (warn only — device may already be in target account)
        if existing:
            try:
                health = target_ctx.central_client.get(
                    "/network-config/v1alpha1/config-health/devices",
                    params={"filter": f"serial eq '{record.serial_number}'"},
                )
                health_items = health.get("devices", health.get("items", []))
                if health_items:
                    config_status = str(health_items[0].get("configStatus", "")).upper()
                    if config_status and config_status != "SYNCHRONIZED":
                        warnings.append(
                            f"configStatus={config_status!r} in target (device may need reconfiguration)"
                        )
            except Exception as exc:
                logger.warning("Config-health preflight check failed for %s: %s", record.serial_number, exc)

        # 7. AOS-S + AOS 10 target → mark firmware as skip
        if record.hardware_series == HardwareSeries.AOS_S and record.firmware_target.startswith("10."):
            record.firmware_action = FirmwareAction.SKIP
            warnings.append("AOS-S hardware cannot run AOS 10 — firmware upgrade will be skipped")

        return StageResult.success(
            site_id=record.site_id,
            needs_site_create=record.needs_site_create,
            firmware_action=record.firmware_action.value,
            warnings=warnings,
        )
