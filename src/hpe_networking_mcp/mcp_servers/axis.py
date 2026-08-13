"""MCP server — optional Axis Atmos Cloud backend.

The 47-tool registry is a reviewed derivation of the current MIT-licensed
``nowireless4u/hpe-networking-mcp`` Axis backend (25 upstream tools). Axis has
not published a distributable OpenAPI document used by this project;
provenance and the exact reviewed source digest are committed in
``src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/axis.json``. Upstream fuses create/
update/delete for each entity behind one ``action_type``-driven function
(see ``tools/_manage.py``); hpe-networking-mcp splits each into distinct
``axis_create_*``/``axis_update_*``/``axis_delete_*`` generated operations
with exact write/destructive capability annotations per verb.

Auth/env:
  AXIS_BASE_URL    defaults to https://admin-api.axissecurity.com/api/v1.0
  AXIS_API_TOKEN   static bearer token generated in the Axis admin portal
"""

from __future__ import annotations

import hashlib
import inspect
import json
from typing import Any, Optional
from urllib.parse import quote

import httpx
from mcp.server.mcpserver import MCPServer

from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import load_manifest
from hpe_networking_mcp.mcp_servers.shared import (
    DESTRUCTIVE,
    READ_ONLY,
    WRITE,
    bound_collection_response,
    bounded_response_payload,
    clamp_limit,
    redact_sensitive,
    validate_product_base_url,
)
from hpe_networking_mcp.mcp_servers.shared import (
    platform_write_blocked as _platform_write_blocked,
)
from hpe_networking_mcp.mcp_servers.shared import (
    platform_writes_allowed as _platform_writes_allowed,
)

mcp = MCPServer("axis-core")

_AXIS_DEFAULT_BASE_URL = "https://admin-api.axissecurity.com/api/v1.0"
_AXIS_MAX_RESPONSE_BYTES = 131_072
_AXIS_EXECUTE_HINT = "Review the request, then call again with dry_run=False and confirm=True."
_AXIS_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def optional_product_writes_allowed() -> bool:
    return _platform_writes_allowed("axis")


def optional_product_write_blocked(
    tool_name: str,
    *,
    capability: str = "write",
    dry_run_state: str = "default_preview",
) -> dict[str, Any]:
    return _platform_write_blocked(
        "axis",
        tool_name,
        capability=capability,
        supports_dry_run=True,
        dry_run_state=dry_run_state,
        supports_confirm=True,
        requires_confirmation=True,
    )


def _axis_config() -> tuple[str | None, str | None]:
    import os

    base_url = os.getenv("AXIS_BASE_URL", _AXIS_DEFAULT_BASE_URL).strip().rstrip("/")
    token = os.getenv("AXIS_API_TOKEN", "").strip()
    return (base_url or None, token or None)


def _axis_prepare(path: str) -> tuple[str | None, str | None, str | None]:
    base_url, token = _axis_config()
    if not base_url or not token:
        return None, None, "Axis not configured. Set AXIS_API_TOKEN."
    if not path.startswith("/") or "?" in path or "#" in path or "\\" in path:
        return None, None, "Axis operation path is invalid."
    try:
        base_url = validate_product_base_url(base_url, product="Axis")
    except ValueError as exc:
        return None, None, str(exc)
    return f"{base_url}{path}", token, None


def _axis_path_segment(value: Any, name: str) -> str:
    text = str(value)
    if not text or text in {".", ".."} or "/" in text or "\\" in text:
        raise ValueError(f"invalid path parameter value for {name!r}")
    return quote(text, safe="")


def _axis_bounded_payload(resp: Any) -> Any:
    try:
        payload = resp.json()
    except (TypeError, ValueError):
        return bounded_response_payload(resp, max_bytes=_AXIS_MAX_RESPONSE_BYTES)
    encoded = json.dumps(payload, ensure_ascii=False, default=str).encode()
    if len(encoded) > _AXIS_MAX_RESPONSE_BYTES:
        preview = encoded[:_AXIS_MAX_RESPONSE_BYTES]
        return {
            "content_type": "application/json",
            "size_bytes": len(encoded),
            "truncated": True,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "text": preview.decode("utf-8", errors="replace"),
        }
    return bound_collection_response(payload, limit=clamp_limit(None), offset=0)


async def _axis_request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: Any = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    url, token, error = _axis_prepare(path)
    if error:
        return {"error": error}
    assert url is not None and token is not None
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    kwargs: dict[str, Any] = {
        "headers": headers,
        "params": {key: value for key, value in (params or {}).items() if value is not None},
    }
    if body is not None:
        kwargs["json"] = body
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, **kwargs)
        if resp.status_code == 401:
            return {
                "error": (
                    "Axis API returned 401. The static token is missing, expired, or revoked; "
                    "regenerate it at Settings > Admin API in the Axis portal."
                ),
                "status_code": 401,
                "url": url,
            }
        if not 200 <= resp.status_code < 300:
            return {
                "error": f"Axis API returned HTTP {resp.status_code}.",
                "status_code": resp.status_code,
                "data": redact_sensitive(_axis_bounded_payload(resp)),
                "url": url,
            }
        return {
            "status_code": resp.status_code,
            "data": redact_sensitive(_axis_bounded_payload(resp)),
            "url": url,
        }
    except httpx.HTTPError as exc:
        return {"error": str(exc), "url": url}


def _axis_write_preview(
    name: str,
    method: str,
    path: str,
    body: Any,
    *,
    capability: str,
    dry_run: bool,
    confirm: bool,
) -> dict[str, Any] | None:
    if not optional_product_writes_allowed():
        return optional_product_write_blocked(
            name,
            capability=capability,
            dry_run_state="preview" if dry_run else "execution_requested",
        )
    url, _, error = _axis_prepare(path)
    if error:
        return {"error": error}
    preview = {
        "method": method,
        "path": path,
        "url": url,
        "json": redact_sensitive(body),
    }
    if dry_run:
        return {"dry_run": True, **preview, "execute_hint": _AXIS_EXECUTE_HINT}
    if not confirm:
        return {
            "error": "confirm=True is required when dry_run=False.",
            "dry_run": True,
            **preview,
        }
    return None


def _axis_signature(operation: dict[str, Any]) -> tuple[inspect.Signature, dict[str, Any]]:
    parameters: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    specs = operation.get("parameters", [])
    for spec in sorted(specs, key=lambda item: not bool(item.get("required"))):
        name = spec["name"]
        py_type = _AXIS_TYPES.get(spec.get("type", "object"), Any)
        required = bool(spec.get("required"))
        annotation = py_type if required else Optional[py_type]
        default = inspect.Parameter.empty if required else spec.get("default")
        if not required and "default" not in spec:
            default = None
        parameters.append(
            inspect.Parameter(
                name,
                kind=inspect.Parameter.KEYWORD_ONLY,
                annotation=annotation,
                default=default,
            )
        )
        annotations[name] = annotation
    if operation.get("capability") != "read":
        for name, default in (("dry_run", True), ("confirm", False)):
            parameters.append(
                inspect.Parameter(
                    name,
                    kind=inspect.Parameter.KEYWORD_ONLY,
                    annotation=bool,
                    default=default,
                )
            )
            annotations[name] = bool
    annotations["return"] = dict[str, Any]
    return inspect.Signature(parameters), annotations


async def _axis_query(operation: dict[str, Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    path = operation["path"]
    item_id = kwargs.get(operation["id_arg"])
    if item_id:
        try:
            path = f"{path}/{_axis_path_segment(item_id, operation['id_arg'])}"
        except ValueError as exc:
            return {"error": str(exc)}
        return await _axis_request("GET", path)
    page_number = max(1, int(kwargs.get("page_number", 1)))
    page_size = max(1, min(100, int(kwargs.get("page_size", 50))))
    return await _axis_request(
        "GET", path, params={"pageNumber": page_number, "pageSize": page_size}
    )


_AXIS_VERB_METHODS = {"create": "POST", "update": "PUT", "delete": "DELETE"}


async def _axis_manage(
    operation: dict[str, Any], kwargs: dict[str, Any], verb: str
) -> dict[str, Any]:
    """Execute a single create/update/delete verb against ``operation['path']``.

    The upstream ``manage_entity`` helper fuses these three verbs behind one
    ``action_type`` argument; hpe-networking-mcp generates a distinct tool per verb
    (see ``scripts/generate_axis_manifest.py:_crud_operations``), so each
    call here already knows its verb and only validates that verb's inputs.
    """
    item_id = kwargs.get(operation["id_arg"]) if operation.get("id_arg") else None
    payload = kwargs.get("payload")
    if verb in {"update", "delete"} and not item_id:
        return {"error": f"{operation['id_arg']} is required for this operation."}
    if verb in {"create", "update"} and not payload:
        return {"error": "payload is required for this operation."}
    path = operation["path"]
    if verb in {"update", "delete"}:
        try:
            path = f"{path}/{_axis_path_segment(item_id, operation['id_arg'])}"
        except ValueError as exc:
            return {"error": str(exc)}
    method = _AXIS_VERB_METHODS[verb]
    body = None if verb == "delete" else payload
    blocked = _axis_write_preview(
        operation["name"],
        method,
        path,
        body,
        capability=operation["capability"],
        dry_run=bool(kwargs.get("dry_run", True)),
        confirm=bool(kwargs.get("confirm", False)),
    )
    if blocked is not None:
        return blocked
    result = await _axis_request(method, path, body=body)
    if "error" not in result:
        result["next_step"] = operation["next_step"]
    return result


async def _axis_dispatch(operation: dict[str, Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    kind = operation["kind"]
    if kind == "query":
        return await _axis_query(operation, kwargs)
    if kind == "subquery":
        try:
            parent = _axis_path_segment(kwargs["location_id"], "location_id")
        except ValueError as exc:
            return {"error": str(exc)}
        nested = {**operation, "path": f"/Locations/{parent}/SubLocations"}
        return await _axis_query(nested, kwargs)
    if kind in {"create", "update", "delete", "subcreate", "subupdate", "subdelete"}:
        target = operation
        verb = kind
        if kind.startswith("sub"):
            try:
                parent = _axis_path_segment(kwargs["location_id"], "location_id")
            except ValueError as exc:
                return {"error": str(exc)}
            target = {**operation, "path": f"/Locations/{parent}/SubLocations"}
            verb = kind.removeprefix("sub")
        return await _axis_manage(target, kwargs, verb)
    if kind == "status":
        entity_type = str(kwargs["entity_type"]).lower()
        if entity_type not in {"connector", "tunnel"}:
            return {"error": "entity_type must be 'connector' or 'tunnel'."}
        collection = "Connectors" if entity_type == "connector" else "Tunnels"
        try:
            entity_id = _axis_path_segment(kwargs["entity_id"], "entity_id")
        except ValueError as exc:
            return {"error": str(exc)}
        return await _axis_request("GET", f"/{collection}/{entity_id}/status")
    if kind == "action":
        path = operation["path"]
        for parameter in operation.get("parameters", []):
            name = parameter["name"]
            try:
                path = path.replace(f"{{{name}}}", _axis_path_segment(kwargs[name], name))
            except ValueError as exc:
                return {"error": str(exc)}
        body = {} if operation["name"] == "axis_regenerate_connector" else None
        blocked = _axis_write_preview(
            operation["name"],
            operation["method"],
            path,
            body,
            capability=operation["capability"],
            dry_run=bool(kwargs.get("dry_run", True)),
            confirm=bool(kwargs.get("confirm", False)),
        )
        if blocked is not None:
            return blocked
        return await _axis_request(
            operation["method"], path, body=body, timeout=float(operation.get("timeout", 30))
        )
    return {"error": f"Unsupported Axis registry operation kind: {kind}"}


def _make_axis_tool(operation: dict[str, Any]) -> Any:
    async def _tool(**kwargs: Any) -> dict[str, Any]:
        return await _axis_dispatch(operation, kwargs)

    signature, annotations = _axis_signature(operation)
    _tool.__name__ = operation["name"]
    _tool.__qualname__ = operation["name"]
    _tool.__doc__ = operation["summary"]
    _tool.__signature__ = signature  # type: ignore[attr-defined]
    _tool.__annotations__ = annotations
    return _tool


def _register_axis_tools() -> list[str]:
    manifest = load_manifest("axis")
    registered: list[str] = []
    for operation in manifest.get("operations", []):
        fn = _make_axis_tool(operation)
        capability = operation["capability"]
        annotations = (
            READ_ONLY
            if capability == "read"
            else DESTRUCTIVE
            if capability == "destructive"
            else WRITE
        )
        mcp.add_tool(
            fn,
            name=operation["name"],
            description=operation["summary"],
            annotations=annotations,
        )
        globals()[operation["name"]] = fn
        registered.append(operation["name"])
    return registered


GENERATED_AXIS_TOOLS = _register_axis_tools()


if __name__ == "__main__":
    from hpe_networking_mcp.mcp_servers._cache_hygiene import stable_list_tools
    from hpe_networking_mcp.mcp_servers._middleware import (
        NullStripMiddleware,
        RateLimitMiddleware,
        SecretTokenizeMiddleware,
        install_middleware,
    )
    from hpe_networking_mcp.mcp_servers.shared import run_server

    stable_list_tools(mcp)
    install_middleware(
        mcp,
        [
            NullStripMiddleware(),
            SecretTokenizeMiddleware(),
            RateLimitMiddleware(),
        ],
    )
    run_server(mcp)
