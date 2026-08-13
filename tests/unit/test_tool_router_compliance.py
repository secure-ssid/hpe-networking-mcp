"""Unit tests for the router-level compliance-policy evaluator:
``evaluate_compliance_policy`` in ``hpe_networking_mcp.mcp_servers.tool_router``.

Covers:
- Registration only outside ``minimal`` mode (matches plan_tool_workflow /
  plan_reconciliation_schedule).
- A valid evaluation returns "ok", accurate counts, and a valid
  ``compliance_report`` artifact.
- An invalid policy/observations input fails closed with "ok": False and a
  bounded error message, before any rule evaluation.
- Bounded per-rule result detail with an accurate results_total/
  results_truncated, independent of the artifact's own bound.
- The tool never calls ``invoke_tool``/``invoke_read_tool`` (no dispatch
  happens as a side effect of evaluation) -- it is pure, caller-supplied-
  data evaluation only.
- Read-only annotation on the registered MCPServer tool.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import hpe_networking_mcp.mcp_servers.tool_router as router
from hpe_networking_mcp.mcp_servers.shared import READ_ONLY


def _evaluate_compliance_policy_fn():
    fn = getattr(router, "evaluate_compliance_policy", None)
    if fn is None:
        pytest.skip("evaluate_compliance_policy not registered (router in minimal mode)")
    return fn


def _call(tool_fn, *args, **kwargs):
    """Call a @mcp.tool()-wrapped function, unwrapping FunctionTool if needed."""
    target = getattr(tool_fn, "fn", tool_fn)
    return target(*args, **kwargs)


class TestRegistration:
    def test_registered_outside_minimal_mode(self):
        fn = _evaluate_compliance_policy_fn()
        assert fn is not None

    def test_annotated_read_only(self):
        _evaluate_compliance_policy_fn()
        tool = router.mcp._tool_manager._tools.get("evaluate_compliance_policy")
        assert tool is not None
        assert tool.annotations.read_only_hint is READ_ONLY.read_only_hint
        assert tool.annotations.destructive_hint is READ_ONLY.destructive_hint


class TestEvaluateCompliancePolicySuccess:
    def test_valid_evaluation_reports_pass_fail_counts(self):
        fn = _evaluate_compliance_policy_fn()
        out = _call(
            fn,
            observations=[
                {"hostname": "sw1", "firmware": {"version": "8.10.0"}},
                {"hostname": "sw2", "firmware": {"version": "8.0.0"}},
            ],
            policy=[{"field": "firmware.version", "operator": "version_gte", "expected": "8.9.0"}],
        )
        assert out["ok"] is True
        assert out["compliant"] is False
        assert out["counts"] == {"pass": 1, "fail": 1, "error": 0, "skipped": 0}
        assert out["observation_count"] == 2
        assert out["rule_count"] == 1

    def test_valid_evaluation_produces_artifact(self):
        fn = _evaluate_compliance_policy_fn()
        out = _call(
            fn,
            observations=[{"hostname": "sw1", "up": True}],
            policy=[{"field": "up", "operator": "eq", "expected": True}],
        )
        assert out["artifact"] is not None
        assert out["artifact_error"] is None
        assert out["artifact"]["kind"] == "compliance_report"
        assert out["artifact"]["schema_version"] == 1

    def test_policy_id_carried_through(self):
        fn = _evaluate_compliance_policy_fn()
        out = _call(
            fn,
            observations=[{"up": True}],
            policy=[{"field": "up", "operator": "eq", "expected": True}],
            policy_id="baseline-2026",
        )
        assert out["policy_id"] == "baseline-2026"
        assert out["artifact"]["policy_id"] == "baseline-2026"

    def test_bounded_result_detail_with_accurate_total(self):
        fn = _evaluate_compliance_policy_fn()
        observations = [{"a": i} for i in range(5)]
        out = _call(
            fn,
            observations=observations,
            policy=[{"field": "a", "operator": "ge", "expected": 0}],
            max_result_entries=2,
        )
        assert len(out["results"]) == 2
        assert out["results_total"] == 5
        assert out["results_truncated"] is True
        assert out["counts"]["pass"] == 5


class TestEvaluateCompliancePolicyFailsClosed:
    def test_invalid_operator_fails_closed(self):
        fn = _evaluate_compliance_policy_fn()
        out = _call(
            fn,
            observations=[{"a": 1}],
            policy=[{"field": "a", "operator": "sql_inject", "expected": 1}],
        )
        assert out["ok"] is False
        assert "error" in out
        assert "results" not in out

    def test_empty_observations_fails_closed(self):
        fn = _evaluate_compliance_policy_fn()
        out = _call(fn, observations=[], policy=[{"field": "a", "operator": "eq", "expected": 1}])
        assert out["ok"] is False

    def test_malformed_field_path_fails_closed(self):
        fn = _evaluate_compliance_policy_fn()
        out = _call(
            fn,
            observations=[{"a": 1}],
            policy=[{"field": "a; import os", "operator": "eq", "expected": 1}],
        )
        assert out["ok"] is False

    def test_oversized_policy_fails_closed(self):
        fn = _evaluate_compliance_policy_fn()
        from hpe_networking_mcp.pipeline import compliance as pc

        policy = [
            {"field": "a", "operator": "eq", "expected": 1} for _ in range(pc.MAX_POLICY_RULES + 1)
        ]
        out = _call(fn, observations=[{"a": 1}], policy=policy)
        assert out["ok"] is False

    def test_oversized_regex_pattern_fails_closed(self):
        fn = _evaluate_compliance_policy_fn()
        from hpe_networking_mcp.pipeline import compliance as pc

        out = _call(
            fn,
            observations=[{"a": "x"}],
            policy=[
                {
                    "field": "a",
                    "operator": "regex_fullmatch",
                    "expected": "a" * (pc.MAX_REGEX_PATTERN_CHARS + 1),
                }
            ],
        )
        assert out["ok"] is False

    def test_malformed_version_fails_closed(self):
        fn = _evaluate_compliance_policy_fn()
        out = _call(
            fn,
            observations=[{"a": "1.0"}],
            policy=[{"field": "a", "operator": "version_gte", "expected": "not-a-version"}],
        )
        assert out["ok"] is False

    def test_invalid_result_limit_fails_closed(self):
        fn = _evaluate_compliance_policy_fn()
        out = _call(
            fn,
            observations=[{"a": 1}],
            policy=[{"field": "a", "operator": "eq", "expected": 1}],
            max_result_entries=True,
        )
        assert out["ok"] is False

    def test_artifact_validation_failure_is_not_success_shaped(self, monkeypatch):
        fn = _evaluate_compliance_policy_fn()

        def _fail_artifact(*args, **kwargs):
            raise router._artifact_contracts.ArtifactValidationError("contract mismatch")

        monkeypatch.setattr(router._artifact_contracts, "build_artifact", _fail_artifact)
        out = _call(
            fn,
            observations=[{"a": 1}],
            policy=[{"field": "a", "operator": "eq", "expected": 1}],
        )
        assert out["ok"] is False
        assert out["artifact"] is None
        assert out["artifact_error"] == "contract mismatch"


class TestNeverDispatches:
    def test_never_calls_invoke_tool_or_invoke_read_tool(self, monkeypatch):
        spy = AsyncMock(
            side_effect=AssertionError("evaluate_compliance_policy must never dispatch")
        )
        monkeypatch.setattr(router, "invoke_tool", spy, raising=True)
        monkeypatch.setattr(router, "invoke_read_tool", spy, raising=True)
        fn = _evaluate_compliance_policy_fn()
        out = _call(
            fn,
            observations=[{"up": True}],
            policy=[{"field": "up", "operator": "eq", "expected": True}],
        )
        assert out["ok"] is True
        spy.assert_not_called()

    def test_does_not_require_loaded_backend_catalog(self, monkeypatch):
        # Unlike plan_tool_workflow/plan_reconciliation_schedule, this tool
        # never resolves anything against the backend catalog -- it must
        # work even if _load_all_backends is never called successfully.
        def _boom():
            raise AssertionError("evaluate_compliance_policy must not load backends")

        monkeypatch.setattr(router, "_load_all_backends", _boom, raising=True)
        fn = _evaluate_compliance_policy_fn()
        out = _call(
            fn,
            observations=[{"up": True}],
            policy=[{"field": "up", "operator": "eq", "expected": True}],
        )
        assert out["ok"] is True


# ---------------------------------------------------------------------------
# Regression: leaf/container secret & tenant redaction, end-to-end through
# the registered router tool (never just hpe_networking_mcp.pipeline.compliance directly).
# ---------------------------------------------------------------------------


class TestRedactionEndToEndThroughRouter:
    def test_leaf_secret_never_appears_in_response_or_artifact(self):
        fn = _evaluate_compliance_policy_fn()
        out = _call(
            fn,
            observations=[
                {"hostname": "sw1", "credentials": {"password": "s3cr3t-router-leak"}}
            ],
            policy=[{"field": "credentials.password", "operator": "exists"}],
        )
        assert out["ok"] is True
        assert "s3cr3t-router-leak" not in str(out)
        assert out["results"][0]["actual"] != "s3cr3t-router-leak"

    def test_container_secret_never_appears_in_response_or_artifact(self):
        fn = _evaluate_compliance_policy_fn()
        out = _call(
            fn,
            observations=[
                {
                    "hostname": "sw1",
                    "devices": [{"name": "sw1", "api_key": "AKIA-router-leak"}],
                }
            ],
            policy=[{"field": "devices", "operator": "exists"}],
        )
        assert out["ok"] is True
        assert "AKIA-router-leak" not in str(out)

    def test_leaf_tenant_id_never_appears_in_response_or_artifact(self):
        fn = _evaluate_compliance_policy_fn()
        out = _call(
            fn,
            observations=[{"hostname": "sw1", "account": {"tenant_id": "tenant-router-leak"}}],
            policy=[{"field": "account.tenant_id", "operator": "exists"}],
        )
        assert out["ok"] is True
        assert "tenant-router-leak" not in str(out)


# ---------------------------------------------------------------------------
# Regression: evaluator/artifact-contract mismatch -- a realistic, valid,
# legitimately large observation (a 50-interface list) must always produce
# a non-null artifact with no artifact_error through the registered
# router tool.
# ---------------------------------------------------------------------------


class TestRealisticOversizedObservationArtifact:
    def test_fifty_interface_observation_produces_valid_artifact(self):
        fn = _evaluate_compliance_policy_fn()
        interfaces = [
            {
                "name": f"GigabitEthernet0/{i}",
                "status": "up" if i % 2 == 0 else "down",
                "speed": "1000",
                "mac": f"00:11:22:33:44:{i:02x}",
                "description": f"port {i} realistic description text for load testing",
                "vlan": 100 + i,
                "duplex": "full",
                "errors": 0,
            }
            for i in range(50)
        ]
        out = _call(
            fn,
            observations=[{"hostname": "sw1", "interfaces": interfaces}],
            policy=[{"field": "interfaces", "operator": "exists"}],
        )
        assert out["ok"] is True
        assert out["artifact"] is not None
        assert out["artifact_error"] is None
        assert out["artifact"]["kind"] == "compliance_report"


# ---------------------------------------------------------------------------
# Regression: fail-closed ReDoS-safe regex subset, end-to-end through the
# registered router tool.
# ---------------------------------------------------------------------------


class TestSafeRegexSubsetThroughRouter:
    def test_nested_quantified_group_policy_fails_closed(self):
        fn = _evaluate_compliance_policy_fn()
        out = _call(
            fn,
            observations=[{"value": "a" * 40 + "!"}],
            policy=[{"field": "value", "operator": "regex_fullmatch", "expected": r"(a+)+b"}],
        )
        assert out["ok"] is False
        assert "results" not in out

    def test_backreference_policy_fails_closed(self):
        fn = _evaluate_compliance_policy_fn()
        out = _call(
            fn,
            observations=[{"value": "abab"}],
            policy=[{"field": "value", "operator": "regex_fullmatch", "expected": r"(ab)\1"}],
        )
        assert out["ok"] is False

    def test_ordinary_bounded_pattern_still_evaluates(self):
        fn = _evaluate_compliance_policy_fn()
        out = _call(
            fn,
            observations=[{"hostname": "sw12"}],
            policy=[{"field": "hostname", "operator": "regex_fullmatch", "expected": r"sw[0-9]+"}],
        )
        assert out["ok"] is True
        assert out["compliant"] is True
