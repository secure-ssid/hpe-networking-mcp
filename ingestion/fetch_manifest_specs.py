"""Fetch every OpenAPI spec declared in ``openapi_registry_manifest.json``.

The manifest is the committed record of which ReadMe api-registry documents
back the New Central portal, but the spec files themselves are large and are
not always present in a fresh checkout. Re-crawling reference pages to
rediscover the registry ids is slow and brittle (the portal has changed page
layout twice); the manifest already stores each ``registry_id``, so this
script fetches directly from it.

Usage:
    python ingestion/fetch_manifest_specs.py [--force] [--limit N]

By default already-present spec files are skipped.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from ingestion.readme_registry import (  # noqa: E402
    OasPointer,
    RegistryFetchError,
    fetch_registry_spec,
)

MANIFEST_PATH = REPO_ROOT / "ingestion" / "openapi_registry_manifest.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="refetch specs already on disk")
    parser.add_argument("--limit", type=int, default=0, help="stop after N fetches (0 = all)")
    args = parser.parse_args()

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    registries: dict[str, dict] = manifest.get("registries", {})
    print(f"manifest declares {len(registries)} registries")

    fetched = skipped = failed = 0
    for registry_id, entry in sorted(registries.items()):
        out_path = REPO_ROOT / entry["output_path"]
        if out_path.exists() and not args.force:
            skipped += 1
            continue
        if args.limit and fetched >= args.limit:
            break
        pointer = OasPointer(
            project=entry.get("project", "aruba-new-central"),
            version=entry.get("portal_version", ""),
            registry_id=registry_id,
        )
        try:
            spec = fetch_registry_spec(pointer)
        except RegistryFetchError as exc:
            print(f"  FAIL {entry.get('title')}: {exc}")
            failed += 1
            continue
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(spec, indent=2, sort_keys=True), encoding="utf-8")
        print(
            f"  OK   {entry.get('title')} -> {out_path.name} "
            f"({len(spec.get('paths', {}))} paths)"
        )
        fetched += 1

    print(f"\nfetched={fetched} skipped={skipped} failed={failed}")
    return 1 if failed and not fetched else 0


if __name__ == "__main__":
    raise SystemExit(main())
