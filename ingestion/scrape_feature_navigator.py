#!/usr/bin/env python3
"""
Scrape HPE Aruba Networking Feature Navigator's public JSON APIs into the
``feature_navigator`` RAG source.

Feature Navigator (https://feature-navigator.arubanetworking.hpe.com/) is a
Next.js app; its page HTML ships no data (client-rendered), but the compiled
JS bundles reference a small, unauthenticated public API that returns the
real comparison data the UI renders:

* ``GET /api/productInfo`` -> every AOS-CX switch platform (25 as of
  writing: CX 10000 down to legacy 2530/2540/3810/5400R), each with its
  minimum and full list of supported AOS-CX releases.
* ``GET /api/switchReleaseCompatibility?productId=<id>&productReleaseNames=<csv>&licenses=1,2,3``
  -> the full per-feature Yes/No/blank support matrix for that platform
  across the requested releases (889 rows across 28 feature categories for
  CX 6300 alone). ``licenses`` selects AOS-CX license tiers
  (1=Native, 2=Advanced, 3=Premium); passing all three returns every row
  regardless of which tier gates it.
* ``GET /api/releases`` -> the AOS10 (AP/gateway/Mobility Controller)
  release list.
* ``GET /api/releaseCompatibility?ids=<release-id-csv>&releases=<release-name-csv>``
  -> the AOS10 feature/release matrix (204 rows across 22 categories as of
  writing) — AOS10 is one shared OS image, so this is not split per
  hardware platform the way AOS-CX is.

Only the *latest* release is requested per AOS-CX switch platform (a
current-state snapshot, matching this repo's hardware_specs.py convention
of documenting current specs rather than full version history), while the
AOS10 side keeps every published release since the response stays small
(~40KB) and shows which release introduced a feature.

This was previously a declared-but-unfilled RAG source
(ingestion/source_manifest.json's ``feature_navigator`` entry had
``scraper: null`` because the page was assumed to require a browser-driven
export). No login, cookies, or Playwright are required — plain HTTP is
sufficient.

Usage:
    uv run python ingestion/scrape_feature_navigator.py
Writes:
    ingestion/sources/feature_navigator/cx-<slug>.md   (one per switch platform)
    ingestion/sources/feature_navigator/aos10-features.md
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = "https://feature-navigator.arubanetworking.hpe.com"
OUTPUT_DIR = Path(__file__).parent / "sources" / "feature_navigator"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}

# All three AOS-CX license tiers, so the matrix includes every feature
# regardless of which tier gates it (Native=1, Advanced=2, Premium=3).
ALL_LICENSES = "1,2,3"

# The productInfo/switchReleaseCompatibility responses are copy/pasted from a
# spreadsheet upstream and carry stray whitespace on some release strings
# (e.g. "10.04.1000\n", "10.08.0001 ") that must not survive into a query
# string or a de-duplication set.
def _clean(value: str) -> str:
    return value.strip()


#: A real AOS-CX release is purely dot-separated digits (e.g. "10.13.1000").
#: The upstream spreadsheet leaks two kinds of non-AOS-CX values into
#: supportedReleases: a literal "#REF!" broken-Excel-reference artifact (CX
#: 4100i), and legacy ProVision/AOS-S version strings like "YA/YB.16.08.0001"
#: or a two-releases-joined-by-a-literal-newline "YA.15.10\nYB.15.12" (the
#: 2530, which is not an AOS-CX platform at all despite appearing in this
#: API). Both fail switchReleaseCompatibility with HTTP 400 if selected as
#: "latest", so they must be filtered out before the version sort rather than
#: merely tolerated by it.
_VALID_RELEASE_RE = re.compile(r"^\d+(\.\d+)*$")


def _version_key(release: str) -> tuple:
    """Sort key that orders dotted release strings numerically.

    A plain string sort puts "10.10.0002" before "10.9.0002"; splitting on
    "." and comparing each part as an int avoids that. Callers must pre-filter
    with _VALID_RELEASE_RE first — this assumes every part is a plain integer.
    """
    return tuple(int(part) for part in release.split("."))


def fetch_json(path: str, params: dict[str, str] | None = None) -> object:
    url = f"{BASE_URL}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def format_switch_doc(product: dict, latest: str, rows: list[dict]) -> str:
    name = product["productName"]
    url = (
        f"{BASE_URL}/wired?mode=compare"
        f"&productId={product['productID']}&release={latest}"
    )
    lines = [
        f"<!-- source: {url} -->",
        f"# HPE Aruba Networking {name} — supported features (AOS-CX {latest})",
        "",
        f"Product type: {product['productType']}",
        f"Minimum supported release: {_clean(product['minSupportedRelease'])}",
        f"Feature matrix current as of AOS-CX release: {latest}",
        "",
    ]

    by_type: dict[str, list[dict]] = {}
    for row in rows:
        ftype = _clean(row.get("FeatureType") or "Other")
        by_type.setdefault(ftype, []).append(row)

    for ftype in sorted(by_type):
        lines.append(f"## {ftype}")
        for row in by_type[ftype]:
            fname = html.unescape(_clean(row.get("FeatureName") or ""))
            support = row.get(latest)
            support = _clean(support) if isinstance(support, str) else "Not documented"
            lines.append(f"- {fname}: {support}")
            ref = row.get("FeaturePubRel")
            if ref:
                lines.append(f"  - Release notes: {ref}")
        lines.append("")

    return "\n".join(lines)


def scrape_switches() -> list[str]:
    results = []
    products = fetch_json("/api/productInfo")
    print(f"  productInfo: {len(products)} AOS-CX switch platforms")
    for i, product in enumerate(products, 1):
        name = product["productName"]
        cleaned = {_clean(r) for r in product.get("supportedReleases", []) if _clean(r)}
        releases = sorted(
            (r for r in cleaned if _VALID_RELEASE_RE.match(r)),
            key=_version_key,
        )
        if not releases:
            skipped = cleaned - set(releases)
            results.append(
                f"SKIP {name}: no AOS-CX-format release in supportedReleases "
                f"(non-AOS-CX/malformed entries: {sorted(skipped)})"
            )
            continue
        latest = releases[-1]
        try:
            rows = fetch_json(
                "/api/switchReleaseCompatibility",
                {
                    "productId": str(product["productID"]),
                    "productReleaseNames": latest,
                    "licenses": ALL_LICENSES,
                },
            )
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            results.append(f"ERROR {name}: {e}")
            continue
        if not isinstance(rows, list) or not rows:
            results.append(f"SKIP {name}: empty feature matrix for {latest}")
            continue
        doc = format_switch_doc(product, latest, rows)
        out_path = OUTPUT_DIR / f"cx-{slugify(name)}.md"
        out_path.write_text(doc, encoding="utf-8")
        results.append(f"OK {name} ({len(rows)} features @ {latest}, {len(doc)} chars)")
        if i % 5 == 0 or i == len(products):
            print(f"  [{i}/{len(products)}] {results[-1]}")
        time.sleep(0.3)
    return results


def format_aos10_doc(releases: list[dict], rows: list[dict]) -> str:
    release_names = [r["ReleaseName"] for r in releases]
    url = f"{BASE_URL}/aos10?mode=compare"
    lines = [
        f"<!-- source: {url} -->",
        "# HPE Aruba Networking AOS10 — feature/release compatibility matrix",
        "",
        "AOS10 runs across Aruba Access Points, Gateways, and Mobility",
        "Conductor/Controller-managed deployments as one shared OS image, so",
        "this matrix is not split per hardware platform the way AOS-CX is.",
        f"Releases covered: {', '.join(release_names)}",
        "",
    ]

    by_type: dict[str, list[dict]] = {}
    for row in rows:
        ftype = _clean(row.get("featureType") or "Other")
        by_type.setdefault(ftype, []).append(row)

    for ftype in sorted(by_type):
        lines.append(f"## {ftype}")
        for row in by_type[ftype]:
            fname = html.unescape(_clean(row.get("feature") or ""))
            support = ", ".join(
                f"{rel}={_clean(row[rel]) if isinstance(row.get(rel), str) else 'Not documented'}"
                for rel in release_names
                if rel in row
            )
            lines.append(f"- {fname}: {support}")
        lines.append("")

    return "\n".join(lines)


def scrape_aos10() -> str:
    releases = fetch_json("/api/releases")
    print(f"  releases: {len(releases)} AOS10 releases")
    ids = ",".join(str(r["ReleaseID"]) for r in releases)
    names = ",".join(r["ReleaseName"] for r in releases)
    rows = fetch_json("/api/releaseCompatibility", {"ids": ids, "releases": names})
    if not isinstance(rows, list) or not rows:
        return "SKIP aos10: empty feature matrix"
    doc = format_aos10_doc(releases, rows)
    out_path = OUTPUT_DIR / "aos10-features.md"
    out_path.write_text(doc, encoding="utf-8")
    return f"OK aos10-features.md ({len(rows)} features, {len(doc)} chars)"


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Scraping Feature Navigator -> {OUTPUT_DIR}")

    print("AOS-CX switch platforms:")
    switch_results = scrape_switches()
    errors = [r for r in switch_results if r.startswith(("ERROR", "SKIP"))]

    print("AOS10 (AP/gateway) release matrix:")
    aos10_result = scrape_aos10()
    print(f"  {aos10_result}")

    print(f"\nDone. {len(switch_results) - len(errors)} switch docs written, {len(errors)} skipped/errored.")
    for e in errors:
        print(" ", e)


if __name__ == "__main__":
    main()
