#!/usr/bin/env python3
"""Scrape Juniper support-portal KB articles for Mist and Apstra.

The support portal publishes ~13k knowledge articles, of which the Mist and
Apstra ones are in scope for this project. ``scrape_security_lifecycle.py``
already harvests the same sitemap but keeps only the handful of formal
"Security Bulletin" articles, so the far larger body of troubleshooting,
licensing, and configuration KBs was going unindexed. This script covers that
remainder.

The portal is a Salesforce Lightning app: the raw HTML is a JS shell that does
not contain the article text, so the pages have to be rendered in a browser
rather than fetched with urllib.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

import scrape_security_lifecycle as sec

OUTPUT_DIR = Path(__file__).resolve().parent / "sources" / "juniper_kb"

# The Lightning page has no <main> or <article>; the article body is the only
# role=main region. Reading it directly avoids dragging in the portal's nav,
# search box, and footer, which otherwise dominate short articles.
CONTENT_SELECTORS = ("[role=main]", "article", ".slds-rich-text-editor__output")

# Rendered pages carry the portal's own navigation and footer around the
# article body; drop the boilerplate so it does not dominate the embeddings.
_BOILERPLATE_PREFIXES = (
    "Skip to main content",
    "CEC Juniper Community",
    "Search Search",
)
# Exact chrome lines the Lightning shell paints around every article.
_BOILERPLATE_LINES = frozenset({
    "Skip to Main Content",
    "Juniper Support Portal - Home",
    "Home",
    "Knowledge",
    "Quick Links",
    "Expand search",
    "Log in",
    "Back",
    "Knowledge Base",
    "Print",
    "Report a Security Vulnerability",
})
_BOILERPLATE_MARKERS = (
    "Report a Security Vulnerability",
    "Feedback",
)


def in_scope(url: str) -> bool:
    """KB articles about Mist or Apstra, excluding the security bulletins.

    The bulletins are deliberately left to
    ``juniper_security_advisories`` so a given article lands in exactly one
    source and advisory tooling keeps its curated, provenance-tracked view.
    """
    lowered = url.lower()
    return (
        "/s/article/" in lowered
        and ("mist" in lowered or "apstra" in lowered)
        and "security-bulletin" not in lowered
    )


def discover_urls() -> list[str]:
    """In-scope article URLs from the portal's reviewed sitemap index."""
    index_xml = sec.fetch_bytes(sec.JUNIPER_SECURITY_SITEMAP_INDEX_URL).decode(
        "utf-8", "replace"
    )
    children = sec.parse_juniper_security_sitemap_index(index_xml)
    urls: set[str] = set()
    for child in children:
        child_xml = sec.fetch_bytes(child).decode("utf-8", "replace")
        for match in re.finditer(r"<loc>([^<]+)</loc>", child_xml):
            url = match.group(1).strip()
            if in_scope(url):
                urls.add(url)
    return sorted(urls)


def slug_for(url: str) -> str:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", slug)
    return (slug or "article")[:150]


def clean_text(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if kept and kept[-1] != "":
                kept.append("")
            continue
        if stripped.startswith(_BOILERPLATE_PREFIXES) or stripped in _BOILERPLATE_LINES:
            continue
        if stripped.startswith("\u00a9") and "Juniper Networks" in stripped:
            continue
        kept.append(line)
    body = "\n".join(kept).strip()
    for marker in _BOILERPLATE_MARKERS:
        idx = body.rfind(f"\n{marker}")
        if idx > len(body) * 0.6:
            body = body[:idx].strip()
    return re.sub(r"\n{3,}", "\n\n", body)


def extract_text(page) -> str:
    """Visible text of the article region, falling back to the whole body."""
    for selector in CONTENT_SELECTORS:
        try:
            if page.eval_on_selector_all(selector, "e => e.length") == 0:
                continue
            text = page.eval_on_selector(selector, "e => e.innerText")
        except Exception:  # noqa: BLE001 - try the next selector
            continue
        if text and text.strip():
            return text
    return page.evaluate("document.body.innerText")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="Stop after N articles (0 = all).")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds to settle after load.")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument(
        "--min-chars",
        type=int,
        default=200,
        help="Reject pages whose visible text is shorter than this (default: 200).",
    )
    args = parser.parse_args()

    urls = discover_urls()
    if args.limit:
        urls = urls[: args.limit]
    print(f"Discovered {len(urls)} in-scope Mist/Apstra KB articles.")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    saved = 0
    skipped = 0
    errors: list[str] = []
    with sync_playwright() as pw:
        # Headless is fingerprinted and blocked by the portal's bot defenses.
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        for i, url in enumerate(urls, 1):
            target = OUTPUT_DIR / f"{slug_for(url)}.md"
            if args.skip_existing and target.exists():
                skipped += 1
                continue
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                if response is not None and response.status >= 400:
                    errors.append(f"{url}: HTTP {response.status}")
                    continue
                # The shell paints its nav before the article body arrives, so
                # waiting on the element alone still races: wait until the
                # article region actually holds text, or the page falls back to
                # the whole body and the nav gets captured as the article.
                try:
                    page.wait_for_function(
                        """(minChars) => {
                            const el = document.querySelector('[role=main]');
                            return el && el.innerText.trim().length > minChars;
                        }""",
                        arg=args.min_chars,
                        timeout=25_000,
                    )
                except Exception:  # noqa: BLE001 - fall back to the settle delay
                    pass
                page.wait_for_timeout(int(args.delay * 1000))
                text = clean_text(extract_text(page))
            except Exception as exc:  # noqa: BLE001 - report and keep going
                errors.append(f"{url}: {exc}")
                continue

            if len(text) < args.min_chars:
                errors.append(f"{url}: only {len(text)} chars of text")
                continue

            title = url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
            target.write_text(
                f"<!-- source: {url} -->\n# {title}\n\n{text}\n", encoding="utf-8"
            )
            saved += 1
            print(f"[{i}/{len(urls)}] OK {target.name} ({len(text)} chars)")
            time.sleep(args.delay)

        browser.close()

    print(f"\nDone. {saved} saved, {skipped} skipped, {len(errors)} errors -> {OUTPUT_DIR}")
    if errors:
        Path("/tmp/juniper_kb_errors.json").write_text(json.dumps(errors, indent=2))
        print("Errors written to /tmp/juniper_kb_errors.json")


if __name__ == "__main__":
    main()
