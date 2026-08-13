"""Gate/behavior tests for `scripts/evaluate_aos8_070_disposable_lifecycle.py`.

No live network calls anywhere in this file -- every read/write invoker is a
fake. These tests exist to prove the harness's *gating* (status never calls
out; read/write require the correct `hpe_networking_mcp.pipeline.live_test_config` opt-ins;
write additionally requires confirm + lab-prefix; unsupported kinds are
always refused) is correct, not to exercise a real AOS8/Central account.
"""

from __future__ import annotations

import pytest

from hpe_networking_mcp.pipeline.live_test_config import (
    live_test_read_env_var,
    live_test_write_env_var,
)
from scripts import evaluate_aos8_070_disposable_lifecycle as harness


def _clear_gates(monkeypatch):
    monkeypatch.delenv(live_test_read_env_var("central"), raising=False)
    monkeypatch.delenv(live_test_write_env_var("central"), raising=False)


def _enable_read(monkeypatch):
    monkeypatch.setenv(live_test_read_env_var("central"), "1")


def _enable_read_and_write(monkeypatch):
    monkeypatch.setenv(live_test_read_env_var("central"), "1")
    monkeypatch.setenv(live_test_write_env_var("central"), "1")


class _FakeInvokers:
    def __init__(self):
        self.reads: list = []
        self.writes: list = []

    def read(self, operation):
        self.reads.append(operation)
        return {"name": operation.name, "found": False}

    def write(self, operation, *, confirmation):
        self.writes.append((operation, confirmation))
        dry_run = bool(operation.arguments.get("dry_run"))
        return {"ok": True, "name": operation.name, "dry_run": dry_run}


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_report_never_touches_network_and_lists_every_kind(monkeypatch):
    _clear_gates(monkeypatch)
    report = harness.status_report()
    assert report["live_test_status"]["read_enabled"] is False
    assert report["live_test_status"]["write_enabled"] is False
    assert set(report["kinds"]) == set(harness.LIFECYCLE_KINDS)
    for name in ("route", "vrrp", "ap_group"):
        assert report["kinds"][name]["supports_write"] is False
        assert report["kinds"][name]["unsupported_reason"]
    for name in (
        "auth_server",
        "server_group",
        "aaa_profile",
        "dot1x_auth_profile",
        "mac_auth_profile",
        "assignment",
    ):
        assert report["kinds"][name]["supports_write"] is True


# ---------------------------------------------------------------------------
# read_evidence
# ---------------------------------------------------------------------------


def test_read_evidence_refuses_when_read_gate_disabled(monkeypatch):
    _clear_gates(monkeypatch)
    fakes = _FakeInvokers()
    with pytest.raises(harness.LifecycleHarnessError, match="Read-only"):
        harness.read_evidence(
            "auth_server",
            read_invoker=fakes.read,
            scope_id="100",
            scope_name="Branch",
            persona="MOBILITY_GW",
        )
    assert fakes.reads == []


def test_read_evidence_succeeds_once_read_gate_enabled(monkeypatch):
    _enable_read(monkeypatch)
    fakes = _FakeInvokers()
    result = harness.read_evidence(
        "auth_server",
        read_invoker=fakes.read,
        scope_id="100",
        scope_name="Branch",
        persona="MOBILITY_GW",
    )
    assert result["kind"] == "auth_server"
    assert result["read_operation"]["tool_or_endpoint"] == "get_auth_server"
    assert len(fakes.reads) == 1


def test_read_evidence_refuses_unsupported_kinds(monkeypatch):
    _enable_read(monkeypatch)
    fakes = _FakeInvokers()
    for kind_name in ("route", "vrrp", "ap_group"):
        kind = harness.LIFECYCLE_KINDS[kind_name]
        with pytest.raises(harness.LifecycleHarnessError, match=kind_name):
            harness.read_evidence(
                kind_name,
                read_invoker=fakes.read,
                scope_id="100",
                scope_name="Branch",
                persona="MOBILITY_GW",
            )
        assert kind.unsupported_reason is not None
    assert fakes.reads == []


def test_read_evidence_rejects_unknown_kind(monkeypatch):
    _enable_read(monkeypatch)
    fakes = _FakeInvokers()
    with pytest.raises(harness.LifecycleHarnessError, match="Unknown"):
        harness.read_evidence(
            "not-a-real-kind",
            read_invoker=fakes.read,
            scope_id="100",
            scope_name="Branch",
            persona="MOBILITY_GW",
        )


# ---------------------------------------------------------------------------
# run_disposable_write_lifecycle
# ---------------------------------------------------------------------------


def _run_kwargs(fakes: _FakeInvokers, **overrides):
    base = dict(
        confirm=True,
        lab_prefix=harness.DEFAULT_LAB_PREFIX,
        lab_name=f"{harness.DEFAULT_LAB_PREFIX}rt1",
        read_invoker=fakes.read,
        write_invoker=fakes.write,
        scope_id="100",
        scope_name="Branch",
        persona="MOBILITY_GW",
    )
    base.update(overrides)
    return base


def test_disposable_write_refuses_without_confirm(monkeypatch):
    _enable_read_and_write(monkeypatch)
    fakes = _FakeInvokers()
    with pytest.raises(harness.LifecycleHarnessError, match="confirm=True"):
        harness.run_disposable_write_lifecycle(
            "auth_server", **_run_kwargs(fakes, confirm=False)
        )
    assert fakes.writes == []


def test_disposable_write_refuses_when_write_gate_disabled(monkeypatch):
    _enable_read(monkeypatch)  # read enabled, write not
    fakes = _FakeInvokers()
    with pytest.raises(harness.LifecycleHarnessError, match="disabled"):
        harness.run_disposable_write_lifecycle(
            "auth_server", **_run_kwargs(fakes)
        )
    assert fakes.writes == []


def test_disposable_write_refuses_short_lab_prefix(monkeypatch):
    _enable_read_and_write(monkeypatch)
    fakes = _FakeInvokers()
    with pytest.raises(harness.LifecycleHarnessError, match="six characters"):
        harness.run_disposable_write_lifecycle(
            "auth_server", **_run_kwargs(fakes, lab_prefix="ab", lab_name="ab-x")
        )
    assert fakes.writes == []


def test_disposable_write_refuses_lab_name_not_matching_prefix(monkeypatch):
    _enable_read_and_write(monkeypatch)
    fakes = _FakeInvokers()
    with pytest.raises(harness.LifecycleHarnessError, match="must start with"):
        harness.run_disposable_write_lifecycle(
            "auth_server", **_run_kwargs(fakes, lab_name="not-lab-owned")
        )
    assert fakes.writes == []


def test_disposable_write_refuses_unsupported_kinds(monkeypatch):
    _enable_read_and_write(monkeypatch)
    fakes = _FakeInvokers()
    for kind_name in ("route", "vrrp", "ap_group"):
        with pytest.raises(harness.LifecycleHarnessError, match=kind_name):
            harness.run_disposable_write_lifecycle(
                kind_name, **_run_kwargs(fakes)
            )
    assert fakes.writes == []


def test_disposable_write_full_round_trip_creates_reads_back_and_cleans_up(
    monkeypatch,
):
    _enable_read_and_write(monkeypatch)
    fakes = _FakeInvokers()
    evidence = harness.run_disposable_write_lifecycle(
        "auth_server", **_run_kwargs(fakes)
    )
    assert evidence["kind"] == "auth_server"
    assert evidence["create_status"] == "applied"
    assert evidence["cleanup_ok"] is True
    assert any(
        operation.name == "delete_auth_server" for operation, _confirmation in fakes.writes
    )


def test_disposable_write_still_attempts_cleanup_even_when_no_delete_verified(
    monkeypatch,
):
    """Proves the harness's own `cleanup_ok=False` reporting path (rather
    than crashing) for a hypothetical kind whose adapter action carries no
    delete metadata, by directly exercising `_lab_candidate`/adapter wiring
    at a lower level with a fake, no-delete `CandidateAction`."""
    from hpe_networking_mcp.pipeline.aos8_target_adapters import CandidateAction

    fakes = _FakeInvokers()

    class _NoDeleteAdapter:
        def __init__(self, *_, **__):
            pass

        def candidate_action(self, candidate):
            return CandidateAction(key="auth_server:radius:x", candidate=candidate)

    monkeypatch.setattr(harness, "_build_adapter", lambda *a, **k: _NoDeleteAdapter())
    _enable_read_and_write(monkeypatch)
    evidence = harness.run_disposable_write_lifecycle(
        "auth_server", **_run_kwargs(fakes)
    )
    assert evidence["create_status"] == "applied"
    assert evidence["cleanup_ok"] is False
    assert "no verified delete_operations" in evidence["cleanup"][0]["error"]
