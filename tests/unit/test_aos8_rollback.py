"""Pure-python tests for `hpe_networking_mcp.pipeline.aos8_rollback` (no MCP/network dependency)."""

from __future__ import annotations

import pytest

from hpe_networking_mcp.pipeline.aos8_rollback import (
    ROLLBACK_WRITE_GATE_ENV_VAR,
    RollbackConflictPolicy,
    execute_rollback_plan,
    plan_rollback,
    reverse_dependency_order,
    rollback_writes_enabled,
)
from hpe_networking_mcp.pipeline.aos8_target_adapters import AdapterError, CandidateAction, Operation, WriteGateError


def _delete_op(name: str) -> Operation:
    return Operation(invocation="tool", name=name, arguments={"dry_run": True})


def _action_for_factory(mapping):
    """Build an `action_for` callable from {candidate_key: CandidateAction}."""

    def action_for(candidate):
        key = f"{candidate['object_type']}:{candidate['identifier']}"
        if key not in mapping:
            raise AdapterError(f"{key}: no mapping configured for this test")
        return mapping[key]

    return action_for


def _candidate(object_type, identifier, dependencies=None):
    return {
        "object_type": object_type,
        "identifier": identifier,
        "dependencies": dependencies or [],
        "apply_order": 10,
        "payload": {},
        "warnings": [],
        "unsupported_fields": {},
    }


# ---------------------------------------------------------------------------
# reverse_dependency_order
# ---------------------------------------------------------------------------


def test_reverse_dependency_order_rolls_back_dependents_before_dependencies():
    vlan = _candidate("vlan", "20")
    role = _candidate("role", "employee", dependencies=["vlan:20"])
    wlan = _candidate("wlan", "Corp", dependencies=["vlan:20", "role:employee"])
    ordered = reverse_dependency_order([vlan, role, wlan])
    keys = [f"{c['object_type']}:{c['identifier']}" for c in ordered]
    assert keys.index("wlan:Corp") < keys.index("role:employee")
    assert keys.index("role:employee") < keys.index("vlan:20")


def test_reverse_dependency_order_ignores_dependencies_outside_the_given_set():
    wlan = _candidate("wlan", "Corp", dependencies=["vlan:999"])
    ordered = reverse_dependency_order([wlan])
    assert len(ordered) == 1


def test_reverse_dependency_order_never_raises_on_a_cycle():
    a = _candidate("role", "a", dependencies=["role:b"])
    b = _candidate("role", "b", dependencies=["role:a"])
    ordered = reverse_dependency_order([a, b])
    assert {f"{c['object_type']}:{c['identifier']}" for c in ordered} == {
        "role:a",
        "role:b",
    }


# ---------------------------------------------------------------------------
# plan_rollback
# ---------------------------------------------------------------------------


def test_plan_rollback_supports_candidates_with_verified_delete_operations():
    role = _candidate("role", "employee")
    action = CandidateAction(
        key="role:employee",
        candidate=role,
        delete_operations=[_delete_op("delete_role")],
    )
    plan = plan_rollback([role], _action_for_factory({"role:employee": action}))
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.supported is True
    assert step.source == "delete_operations"
    assert [op.name for op in step.operations] == ["delete_role"]


def test_plan_rollback_supports_candidates_with_verified_classic_rollback_operations():
    wlan = _candidate("wlan", "Corp")
    action = CandidateAction(
        key="wlan:Corp",
        candidate=wlan,
        rollback_operations=[_delete_op("delete_wlan")],
    )
    plan = plan_rollback([wlan], _action_for_factory({"wlan:Corp": action}))
    assert plan.steps[0].supported is True
    assert plan.steps[0].source == "rollback_operations"


def test_plan_rollback_refuses_candidates_with_no_verified_inverse():
    vlan = _candidate("vlan", "20")
    action = CandidateAction(key="vlan:20", candidate=vlan)  # no delete/rollback ops
    plan = plan_rollback([vlan], _action_for_factory({"vlan:20": action}))
    step = plan.steps[0]
    assert step.supported is False
    assert step.operations == ()
    assert "no verified inverse" in step.reason


def test_plan_rollback_refuses_when_action_for_raises_adapter_error():
    role = _candidate("role", "employee")
    plan = plan_rollback([role], _action_for_factory({}))
    step = plan.steps[0]
    assert step.supported is False
    assert "could not re-derive a mapping" in step.reason


def test_plan_rollback_orders_steps_in_reverse_dependency_order():
    vlan = _candidate("vlan", "20")
    role = _candidate("role", "employee", dependencies=["vlan:20"])
    vlan_action = CandidateAction(key="vlan:20", candidate=vlan)
    role_action = CandidateAction(
        key="role:employee",
        candidate=role,
        delete_operations=[_delete_op("delete_role")],
    )
    plan = plan_rollback(
        [vlan, role],
        _action_for_factory({"vlan:20": vlan_action, "role:employee": role_action}),
    )
    assert [step.key for step in plan.steps] == ["role:employee", "vlan:20"]


def test_rollback_plan_to_dict_is_json_safe_and_summarizes():
    import json

    role = _candidate("role", "employee")
    vlan = _candidate("vlan", "20")
    action_map = {
        "role:employee": CandidateAction(
            key="role:employee",
            candidate=role,
            delete_operations=[_delete_op("delete_role")],
        ),
        "vlan:20": CandidateAction(key="vlan:20", candidate=vlan),
    }
    plan = plan_rollback([role, vlan], _action_for_factory(action_map))
    serialized = plan.to_dict()
    json.dumps(serialized)
    assert serialized["summary"] == {"total": 2, "supported": 1, "refused": 1}


# ---------------------------------------------------------------------------
# execute_rollback_plan
# ---------------------------------------------------------------------------


def _plan_with_two_supported_steps():
    a = _candidate("role", "a")
    b = _candidate("role", "b", dependencies=["role:a"])
    action_map = {
        "role:a": CandidateAction(
            key="role:a", candidate=a, delete_operations=[_delete_op("delete_role")]
        ),
        "role:b": CandidateAction(
            key="role:b", candidate=b, delete_operations=[_delete_op("delete_role")]
        ),
    }
    return plan_rollback([a, b], _action_for_factory(action_map))


def test_execute_rollback_plan_dry_run_does_not_require_gates():
    plan = _plan_with_two_supported_steps()
    result = execute_rollback_plan(
        plan,
        dry_run=True,
        confirmation=False,
        write_invoker=lambda operation, confirmation: {"ok": True},
    )
    assert result["dry_run"] is True
    assert result["summary"]["dry_run_ok"] == 2
    assert all(r["status"] == "dry-run" for r in result["results"])


def test_execute_rollback_plan_real_execution_requires_confirmation(monkeypatch):
    monkeypatch.setenv(ROLLBACK_WRITE_GATE_ENV_VAR, "1")
    plan = _plan_with_two_supported_steps()
    with pytest.raises(WriteGateError, match="confirmation=True"):
        execute_rollback_plan(
            plan,
            dry_run=False,
            confirmation=False,
            write_invoker=lambda operation, confirmation: {"ok": True},
        )


def test_execute_rollback_plan_real_execution_requires_rollback_write_gate(monkeypatch):
    monkeypatch.delenv(ROLLBACK_WRITE_GATE_ENV_VAR, raising=False)
    plan = _plan_with_two_supported_steps()
    assert rollback_writes_enabled() is False
    with pytest.raises(WriteGateError, match=ROLLBACK_WRITE_GATE_ENV_VAR):
        execute_rollback_plan(
            plan,
            dry_run=False,
            confirmation=True,
            write_invoker=lambda operation, confirmation: {"ok": True},
        )


def test_execute_rollback_plan_real_execution_succeeds_once_both_gates_are_open(
    monkeypatch,
):
    monkeypatch.setenv(ROLLBACK_WRITE_GATE_ENV_VAR, "1")
    plan = _plan_with_two_supported_steps()
    result = execute_rollback_plan(
        plan,
        dry_run=False,
        confirmation=True,
        write_invoker=lambda operation, confirmation: {"ok": True},
    )
    assert result["summary"]["applied"] == 2
    assert result["summary"]["failed"] == 0


def test_execute_rollback_plan_refused_step_is_never_attempted(monkeypatch):
    monkeypatch.setenv(ROLLBACK_WRITE_GATE_ENV_VAR, "1")
    vlan = _candidate("vlan", "20")
    action = CandidateAction(key="vlan:20", candidate=vlan)
    plan = plan_rollback([vlan], _action_for_factory({"vlan:20": action}))
    invoked = []
    result = execute_rollback_plan(
        plan,
        dry_run=False,
        confirmation=True,
        write_invoker=lambda operation, confirmation: invoked.append(operation) or {"ok": True},
    )
    assert invoked == []
    assert result["results"][0]["status"] == "refused"


def test_execute_rollback_plan_abort_policy_stops_at_first_failure(monkeypatch):
    monkeypatch.setenv(ROLLBACK_WRITE_GATE_ENV_VAR, "1")
    plan = _plan_with_two_supported_steps()

    def failing_invoker(operation, confirmation):
        raise RuntimeError("backend unavailable")

    result = execute_rollback_plan(
        plan,
        dry_run=False,
        confirmation=True,
        write_invoker=failing_invoker,
        conflict_policy=RollbackConflictPolicy.ABORT,
    )
    statuses = [r["status"] for r in result["results"]]
    assert statuses[0] == "failed"
    assert statuses[1] == "not_attempted"
    assert result["summary"]["failed"] == 1
    assert result["summary"]["not_attempted"] == 1


def test_execute_rollback_plan_continue_policy_attempts_every_step(monkeypatch):
    monkeypatch.setenv(ROLLBACK_WRITE_GATE_ENV_VAR, "1")
    plan = _plan_with_two_supported_steps()

    def failing_invoker(operation, confirmation):
        raise RuntimeError("backend unavailable")

    result = execute_rollback_plan(
        plan,
        dry_run=False,
        confirmation=True,
        write_invoker=failing_invoker,
        conflict_policy=RollbackConflictPolicy.CONTINUE,
    )
    statuses = [r["status"] for r in result["results"]]
    assert statuses == ["failed", "failed"]
    assert result["summary"]["not_attempted"] == 0


def test_execute_rollback_plan_resume_from_skips_already_applied_steps(monkeypatch):
    monkeypatch.setenv(ROLLBACK_WRITE_GATE_ENV_VAR, "1")
    plan = _plan_with_two_supported_steps()
    invoked = []
    result = execute_rollback_plan(
        plan,
        dry_run=False,
        confirmation=True,
        write_invoker=lambda operation, confirmation: invoked.append(operation) or {"ok": True},
        resume_from={"role:b": "applied"},
    )
    statuses = {r["candidate"]: r["status"] for r in result["results"]}
    assert statuses["role:b"] == "already_applied"
    assert statuses["role:a"] == "applied"
    assert len(invoked) == 1


def test_execute_rollback_plan_resumes_multi_operation_step_after_partial_success(
    monkeypatch,
):
    monkeypatch.setenv(ROLLBACK_WRITE_GATE_ENV_VAR, "1")
    role = _candidate("role", "employee")
    action = CandidateAction(
        key="role:employee",
        candidate=role,
        delete_operations=[
            _delete_op("delete_assignment"),
            _delete_op("delete_role"),
        ],
    )
    plan = plan_rollback(
        [role],
        _action_for_factory({"role:employee": action}),
    )
    first_calls: list[str] = []

    def fail_second(operation, confirmation):
        first_calls.append(operation.name)
        if operation.name == "delete_role":
            raise RuntimeError("temporary backend failure")
        return {"ok": True}

    first = execute_rollback_plan(
        plan,
        dry_run=False,
        confirmation=True,
        write_invoker=fail_second,
    )

    assert first_calls == ["delete_assignment", "delete_role"]
    first_entry = first["results"][0]
    assert first_entry["status"] == "failed"
    assert first_entry["completed_operations"] == 1

    resumed_calls: list[str] = []
    resumed = execute_rollback_plan(
        plan,
        dry_run=False,
        confirmation=True,
        write_invoker=lambda operation, confirmation: (
            resumed_calls.append(operation.name) or {"ok": True}
        ),
        resume_from={
            "role:employee": {
                "status": "failed",
                "completed_operations": first_entry["completed_operations"],
            }
        },
    )

    assert resumed_calls == ["delete_role"]
    resumed_entry = resumed["results"][0]
    assert resumed_entry["status"] == "applied"
    assert resumed_entry["completed_operations"] == 2
    assert resumed_entry["results"][0]["status"] == "already_applied"


def test_rollback_writes_enabled_defaults_to_disabled(monkeypatch):
    monkeypatch.delenv(ROLLBACK_WRITE_GATE_ENV_VAR, raising=False)
    assert rollback_writes_enabled() is False


def test_rollback_writes_enabled_true_only_for_truthy_values(monkeypatch):
    monkeypatch.setenv(ROLLBACK_WRITE_GATE_ENV_VAR, "yes")
    assert rollback_writes_enabled() is True
    monkeypatch.setenv(ROLLBACK_WRITE_GATE_ENV_VAR, "0")
    assert rollback_writes_enabled() is False
