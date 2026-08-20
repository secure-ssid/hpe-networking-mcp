#!/usr/bin/env python3
"""Discover every page in Juniper's current Junos CLI reference TOC.

The CLI reference publishes a machine-readable ``__toc.js`` beside the guide
index. The file contains the complete statement/command tree, so discovery
does not need to crawl 11,000+ pages or depend on browser rendering.

Writes: ingestion/junos_cli_urls.json
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

INDEX_URL = (
    "https://www.juniper.net/documentation/us/en/software/junos/cli-reference/index.html"
)
TOC_URL = (
    "https://www.juniper.net/documentation/us/en/software/junos/cli-reference/__toc.js"
)
PATH_PREFIX = "/documentation/us/en/software/junos/cli-reference/"
OUT_PATH = Path(__file__).parent / "junos_cli_urls.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Accept": "application/javascript,text/javascript,*/*;q=0.8",
}


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def parse_toc(script: str) -> dict:
    """Decode the JSON object assigned to ``var __data`` in ``__toc.js``."""
    marker = "var __data"
    marker_start = script.find(marker)
    if marker_start < 0:
        raise ValueError(f"{TOC_URL} does not contain the expected __data assignment")

    object_start = script.find("{", marker_start)
    if object_start < 0:
        raise ValueError(f"{TOC_URL} contains no JSON object after __data")

    value, _ = json.JSONDecoder().raw_decode(script[object_start:])
    if not isinstance(value, dict) or not isinstance(value.get("toc"), dict):
        raise ValueError(f"{TOC_URL} has an unexpected TOC shape")
    return value


def _toc_links(node: object) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        url = node.get("url")
        if isinstance(url, str):
            found.append(url)
        for value in node.values():
            found.extend(_toc_links(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_toc_links(value))
    return found


def normalize_url(href: str) -> str | None:
    absolute = urldefrag(urljoin(INDEX_URL, href))[0]
    parsed = urlparse(absolute)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.juniper.net"
        or not parsed.path.startswith(PATH_PREFIX)
        or not parsed.path.endswith(".html")
    ):
        return None
    return absolute


def discover_urls(script: str) -> list[str]:
    toc = parse_toc(script)
    urls = {normalized for href in _toc_links(toc["toc"]) if (normalized := normalize_url(href))}
    if not urls:
        raise ValueError("Junos CLI TOC contained no in-scope HTML page URLs")
    return sorted(urls)


def main() -> int:
    print(f"Fetching Junos CLI TOC: {TOC_URL}")
    urls = discover_urls(fetch(TOC_URL))
    OUT_PATH.write_text(json.dumps(urls, indent=2) + "\n", encoding="utf-8")
    print(f"Discovered {len(urls)} unique Junos CLI pages -> {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
