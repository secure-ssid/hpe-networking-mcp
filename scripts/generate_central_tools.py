#!/usr/bin/env python3
"""Generate (or verify) the committed merged Aruba Central operation manifest.

Unlike the single-spec ``generate_openapi_tools.py`` path, Central's surface is
spread across the whole Aruba ReadMe OpenAPI registry (hundreds of small config
node specs plus the network-config aggregate). This helper:

* loads every ``*.json`` OpenAPI document under the spec directory
  (``ingestion/sources/openapi_specs`` by default, gitignored / local-only),
  excluding the MIT-licensed Mist spec that has its own manifest;
* normalizes each document's server base path onto its operation paths so that
  relative-server config specs (``servers: [{"url": "/network-config/v1alpha1"}]``
  with bare ``/bgp`` paths) collapse onto the identical full-path operations from
  the absolute-host specs -- this is what makes ``build_merged_manifest`` dedupe
  them and what yields correct runtime request paths;
* merges them deterministically via
  :func:`hpe_networking_mcp.mcp_servers.openapi_gen.manifest.build_merged_manifest`, keeping one
  copy of each unique ``METHOD /path`` and recording per-source provenance,
  digests, and the dropped duplicates;
* writes / drift-checks ``src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/central.json``.

No raw upstream spec is ever committed here; only the compact operation
manifest is written.

Usage::

    uv run python scripts/generate_central_tools.py            # write
    uv run python scripts/generate_central_tools.py --check    # CI drift gate
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from hpe_networking_mcp.mcp_servers.openapi_gen import manifest as manifest_mod  # noqa: E402

PLATFORM = "central"
DEFAULT_SPEC_DIR = _REPO_ROOT / "ingestion" / "sources" / "openapi_specs"
# The Mist spec is MIT-licensed and generated under its own platform manifest.
EXCLUDED_SPEC_NAMES = frozenset({"mist-openapi.json"})


def server_base_path(spec: dict[str, Any]) -> str:
    """Return the leading path component of the first declared server URL.

    Absolute host URLs (``https://host``) contribute an empty base -- their
    operation paths are already full. Relative servers (``/network-config/
    v1alpha1``) contribute that prefix, which we prepend to each path.
    """
    servers = spec.get("servers") or []
    if not servers or not isinstance(servers, list):
        return ""
    first = servers[0]
    url = str(first.get("url", "")) if isinstance(first, dict) else ""
    return urlsplit(url).path.rstrip("/")


def normalize_central_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Prepend the server base path onto every operation path.

    Returns the spec unchanged when there is no relative server base, so
    absolute-host specs (already carrying full paths) are untouched. The
    resulting document has a single canonical path per operation, which lets
    the merge step deduplicate identical operations across sibling specs.
    """
    base = server_base_path(spec)
    paths = spec.get("paths")
    if not base or not isinstance(paths, dict):
        return spec
    rewritten: dict[str, Any] = {}
    for path, item in paths.items():
        suffix = path if str(path).startswith("/") else f"/{path}"
        rewritten[f"{base}{suffix}"] = item
    out = dict(spec)
    out["paths"] = rewritten
    return out


def iter_spec_files(spec_dir: Path) -> list[Path]:
    return [
        path
        for path in sorted(spec_dir.glob("*.json"))
        if path.name not in EXCLUDED_SPEC_NAMES
    ]


def build_central_manifest(spec_dir: Path) -> dict[str, Any]:
    documents: list[tuple[str, str, dict[str, Any]]] = []
    for path in iter_spec_files(spec_dir):
        raw = path.read_bytes()
        spec = json.loads(raw)
        documents.append((path.name, manifest_mod.sha256_bytes(raw), normalize_central_spec(spec)))
    if not documents:
        raise SystemExit(f"No Central specs found under {spec_dir}")
    return manifest_mod.build_merged_manifest(
        documents,
        platform=PLATFORM,
        overrides=manifest_mod.load_overrides(PLATFORM),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--spec-dir",
        default=str(DEFAULT_SPEC_DIR),
        help="directory of Aruba Central OpenAPI JSON specs (local-only)",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="verify the committed manifest matches a fresh build; do not write",
    )
    args = ap.parse_args()

    spec_dir = Path(args.spec_dir)
    if not spec_dir.is_absolute():
        spec_dir = _REPO_ROOT / spec_dir
    if not spec_dir.exists():
        print(f"Spec directory not found: {spec_dir}", file=sys.stderr)
        return 2

    manifest = build_central_manifest(spec_dir)
    rendered = manifest_mod.dumps(manifest)
    out_path = manifest_mod.manifest_path(PLATFORM)

    if args.check:
        if not out_path.exists():
            print(f"DRIFT: manifest missing at {out_path.relative_to(_REPO_ROOT)}", file=sys.stderr)
            return 1
        if out_path.read_text() != rendered:
            print(
                "DRIFT: committed Central manifest is stale. "
                "Re-run without --check to regenerate.",
                file=sys.stderr,
            )
            return 1
        print(
            f"OK: Central manifest current ({manifest['source']['operation_count']} operations, "
            f"{manifest['source']['duplicate_operation_count']} duplicates dropped, "
            f"source sha256 {manifest['source']['sha256'][:12]})."
        )
        return 0

    manifest_mod.write_manifest(PLATFORM, manifest)
    print(
        f"Wrote {out_path.relative_to(_REPO_ROOT)} "
        f"({manifest['source']['operation_count']} operations, "
        f"{manifest['source']['duplicate_operation_count']} duplicates dropped, "
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
