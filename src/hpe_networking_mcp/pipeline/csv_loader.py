"""Parse and validate the migration input CSV into DeviceRecord objects."""

from __future__ import annotations

import csv
import logging
from pathlib import Path

from hpe_networking_mcp.pipeline.models import (
    DeviceRecord,
    FirmwareAction,
    HardwareSeries,
    Persona,
    SourceType,
    TargetAccount,
)

logger = logging.getLogger(__name__)

_REQUIRED_COLUMNS = {
    "serial_number",
    "source_type",
    "hardware_series",
    "target_account",
    "target_site",
    "target_group",
    "persona",
    "firmware_target",
}


class CSVValidationError(Exception):
    pass


def load_csv(path: str) -> list[DeviceRecord]:
    """Parse the input CSV and return validated DeviceRecord objects.

    Raises:
        CSVValidationError: If required columns are missing or values are invalid.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {path}")

    records: list[DeviceRecord] = []
    errors: list[str] = []
    seen_serials: set[str] = set()

    with open(file_path, newline="") as f:
        reader = csv.DictReader(f)

        if reader.fieldnames is None:
            raise CSVValidationError("CSV file is empty or has no header row.")

        missing = _REQUIRED_COLUMNS - set(reader.fieldnames)
        if missing:
            raise CSVValidationError(f"CSV missing required columns: {sorted(missing)}")

        for row_num, row in enumerate(reader, start=2):  # row 1 = header
            serial = row.get("serial_number", "").strip().upper()
            if not serial:
                errors.append(f"Row {row_num}: serial_number is required.")
                continue

            if serial in seen_serials:
                errors.append(f"Row {row_num}: duplicate serial_number '{serial}'.")
                continue
            seen_serials.add(serial)

            try:
                record = _parse_row(row_num, serial, row)
                records.append(record)
            except CSVValidationError as exc:
                errors.append(str(exc))

    if errors:
        error_text = "\n".join(errors)
        raise CSVValidationError(f"CSV validation failed:\n{error_text}")

    logger.info("Loaded %d device(s) from %s", len(records), path)
    return records


def _parse_row(row_num: int, serial: str, row: dict[str, str]) -> DeviceRecord:
    def field(name: str) -> str:
        return row.get(name, "").strip()

    def enum_field(name: str, enum_cls: type, row_num: int = row_num) -> any:
        raw = field(name).lower()
        try:
            return enum_cls(raw)
        except ValueError:
            valid = [e.value for e in enum_cls]
            raise CSVValidationError(
                f"Row {row_num}: invalid {name}='{raw}'. Valid values: {valid}"
            )

    source_type = enum_field("source_type", SourceType)
    hardware_series = enum_field("hardware_series", HardwareSeries)
    target_account = enum_field("target_account", TargetAccount)
    persona = enum_field("persona", Persona)

    firmware_target = field("firmware_target")
    if not firmware_target:
        raise CSVValidationError(f"Row {row_num}: firmware_target is required.")

    record = DeviceRecord(
        serial_number=serial,
        source_type=source_type,
        hardware_series=hardware_series,
        target_account=target_account,
        target_site=field("target_site"),
        target_group=field("target_group"),
        persona=persona,
        firmware_target=firmware_target,
        mac_address=field("mac_address") or None,
        notes=field("notes") or None,
        vlan_config_file=field("vlan_config_file") or None,
        vlan_interface_config_file=field("vlan_interface_config_file") or None,
    )

    # AOS-S cannot run AOS 10 — skip firmware upgrade with a warning
    if hardware_series == HardwareSeries.AOS_S and firmware_target.startswith("10."):
        logger.warning(
            "Row %d serial=%s: AOS-S hardware cannot run AOS 10 firmware. "
            "firmware_action set to SKIP.",
            row_num,
            serial,
        )
        record.firmware_action = FirmwareAction.SKIP

    return record
