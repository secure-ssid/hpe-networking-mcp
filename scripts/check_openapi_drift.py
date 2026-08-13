#!/usr/bin/env python3
"""Classified drift gate for the Aruba developer-portal OpenAPI registry manifest.

Re-resolves each reference page recorded in
``ingestion/openapi_registry_manifest.json`` back to its ``oasPublicUrl``
pointer, fetches the pointed-at ReadMe api-registry document, and compares
its sha256 against the manifest's recorded hash. Designed to run on a
schedule so a developer-portal change -- a spec update, or another platform
migration like the July 2026 ReadMe SuperHub move -- surfaces as a failing
CI job instead of silently stale ingestion output.

Every finding is classified with the shared taxonomy in
``hpe_networking_mcp.pipeline.drift_taxonomy`` so the three failure modes
that used to collapse onto exit code 1 stay distinguishable:

* ``fresh`` -- pointer and sha256 both match the manifest.
* ``content_drift`` -- same registry pointer, different spec sha256.
* ``pointer_change`` -- the page now points at a different registry id
  (portal/layout move); the content was never compared.
* ``source_removed`` -- the reference page or registry document is gone
  (404/410), or a manifest-declared spec file is required locally and
  missing.
* ``source_added`` -- a spec file exists under the registry output
  directory that no manifest entry declares.
* ``unavailable`` -- transient/blocked transport failure after retries.
* ``parser_error`` -- fetched, but the pointer/JSON could not be parsed.
* ``not_checked`` -- ``--offline``: nothing was fetched, so nothing is
  claimed to be fresh.

Exit codes are per class (see ``drift_taxonomy.EXIT_CODES``); pass
``--exit-code-mode legacy`` for the old "any problem exits 1" behavior.
``2`` still means "no manifest to check" (run the ingestion scripts first).

Read-only: this never writes a spec, never advances a pin, and never
rewrites the manifest. Run ``ingestion/scrape_openapi.py`` /
``ingestion/scrape_cnac_spec.py`` to actually refresh a drifted entry.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from hpe_networking_mcp.pipeline import drift_taxonomy as taxonomy  # noqa: E402
from ingestion.readme_registry import check_entry_drift, load_manifest  # noqa: E402

DEFAULT_MANIFEST_PATH = _REPO_ROOT / "ingestion" / "openapi_registry_manifest.json"
DEFAULT_ARTIFACT_PATH = _REPO_ROOT / "outputs" / "drift" / "openapi-registry-drift.json"
CHECK_NAME = "openapi_registry_drift"


def _spec_dir(manifest_path: Path) -> Path:
    return _REPO_ROOT / "ingestion" / "sources" / "openapi_specs"


def local_spec_findings(
    registries: dict[str, dict],
    *,
    spec_dir: Path,
    require_local_specs: bool,
) -> list[taxonomy.Finding]:
    """Compare declared output paths against what is actually on disk.

    Spec files are git-ignored build artifacts, so a missing file is
    ``not_checked`` by default and only ``source_removed`` under
    ``--require-local-specs`` (a checkout that is supposed to be populated).
    A spec present on disk that no manifest entry declares is
    ``source_added`` -- that is how a hand-fetched or newly-published
    registry document shows up without a network call.
    """
    findings: list[taxonomy.Finding] = []
    declared: set[Path] = set()
    for registry_id, entry in sorted(registries.items()):
        output_path = entry.get("output_path")
        if not output_path:
            findings.append(
                taxonomy.Finding(
                    target=registry_id,
                    result_class=taxonomy.PARSER_ERROR,
                    detail="manifest entry has no output_path",
                )
            )
            continue
        path = _REPO_ROOT / output_path
        declared.add(path.resolve())
        if path.is_file():
            continue
        if require_local_specs:
            findings.append(
                taxonomy.Finding(
                    target=registry_id,
                    result_class=taxonomy.SOURCE_REMOVED,
                    detail=f"declared spec file is missing: {output_path}",
                    evidence={"output_path": output_path},
                )
            )
        else:
            findings.append(
                taxonomy.Finding(
                    target=registry_id,
                    result_class=taxonomy.NOT_CHECKED,
                    detail=(
                        f"spec file absent locally ({output_path}); git-ignored build "
                        "artifact, run ingestion/fetch_manifest_specs.py to populate"
                    ),
                )
            )
    if spec_dir.is_dir():
        for path in sorted(spec_dir.glob("*.json")):
            if path.resolve() in declared:
                continue
            try:
                display = str(path.relative_to(_REPO_ROOT))
            except ValueError:  # spec dir outside the repo (tests)
                display = str(path)
            findings.append(
                taxonomy.Finding(
                    target=path.name,
                    result_class=taxonomy.SOURCE_ADDED,
                    detail="spec file on disk is not declared by the registry manifest",
                    evidence={"path": display},
                )
            )
    return findings


def evaluate(
    manifest_path: Path,
    *,
    offline: bool = False,
    require_local_specs: bool = False,
    spec_dir: Path | None = None,
) -> tuple[list[taxonomy.Finding], int]:
    """Return (findings, declared registry count). Never raises on fetch errors."""
    manifest = load_manifest(manifest_path)
    registries: dict[str, dict] = manifest.get("registries", {})
    findings: list[taxonomy.Finding] = []
    for registry_id, entry in sorted(registries.items()):
        result = check_entry_drift(entry, offline=offline)
        findings.append(
            taxonomy.Finding(
                target=registry_id,
                result_class=result.result_class,
                detail=result.detail,
                legacy_status=result.status,
                evidence={
                    "source_url": result.source_url,
                    "observed_registry_id": result.observed_registry_id,
                    "observed_sha256": result.observed_sha256,
                    "manifest_sha256": entry.get("sha256"),
                },
            )
        )
    findings.extend(
        local_spec_findings(
            registries,
            spec_dir=spec_dir or _spec_dir(manifest_path),
            require_local_specs=require_local_specs,
        )
    )
    return findings, len(registries)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument(
        "--offline",
        action="store_true",
        help=(
            "Do not fetch anything: report every registry as not_checked. Use when "
            "external source refresh is disabled; never reports fresh."
        ),
    )
    parser.add_argument(
        "--require-local-specs",
        action="store_true",
        help="Treat a manifest-declared spec file missing from disk as source_removed.",
    )
    parser.add_argument("--spec-dir", type=Path, default=None)
    taxonomy.add_common_arguments(parser, default_artifact=DEFAULT_ARTIFACT_PATH)
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    if not manifest.get("registries"):
        print(
            f"No registries recorded in {args.manifest} -- run "
            "ingestion/scrape_openapi.py (and scrape_cnac_spec.py) at least "
            "once before checking drift.",
            file=sys.stderr,
        )
        return taxonomy.EXIT_USAGE

    findings, declared = evaluate(
        args.manifest,
        offline=args.offline,
        require_local_specs=args.require_local_specs,
        spec_dir=args.spec_dir,
    )
    report = taxonomy.build_report(
        CHECK_NAME,
        findings,
        refresh_sources=not args.offline,
        exit_code_mode=args.exit_code_mode,
        notes=(
            f"{declared} registries declared in {args.manifest.name}; "
            "offline run claims no freshness" if args.offline else
            f"{declared} registries declared in {args.manifest.name}"
        ),
    )
    taxonomy.print_report(report)
    if not args.no_artifact and args.json_artifact:
        path = taxonomy.write_report(args.json_artifact, report)
        print(f"wrote {path}")

    if report["dominant_class"] in (taxonomy.CONTENT_DRIFT, taxonomy.POINTER_CHANGE):
        print(
            "\nRefresh with ingestion/scrape_openapi.py and/or "
            "ingestion/scrape_cnac_spec.py, then re-run this check."
        )
    if report["check_incomplete"]:
        print(
            "\nSome registries could not be checked (network/parse failure). That is "
            "NOT confirmed content drift -- fix access or the parser and re-run."
        )
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
