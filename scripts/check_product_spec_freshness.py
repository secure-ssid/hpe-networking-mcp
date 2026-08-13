#!/usr/bin/env python3
"""Locally-derivable freshness gate for the api-next product OpenAPI specs.

``ingestion/product_specs_manifest.json`` is the committed discovery record
for the seven non-Central Aruba developer-portal sections (AOS-CX, AOS-8,
Central 2.x, ClearPass, UXI, Fabric Composer, EdgeConnect) harvested by
``ingestion/scrape_apinext_specs.py``. Until now nothing checked it at all:
a renamed ``spec_uri``, a branch bump, a section that no longer exists in
the sidebar map, a spec whose ``path_count`` no longer matches the file on
disk, or a stray spec file nobody declared would all go unnoticed.

Everything this gate asserts is derivable **locally** -- no portal fetch is
required, so it runs in CI and in tests with no network at all:

``branch`` / ``spec_uri``
    ``spec_uri`` must be ``/branches/<branch>/apis/<file>.json``, i.e. the
    URI and the recorded branch must agree. A disagreement is a
    ``pointer_change``: the portal moved the document, the content was
    never compared.
``sidebar membership``
    ``section`` must still be a key of
    ``ingestion.scrape_apinext_specs.PROJECTS`` and ``project`` must be the
    slug that map assigns it. A section that left the map (or a project
    renamed under it) is a ``pointer_change``.
``output_path``
    Must follow the scraper's own convention,
    ``ingestion/sources/product_specs/<section>-<slug(uri stem)>.json``.
``path_count`` / ``digest``
    When the spec file is present on disk its real path count is compared
    with the manifest's ``path_count`` (``content_drift`` on mismatch) and
    its sha256 is compared with the manifest's optional ``sha256``
    (``content_drift`` on mismatch). Specs are git-ignored build artifacts,
    so an absent file is ``not_checked`` -- never a silent pass and never a
    failure -- unless ``--require-local-specs`` is given, which makes it
    ``source_removed``.
``undeclared specs``
    A ``*.json`` under the product-spec directory that no manifest entry
    declares is ``source_added``.

Manifest-level problems (unreadable/non-JSON file, missing required keys,
duplicate ``spec_uri``/``output_path``) are ``parser_error`` -- the gate
cannot conclude anything about freshness from a manifest it cannot read,
and must not report that as drift.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from hpe_networking_mcp.pipeline import drift_taxonomy as taxonomy  # noqa: E402
from ingestion.scrape_apinext_specs import PROJECTS, slugify  # noqa: E402

CHECK_NAME = "product_spec_freshness"
DEFAULT_MANIFEST_PATH = _REPO_ROOT / "ingestion" / "product_specs_manifest.json"
DEFAULT_SPEC_DIR = _REPO_ROOT / "ingestion" / "sources" / "product_specs"
DEFAULT_ARTIFACT_PATH = _REPO_ROOT / "outputs" / "drift" / "product-spec-freshness.json"

REQUIRED_KEYS = (
    "branch",
    "output_path",
    "path_count",
    "project",
    "section",
    "source_url",
    "spec_uri",
    "title",
)

_SPEC_URI_RE = re.compile(r"^/branches/(?P<branch>[^/]+)/apis/(?P<stem>[^/]+)\.json$")


class ManifestError(Exception):
    """The product-spec manifest itself is unreadable or malformed."""


def load_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> list[dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path} is not valid JSON: {exc}") from exc
    specs = data.get("specs") if isinstance(data, dict) else None
    if not isinstance(specs, list) or not specs:
        raise ManifestError(f"{path} has no non-empty 'specs' list")
    return specs


def expected_output_path(entry: dict[str, Any]) -> str | None:
    """Derive the scraper's own output path convention for ``entry``."""
    match = _SPEC_URI_RE.match(str(entry.get("spec_uri", "")))
    if not match:
        return None
    return f"ingestion/sources/product_specs/{entry['section']}-{slugify(match['stem'])}.json"


def evaluate_entry(
    entry: dict[str, Any],
    *,
    repo_root: Path,
    spec_dir: Path,
    require_local_specs: bool,
) -> taxonomy.Finding:
    target = str(entry.get("output_path") or entry.get("spec_uri") or "<unnamed spec>")

    missing = [key for key in REQUIRED_KEYS if entry.get(key) in (None, "")]
    if missing:
        return taxonomy.Finding(
            target=target,
            result_class=taxonomy.PARSER_ERROR,
            detail=f"manifest entry missing required keys: {', '.join(missing)}",
        )

    section = str(entry["section"])
    project = str(entry["project"])
    if section not in PROJECTS:
        return taxonomy.Finding(
            target=target,
            result_class=taxonomy.POINTER_CHANGE,
            detail=(
                f"section {section!r} is no longer a known api-next sidebar section "
                f"(known: {', '.join(sorted(PROJECTS))})"
            ),
            evidence={"section": section, "project": project},
        )
    if PROJECTS[section] != project:
        return taxonomy.Finding(
            target=target,
            result_class=taxonomy.POINTER_CHANGE,
            detail=(
                f"section {section!r} maps to project {PROJECTS[section]!r}, "
                f"manifest records {project!r}"
            ),
            evidence={"section": section, "project": project},
        )

    match = _SPEC_URI_RE.match(str(entry["spec_uri"]))
    if not match:
        return taxonomy.Finding(
            target=target,
            result_class=taxonomy.POINTER_CHANGE,
            detail=(
                f"spec_uri {entry['spec_uri']!r} is not /branches/<branch>/apis/<file>.json"
            ),
            evidence={"spec_uri": entry["spec_uri"]},
        )
    if match["branch"] != str(entry["branch"]):
        return taxonomy.Finding(
            target=target,
            result_class=taxonomy.POINTER_CHANGE,
            detail=(
                f"branch mismatch: spec_uri carries {match['branch']!r}, "
                f"manifest records {entry['branch']!r}"
            ),
            evidence={"spec_uri": entry["spec_uri"], "branch": entry["branch"]},
        )

    expected = expected_output_path(entry)
    if expected and str(entry["output_path"]) != expected:
        return taxonomy.Finding(
            target=target,
            result_class=taxonomy.POINTER_CHANGE,
            detail=(
                f"output_path {entry['output_path']!r} does not follow the scraper "
                f"convention {expected!r}"
            ),
        )

    spec_path = repo_root / str(entry["output_path"])
    if not spec_path.is_file():
        if require_local_specs:
            return taxonomy.Finding(
                target=target,
                result_class=taxonomy.SOURCE_REMOVED,
                detail=f"declared spec file is missing: {entry['output_path']}",
            )
        return taxonomy.Finding(
            target=target,
            result_class=taxonomy.NOT_CHECKED,
            detail=(
                "spec file absent locally (git-ignored build artifact); run "
                "ingestion/scrape_apinext_specs.py to materialize it"
            ),
            evidence={"branch": entry["branch"], "spec_uri": entry["spec_uri"]},
        )

    raw = spec_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        return taxonomy.Finding(
            target=target,
            result_class=taxonomy.PARSER_ERROR,
            detail=f"on-disk spec is not valid JSON: {exc}",
        )
    paths = spec.get("paths") if isinstance(spec, dict) else None
    if not isinstance(paths, dict):
        return taxonomy.Finding(
            target=target,
            result_class=taxonomy.PARSER_ERROR,
            detail="on-disk spec has no 'paths' object",
        )

    recorded_digest = entry.get("sha256")
    if recorded_digest and recorded_digest != digest:
        return taxonomy.Finding(
            target=target,
            result_class=taxonomy.CONTENT_DRIFT,
            detail=f"sha256 {str(recorded_digest)[:12]} -> {digest[:12]}",
            evidence={"observed_sha256": digest},
        )
    if len(paths) != int(entry["path_count"]):
        return taxonomy.Finding(
            target=target,
            result_class=taxonomy.CONTENT_DRIFT,
            detail=(
                f"path_count {entry['path_count']} recorded, {len(paths)} on disk"
            ),
            evidence={"observed_path_count": len(paths), "observed_sha256": digest},
        )
    return taxonomy.Finding(
        target=target,
        result_class=taxonomy.FRESH,
        detail=(
            f"branch {entry['branch']}, {len(paths)} paths, sha256 {digest[:12]}"
            + ("" if recorded_digest else " (no digest baseline recorded)")
        ),
        evidence={
            "observed_sha256": digest,
            "digest_baseline_recorded": bool(recorded_digest),
            "branch": entry["branch"],
        },
    )


def undeclared_spec_findings(
    entries: list[dict[str, Any]], *, repo_root: Path, spec_dir: Path
) -> list[taxonomy.Finding]:
    declared = {
        (repo_root / str(entry.get("output_path", ""))).resolve()
        for entry in entries
        if entry.get("output_path")
    }
    findings: list[taxonomy.Finding] = []
    if not spec_dir.is_dir():
        return findings
    for path in sorted(spec_dir.glob("*.json")):
        if path.resolve() in declared:
            continue
        findings.append(
            taxonomy.Finding(
                target=path.name,
                result_class=taxonomy.SOURCE_ADDED,
                detail="spec file on disk is not declared in product_specs_manifest.json",
            )
        )
    return findings


def duplicate_findings(entries: list[dict[str, Any]]) -> list[taxonomy.Finding]:
    findings: list[taxonomy.Finding] = []
    for key in ("spec_uri", "output_path"):
        seen: dict[str, int] = {}
        for entry in entries:
            value = str(entry.get(key, ""))
            if not value:
                continue
            seen[value] = seen.get(value, 0) + 1
        for value, count in sorted(seen.items()):
            if count > 1:
                findings.append(
                    taxonomy.Finding(
                        target=value,
                        result_class=taxonomy.PARSER_ERROR,
                        detail=f"duplicate {key} declared {count} times",
                    )
                )
    return findings


def evaluate(
    *,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    spec_dir: Path = DEFAULT_SPEC_DIR,
    repo_root: Path = _REPO_ROOT,
    require_local_specs: bool = False,
) -> list[taxonomy.Finding]:
    try:
        entries = load_manifest(manifest_path)
    except ManifestError as exc:
        return [
            taxonomy.Finding(
                target=str(manifest_path.name),
                result_class=taxonomy.PARSER_ERROR,
                detail=str(exc),
            )
        ]
    findings = [
        evaluate_entry(
            entry,
            repo_root=repo_root,
            spec_dir=spec_dir,
            require_local_specs=require_local_specs,
        )
        for entry in entries
    ]
    findings.extend(duplicate_findings(entries))
    findings.extend(undeclared_spec_findings(entries, repo_root=repo_root, spec_dir=spec_dir))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--spec-dir", type=Path, default=DEFAULT_SPEC_DIR)
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    parser.add_argument(
        "--require-local-specs",
        action="store_true",
        help="Treat a declared spec file missing from disk as source_removed.",
    )
    taxonomy.add_common_arguments(parser, default_artifact=DEFAULT_ARTIFACT_PATH)
    args = parser.parse_args(argv)

    findings = evaluate(
        manifest_path=args.manifest,
        spec_dir=args.spec_dir,
        repo_root=args.repo_root,
        require_local_specs=args.require_local_specs,
    )
    report = taxonomy.build_report(
        CHECK_NAME,
        findings,
        refresh_sources=False,
        exit_code_mode=args.exit_code_mode,
        notes=(
            "Locally derived only: branch/spec_uri agreement, sidebar-section "
            "membership, output-path convention, on-disk path_count and sha256. "
            "No portal fetch is performed, so absent git-ignored specs are "
            "not_checked rather than fresh."
        ),
    )
    taxonomy.print_report(report)
    if not args.no_artifact and args.json_artifact:
        print(f"wrote {taxonomy.write_report(args.json_artifact, report)}")
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
