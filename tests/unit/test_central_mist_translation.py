"""Unit tests for the Central <-> Mist WLAN/site translation seams.

Reference-capability audit note: see
``hpe_networking_mcp.mcp_servers.central_mist_translation`` module docstring
for provenance -- this is an original implementation closing a capability
gap (no cross-platform concept-translation helpers previously existed in
this repo), grounded in this repo's own reviewed Central/Mist schemas.
"""

from __future__ import annotations

from hpe_networking_mcp.mcp_servers.central_mist_translation import (
    translate_central_site_to_mist_site,
    translate_central_wlan_to_mist_wlan,
    translate_mist_site_to_central_site,
    translate_mist_wlan_to_central_wlan,
)


class TestCentralToMistWlan:
    def test_basic_open_ssid_fields_map_across(self):
        profile = {
            "ssid_name": "Guest-WiFi",
            "vlan_ids": ["200"],
            "opmode": "OPEN",
            "hide_ssid": False,
            "max_clients": 512,
            "client_isolation": True,
            "inactivity_timeout": 1000,
            "dtim_period": 1,
        }

        result = translate_central_wlan_to_mist_wlan(profile)

        wlan = result["wlan"]
        assert wlan["ssid"] == "Guest-WiFi"
        assert wlan["vlan_ids"] == ["200"]
        assert wlan["vlan_id"] == 200
        assert wlan["hide_ssid"] is False
        assert wlan["max_num_clients"] == 512
        assert wlan["isolation"] is True
        assert wlan["max_idletime"] == 1000
        assert wlan["dtim"] == 1
        assert wlan["auth"] == {"type": "open"}
        # opmode mapping is a documented best-effort caveat -> always warns.
        assert any("best-effort" in w for w in result["warnings"])

    def test_psk_opmode_carries_passphrase(self):
        profile = {
            "ssid_name": "Corp-WiFi",
            "vlan_ids": ["10"],
            "opmode": "WPA2_PERSONAL",
            "wpa_passphrase": "s3cr3t-passphrase",
        }

        result = translate_central_wlan_to_mist_wlan(profile)

        assert result["wlan"]["auth"] == {"type": "psk", "psk": "s3cr3t-passphrase"}

    def test_unmapped_opmode_warns_and_omits_auth(self):
        result = translate_central_wlan_to_mist_wlan(
            {"ssid_name": "Weird", "opmode": "SOME_FUTURE_MODE"}
        )

        assert "auth" not in result["wlan"]
        assert any("unmapped Central opmode" in w for w in result["warnings"])

    def test_band_all_leaves_band_unset(self):
        result = translate_central_wlan_to_mist_wlan(
            {"ssid_name": "AllBands", "rf_band": "BAND_ALL"}
        )

        assert "band" not in result["wlan"]
        assert result["warnings"] == []

    def test_specific_band_maps(self):
        result = translate_central_wlan_to_mist_wlan(
            {"ssid_name": "5G-only", "rf_band": "BAND_5"}
        )

        assert result["wlan"]["band"] == "5"

    def test_unrecognized_keys_are_ignored_not_passed_through(self):
        result = translate_central_wlan_to_mist_wlan(
            {"ssid_name": "X", "some_central_only_field": "value"}
        )

        assert "some_central_only_field" not in result["wlan"]

    def test_non_numeric_single_vlan_warns(self):
        result = translate_central_wlan_to_mist_wlan(
            {"ssid_name": "X", "vlan_ids": ["not-a-number"]}
        )

        assert result["wlan"]["vlan_ids"] == ["not-a-number"]
        assert "vlan_id" not in result["wlan"]
        assert any("not numeric" in w for w in result["warnings"])


class TestMistToCentralWlan:
    def test_basic_fields_map_across(self):
        wlan = {
            "ssid": "Guest-WiFi",
            "vlan_id": 200,
            "enabled": True,
            "hide_ssid": False,
            "max_num_clients": 512,
            "isolation": True,
            "max_idletime": 1000,
            "dtim": 1,
            "band": "5",
            "auth": {"type": "open"},
        }

        result = translate_mist_wlan_to_central_wlan(wlan)

        profile = result["profile"]
        assert profile["ssid_name"] == "Guest-WiFi"
        assert profile["vlan_ids"] == ["200"]
        assert profile["enabled"] is True
        assert profile["hide_ssid"] is False
        assert profile["max_clients"] == 512
        assert profile["client_isolation"] is True
        assert profile["inactivity_timeout"] == 1000
        assert profile["dtim_period"] == 1
        assert profile["rf_band"] == "BAND_5"
        assert profile["opmode"] == "OPEN"

    def test_psk_auth_carries_passphrase(self):
        wlan = {"ssid": "Corp-WiFi", "auth": {"type": "psk", "psk": "s3cr3t"}}

        result = translate_mist_wlan_to_central_wlan(wlan)

        assert result["profile"]["opmode"] == "WPA2_PERSONAL"
        assert result["profile"]["wpa_passphrase"] == "s3cr3t"

    def test_unmapped_auth_type_warns(self):
        result = translate_mist_wlan_to_central_wlan(
            {"ssid": "X", "auth": {"type": "some-future-auth"}}
        )

        assert "opmode" not in result["profile"]
        assert any("unmapped Mist auth.type" in w for w in result["warnings"])

    def test_vlan_ids_preferred_over_vlan_id(self):
        result = translate_mist_wlan_to_central_wlan(
            {"ssid": "X", "vlan_ids": [10, 20], "vlan_id": 10}
        )

        assert result["profile"]["vlan_ids"] == ["10", "20"]


class TestWlanRoundTrip:
    def test_central_to_mist_to_central_preserves_core_fields(self):
        original = {
            "ssid_name": "Roundtrip-WiFi",
            "vlan_ids": ["300"],
            "opmode": "OPEN",
            "hide_ssid": True,
            "max_clients": 100,
        }

        mist_shape = translate_central_wlan_to_mist_wlan(original)["wlan"]
        back = translate_mist_wlan_to_central_wlan(mist_shape)["profile"]

        assert back["ssid_name"] == "Roundtrip-WiFi"
        assert back["vlan_ids"] == ["300"]
        assert back["hide_ssid"] is True
        assert back["max_clients"] == 100
        assert back["opmode"] == "OPEN"


class TestCentralToMistSite:
    def test_full_address_and_coordinates_map_across(self):
        site = {
            "name": "HQ",
            "address": "3000 Hanover St",
            "city": "Palo Alto",
            "state": "CA",
            "zipcode": "94304",
            "country": "US",
            "latitude": 37.4,
            "longitude": -122.1,
        }

        result = translate_central_site_to_mist_site(site)

        mist_site = result["site"]
        assert mist_site["name"] == "HQ"
        assert mist_site["address"] == "3000 Hanover St, Palo Alto, CA, 94304"
        assert mist_site["country_code"] == "US"
        assert mist_site["latlng"] == {"lat": 37.4, "lng": -122.1}
        assert result["warnings"] == []

    def test_non_alpha2_country_warns(self):
        result = translate_central_site_to_mist_site({"name": "X", "country": "United States"})

        assert result["site"]["country_code"] == "United States"
        assert any("2-letter code" in w for w in result["warnings"])

    def test_minimal_site_has_no_optional_keys(self):
        result = translate_central_site_to_mist_site({"name": "Minimal"})

        assert result["site"] == {"name": "Minimal"}
        assert result["warnings"] == []


class TestMistToCentralSite:
    def test_basic_fields_map_across(self):
        site = {
            "name": "Branch-1",
            "address": "1 Mist Way",
            "country_code": "US",
            "latlng": {"lat": 10.0, "lng": 20.0},
        }

        result = translate_mist_site_to_central_site(site)

        central_site = result["site"]
        assert central_site["name"] == "Branch-1"
        assert central_site["address"] == "1 Mist Way"
        assert central_site["country"] == "US"
        assert central_site["latitude"] == 10.0
        assert central_site["longitude"] == 20.0
        assert any("city/state/zipcode" in w for w in result["warnings"])

    def test_no_address_no_warning(self):
        result = translate_mist_site_to_central_site({"name": "NoAddress"})

        assert result["warnings"] == []
