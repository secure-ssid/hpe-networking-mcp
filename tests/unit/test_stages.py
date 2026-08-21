"""Unit tests for pipeline stages — all API calls mocked."""

from __future__ import annotations

from unittest.mock import MagicMock

from hpe_networking_mcp.pipeline.models import FirmwareAction, StageStatus, TargetAccount
from hpe_networking_mcp.pipeline.stages.s1_discover import DiscoverStage
from hpe_networking_mcp.pipeline.stages.s2_validate import ValidateStage
from hpe_networking_mcp.pipeline.stages.s3_offboard import OffboardStage
from hpe_networking_mcp.pipeline.stages.s4_transfer import TransferStage
from hpe_networking_mcp.pipeline.stages.s6_configure import ConfigureStage
from hpe_networking_mcp.pipeline.stages.s7_firmware import FirmwareStage
from hpe_networking_mcp.pipeline.stages.s8_verify import VerifyStage

# ---------------------------------------------------------------------------
# S1 — Discover
# ---------------------------------------------------------------------------


def test_s1_discover_unmanaged_found_via_mcp(
    record_unmanaged, source_ctx, target_ctx, state, run_id
):
    source_ctx.mcp_client.get_device_by_serial.return_value = {
        "model": "CX-6300", "firmwareVersion": "10.10.0"
    }
    result = DiscoverStage()._execute(record_unmanaged, run_id, source_ctx, target_ctx, state, False)
    assert result.status == StageStatus.SUCCESS
    assert record_unmanaged.model == "CX-6300"


def test_s1_discover_unmanaged_falls_back_to_glp(
    record_unmanaged, source_ctx, target_ctx, state, run_id
):
    source_ctx.mcp_client.get_device_by_serial.return_value = None
    source_ctx.glp_client.get_device.return_value = {"model": "CX-8360", "firmwareVersion": "10.9.0"}
    result = DiscoverStage()._execute(record_unmanaged, run_id, source_ctx, target_ctx, state, False)
    assert result.status == StageStatus.SUCCESS
    assert record_unmanaged.model == "CX-8360"


def test_s1_discover_not_found(record_unmanaged, source_ctx, target_ctx, state, run_id):
    source_ctx.mcp_client.get_device_by_serial.return_value = None
    source_ctx.glp_client.get_device.return_value = None
    result = DiscoverStage()._execute(record_unmanaged, run_id, source_ctx, target_ctx, state, False)
    assert result.status == StageStatus.FAILED
    assert "DISCOVERY_FAILED" in result.error


def test_s1_discover_classic_central(record_classic_central, source_ctx, target_ctx, state, run_id):
    source_ctx.central_client.get.return_value = {"model": "CX-6300", "firmwareVersion": "10.10.0"}
    result = DiscoverStage()._execute(record_classic_central, run_id, source_ctx, target_ctx, state, False)
    assert result.status == StageStatus.SUCCESS


def test_s1_discover_resume_skip(record_unmanaged, source_ctx, target_ctx, state, run_id):
    """Stage should be skipped if already succeeded."""
    from hpe_networking_mcp.pipeline.models import StageStatus
    state.set_stage_status(record_unmanaged.serial_number, run_id, "s1_discover", StageStatus.SUCCESS)
    result = DiscoverStage().run(record_unmanaged, run_id, source_ctx, target_ctx, state)
    assert result.status == StageStatus.SKIPPED


# ---------------------------------------------------------------------------
# S2 — Validate
# ---------------------------------------------------------------------------


def test_s2_validate_passes(record_unmanaged, source_ctx, target_ctx, state, run_id):
    target_ctx.mcp_client.get_device_by_serial.return_value = {"isProvisioned": "NO"}
    target_ctx.mcp_client.get_site_by_name.return_value = {"siteId": "site-123"}
    target_ctx.mcp_client.get_alerts.return_value = []
    target_ctx.central_client.get.return_value = {"data": [{"group": "Onboarding"}]}
    result = ValidateStage()._execute(record_unmanaged, run_id, source_ctx, target_ctx, state, False)
    assert result.status == StageStatus.SUCCESS
    assert record_unmanaged.site_id == "site-123"


def test_s2_validate_already_provisioned_fails(
    record_unmanaged, source_ctx, target_ctx, state, run_id
):
    target_ctx.mcp_client.get_device_by_serial.return_value = {"isProvisioned": "YES"}
    target_ctx.mcp_client.get_site_by_name.return_value = {"siteId": "site-123"}
    target_ctx.central_client.get.return_value = {"data": [{"group": "Onboarding"}]}
    result = ValidateStage()._execute(record_unmanaged, run_id, source_ctx, target_ctx, state, False)
    assert result.status == StageStatus.FAILED
    assert "already provisioned" in result.error


def test_s2_validate_missing_group_fails(
    record_unmanaged, source_ctx, target_ctx, state, run_id
):
    target_ctx.mcp_client.get_device_by_serial.return_value = {"isProvisioned": "NO"}
    target_ctx.mcp_client.get_site_by_name.return_value = None
    target_ctx.mcp_client.get_alerts.return_value = []
    target_ctx.central_client.get.return_value = {"data": [{"group": "OtherGroup"}]}
    result = ValidateStage()._execute(record_unmanaged, run_id, source_ctx, target_ctx, state, False)
    assert result.status == StageStatus.FAILED
    assert "VALIDATION_FAILED" in result.error


# ---------------------------------------------------------------------------
# S3 — Offboard
# ---------------------------------------------------------------------------


def test_s3_offboard_skipped_for_unmanaged(
    record_unmanaged, source_ctx, target_ctx, state, run_id
):
    result = OffboardStage()._execute(record_unmanaged, run_id, source_ctx, target_ctx, state, False)
    assert result.status == StageStatus.SKIPPED


def test_s3_offboard_skipped_in_dry_run(
    record_classic_central, source_ctx, target_ctx, state, run_id
):
    result = OffboardStage()._execute(
        record_classic_central, run_id, source_ctx, target_ctx, state, dry_run=True
    )
    assert result.status == StageStatus.SKIPPED


# ---------------------------------------------------------------------------
# S4 — Transfer
# ---------------------------------------------------------------------------


def test_s4_transfer_skipped_for_same_account(
    record_unmanaged, source_ctx, target_ctx, state, run_id
):
    assert record_unmanaged.target_account == TargetAccount.SAME
    result = TransferStage()._execute(record_unmanaged, run_id, source_ctx, target_ctx, state, False)
    assert result.status == StageStatus.SKIPPED


def test_s4_transfer_cross_account_success(
    record_aos8, source_ctx, target_ctx, state, run_id
):
    assert record_aos8.target_account == TargetAccount.NEW
    source_ctx.glp_client.unarchive_device.return_value = {}
    source_ctx.glp_client.unassign_subscription.return_value = {}
    target_ctx.glp_client.add_device.return_value = "task-001"
    target_ctx.glp_client.poll_task.return_value = {"status": "completed"}
    target_ctx.glp_client.get_device.return_value = {"id": "glp-device-abc"}
    result = TransferStage()._execute(record_aos8, run_id, source_ctx, target_ctx, state, False)
    assert result.status == StageStatus.SUCCESS


# ---------------------------------------------------------------------------
# S6 — Configure (VLAN push)
# ---------------------------------------------------------------------------


SWITCH_GROUP_SCOPE_ID = "88888"


def _setup_s6_mocks(target_ctx, switch_group_name="Switches"):
    """Set up minimum mocks for S6 to reach the VLAN step.

    S6 resolves the switch device-group scope-id at runtime (no hardcoded
    tenant IDs), so the device-groups collection has to answer with a named
    group carrying a scopeId.
    """
    target_ctx.global_scope_id = "99999"

    def _get(path, params=None):
        if "device-groups" in path:
            if params and params.get("offset", 0):
                return {"items": []}
            return {
                "items": [
                    {"scopeName": switch_group_name, "scopeId": SWITCH_GROUP_SCOPE_ID},
                    {"scopeName": "Other", "scopeId": "77777"},
                ]
            }
        return {"items": [{"scopeId": "99999", "id": "site-abc"}]}

    target_ctx.central_client.get.side_effect = _get
    target_ctx.central_client.post.return_value = {}
    target_ctx.mcp_client.get_device_scope_id.return_value = "12345"  # numeric string — required by scope-map
    target_ctx.mcp_client.get_site_by_name.return_value = {"siteId": "site-abc"}


def test_s6_vlan_push_access_switch(record_unmanaged, source_ctx, target_ctx, state, run_id, tmp_path):
    """VLANs from config file are posted when persona is ACCESS_SWITCH."""
    vlan_cfg = tmp_path / "switch.cfg"
    vlan_cfg.write_text("vlan 10\nvlan 20\n")
    record_unmanaged.vlan_config_file = str(vlan_cfg)
    record_unmanaged.scope_id = "12345"
    _setup_s6_mocks(target_ctx)

    result = ConfigureStage()._execute(record_unmanaged, run_id, source_ctx, target_ctx, state, False)

    assert result.status == StageStatus.SUCCESS
    assert result.data["vlans_pushed"] == 2
    # layer2-vlan POST should have been called once
    post_calls = [str(c) for c in target_ctx.central_client.post.call_args_list]
    assert any("layer2-vlan" in c for c in post_calls)


def test_s6_vlan_push_skipped_for_non_access(record_classic_central, source_ctx, target_ctx, state, run_id, tmp_path):
    """VLANs are not pushed when persona is not ACCESS_SWITCH."""
    vlan_cfg = tmp_path / "switch.cfg"
    vlan_cfg.write_text("vlan 10\nvlan 20\n")
    record_classic_central.vlan_config_file = str(vlan_cfg)
    record_classic_central.scope_id = "12345"
    _setup_s6_mocks(target_ctx)

    result = ConfigureStage()._execute(record_classic_central, run_id, source_ctx, target_ctx, state, False)

    assert result.status == StageStatus.SUCCESS
    assert result.data["vlans_pushed"] == 0
    post_calls = [str(c) for c in target_ctx.central_client.post.call_args_list]
    assert not any("layer2-vlan" in c for c in post_calls)


def test_s6_vlan_push_skipped_when_no_file(record_unmanaged, source_ctx, target_ctx, state, run_id):
    """No VLAN push if vlan_config_file is None."""
    record_unmanaged.scope_id = "12345"
    _setup_s6_mocks(target_ctx)

    result = ConfigureStage()._execute(record_unmanaged, run_id, source_ctx, target_ctx, state, False)

    assert result.status == StageStatus.SUCCESS
    assert result.data["vlans_pushed"] == 0


def test_s6_vlan_interface_push(record_unmanaged, source_ctx, target_ctx, state, run_id, tmp_path):
    """VLAN interfaces are posted with correct 4-step sequence."""
    cfg = tmp_path / "intfs.cfg"
    cfg.write_text(
        "interface vlan 5\n    ip address 10.11.154.2/24\n    ip helper-address 10.11.154.19\n"
        "interface vlan 50\n    ip dhcp\n"
    )
    record_unmanaged.vlan_interface_config_file = str(cfg)
    record_unmanaged.scope_id = "12345"
    _setup_s6_mocks(target_ctx)

    result = ConfigureStage()._execute(record_unmanaged, run_id, source_ctx, target_ctx, state, False)

    assert result.status == StageStatus.SUCCESS
    assert result.data["vlan_interfaces_pushed"] == 2

    post_calls = [str(c) for c in target_ctx.central_client.post.call_args_list]
    # L3 VLAN interface endpoint was called for each interface
    assert any("vlan-interfaces/5" in c for c in post_calls)
    assert any("vlan-interfaces/50" in c for c in post_calls)
    # Layer2-vlan was created for each interface
    assert any("layer2-vlan/5" in c for c in post_calls)
    assert any("layer2-vlan/50" in c for c in post_calls)
    # IPv4 address pushed (LOCAL scope) for the static-IP vlan, not for the dhcp vlan
    assert any("vlan-interfaces/5" in c and "ipv4" in c and "LOCAL" in c for c in post_calls)
    assert not any("vlan-interfaces/50" in c and "ipv4" in c for c in post_calls)


def test_s6_vlan_interface_skipped_when_no_file(record_unmanaged, source_ctx, target_ctx, state, run_id):
    """No VLAN interface push if vlan_interface_config_file is None."""
    record_unmanaged.scope_id = "12345"
    _setup_s6_mocks(target_ctx)

    result = ConfigureStage()._execute(record_unmanaged, run_id, source_ctx, target_ctx, state, False)

    assert result.status == StageStatus.SUCCESS
    assert result.data["vlan_interfaces_pushed"] == 0


def test_s6_device_profiles_created(record_unmanaged, source_ctx, target_ctx, state, run_id):
    """Device profiles are POSTed once on the first device processed."""
    record_unmanaged.scope_id = "12345"
    _setup_s6_mocks(target_ctx)
    target_ctx.device_profiles_created = False

    result = ConfigureStage()._execute(record_unmanaged, run_id, source_ctx, target_ctx, state, False)

    assert result.status == StageStatus.SUCCESS
    post_calls = [str(c) for c in target_ctx.central_client.post.call_args_list]
    # Every device-profile reference: 4 profile creates + 8 scope-map posts (4 profiles x 2 scopes).
    profile_calls = [c for c in post_calls if "device-profile" in c]
    creates = [c for c in profile_calls if "device-profile/" in c and "scope-maps" not in c]
    scope_maps = [c for c in profile_calls if "scope-maps" in c]
    assert len(creates) == 4
    assert len(scope_maps) == 8
    assert len(profile_calls) == 12
    assert any("arubaAP" in c for c in creates)
    assert any("arubaGW" in c for c in creates)
    assert any("arubaSW" in c for c in creates)
    assert any("arubaAOS" in c for c in creates)
    assert target_ctx.device_profiles_created is True


def test_s6_device_profiles_skipped_second_device(record_unmanaged, source_ctx, target_ctx, state, run_id):
    """Device profiles are NOT re-created when device_profiles_created is already True."""
    record_unmanaged.scope_id = "12345"
    _setup_s6_mocks(target_ctx)
    target_ctx.device_profiles_created = True  # already done in prior device

    result = ConfigureStage()._execute(record_unmanaged, run_id, source_ctx, target_ctx, state, False)

    assert result.status == StageStatus.SUCCESS
    post_calls = [str(c) for c in target_ctx.central_client.post.call_args_list]
    assert not any("device-profile" in c for c in post_calls)


def test_s6_device_profile_duplicate_is_silent(record_unmanaged, source_ctx, target_ctx, state, run_id):
    """A 'duplicate' error on profile POST is silently skipped and stage still succeeds."""
    record_unmanaged.scope_id = "12345"
    _setup_s6_mocks(target_ctx)
    target_ctx.device_profiles_created = False

    exc = Exception("duplicate profile")
    resp = MagicMock()
    resp.text = "Cannot create duplicate config"
    exc.response = resp
    target_ctx.central_client.post.side_effect = exc

    result = ConfigureStage()._execute(record_unmanaged, run_id, source_ctx, target_ctx, state, False)

    assert result.status == StageStatus.SUCCESS
    assert target_ctx.device_profiles_created is True


# ---------------------------------------------------------------------------
# S7 — Firmware
# ---------------------------------------------------------------------------


def test_s7_firmware_skipped_for_aos_s(record_aos_s, source_ctx, target_ctx, state, run_id):
    assert record_aos_s.firmware_action == FirmwareAction.SKIP
    result = FirmwareStage()._execute(record_aos_s, run_id, source_ctx, target_ctx, state, False)
    assert result.status == StageStatus.SKIPPED


def test_s7_firmware_skipped_in_dry_run(
    record_unmanaged, source_ctx, target_ctx, state, run_id
):
    result = FirmwareStage()._execute(
        record_unmanaged, run_id, source_ctx, target_ctx, state, dry_run=True
    )
    assert result.status == StageStatus.SKIPPED


# ---------------------------------------------------------------------------
# S8 — Verify
# ---------------------------------------------------------------------------


def test_s8_verify_passes(record_unmanaged, source_ctx, target_ctx, state, run_id):
    target_ctx.mcp_client.get_device_by_serial.return_value = {
        "isProvisioned": "YES",
        "status": "ONLINE",
        "firmwareVersion": "10.13.1010",
    }
    target_ctx.central_client.get.return_value = {
        "softwareVersion": "10.13.1010",
        "upgradeStatus": "Up To Date",
    }
    result = VerifyStage()._execute(record_unmanaged, run_id, source_ctx, target_ctx, state, False)
    assert result.status == StageStatus.SUCCESS
    assert result.data["is_provisioned"] is True


def test_s8_verify_offline_is_warning_only(record_unmanaged, source_ctx, target_ctx, state, run_id):
    # OFFLINE status is a warning, not a hard failure — device still passes if provisioned + firmware ok
    target_ctx.mcp_client.get_device_by_serial.return_value = {
        "isProvisioned": "YES",
        "status": "OFFLINE",
        "firmwareVersion": "10.13.1010",
    }
    target_ctx.central_client.get.return_value = {"devices": [{"configStatus": "SYNCHRONIZED"}]}
    result = VerifyStage()._execute(record_unmanaged, run_id, source_ctx, target_ctx, state, False)
    assert result.status == StageStatus.SUCCESS


def test_s8_verify_fails_config_out_of_sync(record_unmanaged, source_ctx, target_ctx, state, run_id):
    target_ctx.mcp_client.get_device_by_serial.return_value = {
        "isProvisioned": "YES",
        "status": "ONLINE",
        "firmwareVersion": "10.13.1010",
    }
    target_ctx.central_client.get.return_value = {"devices": [{"configStatus": "OUT_OF_SYNC"}]}
    result = VerifyStage()._execute(record_unmanaged, run_id, source_ctx, target_ctx, state, False)
    assert result.status == StageStatus.FAILED
    assert "OUT_OF_SYNC" in result.error


def test_s8_verify_skipped_in_dry_run(record_unmanaged, source_ctx, target_ctx, state, run_id):
    result = VerifyStage()._execute(
        record_unmanaged, run_id, source_ctx, target_ctx, state, dry_run=True
    )
    assert result.status == StageStatus.SKIPPED
