#!/usr/bin/env python3
"""
Discover URLs for the ClearPass Policy Manager Online Help guide.

Same support.hpe.com/hpesc/public/docDisplay?docId=... viewer as the AOS-CX
guides/release-notes scrapers (confirmed via live probe: a flat, DOM-ordered
link list where every real content page is an <a href="...&page=GUID-...">
entry). Scoped to the current 6.14 release only -- unlike release notes,
a Policy Manager user guide is a cumulative "how it works today" reference,
not a per-version changelog, so scraping older major versions (6.11, 6.12,
...) would mostly duplicate the same configuration guidance rather than add
distinct historical content. Add older docIds here later only if a
version-specific behavior question needs it.

Outputs: /tmp/clearpass_guide_urls.json (list of entries).
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

DOC_ID = "sd00007218en_us"
LABEL = "ClearPass Policy Manager 6.14 Online Help"

OUT_PATH = Path("/tmp/clearpass_guide_urls.json")
_PAGE_RE = re.compile(r"page=GUID")


def guide_pages(page, doc_id: str, label: str) -> list[dict]:
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
            entries.append({"url": href, "title": text, "doc_id": doc_id, "guide": label})
    return entries


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()
        entries = guide_pages(page, DOC_ID, LABEL)
        browser.close()

    OUT_PATH.write_text(json.dumps(entries, indent=2))
    print(f"{LABEL} ({DOC_ID}): {len(entries)} pages -> {OUT_PATH}")


if __name__ == "__main__":
    main()
