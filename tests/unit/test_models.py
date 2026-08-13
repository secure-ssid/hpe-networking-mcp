"""Tests for src/hpe_networking_mcp/pipeline/models.py."""

from hpe_networking_mcp.pipeline.models import Persona, StageResult, StageStatus


def test_persona_api_values():
    assert Persona.ACCESS_SWITCH.to_api_value() == "ACCESS_SWITCH"
    assert Persona.CORE_SWITCH.to_api_value() == "CORE_SWITCH"
    assert Persona.AGGREGATION_SWITCH.to_api_value() == "AGG_SWITCH"


def test_stage_result_success():
    r = StageResult.success(site_id="abc")
    assert r.status == StageStatus.SUCCESS
    assert r.data["site_id"] == "abc"
    assert r.error is None


def test_stage_result_failed():
    r = StageResult.failed("something broke", code=42)
    assert r.status == StageStatus.FAILED
    assert "something broke" in r.error
    assert r.data["code"] == 42


def test_stage_result_skipped():
    r = StageResult.skipped("no-op")
    assert r.status == StageStatus.SKIPPED
    assert r.data["reason"] == "no-op"
