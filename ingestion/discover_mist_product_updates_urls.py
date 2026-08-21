#!/usr/bin/env python3
"""
Discover Juniper Mist "Product Updates" (release notes) page URLs.

juniper.net/documentation/us/en/software/mist/product-updates/ is a
Confluence-exported doc tree, one page per dated release covering every
Mist cloud service (Marvis, Access/Wireless/Wired/WAN Assurance, Location
Services, Premium Analytics) in a single page -- unlike the per-topic DITA
pages under .../software/mist/ that discover_mist_docs_urls.py already
covers via the sitemap.

This section is NOT in that sitemap and has no per-URL <loc> entries to crawl;
instead the site ships a client-side TOC data file,
.../product-updates/__toc.js, which assigns
`window.__data = {toc: {children: [...]}}` -- a JS variable, not quite valid
JSON, but trivially recovered by stripping the `var __data=` prefix and
trailing `;`. Plain urllib works fine (confirmed 200 OK, real TOC data, no
Akamai/bot gate on this host), so no Playwright is needed here.

By explicit user decision this keeps the FULL history (2017 to present,
~186 pages) rather than only recent releases, and excludes the parallel
"govcloud" release track (a separate, smaller set of dated pages for
GovCloud-only customers, out of scope for general design guidance).

Writes: ingestion/mist_product_updates_urls.json (list of
{url, title, year}).
"""
import json
import re
import urllib.request
from pathlib import Path

TOC_URL = "https://www.juniper.net/documentation/us/en/software/mist/product-updates/__toc.js"
BASE = "https://www.juniper.net"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}

OUT_PATH = Path(__file__).parent / "mist_product_updates_urls.json"

_VAR_PREFIX_RE = re.compile(r"^\s*var\s+__data\s*=\s*")


def fetch_toc() -> dict:
    req = urllib.request.Request(TOC_URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    raw = _VAR_PREFIX_RE.sub("", raw.strip()).rstrip(";").rstrip()
    return json.loads(raw)


def walk(node: dict, year: str | None, out: list[dict]) -> None:
    title = node.get("title") or ""
    url = node.get("url") or ""
    if url.endswith("-updates.html") and "govcloud" not in url:
        out.append({"url": BASE + url, "title": title, "year": year or ""})
    next_year = title if re.fullmatch(r"\d{4}", title) else year
    for child in node.get("children", None) or []:
        walk(child, next_year, out)


def main() -> None:
    data = fetch_toc()
    entries: list[dict] = []
    walk(data["toc"], None, entries)
    # De-dup by URL while preserving order (TOC is already newest-first).
    seen: set[str] = set()
    deduped = []
    for e in entries:
        if e["url"] not in seen:
            seen.add(e["url"])
            deduped.append(e)
    OUT_PATH.write_text(json.dumps(deduped, indent=2))
    years = sorted({e["year"] for e in deduped if e["year"]})
    print(f"Discovered {len(deduped)} Mist product-update pages spanning {years[0]}-{years[-1]}")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
