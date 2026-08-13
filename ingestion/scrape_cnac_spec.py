#!/usr/bin/env python3
"""Fetch the Central NAC (client-registration) OpenAPI spec via ReadMe SuperHub.

The CNAC client-registration API (cnac-mac-reg, cnac-visitor,
cnac-named-mpsk-reg, cnac-dpp-reg, certificates, jobs) is not served by the
same registry as the main config API. This used to work by regex-scraping
an embedded ``oasDefinition`` blob out of the reference page's HTML -- the
July 2026 ReadMe SuperHub migration removed that blob entirely. The page
now embeds an ``oasPublicUrl`` pointer instead; see
``ingestion/readme_registry.py`` for the shared parse/fetch/hash logic this
script (and ``scrape_openapi.py``) are both built on.

Usage: python ingestion/scrape_cnac_spec.py
Writes: ingestion/sources/openapi_specs/cnac-client-registration.json
        (plus a manifest entry in ingestion/openapi_registry_manifest.json)
Then rebuild the index: python -m hpe_networking_mcp.pipeline.clients.specs_index --build
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# Support running this file directly (`python ingestion/<script>.py`), where
# Python puts this file's own directory on sys.path[0] rather than the repo
# root -- without this, `from ingestion.readme_registry import ...` below
# fails with ModuleNotFoundError even though `python -m ingestion.<script>`
# or a plain `import ingestion.<script>` both work fine.
_REPO_ROOT = _Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_REPO_ROOT))
    _sys.path.insert(0, str(_REPO_ROOT / "src"))

import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

from ingestion.readme_registry import (  # noqa: E402
    RegistryFetchError,
    build_registry_entry,
    fetch_spec_for_page,
    load_manifest,
    save_manifest,
    upsert_registry_entry,
)

PAGE_URL = "https://developer.arubanetworks.com/new-central-config/reference/mac-registration"
INGESTION_DIR = Path(__file__).parent
OUT_PATH = INGESTION_DIR / "sources" / "openapi_specs" / "cnac-client-registration.json"
MANIFEST_PATH = INGESTION_DIR / "openapi_registry_manifest.json"


def main() -> int:
    try:
        pointer, spec = fetch_spec_for_page(PAGE_URL)
    except RegistryFetchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    info = spec.get("info", {})
    print(
        f"Fetched {info.get('title')!r} v{info.get('version')} "
        f"(registry {pointer.registry_id}, portal {pointer.project}/{pointer.version}): "
        f"{len(spec.get('paths', {}))} paths, "
        f"{len(spec.get('components', {}).get('schemas', {}))} schemas"
    )

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(spec, indent=1))
    print(f"Wrote {OUT_PATH}")

    manifest = load_manifest(MANIFEST_PATH)
    entry = build_registry_entry(
        pointer,
        spec,
        source_url=PAGE_URL,
        output_path=os.path.relpath(OUT_PATH, start=INGESTION_DIR.parent),
    )
    upsert_registry_entry(manifest, entry)
    save_manifest(MANIFEST_PATH, manifest)
    print(f"Manifest updated: {MANIFEST_PATH}")

    print("Rebuild the index: python -m hpe_networking_mcp.pipeline.clients.specs_index --build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
