#!/usr/bin/env python3
"""Generate (or verify) the committed HPE GreenLake (GLP) operation manifest.

Source of truth: the **MIT-licensed** community project
``nowireless4u/hpe-networking-mcp``, which vendors the public HPE GreenLake
OpenAPI specifications under ``vendor/greenlake/*.json`` (fetched from HPE's
developer portal by that project's ``fetch_greenlake_oas.py``). Those raw specs
are HPE proprietary and are **never committed here** -- this helper fetches them
locally (or reads a pre-fetched directory), derives a compact operation manifest
(names / methods / paths / parameters + a per-source SHA-256 digest), and writes
only that manifest to ``src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/glp.json``.

The upstream reference is pinned to a specific commit so digests are
reproducible. Regenerate with::

    uv run python scripts/generate_glp_tools.py --fetch          # download + build
    uv run python scripts/generate_glp_tools.py --spec-dir DIR   # build from local specs
    uv run python scripts/generate_glp_tools.py --spec-dir DIR --check   # CI drift gate
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from hpe_networking_mcp.mcp_servers.openapi_gen import manifest as manifest_mod  # noqa: E402
from hpe_networking_mcp.mcp_servers.openapi_gen.ir import OpenApiError, SpecParser  # noqa: E402

PLATFORM = "glp"

UPSTREAM_REPO = "https://github.com/nowireless4u/hpe-networking-mcp"
UPSTREAM_REF = "a1b2afaac11001fa75a9b04bc8a3d0d5c0ffc387"
UPSTREAM_LICENSE = "MIT"
VENDOR_DIR = "vendor/greenlake"
RAW_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/nowireless4u/hpe-networking-mcp/"
    "{ref}/{vendor_dir}/{file}"
)

# The 30 vendored GreenLake OpenAPI spec filenames at the pinned upstream ref.
# ``sources.json`` is an upstream spec-index manifest (``{_comment, specs}``),
# not an OpenAPI document, so it contributes no operations and is excluded.
VENDOR_SPECS: tuple[str, ...] = (
    "audit-logs.json",
    "authorization__authz-v1beta1-external-authz-v2-config.json",
    "authorization__groups-v1beta1-external-groups-v1beta1.json",
    "backup-recovery.json",
    "block-storage.json",
    "compute-ops-mgmt.json",
    "consumption-analytics.json",
    "credentials.json",
    "data-services.json",
    "device-management.json",
    "event__webhook-v1beta1-system-webhook-v1beta1-nbapi.json",
    "event__webhook-v1beta1-webhook-v1beta1-nbapi.json",
    "flex.json",
    "identity__identity-v1-nb-openapi-identity.json",
    "location-management.json",
    "object-storage.json",
    "private-cloud-business.json",
    "reporting.json",
    "scim.json",
    "service-catalog__service-catalog-v1beta1-service-catalog-v1.json",
    "service-catalog__service-provision-nbapi-v1beta1-service-provision-v1beta1.json",
    "service-catalog__service-registry-v1beta1-service-catalog-v1beta1-nbapi.json",
    "sources.json",
    "storage-fleet.json",
    "subscription-management.json",
    "sustainability.json",
    "tags.json",
    "virtualization.json",
    "wellness.json",
    "workspace__workspace-management-v1-nb-openapi-workspace.json",
)

DEFAULT_SPEC_DIR = _REPO_ROOT / "ingestion" / "greenlake_specs"


def raw_url(file: str) -> str:
    return RAW_URL_TEMPLATE.format(ref=UPSTREAM_REF, vendor_dir=VENDOR_DIR, file=file)


def fetch_specs(spec_dir: Path) -> None:
    """Download the pinned vendored GreenLake specs into ``spec_dir`` (local-only)."""
    spec_dir.mkdir(parents=True, exist_ok=True)
    for file in VENDOR_SPECS:
        url = raw_url(file)
        dest = spec_dir / file
        with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 (pinned HTTPS)
            dest.write_bytes(resp.read())
        print(f"fetched {file}")


def build_glp_manifest(spec_dir: Path) -> dict[str, Any]:
    documents: list[tuple[str, str, dict[str, Any]]] = []
    servers_by_file: dict[str, list[str]] = {}
    excluded: list[dict[str, str]] = []
    for file in sorted(VENDOR_SPECS):
        path = spec_dir / file
        if not path.exists():
            raise SystemExit(
                f"Missing vendored spec {file} in {spec_dir}. Run with --fetch first."
            )
        raw = path.read_bytes()
        spec = json.loads(raw)
        digest = manifest_mod.sha256_bytes(raw)
        try:
            SpecParser(spec).operations()
        except OpenApiError as exc:
            excluded.append({"file": file, "sha256": digest, "reason": str(exc)})
            continue
        servers_by_file[file] = [
            str(server["url"]).rstrip("/")
            for server in spec.get("servers", [])
            if isinstance(server, dict) and server.get("url")
        ]
        documents.append((file, digest, spec))

    manifest = manifest_mod.build_merged_manifest(
        documents,
        platform=PLATFORM,
        overrides=manifest_mod.load_overrides(PLATFORM),
    )
    for operation in manifest["operations"]:
        operation["server_urls"] = servers_by_file.get(operation["source_file"], [])
        if operation["name"] == "glp_create_location_csv":
            operation["request_body"] = {
                "required": True,
                "content_type": "multipart/form-data",
                "schema_type": "object",
                "description": (
                    "CSV upload. Pass body.file as an object containing filename, "
                    "content_base64, and optional content_type."
                ),
                "properties": ["file"],
            }

    provenance = {
        "upstream_repo": UPSTREAM_REPO,
        "upstream_ref": UPSTREAM_REF,
        "upstream_license": UPSTREAM_LICENSE,
        "vendor_dir": VENDOR_DIR,
        "raw_url_template": RAW_URL_TEMPLATE,
        "excluded_sources": excluded,
        "note": (
            "Raw HPE GreenLake OpenAPI specs are HPE proprietary and are NOT committed. "
            "Only this derived operation manifest (names/methods/paths/parameters plus "
            "per-source sha256 digests) is committed. Regenerate with "
            "scripts/generate_glp_tools.py --fetch (specs are pinned to upstream_ref)."
        ),
    }
    # Insert provenance right after the source-provenance block for readability.
    ordered: dict[str, Any] = {}
    for key, value in manifest.items():
        ordered[key] = value
        if key == "source":
            ordered["provenance"] = provenance
    return ordered


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--spec-dir",
        default=str(DEFAULT_SPEC_DIR),
        help="local directory of vendored GreenLake OpenAPI specs (gitignored)",
    )
    ap.add_argument(
        "--fetch",
        action="store_true",
        help="download the pinned vendored specs into --spec-dir before building",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="verify the committed manifest matches a fresh build; do not write",
    )
    args = ap.parse_args()

    spec_dir = Path(args.spec_dir)
    if not spec_dir.is_absolute():
        spec_dir = _REPO_ROOT / spec_dir
    if args.fetch:
        fetch_specs(spec_dir)
    if not spec_dir.exists():
        print(f"Spec directory not found: {spec_dir} (run with --fetch)", file=sys.stderr)
        return 2

    manifest = build_glp_manifest(spec_dir)
    rendered = manifest_mod.dumps(manifest)
    out_path = manifest_mod.manifest_path(PLATFORM)

    if args.check:
        if not out_path.exists():
            print(f"DRIFT: manifest missing at {out_path.relative_to(_REPO_ROOT)}", file=sys.stderr)
            return 1
        if out_path.read_text() != rendered:
            print(
                "DRIFT: committed GLP manifest is stale. Re-run without --check to regenerate.",
                file=sys.stderr,
            )
            return 1
        print(
            f"OK: GLP manifest current ({manifest['source']['operation_count']} operations, "
            f"{len(manifest['provenance']['excluded_sources'])} excluded, "
            f"source sha256 {manifest['source']['sha256'][:12]})."
        )
        return 0

    manifest_mod.write_manifest(PLATFORM, manifest)
    print(
        f"Wrote {out_path.relative_to(_REPO_ROOT)} "
        f"({manifest['source']['operation_count']} operations, "
        f"{len(manifest['provenance']['excluded_sources'])} excluded)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
