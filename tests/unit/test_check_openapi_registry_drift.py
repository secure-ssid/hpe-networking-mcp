"""Unit tests for scripts/check_openapi_drift.py + its readme_registry classifier.

Network is always monkeypatched. The point of these tests is the boundary
the old check did not draw: a ReadMe page that moved to a different registry
id (``pointer_change``), a page that 404s (``source_removed``), a timeout or
429 (``unavailable``), and an unparsable page/registry body
(``parser_error``) must each be distinguishable from a genuine spec hash
change (``content_drift``) -- in the report and in the exit code.
"""

from __future__ import annotations

import json
import urllib.error

from hpe_networking_mcp.pipeline import drift_taxonomy as taxonomy
from ingestion import readme_registry as rr
from scripts import check_openapi_drift as gate

_SPEC = {"openapi": "3.0.0", "info": {"title": "Security"}, "paths": {"/a": {}}}


def _entry(tmp_path=None, **overrides):
    pointer = rr.OasPointer("aruba-new-central-config", "v26.04", "abc123")
    entry = rr.build_registry_entry(
        pointer,
        _SPEC,
        source_url="https://developer.arubanetworks.com/x/reference/y",
        output_path="ingestion/sources/openapi_specs/security-abc123.json",
    )
    entry.update(overrides)
    return entry


def _manifest(tmp_path, entries):
    path = tmp_path / "openapi_registry_manifest.json"
    path.write_text(
        json.dumps({"generated_at": None, "registries": {e["registry_id"]: e for e in entries}}),
        encoding="utf-8",
    )
    return path


class TestEntryClassification:
    def test_identical_spec_is_fresh(self, monkeypatch):
        pointer = rr.OasPointer("p", "v", "abc123")
        monkeypatch.setattr(rr, "fetch_spec_for_page", lambda url, **k: (pointer, _SPEC))

        result = rr.check_entry_drift(_entry())

        assert result.result_class == taxonomy.FRESH
        assert result.status == "unchanged"

    def test_changed_hash_is_content_drift(self, monkeypatch):
        pointer = rr.OasPointer("p", "v", "abc123")
        changed = {**_SPEC, "paths": {"/a": {}, "/b": {}}}
        monkeypatch.setattr(rr, "fetch_spec_for_page", lambda url, **k: (pointer, changed))

        result = rr.check_entry_drift(_entry())

        assert result.result_class == taxonomy.CONTENT_DRIFT
        assert result.observed_sha256 == rr.spec_fingerprint(changed)

    def test_registry_id_move_is_pointer_change_not_content_drift(self, monkeypatch):
        moved = rr.OasPointer("p", "v", "zzz999")
        monkeypatch.setattr(rr, "fetch_spec_for_page", lambda url, **k: (moved, _SPEC))

        result = rr.check_entry_drift(_entry())

        assert result.result_class == taxonomy.POINTER_CHANGE
        assert result.result_class != taxonomy.CONTENT_DRIFT
        assert result.observed_registry_id == "zzz999"

    def test_missing_page_is_source_removed(self, monkeypatch):
        def _gone(url, **k):
            raise rr.RegistryMissingError("reference page is gone (HTTP 404)")

        monkeypatch.setattr(rr, "fetch_spec_for_page", _gone)

        assert rr.check_entry_drift(_entry()).result_class == taxonomy.SOURCE_REMOVED

    def test_transient_failure_is_unavailable(self, monkeypatch):
        def _flaky(url, **k):
            raise rr.RegistryUnavailableError("failed to fetch: HTTP Error 503")

        monkeypatch.setattr(rr, "fetch_spec_for_page", _flaky)
        monkeypatch.setattr(rr.time, "sleep", lambda _s: None)

        result = rr.check_entry_drift(_entry())

        assert result.result_class == taxonomy.UNAVAILABLE
        assert result.result_class != taxonomy.CONTENT_DRIFT

    def test_unparsable_page_is_parser_error(self, monkeypatch):
        def _unparsable(url, **k):
            raise rr.RegistryParseError("no oasPublicUrl pointer found in page HTML")

        monkeypatch.setattr(rr, "fetch_spec_for_page", _unparsable)

        assert rr.check_entry_drift(_entry()).result_class == taxonomy.PARSER_ERROR

    def test_offline_reports_not_checked_never_fresh(self):
        result = rr.check_entry_drift(_entry(), offline=True)

        assert result.result_class == taxonomy.NOT_CHECKED
        assert result.result_class != taxonomy.FRESH

    def test_transport_errors_are_classified_by_status(self):
        http404 = urllib.error.HTTPError("u", 404, "gone", None, None)
        http503 = urllib.error.HTTPError("u", 503, "busy", None, None)

        assert isinstance(rr.classify_transport_error(http404, "u"), rr.RegistryMissingError)
        assert isinstance(rr.classify_transport_error(http503, "u"), rr.RegistryUnavailableError)


class TestLocalSpecInventory:
    def test_absent_git_ignored_spec_is_not_checked(self, tmp_path):
        findings = gate.local_spec_findings(
            {"abc123": _entry()}, spec_dir=tmp_path, require_local_specs=False
        )

        assert [f.result_class for f in findings] == [taxonomy.NOT_CHECKED]

    def test_absent_spec_is_source_removed_when_required(self, tmp_path):
        findings = gate.local_spec_findings(
            {"abc123": _entry()}, spec_dir=tmp_path, require_local_specs=True
        )

        assert [f.result_class for f in findings] == [taxonomy.SOURCE_REMOVED]

    def test_undeclared_spec_on_disk_is_source_added(self, tmp_path):
        (tmp_path / "surprise.json").write_text("{}", encoding="utf-8")

        findings = gate.local_spec_findings({}, spec_dir=tmp_path, require_local_specs=False)

        assert [f.result_class for f in findings] == [taxonomy.SOURCE_ADDED]

    def test_entry_without_output_path_is_parser_error(self, tmp_path):
        entry = _entry()
        entry.pop("output_path")

        findings = gate.local_spec_findings(
            {"abc123": entry}, spec_dir=tmp_path, require_local_specs=False
        )

        assert findings[0].result_class == taxonomy.PARSER_ERROR


class TestMain:
    def test_empty_manifest_exits_usage(self, tmp_path, capsys):
        path = tmp_path / "m.json"
        path.write_text(json.dumps({"registries": {}}), encoding="utf-8")

        assert gate.main(["--manifest", str(path), "--no-artifact"]) == taxonomy.EXIT_USAGE

    def test_offline_run_writes_artifact_and_exits_zero(self, tmp_path):
        manifest = _manifest(tmp_path, [_entry()])
        artifact = tmp_path / "drift.json"

        exit_code = gate.main(
            [
                "--manifest",
                str(manifest),
                "--offline",
                "--spec-dir",
                str(tmp_path / "specs"),
                "--json-artifact",
                str(artifact),
            ]
        )

        assert exit_code == 0
        report = json.loads(artifact.read_text())
        assert report["refresh_sources"] is False
        assert report["counts"][taxonomy.NOT_CHECKED] >= 1
        assert report["counts"][taxonomy.FRESH] == 0

    def test_transport_failure_exits_unavailable_not_drift(self, tmp_path, monkeypatch):
        manifest = _manifest(tmp_path, [_entry()])

        def _flaky(url, **k):
            raise rr.RegistryUnavailableError("failed to fetch: network error")

        monkeypatch.setattr(rr, "fetch_spec_for_page", _flaky)

        exit_code = gate.main(
            [
                "--manifest",
                str(manifest),
                "--spec-dir",
                str(tmp_path / "specs"),
                "--no-artifact",
            ]
        )

        assert exit_code == taxonomy.EXIT_UNAVAILABLE
        assert exit_code != taxonomy.EXIT_CONTENT_DRIFT

    def test_pointer_change_exits_with_its_own_code(self, tmp_path, monkeypatch):
        manifest = _manifest(tmp_path, [_entry()])
        moved = rr.OasPointer("p", "v", "zzz999")
        monkeypatch.setattr(rr, "fetch_spec_for_page", lambda url, **k: (moved, _SPEC))

        exit_code = gate.main(
            [
                "--manifest",
                str(manifest),
                "--spec-dir",
                str(tmp_path / "specs"),
                "--no-artifact",
            ]
        )

        assert exit_code == taxonomy.EXIT_POINTER_CHANGE
