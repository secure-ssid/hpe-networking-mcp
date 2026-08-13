"""Unit tests for hpe_networking_mcp.pipeline.artifact_contracts.

Covers:
- Valid artifacts for each of the ten supported kinds (including the
  router automation dependency/reconciliation plan kinds and the
  declarative compliance-report kind added in v0.7).
- Invalid/malformed data (missing/wrong-typed required fields).
- Bounded-collection enforcement.
- Deterministic content-hash/digest behavior.
- Recursive redaction of nested secrets and tenant/workspace/account
  identifiers before a write.
- No secret leakage in serialized output or validation error messages.
"""

from __future__ import annotations

import json

import pytest

from hpe_networking_mcp.pipeline import artifact_contracts as contracts

GENERATED_AT = "2026-07-25T12:00:00+00:00"


# ---------------------------------------------------------------------------
# 1. Valid artifacts for each kind
# ---------------------------------------------------------------------------


class TestValidArtifacts:
    def test_live_lifecycle_evidence(self, tmp_path):
        payload = {
            "platform": "aos8",
            "mode": "read_only",
            "generated_at": GENERATED_AT,
            "steps": [{"name": "list_vlans", "status": "ok"}],
            "summary": {"vlan_count": 3},
            "errors": [],
        }
        entry = contracts.write_artifact(
            tmp_path / "evidence.json", contracts.LIVE_LIFECYCLE_EVIDENCE, payload
        )
        assert entry.kind == contracts.LIVE_LIFECYCLE_EVIDENCE
        assert entry.schema_version == 1
        assert entry.redacted is True
        assert entry.size_bytes > 0
        assert len(entry.sha256) == 64

    def test_platform_compatibility_matrix(self, tmp_path):
        payload = {
            "generated_at": GENERATED_AT,
            "entries": [
                {"platform": "edgeconnect", "compatible": True},
                {
                    "platform": "axis",
                    "compatible": False,
                    "reasons": ["operation removed"],
                },
            ],
        }
        entry = contracts.write_artifact(
            tmp_path / "compat.json", contracts.PLATFORM_COMPATIBILITY_RESULT, payload
        )
        assert entry.kind == contracts.PLATFORM_COMPATIBILITY_RESULT

    def test_migration_report_metadata(self, tmp_path):
        payload = {
            "run_id": "run-2026-07-25",
            "generated_at": GENERATED_AT,
            "report_formats": ["csv", "json"],
            "device_count": 12,
            "status_counts": {"done": 10, "failed": 2},
            "device_ref_hashes": [contracts.hash_identifier("SN123456")],
        }
        entry = contracts.write_artifact(
            tmp_path / "migration.json", contracts.MIGRATION_REPORT_METADATA, payload
        )
        assert entry.kind == contracts.MIGRATION_REPORT_METADATA

    def test_capability_snapshot(self, tmp_path):
        payload = {
            "generated_at": GENERATED_AT,
            "platforms": [
                {"platform": "central", "read": 10, "write": 5, "source": "curated"},
                {"platform": "mist", "read": 100, "diagnostic": 2, "source": "generated"},
            ],
        }
        entry = contracts.write_artifact(
            tmp_path / "capability.json", contracts.CAPABILITY_SNAPSHOT, payload
        )
        assert entry.kind == contracts.CAPABILITY_SNAPSHOT

    def test_source_freshness_result(self, tmp_path):
        payload = {
            "generated_at": GENERATED_AT,
            "entries": [
                {"source": "aruba_advisories", "count": 95, "minimum": 90},
                {
                    "source": "hpe_lifecycle_notices",
                    "count": 250,
                    "minimum": 300,
                    "drift_detected": True,
                    "detail": "below minimum",
                },
            ],
        }
        entry = contracts.write_artifact(
            tmp_path / "freshness.json", contracts.SOURCE_FRESHNESS_RESULT, payload
        )
        assert entry.kind == contracts.SOURCE_FRESHNESS_RESULT

    def test_release_artifact_manifest(self, tmp_path):
        inner_entry = contracts.write_artifact(
            tmp_path / "evidence.json",
            contracts.LIVE_LIFECYCLE_EVIDENCE,
            {
                "platform": "mist",
                "mode": "read_only",
                "generated_at": GENERATED_AT,
            },
        )
        payload = {
            "generated_at": GENERATED_AT,
            "release_version": "v0.7.0",
            "entries": [
                {
                    "filename": inner_entry.filename,
                    "kind": inner_entry.kind,
                    "schema_version": inner_entry.schema_version,
                    "size_bytes": inner_entry.size_bytes,
                    "sha256": inner_entry.sha256,
                    "generated_at": inner_entry.generated_at,
                    "redacted": inner_entry.redacted,
                }
            ],
        }
        entry = contracts.write_artifact(
            tmp_path / "manifest.json", contracts.RELEASE_ARTIFACT_MANIFEST, payload
        )
        assert entry.kind == contracts.RELEASE_ARTIFACT_MANIFEST

    def test_router_dependency_plan(self, tmp_path):
        payload = {
            "generated_at": GENERATED_AT,
            "steps": [
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
                    "tool": None,
                    "resolved": False,
                    "ambiguous": False,
                    "capability": "unknown",
                    "platform": None,
                    "depends_on": ["a"],
                },
            ],
            "order": [],
            "acyclic": True,
            "cycles": [],
            "unresolved_step_ids": ["b"],
        }
        entry = contracts.write_artifact(
            tmp_path / "dependency-plan.json", contracts.ROUTER_DEPENDENCY_PLAN, payload
        )
        assert entry.kind == contracts.ROUTER_DEPENDENCY_PLAN
        assert entry.schema_version == 1

    def test_router_reconciliation_plan(self, tmp_path):
        payload = {
            "generated_at": GENERATED_AT,
            "cadence": {"valid": True, "kind": "daily"},
            "entries": [
                {
                    "tool": "list_devices",
                    "server": "central-monitoring",
                    "platform": "central",
                    "capability": "read",
                    "enabled": True,
                }
            ],
            "excluded_count": 1,
            "excluded": [
                {
                    "tool": "reboot_device",
                    "capability": "destructive",
                    "reason": "capability_not_eligible_for_reconciliation",
                }
            ],
            "dry_run": True,
        }
        entry = contracts.write_artifact(
            tmp_path / "reconciliation-plan.json", contracts.ROUTER_RECONCILIATION_PLAN, payload
        )
        assert entry.kind == contracts.ROUTER_RECONCILIATION_PLAN
        assert entry.schema_version == 1

    def test_validation_matrix_result(self, tmp_path):
        payload = {
            "generated_at": GENERATED_AT,
            "entries": [
                {
                    "category": "central",
                    "classification": "offline_fixture",
                    "detail": "offline self-check passed",
                    "read_enabled": False,
                    "write_enabled": False,
                    "credentials_configured": False,
                },
                {
                    "category": "uxi",
                    "classification": "coverage_gap",
                    "detail": "permanent write-gap platform",
                    "read_enabled": True,
                    "write_enabled": True,
                    "credentials_configured": True,
                },
                {
                    "category": "glp",
                    "classification": "blocked",
                    "detail": "no offline path and read gate disabled",
                },
            ],
        }
        entry = contracts.write_artifact(
            tmp_path / "validation-matrix.json", contracts.VALIDATION_MATRIX_RESULT, payload
        )
        assert entry.kind == contracts.VALIDATION_MATRIX_RESULT
        assert entry.schema_version == 1

    def test_compliance_report(self, tmp_path):
        payload = {
            "generated_at": GENERATED_AT,
            "policy_id": "baseline",
            "compliant": False,
            "counts": {"pass": 1, "fail": 1, "error": 0, "skipped": 0},
            "observations": [
                {
                    "observation_index": 0,
                    "observation_id": "sw1",
                    "compliant": False,
                    "counts": {"pass": 1, "fail": 1, "error": 0, "skipped": 0},
                }
            ],
            "results": [
                {
                    "rule_id": "rule_0",
                    "field": "firmware.version",
                    "operator": "version_gte",
                    "status": "pass",
                    "observation_index": 0,
                    "observation_id": "sw1",
                    "severity": "error",
                    "actual": "8.10.0",
                    "message": "",
                },
                {
                    "rule_id": "rule_1",
                    "field": "hostname",
                    "operator": "eq",
                    "status": "fail",
                    "observation_index": 0,
                    "observation_id": "sw1",
                    "severity": "error",
                    "actual": "sw1",
                    "message": "",
                },
            ],
            "results_total": 2,
        }
        entry = contracts.write_artifact(
            tmp_path / "compliance-report.json", contracts.COMPLIANCE_REPORT, payload
        )
        assert entry.kind == contracts.COMPLIANCE_REPORT
        assert entry.schema_version == 1


# ---------------------------------------------------------------------------
# 2. Invalid / malformed data
# ---------------------------------------------------------------------------


class TestInvalidArtifacts:
    def test_unknown_kind_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="unknown artifact kind"):
            contracts.build_artifact("not_a_real_kind", {})

    def test_non_mapping_payload_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="mapping"):
            contracts.build_artifact(contracts.LIVE_LIFECYCLE_EVIDENCE, ["not", "a", "dict"])

    def test_missing_required_field_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError):
            contracts.build_artifact(
                contracts.LIVE_LIFECYCLE_EVIDENCE,
                {"mode": "read_only", "generated_at": GENERATED_AT},
            )

    def test_wrong_type_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="platform"):
            contracts.build_artifact(
                contracts.LIVE_LIFECYCLE_EVIDENCE,
                {"platform": 123, "mode": "read_only", "generated_at": GENERATED_AT},
            )

    def test_invalid_enum_value_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="mode"):
            contracts.build_artifact(
                contracts.LIVE_LIFECYCLE_EVIDENCE,
                {"platform": "aos8", "mode": "read_write_yolo", "generated_at": GENERATED_AT},
            )

    def test_malformed_timestamp_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="ISO-8601"):
            contracts.build_artifact(
                contracts.LIVE_LIFECYCLE_EVIDENCE,
                {"platform": "aos8", "mode": "read_only", "generated_at": "not-a-timestamp"},
            )

    def test_secrets_included_true_is_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="secrets_included"):
            contracts.build_artifact(
                contracts.LIVE_LIFECYCLE_EVIDENCE,
                {
                    "platform": "aos8",
                    "mode": "read_only",
                    "generated_at": GENERATED_AT,
                    "secrets_included": True,
                },
            )

    @pytest.mark.parametrize("filename", [".", ".."])
    def test_manifest_entry_dot_path_rejected(self, filename):
        with pytest.raises(contracts.ArtifactValidationError, match="basename"):
            contracts.ManifestEntry(
                filename=filename,
                kind=contracts.LIVE_LIFECYCLE_EVIDENCE,
                schema_version=1,
                size_bytes=10,
                sha256="a" * 64,
                generated_at=GENERATED_AT,
                redacted=True,
            )

    def test_raw_response_included_true_is_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="raw_response_included"):
            contracts.build_artifact(
                contracts.LIVE_LIFECYCLE_EVIDENCE,
                {
                    "platform": "aos8",
                    "mode": "read_only",
                    "generated_at": GENERATED_AT,
                    "raw_response_included": True,
                },
            )

    def test_raw_target_identifier_hash_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="target_identifier_hash"):
            contracts.build_artifact(
                contracts.LIVE_LIFECYCLE_EVIDENCE,
                {
                    "platform": "aos8",
                    "mode": "read_only",
                    "generated_at": GENERATED_AT,
                    "target_identifier_hash": "my-real-customer-scope",
                },
            )

    def test_incompatible_entry_without_reasons_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="no reasons"):
            contracts.build_artifact(
                contracts.PLATFORM_COMPATIBILITY_RESULT,
                {
                    "generated_at": GENERATED_AT,
                    "entries": [{"platform": "axis", "compatible": False}],
                },
            )

    def test_unsupported_report_format_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="report_formats"):
            contracts.build_artifact(
                contracts.MIGRATION_REPORT_METADATA,
                {
                    "run_id": "run-1",
                    "generated_at": GENERATED_AT,
                    "report_formats": ["xml"],
                },
            )

    def test_raw_device_serial_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="sha256"):
            contracts.build_artifact(
                contracts.MIGRATION_REPORT_METADATA,
                {
                    "run_id": "run-1",
                    "generated_at": GENERATED_AT,
                    "device_ref_hashes": ["CN1234567"],
                },
            )

    def test_duplicate_platform_entries_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="duplicate"):
            contracts.build_artifact(
                contracts.CAPABILITY_SNAPSHOT,
                {
                    "generated_at": GENERATED_AT,
                    "platforms": [
                        {"platform": "mist", "read": 1},
                        {"platform": "mist", "read": 2},
                    ],
                },
            )

    def test_wrong_schema_version_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="schema_version"):
            contracts.build_artifact(
                contracts.LIVE_LIFECYCLE_EVIDENCE,
                {
                    "platform": "aos8",
                    "mode": "read_only",
                    "generated_at": GENERATED_AT,
                    "schema_version": 999,
                },
            )

    def test_kind_mismatch_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="kind"):
            contracts.build_artifact(
                contracts.LIVE_LIFECYCLE_EVIDENCE,
                {
                    "platform": "aos8",
                    "mode": "read_only",
                    "generated_at": GENERATED_AT,
                    "kind": "some_other_kind",
                },
            )

    def test_manifest_entry_path_traversal_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="basename"):
            contracts.build_artifact(
                contracts.RELEASE_ARTIFACT_MANIFEST,
                {
                    "generated_at": GENERATED_AT,
                    "release_version": "v0.7.0",
                    "entries": [
                        {
                            "filename": "../../etc/passwd",
                            "kind": contracts.LIVE_LIFECYCLE_EVIDENCE,
                            "schema_version": 1,
                            "size_bytes": 10,
                            "sha256": "a" * 64,
                            "generated_at": GENERATED_AT,
                            "redacted": True,
                        }
                    ],
                },
            )

    def test_bad_sha256_shape_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="sha256"):
            contracts.build_artifact(
                contracts.RELEASE_ARTIFACT_MANIFEST,
                {
                    "generated_at": GENERATED_AT,
                    "release_version": "v0.7.0",
                    "entries": [
                        {
                            "filename": "evidence.json",
                            "kind": contracts.LIVE_LIFECYCLE_EVIDENCE,
                            "schema_version": 1,
                            "size_bytes": 10,
                            "sha256": "not-hex",
                            "generated_at": GENERATED_AT,
                            "redacted": True,
                        }
                    ],
                },
            )

    def test_dependency_plan_step_not_in_order_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="order"):
            contracts.build_artifact(
                contracts.ROUTER_DEPENDENCY_PLAN,
                {
                    "generated_at": GENERATED_AT,
                    "steps": [
                        {
                            "step_id": "a",
                            "tool": "list_devices",
                            "resolved": True,
                            "ambiguous": False,
                            "capability": "read",
                            "platform": "central",
                            "depends_on": [],
                        }
                    ],
                    "order": ["not-a-real-step"],
                    "acyclic": True,
                    "cycles": [],
                    "unresolved_step_ids": [],
                },
            )

    def test_dependency_plan_duplicate_step_id_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="duplicate"):
            contracts.build_artifact(
                contracts.ROUTER_DEPENDENCY_PLAN,
                {
                    "generated_at": GENERATED_AT,
                    "steps": [
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
                            "step_id": "a",
                            "tool": "list_sites",
                            "resolved": True,
                            "ambiguous": False,
                            "capability": "read",
                            "platform": "central",
                            "depends_on": [],
                        },
                    ],
                    "order": [],
                    "acyclic": True,
                    "cycles": [],
                    "unresolved_step_ids": [],
                },
            )

    def test_reconciliation_plan_excluded_count_below_detail_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="excluded_count"):
            contracts.build_artifact(
                contracts.ROUTER_RECONCILIATION_PLAN,
                {
                    "generated_at": GENERATED_AT,
                    "cadence": {"valid": True, "kind": "daily"},
                    "entries": [],
                    "excluded_count": 0,
                    "excluded": [
                        {
                            "tool": "reboot_device",
                            "capability": "destructive",
                            "reason": "capability_not_eligible_for_reconciliation",
                        }
                    ],
                    "dry_run": True,
                },
            )

    def test_reconciliation_plan_dry_run_false_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="dry_run"):
            contracts.build_artifact(
                contracts.ROUTER_RECONCILIATION_PLAN,
                {
                    "generated_at": GENERATED_AT,
                    "cadence": {"valid": True, "kind": "daily"},
                    "entries": [],
                    "excluded_count": 0,
                    "excluded": [],
                    "dry_run": False,
                },
            )

    def test_validation_matrix_unknown_classification_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="classification"):
            contracts.build_artifact(
                contracts.VALIDATION_MATRIX_RESULT,
                {
                    "generated_at": GENERATED_AT,
                    "entries": [
                        {"category": "central", "classification": "totally_fine"},
                    ],
                },
            )

    def test_validation_matrix_write_without_read_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="write_enabled"):
            contracts.build_artifact(
                contracts.VALIDATION_MATRIX_RESULT,
                {
                    "generated_at": GENERATED_AT,
                    "entries": [
                        {
                            "category": "apstra",
                            "classification": "disposable_write",
                            "read_enabled": False,
                            "write_enabled": True,
                        },
                    ],
                },
            )

    def test_validation_matrix_empty_entries_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="at least one category"):
            contracts.build_artifact(
                contracts.VALIDATION_MATRIX_RESULT,
                {"generated_at": GENERATED_AT, "entries": []},
            )

    def test_validation_matrix_duplicate_category_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="duplicate category"):
            contracts.build_artifact(
                contracts.VALIDATION_MATRIX_RESULT,
                {
                    "generated_at": GENERATED_AT,
                    "entries": [
                        {"category": "central", "classification": "offline_fixture"},
                        {"category": "central", "classification": "blocked"},
                    ],
                },
            )

    def test_compliance_report_unknown_status_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="status"):
            contracts.build_artifact(
                contracts.COMPLIANCE_REPORT,
                {
                    "generated_at": GENERATED_AT,
                    "policy_id": "baseline",
                    "compliant": True,
                    "counts": {"pass": 1, "fail": 0, "error": 0, "skipped": 0},
                    "observations": [
                        {
                            "observation_index": 0,
                            "compliant": True,
                            "counts": {"pass": 1, "fail": 0, "error": 0, "skipped": 0},
                        }
                    ],
                    "results": [
                        {
                            "rule_id": "rule_0",
                            "field": "a",
                            "operator": "eq",
                            "status": "success",
                            "observation_index": 0,
                        }
                    ],
                    "results_total": 1,
                },
            )

    def test_compliance_report_compliant_flag_must_match_counts(self):
        # compliant=True while counts.fail is nonzero must be rejected --
        # this contract can never be success-shaped over a real failure.
        with pytest.raises(contracts.ArtifactValidationError, match="never success-shaped"):
            contracts.build_artifact(
                contracts.COMPLIANCE_REPORT,
                {
                    "generated_at": GENERATED_AT,
                    "policy_id": "baseline",
                    "compliant": True,
                    "counts": {"pass": 0, "fail": 1, "error": 0, "skipped": 0},
                    "observations": [
                        {
                            "observation_index": 0,
                            "compliant": True,
                            "counts": {"pass": 0, "fail": 1, "error": 0, "skipped": 0},
                        }
                    ],
                    "results": [
                        {
                            "rule_id": "rule_0",
                            "field": "a",
                            "operator": "eq",
                            "status": "fail",
                            "observation_index": 0,
                        }
                    ],
                    "results_total": 1,
                },
            )

    def test_compliance_report_counts_missing_key_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="missing required key"):
            contracts.build_artifact(
                contracts.COMPLIANCE_REPORT,
                {
                    "generated_at": GENERATED_AT,
                    "policy_id": "baseline",
                    "compliant": True,
                    "counts": {"pass": 1, "fail": 0, "error": 0},
                    "observations": [
                        {
                            "observation_index": 0,
                            "compliant": True,
                            "counts": {"pass": 1, "fail": 0, "error": 0, "skipped": 0},
                        }
                    ],
                    "results": [],
                    "results_total": 0,
                },
            )

    def test_compliance_report_counts_must_sum_to_results_total(self):
        with pytest.raises(contracts.ArtifactValidationError, match="sum to results_total"):
            contracts.build_artifact(
                contracts.COMPLIANCE_REPORT,
                {
                    "generated_at": GENERATED_AT,
                    "policy_id": "baseline",
                    "compliant": True,
                    "counts": {"pass": 1, "fail": 0, "error": 0, "skipped": 0},
                    "observations": [
                        {
                            "observation_index": 0,
                            "compliant": True,
                            "counts": {"pass": 1, "fail": 0, "error": 0, "skipped": 0},
                        }
                    ],
                    "results": [],
                    "results_total": 5,
                },
            )

    def test_compliance_report_results_total_below_len_results_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="results_total must be"):
            contracts.build_artifact(
                contracts.COMPLIANCE_REPORT,
                {
                    "generated_at": GENERATED_AT,
                    "policy_id": "baseline",
                    "compliant": False,
                    "counts": {"pass": 0, "fail": 2, "error": 0, "skipped": 0},
                    "observations": [
                        {
                            "observation_index": 0,
                            "compliant": False,
                            "counts": {"pass": 0, "fail": 2, "error": 0, "skipped": 0},
                        }
                    ],
                    "results": [
                        {
                            "rule_id": "rule_0",
                            "field": "a",
                            "operator": "eq",
                            "status": "fail",
                            "observation_index": 0,
                        },
                        {
                            "rule_id": "rule_1",
                            "field": "b",
                            "operator": "eq",
                            "status": "fail",
                            "observation_index": 0,
                        },
                    ],
                    "results_total": 1,
                },
            )

    def test_compliance_report_empty_observations_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="at least one entry"):
            contracts.build_artifact(
                contracts.COMPLIANCE_REPORT,
                {
                    "generated_at": GENERATED_AT,
                    "policy_id": "baseline",
                    "compliant": True,
                    "counts": {"pass": 0, "fail": 0, "error": 0, "skipped": 0},
                    "observations": [],
                    "results": [],
                    "results_total": 0,
                },
            )


# ---------------------------------------------------------------------------
# 3. Collection bounds
# ---------------------------------------------------------------------------


class TestBounds:
    def test_evidence_steps_over_bound_rejected(self):
        step_count = contracts.MAX_EVIDENCE_STEPS + 1
        steps = [{"name": f"step-{i}", "status": "ok"} for i in range(step_count)]
        with pytest.raises(contracts.ArtifactValidationError, match="exceeding the bound"):
            contracts.build_artifact(
                contracts.LIVE_LIFECYCLE_EVIDENCE,
                {
                    "platform": "aos8",
                    "mode": "read_only",
                    "generated_at": GENERATED_AT,
                    "steps": steps,
                },
            )

    def test_evidence_steps_at_bound_accepted(self):
        steps = [{"name": f"step-{i}", "status": "ok"} for i in range(contracts.MAX_EVIDENCE_STEPS)]
        artifact = contracts.build_artifact(
            contracts.LIVE_LIFECYCLE_EVIDENCE,
            {
                "platform": "aos8",
                "mode": "read_only",
                "generated_at": GENERATED_AT,
                "steps": steps,
            },
        )
        assert len(artifact.steps) == contracts.MAX_EVIDENCE_STEPS

    def test_compatibility_matrix_platforms_over_bound_rejected(self):
        entries = [
            {"platform": f"platform-{i}", "compatible": True}
            for i in range(contracts.MAX_MATRIX_PLATFORMS + 1)
        ]
        with pytest.raises(contracts.ArtifactValidationError, match="exceeding the bound"):
            contracts.build_artifact(
                contracts.PLATFORM_COMPATIBILITY_RESULT,
                {"generated_at": GENERATED_AT, "entries": entries},
            )

    def test_manifest_entries_over_bound_rejected(self):
        entries = [
            {
                "filename": f"artifact-{i}.json",
                "kind": contracts.LIVE_LIFECYCLE_EVIDENCE,
                "schema_version": 1,
                "size_bytes": 10,
                "sha256": "a" * 64,
                "generated_at": GENERATED_AT,
                "redacted": True,
            }
            for i in range(contracts.MAX_MANIFEST_ENTRIES + 1)
        ]
        with pytest.raises(contracts.ArtifactValidationError, match="exceeding the bound"):
            contracts.build_artifact(
                contracts.RELEASE_ARTIFACT_MANIFEST,
                {
                    "generated_at": GENERATED_AT,
                    "release_version": "v0.7.0",
                    "entries": entries,
                },
            )

    def test_migration_device_ref_hashes_over_bound_rejected(self):
        refs = [
            contracts.hash_identifier(f"sn-{i}")
            for i in range(contracts.MAX_MIGRATION_DEVICE_REFS + 1)
        ]
        with pytest.raises(contracts.ArtifactValidationError, match="exceeding the bound"):
            contracts.build_artifact(
                contracts.MIGRATION_REPORT_METADATA,
                {
                    "run_id": "run-1",
                    "generated_at": GENERATED_AT,
                    "device_ref_hashes": refs,
                },
            )

    def test_dependency_plan_steps_over_bound_rejected(self):
        steps = [
            {
                "step_id": f"step-{i}",
                "tool": None,
                "resolved": False,
                "ambiguous": False,
                "capability": "unknown",
                "platform": None,
                "depends_on": [],
            }
            for i in range(contracts.MAX_ROUTER_PLAN_STEPS + 1)
        ]
        with pytest.raises(contracts.ArtifactValidationError, match="exceeding the bound"):
            contracts.build_artifact(
                contracts.ROUTER_DEPENDENCY_PLAN,
                {
                    "generated_at": GENERATED_AT,
                    "steps": steps,
                    "order": [],
                    "acyclic": True,
                    "cycles": [],
                    "unresolved_step_ids": [f"step-{i}" for i in range(len(steps))],
                },
            )

    def test_dependency_plan_steps_at_bound_accepted(self):
        steps = [
            {
                "step_id": f"step-{i}",
                "tool": None,
                "resolved": False,
                "ambiguous": False,
                "capability": "unknown",
                "platform": None,
                "depends_on": [],
            }
            for i in range(contracts.MAX_ROUTER_PLAN_STEPS)
        ]
        artifact = contracts.build_artifact(
            contracts.ROUTER_DEPENDENCY_PLAN,
            {
                "generated_at": GENERATED_AT,
                "steps": steps,
                "order": [],
                "acyclic": True,
                "cycles": [],
                "unresolved_step_ids": [f"step-{i}" for i in range(len(steps))],
            },
        )
        assert len(artifact.steps) == contracts.MAX_ROUTER_PLAN_STEPS

    def test_reconciliation_plan_entries_over_bound_rejected(self):
        entries = [
            {
                "tool": f"tool-{i}",
                "server": "central-monitoring",
                "platform": "central",
                "capability": "read",
                "enabled": True,
            }
            for i in range(contracts.MAX_ROUTER_RECONCILIATION_ENTRIES + 1)
        ]
        with pytest.raises(contracts.ArtifactValidationError, match="exceeding the bound"):
            contracts.build_artifact(
                contracts.ROUTER_RECONCILIATION_PLAN,
                {
                    "generated_at": GENERATED_AT,
                    "cadence": {"valid": True, "kind": "daily"},
                    "entries": entries,
                    "excluded_count": 0,
                    "excluded": [],
                    "dry_run": True,
                },
            )

    def test_reconciliation_plan_excluded_over_bound_rejected(self):
        excluded = [
            {"tool": f"tool-{i}", "capability": "destructive", "reason": "write"}
            for i in range(contracts.MAX_ROUTER_RECONCILIATION_EXCLUDED + 1)
        ]
        with pytest.raises(contracts.ArtifactValidationError, match="exceeding the bound"):
            contracts.build_artifact(
                contracts.ROUTER_RECONCILIATION_PLAN,
                {
                    "generated_at": GENERATED_AT,
                    "cadence": {"valid": True, "kind": "daily"},
                    "entries": [],
                    "excluded_count": len(excluded),
                    "excluded": excluded,
                    "dry_run": True,
                },
            )

    def test_validation_matrix_entries_over_bound_rejected(self):
        entries = [
            {"category": f"platform-{i}", "classification": "blocked"}
            for i in range(contracts.MAX_VALIDATION_MATRIX_ENTRIES + 1)
        ]
        with pytest.raises(contracts.ArtifactValidationError, match="exceeding the bound"):
            contracts.build_artifact(
                contracts.VALIDATION_MATRIX_RESULT,
                {"generated_at": GENERATED_AT, "entries": entries},
            )

    def test_validation_matrix_entries_at_bound_accepted(self):
        entries = [
            {"category": f"platform-{i}", "classification": "blocked"}
            for i in range(contracts.MAX_VALIDATION_MATRIX_ENTRIES)
        ]
        matrix = contracts.build_artifact(
            contracts.VALIDATION_MATRIX_RESULT,
            {"generated_at": GENERATED_AT, "entries": entries},
        )
        assert len(matrix.entries) == contracts.MAX_VALIDATION_MATRIX_ENTRIES

    def test_validation_matrix_detail_over_bound_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="character bound"):
            contracts.build_artifact(
                contracts.VALIDATION_MATRIX_RESULT,
                {
                    "generated_at": GENERATED_AT,
                    "entries": [
                        {
                            "category": "central",
                            "classification": "blocked",
                            "detail": "x" * (contracts.MAX_VALIDATION_MATRIX_DETAIL_CHARS + 1),
                        }
                    ],
                },
            )

    def test_compliance_report_observations_over_bound_rejected(self):
        count = contracts.MAX_COMPLIANCE_OBSERVATIONS + 1
        observations = [
            {
                "observation_index": i,
                "compliant": True,
                "counts": {"pass": 0, "fail": 0, "error": 0, "skipped": 0},
            }
            for i in range(count)
        ]
        with pytest.raises(contracts.ArtifactValidationError, match="exceeding the bound"):
            contracts.build_artifact(
                contracts.COMPLIANCE_REPORT,
                {
                    "generated_at": GENERATED_AT,
                    "policy_id": "baseline",
                    "compliant": True,
                    "counts": {"pass": 0, "fail": 0, "error": 0, "skipped": 0},
                    "observations": observations,
                    "results": [],
                    "results_total": 0,
                },
            )

    def test_compliance_report_observations_at_bound_accepted(self):
        count = contracts.MAX_COMPLIANCE_OBSERVATIONS
        observations = [
            {
                "observation_index": i,
                "compliant": True,
                "counts": {"pass": 0, "fail": 0, "error": 0, "skipped": 0},
            }
            for i in range(count)
        ]
        artifact = contracts.build_artifact(
            contracts.COMPLIANCE_REPORT,
            {
                "generated_at": GENERATED_AT,
                "policy_id": "baseline",
                "compliant": True,
                "counts": {"pass": 0, "fail": 0, "error": 0, "skipped": 0},
                "observations": observations,
                "results": [],
                "results_total": 0,
            },
        )
        assert len(artifact.observations) == contracts.MAX_COMPLIANCE_OBSERVATIONS

    def test_compliance_report_results_over_bound_rejected(self):
        count = contracts.MAX_COMPLIANCE_RESULTS + 1
        results = [
            {
                "rule_id": f"rule_{i}",
                "field": "a",
                "operator": "eq",
                "status": "pass",
                "observation_index": 0,
            }
            for i in range(count)
        ]
        with pytest.raises(contracts.ArtifactValidationError, match="exceeding the bound"):
            contracts.build_artifact(
                contracts.COMPLIANCE_REPORT,
                {
                    "generated_at": GENERATED_AT,
                    "policy_id": "baseline",
                    "compliant": True,
                    "counts": {"pass": count, "fail": 0, "error": 0, "skipped": 0},
                    "observations": [
                        {
                            "observation_index": 0,
                            "compliant": True,
                            "counts": {"pass": count, "fail": 0, "error": 0, "skipped": 0},
                        }
                    ],
                    "results": results,
                    "results_total": count,
                },
            )

    def test_compliance_report_message_over_bound_rejected(self):
        with pytest.raises(contracts.ArtifactValidationError, match="character bound"):
            contracts.build_artifact(
                contracts.COMPLIANCE_REPORT,
                {
                    "generated_at": GENERATED_AT,
                    "policy_id": "baseline",
                    "compliant": False,
                    "counts": {"pass": 0, "fail": 1, "error": 0, "skipped": 0},
                    "observations": [
                        {
                            "observation_index": 0,
                            "compliant": False,
                            "counts": {"pass": 0, "fail": 1, "error": 0, "skipped": 0},
                        }
                    ],
                    "results": [
                        {
                            "rule_id": "rule_0",
                            "field": "a",
                            "operator": "eq",
                            "status": "fail",
                            "observation_index": 0,
                            "message": "x" * (contracts.MAX_COMPLIANCE_MESSAGE_CHARS + 1),
                        }
                    ],
                    "results_total": 1,
                },
            )


# ---------------------------------------------------------------------------
# 4. Deterministic digest behavior
# ---------------------------------------------------------------------------


class TestDeterministicDigest:
    def test_same_payload_produces_same_digest(self, tmp_path):
        payload = {
            "platform": "aos8",
            "mode": "read_only",
            "generated_at": GENERATED_AT,
            "steps": [{"name": "a", "status": "ok"}],
        }
        entry1 = contracts.write_artifact(
            tmp_path / "e1.json", contracts.LIVE_LIFECYCLE_EVIDENCE, payload
        )
        entry2 = contracts.write_artifact(
            tmp_path / "e2.json", contracts.LIVE_LIFECYCLE_EVIDENCE, payload
        )
        assert entry1.sha256 == entry2.sha256
        assert entry1.size_bytes == entry2.size_bytes

    def test_key_order_does_not_affect_digest(self, tmp_path):
        payload_a = {
            "platform": "aos8",
            "mode": "read_only",
            "generated_at": GENERATED_AT,
        }
        payload_b = {
            "generated_at": GENERATED_AT,
            "mode": "read_only",
            "platform": "aos8",
        }
        entry_a = contracts.write_artifact(
            tmp_path / "a.json", contracts.LIVE_LIFECYCLE_EVIDENCE, payload_a
        )
        entry_b = contracts.write_artifact(
            tmp_path / "b.json", contracts.LIVE_LIFECYCLE_EVIDENCE, payload_b
        )
        assert entry_a.sha256 == entry_b.sha256

    def test_different_payload_produces_different_digest(self, tmp_path):
        payload_a = {"platform": "aos8", "mode": "read_only", "generated_at": GENERATED_AT}
        payload_b = {"platform": "mist", "mode": "read_only", "generated_at": GENERATED_AT}
        entry_a = contracts.write_artifact(
            tmp_path / "a.json", contracts.LIVE_LIFECYCLE_EVIDENCE, payload_a
        )
        entry_b = contracts.write_artifact(
            tmp_path / "b.json", contracts.LIVE_LIFECYCLE_EVIDENCE, payload_b
        )
        assert entry_a.sha256 != entry_b.sha256

    def test_digest_matches_written_file_bytes(self, tmp_path):
        payload = {"platform": "aos8", "mode": "read_only", "generated_at": GENERATED_AT}
        path = tmp_path / "e.json"
        entry = contracts.write_artifact(path, contracts.LIVE_LIFECYCLE_EVIDENCE, payload)
        on_disk = path.read_bytes()
        assert entry.sha256 == contracts.sha256_hex(on_disk)
        assert entry.size_bytes == len(on_disk)


# ---------------------------------------------------------------------------
# 5. Redaction of nested secrets and tenant identifiers
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_nested_secret_is_redacted(self):
        redacted = contracts.redact_artifact_payload(
            {"summary": {"nested": {"api_token": "super-secret-value"}}}
        )
        assert redacted["summary"]["nested"]["api_token"] == "******"
        assert "super-secret-value" not in json.dumps(redacted)

    def test_bearer_token_string_is_redacted(self):
        redacted = contracts.redact_artifact_payload({"auth_header": "Bearer sekret.jwt.value"})
        assert redacted["auth_header"] == "******"

    def test_tenant_workspace_account_ids_are_hashed_not_dropped(self):
        raw = {
            "tenant_id": "tenant-acme-corp",
            "workspace_id": "ws-12345",
            "account_id": "acct-98765",
            "glp_workspace_id": "glp-ws-42",
            "scope_id": "scope-real-customer",
        }
        redacted = contracts.redact_artifact_payload(raw)
        serialized = json.dumps(redacted)
        for key, raw_value in raw.items():
            assert raw_value not in serialized
            assert redacted[key].startswith("sha256:")

    def test_raw_response_body_key_is_omitted(self):
        redacted = contracts.redact_artifact_payload(
            {"raw_response": {"secret_customer_field": "do-not-persist"}}
        )
        assert redacted["raw_response"] == contracts._RAW_PAYLOAD_MARKER
        assert "do-not-persist" not in json.dumps(redacted)

    def test_known_sensitive_values_are_removed_from_free_text(self, tmp_path):
        secret = "AKIA1234567890EXAMPLE"
        tenant = "acme-corp-prod"
        path = tmp_path / "compatibility.json"
        contracts.write_artifact(
            path,
            contracts.PLATFORM_COMPATIBILITY_RESULT,
            {
                "generated_at": GENERATED_AT,
                "entries": [
                    {
                        "platform": "axis",
                        "compatible": False,
                        "reasons": [
                            f"tenant {tenant} rejected credential {secret}"
                        ],
                    }
                ],
            },
            known_sensitive_values=[secret, tenant],
        )
        on_disk = path.read_text()
        assert secret not in on_disk
        assert tenant not in on_disk
        assert on_disk.count("**REDACTED**") == 2

    def test_known_sensitive_values_are_bounded(self):
        with pytest.raises(
            contracts.ArtifactValidationError,
            match="known_sensitive_values",
        ):
            contracts.redact_artifact_payload(
                {"detail": "safe"},
                known_sensitive_values=[
                    str(index)
                    for index in range(contracts.MAX_KNOWN_SENSITIVE_VALUES + 1)
                ],
            )

    def test_write_artifact_redacts_before_persisting(self, tmp_path):
        path = tmp_path / "evidence.json"
        contracts.write_artifact(
            path,
            contracts.LIVE_LIFECYCLE_EVIDENCE,
            {
                "platform": "aos8",
                "mode": "read_only",
                "generated_at": GENERATED_AT,
                "summary": {
                    "password": "hunter2",
                    "scope_id": "real-customer-scope-id",
                },
            },
        )
        on_disk = path.read_text()
        assert "hunter2" not in on_disk
        assert "real-customer-scope-id" not in on_disk

    def test_hash_identifier_is_deterministic_and_irreversible(self):
        first = contracts.hash_identifier("customer-scope-name")
        second = contracts.hash_identifier("customer-scope-name")
        assert first == second
        assert first.startswith("sha256:")
        assert "customer-scope-name" not in first

    def test_reconciliation_plan_redacts_secret_key_in_cadence(self, tmp_path):
        path = tmp_path / "reconciliation-plan.json"
        contracts.write_artifact(
            path,
            contracts.ROUTER_RECONCILIATION_PLAN,
            {
                "generated_at": GENERATED_AT,
                "cadence": {"valid": True, "kind": "daily", "api_token": "hunter2"},
                "entries": [],
                "excluded_count": 1,
                "excluded": [
                    {
                        "tool": "reboot_device",
                        "capability": "destructive",
                        "reason": "capability_not_eligible_for_reconciliation",
                    }
                ],
                "dry_run": True,
            },
        )
        on_disk = path.read_text()
        assert "hunter2" not in on_disk

    def test_reconciliation_plan_redacts_known_sensitive_value_in_reason(self, tmp_path):
        path = tmp_path / "reconciliation-plan.json"
        sensitive_marker = "acme-tenant-secret-marker"
        contracts.write_artifact(
            path,
            contracts.ROUTER_RECONCILIATION_PLAN,
            {
                "generated_at": GENERATED_AT,
                "cadence": {"valid": True, "kind": "daily"},
                "entries": [],
                "excluded_count": 1,
                "excluded": [
                    {
                        "tool": "reboot_device",
                        "capability": "destructive",
                        "reason": f"blocked near {sensitive_marker}",
                    }
                ],
                "dry_run": True,
            },
            known_sensitive_values=[sensitive_marker],
        )
        on_disk = path.read_text()
        assert sensitive_marker not in on_disk

    def test_validation_matrix_redacts_known_sensitive_value_in_detail(self, tmp_path):
        path = tmp_path / "validation-matrix.json"
        sensitive_marker = "tenant-abc-123-secret"
        contracts.write_artifact(
            path,
            contracts.VALIDATION_MATRIX_RESULT,
            {
                "generated_at": GENERATED_AT,
                "entries": [
                    {
                        "category": "central",
                        "classification": "unavailable",
                        "detail": f"credential lookup failed for {sensitive_marker}",
                    }
                ],
            },
            known_sensitive_values=[sensitive_marker],
        )
        on_disk = path.read_text()
        assert sensitive_marker not in on_disk


# ---------------------------------------------------------------------------
# No secret leakage in serialized output / error messages
# ---------------------------------------------------------------------------


class TestNoSecretLeakage:
    def test_validation_error_never_echoes_field_values(self):
        try:
            contracts.build_artifact(
                contracts.LIVE_LIFECYCLE_EVIDENCE,
                {
                    "platform": "aos8",
                    "mode": "not-a-real-mode-super-secret-token-abc123",
                    "generated_at": GENERATED_AT,
                },
            )
        except contracts.ArtifactValidationError as exc:
            assert "super-secret-token-abc123" not in str(exc)
        else:
            pytest.fail("expected ArtifactValidationError")

    def test_type_error_from_unknown_field_never_echoes_secret_value(self):
        try:
            contracts.build_artifact(
                contracts.LIVE_LIFECYCLE_EVIDENCE,
                {
                    "platform": "aos8",
                    "mode": "read_only",
                    "generated_at": GENERATED_AT,
                    "unexpected_api_key": "sekret-value-should-never-appear",
                },
            )
        except contracts.ArtifactValidationError as exc:
            assert "sekret-value-should-never-appear" not in str(exc)
        else:
            pytest.fail("expected ArtifactValidationError")


# ---------------------------------------------------------------------------
# Atomic write behavior
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_write_artifact_leaves_no_temp_file_behind(self, tmp_path):
        path = tmp_path / "evidence.json"
        contracts.write_artifact(
            path,
            contracts.LIVE_LIFECYCLE_EVIDENCE,
            {"platform": "aos8", "mode": "read_only", "generated_at": GENERATED_AT},
        )
        remaining = list(tmp_path.iterdir())
        assert remaining == [path]

    def test_write_artifact_overwrites_atomically(self, tmp_path):
        path = tmp_path / "evidence.json"
        contracts.write_artifact(
            path,
            contracts.LIVE_LIFECYCLE_EVIDENCE,
            {"platform": "aos8", "mode": "read_only", "generated_at": GENERATED_AT},
        )
        contracts.write_artifact(
            path,
            contracts.LIVE_LIFECYCLE_EVIDENCE,
            {"platform": "mist", "mode": "read_only", "generated_at": GENERATED_AT},
        )
        data = json.loads(path.read_text())
        assert data["platform"] == "mist"
