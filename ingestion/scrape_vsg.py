#!/usr/bin/env python3
"""Scrape VSG pages using Playwright (real Chrome, headless=False to bypass Akamai)."""
import argparse
import json
import random
import re
import subprocess
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

MIN_MARKDOWN_CHARS = 400

OUTPUT_DIR = Path(__file__).parent / "sources" / "vsg_docs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

def slug_from_url(url):
    path = url.split("/techdocs/VSG/docs/")[-1].strip("/")
    return re.sub(r"[^a-z0-9_-]", "_", path.lower()).strip("_")

CONTENT_SELECTORS = ("main", "article", "[role=main]", "body")


def extract_content(page):
    """Return the outer HTML of the page's main content region.

    This reads the rendered DOM rather than regexing the markup: the content
    region is full of nested <div>s, so a non-greedy `<div ...>(.*?)</div>`
    match ends at the first inner closing tag and yields an almost empty
    document, which then gets written out as a valid-looking but contentless
    page.
    """
    for selector in CONTENT_SELECTORS:
        try:
            if page.eval_on_selector_all(selector, "e => e.length") == 0:
                continue
            html = page.eval_on_selector(selector, "e => e.outerHTML")
        except Exception:  # noqa: BLE001 - try the next selector
            continue
        if html:
            return html
    return page.content()

def html_to_markdown(content, url):
    result = subprocess.run(
        ["pandoc", "-f", "html", "-t", "markdown_strict", "--wrap=none"],
        input=content.encode(), capture_output=True, timeout=30,
    )
    md = result.stdout.decode("utf-8", errors="replace")
    return f"<!-- source: {url} -->\n\n" + md

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--urls",
        type=Path,
        default=Path(__file__).resolve().parent / "vsg_urls.json",
        help="JSON list of VSG page URLs (build it with discover_vsg_urls.py).",
    )
    parser.add_argument("--limit", type=int, default=0, help="Stop after N pages (0 = all).")
    args = parser.parse_args()

    if not args.urls.exists():
        raise SystemExit(
            f"{args.urls} not found -- run ingestion/discover_vsg_urls.py first"
        )
    urls = json.loads(args.urls.read_text())
    if args.limit:
        urls = urls[: args.limit]
    print(f"Scraping {len(urls)} VSG pages -> {OUTPUT_DIR}")
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, "
                       "like Gecko) Chrome/135.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        for i, url in enumerate(urls, 1):
            slug = slug_from_url(url)
            out_path = OUTPUT_DIR / f"{slug}.md"
            if out_path.exists():
                print(f"  [{i}/{len(urls)}] SKIP {slug}")
                continue
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if "Access Denied" in page.title():
                    raise Exception("403 Access Denied")
                page.wait_for_timeout(1500)
                content = extract_content(page)
                md = html_to_markdown(content, url)
                # A page that renders but yields almost no markdown means the
                # content region was missed; fail loudly instead of writing a
                # stub that looks like a successful scrape.
                if len(md) < MIN_MARKDOWN_CHARS:
                    raise Exception(f"only {len(md)} chars of markdown extracted")
                out_path.write_text(md, encoding="utf-8")
                print(f"  [{i}/{len(urls)}] OK {slug} ({len(md)})")
            except Exception as e:
                errors.append(url)
                print(f"  [{i}/{len(urls)}] ERROR {url}: {e}")
            time.sleep(random.uniform(4.0, 7.0))
        browser.close()
    print(f"\nDone. {len(errors)} errors.")
    with open("/tmp/vsg_missing.json", "w") as f:
        json.dump(errors, f, indent=2)
    for e in errors[:20]:
        print(" ", e)

if __name__ == "__main__":
    main()
