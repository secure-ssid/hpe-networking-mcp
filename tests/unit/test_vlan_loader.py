"""Unit tests for src/hpe_networking_mcp/pipeline/vlan_loader.py."""

from __future__ import annotations

import pytest

from hpe_networking_mcp.pipeline.vlan_loader import load_vlan_config_file, parse_vlans_from_aos8_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path, content: str) -> str:
    p = tmp_path / "switch.cfg"
    p.write_text(content)
    return str(p)


# ---------------------------------------------------------------------------
# parse_vlans_from_aos8_config
# ---------------------------------------------------------------------------


def test_bare_numeric_vlans(tmp_path):
    cfg = _write_config(tmp_path, "vlan 10\nvlan 20\nvlan 30\n")
    result = parse_vlans_from_aos8_config(cfg)
    assert result == [
        {"vlan": 10, "name": "10", "enable": True},
        {"vlan": 20, "name": "20", "enable": True},
        {"vlan": 30, "name": "30", "enable": True},
    ]


def test_named_vlans(tmp_path):
    cfg = _write_config(
        tmp_path,
        "vlan-name Corp_Data\nvlan Corp_Data 100\nvlan-name Printers\nvlan Printers 200\n",
    )
    result = parse_vlans_from_aos8_config(cfg)
    assert len(result) == 2
    id_to_name = {r["vlan"]: r["name"] for r in result}
    assert id_to_name[100] == "Corp_Data"
    assert id_to_name[200] == "Printers"


def test_mixed_bare_and_named(tmp_path):
    cfg = _write_config(
        tmp_path,
        "vlan 17\nvlan 18\nvlan-name SEW_Micros_Tablets\nvlan SEW_Micros_Tablets 17\n",
    )
    result = parse_vlans_from_aos8_config(cfg)
    # vlan 17 has a name; vlan 18 uses numeric fallback
    id_to_name = {r["vlan"]: r["name"] for r in result}
    assert id_to_name[17] == "SEW_Micros_Tablets"
    assert id_to_name[18] == "18"


def test_reserved_vlans_excluded(tmp_path):
    cfg = _write_config(tmp_path, "vlan 1\nvlan 50\nvlan 4094\n")
    result = parse_vlans_from_aos8_config(cfg)
    ids = [r["vlan"] for r in result]
    assert 1 not in ids
    assert 4094 not in ids
    assert 50 in ids


def test_sorted_output(tmp_path):
    cfg = _write_config(tmp_path, "vlan 300\nvlan 10\nvlan 50\n")
    result = parse_vlans_from_aos8_config(cfg)
    assert [r["vlan"] for r in result] == [10, 50, 300]


def test_all_enable_true(tmp_path):
    cfg = _write_config(tmp_path, "vlan 10\nvlan 20\n")
    result = parse_vlans_from_aos8_config(cfg)
    assert all(r["enable"] is True for r in result)


def test_file_not_found():
    with pytest.raises(FileNotFoundError):
        parse_vlans_from_aos8_config("/nonexistent/path/switch.cfg")


def test_empty_config(tmp_path):
    cfg = _write_config(tmp_path, "")
    result = parse_vlans_from_aos8_config(cfg)
    assert result == []


# ---------------------------------------------------------------------------
# load_vlan_config_file (wrapper)
# ---------------------------------------------------------------------------


def test_load_none_returns_empty():
    assert load_vlan_config_file(None) == []


def test_load_empty_string_returns_empty():
    assert load_vlan_config_file("") == []


def test_load_valid_file(tmp_path):
    cfg = _write_config(tmp_path, "vlan 10\nvlan 20\n")
    result = load_vlan_config_file(str(cfg))
    assert len(result) == 2
