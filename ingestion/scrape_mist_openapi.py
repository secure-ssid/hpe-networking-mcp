#!/usr/bin/env python3
"""Download the Juniper Mist OpenAPI 3.x spec for exact API endpoint/schema lookup.

Mist publishes a full OpenAPI document for the entire Mist API (orgs, sites,
devices, WLANs, clients, etc.) on GitHub rather than behind a docs-site JSON
endpoint like scrape_openapi.py's Aruba source, so this just downloads the
single raw file.

Source: https://github.com/mistsys/mist_openapi (branch ``master``) -- Mist's
own repository, and the upstream that `vendor/openapi/mist.openapi.json` is
pinned to by commit. It supersedes the
``Mist-Automation-Programmability/mist_openapi`` mirror this script used to
read, which stopped receiving pushes on 2025-11-24 and has no ``master``.

This fetches the branch tip, so it is deliberately *not* the reproducible
path: for a pinned, digest-verified copy use
``scripts/vendor_openapi_corpus.py``, which reads an immutable commit URL.

Usage: python ingestion/scrape_mist_openapi.py
Writes: ingestion/sources/openapi_specs/mist.openapi.json
Then rebuild the index: python -m hpe_networking_mcp.pipeline.clients.specs_index --build
"""
import json
import urllib.request
from pathlib import Path

SPEC_URL = "https://raw.githubusercontent.com/mistsys/mist_openapi/master/mist.openapi.json"
OUT_PATH = Path(__file__).parent / "sources" / "openapi_specs" / "mist.openapi.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}


def main():
    print(f"Downloading Mist OpenAPI spec: {SPEC_URL}")
    req = urllib.request.Request(SPEC_URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    spec = json.loads(data)  # validate
    title = spec.get("info", {}).get("title", "Mist API")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_bytes(data)
    print(f"OK: {title} ({len(data)} bytes) -> {OUT_PATH}")


if __name__ == "__main__":
    main()
