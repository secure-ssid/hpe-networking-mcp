"""Unit tests for hpe_networking_mcp.pipeline.sbom -- deterministic CycloneDX SBOM generation.

Covers:
- A minimal, valid CycloneDX 1.5 JSON document is produced from a small
  synthetic ``uv.lock`` + ``pyproject.toml`` pair (never the real,
  hundred-plus-package project lockfile, to keep this test fast and
  independent of the project's actual dependency graph).
- Determinism: two builds against byte-identical inputs (same
  ``generated_at``) produce byte-identical JSON.
- Components are sorted by (name, version).
- The root project package and any ``virtual``-sourced entries are
  excluded from ``components`` (they describe the project itself, not a
  third-party dependency).
- Missing/malformed inputs raise ``SbomError`` rather than emitting a
  partial SBOM.
- The component-count safety bound is enforced.
"""

from __future__ import annotations

import json

import pytest

from hpe_networking_mcp.pipeline import sbom

GENERATED_AT = "2026-07-25T12:00:00+00:00"


def _write_project(tmp_path, *, name="demo-project", version="1.2.3"):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n', encoding="utf-8"
    )
    return pyproject


def _write_lockfile(tmp_path, packages_toml: str):
    lockfile = tmp_path / "uv.lock"
    lockfile.write_text(packages_toml, encoding="utf-8")
    return lockfile


BASIC_LOCKFILE = """
[[package]]
name = "demo-project"
version = "1.2.3"
source = { virtual = "." }

[[package]]
name = "requests"
version = "2.31.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "pyyaml"
version = "6.0.1"
source = { registry = "https://pypi.org/simple" }
"""


class TestBuildSbom:
    def test_minimal_valid_cyclonedx_document(self, tmp_path):
        pyproject = _write_project(tmp_path)
        lockfile = _write_lockfile(tmp_path, BASIC_LOCKFILE)

        doc = sbom.build_sbom(
            lockfile_path=lockfile, pyproject_path=pyproject, generated_at=GENERATED_AT
        )

        assert doc["bomFormat"] == "CycloneDX"
        assert doc["specVersion"] == "1.5"
        assert doc["metadata"]["component"]["name"] == "demo-project"
        assert doc["metadata"]["component"]["version"] == "1.2.3"
        assert doc["metadata"]["timestamp"] == GENERATED_AT
        assert "serialNumber" not in doc
        json.dumps(doc)  # must be JSON-serializable

    def test_root_project_and_virtual_entries_excluded_from_components(self, tmp_path):
        pyproject = _write_project(tmp_path)
        lockfile = _write_lockfile(tmp_path, BASIC_LOCKFILE)
        doc = sbom.build_sbom(lockfile_path=lockfile, pyproject_path=pyproject)
        names = {component["name"] for component in doc["components"]}
        assert "demo-project" not in names
        assert names == {"requests", "pyyaml"}

    def test_components_sorted_by_name_and_version(self, tmp_path):
        pyproject = _write_project(tmp_path)
        lockfile = _write_lockfile(tmp_path, BASIC_LOCKFILE)
        doc = sbom.build_sbom(lockfile_path=lockfile, pyproject_path=pyproject)
        names = [component["name"] for component in doc["components"]]
        assert names == sorted(names)

    def test_component_has_pkg_pypi_purl(self, tmp_path):
        pyproject = _write_project(tmp_path)
        lockfile = _write_lockfile(tmp_path, BASIC_LOCKFILE)
        doc = sbom.build_sbom(lockfile_path=lockfile, pyproject_path=pyproject)
        requests_component = next(c for c in doc["components"] if c["name"] == "requests")
        assert requests_component["purl"] == "pkg:pypi/requests@2.31.0"
        assert requests_component["type"] == "library"

    def test_determinism_same_inputs_same_output(self, tmp_path):
        pyproject = _write_project(tmp_path)
        lockfile = _write_lockfile(tmp_path, BASIC_LOCKFILE)
        doc1 = sbom.build_sbom(
            lockfile_path=lockfile, pyproject_path=pyproject, generated_at=GENERATED_AT
        )
        doc2 = sbom.build_sbom(
            lockfile_path=lockfile, pyproject_path=pyproject, generated_at=GENERATED_AT
        )
        assert json.dumps(doc1, sort_keys=True) == json.dumps(doc2, sort_keys=True)

    def test_entry_missing_version_is_skipped_not_fabricated(self, tmp_path):
        pyproject = _write_project(tmp_path)
        lockfile = _write_lockfile(
            tmp_path,
            BASIC_LOCKFILE
            + """
[[package]]
name = "editable-local-thing"
source = { path = "../local" }
""",
        )
        doc = sbom.build_sbom(lockfile_path=lockfile, pyproject_path=pyproject)
        names = {component["name"] for component in doc["components"]}
        assert "editable-local-thing" not in names


class TestSbomErrors:
    def test_missing_lockfile_raises(self, tmp_path):
        pyproject = _write_project(tmp_path)
        with pytest.raises(sbom.SbomError, match="lockfile not found"):
            sbom.build_sbom(lockfile_path=tmp_path / "missing.lock", pyproject_path=pyproject)

    def test_missing_pyproject_raises(self, tmp_path):
        lockfile = _write_lockfile(tmp_path, BASIC_LOCKFILE)
        with pytest.raises(sbom.SbomError, match="pyproject file not found"):
            sbom.build_sbom(lockfile_path=lockfile, pyproject_path=tmp_path / "missing.toml")

    def test_lockfile_without_package_table_raises(self, tmp_path):
        pyproject = _write_project(tmp_path)
        lockfile = _write_lockfile(tmp_path, "version = 1\n")
        with pytest.raises(sbom.SbomError, match=r"no \[\[package\]\] entries"):
            sbom.build_sbom(lockfile_path=lockfile, pyproject_path=pyproject)

    def test_pyproject_without_project_table_raises(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[tool.other]\nkey = 1\n", encoding="utf-8")
        lockfile = _write_lockfile(tmp_path, BASIC_LOCKFILE)
        with pytest.raises(sbom.SbomError, match=r"no \[project\] table"):
            sbom.build_sbom(lockfile_path=lockfile, pyproject_path=pyproject)

    def test_package_entry_missing_name_raises(self, tmp_path):
        pyproject = _write_project(tmp_path)
        lockfile = _write_lockfile(
            tmp_path,
            """
[[package]]
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
""",
        )
        with pytest.raises(sbom.SbomError, match="missing its name"):
            sbom.build_sbom(lockfile_path=lockfile, pyproject_path=pyproject)

    def test_component_count_over_bound_rejected(self, tmp_path, monkeypatch):
        pyproject = _write_project(tmp_path)
        monkeypatch.setattr(sbom, "MAX_SBOM_COMPONENTS", 1)
        lockfile = _write_lockfile(tmp_path, BASIC_LOCKFILE)
        with pytest.raises(sbom.SbomError, match="exceeding the"):
            sbom.build_sbom(lockfile_path=lockfile, pyproject_path=pyproject)
