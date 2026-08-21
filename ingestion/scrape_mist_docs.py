#!/usr/bin/env python3
"""
Scrape Juniper Mist documentation pages and convert to markdown for RAG.

Scaffolded by scripts/add_rag_source.py (strategy: sitemap_crawl), then
adapted from `simple_http_pandoc` (urllib + pandoc) once we confirmed
juniper.net's Mist docs are server-rendered DITA-templated HTML (no
Playwright required, unlike the Aruba techdocs/AOS sources).

Pages use a consistent DITA template: the article body lives in
<div class="topicBody">, with the page title in <h1 class="title ...">.
Extracting just that div avoids the site nav/breadcrumb/TOC noise that a
naive `<body>` dump would include.

Usage: python ingestion/scrape_mist_docs.py
Reads: ingestion/mist_docs_urls.json (written by discover_mist_docs_urls.py)
Writes: ingestion/sources/mist_docs/*.md
"""
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

OUTPUT_DIR = Path(__file__).parent / "sources" / "mist_docs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

URLS_PATH = Path(__file__).parent / "mist_docs_urls.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
}

_TOPIC_BODY_RE = re.compile(
    r'<div class="topicBody">(.*?)</div>\s*(?:<div class="(?:related-links|section (?:context|postreq))"|</body>|<footer)',
    re.DOTALL,
)
_TITLE_RE = re.compile(r'<h1[^>]*class="title[^"]*"[^>]*>(.*?)</h1>', re.DOTALL)


def load_urls() -> list[str]:
    if URLS_PATH.exists():
        return json.loads(URLS_PATH.read_text())
    return []


PAGES = load_urls()


def slug_from_url(url: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "_", url.split("//")[1].replace("/", "_").lower())


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=quote(parts.path)))


def fetch_html(url: str) -> str:
    req = urllib.request.Request(_safe_url(url), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def extract_content(html: str) -> str:
    """Pull the DITA topicBody div (+ h1 title) out of the full page HTML."""
    title_html = ""
    title_m = _TITLE_RE.search(html)
    if title_m:
        title_html = f"<h1>{title_m.group(1)}</h1>\n"

    body_m = _TOPIC_BODY_RE.search(html)
    if body_m:
        return title_html + body_m.group(1)

    # Fallback: less precise but still content-scoped.
    m2 = re.search(r'<div class="topicBody">(.*)', html, re.DOTALL)
    if m2:
        return title_html + m2.group(1)
    return html


def html_to_markdown(html: str, url: str) -> str:
    result = subprocess.run(
        ["pandoc", "-f", "html", "-t", "markdown_strict", "--wrap=none"],
        input=html.encode(),
        capture_output=True,
        timeout=30,
    )
    md = result.stdout.decode("utf-8", errors="replace")
    return f"<!-- source: {url} -->\n\n" + md


def scrape_page(url: str) -> str:
    slug = slug_from_url(url)
    out_path = OUTPUT_DIR / f"{slug}.md"
    try:
        html = fetch_html(url)
        md = html_to_markdown(extract_content(html), url)
        out_path.write_text(md, encoding="utf-8")
        result = f"OK {slug} ({len(md)} chars)"
    except Exception as e:
        result = f"ERROR {url}: {e}"
    time.sleep(0.4)  # per-request pacing to stay well under any rate limit
    return result


def main():
    print(f"Scraping {len(PAGES)} Mist doc pages -> {OUTPUT_DIR}")
    done = 0
    errors = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(scrape_page, url): url for url in PAGES}
        for fut in as_completed(futures):
            result = fut.result()
            done += 1
            if result.startswith("ERROR"):
                errors.append(result)
            if done % 50 == 0 or done == len(PAGES):
                print(f"  [{done}/{len(PAGES)}] {result}")
    print(f"\nDone. {len(errors)} errors.")
    for e in errors[:20]:
        print(" ", e)


if __name__ == "__main__":
    main()
