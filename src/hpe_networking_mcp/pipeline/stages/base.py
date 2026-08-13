"""Abstract base class for all pipeline stages."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from hpe_networking_mcp.pipeline.models import AccountContext, DeviceRecord, StageResult, StageStatus
from hpe_networking_mcp.pipeline.state_store import StateStore

logger = logging.getLogger(__name__)


class Stage(ABC):
    """Base class for a pipeline stage.

    Subclasses implement _execute(). The run() method wraps _execute()
    with resume-skip logic and state persistence.
    """

    #: Stage identifier used as the key in the state store, e.g. "s1_discover"
    name: str

    def run(
        self,
        record: DeviceRecord,
        run_id: str,
        source_ctx: AccountContext,
        target_ctx: AccountContext,
        state: StateStore,
        dry_run: bool = False,
    ) -> StageResult:
        # Resume: skip if already succeeded
        if state.is_stage_done(record.serial_number, run_id, self.name):
            logger.debug("[%s] %s already succeeded — skipping", self.name, record.serial_number)
            return StageResult.skipped("already completed in prior run")

        logger.info("[%s] starting %s", self.name, record.serial_number)
        state.set_stage_status(record.serial_number, run_id, self.name, StageStatus.IN_PROGRESS)

        try:
            result = self._execute(record, run_id, source_ctx, target_ctx, state, dry_run)
        except Exception as exc:
            logger.exception("[%s] unhandled exception for %s", self.name, record.serial_number)
            result = StageResult.failed(str(exc))

        state.set_stage_status(
            record.serial_number,
            run_id,
            self.name,
            result.status,
            error=result.error,
            data=result.data,
        )

        log_fn = logger.info if result.status == StageStatus.SUCCESS else logger.warning
        log_fn(
            "[%s] %s → %s%s",
            self.name,
            record.serial_number,
            result.status.value,
            f" ({result.error})" if result.error else "",
        )
        return result

    @abstractmethod
    def _execute(
        self,
        record: DeviceRecord,
        run_id: str,
        source_ctx: AccountContext,
        target_ctx: AccountContext,
        state: StateStore,
        dry_run: bool,
    ) -> StageResult:
        ...
