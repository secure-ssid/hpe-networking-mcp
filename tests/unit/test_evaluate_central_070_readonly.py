"""Unit tests for scripts/evaluate_central_070_readonly.py — the credential-
gated Central v0.7 live evaluator and disposable-write lifecycle harness.

Covers: default offline fixture mode writes a valid, redacted
live_lifecycle_evidence artifact; --live-read stays blocked without the
explicit env opt-in and never calls a real client when blocked; the
disposable-write harness is gated and never auto-executed; artifact content
never contains a raw scope/serial identifier or secret-shaped value.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

evaluate = importlib.import_module("evaluate_central_070_readonly")


@pytest.fixture(autouse=True)
def _clear_live_test_env(monkeypatch):
    for platform in ("CENTRAL",):
        monkeypatch.delenv(f"HPE_MCP_LIVE_TEST_{platform}_READ", raising=False)
        monkeypatch.delenv(f"HPE_MCP_LIVE_TEST_{platform}_WRITE", raising=False)


def test_offline_mode_writes_valid_redacted_artifact(tmp_path):
    output = tmp_path / "evidence.json"
    exit_code = evaluate.main(["--output", str(output)])

    assert exit_code == 0
    assert output.exists()
    data = json.loads(output.read_text())
    assert data["kind"] == "live_lifecycle_evidence"
    assert data["mode"] == "read_only"
    assert data["secrets_included"] is False
    assert data["raw_response_included"] is False
    assert len(data["steps"]) == 3
    assert all(s["status"] == "ok" for s in data["steps"])


def test_offline_artifact_never_contains_fixture_scope_id(tmp_path):
    """The FakeCentralClient's fixture scope id/serial never leak into the
    written artifact -- only bounded item counts do."""
    output = tmp_path / "evidence.json"
    evaluate.main(["--output", str(output)])
    raw = output.read_text()
    assert "fixture-global-scope" not in raw
    assert "FIXTURE001" not in raw


def test_live_read_blocked_without_env_opt_in(tmp_path, capsys):
    output = tmp_path / "evidence.json"
    exit_code = evaluate.main(["--live-read", "--output", str(output)])

    assert exit_code == 1
    assert not output.exists()
    captured = json.loads(capsys.readouterr().out)
    assert captured["status"] == "blocked"
    assert captured["read_enabled"] is False


def test_live_read_runs_bounded_steps_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("HPE_MCP_LIVE_TEST_CENTRAL_READ", "1")

    calls: list[str] = []

    def fake_live_read_steps():
        calls.append("ran")
        return [{"name": "get_global_scope_id", "status": "ok", "item_count": 1}]

    monkeypatch.setattr(evaluate, "_live_read_steps", fake_live_read_steps)

    output = tmp_path / "evidence.json"
    exit_code = evaluate.main(["--live-read", "--output", str(output)])

    assert exit_code == 0
    assert calls == ["ran"]
    data = json.loads(output.read_text())
    assert data["steps"] == [{"name": "get_global_scope_id", "status": "ok", "item_count": 1}]


def test_live_write_flag_never_executes_disposable_lifecycle(monkeypatch, tmp_path):
    """Even with --live-write passed, main() must never call
    run_disposable_write_lifecycle in this repo revision -- it only reports
    gate status."""
    called = False

    def fake_disposable(*args, **kwargs):
        nonlocal called
        called = True
        return {"mode": "disposable_write", "steps": []}

    monkeypatch.setattr(evaluate, "run_disposable_write_lifecycle", fake_disposable)

    output = tmp_path / "evidence.json"
    exit_code = evaluate.main(["--live-write", "--output", str(output)])

    assert exit_code == 0
    assert called is False
    data = json.loads(output.read_text())
    assert data is not None  # evidence file still produced (offline mode)


def test_disposable_write_lifecycle_is_gated_and_hashes_identifier(monkeypatch):
    """run_disposable_write_lifecycle itself never leaks the raw generated
    template name -- only its hash."""
    import hpe_networking_mcp.mcp_servers.config as config_tools

    created = {}

    def fake_build(name, members, *, scope_id, device_function, dry_run, confirm):
        created["name"] = name
        return {"action": "created", "name": name}

    def fake_get_network_profile(profile_type, **kwargs):
        return {"name": created["name"]}

    def fake_delete(name, *, scope_id, device_function, dry_run, confirm):
        return {"name": name, "read_back": {"deleted_confirmed": True}}

    monkeypatch.setattr(config_tools, "build_vsf_template", fake_build)
    monkeypatch.setattr(config_tools, "get_network_profile", fake_get_network_profile)
    monkeypatch.setattr(config_tools, "delete_vsf_template", fake_delete)

    result = evaluate.run_disposable_write_lifecycle("lab-scope-1", "ACCESS_SWITCH")

    assert result["mode"] == "disposable_write"
    assert result["target_identifier_hash"].startswith("sha256:")
    assert created["name"] not in json.dumps(result)
    step_names = [s["name"] for s in result["steps"]]
    assert step_names == ["create", "read_back_after_create", "delete"]
    assert all(s["status"] == "ok" for s in result["steps"])


def test_disposable_write_lifecycle_reports_partial_failure(monkeypatch):
    """If create fails, read_back_after_create is skipped but delete is
    still attempted (best-effort cleanup)."""
    import hpe_networking_mcp.mcp_servers.config as config_tools

    def failing_build(*args, **kwargs):
        raise RuntimeError("write blocked")

    def fake_delete(name, *, scope_id, device_function, dry_run, confirm):
        return {"name": name}

    monkeypatch.setattr(config_tools, "build_vsf_template", failing_build)
    monkeypatch.setattr(config_tools, "delete_vsf_template", fake_delete)

    result = evaluate.run_disposable_write_lifecycle("lab-scope-1", "ACCESS_SWITCH")

    step_names = [s["name"] for s in result["steps"]]
    assert "read_back_after_create" not in step_names
    assert step_names == ["create", "delete"]
    create_step = next(s for s in result["steps"] if s["name"] == "create")
    assert create_step["status"] == "error"
