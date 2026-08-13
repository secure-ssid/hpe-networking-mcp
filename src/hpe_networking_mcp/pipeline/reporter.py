"""Write per-device migration results to CSV, JSON, and HTML reports."""

from __future__ import annotations

import csv
import html
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from hpe_networking_mcp.pipeline.models import DeviceRecord, OverallStatus, StageStatus
from hpe_networking_mcp.pipeline.state_store import StateStore

logger = logging.getLogger(__name__)

STAGES = ["s1_discover", "s2_validate", "s3_offboard", "s4_transfer",
          "s5_onboard", "s6_configure", "s7_firmware", "s8_verify"]

COLUMNS = [
    "serial_number",
    "source_type",
    "target_account",
    "overall_status",
    *[f"s{i+1}" for i in range(len(STAGES))],
    "is_provisioned",
    "final_firmware",
    "site_id",
    "error_detail",
    "duration_seconds",
    "notes",
]


def _overall_status(stage_statuses: dict[str, str]) -> OverallStatus:
    statuses = set(stage_statuses.values())
    if StageStatus.FAILED.value in statuses:
        # If s8 succeeded despite earlier failures it can't happen, but check s8
        if stage_statuses.get("s8_verify") == StageStatus.SUCCESS.value:
            return OverallStatus.DONE
        return OverallStatus.FAILED
    if all(v == StageStatus.SKIPPED.value for v in stage_statuses.values()):
        return OverallStatus.SKIPPED
    if stage_statuses.get("s8_verify") == StageStatus.SUCCESS.value:
        return OverallStatus.DONE
    if any(v == StageStatus.SUCCESS.value for v in stage_statuses.values()):
        return OverallStatus.PARTIAL
    # No failures, not all skipped, no successes yet → still in progress.
    return OverallStatus.PARTIAL


def _stage_error_message(state: StateStore, serial: str, run_id: str, stage: str) -> str:
    """Read the error_message column for a stage (StateStore has no public getter)."""
    with state._conn() as conn:
        row = conn.execute(
            "SELECT error_message FROM device_state WHERE serial_number=? AND run_id=? AND stage=?",
            (serial, run_id, stage),
        ).fetchone()
    if row and row["error_message"]:
        return str(row["error_message"])
    return ""


def write_report(
    records: list[DeviceRecord],
    run_id: str,
    state: StateStore,
    output_dir: str = "outputs",
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> str:
    """Write the backward-compatible CSV report and return its path."""
    return write_reports(
        records,
        run_id,
        state,
        output_dir=output_dir,
        started_at=started_at,
        ended_at=ended_at,
        formats=("csv",),
    )["csv"]


def _report_rows(
    records: list[DeviceRecord],
    run_id: str,
    state: StateStore,
    *,
    started_at: datetime | None,
    ended_at: datetime | None,
) -> list[dict]:
    duration = ""
    if started_at is not None and ended_at is not None:
        duration = round(max(0.0, (ended_at - started_at).total_seconds()), 3)
    rows: list[dict] = []
    for record in records:
        stage_statuses = state.get_all_stage_statuses(record.serial_number, run_id)
        overall = _overall_status(stage_statuses)
        verify_data = state.get_stage_data(record.serial_number, run_id, "s8_verify")

        error_detail = ""
        for stage in STAGES:
            if stage_statuses.get(stage) != StageStatus.FAILED.value:
                continue
            error_detail = (
                _stage_error_message(state, record.serial_number, run_id, stage)
                or ""
            )
            if not error_detail:
                stage_data = state.get_stage_data(
                    record.serial_number,
                    run_id,
                    stage,
                )
                checks = stage_data.get("checks_failed") or stage_data.get("errors")
                if isinstance(checks, list):
                    error_detail = "; ".join(str(check) for check in checks)
            break

        row: dict = {
            "serial_number": record.serial_number,
            "source_type": record.source_type.value,
            "target_account": record.target_account.value,
            "overall_status": overall.value,
            "is_provisioned": verify_data.get("is_provisioned", ""),
            "final_firmware": verify_data.get("final_firmware", ""),
            "site_id": verify_data.get("site_id", record.site_id or ""),
            "error_detail": error_detail,
            "duration_seconds": duration,
            "notes": record.notes or "",
        }
        for index, stage in enumerate(STAGES):
            row[f"s{index + 1}"] = stage_statuses.get(
                stage,
                StageStatus.PENDING.value,
            )
        rows.append(row)
    return rows


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _write_json(
    path: Path,
    rows: list[dict],
    *,
    run_id: str,
    started_at: datetime | None,
    ended_at: datetime | None,
) -> None:
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": started_at.isoformat() if started_at else None,
        "ended_at": ended_at.isoformat() if ended_at else None,
        "devices": rows,
    }
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n")


def _write_html(path: Path, rows: list[dict], *, run_id: str) -> None:
    headings = "".join(f"<th>{html.escape(column)}</th>" for column in COLUMNS)
    body_rows = []
    for row in rows:
        cells = "".join(
            f"<td>{html.escape(str(row.get(column, '')))}</td>"
            for column in COLUMNS
        )
        body_rows.append(f"<tr>{cells}</tr>")
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>hpe-networking-mcp migration report {html.escape(run_id)}</title>
  <style>
    body {{ font-family: sans-serif; margin: 2rem; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #ccc; padding: .4rem; text-align: left; }}
    th {{ background: #f3f5f7; position: sticky; top: 0; }}
  </style>
</head>
<body>
  <h1>Migration report: {html.escape(run_id)}</h1>
  <table><thead><tr>{headings}</tr></thead>
  <tbody>{''.join(body_rows)}</tbody></table>
</body>
</html>
"""
    path.write_text(document)


def write_reports(
    records: list[DeviceRecord],
    run_id: str,
    state: StateStore,
    output_dir: str = "outputs",
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    formats: tuple[str, ...] = ("csv",),
) -> dict[str, str]:
    """Write selected report formats and return a format-to-path mapping."""
    normalized = tuple(dict.fromkeys(item.strip().lower() for item in formats))
    unsupported = sorted(set(normalized) - {"csv", "json", "html"})
    if unsupported:
        raise ValueError(f"unsupported report formats: {', '.join(unsupported)}")
    if not normalized:
        raise ValueError("at least one report format is required")

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rows = _report_rows(
        records,
        run_id,
        state,
        started_at=started_at,
        ended_at=ended_at,
    )
    paths: dict[str, str] = {}
    for report_format in normalized:
        filename = output_path / f"migration_report_{run_id}_{ts}.{report_format}"
        if report_format == "csv":
            _write_csv(filename, rows)
        elif report_format == "json":
            _write_json(
                filename,
                rows,
                run_id=run_id,
                started_at=started_at,
                ended_at=ended_at,
            )
        else:
            _write_html(filename, rows, run_id=run_id)
        paths[report_format] = str(filename)
        logger.info("%s report written to %s", report_format.upper(), filename)
    return paths
