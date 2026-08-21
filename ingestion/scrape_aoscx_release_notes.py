#!/usr/bin/env python3
"""
Scrape AOS-CX switch series release notes (support.hpe.com docDisplay pages).

Reads /tmp/aoscx_release_notes_urls.json (produced by
discover_aoscx_release_notes_urls.py) and writes each page's <main> content
region under sources/aoscx_release_notes/<series-slug>/<patch-slug>/
<page-slug>.html, preserving the series/version/section title as an <h1> so
ingest_docs.py's html_to_text() keeps useful context per chunk. The patch
version is part of the path (not just the page slug) because, by explicit
user decision, this now scrapes the FULL history back to AOS-CX 10.13.xx --
the same page title (e.g. "Known Issues") repeats once per patch per series
(~10,400 pages total across all 14 series), so the patch segment is required
to avoid every patch's "Known Issues" page overwriting the last one on disk.

The docDisplay viewer is a Salesforce Experience Cloud page (~1MB of Lightning
Design System chrome per load); only the single <main role="main"> region is
kept on disk. support.hpe.com is NOT Akamai-fronted (unlike
arubanetworking.hpe.com/arubanetworks.com) -- it gates on a client-side
anonymous-session check instead of blocking headless UAs outright. Playwright
is still required to execute that JS, but the aggressive Akamai-evasion
pacing used by scrape_aos_pw.py is not needed here, so this uses a shorter
1-2s jittered delay to make a ~10,400-page run tractable. On error, the URL
is queued for retry on the next run.
"""
import json
import random
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent / "sources" / "aoscx_release_notes"
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
    # Allow a /tmp/aoscx_missing.json override to only retry errors.
    missing = Path("/tmp/aoscx_missing.json")
    src = missing if missing.exists() else Path("/tmp/aoscx_release_notes_urls.json")
    if not src.exists():
        raise SystemExit(f"{src} not found. Run discover_aoscx_release_notes_urls.py first.")
    data = json.loads(src.read_text())
    if isinstance(data, dict):
        # {series: [entries]} shape from the discovery script.
        entries: list[dict] = []
        for items in data.values():
            entries.extend(items)
        return entries
    return data  # already a flat list (missing.json retry shape)


def out_path(entry: dict) -> Path:
    series_slug = slugify(entry["series"])
    page_slug = slugify(entry.get("title") or entry["url"])
    patch = entry.get("patch")
    if patch:
        return ROOT / series_slug / slugify(patch) / f"{page_slug}.html"
    # Back-compat for any stale missing.json entries from the old
    # latest-patch-only scrape that predate the "patch" field.
    return ROOT / series_slug / f"{page_slug}.html"


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
                time.sleep(1.5)  # let the Experience Cloud page finish client-side render
                html = page.content()
                content = extract_main(html)
                if not content or len(content.strip()) < 50:
                    raise RuntimeError("no <main> content found (possible sign-in wall)")
                title = entry.get("title") or ""
                series = entry.get("series") or ""
                patch = entry.get("patch") or ""
                label = f"{series} {patch}".strip()
                heading = f"<h1>{label} - {title}</h1>\n" if title else ""
                out.write_text(heading + content, encoding="utf-8")
                print(f"  [{i}/{len(pending)}] OK {out.relative_to(ROOT)} ({len(content)})")
            except Exception as e:
                errors.append(entry)
                print(f"  [{i}/{len(pending)}] ERROR {url}: {e}")
            time.sleep(random.uniform(1.0, 2.0))

        browser.close()

    Path("/tmp/aoscx_missing.json").write_text(json.dumps(errors, indent=2))
    print(f"\nDone. {len(errors)} errors (saved to /tmp/aoscx_missing.json)")
    for e in errors[:20]:
        print(" ", e.get("url"))


if __name__ == "__main__":
    main()
