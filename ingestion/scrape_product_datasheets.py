#!/usr/bin/env python3
"""
Scrape official Juniper EX-series switch and Mist-family access-point
hardware datasheet specifications into the ``product_datasheets`` RAG
source.

Covers only what this environment could actually reach and verify:
juniper.net's EX-series switch spec pages and AP (Mist-line) datasheet
pages. Aruba/HPE-branded product pages
(arubanetworks.com, arubanetworking.hpe.com/techdocs excluded paths,
www.hpe.com) were probed from this environment and every one returned
either an Akamai edge "Access Denied" (403, confirmed via plain HTTP *and*
a genuine headless-Chromium Playwright request) or a hard HTTP/2 stream
reset before any response body — a network/WAF-level block, not a missing
selector or scraping-technique gap. Aruba CX/AP exact specs are served
instead by the structured, hand-verified
``hpe_networking_mcp.pipeline.clients.hardware_specs`` catalog, and AOS-CX
per-platform feature support now comes from the real
``feature_navigator`` source (see scrape_feature_navigator.py).

URL discovery: juniper.net publishes a flat, non-paginated product sitemap
at ``https://www.juniper.net/sitemaps/en_US.xml`` (2,130 <loc> entries as
of writing) — distinct from the *documentation* sitemap
(``https://www.juniper.net/documentation/sitemap/sitemap.xml``) that
ingestion/discover_mist_docs_urls.py reads. This script filters that one
sitemap directly instead of caching a separate discovered-URL JSON file:
unlike the Mist docs corpus (~2,000 pages, multi-hour crawl, needs
--skip-existing reruns), this is ~50 pages fetched in well under a minute,
so a cached seed file would add a stale-cache failure mode for no
practical benefit.

Two page templates, two extractors:

* EX-series ``.../switches/ex-series/<slug>/*specs.html`` — server-rendered
  Adobe Experience Manager component. The whole spec table lives in
  ``div.cmp-specifications`` as label/value ``<tr>`` pairs (h3 label,
  h3+p value) with zero nav/chrome noise; verified stable across the full
  EX2300-EX9250 range.
* Access-point ``.../access-points/<model>-datasheet.html`` — no
  ``cmp-specifications`` wrapper; instead exactly 5 ``<table>`` elements
  hold the real spec content (multi-model comparison, radio/Wi-Fi detail,
  IoT/USB/Ethernet ports, mounting brackets, ordering SKUs). Rendered
  generically: a table whose first row has >2 cells becomes a markdown
  table (comparison tables), everything else becomes label/value bullets.

Usage:
    uv run python ingestion/scrape_product_datasheets.py
Writes:
    ingestion/sources/product_datasheets/switch-<slug>.md
    ingestion/sources/product_datasheets/ap-<model>.md
"""
from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from bs4 import BeautifulSoup

SITEMAP_URL = "https://www.juniper.net/sitemaps/en_US.xml"
OUTPUT_DIR = Path(__file__).parent / "sources" / "product_datasheets"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
}

_LOC_RE = re.compile(r"<loc>\s*(https?://[^\s<]+)\s*</loc>")

# Matches both "/ex-series/ex4400-ethernet-switch/specs.html" and the
# double-named "/ex4400-ethernet-switch/ex4400-ethernet-switch-specs.html"
# variant juniper.net uses inconsistently across the EX line.
_SWITCH_SPECS_RE = re.compile(r"/products/switches/ex-series/([^/]+)/[^/]*specs\.html$")
_AP_DATASHEET_RE = re.compile(r"/products/access-points/([^/]+)-datasheet\.html$")


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def discover_urls() -> tuple[list[str], list[str]]:
    """Return (switch_spec_urls, ap_datasheet_urls) from the product sitemap."""
    xml = fetch(SITEMAP_URL)
    locs = _LOC_RE.findall(xml)
    switches = sorted({u for u in locs if _SWITCH_SPECS_RE.search(u)})
    aps = sorted({u for u in locs if _AP_DATASHEET_RE.search(u)})
    return switches, aps


def slug_for_switch(url: str) -> str:
    m = _SWITCH_SPECS_RE.search(url)
    return m.group(1) if m else re.sub(r"[^a-z0-9]+", "-", url.lower())


def slug_for_ap(url: str) -> str:
    m = _AP_DATASHEET_RE.search(url)
    return m.group(1) if m else re.sub(r"[^a-z0-9]+", "-", url.lower())


def extract_switch_specs(html: str, url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    spec_div = soup.select_one("div.cmp-specifications")
    if spec_div is None:
        return None
    title_el = soup.find("h1")
    title = title_el.get_text(strip=True) if title_el else slug_for_switch(url)

    lines = [f"<!-- source: {url} -->", f"# {title} — specifications", ""]
    for row in spec_div.select("table tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        label = cells[0].get_text(" ", strip=True)
        value = cells[1].get_text("\n", strip=True)
        if not label or not value:
            continue
        value_lines = [v for v in value.split("\n") if v]
        if len(value_lines) > 1:
            lines.append(f"**{label}:**")
            for v in value_lines:
                lines.append(f"- {v}")
        else:
            lines.append(f"**{label}:** {value_lines[0]}")
    if len(lines) <= 3:
        return None
    return "\n".join(lines)


def _table_to_markdown(table) -> list[str]:
    rows = table.find_all("tr")
    if not rows:
        return []
    parsed = [
        [c.get_text(" ", strip=True) for c in r.find_all(["td", "th"])] for r in rows
    ]
    width = max(len(r) for r in parsed)
    if width > 2:
        # A comparison table across multiple models: render as a real
        # markdown table so each model stays in its own column.
        out = []
        header = parsed[0] + [""] * (width - len(parsed[0]))
        out.append("| " + " | ".join(header) + " |")
        out.append("|" + "|".join(["---"] * width) + "|")
        for r in parsed[1:]:
            padded = r + [""] * (width - len(r))
            out.append("| " + " | ".join(padded) + " |")
        return out
    # A label/value table: render as bullets, skipping empty rows.
    out = []
    for r in parsed:
        cells = [c for c in r if c]
        if not cells:
            continue
        if len(cells) == 1:
            out.append(f"- {cells[0]}")
        else:
            out.append(f"- **{cells[0]}:** {' / '.join(cells[1:])}")
    return out


def extract_ap_datasheet(html: str, url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        return None
    title_el = soup.find("h1")
    title = title_el.get_text(strip=True) if title_el else slug_for_ap(url)

    lines = [f"<!-- source: {url} -->", f"# {title}", ""]
    for table in tables:
        md_rows = _table_to_markdown(table)
        if md_rows:
            lines.extend(md_rows)
            lines.append("")
    if len(lines) <= 3:
        return None
    return "\n".join(lines)


def scrape_page(url: str, kind: str) -> str:
    slug = slug_for_switch(url) if kind == "switch" else slug_for_ap(url)
    prefix = "switch" if kind == "switch" else "ap"
    out_path = OUTPUT_DIR / f"{prefix}-{slug}.md"
    try:
        html = fetch(url)
        doc = (
            extract_switch_specs(html, url)
            if kind == "switch"
            else extract_ap_datasheet(html, url)
        )
        if not doc:
            return f"SKIP {url}: no extractable spec content found"
        out_path.write_text(doc, encoding="utf-8")
        result = f"OK {out_path.name} ({len(doc)} chars)"
    except (urllib.error.URLError, TimeoutError) as e:
        result = f"ERROR {url}: {e}"
    time.sleep(0.3)
    return result


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Discovering datasheet URLs from {SITEMAP_URL}")
    switches, aps = discover_urls()
    print(f"  {len(switches)} EX-series switch spec pages, {len(aps)} AP datasheet pages")

    results = []
    for url in switches:
        r = scrape_page(url, "switch")
        results.append(r)
        print(f"  {r}")
    for url in aps:
        r = scrape_page(url, "ap")
        results.append(r)
        print(f"  {r}")

    ok = [r for r in results if r.startswith("OK")]
    errors = [r for r in results if not r.startswith("OK")]
    print(f"\nDone. {len(ok)} docs written, {len(errors)} skipped/errored.")
    for e in errors:
        print(" ", e)


if __name__ == "__main__":
    main()
