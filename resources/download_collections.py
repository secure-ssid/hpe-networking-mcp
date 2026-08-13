"""
Download HPE Aruba Networking Central Postman collections into this directory.

Requires a Postman API key — set POSTMAN_API_KEY env var or pass via --api-key.
Get a key at: https://www.postman.com/settings/me/api-keys

Usage:
    python resources/download_collections.py
    python resources/download_collections.py --api-key YOUR_KEY
"""

import argparse
import os
import sys
import json

try:
    import httpx
except ImportError:
    sys.exit("httpx is not installed — run: uv sync  (or pip install httpx)")

COLLECTION_ID = "32717089-1d8b9f9e-2137-4a7d-b735-1b3c06f87e70"
POSTMAN_API_URL = f"https://api.getpostman.com/collections/{COLLECTION_ID}"

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))


def download(api_key: str) -> None:
    headers = {"X-Api-Key": api_key}
    print(f"Fetching collection {COLLECTION_ID} ...")
    resp = httpx.get(POSTMAN_API_URL, headers=headers, timeout=30)
    if resp.status_code == 401:
        sys.exit("Invalid Postman API key.")
    if resp.status_code == 404:
        sys.exit("Collection not found — it may be private or the ID changed.")
    resp.raise_for_status()

    data = resp.json()
    collection_name = data.get("collection", {}).get("info", {}).get("name", "collection")
    filename = f"{collection_name}.postman_collection.json"
    out_path = os.path.join(OUTPUT_DIR, filename)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data["collection"], f, indent=2)

    print(f"Saved → {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Aruba Central Postman collections.")
    parser.add_argument("--api-key", default=os.environ.get("POSTMAN_API_KEY", ""))
    args = parser.parse_args()

    if not args.api_key:
        sys.exit(
            "Postman API key required.\n"
            "  Set POSTMAN_API_KEY env var, or pass --api-key YOUR_KEY\n"
            "  Get a key at: https://www.postman.com/settings/me/api-keys"
        )

    download(args.api_key)


if __name__ == "__main__":
    main()
