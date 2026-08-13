#!/usr/bin/env python3
"""
Scrape AOS 10 techdocs (Hugo Doks site) using Playwright.

Reads URL lists from /tmp/aos_<book>_urls.json (produced by discover_aos_urls.py)
and writes raw HTML page bodies under sources/aos_techdocs/<book>/...
preserving the URL path so ingest_docs.py can pick them up.

1 page at a time, 4-7s jittered delay - same pacing as scrape_techdocs_pw.py.
On error, the URL is queued for retry on the next run.
"""
import json
import random
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

BOOKS = ["wifi-design-deploy", "loc-serv", "p5g", "aos10"]
ROOT = Path(__file__).parent / "sources" / "aos_techdocs"
ROOT.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)


def load_urls() -> list[str]:
    urls: list[str] = []
    # Allow a /tmp/aos_missing.json to override and only retry errors
    missing = Path("/tmp/aos_missing.json")
    if missing.exists():
        return json.loads(missing.read_text())
    for book in BOOKS:
        p = Path(f"/tmp/aos_{book}_urls.json")
        if p.exists():
            urls.extend(json.loads(p.read_text()))
    return urls


def out_path_from_url(url: str) -> Path:
    # /techdocs/aos/aos10/design/foo/  ->  aos_techdocs/aos10/design/foo/index.html
    path = urlparse(url).path
    # strip leading /techdocs/aos/ and trailing slash
    rel = path[len("/techdocs/aos/"):].strip("/")
    if not rel:
        rel = "index"
    target = ROOT / rel
    if path.endswith("/") or not target.suffix:
        target = target / "index.html"
    return target


def extract_content(html: str) -> str:
    # Hugo Doks uses <main> as the primary content container
    for pattern in [
        r"<main[^>]*>(.*?)</main>",
        r"<article[^>]*>(.*?)</article>",
        r'<div[^>]+\brole=["\']main["\'][^>]*>(.*?)</div>\s*</div>\s*</body',
    ]:
        m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1)
    body = re.search(r"<body[^>]*>(.*?)</body>", html, re.DOTALL | re.IGNORECASE)
    return body.group(1) if body else html


def main():
    urls = load_urls()
    if not urls:
        raise SystemExit("No URLs found. Run discover_aos_urls.py first.")

    # Skip already-scraped pages
    pending = [u for u in urls if not out_path_from_url(u).exists()]
    print(f"Total URLs: {len(urls)}  pending: {len(pending)}  -> {ROOT}")

    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        for i, url in enumerate(pending, 1):
            out = out_path_from_url(url)
            out.parent.mkdir(parents=True, exist_ok=True)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if "Access Denied" in (page.title() or ""):
                    raise RuntimeError("403 Access Denied")
                html = page.content()
                content = extract_content(html)
                out.write_text(content, encoding="utf-8")
                print(f"  [{i}/{len(pending)}] OK {out.relative_to(ROOT)} ({len(content)})")
            except Exception as e:
                errors.append(url)
                print(f"  [{i}/{len(pending)}] ERROR {url}: {e}")
            time.sleep(random.uniform(4.0, 7.0))

        browser.close()

    Path("/tmp/aos_missing.json").write_text(json.dumps(errors, indent=2))
    print(f"\nDone. {len(errors)} errors (saved to /tmp/aos_missing.json)")
    for e in errors[:20]:
        print(" ", e)


if __name__ == "__main__":
    main()
