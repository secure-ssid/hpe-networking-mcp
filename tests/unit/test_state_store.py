"""Tests for src/hpe_networking_mcp/pipeline/state_store.py."""

from hpe_networking_mcp.pipeline.models import StageStatus
from hpe_networking_mcp.pipeline.state_store import StateStore


def test_initial_status_is_pending(state, run_id):
    assert state.get_stage_status("CN001", run_id, "s1_discover") == StageStatus.PENDING


def test_set_and_get_status(state, run_id):
    state.set_stage_status("CN001", run_id, "s1_discover", StageStatus.SUCCESS)
    assert state.get_stage_status("CN001", run_id, "s1_discover") == StageStatus.SUCCESS


def test_is_stage_done(state, run_id):
    assert not state.is_stage_done("CN001", run_id, "s1_discover")
    state.set_stage_status("CN001", run_id, "s1_discover", StageStatus.SUCCESS)
    assert state.is_stage_done("CN001", run_id, "s1_discover")


def test_stage_data_roundtrip(state, run_id):
    state.set_stage_status(
        "CN001", run_id, "s1_discover", StageStatus.SUCCESS,
        data={"model": "CX-6300", "firmware": "10.10.0"}
    )
    data = state.get_stage_data("CN001", run_id, "s1_discover")
    assert data["model"] == "CX-6300"


def test_get_failed_serials(state, run_id):
    state.set_stage_status("CN001", run_id, "s2_validate", StageStatus.FAILED, error="bad group")
    state.set_stage_status("CN002", run_id, "s1_discover", StageStatus.SUCCESS)
    failed = state.get_failed_serials(run_id)
    assert "CN001" in failed
    assert "CN002" not in failed


def test_get_all_stage_statuses(state, run_id):
    state.set_stage_status("CN001", run_id, "s1_discover", StageStatus.SUCCESS)
    state.set_stage_status("CN001", run_id, "s2_validate", StageStatus.FAILED)
    statuses = state.get_all_stage_statuses("CN001", run_id)
    assert statuses["s1_discover"] == "success"
    assert statuses["s2_validate"] == "failed"


def test_resume_skip(state, run_id):
    """is_stage_done should return True only for SUCCESS."""
    state.set_stage_status("CN001", run_id, "s3_offboard", StageStatus.SKIPPED)
    assert not state.is_stage_done("CN001", run_id, "s3_offboard")
    state.set_stage_status("CN001", run_id, "s3_offboard", StageStatus.SUCCESS)
    assert state.is_stage_done("CN001", run_id, "s3_offboard")
