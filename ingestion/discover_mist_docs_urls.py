#!/usr/bin/env python3
"""
Discover Juniper Mist documentation page URLs from juniper.net's public
sitemap index before scraping.

Scaffolded by scripts/add_rag_source.py (strategy: sitemap_crawl), then
filled in with the real juniper.net sitemap format: a top-level sitemap
index (documentation/sitemap/sitemap.xml) fanning out into ~8 per-URL
sitemap files, each a flat <urlset> of <loc> entries (no per-URL <lastmod>
in practice, so freshness is tracked via content hashing, not sitemap
metadata — see ingestion/check_updates.py).

Filters to /documentation/us/en/software/mist/ pages, excludes the
JS-rendered API reference widget (/software/mist/api/...) which returns an
empty shell to plain HTTP clients, and de-duplicates the hreflang-alternate
<loc> repeats juniper.net includes per page.

Writes: ingestion/mist_docs_urls.json (a flat JSON list of absolute URLs) —
referenced from source_manifest.json's `url_seed_file` for this source.
"""
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

SITEMAP_INDEX = "https://www.juniper.net/documentation/sitemap/sitemap.xml"
# Mist-family product docs: the core Mist cloud platform plus adjacent
# products that share the same Mist/Marvis AI-driven assurance platform
# (Juniper Validated Designs reference architectures, and the Data
# Center/Routing Assurance products built on the same Mist cloud backend).
# Deliberately excludes unrelated Juniper product lines also on this sitemap
# (Junos core OS alone is ~24,700 pages, plus vSRX, Contrail, session-smart
# router, ATP, etc.) — those are separate products outside this MCP's scope.
MIST_PATH_PREFIXES = (
    "/documentation/us/en/software/mist/",
    "/documentation/us/en/software/jvd/",
    "/documentation/us/en/software/hpe-mist-networking-data-center-assurance/",
    "/documentation/us/en/software/juniper-data-center-assurance/",
    "/documentation/us/en/software/juniper-routing-assurance/",
)
# The API reference is a JS widget (apimatic-widget) with no server-rendered
# content for plain HTTP clients; the real spec is pulled separately via
# ingestion/scrape_mist_openapi.py instead.
EXCLUDE_SUBSTRINGS = ("/software/mist/api/",)

OUT_PATH = Path(__file__).parent / "mist_docs_urls.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}

_LOC_RE = re.compile(r"<loc>\s*(https?://[^\s<]+)\s*</loc>")


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def sub_sitemap_urls(index_xml: str) -> list[str]:
    return _LOC_RE.findall(index_xml)


def mist_urls(sitemap_xml: str) -> list[str]:
    urls = []
    for loc in _LOC_RE.findall(sitemap_xml):
        if not any(prefix in loc for prefix in MIST_PATH_PREFIXES):
            continue
        if any(bad in loc for bad in EXCLUDE_SUBSTRINGS):
            continue
        if not loc.endswith(".html"):
            continue
        urls.append(loc)
    return urls


def main():
    print(f"Fetching sitemap index: {SITEMAP_INDEX}")
    index_xml = fetch(SITEMAP_INDEX)
    sub_sitemaps = sub_sitemap_urls(index_xml)
    print(f"  {len(sub_sitemaps)} sub-sitemaps")

    all_urls: set[str] = set()
    for i, sm_url in enumerate(sub_sitemaps, 1):
        try:
            xml = fetch(sm_url)
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"  [{i}/{len(sub_sitemaps)}] ERROR {sm_url}: {e}")
            continue
        found = mist_urls(xml)
        all_urls.update(found)
        print(f"  [{i}/{len(sub_sitemaps)}] {sm_url.rsplit('/', 1)[-1]}: +{len(found)} mist urls")
        time.sleep(0.2)

    urls = sorted(all_urls)
    OUT_PATH.write_text(json.dumps(urls, indent=2) + "\n", encoding="utf-8")
    print(f"Discovered {len(urls)} unique Mist doc URLs -> {OUT_PATH}")


if __name__ == "__main__":
    main()
