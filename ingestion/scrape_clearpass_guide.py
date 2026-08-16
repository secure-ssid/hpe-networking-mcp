#!/usr/bin/env python3
"""
Scrape the ClearPass Policy Manager Online Help guide (support.hpe.com
docDisplay pages).

Reads /tmp/clearpass_guide_urls.json (produced by
discover_clearpass_guide_urls.py) and writes each page's <main> content
region under sources/clearpass_guide/<page-slug>.html, preserving the guide
title as an <h1>. Same viewer/gating mechanics as the AOS-CX guide/release-
notes scrapers (Playwright required, short 1-2s jitter, not Akamai-fronted).
A slug-collision guard (short URL-hash suffix on duplicate titles) protects
against generic repeated subsection headings across the ~1,243-page guide.
On error, the URL is queued for retry on the next run.
"""
import hashlib
import json
import random
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent / "sources" / "clearpass_guide"
ROOT.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return slug or "page"


def load_entries() -> list[dict]:
    missing = Path("/tmp/clearpass_guide_missing.json")
    src = missing if missing.exists() else Path("/tmp/clearpass_guide_urls.json")
    if not src.exists():
        raise SystemExit(f"{src} not found. Run discover_clearpass_guide_urls.py first.")
    return json.loads(src.read_text())


_assigned: set[str] = set()


def out_path(entry: dict) -> Path:
    base_slug = slugify(entry.get("title") or entry["url"])
    if not _assigned and ROOT.exists():
        _assigned.update(p.stem for p in ROOT.glob("*.html"))
    slug = base_slug
    if slug in _assigned:
        h = hashlib.sha1(entry["url"].encode("utf-8")).hexdigest()[:8]
        slug = f"{base_slug}-{h}"
    _assigned.add(slug)
    return ROOT / f"{slug}.html"


def extract_main(html: str) -> str | None:
    m = re.search(r"<main\b[^>]*>(.*?)</main>", html, re.DOTALL | re.IGNORECASE)
    return m.group(1) if m else None


def main():
    entries = load_entries()
    pending = [e for e in entries if not out_path(e).exists()]
    print(f"Total entries: {len(entries)}  pending: {len(pending)}  -> {ROOT}")

    errors: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        for i, entry in enumerate(pending, 1):
            url = entry["url"]
            out = out_path(entry)
            out.parent.mkdir(parents=True, exist_ok=True)
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if resp is not None and resp.status == 403:
                    raise RuntimeError("403 Access Denied")
                time.sleep(1.5)
                html = page.content()
                content = extract_main(html)
                if not content or len(content.strip()) < 50:
                    raise RuntimeError("no <main> content found (possible sign-in wall)")
                title = entry.get("title") or ""
                guide = entry.get("guide") or ""
                heading = f"<h1>{guide} - {title}</h1>\n" if title else ""
                out.write_text(heading + content, encoding="utf-8")
                print(f"  [{i}/{len(pending)}] OK {out.relative_to(ROOT)} ({len(content)})")
            except Exception as e:
                errors.append(entry)
                print(f"  [{i}/{len(pending)}] ERROR {url}: {e}")
            time.sleep(random.uniform(1.0, 2.0))

        browser.close()

    Path("/tmp/clearpass_guide_missing.json").write_text(json.dumps(errors, indent=2))
    print(f"\nDone. {len(errors)} errors (saved to /tmp/clearpass_guide_missing.json)")
    for e in errors[:20]:
        print(" ", e.get("url"))


if __name__ == "__main__":
    main()
