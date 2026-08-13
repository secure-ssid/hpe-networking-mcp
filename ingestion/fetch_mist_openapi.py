#!/usr/bin/env python3
"""Fetch the pinned official Mist OpenAPI snapshot for RAG/API lookup.

The generated spec is written under ``ingestion/sources/openapi_specs`` and
is intentionally git-ignored. The commit and digest below make rebuilds
reproducible; update both only after reviewing a newer mistsys/mist_openapi
release.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

REPOSITORY = "mistsys/mist_openapi"
DEFAULT_REF = "f374cffdd5a275c7954645a306fcab7f1227e7a3"
DEFAULT_PATH = "mist.openapi.json"
DEFAULT_SHA256 = "db47f2f4ed809fa83ef429807d80adf097d2610300acb435e500df4a0f062739"
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parent / "sources" / "openapi_specs" / "mist-openapi.json"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default=DEFAULT_REF)
    parser.add_argument("--path", default=DEFAULT_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--expected-sha256")
    args = parser.parse_args()

    expected = args.expected_sha256
    if expected is None and args.ref == DEFAULT_REF and args.path == DEFAULT_PATH:
        expected = DEFAULT_SHA256
    elif expected is None:
        raise SystemExit(
            "--expected-sha256 is required when overriding the pinned Mist "
            "--ref or --path"
        )

    url = f"https://raw.githubusercontent.com/{REPOSITORY}/{args.ref}/{args.path}"
    request = urllib.request.Request(url, headers={"User-Agent": "hpe-networking-mcp-openapi-ingestion"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read()

    digest = hashlib.sha256(payload).hexdigest()
    if expected and digest != expected:
        raise SystemExit(
            f"Mist OpenAPI digest mismatch: expected {expected}, received {digest}"
        )

    spec = json.loads(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload)
    print(
        f"Wrote {args.output} from {REPOSITORY}@{args.ref}: "
        f"version {spec.get('info', {}).get('version', 'unknown')}, "
        f"{len(spec.get('paths', {}))} paths, sha256 {digest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
