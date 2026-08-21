#!/usr/bin/env python3
"""
Scrape Juniper EX/MX/QFX/SRX hardware guide and platform-tagged Junos
release-notes pages.

Same DITA topicBody template and plain-urllib approach as scrape_mist_docs.py
(no Playwright needed -- juniper.net is not bot-gated). Reads the URL seed
files written by discover_junos_urls.py (one `junos_<platform>_hardware_urls.
json` and one `junos_<platform>_release_notes_urls.json` per platform) and
writes markdown to `sources/junos_<platform>_hardware/*.md` and
`sources/junos_<platform>_release_notes/*.md` respectively, so each platform
stays its own distinct pair of RAG sources despite sharing one scraper
implementation.

Formerly scrape_junos_ex.py (EX-only); generalized in place to also cover MX
(routers), QFX (data-center switches), and SRX (firewalls, the "firewall
stuff" gap) once discover_junos_urls.py proved the same sitemap/DITA pattern
holds for those platforms too.

Usage: python ingestion/scrape_junos_docs.py [platform ...]
       (no args scrapes every platform in DATASETS)
"""
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

BASE = Path(__file__).parent
PLATFORMS = ("ex", "mx", "qfx", "srx")
DATASETS = {
    f"junos_{platform}_{kind}": BASE / f"junos_{platform}_{kind}_urls.json"
    for platform in PLATFORMS
    for kind in ("hardware", "release_notes")
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
        print(f"skip {name}: {urls_path} not found (run discover_junos_urls.py first)")
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
    requested = sys.argv[1:]
    if requested:
        datasets = {
            name: path
            for name, path in DATASETS.items()
            if any(name.startswith(f"junos_{p}_") for p in requested)
        }
    else:
        datasets = DATASETS
    for name, urls_path in datasets.items():
        run_dataset(name, urls_path)


if __name__ == "__main__":
    main()
