"""Swagger 2.0 and OpenAPI 3.0/3.1 parser and intermediate representation (IR).

This module turns a raw OpenAPI document into a deterministic, flattened list
of :class:`OperationIR` records that the manifest builder and runtime consume.
It supports the subset of OpenAPI needed for the current Mist and Aruba Central
specs:

* local (``#/...``) ``$ref`` resolution for parameters, request bodies, and
  schemas, with cycle detection and explicit errors for unresolved refs;
* path / query / header / cookie parameters (inline or referenced);
* request bodies with a chosen content type;
* arrays / objects (maps) / enums / defaults;
* enough ``allOf`` / ``oneOf`` / ``anyOf`` handling to classify a schema's
  effective type without exploding the full object graph.

Determinism: :meth:`SpecParser.operations` walks paths in sorted order and
methods in a fixed canonical order so regeneration is byte-stable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Canonical method ordering used for deterministic operation walking.
HTTP_METHODS: tuple[str, ...] = ("get", "put", "post", "delete", "patch", "head", "options")

# Cap on how deep we chase $ref / allOf chains when inferring a schema type.
_MAX_RESOLVE_DEPTH = 64


class OpenApiError(Exception):
    """Base error for OpenAPI parsing problems."""


class UnresolvedRefError(OpenApiError):
    """Raised when a ``$ref`` cannot be resolved to a local component."""


def _rewrite_swagger_refs(value: Any) -> Any:
    """Rewrite Swagger 2 component refs to their OpenAPI 3 locations."""
    if isinstance(value, list):
        return [_rewrite_swagger_refs(item) for item in value]
    if not isinstance(value, dict):
        return value
    rewritten: dict[str, Any] = {}
    for key, item in value.items():
        if key == "$ref" and isinstance(item, str):
            item = item.replace("#/definitions/", "#/components/schemas/", 1)
            item = item.replace("#/parameters/", "#/components/parameters/", 1)
            item = item.replace("#/responses/", "#/components/responses/", 1)
        rewritten[key] = _rewrite_swagger_refs(item)
    return rewritten


def _swagger_parameter_schema(parameter: dict[str, Any]) -> dict[str, Any]:
    schema = parameter.get("schema")
    if isinstance(schema, dict):
        return _rewrite_swagger_refs(schema)
    return {
        key: _rewrite_swagger_refs(parameter[key])
        for key in ("type", "format", "items", "enum", "default")
        if key in parameter
    }


def _normalize_swagger_parameters(
    parameters: list[Any], consumes: list[str]
) -> tuple[list[Any], dict[str, Any] | None]:
    normalized: list[Any] = []
    body_parameter: dict[str, Any] | None = None
    form_properties: dict[str, Any] = {}
    form_required: list[str] = []

    for raw in parameters:
        parameter = _rewrite_swagger_refs(raw)
        if not isinstance(parameter, dict) or "$ref" in parameter:
            normalized.append(parameter)
            continue
        location = parameter.get("in")
        if location == "body":
            if body_parameter is not None:
                raise OpenApiError("Swagger 2 operation declares multiple body parameters")
            body_parameter = {
                "required": bool(parameter.get("required", False)),
                "description": str(parameter.get("description", "")),
                "content": {
                    (consumes[0] if consumes else "application/json"): {
                        "schema": _swagger_parameter_schema(parameter)
                    }
                },
            }
            continue
        if location == "formData":
            name = parameter.get("name")
            if not isinstance(name, str) or not name:
                raise OpenApiError(f"invalid Swagger 2 formData parameter: {raw!r}")
            form_properties[name] = _swagger_parameter_schema(parameter)
            if parameter.get("required"):
                form_required.append(name)
            continue
        converted = dict(parameter)
        converted["schema"] = _swagger_parameter_schema(parameter)
        for key in ("type", "format", "items", "enum", "default"):
            converted.pop(key, None)
        normalized.append(converted)

    if form_properties:
        if body_parameter is not None:
            raise OpenApiError("Swagger 2 operation mixes body and formData parameters")
        schema: dict[str, Any] = {
            "type": "object",
            "properties": dict(sorted(form_properties.items())),
        }
        if form_required:
            schema["required"] = sorted(form_required)
        content_type = next(
            (
                item
                for item in consumes
                if item in {"multipart/form-data", "application/x-www-form-urlencoded"}
            ),
            "multipart/form-data",
        )
        body_parameter = {
            "required": bool(form_required),
            "content": {content_type: {"schema": schema}},
        }
    return normalized, body_parameter


def normalize_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Normalize a supported Swagger/OpenAPI document for the shared IR."""
    if not isinstance(spec, dict):
        raise OpenApiError("spec must be a JSON object")
    if "openapi" in spec:
        version = str(spec.get("openapi") or "")
        if not version.startswith(("3.0", "3.1")):
            raise OpenApiError(
                f"unsupported OpenAPI version {version!r}; expected 3.0.x or 3.1.x"
            )
        return spec

    version = str(spec.get("swagger") or "")
    if version != "2.0":
        raise OpenApiError(
            f"unsupported API document version {version!r}; expected Swagger 2.0 "
            "or OpenAPI 3.0.x/3.1.x"
        )

    definitions = _rewrite_swagger_refs(spec.get("definitions") or {})
    global_consumes = [
        str(item) for item in spec.get("consumes", []) if isinstance(item, str)
    ]
    paths: dict[str, Any] = {}
    raw_paths = spec.get("paths")
    if not isinstance(raw_paths, dict):
        raise OpenApiError("spec has no 'paths' object")
    for path, raw_item in raw_paths.items():
        if not isinstance(raw_item, dict):
            continue
        item = _rewrite_swagger_refs(raw_item)
        converted_item: dict[str, Any] = {}
        shared_parameters, shared_body = _normalize_swagger_parameters(
            list(item.get("parameters", [])), global_consumes
        )
        if shared_body is not None:
            raise OpenApiError("Swagger 2 path-level body/formData parameters are unsupported")
        if shared_parameters:
            converted_item["parameters"] = shared_parameters
        for method, raw_operation in item.items():
            if method not in HTTP_METHODS or not isinstance(raw_operation, dict):
                continue
            operation = dict(raw_operation)
            consumes = [
                str(value)
                for value in operation.get("consumes", global_consumes)
                if isinstance(value, str)
            ]
            parameters, request_body = _normalize_swagger_parameters(
                list(operation.get("parameters", [])), consumes
            )
            operation["parameters"] = parameters
            operation.pop("consumes", None)
            operation.pop("produces", None)
            if request_body is not None:
                operation["requestBody"] = request_body
            converted_item[method] = operation
        paths[str(path)] = converted_item

    security_schemes: dict[str, Any] = {}
    for name, raw_scheme in (spec.get("securityDefinitions") or {}).items():
        if not isinstance(raw_scheme, dict):
            continue
        scheme = _rewrite_swagger_refs(raw_scheme)
        if scheme.get("type") == "basic":
            scheme = {"type": "http", "scheme": "basic"}
        security_schemes[str(name)] = scheme

    components: dict[str, Any] = {}
    if definitions:
        components["schemas"] = definitions
    if spec.get("parameters"):
        if not isinstance(spec["parameters"], dict):
            raise OpenApiError("Swagger 2 reusable parameters must be an object")
        converted_parameters: dict[str, Any] = {}
        for name, raw_parameter in spec["parameters"].items():
            if not isinstance(raw_parameter, dict):
                continue
            parameters, request_body = _normalize_swagger_parameters(
                [raw_parameter], global_consumes
            )
            if request_body is not None:
                raise OpenApiError(
                    "reusable Swagger 2 body/formData parameters are unsupported"
                )
            if parameters:
                converted_parameters[str(name)] = parameters[0]
        components["parameters"] = converted_parameters
    if spec.get("responses"):
        components["responses"] = _rewrite_swagger_refs(spec["responses"])
    if security_schemes:
        components["securitySchemes"] = security_schemes

    normalized: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": _rewrite_swagger_refs(spec.get("info") or {}),
        "paths": paths,
    }
    if components:
        normalized["components"] = components
    if isinstance(spec.get("security"), list):
        normalized["security"] = _rewrite_swagger_refs(spec["security"])
    return normalized


@dataclass
class ParamIR:
    """One request parameter (path/query/header/cookie)."""

    name: str
    location: str  # "path" | "query" | "header" | "cookie"
    required: bool
    schema_type: str  # string|integer|number|boolean|array|object|any
    description: str = ""
    enum: list[Any] | None = None
    default: Any = None
    item_type: str | None = None  # element type when schema_type == "array"
    schema_format: str | None = None
    style: str | None = None
    explode: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "in": self.location,
            "required": self.required,
            "type": self.schema_type,
        }
        if self.description:
            out["description"] = self.description
        if self.enum is not None:
            out["enum"] = self.enum
        if self.default is not None:
            out["default"] = self.default
        if self.item_type is not None:
            out["item_type"] = self.item_type
        if self.schema_format:
            out["format"] = self.schema_format
        if self.style:
            out["style"] = self.style
        if self.explode is not None:
            out["explode"] = self.explode
        return out


@dataclass
class RequestBodyIR:
    """A request body with a single chosen content type."""

    required: bool
    content_type: str
    schema_type: str  # object|array|string|number|integer|boolean|any
    description: str = ""
    item_type: str | None = None
    properties: list[str] = field(default_factory=list)
    required_properties: list[str] = field(default_factory=list)
    property_formats: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "required": self.required,
            "content_type": self.content_type,
            "schema_type": self.schema_type,
        }
        if self.description:
            out["description"] = self.description
        if self.item_type is not None:
            out["item_type"] = self.item_type
        if self.properties:
            out["properties"] = self.properties
        if self.required_properties:
            out["required_properties"] = self.required_properties
        if self.property_formats:
            out["property_formats"] = self.property_formats
        return out


@dataclass
class OperationIR:
    """A single flattened API operation."""

    method: str  # upper-case HTTP verb
    path: str
    operation_id: str | None
    summary: str
    description: str
    parameters: list[ParamIR]
    request_body: RequestBodyIR | None
    tags: list[str] = field(default_factory=list)
    deprecated: bool = False
    security: list[dict[str, list[str]]] = field(default_factory=list)
    response_codes: list[str] = field(default_factory=list)
    sunset: str | None = None

    @property
    def key(self) -> str:
        """Stable operation key: ``"METHOD /path"``."""
        return f"{self.method} {self.path}"


_TYPE_ALIASES = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "array": "array",
    "object": "object",
}


class SpecParser:
    """Resolve refs and flatten operations for one OpenAPI document."""

    def __init__(self, spec: dict[str, Any]):
        original_version = str(spec.get("openapi") or spec.get("swagger") or "")
        self.spec = normalize_spec(spec)
        self.version = original_version

    # -- ref resolution ------------------------------------------------
    def resolve_ref(self, ref: str) -> Any:
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise UnresolvedRefError(f"only local '#/...' refs are supported, got {ref!r}")
        node: Any = self.spec
        for raw in ref[2:].split("/"):
            token = raw.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or token not in node:
                raise UnresolvedRefError(f"unresolved ref: {ref}")
            node = node[token]
        return node

    def _deref(self, node: Any, _depth: int = 0) -> Any:
        """Follow a top-level ``$ref`` (non-recursively into children)."""
        seen: set[str] = set()
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if ref in seen:
                raise UnresolvedRefError(f"cyclic ref: {ref}")
            seen.add(ref)
            if len(seen) > _MAX_RESOLVE_DEPTH:
                raise UnresolvedRefError(f"ref chain too deep starting at {ref}")
            node = self.resolve_ref(ref)
        return node

    # -- schema type inference ----------------------------------------
    def schema_type(
        self, schema: Any, _depth: int = 0
    ) -> tuple[str, str | None, list[Any] | None, Any, list[str]]:
        """Return ``(type, item_type, enum, default, property_names)`` for a schema.

        Handles ``$ref``, ``allOf`` (merged), and ``oneOf``/``anyOf`` (first
        member that yields a concrete type). Unknown shapes map to ``"any"``.
        """
        if _depth > _MAX_RESOLVE_DEPTH:
            return "any", None, None, None, []
        schema = self._deref(schema)
        if not isinstance(schema, dict):
            return "any", None, None, None, []

        default = schema.get("default")
        enum = schema.get("enum")

        # Composition keywords.
        if "allOf" in schema and isinstance(schema["allOf"], list):
            merged_type = "object"
            props: list[str] = []
            for sub in schema["allOf"]:
                st, it, en, de, pr = self.schema_type(sub, _depth + 1)
                if st != "object" and st != "any":
                    merged_type = st
                props.extend(pr)
            return merged_type, None, enum, default, sorted(set(props))
        for comp in ("oneOf", "anyOf"):
            if comp in schema and isinstance(schema[comp], list):
                for sub in schema[comp]:
                    st, it, en, de, pr = self.schema_type(sub, _depth + 1)
                    if st != "any":
                        return st, it, enum or en, default if default is not None else de, pr
                return "any", None, enum, default, []

        raw_type = schema.get("type")
        # OpenAPI 3.1 allows a list of types; pick the first non-null.
        if isinstance(raw_type, list):
            raw_type = next((t for t in raw_type if t != "null"), None)

        if raw_type == "array":
            item_type, _, _, _, _ = self.schema_type(schema.get("items", {}), _depth + 1)
            return "array", item_type, enum, default, []
        if raw_type == "object" or "properties" in schema or "additionalProperties" in schema:
            props = sorted((schema.get("properties") or {}).keys())
            return "object", None, enum, default, props
        if raw_type in _TYPE_ALIASES:
            return _TYPE_ALIASES[raw_type], None, enum, default, []
        if enum:
            # Infer from enum member types.
            if all(isinstance(v, bool) for v in enum):
                return "boolean", None, enum, default, []
            if all(isinstance(v, int) for v in enum):
                return "integer", None, enum, default, []
            return "string", None, enum, default, []
        return "any", None, enum, default, []

    # -- parameters ----------------------------------------------------
    def _parse_param(self, raw: Any) -> ParamIR:
        param = self._deref(raw)
        if not isinstance(param, dict) or "name" not in param or "in" not in param:
            raise OpenApiError(f"invalid parameter object: {raw!r}")
        schema = self._deref(param.get("schema", {}))
        st, item_type, enum, default, _ = self.schema_type(schema)
        required = bool(param.get("required", param.get("in") == "path"))
        return ParamIR(
            name=str(param["name"]),
            location=str(param["in"]),
            required=required,
            schema_type=st,
            description=str(param.get("description", "")).strip(),
            enum=enum,
            default=default,
            item_type=item_type,
            schema_format=(
                str(schema.get("format"))
                if isinstance(schema, dict) and schema.get("format")
                else None
            ),
            style=str(param["style"]) if param.get("style") else None,
            explode=param.get("explode") if isinstance(param.get("explode"), bool) else None,
        )

    def _parse_request_body(self, raw: Any) -> RequestBodyIR | None:
        body = self._deref(raw)
        if not isinstance(body, dict):
            return None
        content = body.get("content")
        if not isinstance(content, dict) or not content:
            return None
        # Prefer application/json, else the first declared content type.
        content_type = "application/json"
        if content_type not in content:
            content_type = sorted(content.keys())[0]
        media = content.get(content_type) or {}
        schema = self._deref(media.get("schema", {}))
        st, item_type, _, _, props = self.schema_type(schema)
        required_properties: list[str] = []
        property_formats: dict[str, str] = {}
        if isinstance(schema, dict):
            required_properties = sorted(
                str(name) for name in schema.get("required", []) if isinstance(name, str)
            )
            for name, raw_property in (schema.get("properties") or {}).items():
                prop = self._deref(raw_property)
                if isinstance(prop, dict) and prop.get("format"):
                    property_formats[str(name)] = str(prop["format"])
        return RequestBodyIR(
            required=bool(body.get("required", False)),
            content_type=content_type,
            schema_type=st,
            description=str(body.get("description", "")).strip(),
            item_type=item_type,
            properties=props,
            required_properties=required_properties,
            property_formats=dict(sorted(property_formats.items())),
        )

    # -- operation walk ------------------------------------------------
    def operations(self) -> list[OperationIR]:
        paths = self.spec.get("paths")
        if not isinstance(paths, dict):
            raise OpenApiError("spec has no 'paths' object")
        ops: list[OperationIR] = []
        for path in sorted(paths.keys()):
            item = paths[path]
            if not isinstance(item, dict):
                continue
            item = self._deref(item)
            shared_params = item.get("parameters", []) if isinstance(item, dict) else []
            for method in HTTP_METHODS:
                op = item.get(method)
                if not isinstance(op, dict):
                    continue
                raw_params = list(shared_params) + list(op.get("parameters", []))
                params = [self._parse_param(p) for p in raw_params]
                request_body = None
                if "requestBody" in op:
                    request_body = self._parse_request_body(op["requestBody"])
                ops.append(
                    OperationIR(
                        method=method.upper(),
                        path=path,
                        operation_id=op.get("operationId"),
                        summary=str(op.get("summary", "")).strip(),
                        description=str(op.get("description", "")).strip(),
                        parameters=params,
                        request_body=request_body,
                        tags=[str(t) for t in op.get("tags", []) if isinstance(t, str)],
                        deprecated=bool(op.get("deprecated", False)),
                        security=[
                            {
                                str(name): [str(scope) for scope in scopes]
                                for name, scopes in requirement.items()
                                if isinstance(scopes, list)
                            }
                            for requirement in op.get("security", self.spec.get("security", []))
                            if isinstance(requirement, dict)
                        ],
                        response_codes=sorted(
                            str(code) for code in (op.get("responses") or {}).keys()
                        ),
                        sunset=(
                            str(op.get("x-sunset") or op.get("x-sunset-date"))
                            if op.get("x-sunset") or op.get("x-sunset-date")
                            else None
                        ),
                    )
                )
        return ops
