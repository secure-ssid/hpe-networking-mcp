"""SQLite-backed per-device stage state store.

Provides resume logic: a device skips any stage that already has
status=SUCCESS in the store for the current run_id.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional

from hpe_networking_mcp.pipeline.models import StageStatus

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_metadata (
    run_id       TEXT PRIMARY KEY,
    input_file   TEXT,
    started_at   TEXT,
    completed_at TEXT,
    total_devices INTEGER DEFAULT 0,
    status       TEXT DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS device_state (
    serial_number TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    stage         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    started_at    TEXT,
    completed_at  TEXT,
    error_message TEXT,
    result_data   TEXT,
    PRIMARY KEY (serial_number, run_id, stage)
);
"""


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class StateStore:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # Run metadata
    # ------------------------------------------------------------------

    def create_run(self, run_id: str, input_file: str, total_devices: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO run_metadata (run_id, input_file, started_at, total_devices) "
                "VALUES (?, ?, ?, ?)",
                (run_id, input_file, _now(), total_devices),
            )

    def complete_run(self, run_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE run_metadata SET completed_at=?, status='completed' WHERE run_id=?",
                (_now(), run_id),
            )

    # ------------------------------------------------------------------
    # Stage state
    # ------------------------------------------------------------------

    def get_stage_status(self, serial: str, run_id: str, stage: str) -> StageStatus:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT status FROM device_state WHERE serial_number=? AND run_id=? AND stage=?",
                (serial, run_id, stage),
            ).fetchone()
        if row is None:
            return StageStatus.PENDING
        return StageStatus(row["status"])

    def set_stage_status(
        self,
        serial: str,
        run_id: str,
        stage: str,
        status: StageStatus,
        error: Optional[str] = None,
        data: Optional[dict[str, Any]] = None,
    ) -> None:
        now = _now()
        data_json = json.dumps(data) if data else None
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO device_state
                    (serial_number, run_id, stage, status, started_at, completed_at, error_message, result_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(serial_number, run_id, stage) DO UPDATE SET
                    status=excluded.status,
                    completed_at=excluded.completed_at,
                    error_message=excluded.error_message,
                    result_data=excluded.result_data
                """,
                (serial, run_id, stage, status.value, now, now, error, data_json),
            )

    def get_stage_data(self, serial: str, run_id: str, stage: str) -> dict[str, Any]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT result_data FROM device_state WHERE serial_number=? AND run_id=? AND stage=?",
                (serial, run_id, stage),
            ).fetchone()
        if row and row["result_data"]:
            return json.loads(row["result_data"])
        return {}

    def is_stage_done(self, serial: str, run_id: str, stage: str) -> bool:
        return self.get_stage_status(serial, run_id, stage) == StageStatus.SUCCESS

    # ------------------------------------------------------------------
    # Resume support
    # ------------------------------------------------------------------

    def get_failed_serials(self, run_id: str) -> list[str]:
        """Return serials that have at least one FAILED stage in the given run."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT serial_number FROM device_state WHERE run_id=? AND status='failed'",
                (run_id,),
            ).fetchall()
        return [r["serial_number"] for r in rows]

    def get_all_stage_statuses(self, serial: str, run_id: str) -> dict[str, str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT stage, status FROM device_state WHERE serial_number=? AND run_id=?",
                (serial, run_id),
            ).fetchall()
        return {r["stage"]: r["status"] for r in rows}
