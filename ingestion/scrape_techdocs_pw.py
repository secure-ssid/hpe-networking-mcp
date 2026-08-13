#!/usr/bin/env python3
"""
Scrape techdocs using Playwright (real Chrome) to bypass TLS fingerprint blocking.
1 page at a time with delay to avoid rate limits.
"""
import json
import os
import random
import re
import subprocess
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

OUTPUT_DIR = Path(__file__).parent / "sources" / "techdocs_html" / "arubanetworking.hpe.com" / "techdocs" / "new-central" / "content"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

def load_urls():
    path = "/tmp/techdocs_missing.json" if os.path.exists("/tmp/techdocs_missing.json") else "/tmp/techdocs_urls.json"
    with open(path) as f:
        urls = json.load(f)
    # Fix double slash
    return [u.replace("new-central//", "new-central/") for u in urls]

def out_path_from_url(url) -> Path:
    # Preserve subdir structure and original .htm extension to match existing files
    rel = url.split("/techdocs/new-central/content/")[-1]
    return OUTPUT_DIR / Path(rel)

def extract_content(html):
    for pattern in [
        r'<div[^>]+\brole=["\']main["\'][^>]*>(.*?)</div>\s*</div>\s*</body',
        r'<div[^>]+class=["\'][^"\']*\bbody\b[^"\']*["\'][^>]*>(.*?)</div>\s*(?:</div>\s*)*</body',
        r'<main[^>]*>(.*?)</main>',
        r'<article[^>]*>(.*?)</article>',
    ]:
        m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1)
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

def main():
    urls = load_urls()
    # Filter to only missing
    urls = [u for u in urls if not out_path_from_url(u).exists()]
    print(f"Scraping {len(urls)} pages with Playwright -> {OUTPUT_DIR}")

    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        for i, url in enumerate(urls, 1):
            out_path = out_path_from_url(url)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if "Access Denied" in page.title():
                    raise Exception("403 Access Denied")
                html = page.content()
                content = extract_content(html)
                out_path.write_text(content, encoding="utf-8")
                print(f"  [{i}/{len(urls)}] OK {out_path.name} ({len(content)})")
            except Exception as e:
                errors.append(url)
                print(f"  [{i}/{len(urls)}] ERROR {url}: {e}")
            time.sleep(random.uniform(4.0, 7.0))

        browser.close()

    print(f"\nDone. {len(errors)} errors.")
    # Save remaining errors for next retry
    with open("/tmp/techdocs_missing.json", "w") as f:
        json.dump(errors, f, indent=2)
    for e in errors[:20]:
        print(" ", e)

if __name__ == "__main__":
    main()
