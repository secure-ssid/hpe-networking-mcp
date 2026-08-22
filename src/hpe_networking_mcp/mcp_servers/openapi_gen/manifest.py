"""Build, serialize, and load compact generated-operation manifests.

The manifest is a single committed JSON file per platform under
``src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/<platform>.json``. It records the source
spec digest plus one compact record per operation (name, method, path,
summary/description, parameters, request schema, content type, capability
classification, and stable operation key). We deliberately commit *one*
manifest -- not thousands of generated Python files.

Only manifests derived from specs we are licensed to redistribute (e.g. the
MIT-licensed Mist OpenAPI) should be committed. The raw upstream spec itself is
never committed from a gitignored path.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from hpe_networking_mcp.mcp_servers.openapi_gen.classify import classify
from hpe_networking_mcp.mcp_servers.openapi_gen.ir import SpecParser
from hpe_networking_mcp.mcp_servers.openapi_gen.naming import NameAllocator

SCHEMA_VERSION = 2
_PKG_DIR = Path(__file__).resolve().parent
MANIFEST_DIR = _PKG_DIR / "manifests"
OVERRIDE_DIR = _PKG_DIR / "overrides"


def manifest_path(platform: str) -> Path:
    return MANIFEST_DIR / f"{platform}.json"


def override_path(platform: str) -> Path:
    return OVERRIDE_DIR / f"{platform}.json"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_overrides(platform: str) -> dict[str, str]:
    """Load the capability override map for ``platform`` (empty if absent)."""
    path = override_path(platform)
    if not path.exists():
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    overrides = doc.get("capabilities", doc) if isinstance(doc, dict) else {}
    return {str(k): str(v) for k, v in overrides.items()}


def _merge_duplicate_record(existing: dict[str, Any], op: Any) -> None:
    """Merge parameters and request metadata from a duplicate method/path."""
    parameters = existing.setdefault("parameters", [])
    by_key = {(item.get("in"), item.get("name")): item for item in parameters}
    for parameter in (item.to_dict() for item in op.parameters):
        key = (parameter.get("in"), parameter.get("name"))
        current = by_key.get(key)
        if current is None:
            parameters.append(parameter)
            by_key[key] = parameter
            continue
        if parameter.get("required"):
            current["required"] = True
        if not current.get("description") and parameter.get("description"):
            current["description"] = parameter["description"]
        if not current.get("enum") and parameter.get("enum"):
            current["enum"] = parameter["enum"]
        for metadata_key in ("format", "style", "explode"):
            if metadata_key not in current and metadata_key in parameter:
                current[metadata_key] = parameter[metadata_key]

    if op.request_body is not None:
        incoming = op.request_body.to_dict()
        current_body = existing.get("request_body")
        if current_body is None:
            existing["request_body"] = incoming
        else:
            current_body["required"] = bool(
                current_body.get("required") or incoming.get("required")
            )
            current_properties = list(current_body.get("properties") or [])
            for name in incoming.get("properties") or []:
                if name not in current_properties:
                    current_properties.append(name)
            if current_properties:
                current_body["properties"] = current_properties
            required_properties = list(current_body.get("required_properties") or [])
            for name in incoming.get("required_properties") or []:
                if name not in required_properties:
                    required_properties.append(name)
            if required_properties:
                current_body["required_properties"] = required_properties
            property_formats = dict(current_body.get("property_formats") or {})
            property_formats.update(incoming.get("property_formats") or {})
            if property_formats:
                current_body["property_formats"] = dict(sorted(property_formats.items()))

    if op.tags:
        tags = list(existing.get("tags") or [])
        for tag in op.tags:
            if tag not in tags:
                tags.append(tag)
        existing["tags"] = tags
    if not existing.get("summary") and op.summary:
        existing["summary"] = op.summary
    if not existing.get("description") and op.description:
        existing["description"] = op.description
    if op.deprecated:
        existing["deprecated"] = True
    if op.sunset and not existing.get("sunset"):
        existing["sunset"] = op.sunset
    if op.security and not existing.get("security"):
        existing["security"] = op.security
    response_codes = sorted(
        set(existing.get("response_codes") or []) | set(op.response_codes)
    )
    if response_codes:
        existing["response_codes"] = response_codes


def _add_operation_metadata(record: dict[str, Any], op: Any) -> None:
    if op.deprecated:
        record["deprecated"] = True
    if op.sunset:
        record["sunset"] = op.sunset
    if op.security:
        record["security"] = op.security
    if op.response_codes:
        record["response_codes"] = op.response_codes


def build_manifest(
    spec: dict[str, Any],
    *,
    platform: str,
    source_file: str,
    source_sha256: str,
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a manifest dict from a parsed spec (deterministic ordering)."""
    parser = SpecParser(spec)
    operations = parser.operations()
    overrides = overrides or {}
    allocator = NameAllocator()

    records: list[dict[str, Any]] = []
    used_override_keys: set[str] = set()
    for op in operations:
        name = allocator.allocate(platform, op.method, op.path, op.operation_id)
        capability = classify(op.method, op.key, overrides)
        if op.key in overrides:
            used_override_keys.add(op.key)
        record: dict[str, Any] = {
            "name": name,
            "key": op.key,
            "method": op.method,
            "path": op.path,
            "capability": capability,
        }
        if op.operation_id:
            record["operation_id"] = op.operation_id
        if op.summary:
            record["summary"] = op.summary
        if op.description:
            record["description"] = op.description
        if op.tags:
            record["tags"] = op.tags
        record["parameters"] = [p.to_dict() for p in op.parameters]
        if op.request_body is not None:
            record["request_body"] = op.request_body.to_dict()
        _add_operation_metadata(record, op)
        records.append(record)

    stray = sorted(set(overrides) - used_override_keys)

    info = spec.get("info", {}) if isinstance(spec.get("info"), dict) else {}
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "platform": platform,
        "source": {
            "file": source_file,
            "sha256": source_sha256,
            "openapi": parser.version,
            "title": info.get("title", ""),
            "version": info.get("version", ""),
            "license": (info.get("license") or {}).get("name", ""),
            "operation_count": len(records),
        },
        "override_keys_applied": sorted(used_override_keys),
        "override_keys_unmatched": stray,
        "operations": records,
    }
    return manifest


def build_merged_manifest(
    documents: list[tuple[str, str, dict[str, Any]]],
    *,
    platform: str,
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build one deterministic manifest from multiple independently resolved specs.

    ``documents`` entries are ``(source_file, source_sha256, parsed_spec)``.
    Duplicate method/path operations are kept once from the lexicographically
    first source file and recorded in source metadata.
    """
    overrides = overrides or {}
    allocator = NameAllocator()
    records: list[dict[str, Any]] = []
    records_by_key: dict[str, dict[str, Any]] = {}
    used_override_keys: set[str] = set()
    seen_operations: dict[str, str] = {}
    duplicates: list[dict[str, str]] = []
    sources: list[dict[str, Any]] = []

    for source_file, source_sha256, spec in sorted(documents, key=lambda item: item[0]):
        parser = SpecParser(spec)
        operations = parser.operations()
        info = spec.get("info", {}) if isinstance(spec.get("info"), dict) else {}
        sources.append(
            {
                "file": source_file,
                "sha256": source_sha256,
                "openapi": parser.version,
                "title": info.get("title", ""),
                "version": info.get("version", ""),
                "operation_count": len(operations),
            }
        )
        for op in operations:
            if op.key in seen_operations:
                _merge_duplicate_record(records_by_key[op.key], op)
                duplicates.append(
                    {
                        "key": op.key,
                        "kept_source": seen_operations[op.key],
                        "duplicate_source": source_file,
                    }
                )
                continue
            seen_operations[op.key] = source_file
            name = allocator.allocate(platform, op.method, op.path, op.operation_id)
            capability = classify(op.method, op.key, overrides)
            if op.key in overrides:
                used_override_keys.add(op.key)
            record: dict[str, Any] = {
                "name": name,
                "key": op.key,
                "method": op.method,
                "path": op.path,
                "capability": capability,
                "source_file": source_file,
            }
            if op.operation_id:
                record["operation_id"] = op.operation_id
            if op.summary:
                record["summary"] = op.summary
            if op.description:
                record["description"] = op.description
            if op.tags:
                record["tags"] = op.tags
            record["parameters"] = [p.to_dict() for p in op.parameters]
            if op.request_body is not None:
                record["request_body"] = op.request_body.to_dict()
            _add_operation_metadata(record, op)
            records.append(record)
            records_by_key[op.key] = record

    digest_input = "\n".join(f"{source['file']}:{source['sha256']}" for source in sources)
    return {
        "schema_version": SCHEMA_VERSION,
        "platform": platform,
        "source": {
            "file_count": len(sources),
            "sha256": sha256_bytes(digest_input.encode()),
            "operation_count": len(records),
            "duplicate_operation_count": len(duplicates),
            "files": sources,
        },
        "override_keys_applied": sorted(used_override_keys),
        "override_keys_unmatched": sorted(set(overrides) - used_override_keys),
        "duplicate_operations": duplicates,
        "operations": records,
    }


def dumps(manifest: dict[str, Any]) -> str:
    """Serialize a manifest deterministically (stable, diff-friendly)."""
    return json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def write_manifest(platform: str, manifest: dict[str, Any]) -> Path:
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    path = manifest_path(platform)
    path.write_text(dumps(manifest), encoding="utf-8")
    return path


def load_manifest(platform: str) -> dict[str, Any]:
    path = manifest_path(platform)
    if not path.exists():
        raise FileNotFoundError(f"no generated manifest for platform {platform!r}: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def manifest_exists(platform: str) -> bool:
    return manifest_path(platform).exists()


def manifest_operation_count(platform: str) -> int:
    """Number of operations in the committed manifest (0 if absent)."""
    if not manifest_exists(platform):
        return 0
    return len(load_manifest(platform).get("operations", []))
