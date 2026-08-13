"""Stage 1 — Discover: resolve device's current state in the source account."""

from __future__ import annotations

import logging

from hpe_networking_mcp.pipeline.models import AccountContext, DeviceRecord, SourceType, StageResult
from hpe_networking_mcp.pipeline.state_store import StateStore
from hpe_networking_mcp.pipeline.stages.base import Stage

logger = logging.getLogger(__name__)


class DiscoverStage(Stage):
    name = "s1_discover"

    def _execute(
        self,
        record: DeviceRecord,
        run_id: str,
        source_ctx: AccountContext,
        target_ctx: AccountContext,
        state: StateStore,
        dry_run: bool,
    ) -> StageResult:
        mcp = source_ctx.mcp_client
        glp = source_ctx.glp_client
        central = source_ctx.central_client

        device: dict | None = None

        if record.source_type == SourceType.UNMANAGED:
            # Try MCP first, fall back to GLP
            device = mcp.get_device_by_serial(record.serial_number)
            if device is None:
                logger.debug("Not found via MCP, trying GLP for %s", record.serial_number)
                device = glp.get_device(record.serial_number)
            if device is None:
                return StageResult.failed(
                    f"DISCOVERY_FAILED: serial {record.serial_number} not found in Central or GLP"
                )

        elif record.source_type in (SourceType.CLASSIC_CENTRAL, SourceType.AOS8):
            try:
                result = central.get(f"/monitoring/v1/devices/{record.serial_number}")
                device = result.get("device", result)
            except Exception as exc:
                return StageResult.failed(f"DISCOVERY_FAILED: {exc}")

            if not device:
                return StageResult.failed(
                    f"DISCOVERY_FAILED: serial {record.serial_number} not found in source Central"
                )

            if record.source_type == SourceType.AOS8:
                record.controller_serial = device.get("controllerSerial") or device.get(
                    "associated_gateway_serial"
                )

        # Populate record fields from discovered data
        record.model = device.get("model") or device.get("deviceModel")
        record.current_firmware = device.get("firmwareVersion") or device.get("swVersion")
        record.glp_device_id = device.get("id") or device.get("deviceId")

        return StageResult.success(
            model=record.model,
            current_firmware=record.current_firmware,
            glp_device_id=record.glp_device_id,
            controller_serial=record.controller_serial,
        )
