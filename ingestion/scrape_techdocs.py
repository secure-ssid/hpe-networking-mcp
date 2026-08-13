#!/usr/bin/env python3
"""
Scrape New Central techdocs (MadCap Flare) pages and convert to markdown.
Content is in <div class="body"> or <div role="main"> in SSR HTML.
"""
import html as htmllib
import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import urllib.request

OUTPUT_DIR = Path(__file__).parent / "markdown_techdocs"
OUTPUT_DIR.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
}

def load_urls():
    import os
    path = "/tmp/techdocs_missing.json" if os.path.exists("/tmp/techdocs_missing.json") else "/tmp/techdocs_urls.json"
    with open(path) as f:
        return json.load(f)

def slug_from_url(url):
    path = url.split("/techdocs/new-central/")[-1]
    return re.sub(r"[^a-z0-9_-]", "_", path.lower()).strip("_")

def fetch_html(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")

def extract_content(html):
    # MadCap Flare: main content is in <div class="body"> or <div role="main">
    for pattern in [
        r'<div[^>]+\brole=["\']main["\'][^>]*>(.*?)</div>\s*</div>\s*</body',
        r'<div[^>]+class=["\'][^"\']*\bbody\b[^"\']*["\'][^>]*>(.*?)</div>\s*(?:</div>\s*)*</body',
        r'<main[^>]*>(.*?)</main>',
        r'<article[^>]*>(.*?)</article>',
    ]:
        m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1)
    # Fallback: strip nav/header/footer and return body
    body = re.search(r'<body[^>]*>(.*?)</body>', html, re.DOTALL | re.IGNORECASE)
    return body.group(1) if body else html

def html_to_markdown(content, url):
    result = subprocess.run(
        ["pandoc", "-f", "html", "-t", "markdown_strict", "--wrap=none"],
        input=content.encode(),
        capture_output=True,
        timeout=30,
    )
    md = result.stdout.decode("utf-8", errors="replace")
    return f"<!-- source: {url} -->\n\n" + md

def scrape_page(url):
    slug = slug_from_url(url)
    out_path = OUTPUT_DIR / f"{slug}.md"
    if out_path.exists():
        return f"SKIP {slug}"
    try:
        html = fetch_html(url)
        content = extract_content(html)
        md = html_to_markdown(content, url)
        out_path.write_text(md, encoding="utf-8")
        time.sleep(3.0)
        return f"OK {slug} ({len(md)})"
    except Exception as e:
        return f"ERROR {url}: {e}"

def main():
    urls = load_urls()
    print(f"Scraping {len(urls)} techdocs pages -> {OUTPUT_DIR}")
    done = 0
    errors = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = {pool.submit(scrape_page, url): url for url in urls}
        for fut in as_completed(futures):
            result = fut.result()
            done += 1
            if result.startswith("ERROR"):
                errors.append(result)
            if done % 100 == 0 or done == len(urls):
                print(f"  [{done}/{len(urls)}] {result}")
    print(f"\nDone. {len(errors)} errors.")
    for e in errors[:20]:
        print(" ", e)

if __name__ == "__main__":
    main()
