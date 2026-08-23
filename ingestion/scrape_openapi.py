#!/usr/bin/env python3
"""Fetch OpenAPI specs from the Aruba developer portal's ReadMe SuperHub registry.

Replaces the pre-July-2026 approach of pulling per-slug JSON files from
``internal-ui.central.arubanetworks.com/cnxconfig/docs/`` -- that host was
retired when the portal migrated to ReadMe's SuperHub platform. The
SuperHub serves one OpenAPI document per operation *category*
(``x-tag-group``), referenced from each reference page via an embedded
``oasPublicUrl`` pointer. See ``ingestion/readme_registry.py`` for the
parsing/fetching helpers this script is built on.

Usage:
    python ingestion/scrape_openapi.py [--urls PATH] [--limit N]

    --urls   JSON file containing a list of developer-portal reference
             page URLs to crawl. Defaults to ingestion/cfg_urls_raw.json.
    --limit  Only process the first N URLs (useful for a quick smoke run).

Writes specs to ingestion/sources/openapi_specs/<category-slug>.json (the
directory hpe_networking_mcp.pipeline.clients.specs_index already indexes) and records a
source URL / project / version / sha256 / fetched-at manifest at
ingestion/openapi_registry_manifest.json for CI drift detection
(scripts/check_openapi_drift.py).

Many reference pages share the same registry id (one category covers many
operations), so this dedupes: each unique registry is fetched exactly
once no matter how many seed URLs point at it.
"""
from __future__ import annotations

import os
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

import argparse  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: E402
from pathlib import Path  # noqa: E402
from threading import Lock  # noqa: E402

from ingestion.readme_registry import (  # noqa: E402
    OasPointer,
    RegistryFetchError,
    build_registry_entry,
    extract_oas_pointer,
    fetch_page_html,
    fetch_registry_spec,
    load_manifest,
    registry_slug,
    save_manifest,
    upsert_registry_entry,
)

INGESTION_DIR = Path(__file__).parent
DEFAULT_URLS_PATH = INGESTION_DIR / "cfg_urls_raw.json"
OUTPUT_DIR = INGESTION_DIR / "sources" / "openapi_specs"
MANIFEST_PATH = INGESTION_DIR / "openapi_registry_manifest.json"


def _load_seed_urls(path: Path) -> list[str]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list of URLs")
    return [str(url) for url in data]


def _resolve_pointer(url: str) -> tuple[str, OasPointer | None, str | None]:
    """Fetch ``url`` and parse its pointer. Returns (url, pointer_or_None, error_or_None)."""
    try:
        html = fetch_page_html(url)
        pointer = extract_oas_pointer(html)
        return url, pointer, None
    except RegistryFetchError as exc:
        return url, None, str(exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--urls", type=Path, default=DEFAULT_URLS_PATH)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args(argv)

    urls = _load_seed_urls(args.urls)
    if args.limit is not None:
        urls = urls[: args.limit]
    print(f"Resolving oasPublicUrl pointers for {len(urls)} reference pages...")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(MANIFEST_PATH)

    seen_registries: dict[str, str] = {}  # registry_id -> first source URL that named it
    page_errors: list[tuple[str, str]] = []
    fetch_errors: list[tuple[str, str]] = []
    written: list[str] = []
    write_lock = Lock()

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_resolve_pointer, url): url for url in urls}
        for future in as_completed(futures):
            url, pointer, error = future.result()
            done += 1
            if done % 100 == 0 or done == len(urls):
                print(f"  [{done}/{len(urls)}] pages resolved")
            if pointer is None:
                page_errors.append((url, error or "unknown error"))
                continue
            with write_lock:
                already_seen = pointer.registry_id in seen_registries
                if not already_seen:
                    seen_registries[pointer.registry_id] = url
            if already_seen:
                continue

            try:
                spec = fetch_registry_spec(pointer)
            except RegistryFetchError as exc:
                fetch_errors.append((pointer.registry_id, str(exc)))
                continue

            slug = registry_slug(pointer, spec)
            out_path = OUTPUT_DIR / f"{slug}.json"
            out_path.write_text(json.dumps(spec, indent=1))
            entry = build_registry_entry(
                pointer,
                spec,
                source_url=url,
                output_path=os.path.relpath(out_path, start=INGESTION_DIR.parent),
            )
            with write_lock:
                upsert_registry_entry(manifest, entry)
                written.append(slug)
            print(f"  OK {slug} ({entry['path_count']} paths) <- {url}")

    save_manifest(MANIFEST_PATH, manifest)

    print(
        f"\nDone. {len(written)} registries written, "
        f"{len(seen_registries)} unique registries seen, "
        f"{len(page_errors)} page errors, {len(fetch_errors)} registry-fetch errors."
    )
    for url, error in page_errors[:20]:
        print(f"  PAGE ERROR {url}: {error}")
    for registry_id, error in fetch_errors[:20]:
        print(f"  REGISTRY ERROR {registry_id}: {error}")
    print(f"Manifest: {MANIFEST_PATH}")
    print("Rebuild the index: python -m hpe_networking_mcp.pipeline.clients.specs_index --build")

    return 1 if fetch_errors else 0


if __name__ == "__main__":
    sys.exit(main())
