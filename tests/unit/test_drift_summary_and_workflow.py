"""Offline tests for the drift aggregation script and the CI drift jobs.

Two halves:

1. ``scripts/summarize_drift_artifacts.py`` -- rolls per-check reports up
   into one summary, surfaces a check that produced *no* artifact instead of
   silently omitting it, and never lets a transport failure look like content
   drift in the aggregate.
2. ``.github/workflows/ci.yml`` -- parsed with PyYAML, never executed. The
   requirement being pinned: each drift check is its own job (so one failure
   cannot hide a later check), each uploads its own JSON artifact, no job
   chains unrelated checks in one shell block, and the strict
   release/index/test gate stays a separate job.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from hpe_networking_mcp.pipeline import drift_taxonomy as taxonomy
from scripts import summarize_drift_artifacts as summarizer

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

DRIFT_CHECK_SCRIPTS = (
    "scripts/check_openapi_drift.py",
    "scripts/check_mist_openapi_drift.py",
    "scripts/check_nowireless_source_drift.py",
    "scripts/check_product_spec_freshness.py",
    "scripts/check_security_lifecycle_drift.py",
    "ingestion/check_updates.py",
)


def _report(check, *classes, refresh=True):
    findings = [taxonomy.Finding(target=f"t{i}", result_class=c) for i, c in enumerate(classes)]
    return taxonomy.build_report(check, findings, refresh_sources=refresh)


def _write(tmp_path, name, report):
    path = tmp_path / name
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


class TestSummarizer:
    def test_rolls_up_every_check(self, tmp_path):
        _write(tmp_path, "a.json", _report("openapi_registry_drift", taxonomy.FRESH))
        _write(tmp_path, "b.json", _report("mist_openapi_drift", taxonomy.STALE_PIN))

        reports, unreadable = summarizer.load_reports(tmp_path)
        summary = summarizer.build_summary(reports, unreadable=unreadable)

        assert {c["check"] for c in summary["checks"]} == {
            "openapi_registry_drift",
            "mist_openapi_drift",
        }
        assert summary["totals"][taxonomy.STALE_PIN] == 1
        assert summary["failing_checks"] == ["mist_openapi_drift"]

    def test_missing_artifact_is_surfaced_not_dropped(self, tmp_path):
        _write(tmp_path, "a.json", _report("openapi_registry_drift", taxonomy.FRESH))

        reports, unreadable = summarizer.load_reports(tmp_path)
        summary = summarizer.build_summary(reports, unreadable=unreadable)

        assert "mist_openapi_drift" in summary["missing_artifact"]
        assert "product_spec_freshness" in summary["missing_artifact"]

    def test_unreadable_artifact_is_surfaced(self, tmp_path):
        (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")

        reports, unreadable = summarizer.load_reports(tmp_path)

        assert reports == []
        assert unreadable and "broken.json" in unreadable[0]

    def test_incomplete_check_does_not_read_as_content_drift(self, tmp_path):
        _write(tmp_path, "a.json", _report("rag_source_freshness", taxonomy.UNAVAILABLE))

        reports, unreadable = summarizer.load_reports(tmp_path)
        summary = summarizer.build_summary(reports, unreadable=unreadable, expected=())

        assert summary["content_drift_detected"] is False
        assert summary["check_incomplete"] is True
        assert summarizer.summary_exit_code(summary) == taxonomy.EXIT_UNAVAILABLE

    def test_exit_code_uses_taxonomy_precedence_across_checks(self, tmp_path):
        _write(tmp_path, "a.json", _report("c1", taxonomy.CONTENT_DRIFT))
        _write(tmp_path, "b.json", _report("c2", taxonomy.PARSER_ERROR))

        reports, unreadable = summarizer.load_reports(tmp_path)
        summary = summarizer.build_summary(reports, unreadable=unreadable, expected=())

        assert summarizer.summary_exit_code(summary) == taxonomy.EXIT_PARSER_ERROR

    def test_legacy_mode_collapses_to_one(self, tmp_path):
        _write(tmp_path, "a.json", _report("c1", taxonomy.CONTENT_DRIFT))

        reports, unreadable = summarizer.load_reports(tmp_path)
        summary = summarizer.build_summary(reports, unreadable=unreadable, expected=())

        assert summarizer.summary_exit_code(summary, mode="legacy") == 1

    def test_main_writes_summary_and_markdown(self, tmp_path):
        _write(tmp_path, "a.json", _report("c1", taxonomy.FRESH))
        out = tmp_path / "summary.json"
        md = tmp_path / "summary.md"

        exit_code = summarizer.main(
            [
                "--input-dir",
                str(tmp_path),
                "--output",
                str(out),
                "--markdown",
                str(md),
                "--expected",
                "c1",
            ]
        )

        assert exit_code == 0
        assert json.loads(out.read_text())["checks"][0]["check"] == "c1"
        assert "Source/API/RAG drift summary" in md.read_text()

    def test_main_never_fail_still_reports(self, tmp_path):
        _write(tmp_path, "a.json", _report("c1", taxonomy.CONTENT_DRIFT))

        assert (
            summarizer.main(
                [
                    "--input-dir",
                    str(tmp_path),
                    "--output",
                    str(tmp_path / "s.json"),
                    "--never-fail",
                ]
            )
            == 0
        )


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))


def _run_steps(job: dict) -> list[str]:
    return [step.get("run", "") for step in job.get("steps", ()) if "run" in step]


class TestDriftJobsAreIndependent:
    def test_each_drift_check_has_its_own_job(self, workflow):
        jobs = workflow["jobs"]
        for script in DRIFT_CHECK_SCRIPTS:
            owning = [
                name
                for name, job in jobs.items()
                if any(script in run for run in _run_steps(job))
            ]
            assert len(owning) == 1, f"{script} should belong to exactly one job, got {owning}"

    def test_no_job_chains_two_unrelated_drift_checks(self, workflow):
        for name, job in workflow["jobs"].items():
            for run in _run_steps(job):
                hits = [script for script in DRIFT_CHECK_SCRIPTS if script in run]
                assert len(hits) <= 1, (
                    f"job {name} runs {hits} in one shell block; a failing first check "
                    "would hide the rest"
                )

    def test_every_drift_job_uploads_a_json_artifact(self, workflow):
        jobs = workflow["jobs"]
        drift_jobs = {
            name
            for name, job in jobs.items()
            if any(any(s in run for s in DRIFT_CHECK_SCRIPTS) for run in _run_steps(job))
        }
        assert drift_jobs
        for name in drift_jobs:
            uploads = [
                step
                for step in jobs[name]["steps"]
                if step.get("uses", "").startswith("actions/upload-artifact@")
            ]
            assert uploads, f"job {name} uploads no drift artifact"
            assert all(step.get("if") == "always()" for step in uploads), (
                f"job {name} must upload its artifact even when the check fails"
            )

    def test_drift_checks_write_machine_readable_reports(self, workflow):
        runs = [run for job in workflow["jobs"].values() for run in _run_steps(job)]
        artifact_runs = [
            run
            for run in runs
            if "--json-artifact" in run or "--drift-artifact-path" in run
        ]
        assert len(artifact_runs) >= 5

    def test_summary_job_aggregates_and_always_runs(self, workflow):
        summary = workflow["jobs"]["drift-summary"]

        assert summary["if"] == "always()"
        assert any("summarize_drift_artifacts.py" in run for run in _run_steps(summary))
        assert any(
            step.get("uses", "").startswith("actions/download-artifact@")
            for step in summary["steps"]
        )
        expected_needs = {
            "product-spec-freshness",
            "rag-source-drift",
            "openapi-registry-drift",
            "mist-openapi-drift",
            "nowireless-community-drift",
            "security-lifecycle-drift",
        }
        assert expected_needs <= set(summary["needs"])

    def test_test_and_strict_index_gates_stay_separate(self, workflow):
        test_job = workflow["jobs"]["test"]
        test_runs = _run_steps(test_job)
        strict_runs = _run_steps(workflow["jobs"]["strict-index"])

        assert any("scripts/validate_release.py" in run for run in test_runs)
        assert any(
            "scripts/download_indexes.py" in run
            and "--manifest .github/index-bundle.json" in run
            for run in strict_runs
        )
        strict = next(run for run in strict_runs if "scripts/validate_release.py" in run)
        assert "--strict-rag" in strict
        assert "--strict-tool-index" in strict
        for run in (*test_runs, *strict_runs):
            assert not any(script in run for script in DRIFT_CHECK_SCRIPTS)

    def test_package_job_builds_and_smoke_installs_distributions(self, workflow):
        runs = _run_steps(workflow["jobs"]["package"])

        assert any("uv build" in run for run in runs)
        smoke = next(run for run in runs if "hpe-mcp-doctor --help" in run)
        assert "hpe-mcp-router --help" in smoke
        assert "hpe-mcp-run-pipeline --help" in smoke
        assert "hpe-mcp-run-ssid --help" in smoke

    def test_workflow_has_canceling_concurrency(self, workflow):
        concurrency = workflow["concurrency"]

        assert concurrency["cancel-in-progress"] is True
        assert "github.ref" in concurrency["group"]

    def test_rag_source_job_is_offline(self, workflow):
        runs = _run_steps(workflow["jobs"]["rag-source-drift"])

        assert any("--offline" in run for run in runs)
        assert any("--plan" in run for run in runs)

    def test_workflow_permissions_are_read_only(self, workflow):
        assert workflow["permissions"] == {"contents": "read"}
