#!/usr/bin/env python3
"""Extract New Central Config API OpenAPI spec fragments from the public
developer.arubanetworks.com reference site (bypasses the internal-only
cnxconfig host that scrape_openapi.py targets, which resolves to a private
AWS NLB IP and isn't reachable outside HPE's network).

Each reference page (e.g. /new-central-config/reference/aaa-profile) embeds
a full, self-contained OpenAPI fragment for its whole category (not just the
one endpoint) under a `"schema":{"components":...}` JSON blob in the page
HTML — same embedding format discovered for scrape_cnac_spec.py. Many slugs
share the same category bundle, so fragments are de-duplicated by
`info.title` and written once each.

Runs single-threaded with a real per-request delay (default 1.5s) to stay
polite to developer.arubanetworks.com — do not increase concurrency or
remove the delay; ~1500 slugs at this pace takes ~35-45 minutes.

Usage:
    python ingestion/scrape_config_reference_specs.py                  # full run
    python ingestion/scrape_config_reference_specs.py --limit 20       # smoke test
    python ingestion/scrape_config_reference_specs.py --delay 2.0      # slower

Writes: ingestion/sources/openapi_specs/config-<slugified-title>.json
Then rebuild the index: python -m hpe_networking_mcp.pipeline.clients.specs_index --build
"""
import argparse
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

INDEX_URL = "https://developer.arubanetworks.com/new-central-config/reference/"
OUT_DIR = Path(__file__).parent / "sources" / "openapi_specs"
PROGRESS_PATH = Path(__file__).parent / "config_reference_scrape_progress.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}

_SLUG_RE = re.compile(r'"slug":"([a-z0-9-]+)"')


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def discover_slugs() -> list[str]:
    html = fetch(INDEX_URL)
    slugs = sorted(set(_SLUG_RE.findall(html)))
    return slugs


def extract_fragment(html: str) -> dict:
    """Return the largest 'schema':{'components':...} OAS fragment on the page."""
    dec = json.JSONDecoder()
    best: dict = {}
    for m in re.finditer(r'"schema":\{"components"', html):
        start = m.end() - len('{"components"')
        try:
            obj, _ = dec.raw_decode(html[start:])
        except Exception:
            continue
        if len(obj.get("paths", {})) > len(best.get("paths", {})):
            best = obj
    return best


def slug_for_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "untitled"


def load_progress() -> dict:
    if PROGRESS_PATH.exists():
        return json.loads(PROGRESS_PATH.read_text())
    return {"done_slugs": [], "seen_titles": []}


def save_progress(progress: dict) -> None:
    PROGRESS_PATH.write_text(json.dumps(progress, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="only process first N slugs")
    parser.add_argument("--delay", type=float, default=1.5, help="seconds between requests")
    parser.add_argument("--resume", action="store_true", help="skip slugs already processed")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Discovering reference slugs from {INDEX_URL}")
    slugs = discover_slugs()
    if args.limit:
        slugs = slugs[: args.limit]
    print(f"  {len(slugs)} slugs to process")

    progress = load_progress() if args.resume else {"done_slugs": [], "seen_titles": []}
    done_slugs = set(progress["done_slugs"])
    seen_titles = set(progress["seen_titles"])
    written = 0
    errors = []

    for i, slug in enumerate(slugs, 1):
        if slug in done_slugs:
            continue
        url = f"{INDEX_URL}{slug}"
        try:
            html = fetch(url)
            fragment = extract_fragment(html)
            title = fragment.get("info", {}).get("title")
            if title and title not in seen_titles:
                out_path = OUT_DIR / f"config-{slug_for_title(title)}.json"
                out_path.write_text(json.dumps(fragment, indent=1))
                seen_titles.add(title)
                written += 1
                print(f"[{i}/{len(slugs)}] {slug}: NEW '{title}' "
                      f"({len(fragment.get('paths', {}))} paths) -> {out_path.name}")
            else:
                print(f"[{i}/{len(slugs)}] {slug}: dup/empty ('{title}')")
        except (urllib.error.URLError, TimeoutError) as e:
            errors.append(f"{slug}: {e}")
            print(f"[{i}/{len(slugs)}] {slug}: ERROR {e}")
        done_slugs.add(slug)
        if i % 25 == 0:
            save_progress({"done_slugs": sorted(done_slugs), "seen_titles": sorted(seen_titles)})
        time.sleep(args.delay)  # polite pacing — do not remove

    save_progress({"done_slugs": sorted(done_slugs), "seen_titles": sorted(seen_titles)})
    print(f"\nDone. {written} unique spec bundles written, {len(errors)} errors.")
    for e in errors[:20]:
        print(" ", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
