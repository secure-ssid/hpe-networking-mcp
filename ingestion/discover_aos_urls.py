#!/usr/bin/env python3
"""
Discover URLs for the 4 AOS 10 techdocs books.

The site (arubanetworking.hpe.com/techdocs/aos/) is a Hugo Doks static site that
exposes a Lunr search index at offline-search-index.<hash>.json. That index lists
every page on the site with absolute paths. We pull the index via Playwright (the
host blocks plain TLS clients with 403), then bucket the entries per book.

Outputs: /tmp/aos_<book>_urls.json (one list per book) and /tmp/aos_all_urls.json.
"""
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

BOOKS = [
    "wifi-design-deploy",
    "loc-serv",
    "p5g",
    "aos10",
]
BASE = "https://arubanetworking.hpe.com/techdocs/aos/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)


def main():
    debug_dir = Path("/tmp/aos_discover_debug")
    debug_dir.mkdir(exist_ok=True)
    out_dir = Path("/tmp")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        # Load the AOS root once and intercept the search index response.
        index_payload: dict = {}

        def on_response(resp):
            url = resp.url
            if "offline-search-index" in url and url.endswith(".json"):
                try:
                    index_payload["url"] = url
                    index_payload["body"] = resp.text()
                except Exception as e:
                    print(f"  WARN reading index: {e}")

        page.on("response", on_response)
        page.goto(BASE, wait_until="networkidle", timeout=45000)
        time.sleep(3)
        page.remove_listener("response", on_response)
        browser.close()

    if "body" not in index_payload:
        raise SystemExit("Failed to capture offline-search-index.json")

    body = index_payload["body"]
    (debug_dir / "offline-search-index.json").write_text(body, encoding="utf-8")
    print(f"Captured search index from {index_payload['url']} ({len(body)} bytes)")

    data = json.loads(body)
    # Doks search index is typically a list of {uri, title, content, ...}
    if isinstance(data, dict):
        # Some variants wrap it; try common keys
        for key in ("docs", "items", "pages"):
            if key in data and isinstance(data[key], list):
                data = data[key]
                break

    if not isinstance(data, list):
        raise SystemExit(f"Unexpected index shape: {type(data)} keys={list(data)[:5] if hasattr(data,'__iter__') else '?'}")

    print(f"Index has {len(data)} entries")

    # Each entry has a `uri` field like "/techdocs/aos/wifi-design-deploy/wifi-overview/"
    per_book: dict[str, set[str]] = {b: set() for b in BOOKS}
    skipped = 0
    for entry in data:
        uri = entry.get("ref") or entry.get("uri") or entry.get("url") or entry.get("href") or entry.get("permalink")
        if not uri:
            skipped += 1
            continue
        # Normalize to absolute URL
        if uri.startswith("http"):
            full = uri
        elif uri.startswith("/"):
            full = "https://arubanetworking.hpe.com" + uri
        else:
            full = urljoin(BASE, uri)

        for book in BOOKS:
            book_root = urljoin(BASE, book + "/")
            if full.startswith(book_root):
                per_book[book].add(full)
                break

    all_urls: set[str] = set()
    for book, urls in per_book.items():
        sorted_urls = sorted(urls)
        (out_dir / f"aos_{book}_urls.json").write_text(json.dumps(sorted_urls, indent=2))
        all_urls.update(sorted_urls)
        print(f"  {book}: {len(sorted_urls)} pages")

    (out_dir / "aos_all_urls.json").write_text(json.dumps(sorted(all_urls), indent=2))
    print(f"\nTotal unique URLs across all 4 books: {len(all_urls)}")
    print(f"Skipped {skipped} entries with no uri field")


if __name__ == "__main__":
    main()
