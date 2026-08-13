"""Unit tests for scripts/check_product_spec_freshness.py.

Everything here is offline by construction: the gate only reads
``ingestion/product_specs_manifest.json`` plus whatever spec files exist on
disk. Covers the committed manifest passing its own locally-derivable
checks, and each failure class in isolation -- branch/spec_uri disagreement
and sidebar-membership loss as ``pointer_change``, path_count/digest
divergence as ``content_drift``, undeclared files as ``source_added``,
missing declared files as ``not_checked``/``source_removed``, and manifest
corruption as ``parser_error`` (never as drift).
"""

from __future__ import annotations

import hashlib
import json

from hpe_networking_mcp.pipeline import drift_taxonomy as taxonomy
from scripts import check_product_spec_freshness as psf


def _entry(**overrides):
    entry = {
        "branch": "10.04",
        "output_path": "ingestion/sources/product_specs/aoscx-arubaos-cx-rest-api.json",
        "path_count": 2,
        "project": "aruba-aoscx",
        "section": "aoscx",
        "source_url": "https://developer.arubanetworks.com/aoscx/reference/x",
        "spec_uri": "/branches/10.04/apis/arubaos-cx-rest-api.json",
        "title": "ArubaOS-CX REST API",
    }
    entry.update(overrides)
    return entry


def _write_spec(root, entry, path_count=2, extra=None):
    path = root / entry["output_path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    spec = {"paths": {f"/p{i}": {} for i in range(path_count)}}
    if extra:
        spec.update(extra)
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def _evaluate(root, entry, **kwargs):
    return psf.evaluate_entry(
        entry,
        repo_root=root,
        spec_dir=root / "ingestion" / "sources" / "product_specs",
        require_local_specs=kwargs.get("require_local_specs", False),
    )


class TestCommittedManifest:
    def test_repo_manifest_passes_every_locally_derivable_check(self):
        findings = psf.evaluate()
        bad = [f for f in findings if f.result_class not in (taxonomy.FRESH, taxonomy.NOT_CHECKED)]

        assert bad == [], [f"{f.target}: {f.result_class} {f.detail}" for f in bad]

    def test_repo_manifest_declares_every_known_sidebar_section(self):
        entries = psf.load_manifest()
        sections = {entry["section"] for entry in entries}

        from ingestion.scrape_apinext_specs import PROJECTS

        assert sections <= set(PROJECTS)


class TestPointerAndLayout:
    def test_branch_mismatch_is_pointer_change(self, tmp_path):
        finding = _evaluate(tmp_path, _entry(branch="10.05"))

        assert finding.result_class == taxonomy.POINTER_CHANGE
        assert "branch mismatch" in finding.detail

    def test_unknown_section_is_pointer_change(self, tmp_path):
        finding = _evaluate(tmp_path, _entry(section="retired-product"))

        assert finding.result_class == taxonomy.POINTER_CHANGE
        assert "sidebar section" in finding.detail

    def test_project_renamed_under_a_section_is_pointer_change(self, tmp_path):
        finding = _evaluate(tmp_path, _entry(project="aruba-something-else"))

        assert finding.result_class == taxonomy.POINTER_CHANGE

    def test_malformed_spec_uri_is_pointer_change(self, tmp_path):
        finding = _evaluate(tmp_path, _entry(spec_uri="/apis/whatever.json"))

        assert finding.result_class == taxonomy.POINTER_CHANGE

    def test_output_path_off_convention_is_pointer_change(self, tmp_path):
        finding = _evaluate(
            tmp_path, _entry(output_path="ingestion/sources/product_specs/renamed.json")
        )

        assert finding.result_class == taxonomy.POINTER_CHANGE
        assert "convention" in finding.detail


class TestDigestAndPathCount:
    def test_matching_path_count_is_fresh(self, tmp_path):
        entry = _entry()
        _write_spec(tmp_path, entry, path_count=2)

        finding = _evaluate(tmp_path, entry)

        assert finding.result_class == taxonomy.FRESH
        assert finding.evidence["digest_baseline_recorded"] is False

    def test_path_count_divergence_is_content_drift(self, tmp_path):
        entry = _entry(path_count=2)
        _write_spec(tmp_path, entry, path_count=7)

        finding = _evaluate(tmp_path, entry)

        assert finding.result_class == taxonomy.CONTENT_DRIFT
        assert finding.evidence["observed_path_count"] == 7

    def test_recorded_digest_mismatch_is_content_drift(self, tmp_path):
        entry = _entry(sha256="0" * 64)
        _write_spec(tmp_path, entry, path_count=2)

        finding = _evaluate(tmp_path, entry)

        assert finding.result_class == taxonomy.CONTENT_DRIFT

    def test_recorded_digest_match_is_fresh(self, tmp_path):
        entry = _entry()
        path = _write_spec(tmp_path, entry, path_count=2)
        entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()

        finding = _evaluate(tmp_path, entry)

        assert finding.result_class == taxonomy.FRESH
        assert finding.evidence["digest_baseline_recorded"] is True

    def test_unparsable_local_spec_is_parser_error_not_drift(self, tmp_path):
        entry = _entry()
        path = tmp_path / entry["output_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        finding = _evaluate(tmp_path, entry)

        assert finding.result_class == taxonomy.PARSER_ERROR
        assert finding.result_class != taxonomy.CONTENT_DRIFT


class TestPresenceOfFiles:
    def test_absent_git_ignored_spec_is_not_checked(self, tmp_path):
        finding = _evaluate(tmp_path, _entry())

        assert finding.result_class == taxonomy.NOT_CHECKED

    def test_absent_spec_is_source_removed_when_required(self, tmp_path):
        finding = _evaluate(tmp_path, _entry(), require_local_specs=True)

        assert finding.result_class == taxonomy.SOURCE_REMOVED

    def test_undeclared_spec_file_is_source_added(self, tmp_path):
        spec_dir = tmp_path / "ingestion" / "sources" / "product_specs"
        spec_dir.mkdir(parents=True)
        (spec_dir / "surprise.json").write_text("{}", encoding="utf-8")

        findings = psf.undeclared_spec_findings([_entry()], repo_root=tmp_path, spec_dir=spec_dir)

        assert [f.result_class for f in findings] == [taxonomy.SOURCE_ADDED]
        assert findings[0].target == "surprise.json"


class TestManifestIntegrity:
    def test_missing_required_key_is_parser_error(self, tmp_path):
        entry = _entry()
        del entry["branch"]

        finding = _evaluate(tmp_path, entry)

        assert finding.result_class == taxonomy.PARSER_ERROR

    def test_duplicate_spec_uri_is_parser_error(self):
        findings = psf.duplicate_findings([_entry(), _entry()])

        assert findings
        assert all(f.result_class == taxonomy.PARSER_ERROR for f in findings)

    def test_corrupt_manifest_is_parser_error_not_drift(self, tmp_path):
        manifest = tmp_path / "product_specs_manifest.json"
        manifest.write_text("{not json", encoding="utf-8")

        findings = psf.evaluate(manifest_path=manifest, spec_dir=tmp_path, repo_root=tmp_path)

        assert [f.result_class for f in findings] == [taxonomy.PARSER_ERROR]

    def test_missing_manifest_is_parser_error(self, tmp_path):
        findings = psf.evaluate(
            manifest_path=tmp_path / "nope.json", spec_dir=tmp_path, repo_root=tmp_path
        )
        assert findings[0].result_class == taxonomy.PARSER_ERROR


class TestMain:
    def test_main_writes_artifact_and_exits_zero_for_committed_manifest(self, tmp_path):
        artifact = tmp_path / "product.json"

        exit_code = psf.main(["--json-artifact", str(artifact)])

        assert exit_code == 0
        report = json.loads(artifact.read_text())
        assert report["check"] == "product_spec_freshness"
        assert report["refresh_sources"] is False
        assert report["counts"][taxonomy.CONTENT_DRIFT] == 0

    def test_main_exit_code_is_classified(self, tmp_path, monkeypatch):
        manifest = tmp_path / "m.json"
        manifest.write_text(json.dumps({"specs": [_entry(branch="9.9")]}), encoding="utf-8")

        exit_code = psf.main(
            [
                "--manifest",
                str(manifest),
                "--spec-dir",
                str(tmp_path / "specs"),
                "--repo-root",
                str(tmp_path),
                "--no-artifact",
            ]
        )

        assert exit_code == taxonomy.EXIT_POINTER_CHANGE
