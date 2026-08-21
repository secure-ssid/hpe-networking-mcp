"""MCP server — ``hpe-networking-mcp`` unified tool router (lazy loading via semantic tool RAG).

Supports three exposure modes:
  minimal  — find_tool + invoke_read_tool + invoke_tool only
  default  — minimal plus convenience wrappers, including the read-only
             automation planners plan_tool_workflow and
             plan_reconciliation_schedule, and the read-only declarative
             compliance-policy evaluator evaluate_compliance_policy
  direct   — default plus every enabled backend tool registered directly

Backend servers are imported in-process — no subprocess overhead.

Optional product backends can be enabled with:
  HPE_MCP_PRODUCTS=clearpass,mist,apstra,aos8,edgeconnect,uxi,axis,design

Toolsets can narrow loaded backends:
  HPE_MCP_TOOLSETS=central,rag

The credential-free ``interop-core`` backend (Central <-> Mist concept
translation + bounded trend normalization) is always loaded, on every
profile, and also has its own ``HPE_MCP_TOOLSETS=interop`` value.

Point MCP clients at THIS server instead of individual backend servers to keep
context cost low and let small local models pick tools reliably.

v0.7 router automation: invoke_tool/invoke_read_tool dispatch enforces a
configurable, deterministic response item/byte budget (see
_bound_router_response / HPE_MCP_ROUTER_RESPONSE_MAX_ITEMS /
HPE_MCP_ROUTER_RESPONSE_MAX_BYTES) and plan_tool_workflow /
plan_reconciliation_schedule provide read-only, catalog-backed dependency
ordering and recurring-reconciliation planning
(src/hpe_networking_mcp/pipeline/router_automation.py) without ever executing
a tool. evaluate_compliance_policy
(src/hpe_networking_mcp/pipeline/compliance.py)
evaluates already-retrieved observations against a bounded, declarative
policy (fixed operator dispatch, no eval/exec) and never dispatches a tool
either.

invoke_read_tool also accepts an optional opaque `cursor` to resume a
previously truncated response (see "Continuation cursors" below). Cursors
are process-local (an HMAC key generated fresh at import time; a server
restart invalidates every outstanding cursor with an explicit error),
integrity-protected, bounded in length/TTL, and bound to the exact tool
name + canonical arguments that issued them. Only capability `read` tools
ever emit or accept a cursor; the generic destructive invoke_tool never
gains continuation support.
"""

import base64
import hashlib
import hmac
import importlib
import json
import logging
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel, ConfigDict, Field

from hpe_networking_mcp.mcp_servers.prompts import register_router_prompts
from hpe_networking_mcp.mcp_servers.shared import (
    ACCESS_PROFILE_ENV_VAR,
    DESTRUCTIVE,
    DIAGNOSTIC,
    MAX_LIST_LIMIT,
    PLATFORM_WRITE_GATE_NAMES,
    READ_ONLY,
    access_profile,
    bound_collection_response,
    build_write_execution_contract,
    global_readonly_enabled,
    global_write_blocked,
    optional_product_access_mode,
    platform_write_blocked,
    platform_write_gate_state,
    platform_writes_allowed,
    reject_unknown_env_choices,
    resolve_rag_backend,
    validate_access_profile_environment,
)
from hpe_networking_mcp.pipeline import artifact_contracts as _artifact_contracts
from hpe_networking_mcp.optional_deps import MissingOptionalDependency
from hpe_networking_mcp.pipeline import compliance as _compliance
from hpe_networking_mcp.pipeline import router_automation as _router_automation
from hpe_networking_mcp.pipeline.clients import error_help as _error_help

_BACKEND = resolve_rag_backend()
_ROUTER_MODE = os.getenv("HPE_MCP_ROUTER_MODE", "default").strip().lower()

if _BACKEND == "redis":
    from hpe_networking_mcp.pipeline.clients.ollama_client import OllamaClient

    try:
        from hpe_networking_mcp.pipeline.clients.redis_client import (
            TOOLS_INDEX,
        )
        from hpe_networking_mcp.pipeline.clients.redis_client import (
            get_client as _get_redis,
        )
        from hpe_networking_mcp.pipeline.clients.redis_client import (
            search_tools as _search_tools,
        )

        _redis_tools = _get_redis()
        _redis_tools.ping()
    except Exception:
        _redis_tools = None
    _ollama = OllamaClient()
else:
    from hpe_networking_mcp.pipeline.clients import lance_client as _lance
    from hpe_networking_mcp.pipeline.clients.embed_client import EmbedClient

    _embedder = EmbedClient()  # lazy — the ONNX model loads on first query

logger = logging.getLogger(__name__)

mcp = MCPServer("hpe-networking-mcp")

# Backend MCP modules (loaded lazily on first invoke_tool).
_BACKENDS_BASE = {
    "central-config": "hpe_networking_mcp.mcp_servers.config",
    "central-monitoring": "hpe_networking_mcp.mcp_servers.monitoring",
    "central-nac": "hpe_networking_mcp.mcp_servers.nac",
    "central-ops": "hpe_networking_mcp.mcp_servers.ops",
    "central-streaming": "hpe_networking_mcp.mcp_servers.central_streaming",
    "site-health": "hpe_networking_mcp.mcp_servers.site_health",
    "glp-core": "hpe_networking_mcp.mcp_servers.glp",
    "rag-core": "hpe_networking_mcp.mcp_servers.rag",
}
_GENERATED_BACKENDS = {
    "central-generated": "hpe_networking_mcp.mcp_servers.central_generated",
}
#: Credential-free, read-only-local backends loaded on *every* profile,
#: whatever HPE_MCP_TOOLSETS/HPE_MCP_PRODUCTS select. They make no API call
#: and need no configuration, so hiding them behind a toolset would only make
#: them undiscoverable from the documented minimal `central,glp,rag` profile.
#: Each also keeps an explicit toolset entry below for callers that want to
#: load *only* it.
_ALWAYS_ON_BACKENDS = {
    "interop-core": "hpe_networking_mcp.mcp_servers.interop",
}
_OPTIONAL_BACKENDS = {
    "clearpass": ("clearpass-core", "hpe_networking_mcp.mcp_servers.clearpass"),
    "mist": ("mist-core", "hpe_networking_mcp.mcp_servers.mist"),
    "apstra": ("apstra-core", "hpe_networking_mcp.mcp_servers.apstra"),
    "aos8": ("aos8-core", "hpe_networking_mcp.mcp_servers.aos8"),
    "edgeconnect": ("edgeconnect-core", "hpe_networking_mcp.mcp_servers.edgeconnect"),
    "uxi": ("uxi-core", "hpe_networking_mcp.mcp_servers.uxi"),
    "axis": ("axis-core", "hpe_networking_mcp.mcp_servers.axis"),
    "design": ("design-core", "hpe_networking_mcp.mcp_servers.design"),
}
_OPTIONAL_SERVER_NAMES = {server_name for server_name, _ in _OPTIONAL_BACKENDS.values()}
_SERVER_PLATFORMS = {
    "interop-core": "interop",
    "central-config": "central",
    "central-monitoring": "central",
    "central-nac": "central",
    "central-ops": "central",
    "central-streaming": "central",
    "site-health": "cross-platform",
    "central-generated": "central",
    "glp-core": "glp",
    "rag-core": "rag",
    **{server_name: product for product, (server_name, _) in _OPTIONAL_BACKENDS.items()},
}
_TOOLSET_BACKENDS = {
    "config": {"central-config"},
    "monitoring": {"central-monitoring"},
    "nac": {"central-nac"},
    "ops": {"central-ops"},
    "glp": {"glp-core"},
    "rag": {"rag-core"},
    "central": {
        "central-config",
        "central-monitoring",
        "central-nac",
        "central-ops",
        "central-streaming",
        "site-health",
    },
    "site-health": {"site-health"},
    "central-generated": {"central-generated"},
    "interop": {"interop-core"},
    "clearpass": {"clearpass-core"},
    "mist": {"mist-core"},
    "apstra": {"apstra-core"},
    "aos8": {"aos8-core"},
    "edgeconnect": {"edgeconnect-core"},
    "uxi": {"uxi-core"},
    "axis": {"axis-core"},
    "design": {"design-core"},
}

# The full set of values HPE_MCP_TOOLSETS/HPE_MCP_PRODUCTS accept --
# used to reject an unrecognized, non-empty selection loudly at startup
# instead of the previous "silently ignored" behavior. "all" is a
# HPE_MCP_TOOLSETS-only keyword (load every known backend); it is not a
# valid HPE_MCP_PRODUCTS value.
_VALID_TOOLSETS = frozenset(_TOOLSET_BACKENDS) | {"all"}
_VALID_PRODUCTS = frozenset(_OPTIONAL_BACKENDS)

# -- Unknown-tool "platform not configured" detection ------------------------
#
# Every optional product except "design" uses its own product key as a
# "<product>_" tool-name prefix -- verified against each backend's own
# @mcp.tool()-registered function names, not assumed: mist_status/mist_get/...,
# clearpass_status/clearpass_get/..., apstra_status/apstra_login/...,
# aos8_status/aos8_login/..., edgeconnect_status/edgeconnect_doctor/...,
# uxi_status/uxi_get/..., and axis_create_*/axis_update_*/axis_delete_*/...
# (manifest-generated; see axis.py's module docstring). "design" is
# deliberately excluded: its tools (list_diagram_icons,
# drawio_network_design_diagram, export_graphviz_topology, ...) don't share a
# "design_" prefix, so a "design_..." guess has no real tool to disambiguate
# against and is left to the ordinary fuzzy-suggestion fallback instead.
#
# Derived from _OPTIONAL_BACKENDS -- never a second hand-typed product list --
# so a newly added optional product is covered automatically unless opted out
# here, and "is this product enabled" always reads the same _BACKENDS every
# other gate in this module already checks.
_PREFIXLESS_OPTIONAL_PRODUCTS = frozenset({"design"})
_OPTIONAL_PRODUCT_TOOL_PREFIXES: dict[str, str] = {
    product: f"{product}_"
    for product in _OPTIONAL_BACKENDS
    if product not in _PREFIXLESS_OPTIONAL_PRODUCTS
}
#: Cosmetic display name only -- never used for gating/env-var logic (that is
#: always the lowercase product key itself, e.g. HPE_MCP_PRODUCTS=mist).
#: tests/unit/test_tool_router_backends.py asserts this covers every key in
#: _OPTIONAL_PRODUCT_TOOL_PREFIXES, so a newly added prefixed product can't
#: silently ship an unlabeled hint.
_OPTIONAL_PRODUCT_LABELS: dict[str, str] = {
    "clearpass": "ClearPass",
    "mist": "Mist",
    "apstra": "Apstra",
    "aos8": "ArubaOS 8",
    "edgeconnect": "EdgeConnect",
    "uxi": "UXI",
    "axis": "Axis",
}


def _unconfigured_platform_hint(name: str) -> dict[str, Any] | None:
    """Classify an unresolved tool ``name`` against known optional-product prefixes.

    Returns a structured ``platform_not_configured`` payload when ``name``
    starts with a recognized optional product's tool-name prefix (``mist_``,
    ``clearpass_``, ``apstra_``, ``aos8_``, ``edgeconnect_``, ``uxi_``,
    ``axis_``) *and* that product's backend is not currently loaded (see
    ``_BACKENDS``) -- the caller is very likely reaching for a real, gated
    capability rather than a name that doesn't exist anywhere.

    Returns ``None`` when ``name`` doesn't match any known prefix, or when it
    does but the backend IS already loaded: a genuine typo against an
    already-enabled platform still wants the ordinary fuzzy-suggestion
    fallback, not this hint.
    """
    lowered = name.lower()
    for product, prefix in _OPTIONAL_PRODUCT_TOOL_PREFIXES.items():
        if not lowered.startswith(prefix):
            continue
        server_name, _module_path = _OPTIONAL_BACKENDS[product]
        if server_name in _BACKENDS:
            return None
        label = _OPTIONAL_PRODUCT_LABELS.get(product, product)
        return {
            "reason": "platform_not_configured",
            "platform": product,
            "hint": (
                f"The '{product}' backend is not currently enabled. Set "
                f"HPE_MCP_PRODUCTS={product} (or include it in "
                f"HPE_MCP_TOOLSETS) and configure {label} credentials, then "
                "restart the server."
            ),
        }
    return None


def _csv_env(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _product_access() -> str:
    return optional_product_access_mode()


def _optional_writes_allowed() -> bool:
    return _product_access() == "read-write"


def _is_read_only_tool(tool: Any) -> bool:
    return bool(getattr(getattr(tool, "annotations", None), "read_only_hint", False))


def _is_diagnostic_tool(tool: Any) -> bool:
    return getattr(tool, "annotations", None) == DIAGNOSTIC


def _tool_capability(tool: Any) -> str:
    annotations = getattr(tool, "annotations", None)
    if bool(getattr(annotations, "read_only_hint", False)):
        return "read"
    if bool(getattr(annotations, "destructive_hint", False)):
        return "destructive"
    if _is_diagnostic_tool(tool):
        return "diagnostic"
    return "write"


def _server_platform(server: str | None) -> str | None:
    """Map a backend server id to its platform key.

    Falls back to the ``<product>-core`` / ``central-<x>`` naming convention so
    a backend added without a ``_SERVER_PLATFORMS`` entry still resolves to a
    sensible platform instead of leaking its raw server id.
    """
    if not server:
        return None
    fallback = "central" if server.startswith("central-") else server.removesuffix("-core")
    return _SERVER_PLATFORMS.get(server, fallback)


def _write_is_enabled(server: str | None, capability: str) -> bool:
    if capability not in {"write", "destructive"}:
        return True
    platform = _server_platform(server)
    if platform in PLATFORM_WRITE_GATE_NAMES:
        return platform_writes_allowed(platform)
    if server in _OPTIONAL_SERVER_NAMES:
        return _optional_writes_allowed()
    return True


def _readonly_blocks(tool: Any) -> bool:
    """True when an aggregate read-only gate hides/blocks ``tool``.

    Blocks only ``write``/``destructive`` capabilities on any backend; ``read``
    and ``diagnostic`` tools are always allowed. A platform whose own gate is
    enabled is still read-only under ``safe-read-only`` or
    ``HPE_MCP_READONLY=1``.
    """
    return global_readonly_enabled() and _tool_capability(tool) in {
        "write",
        "destructive",
    }


def _schema_default(properties: dict[str, Any], name: str) -> Any:
    field = properties.get(name)
    return field.get("default") if isinstance(field, dict) else None


def _dry_run_state(
    properties: dict[str, Any],
    arguments: dict[str, Any] | None,
) -> str:
    if "dry_run" not in properties:
        return "unsupported"
    value = (
        arguments["dry_run"]
        if arguments is not None and "dry_run" in arguments
        else _schema_default(properties, "dry_run")
    )
    if arguments is None:
        if value is True:
            return "default_preview"
        if value is False:
            return "default_execution"
        return "unknown"
    if value is True:
        return "preview"
    if value is False:
        return "execution_requested"
    return "unknown"


def _contract_next_action(
    *,
    platform: str,
    capability: str,
    supports_dry_run: bool,
    dry_run_state: str,
    supports_confirm: bool,
    requires_confirmation: bool,
    arguments: dict[str, Any] | None,
    result: Any = None,
) -> str:
    gate = platform_write_gate_state(platform)
    if not gate["enabled"]:
        retry = (
            "call invoke_tool with dry_run=true to preview"
            if supports_dry_run
            else "retry invoke_tool after explicit user approval"
        )
        if access_profile() == "safe-read-only":
            return (
                f"Set {ACCESS_PROFILE_ENV_VAR}=full-read-write, or set "
                f"{ACCESS_PROFILE_ENV_VAR}=custom and {gate['env_var']}=1, "
                f"then {retry}."
            )
        return f"Set {gate['env_var']}=1, then {retry}."

    if isinstance(result, dict):
        for key in ("next_step", "execute_hint"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if "error" in result:
            confirmed = bool((arguments or {}).get("confirm", False))
            if requires_confirmation and supports_confirm and not confirmed:
                return "Retry invoke_tool with confirm=true after explicit user approval."
            return "Correct the reported error, then retry invoke_tool."
        if dry_run_state == "preview":
            confirm_arg = " and confirm=true" if supports_confirm else ""
            return (
                f"Review the preview, then call invoke_tool again with dry_run=false{confirm_arg}."
            )
        return "No further safety action is required; review the backend result."

    if supports_dry_run:
        return "Call invoke_tool with dry_run=true to preview the change."
    if requires_confirmation and supports_confirm:
        return "Call invoke_tool with confirm=true after explicit user approval."
    if capability == "destructive":
        return (
            "Call invoke_tool after explicit user approval; the backend confirmation flow will run."
        )
    return "Call invoke_tool only after explicit user intent."


def _execution_contract(
    tool: Any,
    server: str | None,
    schema: dict[str, Any],
    *,
    arguments: dict[str, Any] | None = None,
    result: Any = None,
) -> dict[str, Any] | None:
    capability = _tool_capability(tool)
    if capability not in {"write", "destructive"}:
        return None
    platform = _server_platform(server)
    if platform not in PLATFORM_WRITE_GATE_NAMES:
        return None
    properties = schema.get("properties") or {}
    supports_dry_run = "dry_run" in properties
    supports_confirm = "confirm" in properties
    requires_confirmation = capability == "destructive" or supports_confirm
    dry_run_state = _dry_run_state(properties, arguments)
    return build_write_execution_contract(
        platform,
        capability,
        supports_dry_run=supports_dry_run,
        dry_run_state=dry_run_state,
        supports_confirm=supports_confirm,
        requires_confirmation=requires_confirmation,
        idempotent=bool(getattr(getattr(tool, "annotations", None), "idempotent_hint", False)),
        next_action=_contract_next_action(
            platform=platform,
            capability=capability,
            supports_dry_run=supports_dry_run,
            dry_run_state=dry_run_state,
            supports_confirm=supports_confirm,
            requires_confirmation=requires_confirmation,
            arguments=arguments,
            result=result,
        ),
    )


def _discovery_metadata(tool: Any, server: str | None, schema: dict[str, Any]) -> dict[str, Any]:
    capability = _tool_capability(tool)
    generated = _generated_record_for(str(getattr(tool, "name", "")))
    properties = schema.get("properties") or {}
    supports_confirm = "confirm" in properties
    metadata = {
        "platform": _server_platform(server),
        "capability": capability,
        "recommended_dispatcher": ("invoke_read_tool" if capability == "read" else "invoke_tool"),
        "requires_write_enablement": capability in {"write", "destructive"},
        "currently_enabled": bool(server in _BACKENDS) and _write_is_enabled(server, capability),
        "supports_dry_run": "dry_run" in properties,
        "supports_confirm": supports_confirm,
        "requires_confirmation": capability == "destructive"
        or (supports_confirm and capability == "write"),
        "origin": "generated" if generated is not None else "curated",
        **_annotation_flags(tool),
    }
    if generated is not None:
        metadata.update(
            {
                key: value
                for key, value in generated.items()
                if value is not None
            }
        )
        metadata.setdefault("classification", "generated-only")
        metadata.setdefault("router_profile", "opt-in")
    if "limit" in properties and "offset" in properties:
        metadata["pagination"] = "limit-offset"
    elif "cursor" in properties:
        metadata["pagination"] = "cursor"
    limit_schema = properties.get("limit")
    if isinstance(limit_schema, dict) and "default" in limit_schema:
        metadata["default_limit"] = limit_schema["default"]
    if any(key in properties for key in ("region", "glp_region")):
        metadata["region_required"] = True
    contract = _execution_contract(tool, server, schema)
    if contract is not None:
        metadata["execution_contract"] = contract
    return metadata


def _matches_discovery_filters(
    item: dict[str, Any],
    *,
    platform: str | None,
    server: str | None,
    capability: str | None,
    origin: str | None,
    operation_id: str | None,
) -> bool:
    if platform and str(item.get("platform", "")).lower() != platform.strip().lower():
        return False
    if server and str(item.get("server", "")).lower() != server.strip().lower():
        return False
    if capability and item.get("capability") != capability:
        return False
    if origin and item.get("origin") != origin:
        return False
    if operation_id and str(item.get("operation_id", "")).lower() != operation_id.lower():
        return False
    return True


def _optional_write_disabled(name: str, tool: Any | None = None, server: str | None = None) -> bool:
    tool = tool or _tool_index.get(name)
    server = server or _tool_backend_names.get(name)
    return (
        server in _OPTIONAL_SERVER_NAMES
        and tool is not None
        and _tool_capability(tool) in {"write", "destructive"}
        and not _write_is_enabled(server, _tool_capability(tool))
    )


def _build_backends() -> dict[str, str]:
    """Build backend module map, including optional product backends.

    Optional products/toolsets are enabled via:
      HPE_MCP_PRODUCTS=clearpass,mist,apstra,aos8,edgeconnect,uxi,axis,design
      HPE_MCP_TOOLSETS=central,glp,rag
    An unset/empty value keeps the documented default (core backends only).
    A non-empty value naming anything outside the documented sets above
    raises ``InvalidRuntimeConfigError`` at startup instead of the value
    being silently dropped -- see ``reject_unknown_env_choices``.
    PRODUCTS are unioned onto TOOLSETS, so ``TOOLSETS=central,glp,rag`` +
    ``PRODUCTS=design`` loads design-core too. ``_ALWAYS_ON_BACKENDS``
    (interop-core) is unioned on last and is present in every profile.

    Raises:
        InvalidRuntimeConfigError: HPE_MCP_TOOLSETS or HPE_MCP_PRODUCTS
            names a value that is not a recognized toolset/product.
    """
    products = _csv_env("HPE_MCP_PRODUCTS")
    toolsets = _csv_env("HPE_MCP_TOOLSETS")
    reject_unknown_env_choices("HPE_MCP_TOOLSETS", toolsets, _VALID_TOOLSETS)
    reject_unknown_env_choices("HPE_MCP_PRODUCTS", products, _VALID_PRODUCTS)

    optional_by_server = {
        server_name: module_path for server_name, module_path in _OPTIONAL_BACKENDS.values()
    }
    all_backends = {
        **_BACKENDS_BASE,
        **_GENERATED_BACKENDS,
        **_ALWAYS_ON_BACKENDS,
        **optional_by_server,
    }

    if not toolsets:
        out = dict(_BACKENDS_BASE)
    elif "all" in toolsets:
        out = dict(all_backends)
    else:
        wanted_servers: set[str] = set()
        for toolset in toolsets:
            wanted_servers.update(_TOOLSET_BACKENDS.get(toolset, set()))
        out = {server: all_backends[server] for server in wanted_servers if server in all_backends}

    # Always available: credential-free, read-only-local backends stay loaded
    # even under an explicit narrow toolset selection.
    out.update(_ALWAYS_ON_BACKENDS)

    for product in products:
        spec = _OPTIONAL_BACKENDS.get(product)
        if spec:
            server_name, module_path = spec
            out[server_name] = module_path
    return out


_BACKENDS = _build_backends()

# Registered only now that the enabled backend set is known: prompts that name
# tools from a disabled backend (AOS 8 today) are skipped rather than telling
# the model to call tools that are not in the tool list.
register_router_prompts(mcp, enabled_backends=_BACKENDS)

_tool_index: dict[str, Any] = {}  # name -> MCPServer Tool
_tool_servers: dict[str, Any] = {}  # name -> owning MCPServer backend (for dispatch)
_tool_backend_names: dict[str, str] = {}  # name -> owning server name
_generated_tool_records: dict[str, dict[str, Any]] | None = None
# server name -> compact import/index failure string, populated by
# _load_all_backends(). Never cleared implicitly: a backend that failed to
# import stays recorded for the life of the process so the reason a tool is
# missing is always available (see backend_load_errors()).
_backend_load_errors: dict[str, str] = {}


def backend_load_errors() -> dict[str, str]:
    """Backends that failed to import/index, mapped to a compact reason.

    Empty when every enabled backend loaded. Surfaced on unknown-tool
    dispatch errors and on find_tool's error fallback so a missing tool is
    never silently indistinguishable from a tool that never existed.
    """
    return dict(_backend_load_errors)


def _generated_records() -> dict[str, dict[str, Any]]:
    """Map generated tool names to stable manifest provenance."""
    global _generated_tool_records
    if _generated_tool_records is not None:
        return _generated_tool_records
    from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import MANIFEST_DIR
    from hpe_networking_mcp.mcp_servers.openapi_gen.naming import digest

    records: dict[str, dict[str, Any]] = {}
    for path in sorted(MANIFEST_DIR.glob("*.json")):
        try:
            manifest = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for operation in manifest.get("operations") or []:
            if not isinstance(operation, dict) or not operation.get("name"):
                continue
            name = str(operation["name"])
            # ``register_generated_tools`` renames a generated tool to
            # ``<name>_g<digest>`` when a *curated* tool already owns
            # ``<name>`` (openapi_gen/runtime.py). Index that collision alias
            # too, and remember it, so provenance follows the tool that was
            # actually registered instead of being pinned to the manifest
            # name the curated tool kept.
            op_digest = digest(str(operation.get("method") or ""), str(operation.get("path") or ""))
            alias = f"{name}_g{op_digest}"
            record = {
                "operation_id": operation.get("operation_id"),
                "operation_key": operation.get("key"),
                "manifest_platform": path.stem,
                "_collision_alias": alias,
            }
            records[name] = record
            records[alias] = record
    _generated_tool_records = records
    return records


def _generated_record_for(name: str) -> dict[str, Any] | None:
    """Manifest provenance for ``name``, or None when ``name`` is curated.

    A generated operation whose preferred name collides with a curated tool
    is registered under ``<name>_g<digest>``; the curated tool keeps the
    plain manifest name. Matching on the manifest name alone therefore
    reports the curated tool as "generated" (with the generated
    operation_id) and the real generated tool as "curated". When the
    collision alias is itself registered, the plain name belongs to the
    curated tool and carries no generated provenance.
    """
    record = _generated_records().get(name)
    if record is None:
        return None
    alias = record.get("_collision_alias")
    if alias and alias != name and alias in _tool_index:
        return None
    return {key: value for key, value in record.items() if key != "_collision_alias"}


def _load_all_backends() -> None:
    """Import every enabled backend once and index its tools by name.

    Atomic: everything is staged in local dicts and only published to the
    module globals once the whole pass succeeds. A partially-populated
    ``_tool_index`` used to be worse than an empty one, because the
    ``if _tool_index: return`` fast path then made that partial state
    permanent for the life of the process -- one backend raising on import
    silently truncated the router's catalog with no way to retry.

    Resilient to a single bad backend: an import/index failure is recorded in
    ``_backend_load_errors`` (see :func:`backend_load_errors`) and the
    remaining backends still load, so one optional product with a missing
    dependency cannot take the whole router down.

    Still strict about correctness: a duplicate tool name across two backends
    is a genuine ambiguity about where a call would be routed, so it raises --
    and, because staging happens first, it raises without leaving any partial
    state behind.
    """
    validate_access_profile_environment()
    if _tool_index:
        return

    staged_index: dict[str, Any] = {}
    staged_servers: dict[str, Any] = {}
    staged_backend_names: dict[str, str] = {}
    errors: dict[str, str] = {}

    for server_name, module_path in _BACKENDS.items():
        try:
            mod = importlib.import_module(module_path)
            backend_tools = list(mod.mcp._tool_manager._tools.items())
        except Exception as exc:
            errors[server_name] = f"{type(exc).__name__}: {exc}"
            logger.warning("backend %s (%s) failed to load: %s", server_name, module_path, exc)
            continue
        for name, tool in backend_tools:
            if _optional_write_disabled(name, tool, server_name):
                continue
            previous = staged_backend_names.get(name)
            if previous is not None and previous != server_name:
                # Raised before any global is touched -- the router stays
                # completely unloaded rather than half-loaded.
                raise RuntimeError(
                    f"duplicate backend tool name {name!r}: {previous!r} and {server_name!r}"
                )
            staged_index[name] = tool
            staged_servers[name] = mod.mcp
            staged_backend_names[name] = server_name

    _backend_load_errors.clear()
    _backend_load_errors.update(errors)
    _tool_index.update(staged_index)
    _tool_servers.update(staged_servers)
    _tool_backend_names.update(staged_backend_names)


def _register_direct_backend_tools(target: MCPServer | None = None) -> list[str]:
    """Register every enabled backend tool directly on the router server.

    Two invariants this must hold, both of which the previous
    ``target.add_tool(tool.fn, ...)`` form broke:

    1. A write/destructive tool whose platform write gate is disabled is not
       registered at all. ``_load_all_backends`` only filters the *optional*
       product backends (``_optional_write_disabled``), so in direct mode a
       gated-off Central/GLP write was still published in the tool list and
       only failed at call time -- if the backend happened to check. Direct
       mode now matches router mode: a disabled write is simply not offered.
    2. The original MCPServer ``Tool`` object is published verbatim.
       ``add_tool`` re-derives the tool with ``Tool.from_function``, which
       rebuilds ``parameters``/``fn_metadata`` and drops ``title``, ``icons``,
       ``meta`` and any ``structured_output`` choice the backend made. Reusing
       the object keeps the router's published schema byte-identical to the
       backend's.
    """
    target = target or mcp
    _load_all_backends()
    tools = target._tool_manager._tools
    existing = set(tools)
    registered: list[str] = []
    for name, tool in _tool_index.items():
        if name in existing:
            # Router wrappers intentionally retain their compact forwarding
            # signatures when a backend exposes the same public tool name.
            continue
        if not _write_is_enabled(_tool_backend_names.get(name), _tool_capability(tool)):
            continue
        # Aggregate read-only gates hide write/destructive tools from the
        # direct-mode list exactly as they do from find_tool discovery.
        if _readonly_blocks(tool):
            continue
        tools[name] = tool
        existing.add(name)
        registered.append(name)
    return registered


# ── find_tool ────────────────────────────────────────────────────────────────

# Common verbs that also appear in tool names — don't let them dominate overlap.
_STOPWORDS = {
    "list",
    "get",
    "set",
    "find",
    "the",
    "a",
    "an",
    "of",
    "for",
    "to",
    "on",
    "at",
    "in",
    "and",
    "or",
    "all",
    "one",
    "new",
    "show",
    "view",
    "with",
    "from",
    "into",
    "via",
    "use",
    "using",
    "please",
    "make",
    "create",
    "build",
    "generate",
}

# Distinctive scope/identity terms. Used only to boost a name-overlapping hit
# whose schema actually accepts the same parameter — never as sole evidence.
_SCOPE_QUERY_TERMS = {"serial", "site", "workspace", "scope", "region"}


def _query_tokens(query: str) -> set[str]:
    """Tokenize a find_tool query for high-precision name overlap.

    Normalizes underscores/hyphens/slashes and emits collapsed forms for dotted
    tokens so ``draw.io`` matches tool name token ``drawio`` and ``diagrams.net``
    still contributes ``diagrams``.
    """
    import re

    lowered = query.lower()
    spaced = lowered.replace("_", " ").replace("-", " ").replace("/", " ").replace(".", " ")
    tokens: set[str] = set()
    for raw in spaced.split():
        w = "".join(ch for ch in raw if ch.isalnum())
        if len(w) >= 3 and w not in _STOPWORDS:
            tokens.add(w)
    # Collapsed dotted phrases: draw.io -> drawio, diagrams.net -> diagramsnet
    for dotted in re.findall(r"[a-z0-9]+(?:\.[a-z0-9]+)+", lowered):
        collapsed = dotted.replace(".", "")
        if len(collapsed) >= 3 and collapsed not in _STOPWORDS:
            tokens.add(collapsed)
        # Also keep the head token when it is distinctive (diagrams from diagrams.net)
        head = dotted.split(".", 1)[0]
        if len(head) >= 3 and head not in _STOPWORDS:
            tokens.add(head)
    # Light singular/plural bridges used heavily in design discovery.
    if "diagrams" in tokens:
        tokens.add("diagram")
    if "diagram" in tokens:
        tokens.add("diagrams")
    return tokens


def _keyword_hits(query: str, limit: int, include_schema: bool = False) -> list[dict]:
    """High-precision keyword fallback: require a *non-stopword* tool-name-token match.

    Guards against the model asking generic 'list APs' and getting every
    list_* tool ranked by coincidence. Only fires when the query mentions
    something specific like 'vlan', 'ssid', 'mac', 'firmware', 'drawio',
    'diagram', or 'graphviz'. First-line description tokens can reinforce a
    name hit (never sole evidence) so phrases like "network design" still
    surface diagram exporters whose names omit "design".
    """
    _load_all_backends()
    q_tokens = _query_tokens(query)
    if not q_tokens:
        return []
    generated_records = _generated_records()
    q_low = query.lower()
    scored: list[tuple[float, Any]] = []
    for name, tool in _tool_index.items():
        if _optional_write_disabled(name, tool) or _readonly_blocks(tool):
            continue
        name_tokens = set(name.lower().split("_")) - _STOPWORDS
        name_overlap = q_tokens & name_tokens
        if not name_overlap:
            continue
        # Rank by how many distinctive query tokens the name∪first-line cover,
        # then by name precision so short exact name hits (create_vlan, get_topology)
        # still win single-token queries. Multi-word design intents like
        # "draw topology diagram" prefer exporters whose docstring carries the
        # extra terms even when the function name is shorter than the query.
        precision = len(name_overlap) / max(len(name_tokens), 1)
        desc = (getattr(tool, "description", None) or "").strip()
        desc_tokens: set[str] = set()
        if desc:
            desc_tokens = _query_tokens(desc.splitlines()[0])
        combined_overlap = q_tokens & (name_tokens | desc_tokens)
        score = float(len(combined_overlap)) + precision
        if len(name_overlap) >= 2:
            score += 0.15
        generated = generated_records.get(name)
        if generated:
            op_id = str(generated.get("operation_id") or "").lower()
            op_key = str(generated.get("operation_key") or "").lower()
            if op_id and op_id == q_low:
                score += 8.0
            elif op_id and op_id in q_low:
                score += 4.0
            if op_key and op_key in q_low:
                score += 8.0
            elif op_key:
                path = op_key.split(" ", 1)[-1]
                if path and path in q_low:
                    score += 3.0
        schema = tool.parameters if isinstance(tool.parameters, dict) else {}
        param_names = {str(param).lower() for param in (schema.get("properties") or {})}
        score += 0.35 * len(q_tokens & _SCOPE_QUERY_TERMS & param_names)
        scored.append((score, tool))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, t in scored[:limit]:
        schema = t.parameters if isinstance(t.parameters, dict) else {}
        item = {
            "name": t.name,
            "server": (server := _tool_backend_names.get(t.name)),
            "description": (t.description or "").strip(),
            "params": list((schema.get("properties") or {}).keys()),
            "score": round(score, 4),
            "match": "keyword",
            **_discovery_metadata(t, server, schema),
        }
        if include_schema:
            item["schema"] = schema
        out.append(item)
    return out


def _annotation_flags(tool: Any) -> dict[str, bool]:
    annotations = getattr(tool, "annotations", None)
    return {
        "read_only": bool(getattr(annotations, "read_only_hint", False)),
        "destructive": bool(getattr(annotations, "destructive_hint", False)),
        "idempotent": bool(getattr(annotations, "idempotent_hint", False)),
    }


def _exact_discovery_hit(query: str, include_schema: bool = False) -> dict[str, Any] | None:
    """Bridge exact METHOD /path or operationId queries to generated tools.

    Returns a compact discovery record even when the generated backend is not
    in the current router profile (classification=generated-only, currently
    enabled=False). Live tools are hydrated from the loaded catalog.
    """
    try:
        from hpe_networking_mcp.pipeline.clients.capability_coverage import (
            lookup_exact_query,
        )

        record = lookup_exact_query(query)
    except Exception:
        return None
    if not record or not record.get("generated_tool"):
        return None
    name = str(record["generated_tool"])
    _load_all_backends()
    tool = _tool_index.get(name)
    server = _tool_backend_names.get(name)
    file_path = None
    if record.get("source_file") and record.get("key"):
        file_path = f"openapi_specs/{record['source_file']}#{record['key']}"
    if tool is not None:
        schema = tool.parameters if isinstance(tool.parameters, dict) else {}
        item = {
            "name": name,
            "server": server,
            "description": (getattr(tool, "description", None) or record.get("summary") or "").strip(),
            "params": list((schema.get("properties") or {}).keys()),
            "score": 100.0,
            "match": "exact",
            **_discovery_metadata(tool, server, schema),
        }
        if include_schema:
            item["schema"] = schema
    else:
        capability = record.get("capability") or "read"
        item = {
            "name": name,
            "server": None,
            "description": record.get("summary") or "",
            "params": [],
            "score": 100.0,
            "match": "exact",
            "platform": record.get("platform"),
            "capability": capability,
            "recommended_dispatcher": (
                "invoke_read_tool" if capability == "read" else "invoke_tool"
            ),
            "requires_write_enablement": capability in {"write", "destructive"},
            "currently_enabled": False,
            "supports_dry_run": False,
            "supports_confirm": False,
            "requires_confirmation": capability == "destructive",
            "origin": "generated",
            "read_only": capability == "read",
            "destructive": capability == "destructive",
            "idempotent": False,
            "operation_id": record.get("operation_id"),
            "operation_key": record.get("key"),
            "hint": (
                "Generated-only (opt-in). Not in the current router profile; "
                "use lookup_api or enable the generated backend."
            ),
        }
    item.setdefault("classification", record.get("classification"))
    item.setdefault("router_profile", record.get("router_profile"))
    item.setdefault("family", record.get("family"))
    if file_path:
        item.setdefault("file_path", file_path)
    return item


@mcp.tool(annotations=READ_ONLY)
def find_tool(
    query: str,
    top_k: int = 5,
    include_schema: bool = False,
    platform: str | None = None,
    server: str | None = None,
    capability: Literal["read", "diagnostic", "write", "destructive"] | None = None,
    origin: Literal["curated", "generated"] | None = None,
    operation_id: str | None = None,
) -> list[dict[str, Any]]:
    """Find tools by query. Combines semantic search + tool-name keyword match.

    Call this first when you need an action. The returned `name` is what you
    pass to invoke_read_tool for read-only tools or invoke_tool for writes.
    Results are deduplicated; exact METHOD /path or operationId matches are
    annotated match='exact' (including generated-only tools disabled by the
    current profile), semantic matches match='semantic', name-overlap matches
    match='keyword', and safety flags mirror backend ToolAnnotations. Results
    are compact by default; set include_schema=True only when you need the
    full JSON schema for a selected tool. Optional platform, server,
    normalized capability, curated/generated origin, and exact OpenAPI
    operation-ID filters apply to exact, keyword, and semantic matches.

    Args:
        query: What you want to do. e.g. "create a VLAN", "disconnect a client".
        top_k: 1-10 results (default 5).
        include_schema: Include full JSON schemas in results. Defaults to False
            to keep MCP responses compact.
        platform: Filter by normalized platform, such as central, glp, mist,
            clearpass, or apstra.
        server: Filter by exact backend server name, such as central-monitoring.
        capability: Filter by read, diagnostic, write, or destructive.
        origin: Filter by curated or generated implementation.
        operation_id: Filter by an exact generated OpenAPI operationId.
    """
    top_k = max(1, min(top_k, 10))
    # Split the budget so one match type can't starve the other: the keyword
    # pass is capped at half, and whatever it leaves unused is available to
    # the semantic pass (see semantic_allowance below).
    kw_budget = max(1, top_k // 2)
    by_name: dict[str, dict[str, Any]] = {}
    semantic_error: str | None = None

    semantic_hint = "Rebuild the tool index with `uv run python scripts/ingest_tools.py`."
    exact_hit = _exact_discovery_hit(query, include_schema=include_schema)
    if exact_hit is not None and _matches_discovery_filters(
        exact_hit,
        platform=platform,
        server=server,
        capability=capability,
        origin=origin,
        operation_id=operation_id,
    ):
        by_name[exact_hit["name"]] = exact_hit

    keyword_candidates = _keyword_hits(
        query, min(max(top_k * 4, 20), 50), include_schema=include_schema
    )
    keyword_added = 0
    for h in keyword_candidates:
        if h["name"] in by_name:
            continue
        if not _matches_discovery_filters(
            h,
            platform=platform,
            server=server,
            capability=capability,
            origin=origin,
            operation_id=operation_id,
        ):
            continue
        by_name[h["name"]] = h
        keyword_added += 1
        if keyword_added >= kw_budget:
            break

    # The semantic pass may fill every slot the keyword pass left open. This
    # is computed ONCE, before the loop: the previous form recomputed
    # ``kw_budget - len(by_name)`` on every iteration, and because ``by_name``
    # grows as semantic hits are added, the allowance shrank by one for each
    # hit accepted. With no keyword hits and top_k=10 that stopped at 5
    # results instead of 10, so top_k was silently not honored.
    semantic_allowance = max(0, top_k - len(by_name))

    try:
        if _BACKEND == "redis":
            hits = []
            if _redis_tools is not None:
                vec = _ollama.embed(query)
                hits = _search_tools(
                    _redis_tools,
                    vec,
                    top_k=min(max(top_k * 4, 20), 50),
                    index_name=TOOLS_INDEX,
                )
        else:
            vec = _embedder.embed_query(query)
            hits = _lance.search_tools(
                _lance.connect(), query, vec, top_k=min(max(top_k * 4, 20), 50)
            )
        added = 0
        for h in hits:
            name = h.get("name", "")
            hit_server = h.get("server")
            if not name or name in by_name or hit_server not in _BACKENDS:
                continue
            if name not in _tool_index:
                _load_all_backends()
            tool = _tool_index.get(name)
            if tool is None:
                continue
            if _readonly_blocks(tool):
                continue
            if hit_server in _OPTIONAL_SERVER_NAMES and _optional_write_disabled(
                name, tool, hit_server
            ):
                continue
            indexed_schema = json.loads(h.get("schema_json") or "{}")
            published_schema = getattr(tool, "parameters", None)
            schema = published_schema if isinstance(published_schema, dict) else indexed_schema
            metadata = _discovery_metadata(tool, hit_server, schema)
            candidate = {
                "name": name,
                "server": hit_server,
                "description": h.get("description", ""),
                "params": list((schema.get("properties") or {}).keys()),
                "score": h.get("score", 0.0),
                "match": "semantic",
                **metadata,
            }
            if not _matches_discovery_filters(
                candidate,
                platform=platform,
                server=server,
                capability=capability,
                origin=origin,
                operation_id=operation_id,
            ):
                continue
            if added >= semantic_allowance:
                break
            if include_schema:
                candidate["schema"] = schema
            by_name[name] = candidate
            added += 1
    except Exception as exc:
        semantic_error = f"{type(exc).__name__}: {exc}"

        # The embedder and the vector store live in the `ingestion` extra, so
        # "rebuild the index" is the wrong instruction when the package is
        # simply absent -- rebuilding needs the same package. Carry the
        # install command through instead; `hint` stays present either way.
        if isinstance(exc, MissingOptionalDependency):
            semantic_hint = exc.remedy
    if not by_name and semantic_error:
        failure: dict[str, Any] = {
            "error": f"Tool semantic search unavailable: {semantic_error}",
            "hint": semantic_hint,
        }
        if _backend_load_errors:
            failure["backend_load_errors"] = backend_load_errors()
        return [failure]
    return list(by_name.values())[:top_k]


# ── Response budgets / continuation metadata ─────────────────────────────────
#
# A configurable, deterministic safety net applied to every dispatched
# backend result (invoke_tool / invoke_read_tool only -- find_tool's own
# results are already bounded by top_k). Most curated tools already bound
# their own output (limit/offset, bound_collection_response); this exists
# for the remaining/optional/generated tools that don't, and to guarantee a
# hard ceiling regardless of backend behavior. A response already within
# budget is returned byte-for-byte unchanged -- no new keys are added --
# so this is invisible to existing callers/tests until a response actually
# needs clipping.

_RESPONSE_BUDGET_ITEMS_ENV = "HPE_MCP_ROUTER_RESPONSE_MAX_ITEMS"
_RESPONSE_BUDGET_BYTES_ENV = "HPE_MCP_ROUTER_RESPONSE_MAX_BYTES"
_RESPONSE_BUDGET_DEFAULT_ITEMS = MAX_LIST_LIMIT
_RESPONSE_BUDGET_DEFAULT_BYTES = 200_000
_RESPONSE_BUDGET_MIN_BYTES = 1024
_RESPONSE_BUDGET_MIN_ITEMS = 1
_RESPONSE_BUDGET_SHRINK_STEPS = 6


def _env_positive_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def _response_budget_items() -> int:
    return min(
        _env_positive_int(
            _RESPONSE_BUDGET_ITEMS_ENV,
            _RESPONSE_BUDGET_DEFAULT_ITEMS,
        ),
        MAX_LIST_LIMIT,
    )


def _response_budget_bytes() -> int:
    return _env_positive_int(
        _RESPONSE_BUDGET_BYTES_ENV,
        _RESPONSE_BUDGET_DEFAULT_BYTES,
        minimum=_RESPONSE_BUDGET_MIN_BYTES,
    )


def _json_byte_size(value: Any) -> int | None:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return None


def _dict_primary_list_len(data: dict[str, Any]) -> tuple[str | None, int]:
    """Mirror bound_collection_response's own list-key selection so the
    "does this need bounding" pre-check never disagrees with the bounding
    it then applies."""
    candidates = [
        (key, len(value))
        for key, value in data.items()
        if key != "_pagination" and isinstance(value, list)
    ]
    if not candidates:
        return None, 0
    key, length = max(candidates, key=lambda kv: (kv[1], kv[0]))
    return key, length


def _response_bounds_marker(
    *, reason: str, item_limit: int | None, byte_limit: int, size_bytes: int | None = None
) -> dict[str, Any]:
    marker: dict[str, Any] = {
        "truncated": True,
        "reason": reason,
        "byte_limit": byte_limit,
    }
    if item_limit is not None:
        marker["item_limit"] = item_limit
    if size_bytes is not None:
        marker["size_bytes"] = size_bytes
    return marker


# ── Continuation cursors (invoke_read_tool only) ─────────────────────────────
#
# An opaque, integrity-protected token that lets invoke_read_tool resume a
# previously truncated response. It is intentionally stateless server-side:
# it carries only {version, expiry, next-offset, tool-name digest,
# arguments digest}, never raw arguments/identifiers/results/credentials.
# Resuming simply re-dispatches the SAME tool + arguments and re-slices the
# fresh result starting at the stored offset -- safe only for capability
# "read" tools, which is enforced both here and by invoke_read_tool's own
# read-only gate (defense in depth). The signing key is a random value
# generated once per process; a restart silently invalidates every
# outstanding cursor (signature verification fails), which this module
# reports as an explicit, safe error rather than ever calling the backend.

_CURSOR_VERSION = 1
_CURSOR_TTL_ENV = "HPE_MCP_ROUTER_CURSOR_TTL_SECONDS"
_CURSOR_DEFAULT_TTL_SECONDS = 900
_CURSOR_MIN_TTL_SECONDS = 30
_CURSOR_MAX_TTL_SECONDS = 3600
_CURSOR_MAX_LENGTH = 512
_CURSOR_DIGEST_HEX_CHARS = 16  # 64-bit truncated SHA-256; the HMAC (not this
# digest) is the anti-forgery boundary, so this only needs to be
# practically collision-free for distinct tool/argument combinations.
_CURSOR_MAC_BYTES = 16  # 128-bit truncated HMAC-SHA256; keeps cursors compact
# while remaining infeasible to forge without the process-local secret key.
_CURSOR_HMAC_KEY = secrets.token_bytes(32)


class CursorError(Exception):
    """Raised for any malformed/tampered/expired/mismatched cursor.

    Always caught before a backend call is made -- the message is safe to
    return directly to the caller (never includes raw arguments/secrets)."""


def _cursor_ttl_seconds() -> int:
    raw = _env_positive_int(_CURSOR_TTL_ENV, _CURSOR_DEFAULT_TTL_SECONDS, minimum=1)
    return max(_CURSOR_MIN_TTL_SECONDS, min(raw, _CURSOR_MAX_TTL_SECONDS))


def _strip_null_arguments(arguments: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (arguments or {}).items() if v is not None}


def _cursor_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_CURSOR_DIGEST_HEX_CHARS]


def _cursor_args_digest(arguments: dict[str, Any]) -> str:
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return _cursor_digest(canonical)


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _sign_cursor_payload(payload_bytes: bytes) -> bytes:
    return hmac.new(_CURSOR_HMAC_KEY, payload_bytes, hashlib.sha256).digest()[:_CURSOR_MAC_BYTES]


def _encode_continuation_cursor(*, name: str, arguments: dict[str, Any], next_offset: int) -> str:
    payload = {
        "v": _CURSOR_VERSION,
        "exp": int(time.time()) + _cursor_ttl_seconds(),
        "off": int(next_offset),
        "t": _cursor_digest(name),
        "a": _cursor_args_digest(arguments),
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = _sign_cursor_payload(payload_bytes)
    return f"{_b64u_encode(payload_bytes)}.{_b64u_encode(signature)}"


def _decode_and_verify_continuation_cursor(
    cursor: str, *, name: str, arguments: dict[str, Any]
) -> int:
    """Validate ``cursor`` against ``name``/``arguments`` and return the
    resume offset. Raises :class:`CursorError` (never touches the backend)
    for anything malformed, tampered, expired, or bound to a different
    tool/arguments -- including a signature mismatch caused by a server
    restart (a fresh random key invalidates every prior cursor)."""
    if not isinstance(cursor, str) or not cursor:
        raise CursorError("cursor is missing or malformed")
    if len(cursor) > _CURSOR_MAX_LENGTH:
        raise CursorError("cursor exceeds the maximum allowed length")
    parts = cursor.split(".")
    if len(parts) != 2:
        raise CursorError("cursor is malformed")
    payload_b64, signature_b64 = parts
    try:
        payload_bytes = _b64u_decode(payload_b64)
        signature_bytes = _b64u_decode(signature_b64)
    except Exception as exc:
        raise CursorError("cursor is malformed") from exc
    expected_signature = _sign_cursor_payload(payload_bytes)
    if not hmac.compare_digest(signature_bytes, expected_signature):
        raise CursorError("cursor signature is invalid (tampered, or the server process restarted)")
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception as exc:
        raise CursorError("cursor is malformed") from exc
    if not isinstance(payload, dict):
        raise CursorError("cursor is malformed")
    if payload.get("v") != _CURSOR_VERSION:
        raise CursorError("cursor version is unsupported")
    expiry = payload.get("exp")
    if not isinstance(expiry, int) or isinstance(expiry, bool):
        raise CursorError("cursor is malformed")
    if int(time.time()) >= expiry:
        raise CursorError("cursor has expired")
    offset = payload.get("off")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise CursorError("cursor is malformed")
    if payload.get("t") != _cursor_digest(name):
        raise CursorError("cursor does not match the requested tool")
    if payload.get("a") != _cursor_args_digest(arguments):
        raise CursorError("cursor does not match the requested arguments")
    return offset


#: Keys a backend may use to publish its own continuation token, checked at
#: the top level and inside ``_pagination``. When one is present the router
#: must not add a competing ``next_cursor``: the two have different offset
#: semantics, and the router's would shadow the backend's on the wire.
_UPSTREAM_CURSOR_KEYS = ("next_cursor", "nextCursor", "next", "cursor")


def _upstream_cursor_key(result: Any) -> str | None:
    """Return where ``result`` already carries a continuation token, or None.

    The returned string is the location (e.g. ``"next_cursor"`` or
    ``"_pagination.next_cursor"``) so the reason a router cursor was withheld
    can be reported precisely rather than as an opaque flag.
    """
    if not isinstance(result, dict):
        return None
    for key in _UPSTREAM_CURSOR_KEYS:
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return key
    pagination = result.get("_pagination")
    if isinstance(pagination, dict):
        for key in _UPSTREAM_CURSOR_KEYS:
            value = pagination.get(key)
            if isinstance(value, str) and value.strip():
                return f"_pagination.{key}"
    return None


def _bound_router_response(
    result: Any,
    *,
    max_items: int | None = None,
    max_bytes: int | None = None,
    offset: int = 0,
    enable_cursor: bool = False,
    tool_name: str | None = None,
    tool_arguments: dict[str, Any] | None = None,
) -> Any:
    """Deterministically bound one dispatched tool result to a configurable
    item-count/byte-size budget, adding a stable ``_response_bounds``
    continuation marker only when clipping actually happened, plus an
    opaque MCP-style ``next_cursor`` when the caller is eligible to resume
    (``enable_cursor`` -- only ever set True by invoke_read_tool for
    capability ``read`` tools; invoke_tool never sets it).

    Never touches a non-dict/non-list scalar, and never touches a dict
    that already looks like an error response (an ``error`` key present)
    -- error/blocked payload shapes are preserved exactly. Reuses
    ``hpe_networking_mcp.mcp_servers.shared.bound_collection_response`` for item-count
    slicing (the same ``_pagination`` shape already recognized by the
    audit/metrics truncation detectors) before falling back to a bounded
    text preview for content with nothing sliceable (mirroring
    ``hpe_networking_mcp.mcp_servers.shared.bounded_response_payload``'s raw-body fallback).
    A single item too large to fit the byte budget is reported as
    explicitly non-resumable rather than emitting a cursor that would loop
    forever on the same oversized item.
    """
    if isinstance(result, dict) and "error" in result:
        return result
    if not isinstance(result, (dict, list)):
        return result

    requested_items_budget = max_items if max_items is not None else _response_budget_items()
    items_budget = max(1, min(requested_items_budget, MAX_LIST_LIMIT))
    bytes_budget = max_bytes if max_bytes is not None else _response_budget_bytes()
    resume_offset = max(0, int(offset or 0))

    if isinstance(result, list):
        primary_key, item_count = None, len(result)
        nothing_sliceable = False
    else:
        primary_key, item_count = _dict_primary_list_len(result)
        nothing_sliceable = primary_key is None

    if nothing_sliceable:
        # A stale/mismatched resume offset against a shape with nothing
        # sliceable is ignored defensively rather than corrupting output.
        resume_offset = 0

    size = _json_byte_size(result)
    remaining_count = max(0, item_count - resume_offset)
    item_overflow = remaining_count > items_budget
    byte_overflow = resume_offset == 0 and size is not None and size > bytes_budget
    needs_paging = item_overflow or byte_overflow or resume_offset > 0
    if not needs_paging:
        return result

    if nothing_sliceable:
        # Nothing sliceable (byte overflow from scalar/nested-object bloat,
        # never from a bounded list) -- bounded text preview. Never
        # resumable: there is no list to page through.
        encoded = json.dumps(result, ensure_ascii=False, default=str)
        raw = encoded.encode("utf-8")
        marker = _response_bounds_marker(
            reason="byte_budget", item_limit=None, byte_limit=bytes_budget, size_bytes=len(raw)
        )
        marker["resumable"] = False
        marker["resumable_reason"] = "no_sliceable_collection"
        return {
            "_response_bounds": marker,
            "preview": raw[:bytes_budget].decode("utf-8", errors="replace"),
        }

    limit = items_budget
    page = bound_collection_response(result, limit=limit, offset=resume_offset)
    encoded_size = _json_byte_size(page)
    byte_shrunk = False
    for _ in range(_RESPONSE_BUDGET_SHRINK_STEPS):
        if encoded_size is not None and encoded_size <= bytes_budget:
            break
        if limit <= _RESPONSE_BUDGET_MIN_ITEMS:
            break
        limit = max(_RESPONSE_BUDGET_MIN_ITEMS, limit // 2)
        byte_shrunk = True
        page = bound_collection_response(result, limit=limit, offset=resume_offset)
        encoded_size = _json_byte_size(page)

    if encoded_size is not None and encoded_size > bytes_budget:
        # Even a single item (limit already at the floor) can't fit the
        # byte budget -- fall back to a bounded text preview instead of
        # returning an over-budget payload, and explicitly mark this
        # non-resumable so a caller never loops forever on the same item.
        encoded = json.dumps(result, ensure_ascii=False, default=str)
        raw = encoded.encode("utf-8")
        marker = _response_bounds_marker(
            reason="byte_budget", item_limit=limit, byte_limit=bytes_budget, size_bytes=len(raw)
        )
        marker["resumable"] = False
        marker["resumable_reason"] = "single_item_exceeds_byte_budget"
        return {
            "_response_bounds": marker,
            "preview": raw[:bytes_budget].decode("utf-8", errors="replace"),
        }

    slice_key = "items" if isinstance(result, list) else primary_key
    actual_count = len(page.get(slice_key, [])) if isinstance(page, dict) and slice_key else 0
    next_offset = resume_offset + actual_count
    pagination = page.get("_pagination") if isinstance(page, dict) else None
    truncated_by_items = bool(pagination and pagination.get("truncated"))
    was_clipped = truncated_by_items or byte_shrunk
    if not was_clipped:
        # A cursor-resume request that landed exactly on the final,
        # complete tail: still paginated (see _pagination), but nothing
        # was clipped relative to budget, so no _response_bounds/cursor.
        return page

    reasons = [
        reason
        for reason, present in (
            ("item_budget", item_overflow),
            ("byte_budget", byte_shrunk),
        )
        if present
    ] or ["item_budget"]
    # A backend that pages with its own opaque token owns continuation for
    # this response. Emitting a router cursor alongside it would publish two
    # different "next" positions -- and, at the top level, would overwrite the
    # backend's outright -- so the router defers and says so.
    upstream_cursor = _upstream_cursor_key(page)
    can_emit_cursor = (
        enable_cursor
        and truncated_by_items
        and tool_name is not None
        and tool_arguments is not None
        and upstream_cursor is None
    )
    marker = _response_bounds_marker(
        reason="+".join(reasons), item_limit=limit, byte_limit=bytes_budget
    )
    marker["resumable"] = bool(can_emit_cursor)
    if upstream_cursor is not None:
        marker["resumable_reason"] = "upstream_cursor_present"
        marker["upstream_cursor_key"] = upstream_cursor
    if isinstance(page, dict):
        page = {**page, "_response_bounds": marker}
        if can_emit_cursor:
            page["next_cursor"] = _encode_continuation_cursor(
                name=tool_name, arguments=tool_arguments, next_offset=next_offset
            )
            page["cursor_expires_in_seconds"] = _cursor_ttl_seconds()
    return page


# ── Dispatch-level rate gate ────────────────────────────────────────────────
#
# RateLimitMiddleware sits on the *router's* tool manager, so it charges one
# token per inbound MCP call. That is exactly wrong for the dispatching tools:
# invoke_read_tool_batch makes up to 25 backend API calls behind a single
# inbound call, and the backend tool managers are never middleware-installed
# in this process. Without a gate here, a batch would draw one token for 25
# real requests to Central -- the opposite of what the 10 req/s account-wide
# cap requires.
#
# __main__ wires this to the shared RateLimitMiddleware's public ``acquire``
# and exempts the dispatching tools from that middleware, so every backend
# call costs exactly one token, whether it arrived alone or in a batch. Unset
# (the default) it is a no-op, so importing the router in tests or embedding
# it without middleware behaves exactly as before.

_dispatch_rate_gate: Callable[[], Awaitable[None]] | None = None


def set_dispatch_rate_gate(gate: Callable[[], Awaitable[None]] | None) -> None:
    """Install (or clear) the per-backend-call rate gate awaited by dispatch."""
    global _dispatch_rate_gate
    _dispatch_rate_gate = gate


async def _await_dispatch_rate_gate() -> None:
    gate = _dispatch_rate_gate
    if gate is None:
        return
    try:
        await gate()
    except Exception:
        # A broken gate must never block dispatch outright -- rate limiting is
        # a protection, not a correctness requirement.
        logger.warning("dispatch rate gate failed; proceeding", exc_info=True)


# ── invoke_read_tool / invoke_tool ───────────────────────────────────────────


def _unknown_tool_error(name: str) -> dict[str, Any]:
    """Structured 'tool not found' response shared by invoke_tool/invoke_read_tool.

    Distinguishes a name that matches a known-but-disabled optional-product
    prefix (``reason: "platform_not_configured"``, see
    ``_unconfigured_platform_hint``) from a genuine typo/unknown name, which
    keeps the original flat message (optionally decorated with
    ``backend_load_errors`` when an *enabled* backend failed to import).
    """
    platform_hint = _unconfigured_platform_hint(name)
    if platform_hint is not None:
        # Recognized optional-product prefix, backend not loaded at all -- a
        # configuration gap, not a typo. backend_load_errors is intentionally
        # not attached here: that field means a backend WAS selected but
        # failed to import, a different failure mode than "never selected".
        return {
            "error": f"Unknown tool: {name}",
            "tool": name,
            "status": "unknown_tool",
            **platform_hint,
            "suggestions": [],
        }
    error: dict[str, Any] = {
        "error": f"Unknown tool '{name}'. Use find_tool to discover.",
        "tool": name,
        "status": "unknown_tool",
    }
    if _backend_load_errors:
        # A tool can be missing because its backend failed to import. Saying
        # so here is the difference between "typo" and "this deployment is
        # broken".
        error["backend_load_errors"] = backend_load_errors()
    return error


async def _dispatch_tool(
    ctx: Context,
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    resume_offset: int = 0,
    enable_cursor: bool = False,
    max_items: int | None = None,
    max_bytes: int | None = None,
) -> Any:
    """Dispatch one backend tool call.

    Args:
        max_items / max_bytes: Optional per-call response budget overriding the
            process defaults. Threaded through by invoke_read_tool_batch so N
            batched results share the whole-response budget instead of each
            claiming the full single-call budget.
    """
    _load_all_backends()
    backend = _tool_servers.get(name)
    if backend is None:
        return _unknown_tool_error(name)
    args = _strip_null_arguments(arguments)
    tool = _tool_index[name]
    server = _tool_backend_names.get(name)
    schema = tool.parameters if isinstance(tool.parameters, dict) else {}
    capability = _tool_capability(tool)
    # Aggregate read-only gates override every per-platform gate. Refuse a
    # write/destructive dispatch before charging the rate gate; read and
    # diagnostic tools are unaffected.
    if capability in {"write", "destructive"} and global_readonly_enabled():
        return global_write_blocked(name)
    contract = _execution_contract(tool, server, schema, arguments=args)
    platform = _server_platform(server)
    if (
        capability in {"write", "destructive"}
        and platform in PLATFORM_WRITE_GATE_NAMES
        and not _write_is_enabled(server, capability)
    ):
        assert contract is not None
        return platform_write_blocked(
            platform,
            name,
            capability=capability,
            execution_contract=contract,
        )
    # One token per *backend* call, charged after every cheap local rejection
    # above (unknown tool / blocked write) so a refused call never consumes
    # quota it never used.
    await _await_dispatch_rate_gate()
    try:
        result = await backend._tool_manager.call_tool(name, args, context=ctx)
    except Exception as e:
        result = {"error": f"{type(e).__name__}: {e}"}
    # Cursors are only ever eligible for capability "read" tools; this is a
    # redundant, defense-in-depth check -- enable_cursor is only ever passed
    # True by invoke_read_tool, which already refuses non-read-only tools.
    cursor_eligible = enable_cursor and capability == "read"
    result = _bound_router_response(
        result,
        max_items=max_items,
        max_bytes=max_bytes,
        offset=resume_offset,
        enable_cursor=cursor_eligible,
        tool_name=name,
        tool_arguments=args,
    )
    if contract is None:
        return result
    contract = _execution_contract(
        tool,
        server,
        schema,
        arguments=args,
        result=result,
    )
    if isinstance(result, dict):
        return {**result, "execution_contract": contract}
    return {"result": result, "execution_contract": contract}


async def _dispatch_read_tool(
    ctx: Context,
    name: str,
    arguments: dict[str, Any] | None = None,
    cursor: str | None = None,
    *,
    max_items: int | None = None,
    max_bytes: int | None = None,
) -> Any:
    """Shared internal read-only dispatch path -- the single place that
    resolves a tool, enforces the read-only annotation gate, decodes/
    verifies an optional continuation cursor, and calls ``_dispatch_tool``.

    Both ``invoke_read_tool`` (one call) and ``invoke_read_tool_batch`` (a
    bounded, ordered list of calls) call this directly instead of each
    other, so the annotation/permission/cursor/response-bounding logic
    lives in exactly one place. Never touches a write/destructive tool --
    every rejection below returns before ``_dispatch_tool`` (and therefore
    the backend) is ever reached.
    """
    _load_all_backends()
    tool = _tool_index.get(name)
    if tool is None:
        return _unknown_tool_error(name)
    if not bool(getattr(getattr(tool, "annotations", None), "read_only_hint", False)):
        return {
            "error": (
                f"Tool '{name}' is not read-only. Use invoke_tool only after "
                "explicit user intent for write/destructive actions."
            ),
            "tool": name,
            "status": "blocked",
        }
    resume_offset = 0
    if cursor is not None:
        canonical_args = _strip_null_arguments(arguments)
        try:
            resume_offset = _decode_and_verify_continuation_cursor(
                cursor, name=name, arguments=canonical_args
            )
        except CursorError as exc:
            return {"error": str(exc), "tool": name, "status": "invalid_cursor"}
    return await _dispatch_tool(
        ctx,
        name,
        arguments,
        resume_offset=resume_offset,
        enable_cursor=True,
        max_items=max_items,
        max_bytes=max_bytes,
    )


@mcp.tool(annotations=READ_ONLY)
async def invoke_read_tool(
    ctx: Context,
    name: str,
    arguments: dict[str, Any] | None = None,
    cursor: str | None = None,
) -> Any:
    """Call a read-only Aruba tool by name (from find_tool).

    This refuses tools that are not annotated read-only. Use invoke_tool only
    for write/destructive tools after explicit user intent.

    Args:
        cursor: Opaque ``next_cursor`` value from a previous truncated
            response, to resume it from where it left off. Only ever
            returned by this tool for capability "read" tools -- it is
            process-local (invalidated by a server restart), integrity
            protected, time-limited, and bound to this exact tool name and
            these exact arguments. A malformed/tampered/expired/mismatched
            cursor returns an error and never reaches the backend.
    """
    return await _dispatch_read_tool(ctx, name, arguments, cursor)


@mcp.tool(annotations=DESTRUCTIVE)
async def invoke_tool(
    ctx: Context,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> Any:
    """Call an Aruba tool by name (from find_tool). Arguments is a kwargs dict.

    Example: invoke_tool("create_vlan", {"vlan_id": 200, "vlan_name": "Guest"})

    Dispatches through the owning backend's MCPServer tool manager, so arguments
    get MCPServer validation/coercion and the router's request Context is forwarded
    — this is what lets the async, ctx-requiring destructive ops tools
    (reboot_device/port_bounce/poe_bounce/disconnect_client) reach their
    confirmation elicitation. (MCPServer injects `ctx` here and strips it from the
    published schema, so callers only pass name + arguments.)
    """
    return await _dispatch_tool(ctx, name, arguments)


# Router convenience wrappers (list_sites/find_device/ask_docs/...) fan a single
# inbound MCP call into exactly one backend call via invoke_tool. That backend
# call is already charged one token at the dispatch rate gate inside
# _dispatch_tool, so the wrapper must be exempt from RateLimitMiddleware -- else
# each wrapper call draws two tokens (middleware seam + gate) for one backend
# request. This set collects their names; __main__ exempts it alongside the
# dispatching primitives so a wrapped read costs exactly one token, same as a
# direct invoke_read_tool.
_WRAPPER_DISPATCHING_TOOLS: set[str] = set()


def _dispatching_wrapper_tool(annotations: Any):
    """Register a convenience wrapper that internally dispatches to a backend.

    Identical to ``mcp.tool(annotations=...)`` but also records the wrapped
    function's name in :data:`_WRAPPER_DISPATCHING_TOOLS` so its per-backend
    rate cost is charged once (at the dispatch gate), never twice.
    """
    register = mcp.tool(annotations=annotations)

    def decorator(fn):
        _WRAPPER_DISPATCHING_TOOLS.add(fn.__name__)
        return register(fn)

    return decorator


# ── invoke_read_tool_batch: bounded, sequential, read-only fan-out ──────────
#
# Nornir-inspired (see nornir.core.task.Result/MultiResult/AggregatedResult):
# an ordered, per-call result list plus rolled-up aggregate counts. Every
# call dispatches through the exact same _dispatch_read_tool helper
# invoke_read_tool itself calls -- never invoke_read_tool (or invoke_tool)
# recursively, and never a second copy of the annotation/permission/cursor/
# response-bounding logic. Sequential only (no concurrency) in this first
# version: deterministic ordering and the existing single async
# RateLimitMiddleware token bucket are both easier to reason about than an
# interleaved fan-out, and every existing rate-limit/audit/metrics
# assumption already models "one dispatch at a time".
#
# Excluded from `minimal` mode along with plan_tool_workflow/
# plan_reconciliation_schedule/evaluate_compliance_policy to keep that
# profile's tool-list token cost at exactly find_tool + invoke_read_tool +
# invoke_tool.
if _ROUTER_MODE != "minimal":
    MAX_BATCH_CALLS = 25
    MAX_BATCH_CALL_ID_CHARS = 100
    MAX_BATCH_TOOL_NAME_CHARS = 200
    MAX_BATCH_ARGUMENTS_BYTES = 20_000
    MAX_BATCH_ARGUMENTS_DEPTH = 8
    MAX_BATCH_ERROR_CHARS = 500
    _BATCH_RESPONSE_BUDGET_BYTES_ENV = "HPE_MCP_ROUTER_BATCH_RESPONSE_MAX_BYTES"
    _BATCH_RESPONSE_BUDGET_DEFAULT_BYTES = 300_000
    # Every result item's "status" is exactly one of: "ok", "error",
    # "blocked", "unknown_tool", "invalid_cursor", "invalid_call".

    def _batch_response_budget_bytes() -> int:
        return _env_positive_int(
            _BATCH_RESPONSE_BUDGET_BYTES_ENV,
            _BATCH_RESPONSE_BUDGET_DEFAULT_BYTES,
            minimum=_RESPONSE_BUDGET_MIN_BYTES,
        )

    def _batch_argument_depth(value: Any, depth: int = 0) -> int:
        """Max nesting depth of a JSON-shaped value, short-circuiting as
        soon as the running depth already exceeds the bound so one
        pathologically deep/wide payload can't cost more than a bounded
        amount of recursion before being rejected."""
        if depth > MAX_BATCH_ARGUMENTS_DEPTH:
            return depth
        if isinstance(value, dict):
            if not value:
                return depth
            return max(_batch_argument_depth(v, depth + 1) for v in value.values())
        if isinstance(value, list):
            if not value:
                return depth
            return max(_batch_argument_depth(v, depth + 1) for v in value)
        return depth

    def _truncate_batch_error(message: str, limit: int = MAX_BATCH_ERROR_CHARS) -> str:
        if len(message) > limit:
            return message[: max(0, limit - 3)] + "..."
        return message

    class BatchCall(BaseModel):
        """One entry in an invoke_read_tool_batch request.

        A typed model rather than a bare ``dict`` so the published MCP input
        schema states the shape and its bounds up front: a client (or a model
        writing the call) sees ``name``/``arguments``/``id``/``cursor`` and the
        length caps instead of an opaque object. Semantic bounds that a JSON
        schema cannot express -- serialized argument size and nesting depth --
        are still enforced per item in ``_validate_batch_call`` so an oversized
        entry is rejected on its own rather than failing the whole batch.
        """

        model_config = ConfigDict(extra="forbid")

        name: str = Field(
            min_length=1,
            max_length=MAX_BATCH_TOOL_NAME_CHARS,
            description="Exact backend tool name from find_tool.",
        )
        arguments: dict[str, Any] = Field(
            default_factory=dict,
            description="Tool arguments object (default {}).",
        )
        id: str | None = Field(
            default=None,
            max_length=MAX_BATCH_CALL_ID_CHARS,
            description=(
                "Optional caller correlation id, unique within the batch. "
                "Defaults to the call's list index as a string."
            ),
        )
        cursor: str | None = Field(
            default=None,
            description="Optional next_cursor from a previous truncated read.",
        )

    def _as_call_mapping(raw_call: Any) -> Any:
        """Normalize a validated ``BatchCall`` back to a plain mapping.

        MCPServer coerces incoming JSON into ``BatchCall`` instances, while a
        direct in-process caller (and every unit test) passes plain dicts.
        Both are accepted; validation below only ever sees a mapping.
        """
        if isinstance(raw_call, BatchCall):
            return raw_call.model_dump()
        return raw_call

    def _validate_batch_call(
        raw_call: Any, index: int
    ) -> tuple[dict[str, Any] | None, str | None, str, str | None]:
        """Validate/normalize one raw batch call entry.

        Returns ``(normalized, error, resolved_id, raw_name)``.
        ``normalized`` is ``None`` whenever ``error`` is set. Never raises,
        and never echoes a raw argument *value* in the error message --
        only shape/type/bound facts about the call itself, so a caller
        credential/secret placed in ``arguments`` can never leak through a
        validation error message.
        """
        raw_call = _as_call_mapping(raw_call)
        default_id = str(index)
        if not isinstance(raw_call, dict):
            return None, "call must be an object", default_id, None
        raw_name = raw_call.get("name")
        raw_id = raw_call.get("id", default_id)
        if raw_id is None:
            raw_id = default_id
        if not isinstance(raw_id, str) or not raw_id.strip():
            return None, "id must be a non-empty string", default_id, raw_name
        if len(raw_id) > MAX_BATCH_CALL_ID_CHARS:
            return (
                None,
                f"id exceeds the {MAX_BATCH_CALL_ID_CHARS}-character bound",
                default_id,
                raw_name,
            )
        resolved_id = raw_id
        if not isinstance(raw_name, str) or not raw_name.strip():
            return None, "name must be a non-empty string", resolved_id, raw_name
        if len(raw_name) > MAX_BATCH_TOOL_NAME_CHARS:
            return (
                None,
                f"name exceeds the {MAX_BATCH_TOOL_NAME_CHARS}-character bound",
                resolved_id,
                raw_name,
            )
        arguments = raw_call.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return None, "arguments must be an object", resolved_id, raw_name
        try:
            argument_bytes = len(json.dumps(arguments, default=str).encode("utf-8"))
        except TypeError:
            return None, "arguments must be JSON-serializable", resolved_id, raw_name
        if argument_bytes > MAX_BATCH_ARGUMENTS_BYTES:
            return (
                None,
                f"arguments exceed the {MAX_BATCH_ARGUMENTS_BYTES}-byte bound",
                resolved_id,
                raw_name,
            )
        if _batch_argument_depth(arguments) > MAX_BATCH_ARGUMENTS_DEPTH:
            return (
                None,
                f"arguments exceed the {MAX_BATCH_ARGUMENTS_DEPTH}-level nesting bound",
                resolved_id,
                raw_name,
            )
        cursor = raw_call.get("cursor")
        if cursor is not None and not isinstance(cursor, str):
            return None, "cursor must be a string", resolved_id, raw_name
        normalized = {"name": raw_name, "arguments": arguments, "cursor": cursor}
        return normalized, None, resolved_id, raw_name

    async def _dispatch_one_batch_call(
        ctx: Context,
        index: int,
        raw_call: Any,
        *,
        item_max_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Validate and dispatch exactly one batch entry through
        ``_dispatch_read_tool`` -- the same helper ``invoke_read_tool``
        uses -- and normalize the outcome into one bounded, always-present
        result-item shape (``index``/``id``/``tool``/``server``/``status``
        plus either ``result`` or ``error``, never both).

        Contains every failure mode: a validation rejection, an error-shaped
        backend response, and an exception escaping dispatch all become an
        ordinary result item, so one bad call can never abort the batch or
        raise out of the tool.
        """
        normalized, error, resolved_id, raw_name = _validate_batch_call(raw_call, index)
        if error is not None:
            return {
                "index": index,
                "id": resolved_id,
                "tool": raw_name if isinstance(raw_name, str) else None,
                "server": None,
                "status": "invalid_call",
                "error": _truncate_batch_error(error),
            }
        name = normalized["name"]
        server = _tool_backend_names.get(name)
        base = {"index": index, "id": resolved_id, "tool": name, "server": server}
        try:
            result = await _dispatch_read_tool(
                ctx,
                name,
                normalized["arguments"],
                normalized["cursor"],
                max_bytes=item_max_bytes,
            )
        except Exception as exc:
            # Defense in depth: _dispatch_tool already converts backend
            # exceptions into an error dict, so reaching here means the
            # router itself failed. Report it as this item's failure rather
            # than losing every other call's result.
            logger.warning("batch item %d (%s) raised", index, name, exc_info=True)
            return {
                **base,
                "status": "error",
                "error": _truncate_batch_error(f"{type(exc).__name__}: {exc}"),
            }
        base["server"] = _tool_backend_names.get(name, server)
        if isinstance(result, dict) and result.get("status") in (
            "blocked",
            "invalid_cursor",
            "unknown_tool",
        ):
            return {
                **base,
                "status": result["status"],
                "error": _truncate_batch_error(str(result.get("error", ""))),
            }
        if isinstance(result, dict) and "error" in result:
            return {
                **base,
                "status": "error",
                "error": _truncate_batch_error(str(result["error"])),
            }
        return {**base, "status": "ok", "result": result}

    def _first_duplicate_batch_id(calls: list[Any]) -> str | None:
        """First explicitly-supplied id that appears twice, else None.

        Only explicit ids are checked -- an omitted id defaults to the call's
        index and is unique by construction.
        """
        seen: set[str] = set()
        for raw_call in calls:
            mapping = _as_call_mapping(raw_call)
            if not isinstance(mapping, dict):
                continue
            call_id = mapping.get("id")
            if not isinstance(call_id, str) or not call_id.strip():
                continue
            if call_id in seen:
                return call_id
            seen.add(call_id)
        return None

    def _batch_items_byte_size(items: list[dict[str, Any]]) -> int:
        return len(json.dumps(items, ensure_ascii=False, default=str).encode("utf-8"))

    #: Error text is squeezed to this before the response is allowed to be
    #: over budget. Still enough to identify the failure class.
    MIN_BATCH_ERROR_CHARS = 80

    def _shrink_batch_items_for_budget(
        items: list[dict[str, Any]], *, byte_budget: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """Deterministically shrink the *total* serialized batch response to
        ``byte_budget``, in strictly increasing order of destructiveness,
        without ever dropping an item.

        Stages, each applied only if the previous one left the response over
        budget:

        1. Replace an ``"ok"`` item's already-bounded ``result`` with a
           compact ``result_truncated`` marker, one item at a time, in reverse
           call order (the last call shrinks first).
        2. Squeeze every failure's ``error`` text to ``MIN_BATCH_ERROR_CHARS``.

        Failures keep their ``index``/``id``/``tool``/``status`` at every
        stage: exceeding the byte budget must never silently erase evidence
        that a call failed. The result is *strictly* bounded -- the caller
        asserts the final size and falls back to a minimal envelope if even
        stage 2 is not enough (see ``_batch_overflow_envelope``).
        """
        working = [dict(item) for item in items]
        truncated = False
        if _batch_items_byte_size(working) <= byte_budget:
            return working, truncated

        for pos in range(len(working) - 1, -1, -1):
            if _batch_items_byte_size(working) <= byte_budget:
                break
            if working[pos].get("status") != "ok":
                continue
            working[pos] = {
                "index": working[pos]["index"],
                "id": working[pos]["id"],
                "tool": working[pos]["tool"],
                "server": working[pos].get("server"),
                "status": "ok",
                "result_truncated": True,
                "result": None,
            }
            truncated = True

        if _batch_items_byte_size(working) > byte_budget:
            for pos, item in enumerate(working):
                message = item.get("error")
                if not isinstance(message, str) or len(message) <= MIN_BATCH_ERROR_CHARS:
                    continue
                shrunk = dict(item)
                shrunk["error"] = _truncate_batch_error(message, MIN_BATCH_ERROR_CHARS)
                shrunk["error_truncated"] = True
                working[pos] = shrunk
                truncated = True
                if _batch_items_byte_size(working) <= byte_budget:
                    break

        return working, truncated

    def _batch_overflow_envelope(
        items: list[dict[str, Any]],
        counts: dict[str, int],
        failed_ids: list[str],
        failed_indexes: list[int],
        byte_budget: int,
    ) -> dict[str, Any]:
        """Last-resort response when even fully-shrunk items exceed the budget.

        Can only happen with a large batch of long tool names/ids, since every
        payload is already gone by this point. Keeps the aggregate counts and
        the per-item id/index/status skeleton -- never a partial, over-budget
        body -- and is itself clipped to the budget.
        """
        skeleton = [
            {"index": item["index"], "id": item["id"], "status": item["status"]} for item in items
        ]
        while skeleton and _batch_items_byte_size(skeleton) > byte_budget // 2:
            skeleton.pop()
        return {
            "ok": False,
            "error": (f"batch response exceeded the {byte_budget}-byte budget; results omitted"),
            "results": skeleton,
            "results_omitted": len(items) - len(skeleton),
            "counts": counts,
            "failed_ids": failed_ids[:MAX_BATCH_CALLS],
            "failed_indexes": failed_indexes[:MAX_BATCH_CALLS],
            "truncated": True,
        }

    @mcp.tool(annotations=READ_ONLY)
    async def invoke_read_tool_batch(
        ctx: Context,
        calls: list[BatchCall],
    ) -> dict[str, Any]:
        """Dispatch a bounded, ordered batch of read-only tool calls in one round trip.

        Nornir-inspired bounded fan-out: each entry in ``calls`` is
        dispatched sequentially (no concurrency in this version --
        deterministic ordering and rate-limit safety over throughput)
        through the identical read-only gate/response-bounding path
        ``invoke_read_tool`` itself uses. One call's failure never aborts
        the rest: every call gets its own ordered result entry plus
        rolled-up aggregate ``counts``, never a raised exception and never
        a success-shaped failure. A write/destructive/unknown tool named in
        any entry is rejected for that entry alone and never reaches the
        backend (same gate as ``invoke_read_tool``).

        Args:
            calls: bounded (max 25) ordered list of call objects, each with
                required "name" (exact backend tool name from find_tool),
                optional "arguments" (object, default {}), optional "id"
                (caller-supplied correlation string, max 100 chars, unique
                within the batch -- defaults to the call's list index as a
                string), and optional "cursor" (an opaque next_cursor from a
                previous truncated single-call or batch-item read of this
                exact tool+arguments, resumed exactly like invoke_read_tool's
                own cursor argument). "arguments" is bounded to 20,000
                serialized bytes and 8 levels of nesting per call -- an
                oversized/malformed call entry is rejected with status
                "invalid_call" before any dispatch is attempted for that
                entry, and never included in a validation-error message
                (so a secret placed in "arguments" is never echoed back).
                Duplicate ids reject the whole batch before any dispatch:
                correlating results by id is the point of supplying one, and
                silently returning two entries with the same id would make
                that impossible.

        Rate limiting is charged per *backend* call, not per batch, so a
        25-call batch draws 25 tokens from the same bucket a single
        invoke_read_tool call draws one from.

        Returns "ok" (True only when every call in the batch succeeded --
        never True while any failure exists), "results" (ordered list, one
        entry per call, each with "index", "id", "tool", "server", "status" --
        one of "ok", "error", "blocked", "unknown_tool", "invalid_cursor",
        "invalid_call" -- and either "result" (on "ok") or "error" (bounded
        to 500 characters, otherwise)), "counts" ("total"/"succeeded"/
        "failed"), "failed_ids" and "failed_indexes" (both ordered, one
        entry per failed call), and "truncated" (True when the response had
        to be shrunk to fit the configured byte budget --
        HPE_MCP_ROUTER_BATCH_RESPONSE_MAX_BYTES, default 300000). Each
        item additionally gets its own share of that budget while
        dispatching, so no single call can consume the whole batch's budget.
        The returned response is strictly within budget.
        """
        if not isinstance(calls, list):
            return {"ok": False, "error": "calls must be a list"}
        if not calls:
            return {"ok": False, "error": "calls must contain at least one entry"}
        if len(calls) > MAX_BATCH_CALLS:
            return {
                "ok": False,
                "error": (
                    f"calls has {len(calls)} entries, exceeding the {MAX_BATCH_CALLS}-entry bound"
                ),
            }

        duplicate_id = _first_duplicate_batch_id(calls)
        if duplicate_id is not None:
            return {
                "ok": False,
                "error": (
                    f"duplicate call id {duplicate_id!r}: ids must be unique "
                    "within a batch so results can be correlated"
                ),
            }

        byte_budget = _batch_response_budget_bytes()
        # Split the whole-response budget across the batch so one large read
        # cannot crowd out every other call's result. Each item still gets at
        # least the router's per-response floor.
        item_budget = max(_RESPONSE_BUDGET_MIN_BYTES, byte_budget // max(1, len(calls)))

        items: list[dict[str, Any]] = []
        for index, raw_call in enumerate(calls):
            items.append(
                await _dispatch_one_batch_call(ctx, index, raw_call, item_max_bytes=item_budget)
            )

        succeeded = sum(1 for item in items if item["status"] == "ok")
        failed_items = [item for item in items if item["status"] != "ok"]
        counts = {
            "total": len(items),
            "succeeded": succeeded,
            "failed": len(failed_items),
        }
        failed_ids = [item["id"] for item in failed_items]
        failed_indexes = [item["index"] for item in failed_items]

        shrunk, truncated = _shrink_batch_items_for_budget(items, byte_budget=byte_budget)
        if _batch_items_byte_size(shrunk) > byte_budget:
            return _batch_overflow_envelope(items, counts, failed_ids, failed_indexes, byte_budget)

        return {
            "ok": not failed_items,
            "results": shrunk,
            "counts": counts,
            "failed_ids": failed_ids,
            "failed_indexes": failed_indexes,
            "truncated": truncated,
        }


# ── Optional discovery convenience tools ──────────────────────────────────────
#
# default mode: include convenience wrappers (list_sites/find_device/etc.)
# minimal mode: expose only find_tool + invoke_read_tool + invoke_tool to minimize tool-list tokens
if _ROUTER_MODE != "minimal" and "central-monitoring" in _BACKENDS:

    @_dispatching_wrapper_tool(READ_ONLY)
    async def list_scopes(
        ctx: Context, limit: int = 100, offset: int = 0, full_list: bool = False
    ) -> dict[str, Any]:
        """List Central scopes (sites, groups, global) — ID + name (paginated)."""
        return await invoke_tool(
            ctx, "list_scopes", {"limit": limit, "offset": offset, "full_list": full_list}
        )

    @_dispatching_wrapper_tool(READ_ONLY)
    async def get_global_scope_id(ctx: Context) -> dict[str, Any]:
        """Return the global (org-wide) scope-id."""
        return await invoke_tool(ctx, "get_global_scope_id")

    @_dispatching_wrapper_tool(READ_ONLY)
    async def list_sites(ctx: Context, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List sites (paginated)."""
        return await invoke_tool(ctx, "list_sites", {"limit": limit, "offset": offset})

    @_dispatching_wrapper_tool(READ_ONLY)
    async def list_devices(ctx: Context, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List devices (paginated)."""
        return await invoke_tool(ctx, "list_devices", {"limit": limit, "offset": offset})

    @_dispatching_wrapper_tool(READ_ONLY)
    async def find_device(ctx: Context, query: str) -> dict[str, Any]:
        """Find a device by serial number."""
        return await invoke_tool(ctx, "find_device", {"serial_number": query})

    @_dispatching_wrapper_tool(READ_ONLY)
    async def find_client(ctx: Context, query: str) -> dict[str, Any]:
        """Find a client by name / MAC / IP."""
        return await invoke_tool(ctx, "find_client", {"mac_or_ip": query})


if _ROUTER_MODE != "minimal" and "rag-core" in _BACKENDS:

    @_dispatching_wrapper_tool(READ_ONLY)
    async def ask_docs(
        ctx: Context,
        query: str,
        top_k: int = 5,
        context: str | None = None,
    ) -> Any:
        """Ask Aruba/HPE docs for a compact cited answer.

        Use this for prose/how-to questions when you want a short answer instead
        of raw retrieval hits. For an ambiguous follow-up, pass a short summary
        of the prior turn in `context`. Exact endpoint/schema questions should
        still use lookup_api first.
        """
        args: dict[str, Any] = {"question": query, "top_k": top_k}
        if context:
            args["context"] = context
        return await invoke_tool(ctx, "ask_docs", args)

    @_dispatching_wrapper_tool(READ_ONLY)
    async def search_docs(
        ctx: Context,
        query: str,
        top_k: int = 5,
        source: str | None = None,
    ) -> Any:
        """Search Aruba/HPE documentation (Central config, APIs, NAC, VSG).

        For EXACT API questions (enum values, endpoints, schema fields) prefer
        lookup_api — it is lossless; this is fuzzy retrieval.
        """
        args: dict[str, Any] = {"query": query, "top_k": top_k}
        if source:
            args["source"] = source
        return await invoke_tool(ctx, "search_docs", args)

    @_dispatching_wrapper_tool(READ_ONLY)
    async def lookup_api(
        ctx: Context,
        query: str,
        top_k: int = 10,
        source: str | None = None,
        platform: str | None = None,
        version: str | None = None,
        include_metadata: bool = False,
    ) -> Any:
        """Exact Aruba Central API lookup — endpoints, schemas, fields, enum values.

        Use INSTEAD of search_docs for "what enum values does field X accept",
        "which endpoint configures Y and with what method", or "what fields does
        schema Z have". Authoritative answers from the parsed OpenAPI specs.
        Returns [] when the specs hold no confident answer — fall back to
        search_docs in that case.
        """
        args: dict[str, Any] = {"query": query, "top_k": top_k}
        for key, value in (
            ("source", source),
            ("platform", platform),
            ("version", version),
        ):
            if value:
                args[key] = value
        if include_metadata:
            args["include_metadata"] = True
        return await invoke_tool(ctx, "lookup_api", args)

    @_dispatching_wrapper_tool(READ_ONLY)
    async def list_skills(
        ctx: Context,
        platform: str | None = None,
        tag: str | None = None,
        detail: bool = False,
    ) -> Any:
        """Browse bundled multi-step runbooks (skills) from rag-core."""
        return await invoke_tool(
            ctx,
            "list_skills",
            {"platform": platform, "tag": tag, "detail": detail},
        )

    @_dispatching_wrapper_tool(READ_ONLY)
    async def load_skill(ctx: Context, name: str) -> Any:
        """Load one skill runbook body by name from rag-core."""
        return await invoke_tool(ctx, "load_skill", {"name": name})


# ── Router automation: dependency planning + reconciliation scheduling ──────
#
# Both tools below are strictly read-only/plan-only: they resolve tool
# references against the already-loaded, enabled backend catalog (never
# inferring an unavailable tool), and never call invoke_tool/invoke_read_tool
# themselves. Excluded from `minimal` mode to keep that profile's tool-list
# token cost at exactly find_tool + invoke_read_tool + invoke_tool.
if _ROUTER_MODE != "minimal":
    _PLAN_AMBIGUITY_MARGIN = 0.15

    def _plan_step_metadata(name: str) -> dict[str, Any]:
        tool = _tool_index[name]
        server = _tool_backend_names.get(name)
        capability = _tool_capability(tool)
        return {
            "server": server,
            "platform": _server_platform(server),
            "capability": capability,
            "recommended_dispatcher": (
                "invoke_read_tool" if capability == "read" else "invoke_tool"
            ),
        }

    def _resolve_plan_step_tool(
        step: dict[str, Any],
    ) -> tuple[str | None, bool, bool, list[dict[str, Any]]]:
        """Resolve one plan step to a catalog tool name.

        Returns ``(tool_name_or_None, resolved, ambiguous, candidates)``. An
        explicit ``"tool"`` name is resolved only against the currently
        loaded catalog (``_tool_index``) -- never guessed; an unknown name
        resolves to ``(None, False, False, [])``. A ``"hint"`` falls back to
        the same bounded, deterministic keyword search ``find_tool`` uses
        (no semantic/embedding call), and is marked ambiguous when more than
        one candidate scores within ``_PLAN_AMBIGUITY_MARGIN`` of the top
        score.
        """
        explicit = step.get("tool")
        if explicit:
            name = str(explicit)
            if name not in _tool_index:
                return None, False, False, []
            return name, True, False, [{"name": name, **_plan_step_metadata(name)}]
        hint = step.get("hint")
        if not hint:
            return None, False, False, []
        candidates = _keyword_hits(str(hint), _router_automation.MAX_PLAN_CANDIDATES_PER_STEP)
        if not candidates:
            return None, False, False, []
        top_score = candidates[0].get("score", 0.0)
        close = [c for c in candidates if top_score - c.get("score", 0.0) <= _PLAN_AMBIGUITY_MARGIN]
        ambiguous = len(close) > 1
        return candidates[0]["name"], True, ambiguous, candidates

    @mcp.tool(annotations=READ_ONLY)
    def plan_tool_workflow(
        steps: list[dict[str, Any]],
        include_candidates: bool = False,
    ) -> dict[str, Any]:
        """Build a deterministic, read-only dependency/order plan across enabled backend tools.

        Never executes any tool. Every resolved tool reference is checked only
        against the currently loaded, enabled backend catalog (the same index
        find_tool searches) -- an unresolved or ambiguous reference is
        reported explicitly, never guessed or silently dropped.

        Args:
            steps: bounded (max 25) list of step specs. Each step is a dict:
                - "id": optional stable step id (str); defaults to "step_<index>".
                - "tool": exact tool name to resolve via the loaded catalog
                  (preferred -- deterministic, exact match, never guessed).
                - "hint": free-text action description used only when "tool"
                  is omitted; resolved via the same bounded keyword search
                  find_tool uses (no semantic/embedding guessing). Marked
                  "ambiguous" when multiple close-scoring candidates exist.
                - "depends_on": list of step ids (or exact tool names) that
                  must run before this step.
            include_candidates: include up to 5 scored candidate tools per
                unresolved/ambiguous step. Defaults to False to keep the plan
                compact.

        Returns "ok", "steps" (resolved metadata per step), "order"
        (topological order, or None whenever any step/dependency is
        unresolved or the graph has a cycle), "acyclic", "cycles",
        "unresolved_step_ids", "unresolved_dependencies", and "artifact" (a
        router_dependency_plan-shaped payload suitable for
        hpe_networking_mcp.pipeline.artifact_contracts.write_artifact -- never written to disk
        by this tool). This never calls invoke_tool/invoke_read_tool.
        """
        _load_all_backends()
        if not isinstance(steps, list) or not steps:
            return {"ok": False, "error": "steps must be a non-empty list", "steps": []}
        if len(steps) > _router_automation.MAX_PLAN_STEPS:
            return {
                "ok": False,
                "error": (
                    f"steps has {len(steps)} entries, exceeding the "
                    f"{_router_automation.MAX_PLAN_STEPS} bound"
                ),
                "steps": [],
            }

        step_ids: list[str] = []
        by_tool_name: dict[str, str] = {}
        resolved_steps: list[dict[str, Any]] = []
        errors: list[str] = []

        for index, raw_step in enumerate(steps):
            if not isinstance(raw_step, dict):
                errors.append(f"steps[{index}] must be an object")
                step_id = f"step_{index}"
                step_ids.append(step_id)
                resolved_steps.append(
                    {
                        "id": step_id,
                        "tool": None,
                        "resolved": False,
                        "ambiguous": False,
                        "server": None,
                        "platform": None,
                        "capability": "unknown",
                        "recommended_dispatcher": None,
                        "depends_on": [],
                    }
                )
                continue
            step_id = str(raw_step.get("id") or f"step_{index}")
            if step_id in step_ids:
                errors.append(f"duplicate step id: {step_id!r}")
            step_ids.append(step_id)
            tool_name, resolved, ambiguous, candidates = _resolve_plan_step_tool(raw_step)
            entry: dict[str, Any] = {
                "id": step_id,
                "tool": tool_name,
                "resolved": resolved,
                "ambiguous": ambiguous,
                "depends_on": [str(d) for d in (raw_step.get("depends_on") or [])],
            }
            if resolved and tool_name is not None:
                entry.update(_plan_step_metadata(tool_name))
                by_tool_name[tool_name] = step_id
            else:
                entry.update(
                    {
                        "server": None,
                        "platform": None,
                        "capability": "unknown",
                        "recommended_dispatcher": None,
                    }
                )
            if include_candidates and (not resolved or ambiguous):
                entry["candidates"] = [
                    {"name": c["name"], "server": c.get("server"), "score": c.get("score")}
                    for c in candidates[: _router_automation.MAX_PLAN_CANDIDATES_PER_STEP]
                ]
            resolved_steps.append(entry)

        # depends_on may reference a step id OR a resolved tool name.
        edges: dict[str, list[str]] = {}
        unresolved_dependencies: list[dict[str, str]] = []
        for entry in resolved_steps:
            deps: list[str] = []
            for dep in entry["depends_on"]:
                if dep in step_ids:
                    deps.append(dep)
                elif dep in by_tool_name:
                    deps.append(by_tool_name[dep])
                else:
                    unresolved_dependencies.append({"step": entry["id"], "missing": dep})
            edges[entry["id"]] = deps

        order, cycles = _router_automation.resolve_dependency_order(step_ids, edges)
        acyclic = not cycles
        unresolved_step_ids = [e["id"] for e in resolved_steps if not e["resolved"]]
        blocked = (
            bool(errors)
            or bool(unresolved_dependencies)
            or bool(unresolved_step_ids)
            or not acyclic
        )
        effective_order = order if not blocked else None

        artifact: dict[str, Any] | None = None
        artifact_error: str | None = None
        try:
            artifact_steps = [
                {
                    "step_id": entry["id"],
                    "tool": entry["tool"],
                    "resolved": entry["resolved"],
                    "ambiguous": entry["ambiguous"],
                    "capability": entry["capability"],
                    "platform": entry["platform"],
                    "depends_on": entry["depends_on"],
                }
                for entry in resolved_steps
            ]
            payload = _router_automation.build_dependency_plan_payload(
                steps=artifact_steps,
                order=effective_order,
                acyclic=acyclic,
                cycles=cycles,
                unresolved_step_ids=unresolved_step_ids,
            )
            built = _artifact_contracts.build_artifact(
                _artifact_contracts.ROUTER_DEPENDENCY_PLAN, payload
            )
            artifact = _artifact_contracts.to_json_dict(built)
        except _artifact_contracts.ArtifactValidationError as exc:
            artifact_error = str(exc)

        return {
            "ok": not errors and not blocked,
            "steps": resolved_steps,
            "order": effective_order,
            "acyclic": acyclic,
            "cycles": cycles,
            "unresolved_step_ids": unresolved_step_ids,
            "unresolved_dependencies": unresolved_dependencies,
            "errors": errors,
            "artifact": artifact,
            "artifact_error": artifact_error,
        }

    @mcp.tool(annotations=READ_ONLY)
    def plan_reconciliation_schedule(
        cadence: dict[str, Any] | str,
        tools: list[str] | None = None,
        platforms: list[str] | None = None,
        servers: list[str] | None = None,
        max_entries: int = 50,
    ) -> dict[str, Any]:
        """Build a bounded, read-only, plan-only recurring reconciliation schedule.

        Never creates an OS timer, cron job, or GitHub Actions schedule, and
        never executes a tool -- this only validates a cadence and resolves a
        bounded set of currently enabled tools into a schedule
        *specification*. Write/destructive tools are always excluded from the
        executable entry list (reported in "excluded" instead, with a
        reason), regardless of whether the caller explicitly requested them.

        Args:
            cadence: either a named cadence string ("hourly", "daily",
                "weekly") or an object such as
                {"kind": "interval_minutes", "interval_minutes": 30} or
                {"kind": "cron", "expression": "*/15 * * * *"}. Validated
                structurally only -- never parsed into an actual next-run
                time or registered as a real schedule.
            tools: exact tool names to resolve via the loaded catalog. Omit
                to fall back to the platforms/servers filters below.
            platforms: normalized platform filter (e.g. "central", "glp")
                applied to the loaded catalog when tools is omitted.
            servers: exact backend server name filter (e.g.
                "central-monitoring") applied to the loaded catalog when tools
                is omitted.
            max_entries: safety ceiling on schedule entries (default 50, max
                100).

        Returns "ok", "cadence" (validated descriptor), "entries"
        (read/diagnostic tools only), "excluded" (everything else, with a
        reason), "dry_run" (always True), and "artifact" (a
        router_reconciliation_plan-shaped payload suitable for
        hpe_networking_mcp.pipeline.artifact_contracts.write_artifact -- never written to disk
        by this tool).
        """
        _load_all_backends()
        cadence_result = _router_automation.validate_cadence(cadence)
        if not cadence_result.get("valid"):
            return {
                "ok": False,
                "error": cadence_result.get("reason"),
                "cadence": cadence_result,
            }

        bounded_max_entries = max(
            1, min(max_entries, _router_automation.MAX_RECONCILIATION_ENTRIES)
        )
        _max_tools_input = (
            _router_automation.MAX_RECONCILIATION_ENTRIES
            + _router_automation.MAX_RECONCILIATION_EXCLUDED_DETAIL
        )
        if tools is not None and len(tools) > _max_tools_input:
            return {
                "ok": False,
                "error": (
                    f"tools has {len(tools)} entries, exceeding the {_max_tools_input} bound"
                ),
                "cadence": cadence_result,
            }
        platform_filter = {str(p).strip().lower() for p in (platforms or []) if p}
        server_filter = {str(s).strip().lower() for s in (servers or []) if s}

        if tools:
            candidate_names = [str(t) for t in tools]
        else:
            candidate_names = []
            for name, backend_name in _tool_backend_names.items():
                if backend_name not in _BACKENDS:
                    continue
                if server_filter and backend_name.lower() not in server_filter:
                    continue
                platform = _server_platform(backend_name)
                if platform_filter and (platform or "").lower() not in platform_filter:
                    continue
                candidate_names.append(name)
            candidate_names.sort()

        candidates: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        excluded_total = 0
        for name in candidate_names:
            tool = _tool_index.get(name)
            server = _tool_backend_names.get(name)
            if tool is None or server not in _BACKENDS:
                excluded_total += 1
                if len(excluded) < _router_automation.MAX_RECONCILIATION_EXCLUDED_DETAIL:
                    excluded.append(
                        {"tool": name, "capability": "unknown", "reason": "unresolved_tool"}
                    )
                continue
            candidates.append(
                {
                    "tool": name,
                    "server": server,
                    "platform": _server_platform(server),
                    "capability": _tool_capability(tool),
                    "enabled": True,
                }
            )

        entries, capability_excluded, capability_excluded_total = (
            _router_automation.partition_reconciliation_candidates(
                candidates, max_entries=bounded_max_entries
            )
        )
        excluded.extend(capability_excluded)
        excluded_total += capability_excluded_total
        # Each source above is independently bounded to
        # MAX_RECONCILIATION_EXCLUDED_DETAIL, but their concatenation is not
        # -- re-cap the combined detail list so this router-native tool
        # (called directly, not proxied through _dispatch_tool's response
        # budgeting) never returns an unbounded payload. excluded_total
        # already reflects the true count regardless of this cap.
        if len(excluded) > _router_automation.MAX_RECONCILIATION_EXCLUDED_DETAIL:
            excluded = excluded[: _router_automation.MAX_RECONCILIATION_EXCLUDED_DETAIL]

        artifact: dict[str, Any] | None = None
        artifact_error: str | None = None
        try:
            payload = _router_automation.build_reconciliation_plan_payload(
                cadence=cadence_result,
                entries=entries,
                excluded=excluded,
                excluded_count=excluded_total,
            )
            built = _artifact_contracts.build_artifact(
                _artifact_contracts.ROUTER_RECONCILIATION_PLAN, payload
            )
            artifact = _artifact_contracts.to_json_dict(built)
        except _artifact_contracts.ArtifactValidationError as exc:
            artifact_error = str(exc)

        return {
            "ok": artifact_error is None,
            "cadence": cadence_result,
            "entries": entries,
            "excluded": excluded,
            "excluded_count": excluded_total,
            "dry_run": True,
            "artifact": artifact,
            "artifact_error": artifact_error,
        }

    @mcp.tool(annotations=READ_ONLY)
    def evaluate_compliance_policy(
        observations: list[dict[str, Any]],
        policy: list[dict[str, Any]],
        policy_id: str = "ad-hoc",
        max_result_entries: int = 200,
    ) -> dict[str, Any]:
        """Evaluate already-retrieved observations against a declarative compliance policy.

        Pure, bounded, read-only evaluation only -- this never calls
        invoke_tool/invoke_read_tool or any backend itself, and never fetches
        anything. Fetch device/config/inventory state first (e.g. one or more
        invoke_read_tool results), then pass the already-retrieved data here
        as `observations` alongside a declarative `policy`. The architecture
        is inspired by NAPALM's `compliance_report` (a fixed comparison-
        operator dispatch table evaluated over structured state) and by
        Nornir-style aggregate run counts, but is implemented independently
        in `src/hpe_networking_mcp/pipeline/compliance.py` with this repository's own bounds and
        conventions -- no eval/exec, no arbitrary expressions, no dynamic
        imports, and no write/destructive tool is ever reachable from here.

        Args:
            observations: bounded (max 100) list of objects, one per device/
                entity already retrieved by the caller (e.g. a single
                invoke_read_tool result, or one element of a list response).
                Never fetched by this tool.
            policy: bounded (max 50) list of rule objects, each with "field"
                (a dotted/indexed path, e.g. "interfaces[0].status" or
                "firmware.version" -- Mapping key lookup and Sequence integer
                indexing only, never eval/attribute access), "operator" (one
                of "eq", "ne", "lt", "le", "gt", "ge", "contains", "in",
                "regex_fullmatch", "version_gte", "version_range", "exists",
                "not_exists"), and "expected" (required for every operator
                except exists/not_exists). Optional per-rule "id" (defaults
                to "rule_<index>"), "severity" ("critical"/"error"/"warning"/
                "info", default "error", informational only -- it does not
                change pass/fail logic), and "optional" (bool, default
                False -- a missing field on an optional rule is reported
                "skipped" instead of "error"). A structurally invalid policy
                (unknown operator, malformed field path, an "expected" shape
                that does not match its operator, an unparsable regex/
                version value, or exceeding a bound) is rejected before any
                observation is evaluated.
            policy_id: free-text label carried through into the report and
                artifact only.
            max_result_entries: bounded per-rule result detail cap (default
                200, max 500). Aggregate counts always reflect the true
                total even when the detail list is capped -- see
                "results_truncated"/"results_total".

        Returns "ok", "compliant" (True only when every rule for every
        observation passed or was explicitly skipped -- never True while any
        "fail"/"error" result exists), "counts" (pass/fail/error/skipped
        totals), "observations" (per-observation compliant flag + counts),
        "results" (bounded per-rule detail), "results_total"/
        "results_truncated", and "artifact" (a compliance_report-shaped
        payload suitable for hpe_networking_mcp.pipeline.artifact_contracts.write_artifact --
        never written to disk by this tool). A structurally invalid policy/
        observations input fails closed with "ok": False and a bounded
        "error" message before any rule evaluation begins.
        """
        try:
            report = _compliance.evaluate_policy(
                observations,
                policy,
                policy_id=policy_id,
                max_result_entries=max_result_entries,
            )
        except _compliance.ComplianceError as exc:
            return {"ok": False, "error": str(exc)}

        artifact: dict[str, Any] | None = None
        artifact_error: str | None = None
        try:
            payload = _compliance.build_compliance_report_payload(
                policy_id=report["policy_id"],
                compliant=report["compliant"],
                counts=report["counts"],
                observations=report["observations"],
                results=report["results"],
                results_total=report["results_total"],
            )
            built = _artifact_contracts.build_artifact(
                _artifact_contracts.COMPLIANCE_REPORT, payload
            )
            artifact = _artifact_contracts.to_json_dict(built)
        except _artifact_contracts.ArtifactValidationError as exc:
            artifact_error = str(exc)

        return {
            "ok": artifact_error is None,
            "compliant": report["compliant"],
            "policy_id": report["policy_id"],
            "rule_count": report["rule_count"],
            "observation_count": report["observation_count"],
            "counts": report["counts"],
            "observations": report["observations"],
            "results": report["results"],
            "results_total": report["results_total"],
            "results_truncated": report["results_truncated"],
            "artifact": artifact,
            "artifact_error": artifact_error,
        }


if _ROUTER_MODE == "direct":
    _register_direct_backend_tools()


# ── Observability label/classification helpers ───────────────────────────────
#
# Shared by MetricsMiddleware's label_resolver and AuditLogMiddleware's
# classifier so the two never diverge on "what backend tool actually ran".
# Bounded by construction: for invoke_tool/invoke_read_tool this resolves to
# the finite, already-loaded backend tool catalog (falling back to
# "unknown" for anything not found there); for every other router-native
# tool it is just that tool's own (fixed, small) name. Never reads any
# argument value beyond the single expected "name" key, and never reads
# result content at all.
#: Label used when one batch call fans out to more than one distinct backend
#: tool or backend server. Bounded and constant by construction, so it can
#: never leak an argument value into a metric series or an audit record.
BATCH_MULTI_LABEL = "batch_multi"

#: Router tools whose real dispatch target lives in their arguments. Kept in
#: sync with ``hpe_networking_mcp.mcp_servers._middleware.audit_log._DISPATCHING_TOOL_NAMES``.
_DISPATCHING_ROUTER_TOOLS = frozenset({"invoke_tool", "invoke_read_tool", "invoke_read_tool_batch"})

#: Hard cap on how many batch entries label resolution will inspect. Matches
#: the batch bound in default mode, and stays defined in minimal mode (where
#: the batch tool is not registered) so observability never depends on which
#: router mode is active.
MAX_BATCH_CALLS_LABEL_CAP = 25


def _batch_call_targets(arguments: dict[str, Any]) -> list[str]:
    """Bounded list of backend tool names named by one batch request.

    Reads only each entry's ``name``, exactly like the single-call resolver
    reads ``arguments["name"]`` -- never any argument *value*. Entries are
    capped at ``MAX_BATCH_CALLS`` so a malformed oversized request cannot make
    label resolution unbounded.
    """
    calls = arguments.get("calls") if isinstance(arguments, dict) else None
    if not isinstance(calls, list):
        return []
    names: list[str] = []
    for raw_call in calls[:MAX_BATCH_CALLS_LABEL_CAP]:
        mapping = raw_call.model_dump() if isinstance(raw_call, BaseModel) else raw_call
        if not isinstance(mapping, dict):
            continue
        target = mapping.get("name")
        if isinstance(target, str) and target in _tool_index:
            names.append(target)
    return names


def _router_call_labels(name: str, arguments: dict[str, Any]) -> tuple[str, str, str]:
    """Resolve bounded ``(tool, backend, capability)`` labels for one call."""
    if name in {"invoke_tool", "invoke_read_tool"} and isinstance(arguments, dict):
        target = arguments.get("name")
        target_name = str(target) if target else None
        if target_name and target_name in _tool_index:
            backend = _tool_backend_names.get(target_name, "router")
            capability = _tool_capability(_tool_index[target_name])
            return (target_name, backend, capability)
        return (name, "router", "unknown")
    if name == "invoke_read_tool_batch" and isinstance(arguments, dict):
        # A batch is still a dispatching call: labelling it "router"/"unknown"
        # made every batched backend call invisible to metrics and audit. When
        # the whole batch targets one tool/backend, use it; otherwise collapse
        # to a single constant multi-target label.
        targets = _batch_call_targets(arguments)
        if targets:
            distinct = sorted(set(targets))
            backends = sorted({_tool_backend_names.get(target, "router") for target in targets})
            tool_label = distinct[0] if len(distinct) == 1 else BATCH_MULTI_LABEL
            backend_label = backends[0] if len(backends) == 1 else BATCH_MULTI_LABEL
            # Every dispatched entry is annotation-gated read-only.
            return (tool_label, backend_label, "read")
        return (name, "router", "read")
    tool = mcp._tool_manager._tools.get(name)
    capability = _tool_capability(tool) if tool is not None else "unknown"
    return (name, "router", capability)


def _router_call_classification(name: str, arguments: dict[str, Any]) -> str:
    """Audit-log write/destructive classification -- reuses the same
    resolution as metrics so the two never disagree."""
    return _router_call_labels(name, arguments)[2]


def _router_call_target(name: str, arguments: dict[str, Any]) -> str | None:
    """Return only a catalog-resolved dispatch target for audit records."""
    if name not in _DISPATCHING_ROUTER_TOOLS:
        return None
    target, backend, _capability = _router_call_labels(name, arguments)
    return target if backend != "router" else "unknown"


def _reactive_error_hint(
    tool_name: str, status_code: int | None, platform: str | None
) -> str | None:
    """``ResponseEnvelopeMiddleware`` hint resolver -- thin glue to
    ``error_help.reactive_hint`` so ``_middleware/response_envelope.py``
    stays router-agnostic (it never imports ``tool_router`` itself)."""
    return _error_help.reactive_hint(tool_name, status_code, platform=platform)


def _suggest_router_tool(name: str, limit: int) -> list[dict[str, Any]]:
    """Bounded 'did you mean' suggestions for an unknown router tool name."""
    return [
        {
            "name": item["name"],
            "description": item.get("description", ""),
            "match": item.get("match", "keyword"),
            "score": item.get("score", 0.0),
        }
        for item in _keyword_hits(name.replace("_", " "), limit)
    ]


def build_router_middlewares() -> list[Any]:
    """Build the unified router's middleware chain, in execution order.

    Extracted from ``__main__`` so the ``hpe-mcp-router`` console script and
    router-level regression tests install the exact same chain a real client
    session gets -- notably ``PIITokenizeMiddleware``, which has to sit on the
    router because default (minimal-mode) sessions never touch a backend
    server's own middleware: every NAC/ClearPass call arrives here as an
    ``invoke_tool``/``invoke_read_tool`` dispatch.

    Returns:
        List of middleware instances for ``install_middleware``.
    """
    from hpe_networking_mcp.mcp_servers._middleware import (
        AuditLogMiddleware,
        MacNormalizeMiddleware,
        MetricsMiddleware,
        NullStripMiddleware,
        PIITokenizeMiddleware,
        RateLimitMiddleware,
        ResponseEnvelopeMiddleware,
        SecretTokenizeMiddleware,
        UnknownToolSuggestMiddleware,
        get_default_registry,
        metrics_enabled,
    )

    metrics_registry = get_default_registry()
    metrics_on = metrics_enabled()
    # One shared bucket, charged per *backend* call. The dispatching tools are
    # exempt from the middleware (they would otherwise pay one token for the
    # outer MCP call regardless of how many backend calls they make) and the
    # bucket is instead drawn from inside _dispatch_tool via the gate below.
    rate_limiter = RateLimitMiddleware(
        rate=8.0,
        on_wait=metrics_registry.record_rate_limit_wait if metrics_on else None,
        exempt_names=_DISPATCHING_ROUTER_TOOLS | _WRAPPER_DISPATCHING_TOOLS,
    )
    set_dispatch_rate_gate(rate_limiter.acquire)
    middlewares: list[Any] = [
        NullStripMiddleware(),
        rate_limiter,
        UnknownToolSuggestMiddleware(
            lambda: mcp._tool_manager._tools,
            suggestion_provider=_suggest_router_tool,
            platform_hint_resolver=_unconfigured_platform_hint,
        ),
        ResponseEnvelopeMiddleware(
            label_resolver=_router_call_labels,
            platform_resolver=_server_platform,
            hint_resolver=_reactive_error_hint,
        ),
        SecretTokenizeMiddleware(),
        # PII tokenization (opt-in via HPE_MCP_TOKENIZE_PII) must be installed
        # here too, not only on central-nac/clearpass-core: router dispatch
        # returns the backend result through *this* chain, so without it a
        # default `central,glp,rag` session would hand raw visitor/guest
        # email/phone values to the model. Ordered after SecretTokenizeMiddleware
        # so the two independent vaults tokenize in a stable order.
        PIITokenizeMiddleware(),
        MetricsMiddleware(metrics_registry, label_resolver=_router_call_labels),
        AuditLogMiddleware(
            classifier=_router_call_classification,
            target_resolver=_router_call_target,
        ),
    ]
    if os.getenv("HPE_MCP_NORMALIZE_MACS", "").strip().lower() in {"1", "true", "yes"}:
        middlewares.append(MacNormalizeMiddleware())
    return middlewares


def install_router_middleware() -> list[Any]:
    """Install the unified router's middleware chain on this module's server.

    Idempotent (``install_middleware`` replaces rather than stacks). Returns
    the installed chain so callers/tests can assert on its composition.
    """
    from hpe_networking_mcp.mcp_servers._cache_hygiene import stable_list_tools
    from hpe_networking_mcp.mcp_servers._middleware import install_middleware

    middlewares = build_router_middlewares()
    stable_list_tools(mcp)
    install_middleware(mcp, middlewares)
    return middlewares


def main() -> None:
    """Console-script entry point for the unified ``hpe-networking-mcp`` router.

    Installs the router middleware chain, then runs the server on the
    transport selected by ``MCP_TRANSPORT`` (see ``shared.run_server``).
    """
    from hpe_networking_mcp.mcp_servers.shared import run_server

    install_router_middleware()
    run_server(mcp)


if __name__ == "__main__":
    main()
