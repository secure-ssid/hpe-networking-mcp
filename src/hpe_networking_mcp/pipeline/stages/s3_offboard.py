"""Stage 3 — Offboard: remove device from source Central account.

Skipped for unmanaged devices (nothing to offboard).
"""

from __future__ import annotations

import logging
import time

from hpe_networking_mcp.pipeline.models import AccountContext, DeviceRecord, SourceType, StageResult
from hpe_networking_mcp.pipeline.stages.base import Stage
from hpe_networking_mcp.pipeline.state_store import StateStore

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 30  # seconds
_POLL_TIMEOUT = 600  # 10 minutes


class OffboardStage(Stage):
    name = "s3_offboard"

    def _execute(
        self,
        record: DeviceRecord,
        run_id: str,
        source_ctx: AccountContext,
        target_ctx: AccountContext,
        state: StateStore,
        dry_run: bool,
    ) -> StageResult:
        if record.source_type == SourceType.UNMANAGED:
            return StageResult.skipped("unmanaged device — no source offboard needed")

        if dry_run:
            return StageResult.skipped("dry-run — skipping write operations")

        central = source_ctx.central_client
        glp = source_ctx.glp_client
        mcp = source_ctx.mcp_client

        # AOS 8: release controller binding first
        if record.source_type == SourceType.AOS8:
            logger.info(
                "AOS 8 device %s: moving to standalone group to release controller binding",
                record.serial_number,
            )
            try:
                central.post(
                    "/configuration/v1/devices/move",
                    data={"group": "default", "serials": [record.serial_number]},
                )
            except Exception as exc:
                logger.warning("Failed to release AOS 8 controller binding: %s", exc)

        # Archive device in source Central
        logger.info("Archiving %s in source Central", record.serial_number)
        try:
            glp.archive_device(record.serial_number)
        except Exception as exc:
            return StageResult.failed(f"OFFBOARD_FAILED: archive error — {exc}")

        # Poll until device disappears from source Central inventory
        deadline = time.time() + _POLL_TIMEOUT
        while time.time() < deadline:
            device = mcp.get_device_by_serial(record.serial_number)
            if device is None:
                logger.info("%s successfully offboarded from source Central", record.serial_number)
                return StageResult.success()
            logger.debug("%s still visible in source Central — waiting %ds", record.serial_number, _POLL_INTERVAL)
            time.sleep(_POLL_INTERVAL)

        return StageResult.failed(
            f"OFFBOARD_TIMEOUT: device {record.serial_number} still visible in source "
            f"Central after {_POLL_TIMEOUT}s"
        )
