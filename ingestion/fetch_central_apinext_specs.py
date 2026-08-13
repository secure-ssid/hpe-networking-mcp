#!/usr/bin/env python3
"""Fetch Central OpenAPI specs the registry manifest does not already cover.

``fetch_manifest_specs.py`` pulls the 30 registries named in
``openapi_registry_manifest.json`` by id. That manifest was built from the
Central reference portal, but it is not exhaustive: a handful of the specs the
portal serves have no registry entry, so they never land on disk.

Enumerating the ``new-central`` and ``new-central-config`` sidebars turns up 32
distinct specs carrying 2,725 operations, and 2,713 of those are already on
disk under registry-suffixed names (``monitoring-7wjo8aumqq.json`` is the same
document as ``network-monitoring-final-openapi.json``). Writing all 32 would
therefore add ~200 duplicate operations for every genuinely new one, which
inflates the index and gives retrieval many near-identical documents to choose
between.

So this script writes a spec only when *every* operation in it is new. A
partial overlap means it is the same document as one already on disk, just
renamed or a little further ahead, and is reported as drift instead. On the
26.04/26.06 branches the wholly-new specs are
``authorization.json`` (the ``POST /as/token.oauth2`` endpoint every Central
API call has to go through first, which was missing entirely) and
``network-msp-final-openapi.json`` (MSP tenant listing).

Specs that merely drift ahead of a manifest-pinned registry are deliberately
left alone: those files are reproducible from the registry id, and quietly
rewriting them here would mean two scripts disagree about the same filename.
Use ``--report`` to see that drift without writing anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / "ingestion" / "sources" / "openapi_specs"

ORIGIN = "https://developer.arubanetworks.com"
PROJECTS = ("new-central", "new-central-config")

HTTP_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
)

# A spec with no paths means the page rendered but the schema never loaded.
MIN_PATHS = 1

_BRANCHES_JS = """async project => {
  const res = await fetch(`/${project}/api-next/v2/branches`);
  if (res.status !== 200) return null;
  const body = await res.json();
  return (body.data || []).length ? body.data[0].name : null;
}"""

_SIDEBAR_JS = """async ([project, branch]) => {
  const res = await fetch(
    `/${project}/api-next/v2/branches/${branch}/sidebar?page_type=reference`
  );
  if (res.status !== 200) return null;
  return await res.json();
}"""

_REFERENCE_JS = """async ([project, branch, slug]) => {
  const res = await fetch(
    `/${project}/api-next/v2/branches/${branch}/reference/${slug}?reduce=false`
  );
  if (res.status !== 200) return null;
  const body = await res.json();
  const api = (body.data || {}).api || {};
  if (!api.uri || !api.schema) return null;
  return {uri: api.uri, schema: api.schema};
}"""


def operations(spec: dict) -> set[tuple[str, str]]:
    """Return the (METHOD, path) pairs a spec describes."""
    found: set[tuple[str, str]] = set()
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method in item:
            if method.lower() in HTTP_METHODS:
                found.add((method.upper(), path))
    return found


def collect_category_slugs(sidebar) -> dict[str, str]:
    """Map each sidebar category to one operation slug beneath it.

    Every operation page in a category resolves to the same spec, so one slug
    per category is enough and avoids ~1,300 redundant fetches. Only nodes
    carrying ``api_method`` are operations; the rest are folders.
    """
    first_slug: dict[str, str] = {}

    def walk(node, category=None) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child, category)
            return
        if not isinstance(node, dict):
            return
        slug = node.get("slug")
        group = category or slug
        if slug and node.get("api_method") and group not in first_slug:
            first_slug[group] = slug
        for key in ("pages", "children"):
            for child in node.get(key) or []:
                walk(child, group)

    walk(sidebar)
    return first_slug


def existing_operations(output_dir: Path) -> set[tuple[str, str]]:
    covered: set[tuple[str, str]] = set()
    for path in sorted(output_dir.glob("*.json")):
        try:
            covered |= operations(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  WARN unreadable {path.name}: {exc}")
    return covered


def harvest(page, project: str) -> dict[str, dict]:
    """Return {filename: schema} for every distinct spec the project serves."""
    page.goto(f"{ORIGIN}/{project}/reference/", timeout=90_000, wait_until="domcontentloaded")
    page.wait_for_timeout(2_500)

    branch = page.evaluate(_BRANCHES_JS, project)
    if not branch:
        print(f"  {project}: no current branch")
        return {}

    sidebar = page.evaluate(_SIDEBAR_JS, [project, branch])
    if not sidebar:
        print(f"  {project}: no reference sidebar")
        return {}

    slugs = collect_category_slugs(sidebar)
    specs: dict[str, dict] = {}
    for slug in slugs.values():
        result = page.evaluate(_REFERENCE_JS, [project, branch, slug])
        if not result:
            continue
        name = result["uri"].rsplit("/", 1)[-1]
        if name in specs:
            continue
        schema = result["schema"]
        if len(schema.get("paths") or {}) < MIN_PATHS:
            print(f"  {project}: skipping empty {name}")
            continue
        specs[name] = schema

    print(f"  {project} branch={branch}: {len(slugs)} categories -> {len(specs)} specs")
    return specs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        action="store_true",
        help="show coverage overlap for every spec without writing anything",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=OUTPUT_DIR, help="where specs are written"
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    covered = existing_operations(args.output_dir)
    print(f"on disk: {len(list(args.output_dir.glob('*.json')))} specs, {len(covered)} operations")

    from playwright.sync_api import sync_playwright

    harvested: dict[str, dict] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        page = browser.new_page()
        try:
            for project in PROJECTS:
                harvested.update(harvest(page, project))
        finally:
            browser.close()

    written = skipped = 0
    for name in sorted(harvested):
        ops = operations(harvested[name])
        new_ops = ops - covered
        if not new_ops:
            skipped += 1
            if args.report:
                print(f"  covered   {name}: all {len(ops)} operations already on disk")
            continue

        target = args.output_dir / name
        if len(new_ops) < len(ops):
            # A spec that only partly overlaps is the same document as one we
            # already hold, just further ahead or under a different name --
            # network-monitoring-final-openapi.json is monitoring-7wjo8aumqq.json
            # plus two endpoints. Writing it would add ~155 duplicate operations
            # to gain 2, so this is a pin-refresh question for whichever script
            # owns the existing file, not a coverage gap to fill here.
            skipped += 1
            print(
                f"  DRIFT     {name}: +{len(new_ops)} of {len(ops)} operations ahead "
                f"of an existing spec; leaving the on-disk copy alone"
            )
            continue

        if target.exists():
            skipped += 1
            print(f"  exists    {name}: already written")
            continue

        if args.report:
            print(f"  would add {name}: +{len(new_ops)} of {len(ops)} operations")
            continue

        target.write_text(json.dumps(harvested[name], indent=1) + "\n", encoding="utf-8")
        covered |= new_ops
        written += 1
        print(f"  ADDED     {name}: +{len(new_ops)} operations")

    print(f"\n{written} written, {skipped} skipped, {len(harvested)} harvested")
    return 0


if __name__ == "__main__":
    sys.exit(main())
