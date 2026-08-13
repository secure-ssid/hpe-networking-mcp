#!/usr/bin/env python3
"""Scrape New Central NAC/AAA/security reference prose into `nac_docs`.

Replaces the unusable `scrape_reference.py`, which read its URL list from
`/tmp/mrt_urls.json` + `/tmp/cfg_urls.json` (files that no longer exist and
were never produced by anything in the repo) and wrote into
`ingestion/markdown/` rather than the `ingestion/sources/nac_docs` path its
manifest entry declares. As a result `nac_docs` was never populated and its
RAG source silently stayed empty.

Slugs are discovered from the same public reference index used by
`scrape_config_reference_specs.py`, then filtered to the NAC-adjacent
surface described in the manifest (network access control, MAC
registration, visitor, MPSK, DPP, certificates, AAA/802.1X, RADIUS).
Where `scrape_config_reference_specs.py` pulls the embedded *OpenAPI*
fragments off these pages for exact `lookup_api` answers, this pulls the
human-readable prose (endpoint summaries, parameter descriptions, enum
meanings) that `search_docs` needs for "how do I..." questions.

Runs single-threaded with a real per-request delay (default 1.5s) to stay
polite to developer.arubanetworks.com — do not increase concurrency or
remove the delay.

Usage:
    python ingestion/scrape_nac_docs.py                # full run
    python ingestion/scrape_nac_docs.py --limit 10     # smoke test
    python ingestion/scrape_nac_docs.py --delay 2.0    # slower

Writes: ingestion/sources/nac_docs/<slug>.md
"""
import argparse
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

INDEX_URL = "https://developer.arubanetworks.com/new-central-config/reference/"
OUT_DIR = Path(__file__).parent / "sources" / "nac_docs"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}

_SLUG_RE = re.compile(r'"slug":"([a-z0-9-]+)"')

# Substrings that mark a reference slug as NAC/AAA/security-adjacent. Kept as
# an explicit allowlist (rather than scraping all 1,561 slugs) because the
# other categories already reach the index as OpenAPI bundles via
# scrape_config_reference_specs.py — this source only needs the prose for the
# access-control surface the manifest describes.
NAC_SLUG_PATTERNS = (
    "aaa",
    "auth",
    "dot1x",
    "radius",
    "mac-reg",
    "macauth",
    "mpsk",
    "dpp",
    "certificate",
    "cert-",
    "visitor",
    "captive-portal",
    "portal",
    "identity",
    "nac",
    "onboard",
    "role",
    "policy",
    "server-group",
    "servergroup",
    "tacacs",
    "clearpass",
)


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def discover_slugs() -> list[str]:
    html = fetch(INDEX_URL)
    return sorted(set(_SLUG_RE.findall(html)))


def is_nac_slug(slug: str) -> bool:
    return any(pat in slug for pat in NAC_SLUG_PATTERNS)


def extract_article(html: str) -> str:
    """Return the <article> body with icon/script noise stripped.

    ReadMe.io inlines base64 SVG icons and <style>/<script> blocks inside the
    article; left in place they survive pandoc as multi-KB data: URLs that
    would dominate the embedded chunk text.
    """
    m = re.search(r"<article[^>]*>(.*?)</article>", html, re.DOTALL)
    if not m:
        return ""
    art = m.group(1)
    art = re.sub(r"<svg\b.*?</svg>", "", art, flags=re.DOTALL | re.IGNORECASE)
    art = re.sub(r"<img\b[^>]*>", "", art, flags=re.IGNORECASE)
    art = re.sub(r"<style\b.*?</style>", "", art, flags=re.DOTALL | re.IGNORECASE)
    art = re.sub(r"<script\b.*?</script>", "", art, flags=re.DOTALL | re.IGNORECASE)
    return art


def html_to_markdown(article_html: str, url: str) -> str:
    result = subprocess.run(
        ["pandoc", "-f", "html", "-t", "markdown_strict", "--wrap=none"],
        input=article_html.encode(),
        capture_output=True,
        timeout=60,
    )
    md = result.stdout.decode("utf-8", errors="replace")
    md = re.sub(r"<[^>]+>", "", md)  # drop leftover raw tags pandoc passed through
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return f"<!-- source: {url} -->\n\n{md}\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="only process first N slugs")
    parser.add_argument("--delay", type=float, default=1.5, help="seconds between requests")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Discovering reference slugs from {INDEX_URL}")
    slugs = [s for s in discover_slugs() if is_nac_slug(s)]
    if args.limit:
        slugs = slugs[: args.limit]
    print(f"  {len(slugs)} NAC/AAA slugs to scrape -> {OUT_DIR}")

    written = 0
    errors: list[str] = []
    for i, slug in enumerate(slugs, 1):
        url = f"{INDEX_URL}{slug}"
        try:
            article = extract_article(fetch(url))
            if not article:
                errors.append(f"{slug}: no <article> found")
                print(f"[{i}/{len(slugs)}] {slug}: EMPTY")
            else:
                md = html_to_markdown(article, url)
                (OUT_DIR / f"{slug}.md").write_text(md, encoding="utf-8")
                written += 1
                print(f"[{i}/{len(slugs)}] {slug}: OK ({len(md)} chars)")
        except (urllib.error.URLError, TimeoutError, subprocess.TimeoutExpired) as e:
            errors.append(f"{slug}: {e}")
            print(f"[{i}/{len(slugs)}] {slug}: ERROR {e}")
        time.sleep(args.delay)  # polite pacing — do not remove

    print(f"\nDone. {written} pages written, {len(errors)} errors.")
    for e in errors[:20]:
        print(" ", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
