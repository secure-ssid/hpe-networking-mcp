"""Unit tests for src/hpe_networking_mcp/pipeline/vlan_interface_loader.py."""

from __future__ import annotations

import pytest

from hpe_networking_mcp.pipeline.vlan_interface_loader import (
    load_vlan_interface_config_file,
    parse_vlan_interfaces,
    parse_vlan_interfaces_from_file,
)

# ---------------------------------------------------------------------------
# The exact config from SG30LMR164
# ---------------------------------------------------------------------------

_SAMPLE_CONFIG = """\
interface vlan 5
    ip address 10.11.154.2/24
    ip helper-address 10.11.154.19
interface vlan 33
    ip address 172.16.33.2/24
interface vlan 50
    ip dhcp
interface vlan 55
    ip address 10.33.33.2/24
"""


def test_parse_sample_config():
    result = parse_vlan_interfaces(_SAMPLE_CONFIG)
    assert len(result) == 4
    by_vlan = {r["vlan"]: r for r in result}

    assert by_vlan[5]["ip_address"] == "10.11.154.2/24"
    assert by_vlan[5]["helper_address"] == "10.11.154.19"
    assert by_vlan[5]["dhcp"] is False

    assert by_vlan[33]["ip_address"] == "172.16.33.2/24"
    assert by_vlan[33]["helper_address"] is None
    assert by_vlan[33]["dhcp"] is False

    assert by_vlan[50]["ip_address"] is None
    assert by_vlan[50]["dhcp"] is True

    assert by_vlan[55]["ip_address"] == "10.33.33.2/24"
    assert by_vlan[55]["dhcp"] is False


def test_sorted_by_vlan_id():
    result = parse_vlan_interfaces(_SAMPLE_CONFIG)
    assert [r["vlan"] for r in result] == [5, 33, 50, 55]


def test_static_ip_no_helper():
    result = parse_vlan_interfaces("interface vlan 10\n    ip address 192.168.1.1/24\n")
    assert result[0]["ip_address"] == "192.168.1.1/24"
    assert result[0]["helper_address"] is None
    assert result[0]["dhcp"] is False


def test_dhcp_sets_no_ip():
    result = parse_vlan_interfaces("interface vlan 20\n    ip dhcp\n")
    assert result[0]["dhcp"] is True
    assert result[0]["ip_address"] is None


def test_empty_text():
    assert parse_vlan_interfaces("") == []


def test_no_vlan_interfaces():
    assert parse_vlan_interfaces("vlan 10\nvlan 20\n") == []


def test_multiple_helpers_only_last_wins():
    # Only one helper-address per interface is tracked
    text = "interface vlan 5\n    ip address 10.0.0.1/24\n    ip helper-address 1.1.1.1\n    ip helper-address 2.2.2.2\n"
    result = parse_vlan_interfaces(text)
    assert result[0]["helper_address"] == "2.2.2.2"


# ---------------------------------------------------------------------------
# File-based tests
# ---------------------------------------------------------------------------


def test_from_file(tmp_path):
    f = tmp_path / "intfs.cfg"
    f.write_text(_SAMPLE_CONFIG)
    result = parse_vlan_interfaces_from_file(str(f))
    assert len(result) == 4


def test_from_file_not_found():
    with pytest.raises(FileNotFoundError):
        parse_vlan_interfaces_from_file("/nonexistent/intfs.cfg")


def test_load_none_returns_empty():
    assert load_vlan_interface_config_file(None) == []


def test_load_empty_string_returns_empty():
    assert load_vlan_interface_config_file("") == []
