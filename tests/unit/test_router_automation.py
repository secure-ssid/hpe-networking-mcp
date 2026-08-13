"""Unit tests for hpe_networking_mcp.pipeline.router_automation.

Covers:
- Deterministic dependency ordering (Kahn's algorithm), including ties.
- Cycle detection (simple, self-loop, and multi-node cycles), bounded.
- Cadence validation (named cadences, interval-minutes bounds, cron
  structural validation) -- always returns a dict, never raises.
- Reconciliation candidate partitioning: read/diagnostic-only entries,
  bounded excluded detail list with an accurate total count.
- Artifact payload shaping helpers produce a shape that
  hpe_networking_mcp.pipeline.artifact_contracts accepts.
"""

from __future__ import annotations

from hpe_networking_mcp.pipeline import artifact_contracts as contracts
from hpe_networking_mcp.pipeline import router_automation as ra

GENERATED_AT = "2026-07-25T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Dependency ordering
# ---------------------------------------------------------------------------


class TestResolveDependencyOrder:
    def test_linear_chain(self):
        order, cycles = ra.resolve_dependency_order(["a", "b", "c"], {"b": ["a"], "c": ["b"]})
        assert order == ["a", "b", "c"]
        assert cycles == []

    def test_independent_steps_preserve_input_order(self):
        order, cycles = ra.resolve_dependency_order(["a", "b", "c"], {})
        assert order == ["a", "b", "c"]
        assert cycles == []

    def test_diamond_dependency(self):
        order, cycles = ra.resolve_dependency_order(
            ["a", "b", "c", "d"], {"b": ["a"], "c": ["a"], "d": ["b", "c"]}
        )
        assert order is not None
        assert order.index("a") < order.index("b") < order.index("d")
        assert order.index("a") < order.index("c") < order.index("d")
        assert cycles == []

    def test_deterministic_tie_break_uses_input_order(self):
        # b and c both become ready at the same time; input order must win.
        order1, _ = ra.resolve_dependency_order(["a", "c", "b"], {"b": ["a"], "c": ["a"]})
        order2, _ = ra.resolve_dependency_order(["a", "c", "b"], {"b": ["a"], "c": ["a"]})
        assert order1 == order2 == ["a", "c", "b"]

    def test_simple_two_node_cycle(self):
        order, cycles = ra.resolve_dependency_order(["a", "b"], {"a": ["b"], "b": ["a"]})
        assert order is None
        assert len(cycles) == 1
        assert set(cycles[0]) == {"a", "b"}

    def test_self_loop_cycle(self):
        order, cycles = ra.resolve_dependency_order(["a"], {"a": ["a"]})
        assert order is None
        assert cycles == [["a", "a"]]

    def test_three_node_cycle(self):
        order, cycles = ra.resolve_dependency_order(
            ["a", "b", "c"], {"a": ["b"], "b": ["c"], "c": ["a"]}
        )
        assert order is None
        assert len(cycles) >= 1

    def test_unresolved_dependency_ignored_by_ordering_itself(self):
        # "missing" isn't in step_ids -- this function silently ignores it
        # (the caller is responsible for reporting unresolved deps).
        order, cycles = ra.resolve_dependency_order(["a"], {"a": ["missing"]})
        assert order == ["a"]
        assert cycles == []

    def test_duplicate_step_ids_do_not_produce_an_order(self):
        order, cycles = ra.resolve_dependency_order(["a", "a"], {})
        assert order is None
        assert cycles == []

    def test_cycles_bounded(self):
        # Build more independent 2-cycles than MAX_CYCLES_REPORTED.
        step_ids = []
        edges = {}
        for i in range(ra.MAX_CYCLES_REPORTED + 5):
            a, b = f"a{i}", f"b{i}"
            step_ids += [a, b]
            edges[a] = [b]
            edges[b] = [a]
        order, cycles = ra.resolve_dependency_order(step_ids, edges)
        assert order is None
        assert len(cycles) <= ra.MAX_CYCLES_REPORTED


# ---------------------------------------------------------------------------
# Cadence validation
# ---------------------------------------------------------------------------


class TestValidateCadence:
    def test_named_cadence_string(self):
        result = ra.validate_cadence("daily")
        assert result == {"valid": True, "kind": "daily"}

    def test_unknown_cadence_kind(self):
        result = ra.validate_cadence("fortnightly")
        assert result["valid"] is False
        assert "fortnightly" in result["reason"]

    def test_wrong_type(self):
        result = ra.validate_cadence(123)  # type: ignore[arg-type]
        assert result["valid"] is False

    def test_interval_minutes_valid(self):
        result = ra.validate_cadence({"kind": "interval_minutes", "interval_minutes": 30})
        assert result == {"valid": True, "kind": "interval_minutes", "interval_minutes": 30}

    def test_interval_minutes_below_bound(self):
        result = ra.validate_cadence({"kind": "interval_minutes", "interval_minutes": 1})
        assert result["valid"] is False

    def test_interval_minutes_above_bound(self):
        result = ra.validate_cadence(
            {"kind": "interval_minutes", "interval_minutes": ra.MAX_INTERVAL_MINUTES + 1}
        )
        assert result["valid"] is False

    def test_interval_minutes_wrong_type(self):
        result = ra.validate_cadence({"kind": "interval_minutes", "interval_minutes": "30"})
        assert result["valid"] is False

    def test_interval_minutes_bool_rejected(self):
        # bool is an int subclass -- must not be silently accepted.
        result = ra.validate_cadence({"kind": "interval_minutes", "interval_minutes": True})
        assert result["valid"] is False

    def test_cron_valid(self):
        result = ra.validate_cadence({"kind": "cron", "expression": "*/15 * * * *"})
        assert result == {"valid": True, "kind": "cron", "expression": "*/15 * * * *"}

    def test_cron_wrong_field_count(self):
        result = ra.validate_cadence({"kind": "cron", "expression": "* * * *"})
        assert result["valid"] is False

    def test_cron_invalid_field(self):
        result = ra.validate_cadence({"kind": "cron", "expression": "abc * * * *"})
        assert result["valid"] is False

    def test_cron_missing_expression(self):
        result = ra.validate_cadence({"kind": "cron"})
        assert result["valid"] is False

    def test_cron_expression_length_is_bounded(self):
        expression = ",".join(
            str(index)
            for index in range(ra.MAX_CADENCE_EXPRESSION_CHARS)
        )
        result = ra.validate_cadence(
            {"kind": "cron", "expression": f"{expression} * * * *"}
        )
        assert result["valid"] is False
        assert "cannot exceed" in result["reason"]

    def test_never_raises_on_garbage_input(self):
        for garbage in (None, [], {}, {"kind": None}, {"kind": "cron", "expression": None}):
            result = ra.validate_cadence(garbage)  # type: ignore[arg-type]
            assert isinstance(result, dict)
            assert "valid" in result


# ---------------------------------------------------------------------------
# Reconciliation candidate partitioning
# ---------------------------------------------------------------------------


class TestPartitionReconciliationCandidates:
    def test_only_read_and_diagnostic_become_entries(self):
        candidates = [
            {"tool": "list_devices", "capability": "read"},
            {"tool": "aos_s_ping", "capability": "diagnostic"},
            {"tool": "reboot_device", "capability": "destructive"},
            {"tool": "create_vlan", "capability": "write"},
            {"tool": "mystery", "capability": "unknown"},
        ]
        entries, excluded_detail, excluded_total = ra.partition_reconciliation_candidates(
            candidates
        )
        assert [e["tool"] for e in entries] == ["list_devices", "aos_s_ping"]
        assert excluded_total == 3
        assert len(excluded_detail) == 3
        reasons = {item["reason"] for item in excluded_detail}
        assert reasons == {"capability_not_eligible_for_reconciliation"}

    def test_entries_bounded_by_max_entries(self):
        candidates = [{"tool": f"t{i}", "capability": "read"} for i in range(5)]
        entries, excluded_detail, excluded_total = ra.partition_reconciliation_candidates(
            candidates, max_entries=2
        )
        assert len(entries) == 2
        assert excluded_total == 3
        assert all(
            item["reason"] == "reconciliation_entry_bound_exceeded" for item in excluded_detail
        )

    def test_excluded_detail_capped_but_total_accurate(self):
        candidates = [{"tool": f"t{i}", "capability": "write"} for i in range(10)]
        entries, excluded_detail, excluded_total = ra.partition_reconciliation_candidates(
            candidates, max_excluded_detail=3
        )
        assert entries == []
        assert excluded_total == 10
        assert len(excluded_detail) == 3

    def test_empty_input(self):
        entries, excluded_detail, excluded_total = ra.partition_reconciliation_candidates([])
        assert entries == []
        assert excluded_detail == []
        assert excluded_total == 0


# ---------------------------------------------------------------------------
# Artifact payload shaping -- must round-trip through artifact_contracts.
# ---------------------------------------------------------------------------


class TestArtifactPayloadShaping:
    def test_dependency_plan_payload_round_trips(self, tmp_path):
        order, cycles = ra.resolve_dependency_order(["a", "b"], {"b": ["a"]})
        payload = ra.build_dependency_plan_payload(
            steps=[
                {
                    "step_id": "a",
                    "tool": "list_devices",
                    "resolved": True,
                    "ambiguous": False,
                    "capability": "read",
                    "platform": "central",
                    "depends_on": [],
                },
                {
                    "step_id": "b",
                    "tool": "list_sites",
                    "resolved": True,
                    "ambiguous": False,
                    "capability": "read",
                    "platform": "central",
                    "depends_on": ["a"],
                },
            ],
            order=order,
            acyclic=not cycles,
            cycles=cycles,
            unresolved_step_ids=[],
            generated_at=GENERATED_AT,
        )
        entry = contracts.write_artifact(
            tmp_path / "plan.json", contracts.ROUTER_DEPENDENCY_PLAN, payload
        )
        assert entry.kind == contracts.ROUTER_DEPENDENCY_PLAN
        assert entry.schema_version == 1

    def test_dependency_plan_payload_with_cycle(self, tmp_path):
        order, cycles = ra.resolve_dependency_order(["a", "b"], {"a": ["b"], "b": ["a"]})
        payload = ra.build_dependency_plan_payload(
            steps=[
                {"step_id": "a", "tool": "t1", "resolved": True, "ambiguous": False,
                 "capability": "read", "platform": "central", "depends_on": ["b"]},
                {"step_id": "b", "tool": "t2", "resolved": True, "ambiguous": False,
                 "capability": "read", "platform": "central", "depends_on": ["a"]},
            ],
            order=order,
            acyclic=not cycles,
            cycles=cycles,
            unresolved_step_ids=[],
            generated_at=GENERATED_AT,
        )
        assert payload["acyclic"] is False
        assert payload["order"] == []
        entry = contracts.write_artifact(
            tmp_path / "plan_cycle.json", contracts.ROUTER_DEPENDENCY_PLAN, payload
        )
        assert entry.kind == contracts.ROUTER_DEPENDENCY_PLAN

    def test_reconciliation_plan_payload_round_trips(self, tmp_path):
        cadence = ra.validate_cadence("hourly")
        entries, excluded_detail, excluded_total = ra.partition_reconciliation_candidates(
            [
                {"tool": "list_devices", "server": "central-monitoring", "platform": "central",
                 "capability": "read", "enabled": True},
                {"tool": "reboot_device", "server": "central-ops", "platform": "central",
                 "capability": "destructive", "enabled": True},
            ]
        )
        payload = ra.build_reconciliation_plan_payload(
            cadence=cadence,
            entries=entries,
            excluded=excluded_detail,
            excluded_count=excluded_total,
            generated_at=GENERATED_AT,
        )
        assert payload["dry_run"] is True
        entry = contracts.write_artifact(
            tmp_path / "recon.json", contracts.ROUTER_RECONCILIATION_PLAN, payload
        )
        assert entry.kind == contracts.ROUTER_RECONCILIATION_PLAN

    def test_reconciliation_plan_payload_excluded_count_can_exceed_detail_list(self, tmp_path):
        cadence = ra.validate_cadence("daily")
        payload = ra.build_reconciliation_plan_payload(
            cadence=cadence,
            entries=[],
            excluded=[{"tool": "t0", "capability": "write", "reason": "x"}],
            excluded_count=500,
            generated_at=GENERATED_AT,
        )
        entry = contracts.write_artifact(
            tmp_path / "recon_capped.json", contracts.ROUTER_RECONCILIATION_PLAN, payload
        )
        assert entry.kind == contracts.ROUTER_RECONCILIATION_PLAN
