"""Vendor the New Central OpenAPI corpus into ``vendor/openapi/``.

``lookup_api`` is a headline capability, but the OpenAPI documents behind it
have never been part of a checkout: ``ingestion/openapi_registry_manifest.json``
records where they live upstream and ``ingestion/fetch_manifest_specs.py``
re-fetches them on demand, writing into a git-ignored scrape directory. On a
clean clone there is nothing to index.

This script fetches every registry the manifest declares straight from the
ReadMe api-registry -- the same transport ``fetch_manifest_specs.py`` uses and
byte-identical serialisation -- and writes the documents plus a provenance
manifest into ``vendor/openapi/``, which *is* committed.

The corpus is vendored all-or-nothing. Before anything is written, every
document must match the registry manifest on both its declared ``path_count``
and its ``spec_fingerprint`` content hash, so an upstream document that was
reworked -- even one that kept the same number of paths -- aborts the run
instead of half-replacing the committed corpus with content nothing has
reviewed.

Usage:
    python scripts/vendor_openapi_corpus.py [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from ingestion.readme_registry import (  # noqa: E402
    OasPointer,
    RegistryFetchError,
    fetch_registry_spec,
    spec_fingerprint,
)

REGISTRY_MANIFEST = ROOT / "ingestion" / "openapi_registry_manifest.json"
VENDOR = ROOT / "vendor" / "openapi"
MANIFEST_PATH = VENDOR / "MANIFEST.json"

#: These are HPE's own API descriptions, not open-source artefacts. State the
#: terms plainly rather than stamping a permissive licence we cannot grant.
LICENSE = (
    "Proprietary HPE Aruba Networking API documentation; redistributed verbatim "
    "for reference and offline API lookup -- see vendor/openapi/NOTICE.md"
)


def _serialize(spec: dict[str, Any]) -> str:
    """Serialise exactly as ``ingestion/fetch_manifest_specs.py`` does.

    Keeping the two byte-identical means a vendored document and a freshly
    fetched one can be compared with a plain diff.
    """
    return json.dumps(spec, indent=2, sort_keys=True)


def _fetch_all(registries: dict[str, dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    """Fetch every registry, or raise. Returns ``(entry, serialized_document)``."""
    fetched: list[tuple[dict[str, Any], str]] = []
    problems: list[str] = []
    for registry_id, entry in sorted(registries.items()):
        title = entry.get("title", registry_id)
        pointer = OasPointer(
            project=entry.get("project", "aruba-new-central"),
            version=entry.get("portal_version", ""),
            registry_id=registry_id,
        )
        try:
            spec = fetch_registry_spec(pointer)
        except RegistryFetchError as exc:
            problems.append(f"{title} ({registry_id}): fetch failed: {exc}")
            print(f"  FAIL {title}: {exc}")
            continue
        observed = len(spec.get("paths", {}))
        declared = entry.get("path_count")
        if declared is not None and observed != declared:
            problems.append(
                f"{title} ({registry_id}): {observed} paths, manifest declares {declared}"
            )
            print(f"  DRIFT {title}: {observed} paths != declared {declared}")
            continue
        # The path count alone would let a reworked document through unnoticed,
        # and the manifest's fingerprint is then copied into MANIFEST.json as
        # `registry_sha256`. Verify it against what we actually fetched, so that
        # field is evidence rather than an assertion taken on faith.
        fingerprint = spec_fingerprint(spec)
        pinned = entry.get("sha256")
        if pinned and fingerprint != pinned:
            problems.append(
                f"{title} ({registry_id}): content fingerprint {fingerprint} "
                f"!= manifest sha256 {pinned}"
            )
            print(f"  DRIFT {title}: content differs from the pinned fingerprint")
            continue
        print(f"  OK   {title} ({observed} paths, fingerprint matches)")
        fetched.append((entry, _serialize(spec)))
    if problems:
        raise SystemExit(
            "refusing to vendor a partial or mismatched corpus; "
            f"{len(problems)} of {len(registries)} registries are unusable:\n  "
            + "\n  ".join(problems)
        )
    return fetched


def _build_entry(entry: dict[str, Any], payload: str, fetched_on: str) -> dict[str, Any]:
    """Describe one vendored document.

    Two digests, deliberately: ``sha256`` covers the indent=2 bytes on disk, so
    a verifier can prove the committed file is unmodified. ``registry_sha256``
    is the registry manifest's compact ``spec_fingerprint`` over the same
    document's *content*, so a verifier can prove upstream published exactly
    this document. They are over different serialisations and never match.

    ``registry_sha256`` is taken from the registry manifest only after
    ``_fetch_all`` has confirmed the fetched document hashes to that value, so
    it records a checked fact rather than a restated pin.
    """
    name = Path(entry["output_path"]).name
    return {
        "file": name,
        "source_url": entry["source_url"],
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "fetched": fetched_on,
        "license": LICENSE,
        "title": entry.get("title", ""),
        "registry_id": entry["registry_id"],
        "path_count": entry.get("path_count", 0),
        "registry_sha256": entry["sha256"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vendor the New Central OpenAPI corpus.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and verify every registry but write nothing",
    )
    args = parser.parse_args(argv)

    registries: dict[str, dict[str, Any]] = json.loads(
        REGISTRY_MANIFEST.read_text(encoding="utf-8")
    )["registries"]
    print(f"registry manifest declares {len(registries)} registries")

    documents = _fetch_all(registries)
    fetched_on = date.today().isoformat()
    specs = [_build_entry(entry, payload, fetched_on) for entry, payload in documents]

    names = [spec["file"] for spec in specs]
    if len(set(names)) != len(names):
        raise SystemExit("registry manifest output_path basenames collide; cannot vendor")

    if args.dry_run:
        print(f"dry run: {len(specs)} documents verified, nothing written")
        return 0

    VENDOR.mkdir(parents=True, exist_ok=True)
    for spec, (_, payload) in zip(specs, documents, strict=True):
        (VENDOR / spec["file"]).write_text(payload, encoding="utf-8")
    stale = [p for p in VENDOR.glob("*.json") if p.name not in {*names, "MANIFEST.json"}]
    for path in stale:
        print(f"  removing stale document: {path.name}")
        path.unlink()

    MANIFEST_PATH.write_text(
        json.dumps({"schema_version": 1, "specs": specs}, indent=2) + "\n", encoding="utf-8"
    )
    total = sum((VENDOR / spec["file"]).stat().st_size for spec in specs)
    print(f"vendored {len(specs)} specs ({total / 1_000_000:.1f} MB) into {VENDOR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
