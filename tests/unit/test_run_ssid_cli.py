"""Unit tests for the run_ssid.py CLI's --opmode and --rf-band parsing.

Review finding #1: the 0.4.0 published CLI accepted `--opmode WPA2_PSK`.
`run_ssid._build_parser()` must keep accepting it as a deprecated alias so
0.4.0 scripts/CSV pipelines invoking this CLI keep working, while still
normalizing to the authoritative `WPA2_PERSONAL` value downstream in
`hpe_networking_mcp.pipeline.create_ssid`.

Review finding #2: `--rf-band` offered `24GHZ_ONLY` / `5GHZ_ONLY` / `6GHZ_ONLY`,
none of which are valid values of Central's `Aruba802dot11_Wlan802dot11.rf-band`
enum (confirmed via the vendored OpenAPI spec ingested into
`pipeline.clients.specs_index`: `BAND_NONE, 24GHZ, 5GHZ, 6GHZ, 24GHZ_5GHZ,
24GHZ_6GHZ, 5GHZ_6GHZ, BAND_ALL`). `create_ssid.py` forwards `rf_band` to the
wire with no translation, so picking one of the old `_ONLY` choices sent an
invalid enum value straight to a live Central write. Fixed to the bare
`24GHZ` / `5GHZ` / `6GHZ` single-band forms.
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


def test_rf_band_choices_match_the_central_wire_enum():
    """Every offered choice must be a real value of the Central
    `Aruba802dot11_Wlan802dot11.rf-band` enum -- `create_ssid.py` sends
    `args.rf_band` straight to the wire with no remapping, so an invalid
    choice here reaches a live Central write unmodified."""
    parser = _build_parser()
    rf_band_action = next(a for a in parser._actions if a.dest == "rf_band")
    central_rf_band_enum = {
        "BAND_NONE", "24GHZ", "5GHZ", "6GHZ",
        "24GHZ_5GHZ", "24GHZ_6GHZ", "5GHZ_6GHZ", "BAND_ALL",
    }
    assert set(rf_band_action.choices) <= central_rf_band_enum
    assert not set(rf_band_action.choices) & {"24GHZ_ONLY", "5GHZ_ONLY", "6GHZ_ONLY"}


def test_rf_band_default_is_dual_band():
    parser = _build_parser()
    args = parser.parse_args(["--ssid", "Test", "--vlans", "1000"])
    assert args.rf_band == "24GHZ_5GHZ"


def test_rf_band_accepts_single_band_values():
    parser = _build_parser()
    for band in ("24GHZ", "5GHZ", "6GHZ"):
        args = parser.parse_args(
            ["--ssid", "Test", "--vlans", "1000", "--rf-band", band]
        )
        assert args.rf_band == band


def test_rf_band_rejects_the_old_invalid_only_suffixed_values():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["--ssid", "Test", "--vlans", "1000", "--rf-band", "5GHZ_ONLY"]
        )
