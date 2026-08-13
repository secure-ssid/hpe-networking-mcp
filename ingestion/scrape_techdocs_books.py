"""Crawl the remaining arubanetworking.hpe.com WebHelp books into ``tech_docs``.

``scrape_hpe_central_pw.py`` is hardwired to the ``hpe-central`` book. The doc
portal publishes several other books that were never indexed -- most usefully
**CLI-Bank** (the CLI command reference for AOS-10, Central On-Premises,
ClearPass and SD-Branch) and **central-on-premises**.

Only VSG serves a ``sitemap.xml``; these books have to be walked breadth-first
from a landing page. Books also differ in layout: some use ``/Content/`` with
``.htm``, others ``/content/`` with ``.htm``/``.html``, so scope is expressed
per book rather than assumed.

Usage:
    python ingestion/scrape_techdocs_books.py --books cli-bank
    python ingestion/scrape_techdocs_books.py --books cli-bank,cop --skip-existing

Re-run with ``--skip-existing`` until it reports 0 new pages: cached pages are
still traversed for links, otherwise a resumed crawl would stop dead at the
entry point (which always exists after the first run).
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import deque
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

HOST = "arubanetworking.hpe.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)
OUTPUT_ROOT = Path(__file__).parent / "sources" / "tech_docs"

# A book is a URL prefix everything must sit under, plus the pages to start from.
BOOKS: dict[str, dict] = {
    "cli-bank": {
        "prefix": "/techdocs/CLI-Bank/Content/",
        "starts": [
            f"https://{HOST}/techdocs/CLI-Bank/Content/landing-pages/aos10-home.htm",
            f"https://{HOST}/techdocs/CLI-Bank/Content/landing-pages/cop-home.htm",
            f"https://{HOST}/techdocs/CLI-Bank/Content/landing-pages/cppm-home.htm",
            f"https://{HOST}/techdocs/CLI-Bank/Content/landing-pages/sdbranch-home.htm",
        ],
    },
    "cop": {
        "prefix": "/techdocs/central-on-premises/olh/",
        "starts": [
            f"https://{HOST}/techdocs/central-on-premises/olh/303x/content/on-prem/whatsnew/3030.htm",
            f"https://{HOST}/techdocs/central-on-premises/olh/303x/content/sys-mgmt/3030/install/installing-cop.htm",
            f"https://{HOST}/techdocs/central-on-premises/olh/302x/content/ttv/ttv.htm",
        ],
    },
    "aos-switch-rn": {
        "prefix": "/techdocs/AOS-Switch-RN/Content/",
        "starts": [f"https://{HOST}/techdocs/AOS-Switch-RN/Content/home.htm"],
    },
    "airwave": {
        "prefix": "/techdocs/AirWave/8306/webhelp/Content/",
        "starts": [f"https://{HOST}/techdocs/AirWave/8306/webhelp/Content/home.htm"],
    },
}

PAGE_SUFFIXES = (".htm", ".html")


def in_scope(url: str, prefix: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == HOST
        and parsed.path.startswith(prefix)
        and parsed.path.endswith(PAGE_SUFFIXES)
    )


def out_path_for(url: str, book: str, prefix: str) -> Path:
    rel = urlparse(url).path.split(prefix, 1)[-1].lstrip("/")
    return OUTPUT_ROOT / book / rel


def collect_links(page, prefix: str) -> list[str]:
    """In-scope links from the page and any frames, fragments stripped."""
    found: list[str] = []
    for frame in page.frames:
        try:
            found.extend(frame.eval_on_selector_all("a[href]", "e=>e.map(x=>x.href)"))
        except Exception:  # noqa: BLE001 - a detached frame must not stop the crawl
            continue
    return [u for u in (urldefrag(h)[0] for h in found) if in_scope(u, prefix)]


def links_from_saved(path: Path, page_url: str, prefix: str) -> list[str]:
    """In-scope links read off an already-saved topic (see module docstring)."""
    try:
        html = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html)
    absolute = (urldefrag(urljoin(page_url, h))[0] for h in hrefs)
    return [u for u in absolute if in_scope(u, prefix)]


def crawl_book(page, book: str, spec: dict, args) -> tuple[int, list[str]]:
    prefix = spec["prefix"]
    queue: deque[str] = deque(spec["starts"])
    seen: set[str] = set(spec["starts"])
    saved = 0
    errors: list[str] = []

    while queue and saved < args.max_pages:
        url = queue.popleft()
        target = out_path_for(url, book, prefix)
        if args.skip_existing and target.exists():
            for link in links_from_saved(target, url, prefix):
                if link not in seen:
                    seen.add(link)
                    queue.append(link)
            continue
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            # Saving 404 bodies would put stub pages into the RAG corpus.
            if response is not None and response.status >= 400:
                errors.append(f"{url}: HTTP {response.status}")
                continue
            time.sleep(args.delay)
            html = page.content()
        except Exception as exc:  # noqa: BLE001 - report and keep crawling
            errors.append(f"{url}: {exc}")
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"<!-- source: {url} -->\n{html}", encoding="utf-8")
        saved += 1
        if saved % 25 == 0 or saved == 1:
            print(f"  [{book}] {saved} saved, queue={len(queue)}", flush=True)

        for link in collect_links(page, prefix):
            if link not in seen:
                seen.add(link)
                queue.append(link)

    print(f"  [{book}] done: {saved} pages, {len(errors)} errors, {len(queue)} still queued")
    return saved, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--books", default="", help=f"comma-separated: {','.join(BOOKS)}")
    parser.add_argument("--max-pages", type=int, default=2500, help="per-book page bound")
    parser.add_argument("--delay", type=float, default=0.3, help="seconds between navigations")
    parser.add_argument("--skip-existing", action="store_true", help="resume a crawl")
    args = parser.parse_args()

    wanted = [b.strip() for b in args.books.split(",") if b.strip()] or list(BOOKS)
    unknown = [b for b in wanted if b not in BOOKS]
    if unknown:
        print(f"unknown books: {unknown}; known: {list(BOOKS)}")
        return 2

    from playwright.sync_api import sync_playwright

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    total = 0
    all_errors: list[str] = []
    with sync_playwright() as playwright:
        # headless=False mirrors the sibling techdocs/VSG scrapers: the site's
        # bot protection rejects headless Chromium.
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()
        for book in wanted:
            print(f"[{book}] starting")
            saved, errors = crawl_book(page, book, BOOKS[book], args)
            total += saved
            all_errors.extend(errors)
        browser.close()

    print(f"\nDone. {total} pages saved to {OUTPUT_ROOT}, {len(all_errors)} errors.")
    if all_errors:
        Path("/tmp/techdocs_books_errors.json").write_text(json.dumps(all_errors, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
