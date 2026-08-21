"""Unit tests for the generic network-profile helpers (get/set/delete_network_profile)
and the routing-overlay / HA / telemetry / application-experience / config-checkpoint
build_* workflow tools in config.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hpe_networking_mcp.mcp_servers import config


def _resp(status_code=200, payload=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    resp.json.return_value = dict(payload or {})
    resp.text = "{}"
    return resp


# ---------------------------------------------------------------------------
# profile_type validation
# ---------------------------------------------------------------------------


def test_profile_base_rejects_unknown_type():
    with pytest.raises(ValueError, match="profile_type must be one of"):
        config._profile_base("not-a-real-type")


def test_profile_base_resolves_known_type():
    assert config._profile_base("bgp") == "/network-config/v1alpha1/bgp"
    assert config._profile_base("vrf") == "/network-config/v1alpha1/vrfs"
    assert config._profile_base("app-recognition") == "/network-config/v1alpha1/arc"


def test_read_only_profile_base_exposes_route_and_interface_vrrp():
    assert config._profile_base(
        "static-route", read_only=True
    ) == "/network-config/v1alpha1/static-route"
    assert config._profile_base(
        "vrrp-interface", read_only=True
    ) == "/network-config/v1alpha1/vrrp"


@pytest.mark.parametrize("profile_type", ["static-route", "vrrp-interface"])
def test_unverified_evidence_profiles_remain_blocked_for_writes(profile_type):
    with pytest.raises(ValueError, match="profile_type must be one of"):
        config.set_network_profile(
            profile_type,
            "lab-profile",
            {"name": "lab-profile"},
            object_type="LOCAL",
            scope_id="scope-1",
            device_function="GATEWAY",
            dry_run=True,
        )
    with pytest.raises(ValueError, match="profile_type must be one of"):
        config.delete_network_profile(
            profile_type,
            "lab-profile",
            object_type="LOCAL",
            scope_id="scope-1",
            device_function="GATEWAY",
            dry_run=True,
        )


def test_local_object_type_requires_scope_id():
    with pytest.raises(ValueError, match="scope_id is required"):
        config._profile_write_params("LOCAL", None, "GATEWAY")


# ---------------------------------------------------------------------------
# get_network_profile
# ---------------------------------------------------------------------------


def test_get_network_profile_lists_when_name_omitted(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"profile": [{"name": "bgp-1"}]}
    monkeypatch.setattr(config, "get_client", lambda: client)

    result = config.get_network_profile("bgp", limit=10)

    assert result["profile"] == [{"name": "bgp-1"}]
    client.get.assert_called_once_with(
        "/network-config/v1alpha1/bgp", params={"limit": 10, "offset": 0}
    )


def test_get_network_profile_fetches_one_by_name(monkeypatch):
    client = MagicMock()
    client._request.return_value = _resp(payload={"name": "bgp-1", "router": []})
    monkeypatch.setattr(config, "get_client", lambda: client)

    result = config.get_network_profile("bgp", name="bgp-1", scope_id="s1", object_type="LOCAL")

    assert result == {"name": "bgp-1", "router": []}
    client._request.assert_called_once_with(
        "GET", "/network-config/v1alpha1/bgp/bgp-1",
        params={"object-type": "LOCAL", "scope-id": "s1"},
    )


@pytest.mark.parametrize(
    ("profile_type", "endpoint"),
    [
        ("static-route", "/network-config/v1alpha1/static-route"),
        ("vrrp-interface", "/network-config/v1alpha1/vrrp"),
    ],
)
def test_get_network_profile_lists_unverified_evidence_surfaces(
    monkeypatch, profile_type, endpoint
):
    client = MagicMock()
    client.get.return_value = {"profile": []}
    monkeypatch.setattr(config, "get_client", lambda: client)

    result = config.get_network_profile(profile_type, limit=10)

    assert result["profile"] == []
    client.get.assert_called_once_with(endpoint, params={"limit": 10, "offset": 0})


# ---------------------------------------------------------------------------
# set_network_profile / delete_network_profile
# ---------------------------------------------------------------------------


def test_set_network_profile_dry_run_returns_payload_without_sending():
    result = config.set_network_profile(
        "vrf", "vrf-1", {"name": "vrf-1"}, object_type="LOCAL", scope_id="s1", dry_run=True
    )

    assert result["dry_run"] is True
    assert result["payload"] == {"name": "vrf-1"}


def test_set_network_profile_posts_then_patches_on_412(monkeypatch):
    client = MagicMock()
    client._request.side_effect = [
        _resp(status_code=412), _resp(status_code=200, payload={"ok": True})
    ]
    monkeypatch.setattr(config, "get_client", lambda: client)

    result = config.set_network_profile(
        "vrf", "vrf-1", {"name": "vrf-1"}, object_type="LOCAL", scope_id="s1"
    )

    assert result["action"] == "updated"
    assert client._request.call_count == 2
    first_call = client._request.call_args_list[0]
    assert first_call.args[:2] == ("POST", "/network-config/v1alpha1/vrfs/vrf-1")
    second_call = client._request.call_args_list[1]
    assert second_call.args[:2] == ("PATCH", "/network-config/v1alpha1/vrfs/vrf-1")


def test_delete_network_profile_dry_run():
    result = config.delete_network_profile("bgp", "bgp-1", dry_run=True)
    assert result["dry_run"] is True


def test_delete_network_profile_sends_delete(monkeypatch):
    client = MagicMock()
    client._request.return_value = _resp(status_code=204, payload={})
    monkeypatch.setattr(config, "get_client", lambda: client)

    config.delete_network_profile("bgp", "bgp-1", scope_id="s1", object_type="LOCAL")

    client._request.assert_called_once_with(
        "DELETE", "/network-config/v1alpha1/bgp/bgp-1",
        params={"object-type": "LOCAL", "scope-id": "s1"},
    )


# ---------------------------------------------------------------------------
# build_* workflow tools
# ---------------------------------------------------------------------------


def test_build_bgp_overlay_dry_run_shapes_payload():
    result = config.build_bgp_overlay(
        "bgp-1", router=[{"as-number": 65001}], scope_id="s1", dry_run=True
    )

    assert result["dry_run"] is True
    assert result["payload"] == {"name": "bgp-1", "router": [{"as-number": 65001}]}
    assert result["params"] == {
        "object-type": "LOCAL", "scope-id": "s1", "device-function": "GATEWAY"
    }


def test_build_ospf_overlay_rejects_bad_version():
    with pytest.raises(ValueError, match="version must be"):
        config.build_ospf_overlay("ospf-1", {}, scope_id="s1", version="v4")


def test_build_ospf_overlay_dry_run_uses_versioned_profile_type():
    result = config.build_ospf_overlay(
        "ospf-1", {"areas": []}, scope_id="s1", version="v3", dry_run=True
    )

    assert result["profile_type"] == "ospfv3"
    assert result["payload"] == {"areas": [], "name": "ospf-1"}


def test_build_vrf_overlay_dry_run():
    result = config.build_vrf_overlay("vrf-1", {"rd": "65001:1"}, scope_id="s1", dry_run=True)

    assert result["profile_type"] == "vrf"
    assert result["payload"]["rd"] == "65001:1"
    assert result["payload"]["name"] == "vrf-1"


def test_configure_high_availability_creates_both_profiles(monkeypatch):
    client = MagicMock()
    client._request.side_effect = [
        _resp(status_code=200, payload={"vsx": "ok"}),
        _resp(status_code=200, payload={"vrrp": "ok"}),
    ]
    monkeypatch.setattr(config, "get_client", lambda: client)

    result = config.configure_high_availability(
        "vsx-1", {}, "vrrp-1", {}, scope_id="s1", device_function="CX_SWITCH"
    )

    assert result["vsx"]["vsx"] == "ok"
    assert result["vrrp"]["vrrp"] == "ok"
    assert client._request.call_count == 2


def test_enable_telemetry_dry_run():
    result = config.enable_telemetry("tel-1", {"streaming": True}, scope_id="s1", dry_run=True)

    assert result["profile_type"] == "telemetry"
    assert result["payload"] == {"streaming": True, "name": "tel-1"}


def test_configure_application_experience_creates_both_profiles(monkeypatch):
    client = MagicMock()
    client._request.side_effect = [
        _resp(status_code=200, payload={"bw": "ok"}),
        _resp(status_code=200, payload={"arc": "ok"}),
    ]
    monkeypatch.setattr(config, "get_client", lambda: client)

    result = config.configure_application_experience(
        "bw-1", {}, "arc-1", {}, scope_id="s1",
    )

    assert result["app_bandwidth_contract"]["bw"] == "ok"
    assert result["app_recognition"]["arc"] == "ok"


def test_build_config_checkpoint_policy_validates_delay_range():
    with pytest.raises(ValueError, match="post_checkpoint_delay must be between"):
        config.build_config_checkpoint_policy(
            "cp-1", scope_id="s1", device_function="GATEWAY", post_checkpoint_delay=3
        )


def test_build_config_checkpoint_policy_dry_run():
    result = config.build_config_checkpoint_policy(
        "cp-1", scope_id="s1", device_function="GATEWAY", dry_run=True
    )

    assert result["payload"]["post-checkpoint"] is True
    assert result["payload"]["post-checkpoint-delay"] == 300


def test_get_config_rollback_status_reports_no_manual_trigger():
    result = config.get_config_rollback_status()

    assert result["manual_rollback_supported"] is False
    assert result["automatic_rollback_supported"] is True
    assert "build_config_checkpoint_policy" == result["checkpoint_profile_tool"]
