"""Unit tests for src/hpe_networking_mcp/pipeline/create_ssid.py."""

from __future__ import annotations

from unittest.mock import MagicMock, call

from hpe_networking_mcp.pipeline.create_ssid import (
    _build_ssid_body,
    build_overlay_ssid,
    build_underlay_ssid,
    delete_underlay_ssid,
    get_underlay_ssid,
    list_underlay_ssids,
)

# ---------------------------------------------------------------------------
# _build_ssid_body
# ---------------------------------------------------------------------------


def test_build_ssid_body_forward_mode():
    body = _build_ssid_body("TestSSID", ["1000"])
    assert body["forward-mode"] == "FORWARD_MODE_BRIDGE"


def test_build_ssid_body_vlan_selector():
    body = _build_ssid_body("TestSSID", ["1000", "1001"])
    assert body["vlan-selector"] == "VLAN_RANGES"
    assert body["vlan-id-range"] == ["1000", "1001"]


def test_build_ssid_body_essid_matches_ssid():
    body = _build_ssid_body("My SSID", ["200"])
    assert body["ssid"] == "My SSID"
    assert body["essid"]["name"] == "My SSID"


def test_build_ssid_body_defaults():
    body = _build_ssid_body("X", ["1"])
    assert body["enable"] is True
    assert body["opmode"] == "OPEN"
    assert body["hide-ssid"] is False
    assert body["client-isolation"] is False
    assert body["high-efficiency"]["enable"] is True


def test_build_ssid_body_custom_opmode():
    body = _build_ssid_body("X", ["1"], opmode="WPA3_SAE")
    assert body["opmode"] == "WPA3_SAE"


def test_build_ssid_body_wpa3_passphrase():
    body = _build_ssid_body("X", ["1"], opmode="WPA3_SAE", wpa_passphrase="ilikeelephants")
    assert body["personal-security"]["wpa-passphrase"] == "ilikeelephants"
    assert body["personal-security"]["passphrase-format"] == "STRING"


def test_build_ssid_body_wpa2_passphrase():
    body = _build_ssid_body("X", ["1"], opmode="WPA2_PERSONAL", wpa_passphrase="mypassword")
    assert "personal-security" in body


def test_build_ssid_body_normalizes_deprecated_wpa2_psk_alias():
    """`WPA2_PSK` is not a valid New Central opmode (the real enum member is
    `WPA2_PERSONAL` — see docs/aos8-migration-contract-matrix.md §4), but it
    was accepted by the published 0.4.0 CLI. It is kept as a deprecated
    alias and normalized to `WPA2_PERSONAL` before payload/security
    branching, so a caller still passing it gets the same passphrase
    handling as passing `WPA2_PERSONAL` directly."""
    body = _build_ssid_body("X", ["1"], opmode="WPA2_PSK", wpa_passphrase="mypassword")
    assert body["opmode"] == "WPA2_PERSONAL"
    assert body["personal-security"]["wpa-passphrase"] == "mypassword"


def test_build_ssid_body_wpa2_psk_alias_logs_deprecation_warning(caplog):
    import logging

    from hpe_networking_mcp.pipeline.create_ssid import reset_opmode_deprecation_warnings

    # The warning is deduplicated per process, so reset the cache first —
    # another test in this session may already have tripped it.
    reset_opmode_deprecation_warnings()
    with caplog.at_level(logging.WARNING, logger="hpe_networking_mcp.pipeline.create_ssid"):
        _build_ssid_body("X", ["1"], opmode="WPA2_PSK", wpa_passphrase="mypassword")
    assert any(
        "WPA2_PSK" in record.message and "deprecated" in record.message
        for record in caplog.records
    )


def test_build_ssid_body_other_unrecognized_opmode_still_unmapped():
    """Any opmode other than the deprecated `WPA2_PSK` alias remains
    treated as unrecognized — no silent extra aliasing is introduced."""
    body = _build_ssid_body("X", ["1"], opmode="BOGUS_MODE", wpa_passphrase="mypassword")
    assert body["opmode"] == "BOGUS_MODE"
    assert "personal-security" not in body


def test_build_ssid_body_no_passphrase_for_open():
    """ENHANCED_OPEN should never include personal-security even if passphrase passed."""
    body = _build_ssid_body("X", ["1"], opmode="ENHANCED_OPEN", wpa_passphrase="ignored")
    assert "personal-security" not in body


# ---------------------------------------------------------------------------
# build_underlay_ssid — dry-run
# ---------------------------------------------------------------------------


def test_build_underlay_ssid_dry_run_no_api_calls():
    client = MagicMock()
    result = build_underlay_ssid(client, "Test", ["1000"], "99999", dry_run=True)
    client.post.assert_not_called()
    assert result["created"] is True
    assert result["scope_mapped"] is True
    assert result["errors"] == []


# ---------------------------------------------------------------------------
# build_underlay_ssid — happy path
# ---------------------------------------------------------------------------


def test_build_underlay_ssid_creates_and_maps():
    client = MagicMock()
    client.post.return_value = {"errorCode": "SUCC_001"}

    result = build_underlay_ssid(client, "Corp-WiFi", ["1000"], "79236221864456192")

    assert result["created"] is True
    assert result["scope_mapped"] is True
    assert result["errors"] == []

    # Step 2: create SSID
    first_call = client.post.call_args_list[0]
    assert first_call == call(
        "/network-config/v1/wlan-ssids/Corp-WiFi",
        data={
            **_build_ssid_body("Corp-WiFi", ["1000"]),
        },
    )

    # Step 3: scope-map
    second_call = client.post.call_args_list[1]
    assert second_call == call(
        "/network-config/v1/scope-maps",
        data={
            "scope-map": [
                {
                    "scope-name": "79236221864456192",
                    "scope-id": 79236221864456192,
                    "persona": "CAMPUS_AP",
                    "resource": "wlan-ssids/Corp-WiFi",
                }
            ]
        },
    )


def test_build_underlay_ssid_url_encodes_spaces():
    """SSID with spaces must use %20 in the URL path."""
    client = MagicMock()
    client.post.return_value = {}

    build_underlay_ssid(client, "Vanity Group", ["500"], "99999")

    create_call = client.post.call_args_list[0]
    assert create_call[0][0] == "/network-config/v1/wlan-ssids/Vanity%20Group"


def test_build_underlay_ssid_body_preserves_spaces():
    """Body fields must keep the original spaces (not %20)."""
    client = MagicMock()
    client.post.return_value = {}

    build_underlay_ssid(client, "Vanity Group", ["500"], "99999")

    body = client.post.call_args_list[0][1]["data"]
    assert body["ssid"] == "Vanity Group"
    assert body["essid"]["name"] == "Vanity Group"


# ---------------------------------------------------------------------------
# build_underlay_ssid — deprecated WPA2_PSK opmode alias (0.4.0 CLI
# backward compatibility, review finding #1)
# ---------------------------------------------------------------------------


def test_build_underlay_ssid_wpa2_psk_alias_normalizes_payload_and_warns():
    client = MagicMock()
    client.post.return_value = {"errorCode": "SUCC_001"}

    result = build_underlay_ssid(
        client, "Corp-WiFi", ["1000"], "99999",
        opmode="WPA2_PSK", wpa_passphrase="mypassword",
    )

    assert result["errors"] == []
    assert any("WPA2_PSK" in w and "WPA2_PERSONAL" in w for w in result["warnings"])

    create_call = client.post.call_args_list[0]
    body = create_call.kwargs["data"]
    assert body["opmode"] == "WPA2_PERSONAL"
    assert body["personal-security"]["wpa-passphrase"] == "mypassword"


def test_build_underlay_ssid_canonical_wpa2_personal_has_no_warning():
    client = MagicMock()
    client.post.return_value = {"errorCode": "SUCC_001"}

    result = build_underlay_ssid(
        client, "Corp-WiFi", ["1000"], "99999",
        opmode="WPA2_PERSONAL", wpa_passphrase="mypassword",
    )

    assert result["warnings"] == []
    body = client.post.call_args_list[0].kwargs["data"]
    assert body["opmode"] == "WPA2_PERSONAL"


# ---------------------------------------------------------------------------
# build_underlay_ssid — duplicate / already-exists handling
# ---------------------------------------------------------------------------


def _make_http_exc(status: int, text: str) -> Exception:
    exc = Exception("HTTP error")
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    exc.response = resp
    return exc


def test_build_underlay_ssid_create_duplicate_continues_to_scope_map():
    client = MagicMock()
    client.post.side_effect = [
        _make_http_exc(409, "duplicate entry"),  # create returns duplicate
        {"errorCode": "SUCC_001"},               # scope-map succeeds
    ]

    result = build_underlay_ssid(client, "TestSSID", ["1000"], "99999")

    assert result["created"] is True   # treated as success
    assert result["scope_mapped"] is True
    assert result["errors"] == []


def test_build_underlay_ssid_scope_map_already_exists_is_ok():
    client = MagicMock()
    client.post.side_effect = [
        {"errorCode": "SUCC_001"},
        _make_http_exc(409, "scope-map already exists"),
    ]

    result = build_underlay_ssid(client, "TestSSID", ["1000"], "99999")

    assert result["scope_mapped"] is True
    assert result["errors"] == []


def test_build_underlay_ssid_create_hard_failure_aborts():
    client = MagicMock()
    client.post.side_effect = _make_http_exc(500, "internal server error")

    result = build_underlay_ssid(client, "TestSSID", ["1000"], "99999")

    assert result["created"] is False
    assert result["scope_mapped"] is False
    assert len(result["errors"]) == 1
    assert "create_ssid" in result["errors"][0]
    # scope-map must NOT be attempted after a hard create failure
    assert client.post.call_count == 1


def test_build_underlay_ssid_scope_map_hard_failure_recorded():
    client = MagicMock()
    client.post.side_effect = [
        {"errorCode": "SUCC_001"},
        _make_http_exc(400, "bad request"),
    ]

    result = build_underlay_ssid(client, "TestSSID", ["1000"], "99999")

    assert result["created"] is True
    assert result["scope_mapped"] is False
    assert any("scope_map" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# delete_underlay_ssid
# ---------------------------------------------------------------------------


def test_delete_underlay_ssid_dry_run():
    client = MagicMock()
    result = delete_underlay_ssid(client, "Corp-WiFi", dry_run=True)
    client.delete.assert_not_called()
    assert result["deleted"] is True


def test_delete_underlay_ssid_success():
    client = MagicMock()
    client.delete.return_value = {}
    result = delete_underlay_ssid(client, "Corp-WiFi")
    client.delete.assert_called_once_with("/network-config/v1/wlan-ssids/Corp-WiFi")
    assert result["deleted"] is True
    assert result["errors"] == []


def test_delete_underlay_ssid_url_encodes_spaces():
    client = MagicMock()
    client.delete.return_value = {}
    delete_underlay_ssid(client, "My SSID")
    client.delete.assert_called_once_with("/network-config/v1/wlan-ssids/My%20SSID")


def test_delete_underlay_ssid_failure():
    client = MagicMock()
    client.delete.side_effect = Exception("not found")
    result = delete_underlay_ssid(client, "Corp-WiFi")
    assert result["deleted"] is False
    assert len(result["errors"]) == 1


# ---------------------------------------------------------------------------
# get_underlay_ssid
# ---------------------------------------------------------------------------


def test_get_underlay_ssid_found():
    client = MagicMock()
    client.get.return_value = {"ssid": "Corp-WiFi"}
    result = get_underlay_ssid(client, "Corp-WiFi")
    assert result == {"ssid": "Corp-WiFi"}


def test_get_underlay_ssid_not_found_returns_none():
    client = MagicMock()
    exc = Exception("not found")
    resp = MagicMock()
    resp.status_code = 404
    exc.response = resp
    client.get.side_effect = exc
    assert get_underlay_ssid(client, "Missing") is None


# ---------------------------------------------------------------------------
# list_underlay_ssids
# ---------------------------------------------------------------------------


def test_list_underlay_ssids_returns_items():
    client = MagicMock()
    client.get.return_value = {"wlan-ssids": [{"ssid": "A"}, {"ssid": "B"}]}
    items = list_underlay_ssids(client)
    assert len(items) == 2


def test_list_underlay_ssids_empty_on_error():
    client = MagicMock()
    client.get.side_effect = Exception("connection error")
    assert list_underlay_ssids(client) == []



# ---------------------------------------------------------------------------
# build_overlay_ssid — overlay policy-group write validation (Finding #2:
# the raw `_request()` PATCH call bypassed response validation and would
# log success even on a non-2xx result. The fix reuses the already-validated
# `.patch()` wrapper, which calls `response.raise_for_status()`.)
# ---------------------------------------------------------------------------


def _overlay_client(patch_side_effect=None) -> MagicMock:
    client = MagicMock()
    client.get.return_value = {"scopeId": "1"}
    client.post.return_value = {"errorCode": "SUCC_001"}
    if patch_side_effect is not None:
        client.patch.side_effect = patch_side_effect
    else:
        client.patch.return_value = {}
    return client


def test_build_overlay_ssid_policy_group_write_failure_is_recorded_not_logged_success():
    """A non-2xx (raised by the validated `.patch()` wrapper) on the
    policy-group add must be recorded in `errors`, never silently treated as
    success."""
    client = _overlay_client(
        patch_side_effect=[_make_http_exc(500, "internal server error"), {}]
    )
    result = build_overlay_ssid(
        client,
        "Overlay-WiFi",
        ["200"],
        "99999",
        "cluster1",
        "88888",
    )
    assert any("add_policy_group" in err for err in result["errors"])


def test_build_overlay_ssid_policy_group_write_success_uses_validated_patch():
    """The overlay policy-group add must go through the validated `.patch()`
    wrapper (never the raw, unchecked `._request()` primitive) with the
    exact spec-correct collection-body payload."""
    client = _overlay_client()
    result = build_overlay_ssid(
        client,
        "Overlay-WiFi",
        ["200"],
        "99999",
        "cluster1",
        "88888",
    )
    assert not any("add_policy_group" in err for err in result["errors"])
    patch_calls = [
        call_args
        for call_args in client.patch.call_args_list
        if call_args.args and call_args.args[0] == "/network-config/v1alpha1/policy-groups"
    ]
    assert len(patch_calls) == 1
    assert patch_calls[0].kwargs["data"] == {
        "policy-group": {
            "policy-group-list": [{"name": "Overlay-WiFi", "position": 3}]
        }
    }


# ---------------------------------------------------------------------------
# build_overlay_ssid — deprecated WPA2_PSK opmode alias (0.4.0 CLI
# backward compatibility, review finding #1)
# ---------------------------------------------------------------------------


def test_build_overlay_ssid_wpa2_psk_alias_normalizes_payload_and_warns():
    client = _overlay_client()
    result = build_overlay_ssid(
        client,
        "Overlay-WiFi",
        ["200"],
        "99999",
        "cluster1",
        "88888",
        opmode="WPA2_PSK",
        wpa_passphrase="mypassword",
    )

    assert any("WPA2_PSK" in w and "WPA2_PERSONAL" in w for w in result["warnings"])

    ssid_calls = [
        call_args
        for call_args in client.post.call_args_list
        if call_args.args and call_args.args[0] == "/network-config/v1/wlan-ssids/Overlay-WiFi"
    ]
    assert len(ssid_calls) == 1
    body = ssid_calls[0].kwargs["data"]
    assert body["opmode"] == "WPA2_PERSONAL"
    assert body["personal-security"]["wpa-passphrase"] == "mypassword"


def test_build_overlay_ssid_canonical_wpa2_personal_has_no_warning():
    client = _overlay_client()
    result = build_overlay_ssid(
        client,
        "Overlay-WiFi",
        ["200"],
        "99999",
        "cluster1",
        "88888",
        opmode="WPA2_PERSONAL",
        wpa_passphrase="mypassword",
    )
    assert result["warnings"] == []
