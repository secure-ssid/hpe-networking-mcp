#!/usr/bin/env python3
"""
Discover URLs for AOS-CX switch series Fundamentals and CLI Reference guides.

Same support.hpe.com/hpesc/public/docDisplay?docId=... viewer as
discover_aoscx_release_notes_urls.py (confirmed via live probe: each docId
page renders a flat, DOM-ordered link list where every real content page is
an <a href="...&page=GUID-...."> entry). Unlike release notes, these are
plain books with no per-patch history dimension to track -- each entry is
just {url, title, doc_id, guide, series}.

The docId list below was assembled from live-verified search citations (each
confirmed with a direct Playwright load + page=GUID count, not guessed) and
is deliberately NOT exhaustive across every AOS-CX switch-series generation
-- add more (label -> (doc_id, guide_type)) entries here as they are found;
nothing else needs to change.

Known gaps not yet in DOC_IDS below (docId not yet found/verified):
  - CLI Reference: 9300, 10040, 5420 (may share the 6200 or 10000 doc)
  - Fundamentals Guide: 6300/6400, 8100/8320/8325/8360/9300/10000, 8400

Outputs: /tmp/aoscx_guides_urls.json (label -> list of entries).
"""
import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)

# label -> (docId, guide_type)
DOC_IDS: dict[str, tuple[str, str]] = {
    "Fundamentals Guide - 4100i-6000-6100": ("sd00007162en_us", "fundamentals"),
    "Fundamentals Guide - 5420-6200": ("sd00007450en_us", "fundamentals"),
    "CLI Reference - 6000-6100": ("sd00007465en_us", "cli-reference"),
    "CLI Reference - 6200": ("sd00007468en_us", "cli-reference"),
    "CLI Reference - 6300-6400": ("sd00007217en_us", "cli-reference"),
    "CLI Reference - 8100-8360": ("sd00007891en_us", "cli-reference"),
    "CLI Reference - 8400": ("sd00007382en_us", "cli-reference"),
    "CLI Reference - 10000": ("sd00007915en_us", "cli-reference"),
}

OUT_DIR = Path("/tmp")
_PAGE_RE = re.compile(r"page=GUID")


def book_pages(page, doc_id: str, label: str, guide_type: str) -> list[dict]:
    url = f"https://support.hpe.com/hpesc/public/docDisplay?docId={doc_id}"
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)
    links = page.eval_on_selector_all(
        "a", "els => els.map(e => ({href: e.href, text: e.textContent.trim()}))"
    )
    entries: list[dict] = []
    seen: set[str] = set()
    for link in links:
        href = link.get("href") or ""
        text = (link.get("text") or "").strip()
        if _PAGE_RE.search(href) and href not in seen and text:
            seen.add(href)
            entries.append(
                {
                    "url": href,
                    "title": text,
                    "doc_id": doc_id,
                    "guide": label,
                    "guide_type": guide_type,
                }
            )
    return entries


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        all_urls: dict[str, list[dict]] = {}
        for label, (doc_id, guide_type) in DOC_IDS.items():
            entries = book_pages(page, doc_id, label, guide_type)
            all_urls[label] = entries
            print(f"  {label} ({doc_id}): {len(entries)} pages")
            time.sleep(2)

        browser.close()

    OUT_DIR.joinpath("aoscx_guides_urls.json").write_text(
        json.dumps(all_urls, indent=2)
    )
    total = sum(len(v) for v in all_urls.values())
    print(f"\nTotal pages across {len(all_urls)} guides: {total}")


if __name__ == "__main__":
    main()
