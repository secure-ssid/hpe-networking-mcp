"""Unit tests for the shared drift result taxonomy.

The taxonomy exists so a transient network failure, a parser break, a
pointer/layout move, an unverified reviewed pin, and a real content change
can never be confused with one another -- in a report or in an exit code.
These tests pin exactly that: class membership, per-class exit codes, the
precedence used when several classes appear in one run, and the invariant
that ``content_drift_detected`` is false whenever the only findings are
transport/parser failures.
"""

from __future__ import annotations

import json

import pytest

from hpe_networking_mcp.pipeline import drift_taxonomy as taxonomy


def _f(result_class: str, target: str = "t") -> taxonomy.Finding:
    return taxonomy.Finding(target=target, result_class=result_class, detail="d")


class TestClasses:
    def test_required_classes_are_all_declared(self):
        required = {
            taxonomy.FRESH,
            taxonomy.CONTENT_DRIFT,
            taxonomy.SOURCE_ADDED,
            taxonomy.SOURCE_REMOVED,
            taxonomy.POINTER_CHANGE,
            taxonomy.STALE_PIN,
            taxonomy.UNAVAILABLE,
            taxonomy.PARSER_ERROR,
            taxonomy.COVERAGE_GAP,
        }
        assert required <= set(taxonomy.RESULT_CLASSES)

    def test_expected_states_never_fail_a_gate(self):
        assert taxonomy.PASSING_CLASSES == {
            taxonomy.FRESH,
            taxonomy.COVERAGE_GAP,
            taxonomy.NOT_CHECKED,
        }
        for name in taxonomy.PASSING_CLASSES:
            assert taxonomy.EXIT_CODES[name] == taxonomy.EXIT_OK
            assert not _f(name).failing

    def test_transport_and_parser_failures_are_not_content_classes(self):
        assert taxonomy.UNAVAILABLE not in taxonomy.CONTENT_CLASSES
        assert taxonomy.PARSER_ERROR not in taxonomy.CONTENT_CLASSES
        assert taxonomy.STALE_PIN not in taxonomy.CONTENT_CLASSES
        assert taxonomy.POINTER_CHANGE not in taxonomy.CONTENT_CLASSES

    def test_unknown_class_is_rejected_loudly(self):
        with pytest.raises(taxonomy.DriftTaxonomyError):
            taxonomy.Finding(target="t", result_class="probably_fine")


class TestExitCodes:
    def test_failing_classes_do_not_share_exit_codes_across_concerns(self):
        """Only source_added/source_removed intentionally share a code
        (both mean "the tracked set changed"); every other failing class is
        distinguishable by exit code alone."""
        failing = [c for c in taxonomy.RESULT_CLASSES if c not in taxonomy.PASSING_CLASSES]
        codes = {name: taxonomy.EXIT_CODES[name] for name in failing}
        assert codes[taxonomy.SOURCE_ADDED] == codes[taxonomy.SOURCE_REMOVED]
        distinct = {name: code for name, code in codes.items() if name != taxonomy.SOURCE_REMOVED}
        assert len(set(distinct.values())) == len(distinct)
        assert taxonomy.EXIT_OK not in set(codes.values())
        assert taxonomy.EXIT_CODES[taxonomy.CONTENT_DRIFT] != taxonomy.EXIT_CODES[
            taxonomy.UNAVAILABLE
        ]
        assert taxonomy.EXIT_CODES[taxonomy.CONTENT_DRIFT] != taxonomy.EXIT_CODES[
            taxonomy.PARSER_ERROR
        ]
        assert taxonomy.EXIT_CODES[taxonomy.STALE_PIN] != taxonomy.EXIT_CODES[taxonomy.FRESH]

    def test_all_passing_findings_exit_zero(self):
        findings = [_f(taxonomy.FRESH), _f(taxonomy.COVERAGE_GAP), _f(taxonomy.NOT_CHECKED)]
        assert taxonomy.exit_code_for(findings) == 0

    @pytest.mark.parametrize(
        "result_class,expected",
        [
            (taxonomy.CONTENT_DRIFT, taxonomy.EXIT_CONTENT_DRIFT),
            (taxonomy.SOURCE_ADDED, taxonomy.EXIT_SOURCE_SET_CHANGED),
            (taxonomy.SOURCE_REMOVED, taxonomy.EXIT_SOURCE_SET_CHANGED),
            (taxonomy.POINTER_CHANGE, taxonomy.EXIT_POINTER_CHANGE),
            (taxonomy.STALE_PIN, taxonomy.EXIT_STALE_PIN),
            (taxonomy.UNAVAILABLE, taxonomy.EXIT_UNAVAILABLE),
            (taxonomy.PARSER_ERROR, taxonomy.EXIT_PARSER_ERROR),
        ],
    )
    def test_single_class_maps_to_its_own_code(self, result_class, expected):
        assert taxonomy.exit_code_for([_f(result_class)]) == expected

    def test_incomplete_check_outranks_content_drift(self):
        """A run that could not complete must not exit as confirmed drift."""
        findings = [_f(taxonomy.CONTENT_DRIFT), _f(taxonomy.UNAVAILABLE)]
        assert taxonomy.exit_code_for(findings) == taxonomy.EXIT_UNAVAILABLE

        findings = [_f(taxonomy.CONTENT_DRIFT), _f(taxonomy.PARSER_ERROR)]
        assert taxonomy.exit_code_for(findings) == taxonomy.EXIT_PARSER_ERROR

    def test_parser_error_outranks_unavailable(self):
        findings = [_f(taxonomy.UNAVAILABLE), _f(taxonomy.PARSER_ERROR)]
        assert taxonomy.dominant_class(findings) == taxonomy.PARSER_ERROR

    def test_stale_pin_never_masks_real_drift(self):
        findings = [_f(taxonomy.STALE_PIN), _f(taxonomy.CONTENT_DRIFT)]
        assert taxonomy.exit_code_for(findings) == taxonomy.EXIT_CONTENT_DRIFT

    def test_legacy_mode_collapses_every_failure_to_one(self):
        for name in taxonomy.RESULT_CLASSES:
            expected = 0 if name in taxonomy.PASSING_CLASSES else 1
            assert taxonomy.exit_code_for([_f(name)], mode="legacy") == expected

    def test_unknown_mode_rejected(self):
        with pytest.raises(taxonomy.DriftTaxonomyError):
            taxonomy.exit_code_for([], mode="quiet")


class TestReport:
    def test_report_records_all_classes_and_refresh_state(self):
        findings = [_f(taxonomy.FRESH, "a"), _f(taxonomy.UNAVAILABLE, "b")]
        report = taxonomy.build_report("demo", findings, refresh_sources=False)

        assert report["check"] == "demo"
        assert report["refresh_sources"] is False
        assert report["counts"][taxonomy.FRESH] == 1
        assert report["counts"][taxonomy.UNAVAILABLE] == 1
        assert set(report["counts"]) == set(taxonomy.RESULT_CLASSES)
        assert report["dominant_class"] == taxonomy.UNAVAILABLE
        assert report["exit_code"] == taxonomy.EXIT_UNAVAILABLE

    def test_network_failure_is_not_content_drift_in_the_report(self):
        report = taxonomy.build_report(
            "demo", [_f(taxonomy.UNAVAILABLE), _f(taxonomy.PARSER_ERROR)]
        )
        assert report["content_drift_detected"] is False
        assert report["check_incomplete"] is True

    def test_content_drift_flag_set_for_added_and_removed_sources(self):
        for name in (taxonomy.CONTENT_DRIFT, taxonomy.SOURCE_ADDED, taxonomy.SOURCE_REMOVED):
            report = taxonomy.build_report("demo", [_f(name)])
            assert report["content_drift_detected"] is True
            assert report["check_incomplete"] is False

    def test_details_and_evidence_are_bounded(self):
        finding = taxonomy.Finding(
            target="t",
            result_class=taxonomy.FRESH,
            detail="x" * 5000,
            evidence={"blob": "y" * 5000, "items": list(range(100))},
        )
        payload = finding.to_dict()
        assert len(payload["detail"]) <= taxonomy.MAX_DETAIL_CHARS
        assert len(payload["evidence"]["blob"]) <= taxonomy.MAX_DETAIL_CHARS
        assert len(payload["evidence"]["items"]) <= 25

    def test_write_report_is_atomic_json(self, tmp_path):
        report = taxonomy.build_report("demo", [_f(taxonomy.FRESH)])
        path = taxonomy.write_report(tmp_path / "nested" / "r.json", report)

        assert path.is_file()
        assert json.loads(path.read_text())["check"] == "demo"
        assert not list(tmp_path.glob("**/*.tmp"))


class TestSummary:
    def test_summary_rolls_up_classes_and_failing_checks(self):
        a = taxonomy.build_report("a", [_f(taxonomy.FRESH)])
        b = taxonomy.build_report("b", [_f(taxonomy.CONTENT_DRIFT), _f(taxonomy.FRESH)])
        c = taxonomy.build_report("c", [_f(taxonomy.UNAVAILABLE)])

        summary = taxonomy.summarize_reports([a, b, c])

        assert summary["totals"][taxonomy.FRESH] == 2
        assert summary["totals"][taxonomy.CONTENT_DRIFT] == 1
        assert summary["failing_checks"] == ["b", "c"]
        assert summary["content_drift_detected"] is True
        assert summary["check_incomplete"] is True


class TestSummaryClassLabel:
    def test_all_not_checked_run_is_never_labelled_fresh(self):
        report = taxonomy.build_report(
            "demo", [_f(taxonomy.NOT_CHECKED), _f(taxonomy.NOT_CHECKED)]
        )
        summary = taxonomy.summarize_reports([report])

        assert summary["checks"][0]["summary_class"] == taxonomy.NOT_CHECKED

    def test_fresh_run_is_labelled_fresh(self):
        report = taxonomy.build_report("demo", [_f(taxonomy.FRESH)])
        summary = taxonomy.summarize_reports([report])

        assert summary["checks"][0]["summary_class"] == taxonomy.FRESH

    def test_failing_run_is_labelled_with_its_dominant_class(self):
        report = taxonomy.build_report("demo", [_f(taxonomy.FRESH), _f(taxonomy.PARSER_ERROR)])
        summary = taxonomy.summarize_reports([report])

        assert summary["checks"][0]["summary_class"] == taxonomy.PARSER_ERROR

    def test_mixed_fresh_and_not_checked_reports_the_weaker_claim(self):
        report = taxonomy.build_report("demo", [_f(taxonomy.FRESH), _f(taxonomy.NOT_CHECKED)])
        summary = taxonomy.summarize_reports([report])

        assert summary["checks"][0]["summary_class"] == taxonomy.NOT_CHECKED
