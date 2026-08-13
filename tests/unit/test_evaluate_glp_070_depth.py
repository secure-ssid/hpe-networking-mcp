"""Unit tests for scripts/evaluate_glp_070_depth.py (v07-glp-depth).

Covers: default-disabled gating (no network calls without explicit opt-in),
partial-failure step reporting, disposable-write cleanup guarantee, CLI
exit codes, and artifact redaction/leakage via hpe_networking_mcp.pipeline.artifact_contracts.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

evaluate_glp_070_depth = importlib.import_module("evaluate_glp_070_depth")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "HPE_MCP_LIVE_TEST_GLP_READ",
        "HPE_MCP_LIVE_TEST_GLP_WRITE",
        "HPE_MCP_GLP_V2BETA1_WRITES",
        "TARGET_BASE_URL",
        "TARGET_CLIENT_ID",
        "TARGET_CLIENT_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)


def test_status_mode_never_requires_network(monkeypatch, capsys):
    rc = evaluate_glp_070_depth.main(["--status"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["platform"] == "glp"
    assert out["read_enabled"] is False
    assert out["write_enabled"] is False
    assert out["credentials_configured"] is False


def test_read_mode_blocked_by_default(tmp_path, capsys):
    output = tmp_path / "evidence.json"
    rc = evaluate_glp_070_depth.main(["--output", str(output)])
    assert rc == 3
    assert not output.exists()
    assert "HPE_MCP_LIVE_TEST_GLP_READ" in capsys.readouterr().err


def test_write_probe_requires_vm_id(monkeypatch, capsys):
    monkeypatch.setenv("HPE_MCP_LIVE_TEST_GLP_READ", "1")
    monkeypatch.setenv("HPE_MCP_LIVE_TEST_GLP_WRITE", "1")
    rc = evaluate_glp_070_depth.main(["--write-probe"])
    assert rc == 2
    assert "--vm-id" in capsys.readouterr().err


def test_write_probe_blocked_without_write_flag(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HPE_MCP_LIVE_TEST_GLP_READ", "1")
    output = tmp_path / "write-evidence.json"
    rc = evaluate_glp_070_depth.main(
        ["--write-probe", "--vm-id", "vm-lab-1", "--output", str(output)]
    )
    assert rc == 3
    assert not output.exists()


def test_readonly_evaluation_partial_failure_reporting(monkeypatch):
    from hpe_networking_mcp.mcp_servers import glp

    monkeypatch.setattr(
        glp, "glp_write_status", lambda: {"enabled": False}
    )
    monkeypatch.setattr(
        glp, "list_glp_devices", lambda limit: {"items": [{"id": 1}], "errors": []}
    )

    def _boom(limit):
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr(glp, "list_glp_subscriptions", _boom)
    monkeypatch.setattr(glp, "list_glp_users", lambda limit: {"items": [], "errors": []})
    monkeypatch.setattr(
        glp, "list_glp_role_assignments", lambda limit: {"data": {"items": []}, "errors": []}
    )
    monkeypatch.setattr(
        glp, "list_glp_scope_groups", lambda limit: {"data": {"items": []}, "errors": []}
    )

    async def _fake_async_family(limit):
        return {"data": None, "errors": ["region not set"]}

    monkeypatch.setattr(glp, "list_glp_compute_servers", _fake_async_family)
    monkeypatch.setattr(glp, "list_glp_storage_systems", _fake_async_family)
    monkeypatch.setattr(glp, "list_glp_block_storage_volumes", _fake_async_family)
    monkeypatch.setattr(glp, "list_glp_virtual_machines", _fake_async_family)
    monkeypatch.setattr(glp, "list_glp_backup_protection_jobs", _fake_async_family)
    monkeypatch.setattr(glp, "list_glp_data_services_issues", _fake_async_family)
    monkeypatch.setattr(
        glp,
        "plan_glp_reconciliation",
        lambda sample_size: {"counts": {}, "findings": [], "errors": []},
    )

    result = asyncio.run(evaluate_glp_070_depth.run_readonly_evaluation())

    names = [s["name"] for s in result["steps"]]
    assert names[0] == "glp_write_status"
    assert len(result["steps"]) == 13
    by_name = {s["name"]: s for s in result["steps"]}
    assert by_name["list_glp_devices"]["status"] == "ok"
    assert by_name["list_glp_subscriptions"]["status"] == "error"
    assert "upstream unavailable" in by_name["list_glp_subscriptions"]["summary"]["errors"][0]
    # one bad step never aborts the run -- every step still ran
    assert by_name["plan_glp_reconciliation"]["status"] == "ok"
    assert result["secrets_included"] is False
    assert result["raw_response_included"] is False


def test_write_probe_always_runs_cleanup_even_on_power_off_failure(monkeypatch):
    from hpe_networking_mcp.mcp_servers import glp

    calls: list[str] = []

    async def _get_vm(vm_id):
        calls.append(f"get:{vm_id}")
        return {"data": {"id": vm_id, "power_state": "on"}, "errors": []}

    async def _set_power(vm_id, action, dry_run=True, confirm=False):
        calls.append(f"power:{action}")
        if action == "power-off":
            raise RuntimeError("simulated power-off failure")
        return {"status_code": 200, "data": {"ok": True}}

    monkeypatch.setattr(glp, "get_glp_virtual_machine", _get_vm)
    monkeypatch.setattr(glp, "set_glp_virtual_machine_power", _set_power)

    result = asyncio.run(evaluate_glp_070_depth.run_write_probe("vm-lab-1"))

    # power_on_cleanup must have been attempted despite the power_off failure.
    assert "power:power-on" in calls
    by_name = {s["name"]: s for s in result["steps"]}
    assert by_name["power_off"]["status"] == "error"
    assert by_name["power_on_cleanup"]["status"] == "ok"
    assert result["target_identifier_hash"].startswith("sha256:")
    assert "vm-lab-1" not in json.dumps(result)


@pytest.mark.parametrize(
    "failed_result",
    [
        {"status_code": 404, "data": {"message": "VM not found"}},
        {"status": "blocked", "error": "GLP writes disabled"},
    ],
)
def test_write_probe_reports_returned_write_failures(monkeypatch, failed_result):
    from hpe_networking_mcp.mcp_servers import glp

    async def _get_vm(vm_id):
        return {"data": {"id": vm_id, "power_state": "on"}, "errors": []}

    async def _set_power(vm_id, action, dry_run=True, confirm=False):
        if action == "power-off":
            return failed_result
        return {"status_code": 200, "data": {"ok": True}}

    monkeypatch.setattr(glp, "get_glp_virtual_machine", _get_vm)
    monkeypatch.setattr(glp, "set_glp_virtual_machine_power", _set_power)

    result = asyncio.run(evaluate_glp_070_depth.run_write_probe("vm-lab-1"))

    by_name = {step["name"]: step for step in result["steps"]}
    assert by_name["power_off"]["status"] == "error"
    assert by_name["power_off"]["summary"]["errors"]
    assert by_name["power_on_cleanup"]["status"] == "ok"


def test_artifact_written_is_redacted_and_bounded(monkeypatch, tmp_path):
    from hpe_networking_mcp.mcp_servers import glp
    from hpe_networking_mcp.pipeline import artifact_contracts as contracts

    async def _fake_family(limit):
        return {"data": {"items": []}, "errors": []}

    monkeypatch.setattr(glp, "glp_write_status", lambda: {"enabled": False})
    monkeypatch.setattr(glp, "list_glp_devices", lambda limit: {"items": [], "errors": []})
    monkeypatch.setattr(glp, "list_glp_subscriptions", lambda limit: {"items": [], "errors": []})
    monkeypatch.setattr(glp, "list_glp_users", lambda limit: {"items": [], "errors": []})
    monkeypatch.setattr(
        glp, "list_glp_role_assignments", lambda limit: {"data": {"items": []}, "errors": []}
    )
    monkeypatch.setattr(
        glp, "list_glp_scope_groups", lambda limit: {"data": {"items": []}, "errors": []}
    )
    monkeypatch.setattr(glp, "list_glp_compute_servers", _fake_family)
    monkeypatch.setattr(glp, "list_glp_storage_systems", _fake_family)
    monkeypatch.setattr(glp, "list_glp_block_storage_volumes", _fake_family)
    monkeypatch.setattr(glp, "list_glp_virtual_machines", _fake_family)
    monkeypatch.setattr(glp, "list_glp_backup_protection_jobs", _fake_family)
    monkeypatch.setattr(glp, "list_glp_data_services_issues", _fake_family)
    monkeypatch.setattr(
        glp,
        "plan_glp_reconciliation",
        lambda sample_size: {"counts": {}, "findings": [], "errors": []},
    )

    monkeypatch.setenv("HPE_MCP_LIVE_TEST_GLP_READ", "1")
    output = tmp_path / "evidence.json"
    rc = evaluate_glp_070_depth.main(["--output", str(output)])
    assert rc == 0
    assert output.exists()
    body = json.loads(output.read_text())
    assert body["kind"] == contracts.LIVE_LIFECYCLE_EVIDENCE
    assert body["secrets_included"] is False
    assert body["raw_response_included"] is False
    assert len(body["steps"]) == 13
