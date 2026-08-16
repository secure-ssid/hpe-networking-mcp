#!/usr/bin/env python3
"""
Scrape Juniper EX-series hardware guide and EX Junos release-notes pages.

Same DITA topicBody template and plain-urllib approach as scrape_mist_docs.py
(no Playwright needed -- juniper.net is not bot-gated). Reads both
ingestion/junos_ex_hardware_urls.json and
ingestion/junos_ex_release_notes_urls.json (written by
discover_junos_ex_urls.py) and writes markdown to
sources/junos_ex_hardware/*.md and sources/junos_ex_release_notes/*.md
respectively, so the two stay distinct RAG sources despite sharing one
scraper implementation.

Usage: python ingestion/scrape_junos_ex.py
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

BASE = Path(__file__).parent
DATASETS = {
    "junos_ex_hardware": BASE / "junos_ex_hardware_urls.json",
    "junos_ex_release_notes": BASE / "junos_ex_release_notes_urls.json",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
}

_TOPIC_BODY_RE = re.compile(
    r'<div class="topicBody">(.*?)</div>\s*(?:<div class="(?:related-links|section (?:context|postreq))"|</body>|<footer)',
    re.DOTALL,
)
_TITLE_RE = re.compile(r'<h1[^>]*class="title[^"]*"[^>]*>(.*?)</h1>', re.DOTALL)


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
    title_html = ""
    title_m = _TITLE_RE.search(html)
    if title_m:
        title_html = f"<h1>{title_m.group(1)}</h1>\n"

    body_m = _TOPIC_BODY_RE.search(html)
    if body_m:
        return title_html + body_m.group(1)

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


def scrape_page(url: str, out_dir: Path) -> str:
    slug = slug_from_url(url)
    out_path = out_dir / f"{slug}.md"
    try:
        html = fetch_html(url)
        md = html_to_markdown(extract_content(html), url)
        out_path.write_text(md, encoding="utf-8")
        result = f"OK {slug} ({len(md)} chars)"
    except Exception as e:
        result = f"ERROR {url}: {e}"
    time.sleep(0.4)
    return result


def run_dataset(name: str, urls_path: Path) -> None:
    if not urls_path.exists():
        print(f"skip {name}: {urls_path} not found (run discover_junos_ex_urls.py first)")
        return
    urls = json.loads(urls_path.read_text())
    out_dir = BASE / "sources" / name
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Scraping {len(urls)} {name} pages -> {out_dir}")
    done = 0
    errors = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(scrape_page, url, out_dir): url for url in urls}
        for fut in as_completed(futures):
            result = fut.result()
            done += 1
            if result.startswith("ERROR"):
                errors.append(result)
            if done % 50 == 0 or done == len(urls):
                print(f"  [{done}/{len(urls)}] {result}")
    print(f"{name}: done, {len(errors)} errors.")
    for e in errors[:20]:
        print(" ", e)


def main():
    for name, urls_path in DATASETS.items():
        run_dataset(name, urls_path)


if __name__ == "__main__":
    main()
