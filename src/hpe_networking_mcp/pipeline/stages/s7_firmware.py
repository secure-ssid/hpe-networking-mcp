"""Stage 7 — Firmware Upgrade to AOS 10.

Skipped for AOS-S devices (firmware_action == SKIP).
Sets group-level compliance, triggers per-device upgrade, then polls for completion.
"""

from __future__ import annotations

import logging
import time

from hpe_networking_mcp.pipeline.models import (
    AccountContext,
    DeviceRecord,
    FirmwareAction,
    StageResult,
)
from hpe_networking_mcp.pipeline.stages.base import Stage
from hpe_networking_mcp.pipeline.state_store import StateStore

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 60   # seconds
_POLL_TIMEOUT = 2700  # 45 minutes


class FirmwareStage(Stage):
    name = "s7_firmware"

    def _execute(
        self,
        record: DeviceRecord,
        run_id: str,
        source_ctx: AccountContext,
        target_ctx: AccountContext,
        state: StateStore,
        dry_run: bool,
    ) -> StageResult:
        if record.firmware_action == FirmwareAction.SKIP:
            return StageResult.skipped(
                f"AOS-S hardware cannot run AOS 10 — firmware upgrade skipped "
                f"for {record.serial_number}"
            )

        if dry_run:
            return StageResult.skipped("dry-run — skipping write operations")

        central = target_ctx.central_client

        def _get_firmware_item(serial: str) -> dict:
            """Return the firmware-details item for a device (items[] response)."""
            resp = central.get(
                "/network-services/v1alpha1/firmware-details",
                params={"serialNumber": serial},
            )
            items = resp.get("items", [])
            for item in items:
                if item.get("serialNumber") == serial or item.get("id") == serial:
                    return item
            return items[0] if items else {}

        # 0. Check if device is already on target firmware — skip upgrade if so
        try:
            current = _get_firmware_item(record.serial_number)
            software_version = current.get("softwareVersion", "")
            # softwareVersion may have a platform prefix like "PL.10.16.1006"
            if record.firmware_target in software_version:
                logger.info(
                    "%s already on firmware %s — skipping upgrade",
                    record.serial_number, software_version,
                )
                return StageResult.success(
                    firmware_version=software_version,
                    upgrade_status="ALREADY_AT_TARGET",
                )
        except Exception as exc:
            logger.warning(
                "Pre-upgrade firmware check failed for %s: %s", record.serial_number, exc
            )

        # 1. Set firmware compliance for the group (non-blocking — endpoint may not exist)
        logger.info(
            "Setting firmware compliance %s for group '%s'",
            record.firmware_target,
            record.target_group,
        )
        try:
            central.post(
                "/firmware/v1/set-firmware-compliance",
                data={
                    "device_type": "SWITCH",
                    "firmware_version": record.firmware_target,
                    "group": record.target_group,
                },
            )
        except Exception as exc:
            logger.warning(
                "set-firmware-compliance failed: %s — continuing with direct upgrade", exc
            )

        # 2. Trigger per-device firmware upgrade
        logger.info(
            "Triggering firmware upgrade to %s for %s",
            record.firmware_target,
            record.serial_number,
        )
        triggered = False
        for upgrade_path, upgrade_payload in [
            (
                "/firmware/v1/upgrade",
                {"serials": [record.serial_number], "firmware_version": record.firmware_target},
            ),
            (
                "/network-services/v1alpha1/firmware-upgrade",
                {
                    "serialNumbers": [record.serial_number],
                    "firmwareVersion": record.firmware_target,
                },
            ),
        ]:
            try:
                central.post(upgrade_path, data=upgrade_payload)
                triggered = True
                logger.info("Firmware upgrade triggered via %s", upgrade_path)
                break
            except Exception as exc:
                logger.debug("Upgrade path %s failed: %s", upgrade_path, exc)

        if not triggered:
            logger.warning(
                "No firmware upgrade endpoint available for %s — will poll current status",
                record.serial_number,
            )

        # 3. Poll for upgrade completion
        deadline = time.time() + _POLL_TIMEOUT
        while time.time() < deadline:
            try:
                item = _get_firmware_item(record.serial_number)
                upgrade_status = item.get("upgradeStatus", "") or ""
                software_version = item.get("softwareVersion", "") or ""
                logger.debug(
                    "%s firmware: upgradeStatus=%s softwareVersion=%s",
                    record.serial_number, upgrade_status, software_version,
                )
                # Accept if already at target version
                if record.firmware_target in software_version:
                    return StageResult.success(
                        firmware_version=software_version,
                        upgrade_status=upgrade_status or "AT_TARGET",
                    )
                if upgrade_status.lower() == "failed":
                    return StageResult.failed(
                        f"FIRMWARE_FAILED: device reported upgrade failure "
                        f"(softwareVersion={software_version})"
                    )
            except Exception as exc:
                logger.warning("Firmware status poll error: %s", exc)

            time.sleep(_POLL_INTERVAL)

        return StageResult.failed(
            f"FIRMWARE_TIMEOUT: firmware upgrade did not complete within {_POLL_TIMEOUT}s "
            f"(target={record.firmware_target})"
        )
