"""Stage 5 — Onboard to New Central.

Triggers Central onboarding and polls until the device reaches PROVISIONING status.
"""

from __future__ import annotations

import logging
import time

from hpe_networking_mcp.pipeline.models import AccountContext, DeviceRecord, StageResult
from hpe_networking_mcp.pipeline.stages.base import Stage
from hpe_networking_mcp.pipeline.state_store import StateStore

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 30
_POLL_TIMEOUT = 600  # 10 minutes


class OnboardStage(Stage):
    name = "s5_onboard"

    def _execute(
        self,
        record: DeviceRecord,
        run_id: str,
        source_ctx: AccountContext,
        target_ctx: AccountContext,
        state: StateStore,
        dry_run: bool,
    ) -> StageResult:
        if dry_run:
            return StageResult.skipped("dry-run — skipping write operations")

        glp = target_ctx.glp_client
        mcp = target_ctx.mcp_client

        # Check if device already exists in New Central directly.
        # Internal/lab Central instances may not use GLP device-management.
        existing = mcp.get_device_by_serial(record.serial_number)
        if existing:
            prov = str(existing.get("isProvisioned", "")).lower()
            if prov == "yes":
                logger.info("%s already provisioned — skipping onboard", record.serial_number)
                scope_id = mcp.get_device_scope_id(record.serial_number)
                if scope_id:
                    record.scope_id = scope_id
                return StageResult.success(
                    provisioning_status="ALREADY_PROVISIONED", scope_id=scope_id
                )
            # Device is in Central but not yet provisioned (e.g. offline, awaiting group assign).
            # Proceed to S6 configure — provisioning completes when device connects.
            logger.info(
                "%s is in Central (isProvisioned=%s, status=%s) — onboard complete, "
                "provisioning will finalize after group/persona assignment in S6.",
                record.serial_number,
                existing.get("isProvisioned"),
                existing.get("status"),
            )
            return StageResult.success(
                provisioning_status=existing.get("isProvisioned"),
                device_status=existing.get("status"),
            )
        else:
            # Fall back to GLP check
            device = glp.get_device(record.serial_number)
            if device is None:
                return StageResult.failed(
                    f"ONBOARD_FAILED: device {record.serial_number} not found in Central or GLP — "
                    "ensure the device is in GLP with an active subscription."
                )

        # Trigger onboarding directly via Central REST
        try:
            target_ctx.central_client.post(
                "/device-inventory/v1/devices/activate",
                data={"serials": [record.serial_number]},
            )
            logger.info("Onboard activation triggered for %s", record.serial_number)
        except Exception as exc:
            logger.warning(
                "activate call failed for %s: %s — polling for status anyway",
                record.serial_number,
                exc,
            )

        # Poll until device appears and reaches PROVISIONING or PROVISIONED
        deadline = time.time() + _POLL_TIMEOUT
        while time.time() < deadline:
            device_info = mcp.get_device_by_serial(record.serial_number)
            if device_info:
                status = str(device_info.get("provisioningStatus", "")).upper()
                if status in ("PROVISIONING", "PROVISIONED", "YES"):
                    logger.info("%s reached onboarding status: %s", record.serial_number, status)
                    scope_id = mcp.get_device_scope_id(record.serial_number)
                    if scope_id:
                        record.scope_id = scope_id
                    else:
                        logger.warning(
                            "%s: scope_id not found in /network-config/v1alpha1/devices — S6 will "
                            "re-fetch",
                            record.serial_number,
                        )
                    return StageResult.success(provisioning_status=status, scope_id=scope_id)
                logger.debug(
                    "%s provisioning_status=%s — waiting %ds",
                    record.serial_number, status, _POLL_INTERVAL,
                )
            time.sleep(_POLL_INTERVAL)

        return StageResult.failed(
            f"ONBOARD_TIMEOUT: device {record.serial_number} did not reach PROVISIONING "
            f"status within {_POLL_TIMEOUT}s"
        )
