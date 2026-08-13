#!/usr/bin/env python3
"""Scrape the HPE Aruba Networking DevHub site.

The manifest points ``devhub`` at ``scrape.py``, but that script hardcodes the
``developer_docs`` output directory and its own seed list, so it never produced
a DevHub corpus and the source stayed empty.

DevHub publishes no sitemap, so pages are found by breadth-first crawl from the
landing page. Content lives in ``#content``; the surrounding markup is the HPE
cookie banner and site chrome, which would otherwise be most of every page.
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

BASE_URL = "https://devhub.arubanetworks.com/"
HOST = urlparse(BASE_URL).netloc
OUTPUT_DIR = Path(__file__).resolve().parent / "sources" / "devhub"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)
CONTENT_SELECTORS = ("#content", "main", "article", "[role=main]")
# Downloadable artifacts rather than pages to read.
NON_PAGE_SUFFIXES = (
    ".zip", ".tar", ".gz", ".pdf", ".png", ".jpg", ".jpeg", ".svg",
    ".gif", ".whl", ".exe", ".dmg", ".ipynb",
)


def normalize(url: str) -> str:
    """Drop fragments and query strings.

    The catalog pages differ only by filter (``?product=aruba``) and render the
    same content, so keeping the query would store the same page many times.
    """
    clean = urldefrag(url)[0]
    return clean.split("?", 1)[0].rstrip("/") or BASE_URL.rstrip("/")


def in_scope(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme in ("http", "https")
        and parsed.netloc == HOST
        and not url.lower().endswith(NON_PAGE_SUFFIXES)
    )


def out_path_for(url: str) -> Path:
    path = urlparse(url).path.strip("/")
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", path) or "index"
    return OUTPUT_DIR / f"{slug[:150]}.md"


def extract_text(page) -> str:
    for selector in CONTENT_SELECTORS:
        try:
            if page.eval_on_selector_all(selector, "e => e.length") == 0:
                continue
            text = page.eval_on_selector(selector, "e => e.innerText")
        except Exception:  # noqa: BLE001 - try the next selector
            continue
        if text and text.strip():
            return text
    return page.evaluate("document.body.innerText")


def collect_links(page) -> list[str]:
    hrefs = page.evaluate("Array.from(document.querySelectorAll('a[href]')).map(a => a.href)")
    return [n for n in (normalize(urljoin(BASE_URL, h)) for h in hrefs) if in_scope(n)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-pages", type=int, default=400, help="Safety bound (default: 400).")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument(
        "--min-chars",
        type=int,
        default=150,
        help="Reject pages with less visible text than this (default: 150).",
    )
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    start = normalize(BASE_URL)
    queue: deque[str] = deque([start])
    seen: set[str] = {start}
    saved = 0
    errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        while queue and saved < args.max_pages:
            url = queue.popleft()
            target = out_path_for(url)
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                if response is not None and response.status >= 400:
                    errors.append(f"{url}: HTTP {response.status}")
                    continue
                page.wait_for_timeout(int(args.delay * 1000))
                text = extract_text(page).strip()
                links = collect_links(page)
            except Exception as exc:  # noqa: BLE001 - report and keep crawling
                errors.append(f"{url}: {exc}")
                continue

            for link in links:
                if link not in seen:
                    seen.add(link)
                    queue.append(link)

            # Skip the write, not the visit: pages are stored as extracted text,
            # so a skipped page's links cannot be read back off disk. Skipping
            # the visit would end the crawl at the entry point, which always
            # exists once the first run has finished.
            if args.skip_existing and target.exists():
                continue

            if len(text) < args.min_chars:
                errors.append(f"{url}: only {len(text)} chars of text")
                continue

            title = urlparse(url).path.strip("/").replace("-", " ").replace("/", " - ") or "DevHub"
            target.write_text(
                f"<!-- source: {url} -->\n# {title}\n\n{text}\n", encoding="utf-8"
            )
            saved += 1
            print(f"[{saved}] OK {target.name} ({len(text)} chars, queue={len(queue)})")
            time.sleep(args.delay)

        browser.close()

    print(f"\nDone. {saved} pages saved to {OUTPUT_DIR}, {len(errors)} errors.")
    if queue:
        print(
            f"Stopped at --max-pages ({args.max_pages}) with {len(queue)} URLs still "
            "queued; re-run with --skip-existing or raise the bound."
        )
    if errors:
        Path("/tmp/devhub_errors.json").write_text(json.dumps(errors, indent=2))
        print("Errors written to /tmp/devhub_errors.json")


if __name__ == "__main__":
    main()
