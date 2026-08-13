#!/usr/bin/env python3
"""Scrape the HPE Aruba Networking Central techdocs book (Oxygen WebHelp).

The book that used to live at ``techdocs/new-central/content`` was migrated to
``techdocs/hpe-central/content`` and re-published as Oxygen WebHelp, so every
topic is now a ``GUID-*.html`` page. Two consequences drive this scraper:

* ``scrape_techdocs_pw.py`` reads a ``/tmp/techdocs_urls.json`` list that no
  longer resolves, so it cannot refresh this source.
* Akamai answers direct sub-resource requests (including the WebHelp search
  index ``htmlFileInfoList.js``) with 403, so the page list cannot be fetched
  the way ``discover_aos_urls.py`` pulls the Hugo/Lunr index. Ordinary browser
  *navigation* is served normally.

So discovery is a breadth-first crawl over in-book links using real navigation,
and each visited topic is written to disk mirroring the URL path -- the same
layout ``ingest_docs.py`` already expects for ``techdocs_html``.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import deque
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

from playwright.sync_api import sync_playwright

BASE = "https://arubanetworking.hpe.com/techdocs/hpe-central/content/"
START = urljoin(BASE, "index.html")
OUTPUT_DIR = (
    Path(__file__).parent
    / "sources"
    / "techdocs_html"
    / "arubanetworking.hpe.com"
    / "techdocs"
    / "hpe-central"
    / "content"
)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)


def in_scope(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "arubanetworking.hpe.com"
        and parsed.path.startswith("/techdocs/hpe-central/content/")
        and parsed.path.endswith(".html")
    )


def out_path_for(url: str) -> Path:
    rel = urlparse(url).path.split("/techdocs/hpe-central/content/", 1)[-1]
    return OUTPUT_DIR / rel


def collect_links(page) -> list[str]:
    """In-scope links from the page and any frames, fragments stripped."""
    found: list[str] = []
    for frame in page.frames:
        try:
            hrefs = frame.eval_on_selector_all("a[href]", "e=>e.map(x=>x.href)")
        except Exception:
            continue
        found.extend(hrefs)
    return [u for u in (urldefrag(h)[0] for h in found) if in_scope(u)]


def links_from_saved(path: Path, page_url: str) -> list[str]:
    """In-scope links read off an already-saved topic.

    ``--skip-existing`` must still traverse a cached page's links, otherwise
    resuming a crawl stops at the first topic already on disk (the entry point
    is index.html, so the whole crawl would end immediately).
    """
    try:
        html = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html)
    absolute = (urldefrag(urljoin(page_url, h))[0] for h in hrefs)
    return [u for u in absolute if in_scope(u)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--max-pages",
        type=int,
        default=1200,
        help="Safety bound on pages visited (default: 1200).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.4,
        help="Seconds to pause between navigations (default: 0.4).",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Do not re-download topics already written to disk.",
    )
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    queue: deque[str] = deque([START])
    seen: set[str] = {START}
    saved = 0
    errors: list[str] = []

    with sync_playwright() as playwright:
        # headless=False mirrors the sibling techdocs/VSG scrapers: the site's
        # bot protection rejects headless Chromium.
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        while queue and saved < args.max_pages:
            url = queue.popleft()
            target = out_path_for(url)
            if args.skip_existing and target.exists():
                for link in links_from_saved(target, url):
                    if link not in seen:
                        seen.add(link)
                        queue.append(link)
                continue
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                # The book links to topics that no longer exist. Saving those 404
                # bodies would put stub pages into the RAG corpus, so drop them and
                # don't harvest their links.
                if response is not None and response.status >= 400:
                    errors.append(f"{url}: HTTP {response.status}")
                    continue
                time.sleep(args.delay)
                html = page.content()
            except Exception as exc:  # noqa: BLE001 - report and keep crawling
                errors.append(f"{url}: {exc}")
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                f"<!-- source: {url} -->\n{html}", encoding="utf-8"
            )
            saved += 1
            print(f"[{saved}] OK {target.name} ({len(html)} chars, queue={len(queue)})")

            for link in collect_links(page):
                if link not in seen:
                    seen.add(link)
                    queue.append(link)

        browser.close()

    print(f"\nDone. {saved} pages saved to {OUTPUT_DIR}, {len(errors)} errors.")
    if queue:
        print(
            f"Stopped at the --max-pages bound ({args.max_pages}) with {len(queue)} "
            "URLs still queued, so the book is only partly scraped. Re-run with "
            "--skip-existing to resume where this run stopped, repeating until it "
            "reports no new pages, or raise --max-pages."
        )
    if errors:
        Path("/tmp/hpe_central_errors.json").write_text(json.dumps(errors, indent=2))
        print("Errors written to /tmp/hpe_central_errors.json")


if __name__ == "__main__":
    main()
