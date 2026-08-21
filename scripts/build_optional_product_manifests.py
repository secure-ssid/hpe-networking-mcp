#!/usr/bin/env python3
"""Build the derived generated-operation manifests for the optional product
backends: ClearPass (CPPM), ArubaOS 8, UXI, and Apstra.

Provenance / licensing
----------------------
* ClearPass, AOS8, and UXI operation metadata is fetched at build time from the
  Aruba developer portal's ReadMe SuperHub ``api-registry`` (the same public
  registry the docs ingestion pipeline uses). Only the *derived* compact
  operation manifest is written under
  ``src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/<platform>.json`` — the raw proprietary
  OpenAPI documents are never committed.
* Apstra has no distributable full OpenAPI spec, so its manifest is a reviewed
  operation set derived from the pinned official Juniper ``aos-sdk-api``
  package. This is explicitly NOT full OpenAPI coverage.

Usage::

    uv run python scripts/build_optional_product_manifests.py            # all
    uv run python scripts/build_optional_product_manifests.py --platform uxi
    uv run python scripts/build_optional_product_manifests.py --check     # live CI drift
    uv run python scripts/build_optional_product_manifests.py --check --offline

Network access to ``dash.readme.com`` is required for clearpass/uxi/aos8 (not
for apstra). Source and committed-manifest digests are pinned separately under
``src/hpe_networking_mcp/mcp_servers/openapi_gen/provenance``. A normal build or live ``--check``
fails closed when upstream source content changes. ``--check --offline``
validates the committed manifest against those pins without network access.
After reviewing an intentional upstream change, ``--update-provenance`` writes
the new manifest and pins together.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from hpe_networking_mcp.mcp_servers.openapi_gen import manifest as M  # noqa: E402
from ingestion import readme_registry as rr  # noqa: E402

PORTAL = "https://developer.arubanetworks.com"
PROVENANCE_DIR = _REPO_ROOT / "src" / "hpe_networking_mcp" / "mcp_servers" / "openapi_gen" / "provenance"

# ClearPass 6.12.x groups its ~335-path surface into 16 ReadMe api-registry
# categories. Discovered by walking the portal reference sidebar and reading
# each page's ``oasPublicUrl`` pointer (see ingestion/readme_registry.py).
CPPM_REGISTRIES: dict[str, str] = {
    "318h42r1xml85aa55": "API Operations",
    "1ajpg01cml85anrq": "Certificate Authority",
    "at4cgf25ml85b3v0": "Endpoint Visibility",
    "at4cgf1qml85bng3": "Enforcement Profile",
    "15btrd1aeml85c5da": "Global Server Configuration",
    "15btrd12ml85csbt": "Guest Actions",
    "at4cgfzml85da6p": "Guest Configuration",
    "1wxkxoml85duds": "Identities",
    "318h42r1cml85e6m3": "Insight",
    "a48hf0fml85elb0": "Integrations",
    "1ajpg031ml85ey7m": "Local Server Configuration",
    "a48hf0uml85faaa": "Logs",
    "a48hf010ml85fo52": "Platform Certificates",
    "a48hf01qml85g8pg": "Policy Elements",
    "at4cgf25ml85gob5": "Session Control",
    "at4cgf38ml85h0ju": "Tools And Utilities",
}


class SourcePinError(RuntimeError):
    """Generated content no longer matches its reviewed source pins."""


def _fetch(rid: str) -> dict:
    return rr.fetch_registry_spec(rr.OasPointer("x", "v", rid))


def _sha(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


def build_clearpass() -> dict:
    docs = []
    prov = []
    for rid, cat in CPPM_REGISTRIES.items():
        spec = _fetch(rid)
        sha = _sha(spec)
        fname = f"cppm-{cat.lower().replace(' ', '-')}-{rid}.json"
        docs.append((fname, sha, spec))
        prov.append(
            {
                "category": cat,
                "registry_id": rid,
                "registry_url": f"{rr.REGISTRY_BASE_URL}/{rid}",
                "portal_project": "aruba-cppm",
                "sha256": sha,
                "openapi": spec.get("openapi"),
                "title": spec.get("info", {}).get("title"),
                "spec_version": spec.get("info", {}).get("version"),
                "path_count": len(spec.get("paths", {})),
            }
        )
    man = M.build_merged_manifest(
        docs,
        platform="clearpass",
        overrides=M.load_overrides("clearpass"),
    )
    man["provenance"] = {
        "acquired_from": "Aruba developer portal ReadMe SuperHub API registries",
        "portal": f"{PORTAL}/cppm/reference",
        "spec_version": "6.12.7",
        "note": "Derived operation metadata only; raw proprietary specs are not committed.",
        "registries": sorted(prov, key=lambda p: p["category"]),
    }
    return man


def build_single(
    platform: str,
    rid: str,
    spec_version: str,
    portal_ref: str,
    strip_params: list[str] | None = None,
) -> dict:
    spec = _fetch(rid)
    sha = _sha(spec)
    man = M.build_manifest(
        spec,
        platform=platform,
        source_file=f"{platform}-{rid}.json",
        source_sha256=sha,
        overrides=M.load_overrides(platform),
    )
    stripped = {p.lower() for p in (strip_params or [])}
    occurrences = 0
    if stripped:
        for op in man["operations"]:
            kept = []
            for prm in op.get("parameters", []):
                if prm.get("name", "").lower() in stripped:
                    occurrences += 1
                    continue
                kept.append(prm)
            op["parameters"] = kept
    man["provenance"] = {
        "acquired_from": "Aruba developer portal ReadMe SuperHub API registry",
        "portal": portal_ref,
        "registry_id": rid,
        "registry_url": f"{rr.REGISTRY_BASE_URL}/{rid}",
        "spec_version": spec_version,
        "spec_title": spec.get("info", {}).get("title"),
        "note": "Derived operation metadata only; raw proprietary specs are not committed.",
    }
    if stripped:
        man["provenance"]["stripped_auth_parameters"] = sorted(strip_params or [])
        man["provenance"]["stripped_auth_parameter_occurrences"] = occurrences
    return man


def build_apstra() -> dict:
    from scripts._apstra_operations import build_apstra_manifest

    return build_apstra_manifest()


_BUILDERS = {
    "clearpass": build_clearpass,
    "uxi": lambda: build_single("uxi", "2j1jmli8l514", "6.7.0", f"{PORTAL}/uxi/reference"),
    "aos8": lambda: build_single(
        "aos8",
        "cjpas1kkx7bible",
        "8.0 (ArubaOS JSON API 1.0)",
        f"{PORTAL}/aos8/reference",
        strip_params=["UIDARUBA"],
    ),
    "apstra": build_apstra,
}


def provenance_path(platform: str) -> Path:
    return PROVENANCE_DIR / f"{platform}.json"


def load_source_pin(platform: str) -> dict[str, Any]:
    path = provenance_path(platform)
    if not path.exists():
        raise SourcePinError(f"missing source provenance pin: {path.relative_to(_REPO_ROOT)}")
    try:
        pin = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SourcePinError(
            f"invalid source provenance pin {path.relative_to(_REPO_ROOT)}: {exc}"
        ) from exc
    if not isinstance(pin, dict):
        raise SourcePinError(
            f"invalid source provenance pin {path.relative_to(_REPO_ROOT)}: expected object"
        )
    return pin


def _source_registry_ids(manifest: dict[str, Any]) -> list[str]:
    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict):
        return []
    registries = provenance.get("registries")
    if isinstance(registries, list):
        return sorted(
            str(item.get("registry_id"))
            for item in registries
            if isinstance(item, dict) and item.get("registry_id")
        )
    registry_id = provenance.get("registry_id")
    return [str(registry_id)] if registry_id else []


def build_source_pin(platform: str, manifest: dict[str, Any], rendered: str) -> dict[str, Any]:
    source = manifest.get("source")
    if not isinstance(source, dict) or not source.get("sha256"):
        raise SourcePinError(f"{platform} manifest is missing source.sha256")
    provenance = manifest.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    return {
        "schema_version": 1,
        "platform": platform,
        "generator": "scripts/build_optional_product_manifests.py",
        "source_sha256": str(source["sha256"]),
        "manifest_sha256": M.sha256_bytes(rendered.encode()),
        "operation_count": len(manifest.get("operations") or []),
        "registry_ids": _source_registry_ids(manifest),
        "source": {
            key: provenance[key]
            for key in ("acquired_from", "portal", "source_url", "spec_version")
            if provenance.get(key) is not None
        },
    }


def validate_source_pin(
    platform: str,
    manifest: dict[str, Any],
    rendered: str,
    pin: dict[str, Any] | None = None,
) -> None:
    expected = pin or load_source_pin(platform)
    actual = build_source_pin(platform, manifest, rendered)
    mismatches = [
        key
        for key in ("platform", "source_sha256", "manifest_sha256", "operation_count")
        if expected.get(key) != actual.get(key)
    ]
    if list(expected.get("registry_ids") or []) != actual["registry_ids"]:
        mismatches.append("registry_ids")
    if mismatches:
        detail = ", ".join(mismatches)
        raise SourcePinError(
            f"{platform} generated content does not match reviewed provenance "
            f"({detail}); inspect the upstream change, then rerun with "
            "--update-provenance if it is intentional"
        )


def write_source_pin(platform: str, manifest: dict[str, Any], rendered: str) -> Path:
    path = provenance_path(platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_source_pin(platform, manifest, rendered), indent=2) + "\n")
    return path


def check_committed_offline(platform: str) -> None:
    out_path = M.manifest_path(platform)
    if not out_path.exists():
        raise SourcePinError(f"missing committed manifest: {out_path.relative_to(_REPO_ROOT)}")
    try:
        manifest = json.loads(out_path.read_text())
    except json.JSONDecodeError as exc:
        raise SourcePinError(
            f"invalid committed manifest {out_path.relative_to(_REPO_ROOT)}: {exc}"
        ) from exc
    rendered = M.dumps(manifest)
    if out_path.read_text() != rendered:
        raise SourcePinError(
            f"{platform} committed manifest is not in deterministic serialized form"
        )
    validate_source_pin(platform, manifest, rendered)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--platform",
        choices=sorted(_BUILDERS),
        help="build one platform (default: all)",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="verify committed manifests are current; do not write",
    )
    ap.add_argument(
        "--offline",
        action="store_true",
        help="with --check, validate committed manifests against pins without network access",
    )
    ap.add_argument(
        "--update-provenance",
        action="store_true",
        help="after reviewing upstream changes, write manifests and provenance pins together",
    )
    args = ap.parse_args()
    if args.offline and not args.check:
        ap.error("--offline requires --check")
    if args.update_provenance and (args.check or args.offline):
        ap.error("--update-provenance cannot be combined with --check or --offline")

    platforms = [args.platform] if args.platform else list(_BUILDERS)
    rc = 0
    for platform in platforms:
        try:
            if args.offline:
                check_committed_offline(platform)
                count = len(M.load_manifest(platform).get("operations") or [])
                print(f"OK: {platform} manifest and provenance pins valid ({count} ops, offline).")
                continue

            man = _BUILDERS[platform]()
            rendered = M.dumps(man)
            out_path = M.manifest_path(platform)
            count = len(man["operations"])
            if not args.update_provenance:
                validate_source_pin(platform, man, rendered)
            if args.check:
                if not out_path.exists() or out_path.read_text() != rendered:
                    print(
                        f"DRIFT: {platform} manifest is missing or stale ({count} ops).",
                        file=sys.stderr,
                    )
                    rc = 1
                else:
                    print(f"OK: {platform} manifest current ({count} ops).")
            else:
                M.write_manifest(platform, man)
                if args.update_provenance:
                    pin_path = write_source_pin(platform, man, rendered)
                    print(f"Wrote {pin_path.relative_to(_REPO_ROOT)}.")
                print(f"Wrote {out_path.relative_to(_REPO_ROOT)} ({count} operations).")
        except SourcePinError as exc:
            print(f"DRIFT: {exc}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
