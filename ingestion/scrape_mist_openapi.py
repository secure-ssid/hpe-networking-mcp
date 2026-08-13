#!/usr/bin/env python3
"""Download the Juniper Mist OpenAPI 3.0 spec for exact API endpoint/schema lookup.

Mist publishes a full OpenAPI document for the entire Mist API (orgs, sites,
devices, WLANs, clients, etc.) on GitHub rather than behind a docs-site JSON
endpoint like scrape_openapi.py's Aruba source, so this just downloads the
single raw file.

Source: https://github.com/Mist-Automation-Programmability/mist_openapi
Usage: python ingestion/scrape_mist_openapi.py
Writes: ingestion/sources/openapi_specs/mist.openapi.json
Then rebuild the index: python -m hpe_networking_mcp.pipeline.clients.specs_index --build
"""
import json
import urllib.request
from pathlib import Path

SPEC_URL = (
    "https://raw.githubusercontent.com/Mist-Automation-Programmability/"
    "mist_openapi/main/mist.openapi.json"
)
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
