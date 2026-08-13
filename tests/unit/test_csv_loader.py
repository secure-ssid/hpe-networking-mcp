"""Tests for src/hpe_networking_mcp/pipeline/csv_loader.py."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from hpe_networking_mcp.pipeline.csv_loader import CSVValidationError, load_csv
from hpe_networking_mcp.pipeline.models import FirmwareAction, HardwareSeries, SourceType, TargetAccount


def _write_csv(tmp_path: Path, content: str) -> str:
    f = tmp_path / "test.csv"
    f.write_text(textwrap.dedent(content))
    return str(f)


VALID_ROW = (
    "serial_number,source_type,hardware_series,target_account,"
    "target_site,target_group,persona,firmware_target,mac_address,notes\n"
    "CN001,unmanaged,aos_cx,same,HQ,Onboarding,access_switch,10.13.1010,,\n"
)


def test_load_valid_csv(tmp_path):
    path = _write_csv(tmp_path, VALID_ROW)
    records = load_csv(path)
    assert len(records) == 1
    r = records[0]
    assert r.serial_number == "CN001"
    assert r.source_type == SourceType.UNMANAGED
    assert r.hardware_series == HardwareSeries.AOS_CX
    assert r.target_account == TargetAccount.SAME
    assert r.firmware_action == FirmwareAction.UPGRADE


def test_duplicate_serial_raises(tmp_path):
    content = VALID_ROW + "CN001,unmanaged,aos_cx,same,HQ,Onboarding,access_switch,10.13.1010,,\n"
    path = _write_csv(tmp_path, content)
    with pytest.raises(CSVValidationError, match="duplicate"):
        load_csv(path)


def test_missing_required_column_raises(tmp_path):
    content = "serial_number,source_type\nCN001,unmanaged\n"
    path = _write_csv(tmp_path, content)
    with pytest.raises(CSVValidationError, match="missing required columns"):
        load_csv(path)


def test_invalid_enum_raises(tmp_path):
    content = (
        "serial_number,source_type,hardware_series,target_account,"
        "target_site,target_group,persona,firmware_target\n"
        "CN002,INVALID,aos_cx,same,HQ,G1,access_switch,10.0\n"
    )
    path = _write_csv(tmp_path, content)
    with pytest.raises(CSVValidationError, match="source_type"):
        load_csv(path)


def test_aos_s_with_aos10_firmware_sets_skip(tmp_path):
    content = (
        "serial_number,source_type,hardware_series,target_account,"
        "target_site,target_group,persona,firmware_target\n"
        "CN003,classic_central,aos_s,same,HQ,Legacy,access_switch,10.13.1010\n"
    )
    path = _write_csv(tmp_path, content)
    records = load_csv(path)
    assert records[0].firmware_action == FirmwareAction.SKIP


def test_file_not_found():
    with pytest.raises(FileNotFoundError):
        load_csv("/nonexistent/path/file.csv")
