"""Unit tests for the run_ssid.py CLI's --opmode parsing.

Review finding #1: the 0.4.0 published CLI accepted `--opmode WPA2_PSK`.
`run_ssid._build_parser()` must keep accepting it as a deprecated alias so
0.4.0 scripts/CSV pipelines invoking this CLI keep working, while still
normalizing to the authoritative `WPA2_PERSONAL` value downstream in
`hpe_networking_mcp.pipeline.create_ssid`.
"""

from __future__ import annotations

import pytest

from hpe_networking_mcp.cli.run_ssid import _build_parser


def test_opmode_choices_include_deprecated_wpa2_psk_alias():
    parser = _build_parser()
    opmode_action = next(a for a in parser._actions if a.dest == "opmode")
    assert set(opmode_action.choices) == {
        "ENHANCED_OPEN",
        "WPA3_SAE",
        "WPA2_PERSONAL",
        "WPA2_PSK",
    }


def test_opmode_default_is_enhanced_open():
    parser = _build_parser()
    args = parser.parse_args(["--ssid", "Test", "--vlans", "1000"])
    assert args.opmode == "ENHANCED_OPEN"


def test_opmode_accepts_wpa2_psk_alias():
    """The 0.4.0-published `--opmode WPA2_PSK` invocation must still parse
    without argparse rejecting it as an invalid choice."""
    parser = _build_parser()
    args = parser.parse_args(
        ["--ssid", "Test", "--vlans", "1000", "--opmode", "WPA2_PSK"]
    )
    assert args.opmode == "WPA2_PSK"


def test_opmode_accepts_canonical_wpa2_personal():
    parser = _build_parser()
    args = parser.parse_args(
        ["--ssid", "Test", "--vlans", "1000", "--opmode", "WPA2_PERSONAL"]
    )
    assert args.opmode == "WPA2_PERSONAL"


def test_opmode_rejects_unknown_value():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["--ssid", "Test", "--vlans", "1000", "--opmode", "NOT_A_MODE"]
        )
