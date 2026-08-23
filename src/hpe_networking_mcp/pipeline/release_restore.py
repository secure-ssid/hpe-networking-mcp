"""Safe restore and smoke-validation for packaged release bundles.

Generalizes the safe-extraction pattern already used by
``scripts/download_indexes.py`` (path-traversal rejection, sha256 checksum
verification against a sibling ``.sha256`` file) and extends it with:

* a parameterizable required top-level prefix (instead of a hardcoded
  ``"data"``), so it can restore either the RAG/OpenAPI index tarball or
  the full release-artifacts bundle;
* file-count / per-file / total-size bounds, enforced *before* any bytes
  are written;
* rejection of any non-regular-file/non-directory member (symlinks,
  hardlinks, device/fifo entries);
* post-extraction schema validation of every contract-typed JSON file
  referenced by the bundle's own ``release-manifest.json`` (via
  :func:`hpe_networking_mcp.pipeline.artifact_contracts.build_artifact`), plus basic
  structural sanity checks of the non-contract ``sbom.json`` /
  ``provenance.json`` files;
* extraction only into a caller-managed temporary directory, with a hard
  refusal to extract into the repository root or any tracked top-level
  source directory -- a restore/smoke test never overwrites repository
  data, and :func:`smoke_test_bundle` always cleans up after itself.

This module never performs a network call.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from hpe_networking_mcp._paths import repo_root
from hpe_networking_mcp.pipeline import artifact_contracts as contracts

# Repo-level (non-package) paths: resolved centrally so the src-layout
# depth cannot drift again -- see hpe_networking_mcp._paths.
REPO_ROOT = repo_root()

# Top-level source directories that must never be used as a restore target,
# even if a caller passes a path resolving inside the repository by mistake.
_GUARDED_TOP_LEVEL_NAMES = frozenset(
    {
        "pipeline",
        "mcp_servers",
        "scripts",
        "tests",
        "docs",
        "config",
        "ingestion",
        "resources",
        "inputs",
        ".git",
    }
)

DEFAULT_MAX_MEMBERS = 1000
DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB (bundle may embed prebuilt RAG indexes)
DEFAULT_MAX_MEMBER_BYTES = 1024 * 1024 * 1024  # 1 GiB per file


class RestoreError(ValueError):
    """Raised for an unsafe archive member, checksum mismatch, or bound violation."""


@dataclass(frozen=True)
class RestoreBounds:
    """Safety bounds enforced before any archive member is written."""

    max_members: int = DEFAULT_MAX_MEMBERS
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES


@dataclass(frozen=True)
class RestoreReport:
    """Summary of one successful restore + smoke-validation pass."""

    archive: Path
    member_count: int
    total_bytes: int
    validated_contract_files: tuple[str, ...] = field(default_factory=tuple)
    structural_checks: tuple[str, ...] = field(default_factory=tuple)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_checksum_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        checksum = stripped.split()[0].lower()
        if len(checksum) == 64 and all(char in "0123456789abcdef" for char in checksum):
            return checksum
        raise RestoreError(f"invalid checksum line: {line!r}")
    raise RestoreError("checksum file is empty")


def verify_checksum(archive: Path, checksum_file: Path) -> None:
    """Raise :class:`RestoreError` unless ``archive``'s sha256 matches ``checksum_file``."""
    expected = parse_checksum_line(checksum_file.read_text(encoding="utf-8"))
    actual = sha256_file(archive)
    if actual != expected:
        raise RestoreError(
            f"checksum mismatch for {archive}: expected {expected}, got {actual}"
        )


def _assert_safe_output_dir(output_dir: Path) -> Path:
    resolved = output_dir.resolve(strict=False)
    if resolved == REPO_ROOT:
        raise RestoreError("refusing to extract into the repository root")
    try:
        relative = resolved.relative_to(REPO_ROOT)
    except ValueError:
        return resolved  # outside the repository entirely -- always safe
    if relative.parts and relative.parts[0] in _GUARDED_TOP_LEVEL_NAMES:
        raise RestoreError(
            f"refusing to extract into guarded repository path: {resolved}"
        )
    return resolved


def _member_target(
    member: tarfile.TarInfo, output_dir: Path, *, required_prefix: str | None
) -> Path:
    name = PurePosixPath(member.name)
    parts = name.parts
    if name.is_absolute() or not parts or ".." in parts:
        raise RestoreError(f"unsafe archive member path: {member.name!r}")
    if required_prefix is not None and parts[0] != required_prefix:
        raise RestoreError(
            f"archive member {member.name!r} does not start with required "
            f"prefix {required_prefix!r}"
        )
    output_root = output_dir.resolve(strict=False)
    target = output_root.joinpath(*parts).resolve(strict=False)
    try:
        target.relative_to(output_root)
    except ValueError as exc:
        raise RestoreError(f"unsafe archive member path: {member.name!r}") from exc
    return target


def _check_bounds(members: list[tarfile.TarInfo], bounds: RestoreBounds) -> int:
    if len(members) > bounds.max_members:
        raise RestoreError(
            f"archive has {len(members)} members, exceeding the "
            f"{bounds.max_members} member-count bound"
        )
    total_bytes = 0
    for member in members:
        if member.isfile():
            if member.size > bounds.max_member_bytes:
                raise RestoreError(
                    f"archive member {member.name!r} is {member.size} bytes, "
                    f"exceeding the {bounds.max_member_bytes}-byte per-file bound"
                )
            total_bytes += member.size
    if total_bytes > bounds.max_total_bytes:
        raise RestoreError(
            f"archive total uncompressed size {total_bytes} bytes exceeds the "
            f"{bounds.max_total_bytes}-byte bound"
        )
    return total_bytes


def extract_archive(
    archive: Path,
    output_dir: Path,
    *,
    required_prefix: str | None = None,
    bounds: RestoreBounds | None = None,
) -> tuple[int, int]:
    """Safely extract ``archive`` into ``output_dir``.

    Rejects path traversal, absolute paths, and any non-regular-file /
    non-directory member *before* writing anything, and enforces
    file-count / per-file / total-size bounds up front. Refuses to
    extract into the repository root or a guarded top-level source
    directory. Returns ``(member_count, total_bytes)``.
    """
    bounds = bounds or RestoreBounds()
    safe_output_dir = _assert_safe_output_dir(output_dir)
    safe_output_dir.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            if not (member.isfile() or member.isdir()):
                raise RestoreError(f"unsafe archive member type: {member.name!r}")
        total_bytes = _check_bounds(members, bounds)

        for member in members:
            target = _member_target(member, safe_output_dir, required_prefix=required_prefix)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                raise RestoreError(f"could not read archive member: {member.name!r}")
            with source:
                data = source.read()
            target.write_bytes(data)

    return len(members), total_bytes


def _find_by_basename(root: Path, basename: str) -> Path | None:
    matches = [path for path in root.rglob(basename) if path.is_file()]
    if not matches:
        return None
    # Deterministic tie-break: shortest relative path, then lexicographic.
    matches.sort(key=lambda path: (len(path.relative_to(root).parts), path.as_posix()))
    return matches[0]


def validate_extracted_bundle(root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Schema-validate every contract-typed JSON file in an extracted bundle.

    Requires a top-level ``release-manifest.json`` (itself a
    ``release_artifact_manifest`` contract). For every entry it lists,
    locates the matching file by basename, verifies its sha256/size still
    match the manifest record, and re-validates its JSON payload against
    :func:`hpe_networking_mcp.pipeline.artifact_contracts.build_artifact` for that entry's
    ``kind``. Also performs a light structural sanity check of the
    non-contract ``sbom.json`` (CycloneDX) and ``provenance.json`` files
    when present.

    Returns ``(validated_contract_filenames, structural_check_names)``.
    Raises :class:`RestoreError` if the manifest is missing/invalid, an
    entry's file is missing, its checksum/size no longer matches, or its
    payload fails schema validation.
    """
    manifest_candidates = list(root.rglob("release-manifest.json"))
    if not manifest_candidates:
        raise RestoreError("bundle is missing a top-level release-manifest.json")
    manifest_path = min(manifest_candidates, key=lambda path: len(path.relative_to(root).parts))
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = contracts.build_artifact(contracts.RELEASE_ARTIFACT_MANIFEST, manifest_payload)

    validated: list[str] = []
    for entry in manifest.entries:
        found = _find_by_basename(root, entry.filename)
        if found is None:
            raise RestoreError(f"manifest entry {entry.filename!r} not found in extracted bundle")
        actual_size = found.stat().st_size
        if actual_size != entry.size_bytes:
            raise RestoreError(
                f"manifest entry {entry.filename!r} size mismatch: "
                f"expected {entry.size_bytes}, found {actual_size}"
            )
        actual_sha256 = sha256_file(found)
        if actual_sha256 != entry.sha256:
            raise RestoreError(
                f"manifest entry {entry.filename!r} sha256 mismatch: "
                f"expected {entry.sha256}, found {actual_sha256}"
            )
        payload = json.loads(found.read_text(encoding="utf-8"))
        try:
            contracts.build_artifact(entry.kind, payload)
        except contracts.ArtifactValidationError as exc:
            raise RestoreError(
                f"manifest entry {entry.filename!r} failed schema validation: {exc}"
            ) from exc
        validated.append(entry.filename)

    structural: list[str] = []
    sbom_candidates = list(root.rglob("sbom.json"))
    if sbom_candidates:
        sbom_payload = json.loads(sbom_candidates[0].read_text(encoding="utf-8"))
        if sbom_payload.get("bomFormat") != "CycloneDX" or "components" not in sbom_payload:
            raise RestoreError("sbom.json failed structural sanity check (not CycloneDX-shaped)")
        structural.append("sbom.json")

    provenance_candidates = list(root.rglob("provenance.json"))
    if provenance_candidates:
        provenance_payload = json.loads(provenance_candidates[0].read_text(encoding="utf-8"))
        if "subjects" not in provenance_payload or "release_version" not in provenance_payload:
            raise RestoreError("provenance.json failed structural sanity check")
        structural.append("provenance.json")

    return tuple(sorted(validated)), tuple(sorted(structural))


@contextmanager
def _temporary_restore_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="hpe-networking-mcp-restore-smoke-") as tmp:
        yield Path(tmp)


def smoke_test_bundle(
    archive: Path,
    *,
    checksum_file: Path | None = None,
    required_prefix: str | None = None,
    bounds: RestoreBounds | None = None,
) -> RestoreReport:
    """Restore ``archive`` into a throwaway temp directory, validate it, and
    clean up -- never leaves extracted files behind and never touches the
    repository tree.

    Steps: checksum verification (if ``checksum_file`` given or a sibling
    ``<archive>.sha256`` exists) -> safe bounded extraction -> per-file
    schema validation of every contract-typed JSON referenced by the
    bundle's own manifest -> temp-directory cleanup (via
    ``tempfile.TemporaryDirectory``'s own context-manager teardown, even
    on failure).
    """
    checksum_file = checksum_file or archive.with_suffix(archive.suffix + ".sha256")
    if checksum_file.is_file():
        verify_checksum(archive, checksum_file)

    with _temporary_restore_dir() as restore_dir:
        member_count, total_bytes = extract_archive(
            archive, restore_dir, required_prefix=required_prefix, bounds=bounds
        )
        validated, structural = validate_extracted_bundle(restore_dir)
        return RestoreReport(
            archive=archive,
            member_count=member_count,
            total_bytes=total_bytes,
            validated_contract_files=validated,
            structural_checks=structural,
        )
