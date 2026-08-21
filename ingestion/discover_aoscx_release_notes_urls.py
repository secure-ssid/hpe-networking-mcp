#!/usr/bin/env python3
"""
Discover URLs for AOS-CX switch series release notes.

The consolidated release-notes portal
(arubanetworks.com/techdocs/AOS-CX/Consolidated_RNs/Portal_Home/Content/cx-home.htm)
exposes a <select> dropdown whose <option value="..."> entries map each switch
series to a support.hpe.com/hpesc/public/docDisplay?docId=... release-notes
document. Each docId page renders its own multi-year patch history as a flat,
ordered link list grouped by "AOS-CX <major>.<minor>.xx" headers (newest major
group last) and, within a group, by "<major>.<minor>.<patch>" sub-headers
(newest patch first).

The portal dropdown host (arubanetworks.com) returns HTTP 403 to plain
TLS/headless clients (Akamai); a *non-headless* Playwright browser with a
realistic Chrome UA is required there (same constraint as
discover_aos_urls.py / scrape_aos_pw.py). support.hpe.com is NOT
Akamai-fronted -- its docDisplay pages need a JS-executing client to pass an
anonymous-session check, which non-headless Playwright also happens to
satisfy, but it is a materially weaker/different gate.

By explicit user decision this discovers the FULL multi-year history (every
major-version group back to whatever is oldest -- currently AOS-CX 10.13.xx
-- and every patch within each group), not just the latest patch. Each
series has on the order of 800-950 individual sub-pages once fully expanded
(confirmed via live measurement against the 4100i and 6300/6300L/6400
series), so the full 14-series run is on the order of 10,000+ pages total.
Every entry also records its "group" (major-version) and "patch" (exact
version string) so the scraper can lay out one directory per patch and avoid
filename collisions between e.g. 10.13.0001's "Known Issues" and 10.17.0004's
"Known Issues".

Outputs: /tmp/aoscx_series_docids.json (series name -> docId) and
/tmp/aoscx_release_notes_urls.json (series name -> list of
{url, title, doc_id, series, group, patch}).
"""
import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PORTAL_URL = (
    "https://www.arubanetworks.com/techdocs/AOS-CX/Consolidated_RNs/"
    "Portal_Home/Content/cx-home.htm"
)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)

# The dropdown also lists "NetEdit Compatibility", which is not a switch
# series release-notes doc -- skip it explicitly rather than special-casing
# it later in the scraper.
_SKIP_SERIES = {"NetEdit Compatibility"}

_DOCID_RE = re.compile(r"docId=([a-z0-9]+en_us)", re.I)
_GROUP_RE = re.compile(r"^AOS-CX\s+[\d.]*xx$", re.I)
_PATCH_RE = re.compile(r"^\d+\.\d+\.\d+$")

OUT_DIR = Path("/tmp")


def discover_series_docids(page) -> dict[str, str]:
    page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(4)
    options = page.eval_on_selector_all(
        "option", "els => els.map(e => ({value: e.value, text: e.textContent.trim()}))"
    )
    mapping: dict[str, str] = {}
    for opt in options:
        text = (opt.get("text") or "").strip()
        if not text or text in _SKIP_SERIES:
            continue
        m = _DOCID_RE.search(opt.get("value") or "")
        if m:
            mapping[text] = m.group(1)
    return mapping


def all_history_pages(page, doc_id: str, series: str) -> list[dict]:
    """Return every release-notes sub-page across every major-version group
    and every patch within each group, for one series.

    The docDisplay page renders a single flat, DOM-ordered link list. Group
    headers ("AOS-CX 10.13.xx") and patch headers ("10.13.0001") are regular
    <a> entries interleaved with the sub-page links they own, so this walks
    the list once, tracking the most recently seen group/patch header and
    tagging every subsequent "page=GUID" link with that context.
    """
    url = f"https://support.hpe.com/hpesc/public/docDisplay?docId={doc_id}"
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)
    links = page.eval_on_selector_all(
        "a", "els => els.map(e => ({href: e.href, text: e.textContent.trim()}))"
    )

    entries: list[dict] = []
    seen_hrefs: set[str] = set()
    current_group: str | None = None
    current_patch: str | None = None
    for link in links:
        text = (link.get("text") or "").strip()
        href = link.get("href") or ""
        if _GROUP_RE.match(text):
            current_group = text
            current_patch = None
            continue
        if _PATCH_RE.match(text):
            current_patch = text
            continue
        if "page=GUID" in href and current_group and current_patch and href not in seen_hrefs:
            seen_hrefs.add(href)
            entries.append(
                {
                    "url": href,
                    "title": text,
                    "doc_id": doc_id,
                    "series": series,
                    "group": current_group,
                    "patch": current_patch,
                }
            )
    return entries


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        series_docids = discover_series_docids(page)
        print(f"Discovered {len(series_docids)} switch series")
        (OUT_DIR / "aoscx_series_docids.json").write_text(
            json.dumps(series_docids, indent=2)
        )

        all_urls: dict[str, list[dict]] = {}
        for series, doc_id in series_docids.items():
            entries = all_history_pages(page, doc_id, series)
            all_urls[series] = entries
            groups = sorted({e["group"] for e in entries})
            print(f"  {series} ({doc_id}): {len(groups)} groups -> {len(entries)} pages")
            time.sleep(2)

        browser.close()

    (OUT_DIR / "aoscx_release_notes_urls.json").write_text(
        json.dumps(all_urls, indent=2)
    )
    total = sum(len(v) for v in all_urls.values())
    print(f"\nTotal pages across {len(all_urls)} series: {total}")


if __name__ == "__main__":
    main()
