#!/usr/bin/env python3
"""
Scrape AOS-CX Fundamentals and CLI Reference guides (support.hpe.com
docDisplay pages).

Reads /tmp/aoscx_guides_urls.json (produced by
discover_aoscx_guides_urls.py) and writes each page's <main> content region
under sources/aoscx_guides/<guide-slug>/<page-slug>.html, preserving the
guide title as an <h1> so ingest_docs.py's html_to_text() keeps useful
context per chunk.

Same viewer/gating mechanics as scrape_aoscx_release_notes.py: Playwright is
required (support.hpe.com gates on a client-side anonymous-session check,
not Akamai bot-detection), and the per-page delay is a short 1-2s jitter
since this host is not Akamai-fronted. Unlike release notes, these are plain
books with no patch-history dimension -- but CLI Reference guides run
1,300-2,900 pages each, so a slug collision guard (short URL-hash suffix on
duplicate titles within one guide, e.g. repeated generic "Examples" or
"Syntax" subsection headings across many different commands) is applied
before falling back to overwriting. On error, the URL is queued for retry on
the next run.
"""
import hashlib
import json
import random
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent / "sources" / "aoscx_guides"
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
    missing = Path("/tmp/aoscx_guides_missing.json")
    src = missing if missing.exists() else Path("/tmp/aoscx_guides_urls.json")
    if not src.exists():
        raise SystemExit(f"{src} not found. Run discover_aoscx_guides_urls.py first.")
    data = json.loads(src.read_text())
    if isinstance(data, dict):
        entries: list[dict] = []
        for items in data.values():
            entries.extend(items)
        return entries
    return data


def guide_dir(entry: dict) -> Path:
    return ROOT / slugify(entry["guide"])


# Track slugs already assigned to a real (non-retry) disk path this run so a
# duplicate title within the same guide gets a short disambiguating suffix
# instead of silently overwriting a previous page's content.
_assigned: dict[Path, set[str]] = {}


def out_path(entry: dict) -> Path:
    gdir = guide_dir(entry)
    base_slug = slugify(entry.get("title") or entry["url"])
    seen = _assigned.setdefault(gdir, set())
    # Re-derive from any files already on disk (e.g. a prior partial run)
    # the first time we see this guide directory.
    if not seen and gdir.exists():
        seen.update(p.stem for p in gdir.glob("*.html"))
    slug = base_slug
    if slug in seen:
        h = hashlib.sha1(entry["url"].encode("utf-8")).hexdigest()[:8]
        slug = f"{base_slug}-{h}"
    seen.add(slug)
    return gdir / f"{slug}.html"


def extract_main(html: str) -> str | None:
    m = re.search(r"<main\b[^>]*>(.*?)</main>", html, re.DOTALL | re.IGNORECASE)
    return m.group(1) if m else None


def main():
    entries = load_entries()
    # Pre-seed _assigned from anything already on disk so resumed runs still
    # get correct collision detection for not-yet-fetched entries.
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

    Path("/tmp/aoscx_guides_missing.json").write_text(json.dumps(errors, indent=2))
    print(f"\nDone. {len(errors)} errors (saved to /tmp/aoscx_guides_missing.json)")
    for e in errors[:20]:
        print(" ", e.get("url"))


if __name__ == "__main__":
    main()
