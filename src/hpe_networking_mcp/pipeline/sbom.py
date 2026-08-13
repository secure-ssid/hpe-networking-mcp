"""Deterministic, dependency-free CycloneDX SBOM generation from ``uv.lock``.

This module never calls a package registry or any other network endpoint:
every component comes straight from the project's own committed, already
fully-resolved dependency graph (``uv.lock``), the same "ecosystem"
artifact ``uv`` itself produces and that CI already installs from via
``uv sync``. No optional SBOM-generation dependency (``cyclonedx-bom`` or
similar) is added to ``pyproject.toml`` for this -- ``uv.lock`` is TOML,
and this repository's dev dependency group already resolves a TOML parser
transitively (``tomli`` for Python < 3.11, stdlib ``tomllib`` for >= 3.11),
so no new package is required to read it.

The resulting document is a minimal, valid CycloneDX 1.5 JSON SBOM:
``bomFormat``/``specVersion``/``version``/``metadata.component`` plus one
``library`` component per resolved third-party package (name, version, and
a ``pkg:pypi/...`` purl). Components are sorted by ``(name, version)`` and
no random ``serialNumber`` is generated, so two runs against the same
``uv.lock`` always produce byte-identical output (see
``tests/unit/test_release_packaging.py``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hpe_networking_mcp._paths import repo_root

try:  # Python >= 3.11
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as _toml  # type: ignore[import-not-found, no-redef]

# Repo-level (non-package) paths: resolved centrally so the src-layout
# depth cannot drift again -- see hpe_networking_mcp._paths.
REPO_ROOT = repo_root()
DEFAULT_LOCKFILE = REPO_ROOT / "uv.lock"
DEFAULT_PYPROJECT = REPO_ROOT / "pyproject.toml"

CYCLONEDX_BOM_FORMAT = "CycloneDX"
CYCLONEDX_SPEC_VERSION = "1.5"
# Overall safety ceiling -- this repository's lockfile currently resolves
# ~100 packages; this bound only guards against a runaway/corrupted
# lockfile, never a realistic dependency count.
MAX_SBOM_COMPONENTS = 5000


class SbomError(ValueError):
    """Raised for a missing/malformed lockfile or pyproject input."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _project_metadata(pyproject_path: Path) -> dict[str, str]:
    if not pyproject_path.is_file():
        raise SbomError(f"pyproject file not found: {pyproject_path}")
    data = _toml.loads(pyproject_path.read_text(encoding="utf-8"))
    project = data.get("project")
    if not isinstance(project, dict):
        raise SbomError(f"{pyproject_path} has no [project] table")
    name = project.get("name")
    version = project.get("version")
    if not isinstance(name, str) or not name:
        raise SbomError(f"{pyproject_path} is missing [project].name")
    if not isinstance(version, str) or not version:
        raise SbomError(f"{pyproject_path} is missing [project].version")
    return {"name": name, "version": version}


def _source_kind(source: Any) -> str:
    if isinstance(source, dict):
        if "registry" in source:
            return "registry"
        if "git" in source:
            return "git"
        if "path" in source or "editable" in source:
            return "local"
        if "virtual" in source:
            return "virtual"
    return "unknown"


def _lockfile_packages(lockfile_path: Path) -> list[dict[str, str]]:
    if not lockfile_path.is_file():
        raise SbomError(f"lockfile not found: {lockfile_path}")
    data = _toml.loads(lockfile_path.read_text(encoding="utf-8"))
    raw_packages = data.get("package")
    if not isinstance(raw_packages, list):
        raise SbomError(f"{lockfile_path} has no [[package]] entries")
    if len(raw_packages) > MAX_SBOM_COMPONENTS:
        raise SbomError(
            f"{lockfile_path} has {len(raw_packages)} packages, exceeding the "
            f"{MAX_SBOM_COMPONENTS}-component SBOM safety bound"
        )
    packages: list[dict[str, str]] = []
    for raw in raw_packages:
        if not isinstance(raw, dict):
            raise SbomError("a [[package]] entry is not a table")
        name = raw.get("name")
        version = raw.get("version")
        if not isinstance(name, str) or not name:
            raise SbomError("a [[package]] entry is missing its name")
        source_kind = _source_kind(raw.get("source"))
        if not isinstance(version, str) or not version:
            # A local/virtual/editable entry (e.g. this project's own root
            # package under a `source = { virtual = "." }` entry) may
            # legitimately omit a version -- skip rather than fabricate one.
            continue
        packages.append({"name": name, "version": version, "source": source_kind})
    return packages


def _component(package: dict[str, str]) -> dict[str, Any]:
    purl = f"pkg:pypi/{package['name']}@{package['version']}"
    return {
        "type": "library",
        "name": package["name"],
        "version": package["version"],
        "purl": purl,
        "bom-ref": purl,
    }


def build_sbom(
    *,
    lockfile_path: Path = DEFAULT_LOCKFILE,
    pyproject_path: Path = DEFAULT_PYPROJECT,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic CycloneDX 1.5 JSON SBOM document.

    Raises :class:`SbomError` if either input is missing or malformed --
    never silently emits an empty/partial SBOM.
    """
    project = _project_metadata(pyproject_path)
    packages = _lockfile_packages(lockfile_path)
    components = sorted(
        (
            _component(package)
            for package in packages
            if package["source"] != "virtual" and package["name"] != project["name"]
        ),
        key=lambda component: (component["name"], component["version"]),
    )
    root_purl = f"pkg:pypi/{project['name']}@{project['version']}"
    return {
        "bomFormat": CYCLONEDX_BOM_FORMAT,
        "specVersion": CYCLONEDX_SPEC_VERSION,
        "version": 1,
        "metadata": {
            "timestamp": generated_at or _now_iso(),
            "component": {
                "type": "application",
                "name": project["name"],
                "version": project["version"],
                "bom-ref": root_purl,
                "purl": root_purl,
            },
        },
        "components": components,
    }
