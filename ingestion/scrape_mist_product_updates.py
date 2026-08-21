#!/usr/bin/env python3
"""
Scrape Juniper Mist "Product Updates" (dated release notes) pages.

Reads ingestion/mist_product_updates_urls.json (written by
discover_mist_product_updates_urls.py) and writes each page's content to
sources/mist_product_updates/<slug>.html.

Unlike the DITA-templated pages scrape_mist_docs.py handles (topicBody div),
these pages are Confluence-exported: the real content -- including a leading
in-page table of contents -- lives in <div class="confluence-cont">. The
leading TOC (a big anchor-link list, not prose) is stripped via BeautifulSoup
before saving so RAG chunking sees feature-description prose, not link text.
Plain urllib works fine here (confirmed 200 OK on this host, no Akamai/bot
gate), same as scrape_mist_docs.py -- no Playwright needed.

Usage: python ingestion/scrape_mist_product_updates.py
"""
import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from bs4 import BeautifulSoup

OUTPUT_DIR = Path(__file__).parent / "sources" / "mist_product_updates"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

URLS_PATH = Path(__file__).parent / "mist_product_updates_urls.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return slug or "page"


def load_entries() -> list[dict]:
    if not URLS_PATH.exists():
        raise SystemExit(f"{URLS_PATH} not found. Run discover_mist_product_updates_urls.py first.")
    return json.loads(URLS_PATH.read_text())


ENTRIES = load_entries()


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def extract_content(html: str, title: str) -> str:
    """Extract the real content region.

    Product-updates pages span ~9 years and at least two export templates:
    recent (~2025+) pages are Confluence-exported (div.confluence-cont,
    with a leading in-page TOC that is prose-free and stripped); older
    pages (pre-migration) have no dedicated content wrapper at all -- the
    whole <body> (minus script/style/survey-widget chrome) is already a
    small, clean block of real prose, confirmed by direct sampling.
    """
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("div.confluence-cont")
    if body is not None:
        for toc in body.select("div.toc-macro"):
            toc.decompose()
    else:
        body = soup.select_one("div.topicBody") or soup.body
        if body is None:
            raise RuntimeError("no content container found (body missing)")
        for tag in body(["script", "style"]):
            tag.decompose()
        for widget in body.select("#mcxInvitationModal"):
            widget.decompose()
    heading = f"<h1>Mist Product Updates - {title}</h1>\n" if title else ""
    return heading + str(body)


def scrape_entry(entry: dict) -> str:
    url = entry["url"]
    title = entry.get("title") or ""
    slug = slugify(title or url)
    out_path = OUTPUT_DIR / f"{slug}.html"
    if out_path.exists():
        return f"SKIP {slug} (already scraped)"
    try:
        html = fetch_html(url)
        content = extract_content(html, title)
        if len(content.strip()) < 200:
            raise RuntimeError("content too short (possible template change)")
        out_path.write_text(content, encoding="utf-8")
        result = f"OK {slug} ({len(content)} chars)"
    except Exception as e:
        result = f"ERROR {url}: {e}"
    time.sleep(0.4)  # per-request pacing to stay well under any rate limit
    return result


def main() -> None:
    pending = [
        e for e in ENTRIES
        if not (OUTPUT_DIR / f"{slugify(e.get('title') or e['url'])}.html").exists()
    ]
    print(f"Total entries: {len(ENTRIES)}  pending: {len(pending)}  -> {OUTPUT_DIR}")
    done = 0
    errors = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(scrape_entry, e): e for e in pending}
        for fut in as_completed(futures):
            result = fut.result()
            done += 1
            if result.startswith("ERROR"):
                errors.append(result)
            print(f"  [{done}/{len(pending)}] {result}")
    print(f"\nDone. {len(errors)} errors.")
    for e in errors[:20]:
        print(" ", e)


if __name__ == "__main__":
    main()
