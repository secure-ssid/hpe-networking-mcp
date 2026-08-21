"""Vendor the offline OpenAPI corpus into ``vendor/openapi/``.

``lookup_api`` is a headline capability, but the OpenAPI documents behind it
have never been part of a checkout: ``ingestion/openapi_registry_manifest.json``
records where they live upstream and ``ingestion/fetch_manifest_specs.py``
re-fetches them on demand, writing into a git-ignored scrape directory. On a
clean clone there is nothing to index.

Two kinds of upstream feed the corpus, and both are pinned:

* **ReadMe api-registry** -- the 30 HPE New Central documents. Fetched straight
  from the registry, the same transport ``fetch_manifest_specs.py`` uses and
  byte-identical serialisation, then verified against the registry manifest's
  declared ``path_count`` and ``spec_fingerprint``.
* **Commit pins** -- ``COMMIT_PINS`` below. Fetched from an immutable
  ``raw.githubusercontent.com`` commit URL, never a branch, and verified
  against a pinned SHA-256 over the bytes upstream serves. These documents are
  written to disk verbatim: they arrive as files rather than as parsed objects,
  so their digest only reproduces if nothing re-serialises them.

Either way the document is written plus a provenance manifest into
``vendor/openapi/``, which *is* committed.

The corpus is vendored all-or-nothing across both kinds. Every document must
verify before anything is written, so an upstream document that was reworked --
even one that kept the same number of paths -- aborts the run instead of
half-replacing the committed corpus with content nothing has reviewed.

Usage:
    python scripts/vendor_openapi_corpus.py [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
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

#: GitHub serves raw files to anonymous clients; identify ourselves anyway.
PIN_HEADERS = {
    "User-Agent": "hpe-networking-mcp vendor_openapi_corpus",
    "Accept": "application/json",
}


@dataclass(frozen=True)
class CommitPin:
    """One upstream file, pinned to an immutable commit and a content digest.

    ``sha256`` is over the bytes upstream serves, not over any re-serialisation
    of them, which is exactly why these documents are written verbatim. Every
    field here is a claim about upstream that ``_fetch_pins`` checks before the
    document is allowed anywhere near the corpus.
    """

    file: str
    repo: str
    commit: str
    path: str
    sha256: str
    license: str

    @property
    def source_url(self) -> str:
        """The immutable raw URL. A branch name here would defeat the pin."""
        return f"https://raw.githubusercontent.com/{self.repo}/{self.commit}/{self.path}"


#: Documents pinned to a git commit rather than a ReadMe registry.
#:
#: Declared here rather than in a JSON file beside the corpus. A pin is only
#: meaningful together with the code that verifies it; one entry does not pay
#: for a loader, a schema and a test of its own; and a module-level tuple is
#: committed, diffed and reviewed in exactly the way a data file would be. What
#: has to match the registry path is the *behaviour* -- fetch, verify, hard stop
#: before writing -- not the file layout.
COMMIT_PINS: tuple[CommitPin, ...] = (
    CommitPin(
        file="mist.openapi.json",
        repo="mistsys/mist_openapi",
        commit="315b30ff4fa65c1dc3a2b5c1f27931e1b14ed01e",
        path="mist.openapi.json",
        sha256="2c3d769ef188bbce1b9db7a0774b5a10812d0a5bc11960b768de47b66bb88bbf",
        license="MIT",
    ),
)


def _serialize(spec: dict[str, Any]) -> str:
    """Serialise exactly as ``ingestion/fetch_manifest_specs.py`` does.

    Keeping the two byte-identical means a vendored document and a freshly
    fetched one can be compared with a plain diff. This applies to the registry
    documents only -- a commit-pinned document is never re-serialised.
    """
    return json.dumps(spec, indent=2, sort_keys=True)


def _fetch_registries(
    registries: dict[str, dict[str, Any]],
    fetched_on: str,
    problems: list[str],
) -> list[tuple[dict[str, Any], bytes]]:
    """Fetch every ReadMe registry. Returns ``(manifest_entry, payload)``."""
    fetched: list[tuple[dict[str, Any], bytes]] = []
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
        payload = _serialize(spec).encode("utf-8")
        fetched.append((_registry_entry(entry, payload, fetched_on), payload))
    return fetched


def _fetch_pins(
    pins: tuple[CommitPin, ...],
    fetched_on: str,
    problems: list[str],
) -> list[tuple[dict[str, Any], bytes]]:
    """Fetch every commit-pinned document. Returns ``(manifest_entry, payload)``.

    The digest is checked over the exact bytes received, before the document is
    parsed and before anything is written, so a moved tag, a rewritten file or
    a truncated response stops the whole run rather than landing unreviewed
    content in the corpus.
    """
    fetched: list[tuple[dict[str, Any], bytes]] = []
    for pin in pins:
        request = urllib.request.Request(pin.source_url, headers=PIN_HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=180) as response:  # noqa: S310
                payload = response.read()
        except (urllib.error.URLError, OSError) as exc:
            problems.append(f"{pin.file} ({pin.repo}@{pin.commit[:12]}): fetch failed: {exc}")
            print(f"  FAIL {pin.file}: {exc}")
            continue
        digest = hashlib.sha256(payload).hexdigest()
        if digest != pin.sha256:
            problems.append(
                f"{pin.file} ({pin.repo}@{pin.commit[:12]}): upstream digest {digest} "
                f"!= pinned sha256 {pin.sha256}"
            )
            print(f"  DRIFT {pin.file}: upstream bytes differ from the pinned digest")
            continue
        try:
            document = json.loads(payload)
        except json.JSONDecodeError as exc:
            problems.append(f"{pin.file}: pinned content is not JSON: {exc}")
            print(f"  FAIL {pin.file}: pinned content is not JSON")
            continue
        observed = len(document.get("paths") or {})
        print(f"  OK   {pin.file} ({observed} paths, digest matches)")
        fetched.append((_pin_entry(pin, document, digest, observed, fetched_on), payload))
    return fetched


def _fetch_all(
    registries: dict[str, dict[str, Any]],
    pins: tuple[CommitPin, ...],
    fetched_on: str,
) -> list[tuple[dict[str, Any], bytes]]:
    """Fetch and verify every document of both kinds, or raise.

    Problems from both sources are collected into one list and raised together,
    so a failed pin aborts the registry corpus too. There is one write phase and
    it is reached only when nothing is wrong.
    """
    problems: list[str] = []
    documents = _fetch_registries(registries, fetched_on, problems)
    documents += _fetch_pins(pins, fetched_on, problems)
    if problems:
        raise SystemExit(
            "refusing to vendor a partial or mismatched corpus; "
            f"{len(problems)} of {len(registries) + len(pins)} documents are unusable:\n  "
            + "\n  ".join(problems)
        )
    return documents


def _registry_entry(entry: dict[str, Any], payload: bytes, fetched_on: str) -> dict[str, Any]:
    """Describe one vendored registry document.

    Two digests, deliberately: ``sha256`` covers the indent=2 bytes on disk, so
    a verifier can prove the committed file is unmodified. ``registry_sha256``
    is the registry manifest's compact ``spec_fingerprint`` over the same
    document's *content*, so a verifier can prove upstream published exactly
    this document. They are over different serialisations and never match.

    ``registry_sha256`` is taken from the registry manifest only after
    ``_fetch_registries`` has confirmed the fetched document hashes to that
    value, so it records a checked fact rather than a restated pin.
    """
    name = Path(entry["output_path"]).name
    return {
        "file": name,
        "source_url": entry["source_url"],
        "sha256": hashlib.sha256(payload).hexdigest(),
        "fetched": fetched_on,
        "license": LICENSE,
        "title": entry.get("title", ""),
        "registry_id": entry["registry_id"],
        "path_count": entry.get("path_count", 0),
        "registry_sha256": entry["sha256"],
    }


def _pin_entry(
    pin: CommitPin,
    document: dict[str, Any],
    digest: str,
    path_count: int,
    fetched_on: str,
) -> dict[str, Any]:
    """Describe one vendored commit-pinned document.

    One digest here, not two: the file on disk *is* the upstream file, so
    ``sha256`` proves both that the commit is unmodified and that we did not
    touch it. ``title`` and ``path_count`` are read off the verified document
    rather than restated in the pin -- the digest already fixes them, and a
    second copy could only ever go stale.
    """
    return {
        "file": pin.file,
        "source_url": pin.source_url,
        "sha256": digest,
        "fetched": fetched_on,
        "license": pin.license,
        "title": str(document.get("info", {}).get("title") or pin.file),
        "upstream_repo": pin.repo,
        "upstream_commit": pin.commit,
        "path_count": path_count,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vendor the offline OpenAPI corpus.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and verify every document but write nothing",
    )
    args = parser.parse_args(argv)

    registries: dict[str, dict[str, Any]] = json.loads(
        REGISTRY_MANIFEST.read_text(encoding="utf-8")
    )["registries"]
    print(
        f"registry manifest declares {len(registries)} registries; "
        f"{len(COMMIT_PINS)} commit-pinned documents"
    )

    fetched_on = date.today().isoformat()
    documents = _fetch_all(registries, COMMIT_PINS, fetched_on)
    specs = [entry for entry, _ in documents]

    names = [spec["file"] for spec in specs]
    if len(set(names)) != len(names):
        raise SystemExit("vendored document filenames collide; cannot vendor")

    if args.dry_run:
        print(f"dry run: {len(specs)} documents verified, nothing written")
        return 0

    VENDOR.mkdir(parents=True, exist_ok=True)
    for spec, payload in documents:
        (VENDOR / spec["file"]).write_bytes(payload)
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
