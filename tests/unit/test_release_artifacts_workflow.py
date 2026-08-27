"""Offline structural assertions for release and deployment workflows."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-artifacts.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PAGES_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pages.yml"

_PINNED_VERSION_TAG = re.compile(r"@v\d+(\.\d+){0,2}$")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(document: dict) -> dict:
    return document.get("on", document.get(True))


def _iter_uses_steps(document: dict):
    for job in document["jobs"].values():
        for step in job.get("steps", ()):
            if "uses" in step:
                yield job, step


def _run_steps(document: dict) -> list[str]:
    return [
        step["run"]
        for job in document["jobs"].values()
        for step in job.get("steps", ())
        if "run" in step
    ]


class TestReleaseTriggerAndPermissions:
    def test_workflow_is_manual_and_version_input_is_required(self):
        document = _load(RELEASE_WORKFLOW)
        triggers = _triggers(document)

        assert set(triggers) == {"workflow_dispatch"}
        inputs = triggers["workflow_dispatch"]["inputs"]
        assert inputs["tag"]["required"] is True
        assert inputs["draft"]["type"] == "boolean"
        assert inputs["prerelease"]["type"] == "boolean"

    def test_release_concurrency_never_cancels_an_active_publish(self):
        concurrency = _load(RELEASE_WORKFLOW)["concurrency"]

        assert "inputs.tag" in concurrency["group"]
        assert concurrency["cancel-in-progress"] is False

    def test_permissions_are_read_only_except_release_job(self):
        document = _load(RELEASE_WORKFLOW)

        assert document["permissions"] == {"contents": "read"}
        job_permissions = document["jobs"]["validate-build-publish"]["permissions"]
        assert job_permissions == {
            "contents": "write",
            "id-token": "write",
            "attestations": "write",
        }


class TestActionPinning:
    def test_every_action_uses_a_stable_version_tag(self):
        for workflow in (CI_WORKFLOW, RELEASE_WORKFLOW, PAGES_WORKFLOW):
            for _job, step in _iter_uses_steps(_load(workflow)):
                uses = step["uses"]
                assert _PINNED_VERSION_TAG.search(uses), (
                    f"{workflow.name}: {uses!r} is not pinned to a stable version tag"
                )
                assert not uses.endswith(("@main", "@master", "@latest"))

    def test_current_node24_action_majors_are_used(self):
        expected = {
            "actions/attest-build-provenance": "v4",
            "actions/checkout": "v7",
            "actions/configure-pages": "v6",
            "actions/deploy-pages": "v5",
            "actions/download-artifact": "v8",
            "actions/jekyll-build-pages": "v1",
            "actions/upload-artifact": "v7",
            "actions/upload-pages-artifact": "v5",
            "astral-sh/setup-uv": "v10.0.0",
        }
        for workflow in (CI_WORKFLOW, RELEASE_WORKFLOW, PAGES_WORKFLOW):
            for _job, step in _iter_uses_steps(_load(workflow)):
                action, _, version = step["uses"].partition("@")
                if action in expected:
                    assert version == expected[action], (
                        f"{workflow.name} uses {step['uses']}; expected "
                        f"{action}@{expected[action]}"
                    )

    def test_setup_uv_cache_is_pruned(self):
        for workflow in (CI_WORKFLOW, RELEASE_WORKFLOW):
            for _job, step in _iter_uses_steps(_load(workflow)):
                if step["uses"].startswith("astral-sh/setup-uv@"):
                    assert step.get("with", {}).get("prune-cache") is True


class TestReleaseBehavior:
    def test_rebuilds_the_tool_index_and_runs_strict_validation(self):
        runs = _run_steps(_load(RELEASE_WORKFLOW))

        # The tool index and API-spec database are rebuilt from OpenAPI specs
        # committed here, so the release gate needs no downloaded bundle.
        assert any("scripts/ingest_tools.py" in run for run in runs)
        strict = next(run for run in runs if "scripts/validate_release.py" in run)
        assert "--strict-tool-index" in strict
        assert "--skip-tests" not in strict

    def test_the_spec_index_is_built_before_the_gate_that_checks_it(self):
        """Order is the whole point, not an incidental step arrangement.

        The release used to build ``data/specs.sqlite`` *after* validation,
        so every release validated a checkout with no index:
        ``indexes.offline_derivable.*`` was skipped as "not built" and the
        published endpoint/schema/field counts were never compared with the
        database the release then archived and uploaded.
        ``--strict-index-facts`` is what turns "present, so compared" into
        "required, so it cannot pass by not looking".
        """
        runs = _run_steps(_load(RELEASE_WORKFLOW))
        build = next(i for i, run in enumerate(runs) if "scripts/build_spec_index.py" in run)
        validate = next(i for i, run in enumerate(runs) if "scripts/validate_release.py" in run)

        assert build < validate, (
            "data/specs.sqlite is built after the validation meant to check it"
        )
        assert "--strict-index-facts" in runs[validate]

    def test_the_release_gate_never_types_its_own_tool_floor(self):
        """One canonical floor: docs/project-facts.json, via the script default.

        A typed value here is a second copy of a published number, and the
        two drifted -- this gate enforced 6703 while every doc promised 6711.
        """
        strict = next(
            run for run in _run_steps(_load(RELEASE_WORKFLOW))
            if "scripts/validate_release.py" in run
        )

        assert "--min-tools" not in strict

    def test_release_never_ships_the_scraped_prose_corpus(self):
        """data/docs.lance is scraped vendor documentation, not ours to publish.

        The release must neither restore an index bundle nor assert
        ``--strict-rag`` against a corpus that is deliberately absent.
        """
        runs = _run_steps(_load(RELEASE_WORKFLOW))

        for run in runs:
            assert "python scripts/download_indexes.py" not in run
            assert "--strict-rag" not in run
            assert "rag-index" not in run

    def test_builds_python_and_evidence_artifacts_and_smoke_restores(self):
        runs = _run_steps(_load(RELEASE_WORKFLOW))

        assert any("uv build" in run for run in runs)
        bundle = next(run for run in runs if "scripts/build_release_bundle.py" in run)
        assert "--no-indexes" in bundle
        assert any("scripts/restore_release_bundle.py" in run for run in runs)

    def test_attests_and_publishes_release_assets(self):
        document = _load(RELEASE_WORKFLOW)
        actions = {
            step["uses"].split("@")[0]
            for _job, step in _iter_uses_steps(document)
        }
        runs = _run_steps(document)

        assert "actions/attest-build-provenance" in actions
        assert "actions/upload-artifact" in actions
        assert any("gh release create" in run for run in runs)
        assert any("gh release upload" in run for run in runs)
        publish = next(run for run in runs if "gh release upload" in run)
        assert "dist/*.whl" in publish
        assert "sbom.json" in publish

    def test_existing_draft_is_retargeted_to_validated_commit(self):
        publish = next(
            run
            for run in _run_steps(_load(RELEASE_WORKFLOW))
            if "gh release upload" in run
        )

        assert 'gh release edit "$TAG"' in publish
        assert '--target "$GITHUB_SHA"' in publish

    def test_release_tag_must_match_pyproject_and_have_notes(self):
        runs = _run_steps(_load(RELEASE_WORKFLOW))
        validation = next(run for run in runs if "tag must start" in run)

        assert "pyproject.toml" in validation
        assert "release-notes-" in validation

    def test_workflow_contains_no_vendor_fetch_command(self):
        for run in _run_steps(_load(RELEASE_WORKFLOW)):
            assert "curl " not in run
            assert "wget " not in run
            assert "arubanetworks.com" not in run
            assert "juniper.net" not in run


class TestPagesWorkflow:
    def test_pages_build_and_deploy_are_separate(self):
        document = _load(PAGES_WORKFLOW)

        assert set(document["jobs"]) == {"build", "deploy"}
        assert document["jobs"]["deploy"]["needs"] == "build"
        assert document["jobs"]["deploy"]["permissions"] == {
            "pages": "write",
            "id-token": "write",
        }
        assert document["jobs"]["build"]["permissions"] == {
            "contents": "read",
            "pages": "read",
        }

    def test_pages_cancels_superseded_builds(self):
        concurrency = _load(PAGES_WORKFLOW)["concurrency"]

        assert concurrency["group"] == "pages"
        assert concurrency["cancel-in-progress"] is True

    def test_pages_uses_docs_as_jekyll_source(self):
        document = _load(PAGES_WORKFLOW)
        build_steps = document["jobs"]["build"]["steps"]
        jekyll = next(
            step
            for step in build_steps
            if step.get("uses", "").startswith("actions/jekyll-build-pages@")
        )

        assert jekyll["with"]["source"] == "./docs"
        assert jekyll["with"]["destination"] == "./_site"
