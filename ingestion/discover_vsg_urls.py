#!/usr/bin/env python3
"""Discover Validated Solution Guide page URLs.

``scrape_vsg.py`` expected a hand-made ``/tmp/vsg_urls.json`` that no script in
the repo produced, so a fresh checkout could not refresh the VSG corpus at all.
This fills that gap the same way ``discover_aos_urls.py`` does for the AOS books.

The manifest's seed, ``/techdocs/VSG/docs/``, returns 403: Apache refuses to
list that directory. The individual pages underneath it serve normally, and the
Hugo site publishes ``/techdocs/VSG/sitemap.xml`` listing all of them, so the
sitemap is the reliable source of URLs rather than a crawl of the landing page.

The host blocks plain TLS clients, so the sitemap is read through Playwright
with a real browser profile like the other arubanetworking.hpe.com scrapers.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

SITEMAP_URL = "https://arubanetworking.hpe.com/techdocs/VSG/sitemap.xml"
DOCS_PREFIX = "https://arubanetworking.hpe.com/techdocs/VSG/docs/"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "vsg_urls.json"
# The site publishes translated copies of the whole guide under a locale
# segment. They are near-duplicates of the English pages, so indexing them
# doubles the corpus and splits retrieval across two copies of every answer.
TRANSLATION_PREFIXES = ("ja",)
# The sitemap lists binary assets next to the pages: the compiled guide PDFs
# and title-card images. scrape_vsg.py renders HTML, so these only ever fail,
# and their text is already covered by the HTML pages they were built from.
NON_PAGE_SUFFIXES = (".pdf", ".zip", ".png", ".jpg", ".jpeg", ".svg", ".gif")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)


def fetch_sitemap() -> str:
    with sync_playwright() as pw:
        # headless=False: Akamai serves 403 to headless Chromium on this host.
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()
        try:
            response = page.goto(
                SITEMAP_URL, wait_until="domcontentloaded", timeout=60_000
            )
            if response is not None and response.status >= 400:
                raise SystemExit(f"sitemap returned HTTP {response.status}")
            # Chromium renders XML through a viewer, so read the text it shows
            # rather than response.text(), which the viewer does not expose.
            body = page.evaluate("document.body.innerText")
        finally:
            browser.close()
    return body


def extract_urls(body: str, include_translations: bool = False) -> list[str]:
    locations = re.findall(r"<loc>([^<]+)</loc>", body)
    if not locations:
        # The XML viewer can drop the tags and leave only the URL text.
        locations = re.findall(r"https?://[^\s<\"]+", body)
    urls = set()
    for raw in locations:
        loc = raw.strip()
        if not loc.startswith(DOCS_PREFIX) or loc.rstrip("/").endswith("/404"):
            continue
        if loc.lower().endswith(NON_PAGE_SUFFIXES):
            continue
        section = loc[len(DOCS_PREFIX):].split("/", 1)[0]
        if not include_translations and section in TRANSLATION_PREFIXES:
            continue
        urls.add(loc)
    return sorted(urls)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Where to write the URL list (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--include-translations",
        action="store_true",
        help="Also include localized copies of the guide (default: English only).",
    )
    args = parser.parse_args()

    urls = extract_urls(fetch_sitemap(), include_translations=args.include_translations)
    if not urls:
        raise SystemExit("no VSG doc URLs found in the sitemap")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(urls, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(urls)} VSG URLs to {args.output}")

    sections: dict[str, int] = {}
    for url in urls:
        section = url[len(DOCS_PREFIX):].split("/", 1)[0]
        sections[section] = sections.get(section, 0) + 1
    for section, count in sorted(sections.items()):
        print(f"  {section:<40} {count}")


if __name__ == "__main__":
    main()
