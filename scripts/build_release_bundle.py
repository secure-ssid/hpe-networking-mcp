#!/usr/bin/env python3
"""Build one deterministic release-artifacts bundle.

Orchestrates already-existing, already-safe offline helpers -- never
duplicates product/platform logic and never makes a live vendor API call:

* ``scripts.run_v07_validation_matrix`` -- credential-gated validation matrix
* ``scripts.report_capability_gaps`` -- per-platform tool capability counts
* ``hpe_networking_mcp.pipeline.optional_product_evidence`` -- optional-backend
  compatibility evidence
* ``scripts.evaluate_axis_lab`` -- Axis split-CRUD contract evidence
* ``scripts.generate_router_automation_report`` -- router dependency/reconciliation plans
* ``scripts.package_indexes`` -- prebuilt RAG/OpenAPI indexes, only if present
* ``hpe_networking_mcp.pipeline.release_packaging`` -- provenance, checksums,
  and deterministic archive primitives
* ``hpe_networking_mcp.pipeline.sbom`` -- CycloneDX 1.5 JSON SBOM from ``uv.lock``

Produces, under ``--output-dir`` (default ``dist/``):

    <output-dir>/<version>/
        evidence/
            validation-matrix.json          (validation_matrix_result)
            capability-snapshot.json        (capability_snapshot)
            source-freshness.json           (source_freshness_result, if a local snapshot exists)
            optional-product-evidence/*.json  (platform_compatibility_result /
                                               live_lifecycle_evidence)
            router-automation/*.json        (router_dependency_plan / router_reconciliation_plan)
        indexes/
            hpe-networking-mcp-rag-index-<version>.tar.gz(.sha256)
                (only if data/ indexes are present)
        release-manifest.json      (release_artifact_manifest -- contract-typed evidence files only)
        sbom.json                  (CycloneDX 1.5 JSON)
        provenance.json            (human/CI-readable build provenance -- not a signed attestation)
        CHECKSUMS.txt              (sha256sum-format subject-checksums for every staged file)
    <output-dir>/hpe-networking-mcp-release-artifacts-<version>.tar.gz(.sha256)

Does not sign, attest, publish, or upload anything -- see
``.github/workflows/release-artifacts.yml`` for the CI steps that do.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from hpe_networking_mcp.pipeline import artifact_contracts as contracts  # noqa: E402
from hpe_networking_mcp.pipeline import release_packaging  # noqa: E402
from hpe_networking_mcp.pipeline import sbom as sbom_module  # noqa: E402


def collect_capability_snapshot(*, generated_at: str | None = None) -> dict[str, object]:
    """Build a ``capability_snapshot`` payload from the existing capability
    gap report's own per-platform row collection (never re-derived)."""
    from scripts import report_capability_gaps

    rows = report_capability_gaps.collect_rows()
    platforms = []
    for row in rows:
        capabilities = row["capabilities"]
        platforms.append(
            {
                "platform": row["platform"].key,
                "read": int(capabilities.get("read", 0)),
                "diagnostic": int(capabilities.get("diagnostic", 0)),
                "write": int(capabilities.get("write", 0)),
                "destructive": int(capabilities.get("destructive", 0)),
                "source": "combined",
            }
        )
    return {
        "generated_at": generated_at or release_packaging.now_iso(),
        "platforms": platforms,
    }


@dataclass(frozen=True)
class ReleaseBundleResult:
    """Summary of one assembled release bundle."""

    version: str
    staging_dir: Path
    archive_path: Path
    archive_checksum_path: Path
    release_manifest_path: Path
    sbom_path: Path
    provenance_path: Path
    checksums_path: Path
    manifest_entries: tuple[contracts.ManifestEntry, ...]


def assemble_release_bundle(
    *,
    version: str | None = None,
    output_dir: Path = release_packaging.DEFAULT_OUTPUT_DIR,
    include_indexes: bool = True,
) -> ReleaseBundleResult:
    """Assemble one full, deterministic release bundle under ``output_dir``.

    Never makes a network call. Every evidence artifact is produced through
    an existing, already-safe helper -- this function only orchestrates
    and packages, it never duplicates product/platform logic.
    """
    version = version or f"v{release_packaging.project_version()}"
    staging_dir = output_dir / version
    if staging_dir.exists():
        raise release_packaging.ReleasePackagingError(
            f"staging directory already exists: {staging_dir} "
            "(remove it first; assemble_release_bundle never overwrites an existing bundle)"
        )
    staging_dir.mkdir(parents=True)

    evidence_dir = staging_dir / "evidence"
    manifest_entries: list[contracts.ManifestEntry] = []

    # 1. Validation matrix (offline classification across every platform).
    from scripts import run_v07_validation_matrix as matrix_runner

    matrix_payload = matrix_runner.build_validation_matrix_payload()
    manifest_entries.append(
        contracts.write_artifact(
            evidence_dir / "validation-matrix.json",
            contracts.VALIDATION_MATRIX_RESULT,
            matrix_payload,
        )
    )

    # 2. Capability snapshot.
    manifest_entries.append(
        contracts.write_artifact(
            evidence_dir / "capability-snapshot.json",
            contracts.CAPABILITY_SNAPSHOT,
            collect_capability_snapshot(),
        )
    )

    # 3. Source freshness (only if a prior local snapshot exists -- never
    # fetched here).
    freshness_payload = release_packaging.read_source_freshness_payload()
    if freshness_payload is not None:
        manifest_entries.append(
            contracts.write_artifact(
                evidence_dir / "source-freshness.json",
                contracts.SOURCE_FRESHNESS_RESULT,
                freshness_payload,
            )
        )

    # 4. Optional product backend evidence (compatibility + gated live-read).
    from hpe_networking_mcp.pipeline import optional_product_evidence

    optional_dir = evidence_dir / "optional-product-evidence"
    manifest_entries.extend(
        entry
        for entries in optional_product_evidence.write_all_backend_evidence(
            output_dir=optional_dir
        ).values()
        for entry in entries
    )

    # 4b. Axis lab evidence (split-CRUD contract + gated live-read/plan).
    from scripts import evaluate_axis_lab

    manifest_entries.extend(evaluate_axis_lab.build_evidence_artifact(output_dir=optional_dir))

    # 5. Router automation plans (dependency + reconciliation), fully
    # offline/dry-run.
    from scripts import generate_router_automation_report as router_report

    router_dir = evidence_dir / "router-automation"
    router_dir.mkdir(parents=True, exist_ok=True)
    dependency_entry = router_report.generate_dependency_plan_artifact(
        router_dir / "router-automation-dependency-plan.json"
    )
    if dependency_entry is not None:
        manifest_entries.append(dependency_entry)
    reconciliation_entry = router_report.generate_reconciliation_plan_artifact(
        router_dir / "router-automation-reconciliation-plan.json"
    )
    if reconciliation_entry is not None:
        manifest_entries.append(reconciliation_entry)

    # 6. Prebuilt RAG/OpenAPI indexes, only if the local data/ artifacts exist.
    if include_indexes:
        from scripts import package_indexes

        try:
            package_indexes.package_indexes(version, staging_dir / "indexes")
        except SystemExit:
            pass  # indexes not built locally -- optional, never a hard failure

    # 7. Release artifact manifest (contract-typed evidence files only).
    release_manifest_path = staging_dir / "release-manifest.json"
    release_manifest_entry = contracts.write_artifact(
        release_manifest_path,
        contracts.RELEASE_ARTIFACT_MANIFEST,
        {
            "generated_at": release_packaging.now_iso(),
            "release_version": version,
            "entries": [contracts.to_json_dict(entry) for entry in manifest_entries],
        },
    )

    # 8. SBOM (CycloneDX, from uv.lock -- not a contract kind).
    sbom_path = staging_dir / "sbom.json"
    sbom_document = sbom_module.build_sbom()
    sbom_path.write_text(
        json.dumps(sbom_document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    # 9. Checksums for every staged file so far (subject-checksums input).
    checksums_path = release_packaging.write_checksums_file(staging_dir)
    subjects = {}
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        if digest and name:
            subjects[name] = digest

    # 10. Provenance manifest (references every subject hashed above).
    provenance_path = staging_dir / "provenance.json"
    provenance_document = release_packaging.build_provenance_manifest(
        version=version, subjects=subjects
    )
    provenance_path.write_text(
        json.dumps(provenance_document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    # Re-write checksums to also cover sbom.json/provenance.json themselves.
    checksums_path = release_packaging.write_checksums_file(staging_dir)

    # 11. Deterministic outer archive + its own checksum.
    archive_path = output_dir / f"{release_packaging.BUNDLE_NAME_PREFIX}-{version}.tar.gz"
    archive_sha256 = release_packaging.build_deterministic_archive(
        staging_dir,
        archive_path,
        arcname_prefix=f"{release_packaging.BUNDLE_NAME_PREFIX}-{version}",
    )
    archive_checksum_path = archive_path.with_suffix(archive_path.suffix + ".sha256")
    archive_checksum_path.write_text(f"{archive_sha256}  {archive_path.name}\n", encoding="utf-8")

    return ReleaseBundleResult(
        version=version,
        staging_dir=staging_dir,
        archive_path=archive_path,
        archive_checksum_path=archive_checksum_path,
        release_manifest_path=release_manifest_path,
        sbom_path=sbom_path,
        provenance_path=provenance_path,
        checksums_path=checksums_path,
        manifest_entries=tuple(manifest_entries) + (release_manifest_entry,),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        default=None,
        help="Release version tag (defaults to 'v' + pyproject.toml's [project].version)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=release_packaging.DEFAULT_OUTPUT_DIR,
        help="Directory to stage and archive the bundle under (default: dist/)",
    )
    parser.add_argument(
        "--no-indexes",
        action="store_true",
        help="Skip packaging the prebuilt RAG/OpenAPI indexes even if data/ artifacts exist",
    )
    args = parser.parse_args(argv)

    try:
        result = assemble_release_bundle(
            version=args.version,
            output_dir=args.output_dir,
            include_indexes=not args.no_indexes,
        )
    except release_packaging.ReleasePackagingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Staged bundle:      {result.staging_dir}")
    print(f"Release manifest:   {result.release_manifest_path}")
    print(f"SBOM:               {result.sbom_path}")
    print(f"Provenance:         {result.provenance_path}")
    print(f"Checksums:          {result.checksums_path}")
    print(f"Archive:            {result.archive_path}")
    print(f"Archive checksum:   {result.archive_checksum_path}")
    print(f"Manifest entries:   {len(result.manifest_entries)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
