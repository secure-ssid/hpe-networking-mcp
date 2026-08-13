#!/usr/bin/env python3
"""Rebuild the digest-pinned Axis reviewed-operation manifest.

Generation accepts either an upstream repository checkout via ``--source-dir``
or an explicit network fetch via ``--fetch``. ``--check`` needs neither and
verifies the committed manifest against the reviewed, pinned registry.
"""

from __future__ import annotations

import argparse
import ast
import sys
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import (  # noqa: E402
    SCHEMA_VERSION,
    dumps,
    manifest_path,
    sha256_bytes,
    write_manifest,
)

REPOSITORY = "nowireless4u/hpe-networking-mcp"
COMMIT = "a1b2afaac11001fa75a9b04bc8a3d0d5c0ffc387"
AXIS_ROOT = "src/hpe_networking_mcp/platforms/axis"
SOURCE_FILES: tuple[tuple[str, str], ...] = (
    (
        f"{AXIS_ROOT}/__init__.py",
        "f74b56a8caafba43646d89031458fdd1749d952d88f60d7066f4d9a0d4568e13",
    ),
    (
        f"{AXIS_ROOT}/client.py",
        "bc0d83035d6a689a221e0b958c50a8a7e30592e622dd1ad8bc5b666c6d3cb5b3",
    ),
    (
        f"{AXIS_ROOT}/tools/_manage.py",
        "93b2cb9667feab6d8eb783b2d869b75dd5916fe14e25afc47fe57ad2bd3d215f",
    ),
    (
        f"{AXIS_ROOT}/tools/application_groups.py",
        "62ad52e96b7aedea65ace3ce68b3693580b84d9a6dffaa7c284f97af28231ce0",
    ),
    (
        f"{AXIS_ROOT}/tools/applications.py",
        "eae385cec54797965022bf2723d1126ea92020f5c6600b4c03038fb260c0cf41",
    ),
    (
        f"{AXIS_ROOT}/tools/commit.py",
        "9527c24735a4dfd88753b7b00fa24344766bb64c6010fc9a271061d21c3fd3c7",
    ),
    (
        f"{AXIS_ROOT}/tools/connector_zones.py",
        "0ca0738256186bbe85660d1de2b2201912f26a518fef413e735e08f1271630c2",
    ),
    (
        f"{AXIS_ROOT}/tools/connectors.py",
        "6fcea307619bb695dd1304d3221b489c882fc1730a93087efbd79547950c8a2b",
    ),
    (
        f"{AXIS_ROOT}/tools/groups.py",
        "a5766a15c1b3890bd440ce6d9c3168ffe8467694add4c319f8dc9025789adcc2",
    ),
    (
        f"{AXIS_ROOT}/tools/locations.py",
        "5bf166403c865fd64e455a610734d392dc09b5dd945bfdc9c0b663f6a51349a1",
    ),
    (
        f"{AXIS_ROOT}/tools/ssl_exclusions.py",
        "3751d95495d854e1d7fa6572069f96c4bec75fc6be8e11d7de7e5b05e3bb836b",
    ),
    (
        f"{AXIS_ROOT}/tools/status.py",
        "87749556c7d2734ec16b0014eb71bb8a595b88e672073c32f3e72532e4d14298",
    ),
    (
        f"{AXIS_ROOT}/tools/tunnels.py",
        "e4707d650b70ce8f032df920249f527a4269d00aeda3c1c6cd5828742f77a7a0",
    ),
    (
        f"{AXIS_ROOT}/tools/users.py",
        "ec154f47ac033dfa9e9bf5cf0c8a0e068b57bfa396b53f4f61358617b430567e",
    ),
    (
        f"{AXIS_ROOT}/tools/web_categories.py",
        "c06bb23a78029a578a8175a1cb9f12a3450213dc3153d78e3ec23e1c1129ba74",
    ),
)
EXPECTED_DIGESTS = dict(SOURCE_FILES)
ENABLED_REGISTRY: dict[str, list[str]] = {
    "application_groups": [
        "axis_get_application_groups",
        "axis_manage_application_group",
    ],
    "applications": ["axis_get_applications", "axis_manage_application"],
    "connector_zones": ["axis_get_connector_zones", "axis_manage_connector_zone"],
    "connectors": [
        "axis_get_connectors",
        "axis_manage_connector",
        "axis_regenerate_connector",
    ],
    "groups": ["axis_get_groups", "axis_manage_group"],
    "locations": [
        "axis_get_locations",
        "axis_get_sub_locations",
        "axis_manage_location",
        "axis_manage_sub_location",
    ],
    "ssl_exclusions": ["axis_get_ssl_exclusions", "axis_manage_ssl_exclusion"],
    "status": ["axis_get_status"],
    "tunnels": ["axis_get_tunnels", "axis_manage_tunnel"],
    "users": ["axis_get_users", "axis_manage_user"],
    "web_categories": ["axis_get_web_categories", "axis_manage_web_category"],
    "commit": ["axis_commit_changes"],
}
DISABLED_REGISTRY: dict[str, list[str]] = {
    "custom_ip_categories": [
        "axis_get_custom_ip_categories",
        "axis_manage_custom_ip_category",
    ],
    "ip_feed_categories": [
        "axis_get_ip_feed_categories",
        "axis_manage_ip_feed_category",
    ],
}

_ENTITY_SPECS = (
    (
        "application_groups",
        "axis_get_application_groups",
        "axis_manage_application_group",
        "/Tags",
        "application_group_id",
        "application groups",
        "application group",
    ),
    (
        "applications",
        "axis_get_applications",
        "axis_manage_application",
        "/Applications",
        "application_id",
        "applications",
        "application",
    ),
    (
        "connector_zones",
        "axis_get_connector_zones",
        "axis_manage_connector_zone",
        "/ConnectorZones",
        "connector_zone_id",
        "connector zones",
        "connector zone",
    ),
    (
        "connectors",
        "axis_get_connectors",
        "axis_manage_connector",
        "/Connectors",
        "connector_id",
        "connectors",
        "connector",
    ),
    (
        "groups",
        "axis_get_groups",
        "axis_manage_group",
        "/Groups",
        "group_id",
        "groups",
        "group",
    ),
    (
        "locations",
        "axis_get_locations",
        "axis_manage_location",
        "/Locations",
        "location_id",
        "locations",
        "location",
    ),
    (
        "ssl_exclusions",
        "axis_get_ssl_exclusions",
        "axis_manage_ssl_exclusion",
        "/SslExclusions",
        "ssl_exclusion_id",
        "SSL exclusions",
        "SSL exclusion",
    ),
    (
        "tunnels",
        "axis_get_tunnels",
        "axis_manage_tunnel",
        "/Tunnels",
        "tunnel_id",
        "tunnels",
        "tunnel",
    ),
    (
        "users",
        "axis_get_users",
        "axis_manage_user",
        "/Users",
        "user_id",
        "users",
        "user",
    ),
    (
        "web_categories",
        "axis_get_web_categories",
        "axis_manage_web_category",
        "/WebCategories",
        "web_category_id",
        "web categorys",
        "web category",
    ),
)


class AxisSourceError(ValueError):
    """Raised when reviewed Axis source no longer matches the pinned registry."""


def _parameter(
    name: str,
    type_name: str,
    *,
    required: bool,
    default: Any = None,
    enum: list[str] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "name": name,
        "type": type_name,
        "required": required,
    }
    if default is not None:
        record["default"] = default
    if enum is not None:
        record["enum"] = enum
    return record


def _crud_operations(
    manage_name: str,
    path: str,
    id_arg: str,
    label: str,
    *,
    parent_id_arg: str | None = None,
    kind_prefix: str = "",
) -> list[dict[str, Any]]:
    """Split the single upstream fused ``manage_entity`` dispatch (POST create,
    PUT update, DELETE delete — see the pinned ``tools/_manage.py`` helper)
    into three distinct, separately annotated generated operations.

    All three share ``source_name`` (the single upstream ``axis_manage_*``
    function) for source-provenance validation, since the split is purely a
    generated-surface decision — the upstream fused signature is unchanged.
    """
    slug = manage_name.removeprefix("axis_manage_")
    next_step = "Call axis_commit_changes to apply these staged changes."
    parent_params = (
        [_parameter(parent_id_arg, "string", required=True)] if parent_id_arg else []
    )
    return [
        {
            "name": f"axis_create_{slug}",
            "key": f"POST {path}",
            "method": "POST",
            "path": path,
            "kind": f"{kind_prefix}create",
            "capability": "write",
            "summary": f"Create an Axis {label}; changes are staged until commit.",
            "source_name": manage_name,
            "id_arg": id_arg,
            "label": label,
            "next_step": next_step,
            "parameters": [
                *parent_params,
                _parameter("payload", "object", required=True),
            ],
        },
        {
            "name": f"axis_update_{slug}",
            "key": f"PUT {path}/{{{id_arg}}}",
            "method": "PUT",
            "path": path,
            "kind": f"{kind_prefix}update",
            "capability": "write",
            "summary": f"Update an Axis {label}; changes are staged until commit.",
            "source_name": manage_name,
            "id_arg": id_arg,
            "label": label,
            "next_step": next_step,
            "parameters": [
                *parent_params,
                _parameter(id_arg, "string", required=True),
                _parameter("payload", "object", required=True),
            ],
        },
        {
            "name": f"axis_delete_{slug}",
            "key": f"DELETE {path}/{{{id_arg}}}",
            "method": "DELETE",
            "path": path,
            "kind": f"{kind_prefix}delete",
            "capability": "destructive",
            "summary": f"Delete an Axis {label}; changes are staged until commit.",
            "source_name": manage_name,
            "id_arg": id_arg,
            "label": label,
            "next_step": next_step,
            "parameters": [
                *parent_params,
                _parameter(id_arg, "string", required=True),
            ],
        },
    ]


def _entity_operations(
    get_name: str,
    manage_name: str,
    path: str,
    id_arg: str,
    plural: str,
    label: str,
) -> list[dict[str, Any]]:
    return [
        {
            "name": get_name,
            "key": f"QUERY {path}",
            "method": "GET",
            "path": path,
            "kind": "query",
            "capability": "read",
            "summary": f"List or get Axis {plural}.",
            "id_arg": id_arg,
            "parameters": [
                _parameter(id_arg, "string", required=False),
                _parameter("page_number", "integer", required=False, default=1),
                _parameter("page_size", "integer", required=False, default=50),
            ],
        },
        *_crud_operations(manage_name, path, id_arg, label),
    ]


def reviewed_operations() -> list[dict[str, Any]]:
    """Return the deterministic, human-reviewed Axis operation metadata."""
    operations: list[dict[str, Any]] = []
    for category, get_name, manage_name, path, id_arg, plural, label in _ENTITY_SPECS:
        operations.extend(
            _entity_operations(get_name, manage_name, path, id_arg, plural, label)
        )
        if category == "connectors":
            operations.append(
                {
                    "name": "axis_regenerate_connector",
                    "key": "POST /Connectors/{connector_id}/regenerate",
                    "method": "POST",
                    "path": "/Connectors/{connector_id}/regenerate",
                    "kind": "action",
                    "capability": "destructive",
                    "summary": (
                        "Regenerate a connector installation command, invalidating "
                        "the prior command."
                    ),
                    "timeout": 30,
                    "parameters": [
                        _parameter("connector_id", "string", required=True),
                    ],
                }
            )
        if category == "locations":
            operations.extend(
                [
                    {
                        "name": "axis_get_sub_locations",
                        "key": "QUERY /Locations/{location_id}/SubLocations",
                        "method": "GET",
                        "path": "/Locations/{location_id}/SubLocations",
                        "kind": "subquery",
                        "capability": "read",
                        "summary": (
                            "List or get Axis sub-locations under a parent location."
                        ),
                        "id_arg": "sub_location_id",
                        "parameters": [
                            _parameter("location_id", "string", required=True),
                            _parameter(
                                "sub_location_id", "string", required=False
                            ),
                            _parameter(
                                "page_number", "integer", required=False, default=1
                            ),
                            _parameter(
                                "page_size", "integer", required=False, default=50
                            ),
                        ],
                    },
                    *_crud_operations(
                        "axis_manage_sub_location",
                        "/Locations/{location_id}/SubLocations",
                        "sub_location_id",
                        "sub-location",
                        parent_id_arg="location_id",
                        kind_prefix="sub",
                    ),
                ]
            )
    operations.extend(
        [
            {
                "name": "axis_get_status",
                "key": "GET /{entity_type}/{entity_id}/status",
                "method": "GET",
                "path": "/{entity_type}/{entity_id}/status",
                "kind": "status",
                "capability": "read",
                "summary": "Get runtime status for an Axis connector or tunnel.",
                "parameters": [
                    _parameter(
                        "entity_type",
                        "string",
                        required=True,
                        enum=["connector", "tunnel"],
                    ),
                    _parameter("entity_id", "string", required=True),
                ],
            },
            {
                "name": "axis_commit_changes",
                "key": "POST /Commit",
                "method": "POST",
                "path": "/Commit",
                "kind": "action",
                "capability": "write",
                "summary": "Commit all staged Axis configuration changes.",
                "timeout": 120,
                "parameters": [],
            },
        ]
    )
    return operations


def _aggregate_digest(files: list[dict[str, str]]) -> str:
    digest_input = "\n".join(f"{item['file']}:{item['sha256']}" for item in files)
    return sha256_bytes(digest_input.encode())


def build_axis_manifest(
    sources: Mapping[str, bytes] | None = None,
    *,
    expected_digests: Mapping[str, str] = EXPECTED_DIGESTS,
) -> dict[str, Any]:
    """Build the schema-v2 Axis manifest, validating sources when supplied."""
    if sources is not None:
        verify_source_digests(sources, expected_digests=expected_digests)
        validate_reviewed_sources(sources)
    files = [
        {"file": path, "sha256": expected_digests[path]}
        for path, _digest in SOURCE_FILES
    ]
    operations = reviewed_operations()
    return {
        "schema_version": SCHEMA_VERSION,
        "platform": "axis",
        "source": {
            "kind": "reviewed-derived-registry",
            "repository": f"https://github.com/{REPOSITORY}",
            "commit": COMMIT,
            "license": "MIT",
            "official_openapi": False,
            "sha256": _aggregate_digest(files),
            "operation_count": len(operations),
            "files": files,
            "provenance": (
                "Reviewed derivation of the 25 tools enabled by the upstream Axis "
                "TOOLS registry. No official distributable Axis OpenAPI "
                "specification was available or claimed."
            ),
            "excluded_upstream_tools": [
                name for names in DISABLED_REGISTRY.values() for name in names
            ],
            "exclusion_reason": (
                "Upstream keeps these four implementations disabled because the "
                "current service returns HTTP 403 even for read/write tokens."
            ),
        },
        "operations": operations,
    }


def verify_source_digests(
    sources: Mapping[str, bytes],
    *,
    expected_digests: Mapping[str, str] = EXPECTED_DIGESTS,
) -> None:
    """Require exactly the reviewed files and their pinned SHA-256 digests."""
    missing = sorted(set(expected_digests) - set(sources))
    unexpected = sorted(set(sources) - set(expected_digests))
    if missing or unexpected:
        raise AxisSourceError(
            f"Axis source set changed: missing={missing}, unexpected={unexpected}"
        )
    for path, expected in expected_digests.items():
        received = sha256_bytes(sources[path])
        if received != expected:
            raise AxisSourceError(
                f"Axis source digest mismatch for {path}: expected {expected}, "
                f"received {received}"
            )


def _literal_string_dict(node: ast.AST, *, label: str) -> dict[str, list[str]]:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError) as exc:
        raise AxisSourceError(f"Axis {label} is no longer a literal registry") from exc
    if not isinstance(value, dict) or not all(
        isinstance(key, str)
        and isinstance(names, list)
        and all(isinstance(name, str) for name in names)
        for key, names in value.items()
    ):
        raise AxisSourceError(f"Axis {label} has an unsupported shape")
    return value


def parse_registries(source: bytes) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Extract the enabled and disabled tool registries from Axis ``__init__.py``."""
    tree = ast.parse(source)
    assignments: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                assignments[node.target.id] = node.value
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value
    try:
        tools = _literal_string_dict(assignments["TOOLS"], label="TOOLS")
        disabled = _literal_string_dict(
            assignments["_DISABLED_TOOLS"], label="_DISABLED_TOOLS"
        )
    except KeyError as exc:
        raise AxisSourceError(f"Axis registry {exc.args[0]} is missing") from exc
    return tools, disabled


def validate_registry_source(source: bytes) -> None:
    """Require the upstream enabled and disabled registries to match review."""
    enabled, disabled = parse_registries(source)
    if enabled != ENABLED_REGISTRY:
        raise AxisSourceError("Axis enabled TOOLS registry changed from the reviewed pin")
    if disabled != DISABLED_REGISTRY:
        raise AxisSourceError("Axis disabled tool registry changed from the reviewed pin")


def _decorator_capability(node: ast.AsyncFunctionDef) -> str | None:
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        if not isinstance(decorator.func, ast.Name) or decorator.func.id != "tool":
            continue
        for keyword in decorator.keywords:
            if keyword.arg != "capability":
                continue
            value = keyword.value
            if isinstance(value, ast.Attribute):
                return value.attr
    return None


def _format_path(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if node.value.startswith("/") else None
    if not isinstance(node, ast.JoinedStr):
        return None
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            expression = value.value
            if (
                isinstance(expression, ast.Call)
                and isinstance(expression.func, ast.Name)
                and expression.func.id == "path_seg"
                and expression.args
            ):
                expression = expression.args[0]
            if isinstance(expression, ast.Name):
                parts.append(f"{{{expression.id}}}")
            else:
                parts.append("{}")
    path = "".join(parts)
    return path if path.startswith("/") else None


def _function_metadata(source: bytes) -> dict[str, dict[str, Any]]:
    tree = ast.parse(source)
    functions: dict[str, dict[str, Any]] = {}
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef) or not node.name.startswith("axis_"):
            continue
        paths = {
            path
            for child in ast.walk(node)
            if (path := _format_path(child)) is not None
        }
        functions[node.name] = {
            "capability": _decorator_capability(node),
            "parameters": [arg.arg for arg in node.args.args],
            "paths": paths,
        }
    return functions


def _expected_source_parameters(operation: dict[str, Any]) -> list[str]:
    """Return the expected upstream function signature for ``operation``.

    Generated operations produced by :func:`_crud_operations` carry a
    ``source_name`` pointing at the single upstream fused
    ``manage_entity``-backed function (``action_type``/``payload``/id/
    ``confirmed``); the create/update/delete split is a generated-surface
    decision only and does not change the upstream signature being verified.
    """
    if "source_name" in operation:
        return ["ctx", "action_type", "payload", operation["id_arg"], "confirmed"]
    names = [parameter["name"] for parameter in operation["parameters"]]
    if operation["capability"] != "read":
        names.append("confirmed")
    return ["ctx", *names]


def validate_reviewed_sources(sources: Mapping[str, bytes]) -> None:
    """Validate registry membership, function signatures, paths, and capabilities."""
    init_path = f"{AXIS_ROOT}/__init__.py"
    validate_registry_source(sources[init_path])

    metadata: dict[str, dict[str, Any]] = {}
    for path, _digest in SOURCE_FILES:
        if "/tools/" in path and not path.endswith("_manage.py"):
            metadata.update(_function_metadata(sources[path]))

    expected_names = {
        name for names in ENABLED_REGISTRY.values() for name in names
    }
    actual_names = set(metadata)
    if actual_names != expected_names:
        raise AxisSourceError(
            "Axis enabled function set changed: "
            f"missing={sorted(expected_names - actual_names)}, "
            f"unexpected={sorted(actual_names - expected_names)}"
        )

    upstream_capabilities: dict[str, str] = {}
    for operation in reviewed_operations():
        source_key = operation.get("source_name", operation["name"])
        if operation["capability"] == "read":
            upstream_capabilities[source_key] = "READ"
        elif source_key in {"axis_regenerate_connector", "axis_commit_changes"}:
            upstream_capabilities[source_key] = "OPERATIONAL"
        else:
            upstream_capabilities[source_key] = "WRITE_DELETE"

    status_paths = {"/Connectors/{entity_id}/status", "/Tunnels/{entity_id}/status"}
    for operation in reviewed_operations():
        name = operation["name"]
        source_key = operation.get("source_name", name)
        found = metadata[source_key]
        expected_parameters = _expected_source_parameters(operation)
        if found["parameters"] != expected_parameters:
            raise AxisSourceError(
                f"Axis signature changed for {source_key} (generated as {name}): "
                f"expected {expected_parameters}, received {found['parameters']}"
            )
        expected_capability = upstream_capabilities[source_key]
        if found["capability"] != expected_capability:
            raise AxisSourceError(
                f"Axis capability changed for {source_key} (generated as {name}): "
                f"expected {expected_capability}, received {found['capability']}"
            )
        if name == "axis_get_status":
            if not status_paths.issubset(found["paths"]):
                raise AxisSourceError(f"Axis status paths changed: {found['paths']}")
        elif operation["path"] not in found["paths"]:
            raise AxisSourceError(
                f"Axis path changed for {source_key} (generated as {name}): "
                f"expected {operation['path']}, received {sorted(found['paths'])}"
            )


def load_local_sources(source_dir: Path) -> dict[str, bytes]:
    """Read the reviewed source set from an upstream repository checkout."""
    sources: dict[str, bytes] = {}
    for path, _digest in SOURCE_FILES:
        local_path = source_dir / path
        if not local_path.is_file():
            raise AxisSourceError(f"Axis source file is missing: {local_path}")
        sources[path] = local_path.read_bytes()
    return sources


def fetch_pinned_sources() -> dict[str, bytes]:
    """Fetch only reviewed files from the pinned upstream commit."""
    sources: dict[str, bytes] = {}
    base = f"https://raw.githubusercontent.com/{REPOSITORY}/{COMMIT}"
    for path, _digest in SOURCE_FILES:
        request = urllib.request.Request(
            f"{base}/{path}",
            headers={"User-Agent": "hpe-networking-mcp-axis-manifest-generation"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            sources[path] = response.read()
    verify_source_digests(sources)
    return sources


def check_manifest(path: Path | None = None) -> None:
    """Raise when the committed Axis manifest differs from deterministic output."""
    output = path or manifest_path("axis")
    expected = dumps(build_axis_manifest())
    if not output.exists() or output.read_text() != expected:
        raise AxisSourceError(
            f"{output} is stale; regenerate with --source-dir PATH or --fetch"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "--source-dir",
        type=Path,
        help="root of a local checkout of the pinned upstream repository",
    )
    source_group.add_argument(
        "--fetch",
        action="store_true",
        help=f"explicitly fetch reviewed files from {REPOSITORY}@{COMMIT}",
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    try:
        if args.check and args.source_dir is None and not args.fetch:
            check_manifest()
            print(f"{manifest_path('axis')} is current (offline pin check)")
            return 0
        if args.source_dir is None and not args.fetch:
            parser.error("generation requires --source-dir PATH or explicit --fetch")
        sources = (
            load_local_sources(args.source_dir)
            if args.source_dir is not None
            else fetch_pinned_sources()
        )
        manifest = build_axis_manifest(sources)
        if args.check:
            expected = dumps(manifest)
            output = manifest_path("axis")
            if not output.exists() or output.read_text() != expected:
                raise AxisSourceError(f"{output} is stale; regenerate it")
            print(f"{output} is current and its pinned sources are valid")
            return 0
        output = write_manifest("axis", manifest)
        print(
            f"Wrote {output}: {manifest['source']['operation_count']} operations, "
            f"sha256 {manifest['source']['sha256']}"
        )
        return 0
    except (OSError, SyntaxError, AxisSourceError) as exc:
        print(f"Axis manifest generation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
