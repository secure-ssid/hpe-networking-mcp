"""Deterministic release-bundle packaging primitives (v0.7 ``v07-live-artifacts``).

This module never makes a live vendor API call and never touches network
resources at all. It provides the reusable, scripts-independent building
blocks for assembling a release-artifacts bundle:

* :func:`read_source_freshness_payload` -- read (never fetch/regenerate) a
  prior local ``source_freshness_result`` snapshot.
* :func:`build_provenance_manifest` -- a plain, human/CI-readable build
  provenance record (explicitly *not* a signed attestation).
* :func:`write_checksums_file` / :func:`build_deterministic_archive` --
  sha256 checksums and a byte-for-byte-reproducible ``tar.gz`` (fixed
  member order/metadata, fixed gzip header) for a staged directory tree.

The end-to-end orchestrator that stitches these primitives together with
platform-specific evidence collection lives in
``scripts/build_release_bundle.py`` (not here), because that orchestration
needs ``scripts.*`` helpers (``run_v07_validation_matrix``,
``report_capability_gaps``, ``evaluate_axis_lab``,
``generate_router_automation_report``, ``package_indexes``) and this
repository's layering convention keeps ``src/hpe_networking_mcp/pipeline/*.py`` free of
``scripts.*`` imports (only ``scripts/*.py`` imports other
``scripts/*.py`` and ``src/hpe_networking_mcp/pipeline/*.py``, never the reverse).

Determinism: "deterministic" here describes the *archive layout and
packaging mechanics*, not literal byte-for-byte equality of two bundles
built at different times. Every JSON artifact is written through
``hpe_networking_mcp.pipeline.artifact_contracts.write_artifact`` (canonical, sorted-key JSON)
or an equivalent canonical serializer (sorted keys, sorted lists), and the
final ``tar.gz`` always has: a sorted, relative-posix member order; fixed
per-entry metadata (mtime=0, uid=0, gid=0, mode=0o644); and a fixed gzip
header (mtime=0, empty filename). Given byte-identical staged input files,
``build_deterministic_archive`` always produces a byte-identical archive
(see ``tests/unit/test_release_packaging.py``). Full end-to-end bundle
runs will still differ across time because the staged *evidence* files
intentionally embed a ``generated_at`` timestamp each run -- that is
expected and desirable (these are point-in-time evidence snapshots, not
static assets), and is the same behavior as every other timestamped
artifact kind in ``src/hpe_networking_mcp/pipeline/artifact_contracts.py``.

Nothing here signs or uploads anything -- that is a CI workflow's job
(``.github/workflows/release-artifacts.yml``), using the bundle's
``CHECKSUMS.txt`` as the ``subject-checksums`` input to
``actions/attest-build-provenance``.
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hpe_networking_mcp._paths import repo_root
from hpe_networking_mcp.pipeline import artifact_contracts as contracts

# Repo-level (non-package) paths: resolved centrally so the src-layout
# depth cannot drift again -- see hpe_networking_mcp._paths.
REPO_ROOT = repo_root()
DEFAULT_OUTPUT_DIR = REPO_ROOT / "dist"
BUNDLE_NAME_PREFIX = "hpe-networking-mcp-release-artifacts"

MAX_PROVENANCE_SUBJECTS = 500


class ReleasePackagingError(ValueError):
    """Raised for a malformed input or an internal packaging invariant violation."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def project_version(pyproject_path: Path = REPO_ROOT / "pyproject.toml") -> str:
    for line in pyproject_path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("version"):
            return line.split("=", 1)[1].strip().strip('"')
    return "0.0.0"


# ---------------------------------------------------------------------------
# Source freshness -- reads (never regenerates/re-fetches) a prior
# outputs/source-freshness.json snapshot from
# scripts.check_security_lifecycle_drift.py.
# ---------------------------------------------------------------------------


def read_source_freshness_payload(
    path: Path = REPO_ROOT / "outputs" / "source-freshness.json",
) -> dict[str, Any] | None:
    """Return the parsed, schema-validated payload of a prior
    ``source_freshness_result`` snapshot, or None if it doesn't exist.

    Never fetches a source itself -- this only ever reads an
    already-produced local file (from a manual or scheduled-CI run of
    ``scripts/check_security_lifecycle_drift.py``), consistent with this
    module never making any network call.
    """
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    contracts.build_artifact(contracts.SOURCE_FRESHNESS_RESULT, payload)  # validate, don't mutate
    return payload


# ---------------------------------------------------------------------------
# Provenance manifest -- human/CI-readable, never signed here.
# ---------------------------------------------------------------------------


def _git_output(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
            timeout=10,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def build_provenance_manifest(
    *,
    version: str,
    subjects: dict[str, str],
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build a plain, human/CI-readable provenance manifest for one release
    bundle -- never a signed/verified attestation (that step belongs to
    ``actions/attest-build-provenance`` in CI, using this bundle's
    ``CHECKSUMS.txt`` as its ``subject-checksums`` input).
    """
    if not subjects:
        raise ReleasePackagingError("provenance manifest requires at least one subject")
    if len(subjects) > MAX_PROVENANCE_SUBJECTS:
        raise ReleasePackagingError(
            f"provenance manifest has {len(subjects)} subjects, exceeding the "
            f"{MAX_PROVENANCE_SUBJECTS} safety bound"
        )
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    return {
        "provenance_schema": "hpe-networking-mcp-release-provenance-v1",
        "generated_at": generated_at or now_iso(),
        "release_version": version,
        "builder": {
            "id": "github-actions" if in_actions else "local",
            "workflow": os.environ.get("GITHUB_WORKFLOW"),
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        },
        "source": {
            "repository": os.environ.get("GITHUB_REPOSITORY"),
            "ref": os.environ.get("GITHUB_REF")
            or _git_output(["rev-parse", "--abbrev-ref", "HEAD"]),
            "commit": os.environ.get("GITHUB_SHA") or _git_output(["rev-parse", "HEAD"]),
        },
        "subjects": [
            {"name": name, "sha256": digest} for name, digest in sorted(subjects.items())
        ],
        "note": (
            "This manifest records build metadata for human/CI review only. It is "
            "not a cryptographically signed attestation. GitHub artifact attestation "
            "(actions/attest-build-provenance) is produced separately in CI from the "
            "sibling CHECKSUMS.txt subject-checksums file."
        ),
    }


# ---------------------------------------------------------------------------
# Deterministic archive assembly
# ---------------------------------------------------------------------------


def _staged_files(staging_dir: Path) -> list[Path]:
    return sorted(
        (path for path in staging_dir.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(staging_dir).as_posix(),
    )


def write_checksums_file(staging_dir: Path, *, filename: str = "CHECKSUMS.txt") -> Path:
    """Write a sha256sum-format checksums file for every currently staged
    file (sorted, relative posix paths) -- suitable as-is for
    ``actions/attest-build-provenance``'s ``subject-checksums`` input.

    Never lists itself (a checksums file cannot meaningfully include its
    own post-write digest), so calling this more than once as new files
    are staged always yields a self-consistent, fully verifiable file.
    """
    checksums_path = staging_dir / filename
    lines = [
        f"{sha256_file(path)}  {path.relative_to(staging_dir).as_posix()}"
        for path in _staged_files(staging_dir)
        if path != checksums_path
    ]
    checksums_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return checksums_path


def build_deterministic_archive(
    staging_dir: Path,
    archive_path: Path,
    *,
    arcname_prefix: str,
) -> str:
    """Build a deterministic (fixed mtime/uid/gid/mode, fixed gzip header,
    sorted entry order) ``tar.gz`` of every file under ``staging_dir``.

    Returns the archive's own sha256 hex digest. Writes atomically (temp
    file + ``os.replace``) so a reader never observes a partial archive.
    """
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    import uuid

    temporary = archive_path.with_name(f".{archive_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as raw, gzip.GzipFile(
            fileobj=raw, mode="wb", mtime=0, filename=""
        ) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
            for file_path in _staged_files(staging_dir):
                rel = file_path.relative_to(staging_dir).as_posix()
                arcname = f"{arcname_prefix}/{rel}"
                info = tarfile.TarInfo(name=arcname)
                info.size = file_path.stat().st_size
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mode = 0o644
                info.type = tarfile.REGTYPE
                with file_path.open("rb") as handle:
                    tar.addfile(info, handle)
        os.replace(temporary, archive_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return sha256_file(archive_path)
