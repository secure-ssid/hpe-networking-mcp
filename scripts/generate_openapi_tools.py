#!/usr/bin/env python3
"""Generate (or verify) the committed generated-operation manifest for a platform.

Usage:
    uv run python scripts/generate_openapi_tools.py --platform mist \
        --spec ingestion/sources/openapi_specs/mist-openapi.json

    # Deterministic drift/check mode (CI): non-zero exit if the committed
    # manifest is stale relative to the current spec + generator.
    uv run python scripts/generate_openapi_tools.py --platform mist \
        --spec ingestion/sources/openapi_specs/mist-openapi.json --check

The raw upstream spec is never committed here (Mist's is gitignored under
ingestion/sources/); only the compact operation manifest is written to
src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/<platform>.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from hpe_networking_mcp.mcp_servers.openapi_gen import manifest as manifest_mod  # noqa: E402

# Default spec locations by platform (all gitignored; local-only).
_DEFAULT_SPECS: dict[str, str] = {
    "mist": "ingestion/sources/openapi_specs/mist-openapi.json",
}


def _load_spec(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    spec = json.loads(raw)
    return spec, manifest_mod.sha256_bytes(raw)


def build(platform: str, spec_path: Path) -> dict:
    spec, source_sha = _load_spec(spec_path)
    overrides = manifest_mod.load_overrides(platform)
    return manifest_mod.build_manifest(
        spec,
        platform=platform,
        source_file=spec_path.name,
        source_sha256=source_sha,
        overrides=overrides,
    )


def build_many(platform: str, spec_paths: list[Path]) -> dict:
    documents = []
    for path in sorted(spec_paths):
        spec, source_sha = _load_spec(path)
        documents.append((path.name, source_sha, spec))
    return manifest_mod.build_merged_manifest(
        documents,
        platform=platform,
        overrides=manifest_mod.load_overrides(platform),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--platform", required=True, help="platform key, e.g. 'mist'")
    ap.add_argument("--spec", action="append", help="OpenAPI spec path; repeat for many specs")
    ap.add_argument("--spec-dir", help="directory containing OpenAPI JSON specs")
    ap.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="filename to exclude from --spec-dir; repeat as needed",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="verify the committed manifest matches a fresh build; do not write",
    )
    args = ap.parse_args()

    spec_args = list(args.spec or [])
    if args.spec_dir:
        spec_dir = Path(args.spec_dir)
        if not spec_dir.is_absolute():
            spec_dir = _REPO_ROOT / spec_dir
        excluded = set(args.exclude)
        spec_args.extend(
            str(path)
            for path in sorted(spec_dir.glob("*.json"))
            if path.name not in excluded
        )
    if not spec_args:
        default_spec = _DEFAULT_SPECS.get(args.platform)
        if default_spec:
            spec_args.append(default_spec)
    if not spec_args:
        print(f"No spec path given and no default for platform {args.platform!r}", file=sys.stderr)
        return 2
    spec_paths: list[Path] = []
    for spec_arg in spec_args:
        spec_path = Path(spec_arg)
        if not spec_path.is_absolute():
            spec_path = _REPO_ROOT / spec_path
        if not spec_path.exists():
            print(f"Spec not found: {spec_path}", file=sys.stderr)
            return 2
        spec_paths.append(spec_path)

    manifest = build(args.platform, spec_paths[0]) if len(spec_paths) == 1 else build_many(
        args.platform, spec_paths
    )
    rendered = manifest_mod.dumps(manifest)
    out_path = manifest_mod.manifest_path(args.platform)

    if args.check:
        if not out_path.exists():
            print(f"DRIFT: manifest missing at {out_path.relative_to(_REPO_ROOT)}", file=sys.stderr)
            return 1
        current = out_path.read_text()
        if current != rendered:
            print(
                f"DRIFT: committed manifest for {args.platform!r} is stale. "
                f"Re-run without --check to regenerate.",
                file=sys.stderr,
            )
            return 1
        print(
            f"OK: {args.platform} manifest current "
            f"({manifest['source']['operation_count']} operations, "
            f"source sha256 {manifest['source']['sha256'][:12]})."
        )
        return 0

    manifest_mod.write_manifest(args.platform, manifest)
    print(
        f"Wrote {out_path.relative_to(_REPO_ROOT)} "
        f"({manifest['source']['operation_count']} operations, "
        f"source sha256 {manifest['source']['sha256'][:12]})."
    )
    if manifest["override_keys_unmatched"]:
        print(
            f"Warning: {len(manifest['override_keys_unmatched'])} override key(s) "
            f"did not match any operation: {manifest['override_keys_unmatched'][:5]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
