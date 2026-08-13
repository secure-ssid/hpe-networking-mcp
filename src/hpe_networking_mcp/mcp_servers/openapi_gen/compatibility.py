"""Deterministic compatibility checks for generated OpenAPI manifests."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from hpe_networking_mcp.mcp_servers.openapi_gen.ir import OpenApiError, SpecParser
from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import sha256_bytes


class CompatibilityError(Exception):
    """Raised when a document or compatibility baseline cannot be validated."""


def load_api_document(payload: bytes) -> dict[str, Any]:
    """Load a local JSON or YAML API document without retaining its raw content."""
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        try:
            document = yaml.safe_load(payload)
        except yaml.YAMLError as exc:
            raise CompatibilityError(f"malformed Swagger/OpenAPI document: {exc}") from exc
    if not isinstance(document, dict):
        raise CompatibilityError("Swagger/OpenAPI document must be an object")
    return document


def _base_paths(document: dict[str, Any]) -> list[str]:
    if str(document.get("swagger") or "") == "2.0":
        base = str(document.get("basePath") or "/")
        return [base.rstrip("/") or "/"]
    servers = document.get("servers")
    if not servers:
        return ["/"]
    paths: set[str] = set()
    for server in servers:
        if not isinstance(server, dict) or not isinstance(server.get("url"), str):
            raise CompatibilityError("OpenAPI server entries must contain a URL string")
        path = urlsplit(server["url"]).path
        if "{" in path or "}" in path:
            raise CompatibilityError("templated OpenAPI server base paths are unsupported")
        paths.add(path.rstrip("/") or "/")
    return sorted(paths)


def _security_schemes(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if str(document.get("swagger") or "") == "2.0":
        raw_schemes = document.get("securityDefinitions") or {}
    else:
        components = document.get("components") or {}
        raw_schemes = components.get("securitySchemes") or {}
    if not isinstance(raw_schemes, dict):
        raise CompatibilityError("security schemes must be an object")

    schemes: dict[str, dict[str, Any]] = {}
    for name, raw in raw_schemes.items():
        if not isinstance(raw, dict):
            raise CompatibilityError(f"security scheme {name!r} must be an object")
        item: dict[str, Any] = {"name": str(name), "type": str(raw.get("type") or "")}
        for key in ("in", "scheme", "bearerFormat", "flow", "authorizationUrl", "tokenUrl"):
            if raw.get(key) is not None:
                item[key] = str(raw[key])
        if raw.get("name") is not None:
            item["parameter_name"] = str(raw["name"])
        schemes[str(name)] = item
    return schemes


def _security_requirements(document: dict[str, Any]) -> list[list[list[str]]]:
    requirements: list[list[list[str]]] = []
    global_security = document.get("security")
    if isinstance(global_security, list):
        requirements.append(
            [
                sorted(str(name) for name in requirement)
                for requirement in global_security
                if isinstance(requirement, dict)
            ]
        )
    paths = document.get("paths") or {}
    if isinstance(paths, dict):
        for path in sorted(paths):
            item = paths[path]
            if not isinstance(item, dict):
                continue
            for method in ("get", "put", "post", "delete", "patch", "head", "options"):
                operation = item.get(method)
                if not isinstance(operation, dict) or "security" not in operation:
                    continue
                security = operation.get("security")
                if isinstance(security, list):
                    requirements.append(
                        [
                            sorted(str(name) for name in requirement)
                            for requirement in security
                            if isinstance(requirement, dict)
                        ]
                    )
    return requirements


def _is_supported_auth(scheme: dict[str, Any]) -> bool:
    scheme_type = scheme.get("type", "").lower()
    if scheme_type == "apikey":
        return scheme.get("in", "").lower() == "header" and bool(
            scheme.get("parameter_name")
        )
    return scheme_type == "http" and scheme.get("scheme", "").lower() == "bearer"


def _auth_report(
    document: dict[str, Any], provenance: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    schemes = _security_schemes(document)
    requirements = _security_requirements(document)
    supported_names = sorted(name for name, scheme in schemes.items() if _is_supported_auth(scheme))
    used_names = sorted(
        {
            name
            for alternatives in requirements
            for requirement in alternatives
            for name in requirement
        }
    )
    missing_definitions = sorted(set(used_names) - set(schemes))
    unsupported_used = sorted(
        name for name in used_names if name in schemes and not _is_supported_auth(schemes[name])
    )

    if requirements:
        alternatives_supported = all(
            any(
                not requirement
                or all(
                    name in schemes and _is_supported_auth(schemes[name])
                    for name in requirement
                )
                for requirement in alternatives
            )
            for alternatives in requirements
        )
    else:
        alternatives_supported = bool(supported_names)
    compatible = bool(schemes) and alternatives_supported and not missing_definitions

    target = [schemes[name] for name in sorted(schemes)]
    baseline = provenance.get("runtime_auth_schemes") or []
    target_signatures = {
        json.dumps(item, sort_keys=True, separators=(",", ":")) for item in target
    }
    baseline_signatures = {
        json.dumps(item, sort_keys=True, separators=(",", ":")) for item in baseline
    }
    return (
        {
            "runtime_supported": baseline,
            "target_declared": target,
            "added": [
                json.loads(item) for item in sorted(target_signatures - baseline_signatures)
            ],
            "missing": [
                json.loads(item) for item in sorted(baseline_signatures - target_signatures)
            ],
            "used_scheme_names": used_names,
            "supported_target_scheme_names": supported_names,
            "unsupported_used_scheme_names": unsupported_used,
            "missing_scheme_definitions": missing_definitions,
            "compatible": compatible,
        },
        compatible,
    )


def _operation_report(
    baseline_operations: list[dict[str, Any]], target_operations: list[Any]
) -> dict[str, Any]:
    baseline_by_key = {str(item["key"]): item for item in baseline_operations}
    target_by_key = {item.key: item for item in target_operations}
    exact_keys = set(baseline_by_key) & set(target_by_key)

    baseline_remaining = [
        item for key, item in baseline_by_key.items() if key not in exact_keys
    ]
    target_remaining = [item for key, item in target_by_key.items() if key not in exact_keys]
    baseline_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    target_by_id: dict[str, list[Any]] = defaultdict(list)
    for item in baseline_remaining:
        if item.get("operation_id"):
            baseline_by_id[str(item["operation_id"])].append(item)
    for item in target_remaining:
        if item.operation_id:
            target_by_id[str(item.operation_id)].append(item)

    matched_baseline_keys: set[str] = set()
    matched_target_keys: set[str] = set()
    method_changed: list[dict[str, str]] = []
    path_changed: list[dict[str, str]] = []
    for operation_id in sorted(set(baseline_by_id) & set(target_by_id)):
        old_items = baseline_by_id[operation_id]
        new_items = target_by_id[operation_id]
        if len(old_items) != 1 or len(new_items) != 1:
            continue
        old = old_items[0]
        new = new_items[0]
        matched_baseline_keys.add(str(old["key"]))
        matched_target_keys.add(new.key)
        change = {
            "operation_id": operation_id,
            "baseline_method": str(old["method"]),
            "baseline_path": str(old["path"]),
            "target_method": new.method,
            "target_path": new.path,
        }
        if old["method"] != new.method:
            method_changed.append(change)
        if old["path"] != new.path:
            path_changed.append(change)

    added = sorted(
        key
        for key in target_by_key
        if key not in exact_keys and key not in matched_target_keys
    )
    removed = sorted(
        key
        for key in baseline_by_key
        if key not in exact_keys and key not in matched_baseline_keys
    )
    return {
        "baseline_count": len(baseline_by_key),
        "target_count": len(target_by_key),
        "unchanged_count": len(exact_keys),
        "added": added,
        "removed": removed,
        "method_changed": method_changed,
        "path_changed": path_changed,
    }


def build_compatibility_report(
    *,
    payload: bytes,
    source_name: str,
    baseline_manifest: dict[str, Any],
    baseline_manifest_bytes: bytes,
    provenance: dict[str, Any],
    expected_source_sha256: str | None = None,
) -> dict[str, Any]:
    """Compare one local API document with a committed generated manifest."""
    source_sha256 = sha256_bytes(payload)
    document = load_api_document(payload)
    try:
        parser = SpecParser(document)
        target_operations = parser.operations()
    except OpenApiError as exc:
        raise CompatibilityError(str(exc)) from exc

    expected_manifest_sha = str(provenance.get("manifest_sha256") or "")
    actual_manifest_sha = sha256_bytes(baseline_manifest_bytes)
    manifest_current = bool(expected_manifest_sha) and actual_manifest_sha == expected_manifest_sha
    expected_count = int(provenance.get("operation_count") or 0)
    count_current = (
        expected_count > 0
        and len(baseline_manifest.get("operations") or []) == expected_count
        and baseline_manifest.get("source", {}).get("operation_count") == expected_count
    )
    digest_matches = (
        expected_source_sha256 is None or source_sha256 == expected_source_sha256.lower()
    )

    base_paths = _base_paths(document)
    supported_base_paths = sorted(
        str(item) for item in provenance.get("supported_base_paths", ["/"])
    )
    base_paths_compatible = bool(base_paths) and all(
        item in supported_base_paths for item in base_paths
    )
    auth, auth_compatible = _auth_report(document, provenance)
    operations = _operation_report(
        list(baseline_manifest.get("operations") or []), target_operations
    )
    operations_compatible = not any(
        operations[key]
        for key in ("added", "removed", "method_changed", "path_changed")
    )

    reasons: list[str] = []
    if not manifest_current:
        reasons.append("committed manifest digest does not match EdgeConnect provenance")
    if not count_current:
        reasons.append("committed manifest operation count is not the expected baseline")
    if not digest_matches:
        reasons.append("target source digest does not match --expect-sha256")
    if not base_paths_compatible:
        reasons.append("target base path is incompatible with generated runtime assumptions")
    if not auth_compatible:
        reasons.append("target authentication is not safely supported by the runtime")
    if not operations_compatible:
        reasons.append("target operation mappings differ from the committed manifest")

    info = document.get("info") if isinstance(document.get("info"), dict) else {}
    compatible = not reasons
    return {
        "schema_version": 1,
        "source": {
            "file": Path(source_name).name,
            "sha256": source_sha256,
            "expected_sha256": expected_source_sha256,
            "digest_matches": digest_matches,
            "document_format": (
                f"Swagger {document['swagger']}"
                if document.get("swagger")
                else f"OpenAPI {document.get('openapi', '')}"
            ),
            "declared_api_version": str(info.get("version") or ""),
        },
        "baseline": {
            "manifest_sha256": actual_manifest_sha,
            "expected_manifest_sha256": expected_manifest_sha,
            "manifest_digest_matches": manifest_current,
            "operation_count_matches": count_current,
        },
        "operations": operations,
        "authentication": auth,
        "base_path": {
            "target": base_paths,
            "runtime_supported": supported_base_paths,
            "added": sorted(set(base_paths) - set(supported_base_paths)),
            "missing": sorted(set(supported_base_paths) - set(base_paths)),
            "compatible": base_paths_compatible,
        },
        "verdict": {
            "compatible": compatible,
            "value": "compatible" if compatible else "incompatible",
            "reasons": reasons,
        },
    }


def dumps_report(report: dict[str, Any]) -> str:
    """Serialize a compatibility report deterministically."""
    return json.dumps(report, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
