"""Stage 4 — Cross-Account Transfer.

Skipped when target_account == "same".
Flow: source GLP unarchive → unassign license → target GLP add device → assign license.
"""

from __future__ import annotations

import logging

from hpe_networking_mcp.pipeline.models import AccountContext, DeviceRecord, StageResult, TargetAccount
from hpe_networking_mcp.pipeline.state_store import StateStore
from hpe_networking_mcp.pipeline.stages.base import Stage

logger = logging.getLogger(__name__)


class TransferStage(Stage):
    name = "s4_transfer"

    def _execute(
        self,
        record: DeviceRecord,
        run_id: str,
        source_ctx: AccountContext,
        target_ctx: AccountContext,
        state: StateStore,
        dry_run: bool,
    ) -> StageResult:
        if record.target_account == TargetAccount.SAME:
            return StageResult.skipped("same-account migration — no cross-account transfer needed")

        if dry_run:
            return StageResult.skipped("dry-run — skipping write operations")

        src_glp = source_ctx.glp_client
        tgt_glp = target_ctx.glp_client

        # 1. Unarchive in source GLP (returns device to unassigned state)
        logger.info("Unarchiving %s in source GLP", record.serial_number)
        try:
            src_glp.unarchive_device(record.serial_number)
        except Exception as exc:
            return StageResult.failed(f"TRANSFER_FAILED: unarchive error — {exc}")

        # 2. Unassign subscription in source GLP
        logger.info("Unassigning source subscription for %s", record.serial_number)
        try:
            src_glp.unassign_subscription(record.serial_number)
        except Exception as exc:
            logger.warning("Failed to unassign source subscription: %s — continuing", exc)

        # 3. Add device to target GLP workspace
        logger.info("Adding %s to target GLP workspace", record.serial_number)
        try:
            task_id = tgt_glp.add_device(record.serial_number, mac_address=record.mac_address)
            if task_id:
                tgt_glp.poll_task(task_id)
        except Exception as exc:
            return StageResult.failed(f"TRANSFER_FAILED: add to target GLP error — {exc}")

        # 4. Verify device now exists in target GLP
        device = tgt_glp.get_device(record.serial_number)
        if device is None:
            return StageResult.failed(
                f"TRANSFER_FAILED: device {record.serial_number} not found in target GLP "
                "after add — subscription assignment may be needed manually."
            )

        record.glp_device_id = device.get("id") or device.get("deviceId")

        # 5. Assign subscription in target GLP
        # NOTE: subscription_key must be provided via the device record or config.
        # If not available, log a warning and continue — the device is in GLP.
        subscription_key = getattr(record, "subscription_key", None)
        if subscription_key:
            logger.info("Assigning target subscription for %s", record.serial_number)
            try:
                tgt_glp.assign_subscription(record.serial_number, subscription_key)
            except Exception as exc:
                logger.warning(
                    "Failed to assign target subscription: %s — device is in GLP but may need "
                    "manual license assignment before Central onboarding.",
                    exc,
                )
        else:
            logger.warning(
                "%s: no subscription_key provided — skipping license assignment. "
                "Assign a Central subscription in GLP before Stage 5.",
                record.serial_number,
            )

        return StageResult.success(glp_device_id=record.glp_device_id)
