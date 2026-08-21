"""Unit tests for ``device_type_for_troubleshoot`` (H17).

Covers the two-part switch-routing fix:

1. Explicit ``device_type`` normalization:
   - AOS-S aliases ('AOS_S' / 'AOSS' / 'aos-s', any case) map to 'aos-s'
     (previously 'AOS_S'.lower() produced the invalid endpoint 'aos_s').
   - Other explicit mappings ('AP' / 'GATEWAY' / 'CX') are unchanged.
2. Auto-detect disambiguation when inventory ``deviceType`` is SWITCH:
   firmware/softwareVersion prefix routes CX vs AOS-S (FL.10 -> 'cx',
   WC.16 -> 'aos-s'), so SWITCH no longer routes everything to 'cx'.

``get_mcp_client`` is mocked so no network is touched.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hpe_networking_mcp.mcp_servers import shared
from hpe_networking_mcp.mcp_servers.shared import device_type_for_troubleshoot

# ---------------------------------------------------------------------------
# Explicit device_type normalization
# ---------------------------------------------------------------------------


class TestExplicitDeviceType:
    @pytest.mark.parametrize("value", ["AOS_S", "AOSS", "aos-s", "AOS-S", "aos_s", "Aoss"])
    def test_aos_s_aliases_map_to_aos_s(self, value):
        # No inventory lookup needed when device_type is supplied.
        assert device_type_for_troubleshoot("SERIAL1", value) == "aos-s"

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("AP", "aps"),
            ("ACCESS_POINT", "aps"),
            ("CX", "cx"),
            ("GATEWAY", "gateways"),
            ("GW", "gateways"),
        ],
    )
    def test_known_mappings_unchanged(self, value, expected):
        assert device_type_for_troubleshoot("SERIAL1", value) == expected


# ---------------------------------------------------------------------------
# Auto-detect from inventory (deviceType == SWITCH)
# ---------------------------------------------------------------------------


def _patch_inventory(monkeypatch, device: dict | None) -> None:
    """Patch get_mcp_client so get_device_by_serial returns ``device``."""
    mock_client = MagicMock()
    mock_client.get_device_by_serial.return_value = device
    monkeypatch.setattr(shared, "get_mcp_client", lambda: mock_client)


class TestAutoDetectSwitch:
    def test_cx_firmware_routes_to_cx(self, monkeypatch):
        _patch_inventory(
            monkeypatch,
            {"serialNumber": "SW1", "deviceType": "SWITCH", "firmwareVersion": "FL.10.16.1006", "model": "6300M"},
        )
        assert device_type_for_troubleshoot("SW1", None) == "cx"

    def test_aos_s_firmware_routes_to_aos_s(self, monkeypatch):
        _patch_inventory(
            monkeypatch,
            {"serialNumber": "SW2", "deviceType": "SWITCH", "firmwareVersion": "WC.16.11.0010", "model": "2930F"},
        )
        assert device_type_for_troubleshoot("SW2", None) == "aos-s"

    def test_cx_by_model_when_no_firmware(self, monkeypatch):
        _patch_inventory(
            monkeypatch,
            {"serialNumber": "SW3", "deviceType": "SWITCH", "model": "6300M"},
        )
        assert device_type_for_troubleshoot("SW3", None) == "cx"

    def test_aos_s_by_model_when_no_firmware(self, monkeypatch):
        _patch_inventory(
            monkeypatch,
            {"serialNumber": "SW4", "deviceType": "SWITCH", "model": "2930F"},
        )
        assert device_type_for_troubleshoot("SW4", None) == "aos-s"

    def test_ambiguous_switch_defaults_to_cx(self, monkeypatch):
        _patch_inventory(
            monkeypatch,
            {"serialNumber": "SW5", "deviceType": "SWITCH"},
        )
        assert device_type_for_troubleshoot("SW5", None) == "cx"

    def test_ap_auto_detect(self, monkeypatch):
        _patch_inventory(monkeypatch, {"serialNumber": "AP1", "deviceType": "ACCESS_POINT"})
        assert device_type_for_troubleshoot("AP1", None) == "aps"

    def test_gateway_auto_detect(self, monkeypatch):
        _patch_inventory(monkeypatch, {"serialNumber": "GW1", "deviceType": "GATEWAY"})
        assert device_type_for_troubleshoot("GW1", None) == "gateways"

    def test_missing_device_returns_none(self, monkeypatch):
        _patch_inventory(monkeypatch, None)
        assert device_type_for_troubleshoot("NOPE", None) is None
