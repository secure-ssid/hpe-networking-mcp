#!/usr/bin/env python3
"""Validate committed generated-operation manifests without vendor API calls."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from hpe_networking_mcp.mcp_servers.openapi_gen.classify import CAPABILITIES  # noqa: E402
from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import (  # noqa: E402
    MANIFEST_DIR,
    SCHEMA_VERSION,
)
from scripts.build_optional_product_manifests import (  # noqa: E402
    SourcePinError,
    check_committed_offline,
    provenance_path,
)
from scripts.generate_axis_manifest import check_manifest as check_axis_manifest  # noqa: E402


def validate_manifest(path: Path) -> tuple[str, int]:
    doc = json.loads(path.read_text())
    platform = str(doc.get("platform", ""))
    if platform != path.stem:
        raise ValueError(f"{path}: platform {platform!r} does not match filename")
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"{path}: unsupported schema_version {doc.get('schema_version')!r}")
    operations = doc.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ValueError(f"{path}: operations must be a non-empty list")
    source = doc.get("source")
    if not isinstance(source, dict) or source.get("operation_count") != len(operations):
        raise ValueError(f"{path}: source.operation_count does not match operations")
    derived_registry = source.get("official_openapi") is False

    names: set[str] = set()
    keys: set[str] = set()
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise ValueError(f"{path}: operation {index} is not an object")
        name = operation.get("name")
        key = operation.get("key")
        capability = operation.get("capability")
        if not isinstance(name, str) or not name.startswith(f"{platform}_"):
            raise ValueError(f"{path}: invalid generated name at operation {index}: {name!r}")
        if name in names:
            raise ValueError(f"{path}: duplicate generated name {name!r}")
        names.add(name)
        if not isinstance(key, str) or " " not in key:
            raise ValueError(f"{path}: invalid operation key at operation {index}: {key!r}")
        if key in keys:
            raise ValueError(f"{path}: duplicate operation key {key!r}")
        keys.add(key)
        if capability not in CAPABILITIES:
            raise ValueError(f"{path}: invalid capability {capability!r} for {key}")
        method, key_path = key.split(" ", 1)
        if (
            not derived_registry
            and (method != operation.get("method") or key_path != operation.get("path"))
        ):
            raise ValueError(f"{path}: key/method/path mismatch for {name}")
    return platform, len(operations)


def validate_all(manifest_dir: Path = MANIFEST_DIR) -> list[tuple[str, int]]:
    paths = sorted(manifest_dir.glob("*.json"))
    if not paths:
        raise ValueError(f"no generated manifests found under {manifest_dir}")
    results = [validate_manifest(path) for path in paths]
    if (manifest_dir / "axis.json").exists():
        check_axis_manifest(manifest_dir / "axis.json")
    if manifest_dir == MANIFEST_DIR:
        for platform, _count in results:
            if provenance_path(platform).exists() and platform != "edgeconnect":
                check_committed_offline(platform)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-dir", type=Path, default=MANIFEST_DIR)
    args = parser.parse_args()
    try:
        results = validate_all(args.manifest_dir)
    except (OSError, ValueError, json.JSONDecodeError, SourcePinError) as exc:
        print(f"Generated manifest validation failed: {exc}", file=sys.stderr)
        return 1
    for platform, count in results:
        print(f"{platform}: {count} generated operations")
    print(f"{sum(count for _, count in results)} generated operations validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
