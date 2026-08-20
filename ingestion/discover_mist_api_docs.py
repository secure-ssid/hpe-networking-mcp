#!/usr/bin/env python3
"""Discover the canonical generated documentation asset for the Mist API.

Juniper's Mist API portal is an APIMatic widget. Its browser route is only a
shell, but the widget configuration points at one generated JSON document
containing the complete navigation and prose/reference tree. This script
resolves that asset and records it as the freshness URL for the prose scraper.

Writes: ingestion/mist_api_docs_urls.json
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

PORTAL_JS_URL = (
    "https://www.juniper.net/documentation/us/en/software/mist/api/static/js/portal.js"
)
ASSET_BASE = "https://www.juniper.net/documentation/us/en/software/mist/api"
DEFAULT_TEMPLATE = "HTTP_CURL_V1"
OUT_PATH = Path(__file__).parent / "mist_api_docs_urls.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Accept": "application/javascript,text/javascript,*/*;q=0.8",
}


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def docs_asset_url(portal_js: str) -> str:
    match = re.search(r'"initialPlatform":"([^"]+)"', portal_js)
    template = (match.group(1) if match else DEFAULT_TEMPLATE).upper()
    return f"{ASSET_BASE}/static/docs/mist-api-{template}.json"


def page_count(value: object) -> int:
    if isinstance(value, dict):
        own = 1 if isinstance(value.get("Link"), str) and value["Link"].startswith("$h/") else 0
        return own + sum(page_count(child) for child in value.get("SubItems", []))
    if isinstance(value, list):
        return sum(page_count(child) for child in value)
    return 0


def main() -> int:
    portal_js = fetch(PORTAL_JS_URL).decode("utf-8", errors="replace")
    asset_url = docs_asset_url(portal_js)
    document = json.loads(fetch(asset_url))
    if not isinstance(document, dict) or not isinstance(document.get("Sections"), list):
        raise ValueError(f"{asset_url} is not a Mist APIMatic documentation document")

    OUT_PATH.write_text(json.dumps([asset_url], indent=2) + "\n", encoding="utf-8")
    print(
        f"Resolved Mist API docs asset ({page_count(document.get('NavItems', []))} "
        f"virtual pages, {len(json.dumps(document))} JSON chars) -> {OUT_PATH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
