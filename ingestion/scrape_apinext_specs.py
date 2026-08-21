"""Harvest OpenAPI specs for the non-Central Aruba developer-portal products.

The portal hosts nine ReadMe projects. ``new-central``/``new-central-config``
are covered by ``scrape_openapi.py`` + ``fetch_manifest_specs.py``; the other
seven (AOS-CX, AOS-8, Central 2.x, ClearPass, UXI, Fabric Composer,
EdgeConnect) were previously unindexed.

Those projects run ReadMe's *api-next v2* API rather than the older SuperHub
layout, so there is no ``oasPublicUrl`` pointer to scrape. The admin
``/apis`` listing endpoint is 403 for anonymous callers, but the per-page
reference endpoint is public and embeds the **entire** OpenAPI document under
``data.api.schema``. One fetch per spec is therefore enough.

Discovery flow per project:

1. ``GET /{project}/api-next/v2/branches`` -> current branch name.
2. ``GET /{project}/api-next/v2/branches/{ver}/sidebar?page_type=reference``
   -> nav tree. Operation pages are the nodes carrying ``api_method``.
3. Fetch one operation per sidebar *category* and dedupe on
   ``data.api.uri`` (the canonical spec filename), since every operation in a
   category shares one spec.

Specs land in ``ingestion/sources/product_specs/`` -- deliberately *not*
``openapi_specs/``, which is Central-only and feeds the generated Central
client/manifest.

Usage:
    python ingestion/scrape_apinext_specs.py [--sections aoscx,cppm] [--limit N] [--force]
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "ingestion" / "sources" / "product_specs"
MANIFEST_PATH = REPO_ROOT / "ingestion" / "product_specs_manifest.json"

BASE = "https://developer.arubanetworks.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)

# portal section -> ReadMe project slug
PROJECTS: dict[str, str] = {
    "aoscx": "aruba-aoscx",
    "aos8": "aruba-aos",
    "central": "aruba-central",
    "cppm": "aruba-cppm",
    "uxi": "aruba-uxi",
    "afc": "aruba-fabric-composer",
    "edgeconnect": "aruba-edgeconnect",
}

# A spec with no paths means the page rendered but the schema did not load;
# writing it would silently poison the corpus with an empty document.
MIN_PATHS = 1


def slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return re.sub(r"-{2,}", "-", value) or "spec"


def collect_operation_nodes(nodes: Any, acc: list[dict]) -> list[dict]:
    """Walk the sidebar tree collecting nodes that represent API operations."""
    if isinstance(nodes, dict):
        nodes = [nodes]
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        if node.get("slug") and node.get("api_method"):
            acc.append(node)
        for key in ("pages", "children"):
            if node.get(key):
                collect_operation_nodes(node[key], acc)
    return acc


def harvest_project(page, section: str, project: str, *, force: bool, limit: int) -> list[dict]:
    """Fetch every distinct spec backing ``project``. Returns manifest entries."""

    def get_json(path: str) -> Any:
        return page.evaluate(
            """async u => {
                const r = await fetch(u, {headers: {accept: 'application/json'}});
                if (!r.ok) return {__err: r.status};
                return await r.json();
            }""",
            BASE + path,
        )

    branches = get_json(f"/{project}/api-next/v2/branches")
    if not isinstance(branches, dict) or branches.get("__err") or not branches.get("data"):
        print(f"  {section}: cannot resolve branch ({branches})")
        return []
    version = branches["data"][0]["name"]

    sidebar = get_json(f"/{project}/api-next/v2/branches/{version}/sidebar?page_type=reference")
    if isinstance(sidebar, dict) and sidebar.get("__err"):
        print(f"  {section}: sidebar error {sidebar['__err']}")
        return []

    operations = collect_operation_nodes(sidebar, [])
    # One operation per category is enough: a category maps onto one spec.
    by_category: dict[str, dict] = {}
    for node in operations:
        by_category.setdefault(node.get("category") or node["slug"], node)
    print(
        f"  {section}: branch {version}, {len(operations)} operations, "
        f"{len(by_category)} categories to probe"
    )

    entries: list[dict] = []
    seen_uris: set[str] = set()
    probed = 0
    for node in by_category.values():
        if limit and probed >= limit:
            break
        probed += 1
        slug = node["slug"]
        payload = get_json(
            f"/{project}/api-next/v2/branches/{version}/reference/{slug}?reduce=false"
        )
        if not isinstance(payload, dict) or payload.get("__err"):
            print(f"    FAIL {slug}: {payload.get('__err') if isinstance(payload, dict) else '?'}")
            continue
        api = (payload.get("data") or {}).get("api") or {}
        uri = api.get("uri")
        spec = api.get("schema")
        if not uri or uri in seen_uris:
            continue
        seen_uris.add(uri)
        if not isinstance(spec, dict) or len(spec.get("paths") or {}) < MIN_PATHS:
            print(f"    SKIP {uri}: no paths in schema")
            continue

        title = (spec.get("info") or {}).get("title") or Path(uri).stem
        out_name = f"{section}-{slugify(Path(uri).stem)}.json"
        out_path = OUT_DIR / out_name
        if out_path.exists() and not force:
            print(f"    have {out_name}")
        else:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(spec, indent=2, sort_keys=True), encoding="utf-8")
            print(f"    OK   {out_name} ({len(spec['paths'])} paths) {title}")
        entries.append(
            {
                "section": section,
                "project": project,
                "branch": version,
                "spec_uri": uri,
                "title": title,
                "path_count": len(spec["paths"]),
                "output_path": str(out_path.relative_to(REPO_ROOT)),
                "source_url": f"{BASE}/{section}/reference/{slug}",
            }
        )
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sections", default="", help="comma-separated subset of portal sections")
    parser.add_argument("--limit", type=int, default=0, help="max categories probed per project")
    parser.add_argument("--force", action="store_true", help="rewrite specs already on disk")
    args = parser.parse_args()

    wanted = [s.strip() for s in args.sections.split(",") if s.strip()] or list(PROJECTS)
    unknown = [s for s in wanted if s not in PROJECTS]
    if unknown:
        print(f"unknown sections: {unknown}; known: {list(PROJECTS)}")
        return 2

    from playwright.sync_api import sync_playwright

    all_entries: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()
        # Same-origin document required before fetch() calls are allowed.
        page.goto(f"{BASE}/aoscx/reference", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        for section in wanted:
            try:
                all_entries.extend(
                    harvest_project(
                        page, section, PROJECTS[section], force=args.force, limit=args.limit
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one bad project must not abort the rest
                print(f"  {section}: ERROR {exc}")
        browser.close()

    if all_entries:
        MANIFEST_PATH.write_text(
            json.dumps(
                {"specs": sorted(all_entries, key=lambda e: e["output_path"])},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    total_paths = sum(e["path_count"] for e in all_entries)
    print(f"\n{len(all_entries)} specs, {total_paths} paths -> {OUT_DIR}")
    return 0 if all_entries else 1


if __name__ == "__main__":
    raise SystemExit(main())
