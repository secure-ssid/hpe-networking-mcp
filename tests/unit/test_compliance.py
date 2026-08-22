"""Unit tests for hpe_networking_mcp.pipeline.compliance.

Covers:
- Safe dotted/indexed field extraction (both "a.b.0.c" and "a.b[0].c"
  notation), including missing/out-of-range segments and disallowed
  characters.
- Every fixed operator (eq, ne, lt, le, gt, ge, contains, in,
  regex_fullmatch, version_gte, version_range, exists, not_exists).
- Fail-closed policy validation: unknown operator, malformed field path,
  wrong "expected" shape per operator, duplicate rule ids, missing
  "expected", invalid severity, oversized policy/observations.
- Regex bounds (pattern length, total-quantifier-count policy, invalid
  pattern) and version bounds (non-numeric, oversized, malformed range).
- "optional" rule semantics (missing field -> skipped instead of error).
- Aggregate report shape/counts, including bounded per-rule result detail
  with an accurate results_total/results_truncated.
- build_compliance_report_payload shape is accepted by
  hpe_networking_mcp.pipeline.artifact_contracts.
"""

from __future__ import annotations

import json

import pytest

from hpe_networking_mcp.pipeline import artifact_contracts as contracts
from hpe_networking_mcp.pipeline import compliance as c

# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------


class TestExtractField:
    def test_simple_key(self):
        assert c.extract_field({"a": 1}, "a") == 1

    def test_dotted_nested_key(self):
        assert c.extract_field({"a": {"b": {"c": 3}}}, "a.b.c") == 3

    def test_dotted_list_index(self):
        assert c.extract_field({"interfaces": [{"name": "eth0"}]}, "interfaces.0.name") == "eth0"

    def test_bracket_list_index(self):
        assert c.extract_field({"interfaces": [{"name": "eth0"}]}, "interfaces[0].name") == "eth0"

    def test_missing_key_is_sentinel(self):
        assert c.extract_field({"a": 1}, "b") is c._MISSING

    def test_out_of_range_index_is_sentinel(self):
        assert c.extract_field({"a": [1]}, "a.5") is c._MISSING

    def test_non_digit_index_against_list_is_sentinel(self):
        assert c.extract_field({"a": [1]}, "a.x") is c._MISSING

    def test_indexing_into_scalar_is_sentinel(self):
        assert c.extract_field({"a": 1}, "a.b") is c._MISSING

    def test_empty_field_path_rejected(self):
        with pytest.raises(c.ComplianceError):
            c.extract_field({"a": 1}, "")

    def test_non_string_field_path_rejected(self):
        with pytest.raises(c.ComplianceError):
            c.extract_field({"a": 1}, None)  # type: ignore[arg-type]

    def test_disallowed_characters_rejected(self):
        with pytest.raises(c.ComplianceError):
            c.extract_field({"a": 1}, "a; import os")

    def test_oversized_field_path_rejected(self):
        with pytest.raises(c.ComplianceError):
            c.extract_field({"a": 1}, "a" * (c.MAX_FIELD_PATH_CHARS + 1))

    def test_never_uses_eval_or_getattr(self):
        class Sneaky:
            def __getattr__(self, item):
                raise AssertionError("extract_field must never use attribute access")

        assert c.extract_field({"a": Sneaky()}, "a.b") is c._MISSING


# ---------------------------------------------------------------------------
# Policy validation -- fail closed
# ---------------------------------------------------------------------------


class TestValidatePolicy:
    def test_valid_minimal_policy(self):
        normalized = c.validate_policy([{"field": "a", "operator": "eq", "expected": 1}])
        assert normalized[0]["id"] == "rule_0"
        assert normalized[0]["severity"] == "error"
        assert normalized[0]["optional"] is False

    def test_non_list_policy_rejected(self):
        with pytest.raises(c.ComplianceError, match="list"):
            c.validate_policy({"field": "a"})

    def test_empty_policy_rejected(self):
        with pytest.raises(c.ComplianceError, match="at least one rule"):
            c.validate_policy([])

    def test_oversized_policy_rejected(self):
        rule_count = c.MAX_POLICY_RULES + 1
        policy = [{"field": "a", "operator": "eq", "expected": 1} for _ in range(rule_count)]
        with pytest.raises(c.ComplianceError, match="exceeding"):
            c.validate_policy(policy)

    def test_non_object_rule_rejected(self):
        with pytest.raises(c.ComplianceError, match="must be an object"):
            c.validate_policy(["not-a-rule"])

    def test_duplicate_rule_id_rejected(self):
        policy = [
            {"id": "r1", "field": "a", "operator": "eq", "expected": 1},
            {"id": "r1", "field": "b", "operator": "eq", "expected": 2},
        ]
        with pytest.raises(c.ComplianceError, match="duplicate rule id"):
            c.validate_policy(policy)

    def test_unknown_operator_rejected(self):
        with pytest.raises(c.ComplianceError, match="operator"):
            c.validate_policy([{"field": "a", "operator": "sql_inject", "expected": 1}])

    def test_malformed_field_path_rejected(self):
        with pytest.raises(c.ComplianceError):
            c.validate_policy([{"field": "a; b", "operator": "eq", "expected": 1}])

    def test_invalid_severity_rejected(self):
        with pytest.raises(c.ComplianceError, match="severity"):
            c.validate_policy(
                [{"field": "a", "operator": "eq", "expected": 1, "severity": "urgent"}]
            )

    def test_non_bool_optional_rejected(self):
        with pytest.raises(c.ComplianceError, match="optional"):
            c.validate_policy(
                [{"field": "a", "operator": "eq", "expected": 1, "optional": "yes"}]
            )

    def test_missing_expected_rejected_for_eq(self):
        with pytest.raises(c.ComplianceError, match="expected"):
            c.validate_policy([{"field": "a", "operator": "eq"}])

    def test_exists_does_not_require_expected(self):
        normalized = c.validate_policy([{"field": "a", "operator": "exists"}])
        assert normalized[0]["operator"] == "exists"

    def test_not_exists_does_not_require_expected(self):
        normalized = c.validate_policy([{"field": "a", "operator": "not_exists"}])
        assert normalized[0]["operator"] == "not_exists"

    @pytest.mark.parametrize(
        "operator,expected",
        [
            ("lt", "not-a-number"),
            ("le", "not-a-number"),
            ("gt", "not-a-number"),
            ("ge", "not-a-number"),
            ("lt", True),  # bool explicitly excluded from numeric operators
        ],
    )
    def test_numeric_operator_rejects_non_numeric_expected(self, operator, expected):
        with pytest.raises(c.ComplianceError):
            c.validate_policy([{"field": "a", "operator": operator, "expected": expected}])

    def test_in_requires_list_expected(self):
        with pytest.raises(c.ComplianceError, match="list"):
            c.validate_policy([{"field": "a", "operator": "in", "expected": "not-a-list"}])

    def test_in_expected_list_bound_enforced(self):
        oversized = list(range(c.MAX_EXPECTED_LIST_ITEMS + 1))
        with pytest.raises(c.ComplianceError, match="bound"):
            c.validate_policy([{"field": "a", "operator": "in", "expected": oversized}])

    def test_regex_pattern_must_be_string(self):
        with pytest.raises(c.ComplianceError):
            c.validate_policy([{"field": "a", "operator": "regex_fullmatch", "expected": 123}])

    def test_regex_pattern_length_bound_enforced(self):
        pattern = "a" * (c.MAX_REGEX_PATTERN_CHARS + 1)
        with pytest.raises(c.ComplianceError, match="bound"):
            c.validate_policy(
                [{"field": "a", "operator": "regex_fullmatch", "expected": pattern}]
            )

    def test_regex_total_quantifier_bound_enforced(self):
        # This module's actual policy (see hpe_networking_mcp.pipeline.compliance's module
        # docstring and TestSafeRegexSubset below): at most
        # MAX_REGEX_TOTAL_QUANTIFIERS (currently 1) quantifier opcode is
        # permitted anywhere in the whole pattern -- "a*a*" is two sibling
        # (non-nested) quantifiers and must be rejected.
        with pytest.raises(c.ComplianceError, match="quantifier"):
            c.validate_policy(
                [{"field": "a", "operator": "regex_fullmatch", "expected": "a*a*"}]
            )

    def test_invalid_regex_syntax_rejected(self):
        with pytest.raises(c.ComplianceError, match="invalid"):
            c.validate_policy(
                [{"field": "a", "operator": "regex_fullmatch", "expected": "a(b"}]
            )

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("operator", "x" * (c.MAX_RULE_ID_CHARS + 1)),
            ("severity", "x" * (c.MAX_RULE_ID_CHARS + 1)),
        ],
    )
    def test_operator_and_severity_error_values_are_bounded(self, field, value):
        rule = {"field": "a", "operator": "eq", "expected": 1}
        rule[field] = value
        with pytest.raises(c.ComplianceError) as exc_info:
            c.validate_policy([rule])
        assert len(str(exc_info.value)) < 250
        assert value not in str(exc_info.value)

    def test_version_gte_requires_dotted_numeric_string(self):
        with pytest.raises(c.ComplianceError):
            c.validate_policy(
                [{"field": "a", "operator": "version_gte", "expected": "not-a-version"}]
            )

    def test_version_range_requires_min_or_max(self):
        with pytest.raises(c.ComplianceError, match="min"):
            c.validate_policy([{"field": "a", "operator": "version_range", "expected": {}}])

    def test_version_range_min_must_not_exceed_max(self):
        with pytest.raises(c.ComplianceError, match="<="):
            c.validate_policy(
                [
                    {
                        "field": "a",
                        "operator": "version_range",
                        "expected": {"min": "9.0.0", "max": "8.0.0"},
                    }
                ]
            )

    def test_version_range_accepts_min_only(self):
        normalized = c.validate_policy(
            [{"field": "a", "operator": "version_range", "expected": {"min": "1.0.0"}}]
        )
        assert normalized[0]["expected"] == {"min": "1.0.0"}


# ---------------------------------------------------------------------------
# Per-rule evaluation -- every operator
# ---------------------------------------------------------------------------


def _bare_rule(field: str, operator: str) -> dict:
    return {
        "id": "r",
        "field": field,
        "operator": operator,
        "severity": "error",
        "optional": False,
    }


_OPERATORS_TAKING_EXPECTED = (
    "eq", "ne", "lt", "le", "gt", "ge", "contains", "in",
    "regex_fullmatch", "version_gte", "version_range",
)


def _one_rule(operator: str, expected=None, **extra) -> dict:
    rule = {
        "id": "r",
        "field": "value",
        "operator": operator,
        "severity": "error",
        "optional": False,
    }
    if expected is not None or operator in _OPERATORS_TAKING_EXPECTED:
        rule["expected"] = expected
    rule.update(extra)
    return rule


class TestEvaluateRuleOperators:
    def test_eq_pass(self):
        result = c.evaluate_rule({"value": 5}, _one_rule("eq", 5))
        assert result["status"] == "pass"

    def test_eq_fail(self):
        result = c.evaluate_rule({"value": 5}, _one_rule("eq", 6))
        assert result["status"] == "fail"

    def test_ne_pass(self):
        result = c.evaluate_rule({"value": 5}, _one_rule("ne", 6))
        assert result["status"] == "pass"

    def test_lt_pass(self):
        assert c.evaluate_rule({"value": 5}, _one_rule("lt", 10))["status"] == "pass"

    def test_le_pass_on_equal(self):
        assert c.evaluate_rule({"value": 5}, _one_rule("le", 5))["status"] == "pass"

    def test_gt_fail(self):
        assert c.evaluate_rule({"value": 5}, _one_rule("gt", 10))["status"] == "fail"

    def test_ge_pass_on_equal(self):
        assert c.evaluate_rule({"value": 5}, _one_rule("ge", 5))["status"] == "pass"

    def test_numeric_operator_errors_on_non_numeric_actual(self):
        result = c.evaluate_rule({"value": "not-a-number"}, _one_rule("lt", 10))
        assert result["status"] == "error"

    def test_contains_list(self):
        assert c.evaluate_rule({"value": [1, 2, 3]}, _one_rule("contains", 2))["status"] == "pass"

    def test_contains_string(self):
        rule = _one_rule("contains", "world")
        assert c.evaluate_rule({"value": "hello world"}, rule)["status"] == "pass"

    def test_contains_mapping_keys(self):
        rule = _one_rule("contains", "vlan10")
        assert c.evaluate_rule({"value": {"vlan10": True}}, rule)["status"] == "pass"

    def test_contains_wrong_actual_type_errors(self):
        result = c.evaluate_rule({"value": 5}, _one_rule("contains", 1))
        assert result["status"] == "error"

    def test_in_pass(self):
        rule = _one_rule("in", ["up", "down"])
        assert c.evaluate_rule({"value": "up"}, rule)["status"] == "pass"

    def test_in_fail(self):
        rule = _one_rule("in", ["up", "down"])
        assert c.evaluate_rule({"value": "unknown"}, rule)["status"] == "fail"

    def test_regex_fullmatch_pass(self):
        rule = _one_rule("regex_fullmatch", r"sw[0-9]+")
        assert c.evaluate_rule({"value": "sw12"}, rule)["status"] == "pass"

    def test_regex_fullmatch_is_full_not_partial(self):
        rule = _one_rule("regex_fullmatch", r"sw[0-9]+")
        assert c.evaluate_rule({"value": "sw12x"}, rule)["status"] == "fail"

    def test_regex_fullmatch_non_string_actual_errors(self):
        rule = _one_rule("regex_fullmatch", r"[0-9]+")
        assert c.evaluate_rule({"value": 123}, rule)["status"] == "error"

    def test_version_gte_pass(self):
        rule = _one_rule("version_gte", "8.9.0")
        assert c.evaluate_rule({"value": "8.10.0"}, rule)["status"] == "pass"

    def test_version_gte_fail(self):
        rule = _one_rule("version_gte", "8.9.0")
        assert c.evaluate_rule({"value": "8.8.0"}, rule)["status"] == "fail"

    def test_version_gte_treats_trailing_zero_segment_as_equal(self):
        rule = _one_rule("version_gte", "8.10")
        assert c.evaluate_rule({"value": "8.10.0"}, rule)["status"] == "pass"

    def test_version_gte_non_version_actual_errors(self):
        rule = _one_rule("version_gte", "8.9.0")
        assert c.evaluate_rule({"value": "not-a-version"}, rule)["status"] == "error"

    def test_version_range_pass_within_bounds(self):
        rule = _one_rule("version_range", {"min": "8.0.0", "max": "9.0.0"})
        assert c.evaluate_rule({"value": "8.5.0"}, rule)["status"] == "pass"

    def test_version_range_fail_below_min(self):
        rule = _one_rule("version_range", {"min": "8.0.0", "max": "9.0.0"})
        assert c.evaluate_rule({"value": "7.9.9"}, rule)["status"] == "fail"

    def test_version_range_fail_above_max(self):
        rule = _one_rule("version_range", {"min": "8.0.0", "max": "9.0.0"})
        assert c.evaluate_rule({"value": "9.0.1"}, rule)["status"] == "fail"

    def test_exists_pass(self):
        rule = _bare_rule("value", "exists")
        assert c.evaluate_rule({"value": 1}, rule)["status"] == "pass"

    def test_exists_fail_when_missing(self):
        rule = _bare_rule("missing", "exists")
        assert c.evaluate_rule({"value": 1}, rule)["status"] == "fail"

    def test_not_exists_pass_when_missing(self):
        rule = _bare_rule("missing", "not_exists")
        assert c.evaluate_rule({"value": 1}, rule)["status"] == "pass"

    def test_not_exists_fail_when_present(self):
        rule = _bare_rule("value", "not_exists")
        assert c.evaluate_rule({"value": 1}, rule)["status"] == "fail"


class TestOptionalRuleSemantics:
    def test_missing_field_errors_by_default(self):
        rule = _one_rule("eq", 1)
        result = c.evaluate_rule({}, rule)
        assert result["status"] == "error"

    def test_missing_field_skips_when_optional(self):
        rule = _one_rule("eq", 1, optional=True)
        result = c.evaluate_rule({}, rule)
        assert result["status"] == "skipped"


# ---------------------------------------------------------------------------
# Aggregate report
# ---------------------------------------------------------------------------


class TestEvaluatePolicy:
    def test_basic_report_shape_and_counts(self):
        report = c.evaluate_policy(
            observations=[{"hostname": "sw1", "up": True}, {"hostname": "sw2", "up": False}],
            policy=[{"field": "up", "operator": "eq", "expected": True}],
        )
        assert report["rule_count"] == 1
        assert report["observation_count"] == 2
        assert report["counts"] == {"pass": 1, "fail": 1, "error": 0, "skipped": 0}
        assert report["compliant"] is False
        assert report["results_total"] == 2
        assert report["results_truncated"] is False
        assert [o["compliant"] for o in report["observations"]] == [True, False]

    def test_compliant_true_when_all_pass(self):
        report = c.evaluate_policy(
            observations=[{"up": True}],
            policy=[{"field": "up", "operator": "eq", "expected": True}],
        )
        assert report["compliant"] is True

    def test_observation_identifier_extracted_when_present(self):
        report = c.evaluate_policy(
            observations=[{"hostname": "sw1", "up": True}],
            policy=[{"field": "up", "operator": "eq", "expected": True}],
        )
        assert report["observations"][0]["observation_id"] == "sw1"

    def test_non_list_observations_rejected(self):
        policy = [{"field": "a", "operator": "eq", "expected": 1}]
        with pytest.raises(c.ComplianceError, match="list"):
            c.evaluate_policy(observations={"a": 1}, policy=policy)

    def test_empty_observations_rejected(self):
        policy = [{"field": "a", "operator": "eq", "expected": 1}]
        with pytest.raises(c.ComplianceError, match="at least one entry"):
            c.evaluate_policy(observations=[], policy=policy)

    def test_oversized_observations_rejected(self):
        observations = [{"a": 1} for _ in range(c.MAX_OBSERVATIONS + 1)]
        policy = [{"field": "a", "operator": "eq", "expected": 1}]
        with pytest.raises(c.ComplianceError, match="exceeding"):
            c.evaluate_policy(observations=observations, policy=policy)

    def test_non_object_observation_rejected(self):
        policy = [{"field": "a", "operator": "eq", "expected": 1}]
        with pytest.raises(c.ComplianceError, match="must be an object"):
            c.evaluate_policy(observations=["not-a-dict"], policy=policy)

    def test_invalid_policy_raises_before_any_evaluation(self):
        # An operator that doesn't exist must fail closed -- no partial report.
        with pytest.raises(c.ComplianceError):
            c.evaluate_policy(
                observations=[{"a": 1}], policy=[{"field": "a", "operator": "nope", "expected": 1}]
            )

    def test_bounded_result_detail_with_accurate_total(self):
        observations = [{"a": i} for i in range(5)]
        policy = [{"field": "a", "operator": "ge", "expected": 0}]
        report = c.evaluate_policy(observations=observations, policy=policy, max_result_entries=2)
        assert len(report["results"]) == 2
        assert report["results_total"] == 5
        assert report["results_truncated"] is True
        # Aggregate counts are never truncated, even though detail is.
        assert report["counts"]["pass"] == 5

    def test_max_result_entries_is_bounded_by_module_ceiling(self):
        observations = [{"a": 1}]
        policy = [{"field": "a", "operator": "eq", "expected": 1}]
        report = c.evaluate_policy(
            observations=observations, policy=policy, max_result_entries=10_000_000
        )
        # Only one result exists anyway, but the cap itself must never exceed
        # MAX_RESULT_ENTRIES regardless of what the caller requests.
        assert report["results_total"] == 1

    @pytest.mark.parametrize("value", [None, "2", 1.5, True])
    def test_max_result_entries_must_be_an_integer(self, value):
        with pytest.raises(c.ComplianceError, match="must be an integer"):
            c.evaluate_policy(
                observations=[{"a": 1}],
                policy=[{"field": "a", "operator": "eq", "expected": 1}],
                max_result_entries=value,
            )


# ---------------------------------------------------------------------------
# Artifact payload shaping accepted by hpe_networking_mcp.pipeline.artifact_contracts
# ---------------------------------------------------------------------------


class TestBuildCompliancePayload:
    def test_payload_builds_a_valid_artifact(self, tmp_path):
        report = c.evaluate_policy(
            observations=[{"hostname": "sw1", "up": True}],
            policy=[{"field": "up", "operator": "eq", "expected": True}],
        )
        payload = c.build_compliance_report_payload(
            policy_id=report["policy_id"],
            compliant=report["compliant"],
            counts=report["counts"],
            observations=report["observations"],
            results=report["results"],
            results_total=report["results_total"],
        )
        entry = contracts.write_artifact(
            tmp_path / "compliance-report.json", contracts.COMPLIANCE_REPORT, payload
        )
        assert entry.kind == contracts.COMPLIANCE_REPORT
        assert entry.schema_version == 1


# ---------------------------------------------------------------------------
# Regression: leaf/container secret & tenant redaction (evaluate_rule must
# never expose a raw credential/tenant-identifier leaf under the generic
# "actual" key, and must recursively redact nested sensitive/tenant keys
# inside a container "actual" value too).
# ---------------------------------------------------------------------------


class TestActualRedaction:
    def test_leaf_password_field_is_redacted(self):
        rule = _bare_rule("credentials.password", "exists")
        observation = {"credentials": {"password": "s3cr3t-value", "username": "admin"}}
        result = c.evaluate_rule(observation, rule)
        assert result["status"] == "pass"
        assert result["actual"] == c._REDACTED_SENSITIVE
        assert "s3cr3t-value" not in str(result)

    def test_leaf_api_key_field_is_redacted(self):
        rule = _bare_rule("device.api_key", "exists")
        observation = {"device": {"api_key": "AKIA-super-secret"}}
        result = c.evaluate_rule(observation, rule)
        assert result["actual"] == c._REDACTED_SENSITIVE
        assert "AKIA-super-secret" not in str(result)

    def test_leaf_tenant_id_field_is_redacted(self):
        rule = _bare_rule("account.tenant_id", "exists")
        observation = {"account": {"tenant_id": "tenant-abc-12345"}}
        result = c.evaluate_rule(observation, rule)
        assert result["actual"] == c._REDACTED_TENANT
        assert "tenant-abc-12345" not in str(result)

    def test_leaf_secret_redacted_even_when_rule_compares_it(self):
        # eq against a secret leaf must still redact "actual" in the result
        # even though the comparison itself necessarily used the raw value.
        rule = _one_rule("eq", "s3cret", field="credentials.password")
        observation = {"credentials": {"password": "s3cret"}}
        result = c.evaluate_rule(observation, rule)
        assert result["status"] == "pass"
        assert result["actual"] == c._REDACTED_SENSITIVE

    def test_container_value_with_nested_secret_key_is_redacted(self):
        # Extracting the whole "credentials" object is itself flagged as a
        # sensitive leaf key (its own field name), so the whole value is
        # redacted outright.
        rule = _bare_rule("credentials", "exists")
        observation = {"credentials": {"password": "s3cret", "username": "admin"}}
        result = c.evaluate_rule(observation, rule)
        assert result["actual"] == c._REDACTED_SENSITIVE

    def test_container_list_with_nested_secret_keys_is_redacted_recursively(self):
        # The field itself ("devices") is not sensitive, but each nested
        # dict's "api_key" is -- recursion must catch it per-item.
        rule = _bare_rule("devices", "exists")
        observation = {
            "devices": [
                {"name": "sw1", "api_key": "AKIA-secret-1"},
                {"name": "sw2", "api_key": "AKIA-secret-2"},
            ]
        }
        result = c.evaluate_rule(observation, rule)
        assert "AKIA-secret-1" not in str(result)
        assert "AKIA-secret-2" not in str(result)
        assert result["actual"][0]["api_key"] == c._REDACTED_SENSITIVE
        assert result["actual"][0]["name"] == "sw1"

    def test_container_dict_with_nested_tenant_key_is_redacted_recursively(self):
        rule = _bare_rule("device", "exists")
        observation = {"device": {"name": "sw1", "workspace_id": "ws-98765"}}
        result = c.evaluate_rule(observation, rule)
        assert "ws-98765" not in str(result)
        assert result["actual"]["workspace_id"] == c._REDACTED_TENANT
        assert result["actual"]["name"] == "sw1"

    def test_deeply_nested_secret_is_still_redacted_within_depth_bound(self):
        rule = _bare_rule("a", "exists")
        observation = {"a": {"b": {"c": {"password": "deep-secret"}}}}
        result = c.evaluate_rule(observation, rule)
        assert "deep-secret" not in str(result)


# ---------------------------------------------------------------------------
# Regression: sensitive/tenant *ancestor* field-path segments must redact
# "actual" even when the leaf segment alone is not sensitive/tenant-shaped
# (e.g. "auth.value", "token.raw", "account.tenant_id.v"), and a numeric
# list-index segment (e.g. the "0" in "credentials[0]") must never defeat
# detection of a sensitive/tenant segment elsewhere in the same path.
# Previously `_bound_actual` only ever inspected the *leaf* segment, so
# each of these field paths bypassed redaction entirely.
# ---------------------------------------------------------------------------


class TestSensitiveAncestorFieldPathRedaction:
    def test_auth_dot_value_ancestor_redacts_despite_nonsensitive_leaf(self):
        # Leaf segment "value" is not sensitive-shaped on its own; the
        # ancestor "auth" is exactly sensitive-shaped (see
        # _SENSITIVE_KEY_EXACT) and must still trigger redaction.
        rule = _bare_rule("auth.value", "exists")
        observation = {"auth": {"value": "s3cr3t-auth-value"}}
        result = c.evaluate_rule(observation, rule)
        assert result["actual"] == c._REDACTED_SENSITIVE
        assert "s3cr3t-auth-value" not in str(result)

    def test_token_dot_raw_ancestor_redacts_despite_nonsensitive_leaf(self):
        # Leaf segment "raw" is not sensitive-shaped; the ancestor "token"
        # is sensitive-shaped by suffix match and must still redact.
        rule = _bare_rule("token.raw", "exists")
        observation = {"token": {"raw": "s3cr3t-token-raw"}}
        result = c.evaluate_rule(observation, rule)
        assert result["actual"] == c._REDACTED_SENSITIVE
        assert "s3cr3t-token-raw" not in str(result)

    def test_bracket_index_ancestor_redacts_and_numeric_segment_never_defeats_it(self):
        # "credentials[0]" normalizes to the dotted "credentials.0" -- the
        # purely numeric "0" segment must be skipped over (never treated
        # as the sole/leaf segment that decides redaction) while the
        # sensitive "credentials" ancestor segment is still detected.
        rule = _bare_rule("credentials[0]", "exists")
        observation = {"credentials": ["s3cr3t-credentials-0"]}
        result = c.evaluate_rule(observation, rule)
        assert result["actual"] == c._REDACTED_SENSITIVE
        assert "s3cr3t-credentials-0" not in str(result)

    def test_dotted_index_form_of_same_path_also_redacts(self):
        # The dotted-index spelling of the same path (rather than the
        # bracket spelling) must behave identically.
        rule = _bare_rule("credentials.0", "exists")
        observation = {"credentials": ["s3cr3t-credentials-dot-0"]}
        result = c.evaluate_rule(observation, rule)
        assert result["actual"] == c._REDACTED_SENSITIVE
        assert "s3cr3t-credentials-dot-0" not in str(result)

    def test_account_tenant_id_dot_v_ancestor_redacts_as_tenant(self):
        # Leaf segment "v" is not tenant-shaped; the middle ancestor
        # segment "tenant_id" is exactly tenant-shaped (see
        # _TENANT_KEY_EXACT) and must still trigger tenant redaction, even
        # though it is neither the first nor the last segment.
        rule = _bare_rule("account.tenant_id.v", "exists")
        observation = {"account": {"tenant_id": {"v": "tenant-abc-99999"}}}
        result = c.evaluate_rule(observation, rule)
        assert result["actual"] == c._REDACTED_TENANT
        assert "tenant-abc-99999" not in str(result)

    def test_sensitive_ancestor_takes_priority_over_tenant_ancestor(self):
        # A path mixing both shapes (a sensitive segment and a tenant
        # segment) must still redact -- sensitive wins outright, but
        # either alone is already sufficient reason to redact.
        rule = _bare_rule("credentials.tenant_id", "exists")
        observation = {"credentials": {"tenant_id": "either-marker-is-enough"}}
        result = c.evaluate_rule(observation, rule)
        assert result["actual"] in (c._REDACTED_SENSITIVE, c._REDACTED_TENANT)
        assert "either-marker-is-enough" not in str(result)

    def test_non_sensitive_ancestor_and_leaf_is_not_redacted(self):
        # Control case: no segment anywhere in the path is sensitive/
        # tenant-shaped -- the value must pass through unredacted.
        rule = _bare_rule("device.hostname.short", "exists")
        observation = {"device": {"hostname": {"short": "sw1"}}}
        result = c.evaluate_rule(observation, rule)
        assert result["actual"] == "sw1"


class TestEvaluatorArtifactWriteRedactionEndToEnd:
    """End-to-end evaluator -> artifact payload -> write_artifact
    regression coverage: a raw secret/tenant identifier must never appear
    in the bytes actually written to disk, for both a leaf-secret field
    path and a container value holding nested secret keys."""

    def test_leaf_secret_never_written_to_disk(self, tmp_path):
        report = c.evaluate_policy(
            observations=[{"hostname": "sw1", "credentials": {"password": "s3cr3t-on-disk"}}],
            policy=[{"field": "credentials.password", "operator": "exists"}],
        )
        payload = c.build_compliance_report_payload(
            policy_id=report["policy_id"],
            compliant=report["compliant"],
            counts=report["counts"],
            observations=report["observations"],
            results=report["results"],
            results_total=report["results_total"],
        )
        destination = tmp_path / "compliance-leaf-secret.json"
        entry = contracts.write_artifact(destination, contracts.COMPLIANCE_REPORT, payload)
        assert entry.kind == contracts.COMPLIANCE_REPORT
        on_disk = destination.read_text(encoding="utf-8")
        assert "s3cr3t-on-disk" not in on_disk

    def test_container_secret_never_written_to_disk(self, tmp_path):
        report = c.evaluate_policy(
            observations=[
                {
                    "hostname": "sw1",
                    "devices": [
                        {"name": "sw1", "api_key": "AKIA-disk-secret-1"},
                        {"name": "sw2", "api_key": "AKIA-disk-secret-2"},
                    ],
                }
            ],
            policy=[{"field": "devices", "operator": "exists"}],
        )
        payload = c.build_compliance_report_payload(
            policy_id=report["policy_id"],
            compliant=report["compliant"],
            counts=report["counts"],
            observations=report["observations"],
            results=report["results"],
            results_total=report["results_total"],
        )
        destination = tmp_path / "compliance-container-secret.json"
        entry = contracts.write_artifact(destination, contracts.COMPLIANCE_REPORT, payload)
        assert entry.kind == contracts.COMPLIANCE_REPORT
        on_disk = destination.read_text(encoding="utf-8")
        assert "AKIA-disk-secret-1" not in on_disk
        assert "AKIA-disk-secret-2" not in on_disk


# ---------------------------------------------------------------------------
# Regression: a failing version_gte/version_range rule must never echo the
# raw "actual"/"expected" caller value into its ComplianceError/result
# "message" -- previously `_parse_version` embedded `{value!r}` directly
# into its error text, which `evaluate_rule` then stored verbatim as
# "message", bypassing `_bound_actual`'s redaction entirely (that
# redaction choke point only ever governs the "actual" field, never
# "message").
# ---------------------------------------------------------------------------


class TestVersionErrorMessageNeverLeaksCallerValue:
    def test_parse_version_message_never_echoes_raw_value(self):
        secret = "sk-live-TENANT-42-ULTRASECRET"
        with pytest.raises(c.ComplianceError) as excinfo:
            c._parse_version(secret, label="actual value")
        assert secret not in str(excinfo.value)

    def test_parse_version_message_never_echoes_raw_expected_value(self):
        secret = "tenant-workspace-abc-999"
        with pytest.raises(c.ComplianceError) as excinfo:
            c._parse_version(secret, label="policy[0] expected")
        assert secret not in str(excinfo.value)

    def test_failing_version_rule_message_never_contains_actual_value(self):
        # A non-sensitive field path (so this isolates the *message* fix
        # from the independent "actual" leaf-key redaction path): the
        # value legitimately shows up in "actual" (unredacted, by design,
        # since the field itself is not sensitive/tenant-shaped), but the
        # error "message" describing why the version failed to parse must
        # never contain it.
        secret_like_value = "tenant-99-not-a-real-version"
        rule = _one_rule("version_gte", "8.9.0", field="firmware.version")
        observation = {"firmware": {"version": secret_like_value}}
        result = c.evaluate_rule(observation, rule)
        assert result["status"] == "error"
        assert secret_like_value not in result["message"]

    def test_failing_version_rule_on_sensitive_field_leaks_nowhere(self):
        # Combined regression: a *sensitive*-shaped field path holding a
        # secret that also happens to fail version parsing. The secret
        # must not appear in "actual" (already redacted via the sensitive
        # leaf key) *nor* in "message" (the bug this test targets), nor
        # anywhere in a fully serialized compliance_report artifact
        # (build_artifact) or the bytes written to disk (write_artifact).
        secret = "sk-live-TENANT-42-ULTRASECRET-token-value"
        report = c.evaluate_policy(
            observations=[{"hostname": "sw1", "credentials": {"token": secret}}],
            policy=[
                {"field": "credentials.token", "operator": "version_gte", "expected": "1.0.0"}
            ],
        )
        result = report["results"][0]
        assert result["status"] == "error"
        assert result["actual"] == c._REDACTED_SENSITIVE
        assert secret not in result["message"]
        assert secret not in json.dumps(report, default=str)

        payload = c.build_compliance_report_payload(
            policy_id=report["policy_id"],
            compliant=report["compliant"],
            counts=report["counts"],
            observations=report["observations"],
            results=report["results"],
            results_total=report["results_total"],
        )
        artifact = contracts.build_artifact(contracts.COMPLIANCE_REPORT, payload)
        serialized_artifact = json.dumps(contracts.to_json_dict(artifact), default=str)
        assert secret not in serialized_artifact

    def test_failing_version_rule_on_sensitive_field_never_written_to_disk(self, tmp_path):
        secret = "AKIA-version-field-secret-leak-check"
        report = c.evaluate_policy(
            observations=[{"hostname": "sw1", "device": {"api_key": secret}}],
            policy=[{"field": "device.api_key", "operator": "version_gte", "expected": "1.0.0"}],
        )
        payload = c.build_compliance_report_payload(
            policy_id=report["policy_id"],
            compliant=report["compliant"],
            counts=report["counts"],
            observations=report["observations"],
            results=report["results"],
            results_total=report["results_total"],
        )
        destination = tmp_path / "compliance-version-error-secret.json"
        entry = contracts.write_artifact(destination, contracts.COMPLIANCE_REPORT, payload)
        assert entry.kind == contracts.COMPLIANCE_REPORT
        on_disk = destination.read_text(encoding="utf-8")
        assert secret not in on_disk


# ---------------------------------------------------------------------------
# Regression: message truncation must never exceed the artifact contract's
# own MAX_COMPLIANCE_MESSAGE_CHARS ceiling. The 900-character/900-space
# malformed-version value below previously produced a ComplianceError
# message that embedded the *entire* raw value (regardless of its own
# length) via `_parse_version`'s old `{value!r}` interpolation; `_truncate`
# then appended a "... [truncated N chars]" suffix *beyond* its own
# max_chars bound, so the final "message" could exceed
# MAX_COMPLIANCE_MESSAGE_CHARS and fail ComplianceReport/ComplianceRuleResult
# construction (an "artifact_error", never raised as an exception all the
# way up through the router tool, but silently *unusable* as an artifact).
# ---------------------------------------------------------------------------


class TestMessageTruncationNeverExceedsArtifactBound:
    def test_truncate_never_exceeds_max_chars_for_a_long_string(self):
        long_value = "x" * 10_000
        truncated = c._truncate(long_value, c.MAX_MESSAGE_CHARS)
        assert len(truncated) <= c.MAX_MESSAGE_CHARS

    def test_900_space_version_error_message_stays_within_bound(self):
        # A value built almost entirely from spaces (900 of them), padded
        # around a short invalid core, so it both strips down to a short
        # (but still non-numeric, still-invalid) version string *and*
        # remains far longer in its raw form than
        # MAX_COMPLIANCE_MESSAGE_CHARS (500) -- the exact shape that
        # previously could blow the artifact's message bound via raw-value
        # echoing plus non-strict truncation.
        padded_bad_version = " " * 900 + "not-a-version" + " " * 10
        rule = _one_rule("version_gte", "8.9.0", field="firmware.version")
        observation = {"firmware": {"version": padded_bad_version}}
        result = c.evaluate_rule(observation, rule)
        assert result["status"] == "error"
        assert len(result["message"]) <= c.MAX_MESSAGE_CHARS

    def test_900_space_version_error_builds_artifact_without_artifact_error(self, tmp_path):
        padded_bad_version = " " * 900 + "not-a-version" + " " * 10
        report = c.evaluate_policy(
            observations=[{"hostname": "sw1", "firmware": {"version": padded_bad_version}}],
            policy=[
                {"field": "firmware.version", "operator": "version_gte", "expected": "8.9.0"}
            ],
        )
        assert report["counts"]["error"] == 1
        payload = c.build_compliance_report_payload(
            policy_id=report["policy_id"],
            compliant=report["compliant"],
            counts=report["counts"],
            observations=report["observations"],
            results=report["results"],
            results_total=report["results_total"],
        )
        # Must not raise ArtifactValidationError -- this is the regression
        # itself: a bounded-length "message" must always build a valid
        # artifact, never merely "close enough" to the 500-character bound.
        artifact = contracts.build_artifact(contracts.COMPLIANCE_REPORT, payload)
        assert artifact is not None
        assert artifact.kind == contracts.COMPLIANCE_REPORT

        entry = contracts.write_artifact(
            tmp_path / "compliance-900-space-version.json",
            contracts.COMPLIANCE_REPORT,
            payload,
        )
        assert entry.kind == contracts.COMPLIANCE_REPORT

    def test_900_char_message_via_router_tool_produces_no_artifact_error(self):
        # Same regression, exercised through the registered router tool
        # (mirrors src/hpe_networking_mcp/mcp_servers/tool_router.py's own artifact_error
        # degrade-gracefully contract) rather than only hpe_networking_mcp.pipeline.compliance
        # directly.
        import hpe_networking_mcp.mcp_servers.tool_router as router

        fn = getattr(router, "evaluate_compliance_policy", None)
        if fn is None:
            pytest.skip("evaluate_compliance_policy not registered (router in minimal mode)")
        target = getattr(fn, "fn", fn)
        padded_bad_version = " " * 900 + "not-a-version"
        out = target(
            observations=[{"hostname": "sw1", "firmware": {"version": padded_bad_version}}],
            policy=[
                {"field": "firmware.version", "operator": "version_gte", "expected": "8.9.0"}
            ],
        )
        assert out["ok"] is True
        assert out["artifact_error"] is None
        assert out["artifact"] is not None


# ---------------------------------------------------------------------------
# Regression: fail-closed ReDoS-safe regex subset. `re.compile` happily
# accepts every one of these; the safe-subset AST check must reject them
# all regardless, while ordinary anchored/bounded patterns keep working.
# ---------------------------------------------------------------------------


class TestSafeRegexSubset:
    @pytest.mark.parametrize(
        "pattern",
        [
            r"(a+)+b",
            r"(a*)+",
            r"([ab]+)*",
            r"(a+)*",
            r"((a+)+)+",
        ],
    )
    def test_nested_quantified_group_rejected(self, pattern):
        with pytest.raises(c.ComplianceError, match="nested quantified group"):
            c._validate_regex_pattern(pattern)

    @pytest.mark.parametrize(
        "pattern",
        [
            r"(a|a)*b",
            r"(aa|a)*b",
            r"(?:abc|def)+",
        ],
    )
    def test_branch_under_quantifier_rejected(self, pattern):
        # A simple, conservative fail-closed rule: *any* alternation
        # ("BRANCH") reachable underneath a quantifier is rejected outright
        # -- regardless of whether the specific branches are "ambiguous"
        # (e.g. "a|a", "aa|a" -- overlapping, the actual catastrophic-
        # backtracking risk) or not (e.g. "abc|def" -- disjoint prefixes).
        # This module deliberately does not attempt to distinguish the two
        # -- see the _scan_regex_ast docstring.
        with pytest.raises(c.ComplianceError, match="alternation"):
            c._validate_regex_pattern(pattern)

    def test_branch_under_quantifier_rejected_end_to_end_via_validate_policy(self):
        # Same fail-closed rule, exercised through validate_policy so an
        # ambiguous-alternation-under-quantifier pattern is rejected before
        # any observation is ever evaluated (never merely at direct
        # _validate_regex_pattern-call granularity).
        for pattern in (r"(a|a)*b", r"(aa|a)*b"):
            with pytest.raises(c.ComplianceError, match="alternation"):
                c.validate_policy(
                    [{"field": "value", "operator": "regex_fullmatch", "expected": pattern}]
                )

    @pytest.mark.parametrize("pattern", [r"(a)\1", r"(ab)(cd)\2"])
    def test_backreference_rejected(self, pattern):
        with pytest.raises(c.ComplianceError, match="backreference"):
            c._validate_regex_pattern(pattern)

    @pytest.mark.parametrize(
        "pattern",
        [r"(?=abc)def", r"(?!abc)def", r"(?<=abc)def", r"(?<!abc)def"],
    )
    def test_lookaround_rejected(self, pattern):
        with pytest.raises(c.ComplianceError, match="lookaround"):
            c._validate_regex_pattern(pattern)

    @pytest.mark.parametrize(
        "pattern",
        [
            r"^sw[0-9]+$",
            r"^[A-Z]{2}$",
            r"[A-Za-z0-9_-]+",
            r"\d{2,4}",
            r"a{0,5}",
            r".*",
            # A *single-character* alternation under a quantifier (e.g.
            # "(?:a|b)*") is optimized by the stdlib parser itself into an
            # "IN" character-class opcode rather than a "BRANCH" node, so
            # it is unaffected by the branch-under-quantifier rejection
            # above and remains part of the accepted safe subset -- unlike
            # its multi-character sibling "(?:abc|def)+" immediately above,
            # which parses to a real BRANCH node and is now rejected. It
            # also still has only a single MAX_REPEAT quantifier opcode
            # total, so it satisfies the one-quantifier-per-pattern policy
            # below too.
            r"(?:a|b)*c",
            r"(ab){2,4}",
            r"sw[0-9]+",
            # Zero quantifiers at all -- always allowed regardless of the
            # one-quantifier ceiling.
            r"^abc$",
            r"[A-Za-z]",
        ],
    )
    def test_zero_or_one_quantifier_pattern_accepted(self, pattern):
        c._validate_regex_pattern(pattern)  # must not raise

    @pytest.mark.parametrize(
        "pattern",
        [
            # Reported ReDoS findings: sibling/sequential quantifiers that
            # never nest one inside another and involve no alternation --
            # the narrower nested-quantifier/branch-under-quantifier checks
            # in `_scan_regex_ast` alone do not catch these; only the
            # blanket "at most one quantifier in the whole pattern" policy
            # does.
            r"a*a*a*a*a*a*a*a*a*b",
            r".*.*=.*",
            r"[a-z]*[a-z]*!",
            r"^\d+\.\d+\.\d+$",
            # Otherwise-ordinary-looking pattern with two independent,
            # non-nested quantifiers -- rejected under the new policy even
            # though neither `_scan_regex_ast` rule (nested quantifier,
            # branch-under-quantifier) fires on it.
            r"^[\w.-]+@[\w.-]+$",
            # Repeated overlapping character classes -- two quantifiers,
            # still rejected regardless of order/adjacency.
            r"[a-z]+[a-z]+",
            r"(ab){2,4}(cd){2,4}",
        ],
    )
    def test_sibling_or_sequential_multi_quantifier_pattern_rejected(self, pattern):
        with pytest.raises(c.ComplianceError, match="quantifier"):
            c._validate_regex_pattern(pattern)

    @pytest.mark.parametrize(
        "pattern",
        [
            r"a*a*a*a*a*a*a*a*a*b",
            r".*.*=.*",
            r"[a-z]*[a-z]*!",
            r"^\d+\.\d+\.\d+$",
        ],
    )
    def test_reported_redos_patterns_rejected_before_matching(self, pattern):
        # These must be rejected by validate_policy (and therefore never
        # reach re.fullmatch/evaluate_rule against any observation) --
        # rejection is instant static-parse-tree analysis, never a
        # thread/timeout-based mitigation that would otherwise let the
        # catastrophic backtracking start.
        with pytest.raises(c.ComplianceError, match="quantifier"):
            c.validate_policy(
                [{"field": "value", "operator": "regex_fullmatch", "expected": pattern}]
            )

    @pytest.mark.parametrize(
        "pattern",
        [
            r"a*a*",
            r"a+a+",
            r"a*b*",
            r"(a){1,2}(b){1,2}",
        ],
    )
    def test_any_two_quantifier_pattern_rejected(self, pattern):
        with pytest.raises(c.ComplianceError, match="quantifier"):
            c._validate_regex_pattern(pattern)

    def test_zero_quantifier_pattern_accepted(self):
        c._validate_regex_pattern(r"^abc$")  # must not raise

    def test_one_quantifier_pattern_accepted(self):
        c._validate_regex_pattern(r"^sw[0-9]+$")  # must not raise

    @pytest.mark.parametrize(
        "pattern",
        [
            r"(?:\b){4294967294}",
            r"(?:){4294967294}",
            r"a{501}",
            r"a{0,501}",
            r"a{501,}",
        ],
    )
    def test_oversized_or_non_consuming_repeat_rejected(self, pattern):
        with pytest.raises(c.ComplianceError, match="repeat|consume"):
            c._validate_regex_pattern(pattern)

    @pytest.mark.parametrize("pattern", [r"a{500}", r"a{0,500}", r"a*", r"(?:^a)*"])
    def test_consuming_repeat_within_subject_bound_accepted(self, pattern):
        c._validate_regex_pattern(pattern)

    def test_redos_pattern_rejected_end_to_end_via_evaluate_rule(self):
        # (a+)+b against a pathological "aaaa....c" subject would take an
        # extremely long time on stdlib re if it were ever compiled; the
        # policy must be rejected before any observation is evaluated.
        with pytest.raises(c.ComplianceError, match="nested quantified group"):
            c.validate_policy(
                [{"field": "value", "operator": "regex_fullmatch", "expected": r"(a+)+b"}]
            )

    def test_safe_regex_ast_module_missing_fails_closed(self, monkeypatch):
        monkeypatch.setattr(c, "_regex_ast_parse", None)
        with pytest.raises(c.ComplianceError, match="unavailable"):
            c._validate_regex_pattern(r"^sw[0-9]+$")


# ---------------------------------------------------------------------------
# Regression: response-amplification bounds -- policy_id/rule_id length,
# and recursive depth/collection-size/string-size/final-serialized-bytes
# bounding of "actual".
# ---------------------------------------------------------------------------


class TestAmplificationBounds:
    def test_oversized_policy_id_rejected(self):
        with pytest.raises(c.ComplianceError, match="policy_id"):
            c.evaluate_policy(
                observations=[{"a": 1}],
                policy=[{"field": "a", "operator": "eq", "expected": 1}],
                policy_id="x" * (c.MAX_POLICY_ID_CHARS + 1),
            )

    def test_empty_policy_id_rejected(self):
        with pytest.raises(c.ComplianceError, match="policy_id"):
            c.evaluate_policy(
                observations=[{"a": 1}],
                policy=[{"field": "a", "operator": "eq", "expected": 1}],
                policy_id="   ",
            )

    def test_oversized_rule_id_rejected(self):
        with pytest.raises(c.ComplianceError, match="id exceeds"):
            c.validate_policy(
                [
                    {
                        "id": "x" * (c.MAX_RULE_ID_CHARS + 1),
                        "field": "a",
                        "operator": "eq",
                        "expected": 1,
                    }
                ]
            )

    def test_bound_actual_caps_string_length(self):
        bounded = c._bound_actual("x" * 10_000)
        assert len(bounded) < 10_000
        assert "truncated" in bounded

    def test_bound_actual_caps_collection_item_count(self):
        bounded = c._bound_actual(list(range(10_000)))
        assert isinstance(bounded, list)
        assert len(bounded) <= c.MAX_ACTUAL_COLLECTION_ITEMS + 1

    def test_bound_actual_caps_recursion_depth(self):
        deeply_nested: dict = {"leaf": "value"}
        for _ in range(c.MAX_ACTUAL_DEPTH + 5):
            deeply_nested = {"nested": deeply_nested}
        bounded = c._bound_actual(deeply_nested)
        serialized = json.dumps(bounded)
        assert len(serialized) < 2_000

    def test_bound_actual_final_serialized_bytes_never_exceeds_artifact_ceiling(self):
        # A large, deeply-varied structure that recursive bounding alone
        # might not shrink enough must still fall back to the deterministic
        # truncation marker rather than exceed the artifact contract's own
        # ceiling (see hpe_networking_mcp.pipeline.artifact_contracts.MAX_COMPLIANCE_VALUE_CHARS).
        huge = [{f"field_{i}": "v" * 50 for i in range(20)} for _ in range(50)]
        bounded = c._bound_actual(huge)
        serialized_size = len(json.dumps(bounded, default=str))
        assert serialized_size <= contracts.MAX_COMPLIANCE_VALUE_CHARS * 4

    def test_bound_actual_non_json_serializable_falls_back_to_marker(self):
        class Unserializable:
            def __repr__(self):
                return "<unserializable>"

        bounded = c._bound_actual({"weird": Unserializable()})
        # default=str keeps the dict path JSON-serializable via str()
        # fallback -- this asserts the *overall* helper never raises.
        json.dumps(bounded, default=str)

    def test_policy_id_not_repeated_unbounded_in_report(self):
        report = c.evaluate_policy(
            observations=[{"a": 1}],
            policy=[{"field": "a", "operator": "eq", "expected": 1}],
            policy_id="baseline-2026",
        )
        assert report["policy_id"] == "baseline-2026"
        # policy_id appears exactly once at the top level -- never repeated
        # per rule-result entry.
        for result in report["results"]:
            assert "policy_id" not in result


# ---------------------------------------------------------------------------
# Regression: evaluator/artifact-contract mismatch -- a realistic,
# legitimately large observation (e.g. a 50-interface list) must always
# build a valid compliance_report artifact; "actual" bounding must
# conform to the artifact contract's own serialized-size ceiling exactly,
# never merely "close enough".
# ---------------------------------------------------------------------------


class TestEvaluatorArtifactSizeConformance:
    def _fifty_interface_observation(self) -> dict:
        return {
            "hostname": "sw1",
            "interfaces": [
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
            ],
        }

    def test_fifty_interface_observation_builds_artifact_without_error(self, tmp_path):
        report = c.evaluate_policy(
            observations=[self._fifty_interface_observation()],
            policy=[{"field": "interfaces", "operator": "exists"}],
        )
        payload = c.build_compliance_report_payload(
            policy_id=report["policy_id"],
            compliant=report["compliant"],
            counts=report["counts"],
            observations=report["observations"],
            results=report["results"],
            results_total=report["results_total"],
        )
        # Must not raise ArtifactValidationError -- this is the regression
        # itself: evaluator output must always be artifact-buildable for a
        # realistic, legitimately-sized observation.
        artifact = contracts.build_artifact(contracts.COMPLIANCE_REPORT, payload)
        assert artifact is not None
        assert artifact.kind == contracts.COMPLIANCE_REPORT

        entry = contracts.write_artifact(
            tmp_path / "compliance-50-iface.json", contracts.COMPLIANCE_REPORT, payload
        )
        assert entry.kind == contracts.COMPLIANCE_REPORT

    def test_bound_actual_output_always_fits_artifact_ceiling(self):
        observation = self._fifty_interface_observation()
        bounded = c._bound_actual(observation["interfaces"])
        serialized_size = len(json.dumps(bounded, default=str))
        assert serialized_size <= contracts.MAX_COMPLIANCE_VALUE_CHARS * 4
