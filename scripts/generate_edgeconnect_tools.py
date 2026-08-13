#!/usr/bin/env python3
"""Check a target EdgeConnect Swagger/OpenAPI document and optionally generate.

The default action is read-only: compare a user-supplied local document with
the committed 1,216-operation manifest and print a deterministic compatibility
report. The manifest is overwritten only when ``--generate`` is explicit and
every compatibility check succeeds.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from hpe_networking_mcp.mcp_servers.openapi_gen.compatibility import (  # noqa: E402
    CompatibilityError,
    build_compatibility_report,
    dumps_report,
)
from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import (  # noqa: E402
    build_manifest,
    dumps,
    manifest_path,
    override_path,
    sha256_bytes,
)

PLATFORM = "edgeconnect"
PROVENANCE_PATH = (
    _REPO_ROOT / "src/hpe_networking_mcp/mcp_servers/openapi_gen/provenance/edgeconnect.json"
)


def _overrides() -> dict[str, str]:
    document = json.loads(override_path(PLATFORM).read_text())
    overrides = {
        str(key): str(value) for key, value in document["capabilities"].items()
    }
    overrides.update(
        {str(key): "diagnostic" for key in document.get("diagnostics", [])}
    )
    return overrides


def _load_baseline() -> tuple[dict, bytes, dict]:
    path = manifest_path(PLATFORM)
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw)
        provenance = json.loads(PROVENANCE_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CompatibilityError(f"cannot load EdgeConnect compatibility baseline: {exc}") from exc
    return manifest, raw, provenance


def compatibility_report(
    source: Path, *, expected_source_sha256: str | None = None
) -> dict:
    payload = source.read_bytes()
    manifest, manifest_bytes, provenance = _load_baseline()
    return build_compatibility_report(
        payload=payload,
        source_name=source.name,
        baseline_manifest=manifest,
        baseline_manifest_bytes=manifest_bytes,
        provenance=provenance,
        expected_source_sha256=expected_source_sha256,
    )


def _generate(source: Path, payload: bytes, baseline: dict, provenance: dict) -> None:
    from hpe_networking_mcp.mcp_servers.openapi_gen.compatibility import load_api_document

    document = load_api_document(payload)
    source_sha256 = sha256_bytes(payload)
    manifest = build_manifest(
        document,
        platform=PLATFORM,
        source_file=source.name,
        source_sha256=source_sha256,
        overrides=_overrides(),
    )

    baseline_by_key = {
        operation["key"]: operation for operation in baseline["operations"]
    }
    for operation in manifest["operations"]:
        previous = baseline_by_key[operation["key"]]
        operation["name"] = previous["name"]
        operation["capability"] = previous["capability"]

    manifest["source"].update(
        {
            "artifact_name": source.name,
            "provenance": (
                "Generated from a user-supplied target Orchestrator API document "
                "after fail-closed compatibility validation. The raw document is "
                "not copied into this repository."
            ),
        }
    )
    manifest["reviewed_capability_counts"] = dict(
        sorted(Counter(op["capability"] for op in manifest["operations"]).items())
    )
    rendered = dumps(manifest)
    manifest_path(PLATFORM).write_text(rendered)

    updated_provenance = dict(provenance)
    updated_provenance["manifest_sha256"] = sha256_bytes(rendered.encode())
    updated_provenance["operation_count"] = len(manifest["operations"])
    updated_provenance["source_sha256"] = source_sha256
    PROVENANCE_PATH.write_text(
        json.dumps(updated_provenance, indent=2, ensure_ascii=False) + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        required=True,
        type=Path,
        help="local target Orchestrator Swagger/OpenAPI JSON or YAML document",
    )
    parser.add_argument(
        "--expect-sha256",
        help="optional expected SHA-256 for the local source; mismatch fails closed",
    )
    parser.add_argument(
        "--report-output",
        type=Path,
        help="optional local path for the deterministic JSON report",
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="explicitly replace the committed manifest after successful validation",
    )
    args = parser.parse_args()

    if not args.source.is_file():
        print(f"EdgeConnect API document not found: {args.source}", file=sys.stderr)
        return 2
    expected_sha = args.expect_sha256.lower() if args.expect_sha256 else None
    if expected_sha is not None and (
        len(expected_sha) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha)
    ):
        print("--expect-sha256 must be a 64-character hexadecimal digest", file=sys.stderr)
        return 2

    try:
        payload = args.source.read_bytes()
        baseline, baseline_bytes, provenance = _load_baseline()
        report = build_compatibility_report(
            payload=payload,
            source_name=args.source.name,
            baseline_manifest=baseline,
            baseline_manifest_bytes=baseline_bytes,
            provenance=provenance,
            expected_source_sha256=expected_sha,
        )
    except (OSError, CompatibilityError) as exc:
        print(f"EdgeConnect compatibility check failed closed: {exc}", file=sys.stderr)
        return 2

    rendered_report = dumps_report(report)
    if args.report_output:
        args.report_output.write_text(rendered_report)
    print(rendered_report, end="")

    if not report["verdict"]["compatible"]:
        return 1
    if args.generate:
        _generate(args.source, payload, baseline, provenance)
        print(
            f"Generated {manifest_path(PLATFORM).relative_to(_REPO_ROOT)} "
            f"from validated source sha256 {report['source']['sha256']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
