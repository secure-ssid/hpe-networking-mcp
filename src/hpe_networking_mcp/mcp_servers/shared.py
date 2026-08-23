"""Shared clients, helpers, and constants for all MCP servers."""
import ast
import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote, urlsplit

from dotenv import load_dotenv
from mcp.types import ToolAnnotations

from hpe_networking_mcp.mcp_servers import _sdk_compat
from hpe_networking_mcp.pipeline.clients.central_client import CentralClient
from hpe_networking_mcp.pipeline.clients.glp_client import GLPClient
from hpe_networking_mcp.pipeline.clients.mcp_client import MCPClient
from hpe_networking_mcp.pipeline.clients.pooled_clients import aclose_pooled_clients
from hpe_networking_mcp.pipeline.clients.token_manager import TokenManager
from hpe_networking_mcp.pipeline.config import build_account_contexts
from hpe_networking_mcp.pipeline.url_validation import validate_infra_url

if TYPE_CHECKING:
    from mcp.server.transport_security import TransportSecuritySettings

# ---------------------------------------------------------------------------
# MCP Tool Annotations — safety hints for MCP clients
# ---------------------------------------------------------------------------

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

# Same safety profile as READ_ONLY, but ``open_world_hint=False`` for tools that
# only query a local index (LanceDB/SQLite/Ollama) and never reach the live
# Central/GLP API -- there is no unpredictable external system to interact
# with. Used by the local-only RAG tools (search_docs / lookup_api / ask_docs).
READ_ONLY_LOCAL = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

DIAGNOSTIC = ToolAnnotations(
    title="Diagnostic",
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)

WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)

DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)

IDEMPOTENT_WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

_SENSITIVE_KEY_EXACT = {
    "auth",
    "authorization",
    "key",
    "community_string",
    "snmp_read",
    "snmp_write",
}
_SENSITIVE_KEY_SUFFIXES = (
    "api_key",
    "apikey",
    "credential",
    "credentials",
    "_key",
    "passphrase",
    "password",
    "psk",
    "secret",
    "token",
)
_REDACTED = "******"
# Auth header names an operator can *rename* via env. EdgeConnect sends its
# API token under whatever `EDGECONNECT_AUTH_HEADER` names, so the static
# rules above stop covering it the moment that name no longer looks like a
# secret: the default "X-Auth-Token" normalizes to "x_auth_token" and is
# caught by the "token" suffix, but a custom "X-Ec-Session" normalizes to
# "x_ec_session" -- in no exact set, matching no suffix -- and would be
# echoed verbatim if an upstream error body or debug endpoint reflected the
# request headers back. Resolved at call time (not import time) so a
# runtime env change takes effect immediately.
_CONFIGURABLE_SENSITIVE_HEADER_ENV_VARS = ("EDGECONNECT_AUTH_HEADER",)


def _normalize_key(key: Any) -> str:
    """Fold a dict key/header name to a comparable ``snake_case`` token."""
    key_text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key).strip())
    return re.sub(r"[^a-z0-9]+", "_", key_text.lower()).strip("_")


def _configured_sensitive_keys() -> frozenset[str]:
    """Normalized key names contributed by operator-renamable auth headers."""
    return frozenset(
        normalized
        for env_var in _CONFIGURABLE_SENSITIVE_HEADER_ENV_VARS
        if (normalized := _normalize_key(os.environ.get(env_var, "")))
    )


def _is_sensitive_key(key: Any) -> bool:
    normalized = _normalize_key(key)
    if normalized in _SENSITIVE_KEY_EXACT or any(
        normalized.endswith(suffix) for suffix in _SENSITIVE_KEY_SUFFIXES
    ):
        return True
    return normalized in _configured_sensitive_keys()


# ---------------------------------------------------------------------------
# Stringified-container detection — shared by redact_sensitive() here and by
# _middleware/secret_tokenizer.py + _middleware/pii_tokenizer.py.
#
# All three walk a result tree looking for sensitive/PII-*keyed* string
# values to redact/tokenize -- but some APIs return a field whose value is
# itself a JSON- or Python-repr-encoded dict/list serialized as one opaque
# string (for example, a generically-named annotation/metadata field
# carrying nested scope or device details). A sensitive value nested inside
# that blob is invisible to a walker that only ever inspects the *immediate*
# parent key, no matter how the outer field happens to be named. Centralizing
# the detect/parse/re-serialize step here means none of the three call sites
# can diverge on what counts as a "blob" or how one gets re-encoded, and
# keeps this genuinely stateless (no vault, no tokens), so importing it does
# not reintroduce any coupling between the secret and PII vaults -- both
# tokenizer modules deliberately keep those independent.
# ---------------------------------------------------------------------------
_CONTAINER_BRACKETS = {"{": "}", "[": "]", "(": ")"}
# Real nested annotation/config blobs are well under this; anything larger is
# treated as an ordinary (if large) string, not a candidate blob, so this
# can't become a CPU-cost lever for a hostile or merely huge upstream payload.
_MAX_STRINGIFIED_CONTAINER_LENGTH = 50_000
_CONTAINER_PARSE_ERRORS = (
    ValueError,
    TypeError,
    SyntaxError,
    RecursionError,
    MemoryError,
    OverflowError,
)


def parse_stringified_container(text: str) -> tuple[Any, str] | None:
    """Parse ``text`` as a JSON or Python-repr dict/list/tuple, quote-agnostic.

    Returns ``(parsed, dialect)`` — ``dialect`` is ``"json"`` or ``"python"``,
    remembered so :func:`serialize_stringified_container` can re-encode in
    the same style — or ``None`` when ``text`` doesn't look like, or fails to
    parse as, a serialized container. A cheap bracket/length pre-check runs
    before any real parsing, so the overwhelming majority of ordinary string
    values (which don't start/end with a matching ``{}``/``[]``/``()`` pair)
    cost only that check. Never raises: this runs on values callers do not
    control, so any parse failure is treated as "not a blob", exactly like an
    ordinary string.
    """
    stripped = text.strip()
    if len(stripped) < 2 or len(stripped) > _MAX_STRINGIFIED_CONTAINER_LENGTH:
        return None
    if _CONTAINER_BRACKETS.get(stripped[0]) != stripped[-1]:
        return None
    try:
        return json.loads(stripped), "json"
    except _CONTAINER_PARSE_ERRORS:
        pass
    try:
        parsed = ast.literal_eval(stripped)
    except _CONTAINER_PARSE_ERRORS:
        return None
    if isinstance(parsed, (dict, list, tuple)):
        return parsed, "python"
    return None


def serialize_stringified_container(parsed: Any, dialect: str) -> str:
    """Re-encode a parsed container back to text in its original dialect."""
    if dialect == "json":
        try:
            return json.dumps(parsed, default=str)
        except _CONTAINER_PARSE_ERRORS:
            return repr(parsed)
    return repr(parsed)


def redact_sensitive(value: Any) -> Any:
    """Recursively redact likely secrets before returning tool previews/results."""
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            # ``bound_collection_response``'s "_pagination" block is helper-
            # generated metadata whose "list_key" field trips the "_key"
            # sensitive-suffix rule. Redacting it corrupts pagination metadata
            # the router's cursor/truncation logic reads. Exempt ONLY that one
            # field: the rest of the "_pagination" subtree is still recursively
            # redacted, so a real secret nested under a key named _pagination
            # never gets a blanket pass.
            if key == "_pagination" and isinstance(item, dict):
                scrubbed = redact_sensitive(dict(item))
                if "list_key" in item:
                    scrubbed["list_key"] = item["list_key"]
                out[key] = scrubbed
            elif _is_sensitive_key(key):
                out[key] = _REDACTED
            else:
                out[key] = redact_sensitive(item)
        return out
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    if isinstance(value, str):
        stripped = value.strip().lower()
        if stripped.startswith(("bearer ", "token ", "basic ")):
            return _REDACTED
        # Not directly sensitive-shaped -- but it might be a serialized
        # container with a sensitive field nested inside it (see above).
        blob = parse_stringified_container(value)
        if blob is not None:
            parsed, dialect = blob
            redacted_parsed = redact_sensitive(parsed)
            if redacted_parsed != parsed:
                return serialize_stringified_container(redacted_parsed, dialect)
    return value


# Raised backend exceptions embed the SDK-framed error string
# ``ToolError("Error executing tool <name>: <message>")``, which can carry a
# bearer credential *mid-string* -- escaping ``redact_sensitive``'s
# prefix-only value rule. Both error paths (the router's dispatch helper and
# the envelope middleware's on_error) mask that form; homing the regex and the
# two-step mask here keeps the credential shape in one place (HX-1/HX-3).
#
# The {8,} minimum (Sentinel review amendment) preserves masking of real
# platform credentials (always >=16 chars) while letting short bearer PROSE
# like "Bearer token missing or expired" pass through unchanged -- the
# over-masking that would otherwise corrupt exactly the 401 messages most
# likely to appear on an error path. A genuine sub-8-char credential would
# slip past this mask; accepted tradeoff (the vault tokenizer still catches
# known secrets).
ERROR_CREDENTIAL_RE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=\-]{8,}")


def redact_tool_error_text(text: str) -> str:
    """Mask credentials in a client-visible backend-tool error string.

    Reuses the shared walker (``redact_sensitive``) for sensitive-keyed/
    container text and the exact ``bearer ``/``token ``/``basic `` prefix
    shapes, then masks the mid-string bearer-credential form the SDK's
    ``ToolError("Error executing tool <name>: <message>")`` framing can embed.
    Single application point per error path; both callers delegate here.
    """
    text = redact_sensitive(text)
    return ERROR_CREDENTIAL_RE.sub(_REDACTED, text)


_READ_ONLY_ACCESS_VALUES = {"read-only", "readonly", "read_only", "ro"}
_READ_WRITE_ACCESS_VALUES = {"read-write", "readwrite", "read_write", "rw"}
ACCESS_PROFILE_ENV_VAR = "HPE_MCP_ACCESS_PROFILE"
ACCESS_PROFILES = frozenset({"safe-read-only", "custom", "full-read-write"})


def access_profile() -> str:
    """Return the validated aggregate write-access profile.

    ``custom`` is the compatibility default and preserves the existing
    per-platform gate behavior. The two explicit aggregate profiles override
    those defaults without bypassing tool-level dry-run or confirmation gates.
    """
    value = os.getenv(ACCESS_PROFILE_ENV_VAR, "").strip().lower()
    if not value:
        return "custom"
    if value not in ACCESS_PROFILES:
        raise InvalidRuntimeConfigError(
            f"{ACCESS_PROFILE_ENV_VAR}={value!r}; expected one of "
            f"{sorted(ACCESS_PROFILES)!r}."
        )
    return value


def optional_product_access_mode() -> str:
    profile = access_profile()
    if profile == "safe-read-only":
        return "read-only"

    raw = os.getenv("HPE_MCP_PRODUCT_ACCESS")
    if raw is None:
        return "read-write" if profile == "full-read-write" else "read-only"
    value = raw.strip().lower()
    if value in _READ_WRITE_ACCESS_VALUES:
        return "read-write"
    if value in _READ_ONLY_ACCESS_VALUES:
        return "read-only"
    return "read-only"


def optional_product_writes_allowed() -> bool:
    return optional_product_access_mode() == "read-write"


def _safe_profile_transition_instruction(env_var: str | None = None) -> str:
    selective_gate = (
        f"{env_var}=1"
        if env_var is not None
        else "the relevant HPE_MCP_<PLATFORM>_WRITES gate to 1"
    )
    return (
        "Switch the complete coordinated configuration to full-read-write "
        "(for wizard-managed configs, rerun "
        "`python3 scripts/setup_wizard.py --access-profile full-read-write`), "
        "or use custom with HPE_MCP_READONLY=0 and "
        f"{selective_gate}"
    )


def optional_product_write_blocked(tool_name: str) -> dict[str, Any]:
    if access_profile() == "safe-read-only":
        enable_instruction = _safe_profile_transition_instruction()
    else:
        enable_instruction = (
            "Set HPE_MCP_PRODUCT_ACCESS=read-write under custom, or switch the "
            "complete coordinated configuration to full-read-write"
        )
    return {
        "error": (
            f"Tool '{tool_name}' is disabled because optional-product writes are "
            f"read-only or invalid. {enable_instruction}."
        ),
        "tool": tool_name,
        "status": "blocked",
    }


#: Backend server names for the optional starter products.
#:
#: Not every one of these has an entry in :data:`_PLATFORM_WRITE_GATES` --
#: ``design-core`` does not -- yet all of them are enableable through
#: ``HPE_MCP_PRODUCT_ACCESS``. The write gate needs that distinction to tell a
#: known product whose writes are merely *off* from a backend nothing can
#: enable at all, so the two get different refusals.
#:
#: ``tool_router._OPTIONAL_SERVER_NAMES`` is derived independently from
#: ``_OPTIONAL_BACKENDS``; ``test_write_gate_mechanism_parity`` pins the two
#: together so this cannot silently fall behind.
OPTIONAL_PRODUCT_SERVER_NAMES: frozenset[str] = frozenset(
    {
        "aos8-core",
        "apstra-core",
        "axis-core",
        "clearpass-core",
        "design-core",
        "edgeconnect-core",
        "mist-core",
        "uxi-core",
    }
)


_TRUTHY_FLAG_VALUES = {"1", "true", "yes", "on"}


def env_flag(name: str) -> bool:
    """Parse a boolean env flag ("1"/"true"/"yes"/"on", case/space-insensitive)."""
    return os.getenv(name, "").strip().lower() in _TRUTHY_FLAG_VALUES


def global_readonly_enabled() -> bool:
    """Return whether the aggregate or emergency read-only gate is active.

    ``safe-read-only`` and ``HPE_MCP_READONLY=1`` both block every
    ``write``/``destructive`` tool while leaving read and diagnostic tools
    available. The environment kill switch remains authoritative even if a
    contradictory full-read-write profile reaches this low-level check before
    startup validation rejects it.
    """
    return access_profile() == "safe-read-only" or env_flag("HPE_MCP_READONLY")


def global_write_blocked(tool_name: str) -> dict[str, Any]:
    """Build the blocked response for an aggregate read-only gate."""
    if access_profile() == "safe-read-only":
        reason = (
            f"{ACCESS_PROFILE_ENV_VAR}=safe-read-only. "
            f"{_safe_profile_transition_instruction()} to allow guarded write tools."
        )
    else:
        reason = (
            "HPE_MCP_READONLY is set. Unset HPE_MCP_READONLY to allow "
            "write/destructive tools."
        )
    return {
        "error": f"Tool '{tool_name}' is disabled because {reason}",
        "tool": tool_name,
        "status": "blocked",
    }


def ungated_backend_write_blocked(
    server: str | None,
    tool_name: str,
    *,
    capability: str = "write",
    execution_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the blocked response for a write on a backend with no known gate.

    Deny-by-default. A ``write``/``destructive`` tool whose backend resolves to
    no entry in :data:`_PLATFORM_WRITE_GATES` cannot be enabled by *any*
    documented environment variable, so there is nothing an operator could set
    to permit it. Allowing it would mean shipping an ungated write; refusing is
    the only reading consistent with the rest of this module.

    Reaching this is a packaging defect rather than a configuration mistake --
    a backend grew a write tool without its platform being registered -- so the
    message names the registry to fix instead of an env var to set.
    """
    where = f" on backend '{server}'" if server else ""
    payload: dict[str, Any] = {
        "error": (
            f"Tool '{tool_name}' is disabled because it is a {capability} tool"
            f"{where} with no write gate, so no setting can enable it. Register the "
            "backend's platform in _PLATFORM_WRITE_GATES (mcp_servers/shared.py) "
            "before shipping write tools on it."
        ),
        "tool": tool_name,
        "status": "blocked",
    }
    if server:
        payload["server"] = server
    if execution_contract is not None:
        payload["execution_contract"] = execution_contract
    return payload


# ---------------------------------------------------------------------------
# Per-platform write gates
# ---------------------------------------------------------------------------
#
# Under the compatibility-preserving ``custom`` profile, Central and GLP keep
# their existing, independently-audited gates, and both are fail-closed:
#   - Central (ops/config tools): writes stay disabled until
#     HPE_MCP_CENTRAL_WRITES=1 is set, so an operator opts *in* explicitly
#     without touching HPE_MCP_PRODUCT_ACCESS (which also governs the
#     optional-product backends below).
#   - GLP writes stay fail-closed behind HPE_MCP_GLP_V2BETA1_WRITES=1,
#     enforced at the GLPClient layer (hpe_networking_mcp.pipeline.clients.glp_client). The gate
#     below just gives that platform a name in the same lookup table so
#     tooling/docs can treat every platform uniformly.
#
# The seven optional-product starter backends (AOS8, EdgeConnect, Apstra,
# Mist, ClearPass, UXI, Axis) continue to share HPE_MCP_PRODUCT_ACCESS by
# default -- unchanged behavior for anyone who already sets it. Each also
# gets its own override env var so an operator can diverge a single
# platform (e.g. allow Mist writes without opening every optional product).
_TRUTHY_ENV_VALUES = ("1", "true", "yes", "on")
_FALSY_ENV_VALUES = ("0", "false", "no", "off")


@dataclass(frozen=True)
class PlatformWriteGate:
    """One platform's write-enable resolution rule.

    Attributes:
        platform: canonical lowercase platform key (e.g. "mist").
        env_var: the platform-specific override env var name.
        default_enabled: write-enabled state used when ``env_var`` is unset
            and there is no shared fallback. ``default_enabled=False``
            denies until the operator opts in; ``None`` defers to
            :func:`optional_product_writes_allowed`.
    """

    platform: str
    env_var: str
    default_enabled: bool | None


_PLATFORM_WRITE_GATES: dict[str, PlatformWriteGate] = {
    "central": PlatformWriteGate("central", "HPE_MCP_CENTRAL_WRITES", False),
    "glp": PlatformWriteGate("glp", "HPE_MCP_GLP_V2BETA1_WRITES", False),
    "aos8": PlatformWriteGate("aos8", "HPE_MCP_AOS8_WRITES", None),
    "edgeconnect": PlatformWriteGate("edgeconnect", "HPE_MCP_EDGECONNECT_WRITES", None),
    "apstra": PlatformWriteGate("apstra", "HPE_MCP_APSTRA_WRITES", None),
    "mist": PlatformWriteGate("mist", "HPE_MCP_MIST_WRITES", None),
    "clearpass": PlatformWriteGate("clearpass", "HPE_MCP_CLEARPASS_WRITES", None),
    "uxi": PlatformWriteGate("uxi", "HPE_MCP_UXI_WRITES", None),
    "axis": PlatformWriteGate("axis", "HPE_MCP_AXIS_WRITES", None),
}

PLATFORM_WRITE_GATE_NAMES: tuple[str, ...] = tuple(sorted(_PLATFORM_WRITE_GATES))


def _platform_gate(platform: str) -> PlatformWriteGate:
    key = platform.strip().lower()
    gate = _PLATFORM_WRITE_GATES.get(key)
    if gate is None:
        raise ValueError(
            f"unknown platform {platform!r}; expected one of {PLATFORM_WRITE_GATE_NAMES}"
        )
    return gate


def platform_write_gate_state(platform: str) -> dict[str, Any]:
    """Resolve a platform write gate, including override precedence.

    The aggregate safe/full profiles take precedence. ``custom`` preserves the
    existing platform override/default/shared-fallback behavior. Contradictory
    explicit values fail closed here and are rejected by
    :func:`validate_access_profile_environment` during server startup.
    """
    gate = _platform_gate(platform)
    profile = access_profile()
    raw = os.environ.get(gate.env_var)

    if profile == "safe-read-only":
        return {
            "env_var": gate.env_var,
            "state": "disabled",
            "enabled": False,
            "source": ACCESS_PROFILE_ENV_VAR,
        }

    if profile == "full-read-write":
        if gate.default_enabled is None:
            shared_raw = os.environ.get("HPE_MCP_PRODUCT_ACCESS")
            if shared_raw is not None:
                shared_value = shared_raw.strip().lower()
                if shared_value not in _READ_WRITE_ACCESS_VALUES:
                    return {
                        "env_var": gate.env_var,
                        "state": (
                            "disabled"
                            if shared_value in _READ_ONLY_ACCESS_VALUES
                            else "invalid"
                        ),
                        "enabled": False,
                        "source": "HPE_MCP_PRODUCT_ACCESS",
                    }
        if raw is not None:
            value = raw.strip().lower()
            if value not in _TRUTHY_ENV_VALUES:
                return {
                    "env_var": gate.env_var,
                    "state": "disabled" if value in _FALSY_ENV_VALUES else "invalid",
                    "enabled": False,
                    "source": "platform_override",
                }
        return {
            "env_var": gate.env_var,
            "state": "enabled",
            "enabled": True,
            "source": ACCESS_PROFILE_ENV_VAR,
        }

    if raw is not None:
        value = raw.strip().lower()
        if value in _TRUTHY_ENV_VALUES:
            state = "enabled"
        elif value in _FALSY_ENV_VALUES:
            state = "disabled"
        else:
            state = "invalid"
        return {
            "env_var": gate.env_var,
            "state": state,
            "enabled": state == "enabled",
            "source": "platform_override",
        }
    if gate.default_enabled is not None:
        return {
            "env_var": gate.env_var,
            "state": "enabled" if gate.default_enabled else "disabled",
            "enabled": gate.default_enabled,
            "source": "platform_default",
        }

    shared_raw = os.environ.get("HPE_MCP_PRODUCT_ACCESS")
    shared_mode = optional_product_access_mode()
    shared_invalid = (
        shared_raw is not None
        and shared_raw.strip().lower()
        not in (_READ_ONLY_ACCESS_VALUES | _READ_WRITE_ACCESS_VALUES)
    )
    enabled = shared_mode == "read-write"
    return {
        "env_var": gate.env_var,
        "state": "invalid" if shared_invalid else ("enabled" if enabled else "disabled"),
        "enabled": enabled,
        "source": "HPE_MCP_PRODUCT_ACCESS",
    }


def platform_writes_allowed(platform: str) -> bool:
    """Return whether write tools are enabled for ``platform``.

    Resolution order (highest priority first):

    The aggregate ``HPE_MCP_ACCESS_PROFILE`` outranks all three: ``safe-read-only``
    denies every platform and ``full-read-write`` allows every platform without
    any per-platform variable. Under ``custom`` the order below applies.

    1. The platform's own override env var (e.g. ``HPE_MCP_MIST_WRITES=1``),
       read as an explicit opt-in truthy/falsy value if set at all.
    2. The platform's built-in default when there is no shared fallback.
       Central and GLP both default to disabled, so their write tools stay
       out of reach until the operator opts in.
    3. :func:`optional_product_writes_allowed` (the shared
       ``HPE_MCP_PRODUCT_ACCESS`` toggle) for the optional-product
       backends, which is itself read-only unless configured otherwise.

    Raises:
        ValueError: ``platform`` is not one of the known platform keys.
    """
    return bool(platform_write_gate_state(platform)["enabled"])


def platform_write_enable_instruction(platform: str, env_var: str) -> str:
    if access_profile() == "safe-read-only":
        return _safe_profile_transition_instruction(env_var)
    return f"Set {env_var}=1"


def build_write_execution_contract(
    platform: str,
    capability: str,
    *,
    supports_dry_run: bool = False,
    dry_run_state: str = "unsupported",
    supports_confirm: bool = False,
    requires_confirmation: bool = False,
    idempotent: bool = False,
    next_action: str | None = None,
) -> dict[str, Any]:
    """Build the compact contract used for write discovery and dispatch."""
    gate = platform_write_gate_state(platform)
    if next_action is None:
        if not gate["enabled"]:
            next_action = (
                f"{platform_write_enable_instruction(platform, gate['env_var'])}, "
                "then retry only after explicit user approval."
            )
        elif supports_dry_run:
            next_action = "Call invoke_tool with dry_run=true to preview the change."
        elif requires_confirmation:
            next_action = "Call invoke_tool only after explicit user confirmation."
        else:
            next_action = "Call invoke_tool only after explicit user intent."
    return {
        "platform": platform,
        "capability": capability,
        "gate": {
            "env_var": gate["env_var"],
            "state": gate["state"],
            "source": gate["source"],
        },
        "dry_run": {
            "supported": supports_dry_run,
            "state": dry_run_state if supports_dry_run else "unsupported",
        },
        "confirm": {
            "supported": supports_confirm,
            "required": requires_confirmation,
        },
        "idempotent": idempotent,
        "next_action": next_action,
    }


def platform_write_blocked(
    platform: str,
    tool_name: str,
    *,
    capability: str = "write",
    supports_dry_run: bool = False,
    dry_run_state: str = "unsupported",
    supports_confirm: bool = False,
    requires_confirmation: bool = False,
    idempotent: bool = False,
    execution_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the standard blocked-write response for ``tool_name`` on ``platform``."""
    gate = _platform_gate(platform)
    enable_instruction = platform_write_enable_instruction(platform, gate.env_var)
    shared_hint = (
        " The shared fallback also blocks writes when "
        "HPE_MCP_PRODUCT_ACCESS=read-only or invalid."
        if gate.default_enabled is None and access_profile() == "custom"
        else ""
    )
    return {
        "error": (
            f"Tool '{tool_name}' is disabled because {platform} writes are not enabled. "
            f"{enable_instruction} to allow {platform} write workflows."
            f"{shared_hint}"
        ),
        "tool": tool_name,
        "status": "blocked",
        "platform": platform,
        "execution_contract": execution_contract
        or build_write_execution_contract(
            platform,
            capability,
            supports_dry_run=supports_dry_run,
            dry_run_state=dry_run_state,
            supports_confirm=supports_confirm,
            requires_confirmation=requires_confirmation,
            idempotent=idempotent,
        ),
    }


def enforce_platform_write(platform: str, tool_name: str) -> dict[str, Any] | None:
    """Return a blocked-response dict if ``platform`` write access is disabled,
    or ``None`` if the caller should proceed with the write.

    Minimal integration for a domain tool module::

        from hpe_networking_mcp.mcp_servers.shared import enforce_platform_write

        blocked = enforce_platform_write("mist", "mist_set_site")
        if blocked:
            return blocked
        ...  # perform the write
    """
    if platform_writes_allowed(platform):
        return None
    return platform_write_blocked(platform, tool_name)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Precedence (highest first): process environment > .env > YAML > built-in
# defaults. ``load_dotenv(override=False)`` with no explicit path -- rather
# than a hardcoded ``parents[N] / ".env"`` -- searches upward from *this
# file's* location (see python-dotenv's ``find_dotenv``), the same way
# ``hpe_networking_mcp.pipeline.config.load_credentials`` already does. That
# matters after the src-layout move: a fixed ``parents[1]`` here resolved to
# ``src/hpe_networking_mcp/.env`` -- one directory too shallow to ever reach
# the repository root where ``scripts/setup_wizard.py``,
# ``scripts/doctor.py``, and ``scripts/run_http_router.sh`` all read/write
# ``.env`` -- so a repo-root ``.env`` was silently never loaded before the
# module-level MCP_TRANSPORT/MCP_HOST/etc. reads just below. Walking upward
# from ``__file__`` finds that same repo-root ``.env`` regardless of the
# process's current working directory. ``override=False`` means a value the
# caller's shell/orchestrator already exported always wins over ``.env``.
load_dotenv(override=False)

# ---------------------------------------------------------------------------
# Transport configuration
# ---------------------------------------------------------------------------

MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
DEFAULT_HTTP_PORT = 8010
MCP_PORT = int(os.environ.get("MCP_PORT", str(DEFAULT_HTTP_PORT)))


def _csv_env(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


# ---------------------------------------------------------------------------
# Runtime-selection env var validation (HPE_MCP_TOOLSETS / _PRODUCTS /
# _RAG_BACKEND)
# ---------------------------------------------------------------------------
#
# These three env vars pick which backend modules load at startup. Before
# this hardening pass, an unrecognized value was either silently ignored
# (an unknown HPE_MCP_PRODUCTS/_TOOLSETS entry never matched any backend
# and was dropped with no error at all) or silently coerced to a default
# (any HPE_MCP_RAG_BACKEND other than "redis" fell through to the
# "lancedb" branch, even a typo like "lancdeb"). Both hide a real
# misconfiguration until someone notices a tool/backend is missing. Reject
# non-empty unrecognized values loudly instead, at import/startup time,
# naming both what was requested and what is actually valid so the fix is
# obvious from the error alone.


class InvalidRuntimeConfigError(RuntimeError):
    """Raised when a HPE_MCP_* runtime-selection env var names an
    unrecognized or contradictory value. Carries the offending env var name
    plus enough context for an operator to fix the config without reading
    source."""


def reject_unknown_env_choices(
    env_var: str,
    requested: list[str],
    valid: "frozenset[str] | set[str]",
) -> None:
    """Raise :class:`InvalidRuntimeConfigError` if ``requested`` names any
    value outside ``valid``.

    A no-op when ``requested`` is empty -- an unset/empty env var keeps
    whatever default behavior the caller already implements; this only
    rejects a genuinely non-empty, unrecognized selection.

    Args:
        env_var: the env var name, used verbatim in the error message.
        requested: the parsed (already lower-cased/stripped) values the
            operator actually set.
        valid: the full set of recognized values for this env var.
    """
    if not requested:
        return
    unknown = sorted({value for value in requested if value not in valid})
    if unknown:
        raise InvalidRuntimeConfigError(
            f"{env_var} requested unrecognized value(s) {unknown!r} "
            f"(requested={requested!r}); valid values are {sorted(valid)!r}."
        )


def validate_access_profile_environment() -> str:
    """Validate aggregate and legacy write-access settings as one contract."""
    profile = access_profile()
    invalid: list[str] = []
    conflicts: list[str] = []

    product_raw = os.environ.get("HPE_MCP_PRODUCT_ACCESS")
    product_mode: str | None = None
    if product_raw is not None:
        value = product_raw.strip().lower()
        if value in _READ_ONLY_ACCESS_VALUES:
            product_mode = "read-only"
        elif value in _READ_WRITE_ACCESS_VALUES:
            product_mode = "read-write"
        else:
            invalid.append(f"HPE_MCP_PRODUCT_ACCESS={product_raw!r}")

    readonly_raw = os.environ.get("HPE_MCP_READONLY")
    readonly_enabled = False
    if readonly_raw is not None:
        value = readonly_raw.strip().lower()
        if value in _TRUTHY_ENV_VALUES:
            readonly_enabled = True
        elif value not in _FALSY_ENV_VALUES:
            invalid.append(f"HPE_MCP_READONLY={readonly_raw!r}")

    platform_values: dict[str, bool] = {}
    for gate in _PLATFORM_WRITE_GATES.values():
        raw = os.environ.get(gate.env_var)
        if raw is None:
            continue
        value = raw.strip().lower()
        if value in _TRUTHY_ENV_VALUES:
            platform_values[gate.env_var] = True
        elif value in _FALSY_ENV_VALUES:
            platform_values[gate.env_var] = False
        else:
            invalid.append(f"{gate.env_var}={raw!r}")

    if invalid:
        raise InvalidRuntimeConfigError(
            "Invalid write-access setting(s): "
            + ", ".join(sorted(invalid))
            + ". Boolean gates accept 1/0, true/false, yes/no, or on/off; "
            "HPE_MCP_PRODUCT_ACCESS accepts read-only or read-write."
        )

    if profile == "safe-read-only":
        if product_mode == "read-write":
            conflicts.append("HPE_MCP_PRODUCT_ACCESS=read-write")
        conflicts.extend(name for name, enabled in platform_values.items() if enabled)
    elif profile == "full-read-write":
        if readonly_enabled:
            conflicts.append("HPE_MCP_READONLY=1")
        if product_mode == "read-only":
            conflicts.append("HPE_MCP_PRODUCT_ACCESS=read-only")
        conflicts.extend(name for name, enabled in platform_values.items() if not enabled)

    if conflicts:
        raise InvalidRuntimeConfigError(
            f"{ACCESS_PROFILE_ENV_VAR}={profile!r} conflicts with "
            f"{sorted(conflicts)!r}. Use {ACCESS_PROFILE_ENV_VAR}=custom for "
            "mixed per-platform settings, or remove the contradictory values."
        )
    return profile


_VALID_RAG_BACKENDS = frozenset({"lancedb", "redis"})


def resolve_rag_backend(default: str = "lancedb") -> str:
    """Resolve + validate ``HPE_MCP_RAG_BACKEND``.

    Unset/empty keeps the documented default (``lancedb``). Any non-empty,
    unrecognized value raises :class:`InvalidRuntimeConfigError` instead of
    silently falling back to the default -- the same "reject, don't
    silently coerce" behavior as :func:`reject_unknown_env_choices` for
    HPE_MCP_TOOLSETS/HPE_MCP_PRODUCTS.
    """
    raw = os.environ.get("HPE_MCP_RAG_BACKEND", "").strip().lower()
    if not raw:
        return default
    reject_unknown_env_choices("HPE_MCP_RAG_BACKEND", [raw], _VALID_RAG_BACKENDS)
    return raw


def validate_product_base_url(value: str, *, product: str) -> str:
    """Validate optional product base URLs before attaching API tokens.

    Thin wrapper over :func:`hpe_networking_mcp.pipeline.url_validation.validate_infra_url`
    -- the same validator Central/GLP base+token URLs go through
    (``pipeline.config.build_account_contexts``) -- kept here under its
    original name/signature for the optional-product backends that already
    import it from this module.
    """
    try:
        return validate_infra_url(value, label=f"{product} base URL")
    except ValueError as exc:
        # Preserve the exact historical message shape ("{product} base URL
        # must be an absolute HTTP(S) URL", with no repeated `(got ...)`
        # suffix) for existing callers/log scrapers.
        message = str(exc)
        prefix = f"{product} base URL must be an absolute HTTP(S) URL"
        if message.startswith(prefix):
            raise ValueError(prefix) from exc
        raise


_LOOPBACK_HOST_NAMES = {"localhost", "localhost.localdomain"}


def _is_loopback_host(host: str) -> bool:
    """True if ``host`` is loopback-only (127.0.0.1 / ::1 / localhost)."""
    candidate = host.strip().lower().strip("[]")
    if candidate in _LOOPBACK_HOST_NAMES:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


class UnsafeHttpBindingError(RuntimeError):
    """Raised when MCP_HOST binds beyond loopback without an explicit,
    reviewed allow-list -- refuses to start rather than silently exposing
    (or silently breaking, via the SDK's loopback-only allow-list default)
    the streamable-HTTP transport."""


def _is_supported_sdk_wildcard(value: str) -> bool:
    """True if ``value`` is the *only* wildcard shape the installed MCP
    SDK's ``TransportSecurityMiddleware`` actually matches: a non-empty,
    literal host/origin immediately followed by a literal ``:*`` port
    suffix (e.g. ``example.com:*`` / ``https://example.com:*``).

    ``TransportSecurityMiddleware._validate_host``/``_validate_origin`` do
    an exact-string match, or -- only for an entry ending in ``:*`` -- a
    plain ``str.startswith(base + ":")`` check. Any other use of ``*``
    (a bare ``"*"``, a subdomain glob like ``*.example.com``, ``*:*``, a
    ``:*`` with an empty base, etc.) is not recognized by the SDK at all:
    it is compared as an exact literal string that a real Host/Origin
    header will never equal, so it silently matches *nothing* -- turning
    every real request into a 421/403 instead of the open door an operator
    who wrote it probably intended.
    """
    stripped = value.strip()
    if not stripped.endswith(":*"):
        return False
    base = stripped[: -len(":*")]
    return bool(base) and "*" not in base


def _has_unsupported_wildcard(values: list[str]) -> bool:
    """True if any entry in ``values`` uses ``*`` outside the one grammar
    :func:`_is_supported_sdk_wildcard` recognizes."""
    return any("*" in value and not _is_supported_sdk_wildcard(value) for value in values)


def _configure_http_transport(host: str, port: int) -> "TransportSecuritySettings | None":
    """Build the explicit ``transport_security`` for this HTTP bind.

    Installed MCP SDK 2.x's ``MCPServer.Settings`` no longer carries
    ``host``/``port``/``transport_security`` fields at all -- those became
    explicit keyword arguments to ``MCPServer.run(transport="streamable-http",
    host=..., port=..., transport_security=...)`` /
    ``MCPServer.streamable_http_app(host=..., transport_security=...)``
    instead (see ``mcp.server.mcpserver.server.MCPServer`` in the installed
    SDK). Assigning ``settings.host``/``settings.port`` raises
    ``pydantic.ValidationError`` ("no field") because ``Settings`` is a strict
    pydantic model with none of those fields any more. This helper computes
    the ``TransportSecuritySettings`` value to pass through explicitly;
    ``run_server`` threads ``host``/``port`` the same way.

    Also hardens host/origin allow-list behavior when ``MCP_HOST`` changes:
    the SDK only auto-applies a loopback-safe ``transport_security`` default
    when ``host`` is exactly ``"127.0.0.1"``/``"localhost"``/``"::1"``; for
    any other host it passes ``transport_security=None`` straight through,
    which disables Host/Origin validation entirely rather than falling back
    to a safe default. Binding beyond loopback without an explicit
    ``MCP_ALLOWED_HOSTS``/``MCP_ALLOWED_ORIGINS`` is refused loudly instead:
    see ``UnsafeHttpBindingError``.

    Returns:
        A ``TransportSecuritySettings`` to pass explicitly to
        ``run()``/``streamable_http_app()``, or ``None`` to let the SDK apply
        its own loopback-only default (safe only because ``host`` is
        confirmed loopback in that branch).

    Raises:
        UnsafeHttpBindingError: ``host`` is non-loopback and the allow-list
            configuration is missing, wildcarded, or DNS-rebinding
            protection is disabled without its explicit acknowledgement.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    dns_rebinding_protection = _env_bool("MCP_DNS_REBINDING_PROTECTION", True)
    allowed_hosts = _csv_env("MCP_ALLOWED_HOSTS")
    allowed_origins = _csv_env("MCP_ALLOWED_ORIGINS")

    if not _is_loopback_host(host):
        if not allowed_hosts or not allowed_origins:
            raise UnsafeHttpBindingError(
                f"MCP_HOST={host!r} binds beyond loopback; MCP_ALLOWED_HOSTS and "
                "MCP_ALLOWED_ORIGINS must both be set explicitly (comma-separated "
                "host:port / origin values, e.g. 'central-mcp.example.com:*') before "
                "the server will start. Refusing to start with an ambiguous "
                "allow-list rather than binding publicly without one."
            )
        if _has_unsupported_wildcard(allowed_hosts) or _has_unsupported_wildcard(
            allowed_origins
        ):
            raise UnsafeHttpBindingError(
                "MCP_ALLOWED_HOSTS/MCP_ALLOWED_ORIGINS contain a wildcard entry the "
                "installed MCP SDK does not recognize as a port wildcard (only an "
                "exact '<host>:*' / '<origin>:*' suffix is supported -- see "
                "_is_supported_sdk_wildcard) while "
                f"MCP_HOST={host!r} binds beyond loopback. An unrecognized wildcard "
                "(e.g. a bare '*' or a subdomain glob) silently matches nothing and "
                "would block every real request. Use explicit host:port / origin "
                "values or an exact '<host>:*' port wildcard."
            )
        if not dns_rebinding_protection and not _env_bool(
            "HPE_MCP_ALLOW_INSECURE_HTTP_BINDING", False
        ):
            raise UnsafeHttpBindingError(
                f"MCP_HOST={host!r} binds beyond loopback with DNS-rebinding "
                "protection disabled (MCP_DNS_REBINDING_PROTECTION=0). Set "
                "HPE_MCP_ALLOW_INSECURE_HTTP_BINDING=1 to acknowledge the risk."
            )
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=dns_rebinding_protection,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        )

    # Loopback host: only build an explicit TransportSecuritySettings if the
    # operator supplied their own allow-list or disabled DNS-rebinding
    # protection; otherwise return None so the SDK applies its own
    # loopback-only default (127.0.0.1/localhost/::1 with wildcard ports).
    if allowed_hosts or allowed_origins or not dns_rebinding_protection:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=dns_rebinding_protection,
            allowed_hosts=allowed_hosts
            or ["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=allowed_origins
            or ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
        )
    return None


_HEALTH_PATHS = ("/livez", "/readyz", "/healthz")
_HEALTH_ROUTES_ATTR = "_hpe_mcp_health_routes_installed"


def _readiness_detail() -> dict[str, Any]:
    """Process-local readiness signal -- config is loadable, no network calls.

    Deliberately does not call Central/GLP/any external API: readiness here
    means "this process is configured well enough to attempt work", not
    "upstream APIs are reachable" (that belongs in a tool call, not a probe
    an orchestrator polls every few seconds).

    Beyond "does the credentials file exist", this also confirms the
    credentials file actually *parses* into the expected structure and
    passes the same URL/workspace-ID validation
    ``get_client``/``get_glp_client`` rely on
    (``pipeline.config.build_account_contexts``) -- still with zero network
    I/O; ``build_account_contexts`` never constructs a ``TokenManager`` or
    makes a request, it only loads YAML/env and validates shapes/URLs.

    Never includes a credential *value* (client secret, token, etc.) in the
    returned detail:
      - our own ``ValueError``s (raised by ``build_account_contexts`` /
        ``validate_infra_url`` / workspace-ID validation) only ever
        interpolate URLs, hostnames, and workspace IDs -- never a secret --
        so those messages are safe to return verbatim and name the exact
        setting to fix.
      - anything else (e.g. a YAML parse error, which can otherwise echo a
        raw snippet of the file -- including a `client_secret:` line -- back
        in its own message) is reported only by exception type, never by
        its message text.
    """
    creds_path = os.environ.get("CREDS_PATH", "config/credentials.yaml")
    exists = Path(creds_path).exists()
    detail: dict[str, Any] = {
        "creds_path": creds_path,
        "creds_path_exists": exists,
        "credentials_loadable": False,
    }
    if not exists:
        detail["credentials_error"] = "credentials file does not exist"
        return detail

    try:
        build_account_contexts(creds_path)
    except ValueError:
        detail["credentials_error"] = (
            "credential configuration is invalid; check server logs or run "
            "`hpe-mcp-doctor` locally for details"
        )
    except Exception as exc:  # noqa: BLE001 -- never leak file content/secrets
        detail["credentials_error"] = (
            f"credentials file failed to load ({type(exc).__name__}); "
            "check server logs for detail"
        )
    else:
        detail["credentials_loadable"] = True
    return detail


def _register_health_routes(mcp_instance: Any) -> None:
    """Register unauthenticated /livez, /readyz, /healthz routes.

    Uses ``MCPServer.custom_route`` (installed SDK API) so these bypass MCP
    protocol negotiation entirely and never touch an external API -- safe
    for a container orchestrator or load balancer to poll every few
    seconds. Idempotent: safe to call multiple times on the same instance.
    """
    if getattr(mcp_instance, _HEALTH_ROUTES_ATTR, False):
        return

    from starlette.requests import Request
    from starlette.responses import JSONResponse

    async def livez(_request: Request) -> JSONResponse:
        # Process is up and able to answer HTTP at all.
        return JSONResponse({"status": "ok"})

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def readyz(_request: Request) -> JSONResponse:
        detail = _readiness_detail()
        ready = detail["creds_path_exists"] and detail["credentials_loadable"]
        body = {"status": "ok" if ready else "not_ready", "detail": detail}
        return JSONResponse(body, status_code=200 if ready else 503)

    mcp_instance.custom_route("/livez", methods=["GET"], include_in_schema=False)(livez)
    mcp_instance.custom_route("/readyz", methods=["GET"], include_in_schema=False)(readyz)
    mcp_instance.custom_route("/healthz", methods=["GET"], include_in_schema=False)(healthz)
    setattr(mcp_instance, _HEALTH_ROUTES_ATTR, True)


_METRICS_ROUTE_ATTR = "_hpe_mcp_metrics_route_installed"


def _register_metrics_route(mcp_instance: Any) -> None:
    """Register an opt-in ``GET /metrics`` bounded-JSON-snapshot route.

    Only called from ``run_server`` when ``HPE_MCP_METRICS_HTTP`` is
    explicitly truthy (see below) -- never registered by default, and never
    reached at all on the stdio transport. This is a ``custom_route`` on the
    *same* MCPServer-built Starlette app as every other HTTP route on this instance,
    so it automatically inherits the same loopback/allow-list protections
    (``_configure_http_transport``) and the same bearer-token gate
    (``BearerAuthASGIMiddleware`` only exempts ``_HEALTH_PATHS``, so
    ``/metrics`` requires ``Authorization: Bearer <token>`` whenever
    ``MCP_HTTP_BEARER_TOKEN`` is set) -- no separate auth mechanism to keep in sync.

    The snapshot itself (``MetricsRegistry.snapshot``) contains only
    bounded, allow-listed labels and numeric aggregates -- never arguments,
    results, identifiers, or exception messages.
    """
    if getattr(mcp_instance, _METRICS_ROUTE_ATTR, False):
        return

    from starlette.requests import Request
    from starlette.responses import JSONResponse

    async def metrics(_request: Request) -> JSONResponse:
        from hpe_networking_mcp.mcp_servers._middleware.metrics import (
            get_default_registry,
            metrics_enabled,
        )

        if not metrics_enabled():
            return JSONResponse(
                {
                    "enabled": False,
                    "hint": "Set HPE_MCP_METRICS=1 to enable in-process collection.",
                }
            )
        try:
            snapshot = get_default_registry().snapshot()
        except Exception:
            return JSONResponse({"error": "metrics snapshot unavailable"}, status_code=500)
        snapshot["enabled"] = True
        return JSONResponse(snapshot)

    mcp_instance.custom_route("/metrics", methods=["GET"], include_in_schema=False)(metrics)
    setattr(mcp_instance, _METRICS_ROUTE_ATTR, True)


def _http_bearer_token() -> str | None:
    """Optional static bearer token protecting the MCP HTTP endpoint.

    Unset by default -- localhost stdio/HTTP stay frictionless. Health
    endpoints (_HEALTH_PATHS) are always exempt: they must work for an
    orchestrator before any client authenticates, and must not require MCP
    negotiation or a shared secret.
    """
    token = os.environ.get("MCP_HTTP_BEARER_TOKEN", "").strip()
    return token or None


class BearerAuthASGIMiddleware:
    """Narrow ASGI middleware enforcing a static bearer token on every HTTP
    path except the exempt health-check paths.

    This deliberately does not use the SDK's OAuth resource-server flow
    (``TokenVerifier`` / ``AuthSettings``): that machinery expects a real
    issuer, scopes, and protected-resource metadata, which is the wrong
    shape for "one shared secret protects the local/lab HTTP endpoint".
    A narrow ASGI middleware keeps that simple case simple and auditable.
    """

    def __init__(self, app: Any, token: str, exempt_paths: tuple[str, ...] = _HEALTH_PATHS):
        self.app = app
        self._token = token
        # Pre-encoded once: the comparison itself is done on bytes (see
        # __call__) so a non-ASCII presented token can never raise.
        self._expected_token = token.encode("utf-8")
        self.exempt_paths = exempt_paths

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path") in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        # Compare raw bytes, never decoded ``str``. ``hmac.compare_digest``
        # raises TypeError on a ``str`` containing non-ASCII characters, so a
        # request with (say) a latin-1 high byte in the Authorization header
        # used to surface as a 500 from an unhandled exception instead of a
        # clean 401. Bytes have no such restriction, so a garbage header is
        # simply an unauthorized header.
        headers = dict(scope.get("headers") or [])
        raw_auth = headers.get(b"authorization") or b""
        if not isinstance(raw_auth, (bytes, bytearray)):
            raw_auth = str(raw_auth).encode("utf-8", errors="replace")
        scheme, _, presented = bytes(raw_auth).partition(b" ")
        authorized = scheme.lower() == b"bearer" and hmac.compare_digest(
            presented, self._expected_token
        )

        if not authorized:
            from starlette.responses import JSONResponse

            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


async def _serve_with_pool_cleanup(serve: Callable[[], Awaitable[None]]) -> None:
    """Run ``serve`` and always drain pooled HTTP clients when it returns.

    Pooled ``httpx.AsyncClient`` objects are bound to the serving event loop
    (see ``hpe_networking_mcp.pipeline.clients.pooled_clients``); once that
    loop closes they can no longer be awaited and leak sockets until GC.
    Draining them in ``finally`` here covers graceful shutdown *and* the
    in-process restart case (a supervisor driving ``run_server`` again on a
    fresh loop), where undrained clients would otherwise pile up unclosable.
    """
    try:
        await serve()
    finally:
        await aclose_pooled_clients()


async def _serve_streamable_http_with_bearer(
    mcp_instance: Any,
    token: str,
    host: str,
    port: int,
    transport_security: "TransportSecuritySettings | None",
) -> None:
    """Serve ``mcp_instance``'s streamable-HTTP app behind a bearer check.

    Mirrors ``MCPServer.run_streamable_http_async`` (installed SDK) but wraps
    the built ASGI app with :class:`BearerAuthASGIMiddleware` first, since
    the SDK has no first-class hook to inject an arbitrary ASGI middleware
    around the app it builds internally. ``host``/``port``/``transport_security``
    are threaded in explicitly rather than read off ``mcp_instance.settings``
    -- the installed SDK's ``Settings`` model carries none of those fields
    (see ``_configure_http_transport``), so the caller (``run_server``) is
    the single source of truth for the resolved bind address and allow-list.
    """
    import uvicorn

    app = mcp_instance.streamable_http_app(host=host, transport_security=transport_security)
    # Keep the Starlette application as Uvicorn's top-level app so its MCP
    # lifespan/session-manager context is detected and driven correctly.
    app.add_middleware(BearerAuthASGIMiddleware, token=token)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=mcp_instance.settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    await _serve_with_pool_cleanup(server.serve)


# ---------------------------------------------------------------------------
# Standalone backend write gate
# ---------------------------------------------------------------------------
#
# The unified router (src/hpe_networking_mcp/mcp_servers/tool_router.py) enforces the per-platform
# write gates itself before dispatching into a backend. A backend module run
# *standalone* -- ``python -m hpe_networking_mcp.mcp_servers.glp``, or any of the entries in
# .cursor/mcp.dev.json -- bypasses the router entirely, so until now the only
# thing standing between a client and a GLP/Central write was whatever the
# individual tool happened to check. That made the gate's coverage depend on
# how the server was launched, which is exactly the sort of asymmetry a
# safety gate must not have.
#
# ``install_platform_write_gate`` closes that gap at the same seam
# ``install_middleware`` uses (ToolManager.call_tool), keyed off the MCP
# server's own name and each tool's published annotations, so it needs no
# per-tool registration and cannot drift from the tool list.

#: MCP server name -> platform key in :data:`_PLATFORM_WRITE_GATES`. Server
#: names are the strings passed to ``MCPServer(...)`` in each backend module.
_SERVER_NAME_PLATFORMS: dict[str, str] = {
    "central-config": "central",
    "central-monitoring": "central",
    "central-nac": "central",
    "central-ops": "central",
    "central-generated": "central",
    "glp-core": "glp",
    "clearpass-core": "clearpass",
    "mist-core": "mist",
    "apstra-core": "apstra",
    "aos8-core": "aos8",
    "edgeconnect-core": "edgeconnect",
    "uxi-core": "uxi",
    "axis-core": "axis",
}

_WRITE_GATE_INSTALLED_ATTR = "_hpe_mcp_write_gate_original"


def platform_for_server_name(server_name: str | None) -> str | None:
    """Resolve an MCP server name to a gated platform key, or ``None``.

    Falls back to the ``<product>-core`` / ``central-<x>`` naming convention so
    a newly added optional-product or Central backend is gated by default
    rather than silently ungated. Returns ``None`` for anything that is not a
    known write gate (e.g. ``rag-core``, ``hpe-networking-mcp``).
    """
    if not server_name:
        return None
    name = str(server_name).strip().lower()
    platform = _SERVER_NAME_PLATFORMS.get(name)
    if platform is None:
        platform = "central" if name.startswith("central-") else name.removesuffix("-core")
    return platform if platform in _PLATFORM_WRITE_GATES else None


def tool_write_capability(tool: Any) -> str:
    """Normalized capability of an MCPServer tool from its annotations.

    Mirrors ``hpe_networking_mcp.mcp_servers.tool_router._tool_capability`` so the standalone
    gate and the router gate classify a tool identically.
    """
    annotations = getattr(tool, "annotations", None)
    if bool(getattr(annotations, "read_only_hint", False)):
        return "read"
    if bool(getattr(annotations, "destructive_hint", False)):
        return "destructive"
    if annotations == DIAGNOSTIC:
        return "diagnostic"
    return "write"


def install_platform_write_gate(mcp_instance: Any) -> bool:
    """Enforce aggregate and platform write gates on every backend tool call.

    Intercepts the server's tool dispatcher (via
    :func:`hpe_networking_mcp.mcp_servers._sdk_compat.set_dispatcher`) so any
    tool whose annotations classify it as ``write``/``destructive`` is refused
    *before* the tool body runs whenever either (a) an aggregate read-only gate
    is active -- returning :func:`global_write_blocked` -- or (b) the server's
    platform gate is disabled -- returning
    :func:`platform_write_blocked`. Read-only and diagnostic tools are never
    affected. Aggregate read-only mode also protects standalone servers that
    have no platform gate. The router itself remains untouched because it
    enforces both gates after resolving the backend tool; wrapping its generic
    destructive dispatcher would also block diagnostic calls.

    Coverage. The dispatcher is the single choke point every call path funnels
    through, so the gate applies to all three of them: a ``tools/call`` arriving
    over a transport (the SDK's handler delegates to ``MCPServer.call_tool``,
    which delegates here), a direct in-process ``server.call_tool(name, ...)``
    by name, and the router's raw dispatch
    (:func:`hpe_networking_mcp.mcp_servers._sdk_compat.call_tool_raw`). Note
    that the SDK's ``ServerMiddleware`` chain is *not* an alternative position:
    it observes inbound wire messages only, and backend servers here are
    imported and called in-process, so a middleware-tier gate would see none of
    this traffic.

    Install order. ``install_middleware`` runs first (each backend's own
    ``run_server`` does it), then ``shared.run_server`` installs this gate
    outermost. Re-installing *this* gate is safe -- it replaces its own wrapper
    rather than stacking. Re-installing the middleware chain afterwards is not,
    and now raises: it would rebuild from a snapshot taken before this gate
    existed and drop it. Install each interceptor exactly once, outermost last.

    Deny-by-default, resolved in the same three tiers the router uses so both
    paths refuse exactly the same calls:

    1. the server's platform has a registered gate -> that gate decides;
    2. no registered gate but a known optional product (``design-core`` is one)
       -> the shared ``HPE_MCP_PRODUCT_ACCESS`` fallback decides;
    3. neither -> refuse via :func:`ungated_backend_write_blocked`.

    Tier 3 must deny rather than allow. The alternative is that adding a
    backend, or adding a write tool to a today-read-only one, silently ships an
    ungated write -- and unlike tiers 1 and 2 there is no setting an operator
    could use to notice, because none applies.

    Returns:
        ``True`` if a gate was installed (or refreshed), ``False`` only for the
        router itself, which enforces both gates after resolving the backend
        tool.
    """
    server_name = str(getattr(mcp_instance, "name", "")).strip().lower()
    if server_name == "hpe-networking-mcp":
        return False
    validate_access_profile_environment()
    platform = platform_for_server_name(server_name)
    optional_product = platform is None and server_name in OPTIONAL_PRODUCT_SERVER_NAMES

    original = _sdk_compat.claim_dispatcher(mcp_instance, _WRITE_GATE_INSTALLED_ATTR)

    async def gated_call_tool(
        name: str,
        arguments: dict[str, Any] | None,
        context: Any = None,
        convert_result: bool = False,
    ) -> Any:
        tool = _sdk_compat.get_tool(mcp_instance, name)
        if tool is not None:
            capability = tool_write_capability(tool)
            if capability in ("write", "destructive"):
                if global_readonly_enabled():
                    blocked: dict[str, Any] | None = global_write_blocked(name)
                elif platform is not None:
                    blocked = (
                        None
                        if platform_writes_allowed(platform)
                        else platform_write_blocked(platform, name, capability=capability)
                    )
                elif optional_product:
                    blocked = (
                        None
                        if optional_product_writes_allowed()
                        else optional_product_write_blocked(name)
                    )
                else:
                    blocked = ungated_backend_write_blocked(
                        server_name or None,
                        name,
                        capability=capability,
                    )
            else:
                blocked = None
            if blocked is not None:
                if convert_result:
                    try:
                        return tool.fn_metadata.convert_result(blocked)
                    except Exception:
                        from mcp.server.mcpserver.utilities.func_metadata import (
                            _convert_to_content,
                        )

                        return _convert_to_content(blocked)
                return blocked
        return await original(
            name, arguments, context=context, convert_result=convert_result
        )

    _sdk_compat.set_dispatcher(mcp_instance, gated_call_tool, _WRITE_GATE_INSTALLED_ATTR)
    return True


def run_server(mcp_instance: Any, default_port: int | None = None) -> None:
    """Run an MCP server with transport configured by environment.

    MCP_TRANSPORT: 'stdio' (default) or 'streamable-http'
    MCP_HOST: bind address (default 127.0.0.1)
    MCP_PORT: port (default 8010, or default_port if provided)
    MCP_ALLOWED_HOSTS / MCP_ALLOWED_ORIGINS: comma-separated DNS rebinding allowlists
      -- required (non-wildcard) once MCP_HOST is not loopback; see
      ``_configure_http_transport``.
    MCP_HTTP_BEARER_TOKEN: optional shared secret; when set, every HTTP path
      except /livez, /readyz, /healthz requires ``Authorization: Bearer <token>``.
    HPE_MCP_METRICS_HTTP: optional; when explicitly truthy, also registers
      ``GET /metrics`` (a bounded JSON snapshot of in-process metrics -- see
      ``hpe_networking_mcp.mcp_servers._middleware.metrics``) under the same auth/allow-list
      protections as every other HTTP route here. Collection itself is a
      separate opt-in (``HPE_MCP_METRICS=1``); with only the HTTP flag
      set, the route responds with ``{"enabled": false}``.

    Always registers /livez, /readyz, /healthz on HTTP transports -- see
    ``_register_health_routes``.

    Also installs this server's platform write gate (see
    ``install_platform_write_gate``) on every transport, so a backend run
    standalone fails write/destructive calls closed exactly like the router
    does. This is idempotent and a no-op for servers with no gated platform.

    Every transport is served through ``_serve_with_pool_cleanup``, so pooled
    per-platform HTTP clients (``pooled_clients``) are closed on the serving
    loop when the server exits instead of leaking sockets until GC.
    """
    validate_access_profile_environment()
    install_platform_write_gate(mcp_instance)
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        import anyio

        # ``MCPServer.run()`` is a thin ``anyio.run(self.run_stdio_async)``;
        # calling the async entry point through ``_serve_with_pool_cleanup``
        # drains pooled HTTP clients on the serving loop at exit instead of
        # leaking them to GC.
        anyio.run(_serve_with_pool_cleanup, mcp_instance.run_stdio_async)
        return

    host = os.environ.get("MCP_HOST", "127.0.0.1")
    fallback_port = default_port if default_port is not None else DEFAULT_HTTP_PORT
    port = int(os.environ.get("MCP_PORT", str(fallback_port)))
    # Installed SDK 2.x's MCPServer.Settings carries no host/port/
    # transport_security fields any more -- both must be threaded explicitly
    # into run()/streamable_http_app() rather than assigned onto settings.
    transport_security = _configure_http_transport(host, port)
    _register_health_routes(mcp_instance)
    if _env_bool("HPE_MCP_METRICS_HTTP", False):
        _register_metrics_route(mcp_instance)

    bearer_token = _http_bearer_token()
    if bearer_token is not None and transport != "streamable-http":
        raise UnsafeHttpBindingError(
            f"MCP_HTTP_BEARER_TOKEN is set but MCP_TRANSPORT={transport!r} cannot "
            "enforce the bearer-auth wrapper. Use MCP_TRANSPORT=streamable-http "
            "or unset MCP_HTTP_BEARER_TOKEN; refusing to start an HTTP listener "
            "that appears protected but is not."
        )
    if bearer_token is None:
        import anyio

        async def _serve_http() -> None:
            # Mirror ``MCPServer.run(transport=...)``'s dispatch (it is just
            # ``anyio.run(lambda: self.run_<transport>_async(**kwargs))``)
            # with the same kwargs, so pool cleanup can run on the serving
            # loop. Unknown transports fail exactly as the SDK's ``run``.
            if transport == "streamable-http":
                await mcp_instance.run_streamable_http_async(
                    host=host,
                    port=port,
                    transport_security=transport_security,
                )
            elif transport == "sse":
                await mcp_instance.run_sse_async(
                    host=host,
                    port=port,
                    transport_security=transport_security,
                )
            else:
                raise ValueError(f"Unknown transport: {transport}")

        anyio.run(_serve_with_pool_cleanup, _serve_http)
        return

    import anyio

    anyio.run(
        _serve_streamable_http_with_bearer,
        mcp_instance,
        bearer_token,
        host,
        port,
        transport_security,
    )


# ---------------------------------------------------------------------------
# Lazy-initialised clients
# ---------------------------------------------------------------------------

_central_client: CentralClient | None = None
_mcp_client: MCPClient | None = None
_glp_client: GLPClient | None = None
_ENCODED_PATH_RESERVED = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)
_ENCODED_PATH_DELIMITERS = re.compile(r"%(?:23|3f)", re.IGNORECASE)


def get_client() -> CentralClient:
    global _central_client
    if _central_client is None:
        creds_path = os.environ.get("CREDS_PATH", "config/credentials.yaml")
        source_ctx, _ = build_account_contexts(creds_path)
        tm = TokenManager(
            client_id=source_ctx.client_id,
            client_secret=source_ctx.client_secret,
            cache_context=f"{source_ctx.base_url}|{source_ctx.glp_workspace_id}",
            cache_key="source",
        )
        _central_client = CentralClient(base_url=source_ctx.base_url, token_manager=tm)
    return _central_client


def get_mcp_client() -> MCPClient:
    global _mcp_client
    if _mcp_client is None:
        _mcp_client = MCPClient(get_client())
    return _mcp_client


def get_glp_client() -> GLPClient:
    global _glp_client
    if _glp_client is None:
        creds_path = os.environ.get("CREDS_PATH", "config/credentials.yaml")
        _, target_ctx = build_account_contexts(creds_path)
        # GLP tokens live only ~15 min, so use a smaller refresh buffer than
        # the Central default (300s would burn a third of every window).
        tm = TokenManager(
            client_id=target_ctx.client_id,
            client_secret=target_ctx.client_secret,
            token_url=target_ctx.glp_token_url,
            cache_context=f"{target_ctx.glp_base_url}|{target_ctx.glp_workspace_id}",
            cache_key="glp",
            expiry_buffer=60,
        )
        _glp_client = GLPClient(
            token_manager=tm,
            workspace_id=target_ctx.glp_workspace_id,
            base_url=target_ctx.glp_base_url,
        )
    return _glp_client


# ---------------------------------------------------------------------------
# Async troubleshooting helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Network-troubleshooting API version selection
# ---------------------------------------------------------------------------
#
# Central's network-troubleshooting API moved its stable surface to
# ``/network-troubleshooting/v1``; ``v1alpha1`` is the legacy path some
# tenants (older Central instances, or ones pinned during a staged rollout)
# still require. Module-level constants below resolve to v1 by default so
# existing imports (``ops.py`` etc.) upgrade automatically, while
# ``HPE_MCP_TROUBLESHOOTING_API_VERSION`` lets an operator pin back to
# v1alpha1 for tenants that need it -- no shared.py edit required.
#
# ``troubleshooting_endpoint_candidates()`` is the reusable helper: it
# returns an ordered list of endpoint paths (preferred version first) for a
# device-type segment/serial/action. Pass that list -- instead of a single
# hardcoded path -- to ``atroubleshoot_async()`` and it will automatically
# fall back to the next candidate on a 404 (this Central tenant doesn't
# serve that version), collapsing "try v1, fall back to v1alpha1" into one
# call for any domain tool module that adopts it.
_TROUBLESHOOTING_API_VERSION_ENV = "HPE_MCP_TROUBLESHOOTING_API_VERSION"
_TROUBLESHOOTING_DEFAULT_ORDER: tuple[str, ...] = ("v1", "v1alpha1")
_TROUBLESHOOTING_KNOWN_VERSIONS = {"v1", "v1alpha1"}


def troubleshooting_version_order() -> tuple[str, ...]:
    """Ordered (preferred-first) network-troubleshooting API versions.

    Honors ``HPE_MCP_TROUBLESHOOTING_API_VERSION``:
    - unset / "v1" / "current": v1 first, v1alpha1 fallback (default).
    - "v1alpha1" / "legacy": v1alpha1 first, v1 fallback -- for tenants
      still pinned to the legacy API during a staged rollout.
    """
    override = os.environ.get(_TROUBLESHOOTING_API_VERSION_ENV, "").strip().lower()
    if override in ("v1alpha1", "legacy"):
        return ("v1alpha1", "v1")
    if override in ("v1", "current", ""):
        return _TROUBLESHOOTING_DEFAULT_ORDER
    logging.getLogger(__name__).warning(
        "Unrecognized %s=%r; using default order %r",
        _TROUBLESHOOTING_API_VERSION_ENV,
        override,
        _TROUBLESHOOTING_DEFAULT_ORDER,
    )
    return _TROUBLESHOOTING_DEFAULT_ORDER


def troubleshooting_base(segment: str) -> str:
    """Preferred-version troubleshooting base path for a device-type segment
    (e.g. ``"cx"``, ``"aos-s"``, ``"gateways"``, ``"aps"``)."""
    return f"/network-troubleshooting/{troubleshooting_version_order()[0]}/{segment}"


def troubleshooting_endpoint_candidates(
    segment: str, serial_number: str, action: str
) -> list[str]:
    """Ordered candidate troubleshooting endpoints -- preferred version
    first, explicit fallback version(s) after -- for a device-type segment,
    serial number, and action.

    Domain tool modules should build endpoints with this instead of
    hardcoding ``/network-troubleshooting/v1alpha1/...`` (or ``v1``)
    directly, then pass the returned list to ``atroubleshoot_async`` so a
    404 on the preferred version automatically retries the fallback
    version -- no further shared.py changes needed to adopt it.
    """
    def path_segment(value: str, label: str) -> str:
        normalized = str(value).strip()
        if (
            not normalized
            or len(normalized) > 128
            or normalized in {".", ".."}
            or any(
                char in "/\\?#%"
                or char.isspace()
                or ord(char) < 0x20
                or ord(char) == 0x7F
                for char in normalized
            )
        ):
            raise ValueError(f"{label} contains invalid URL path characters")
        return quote(normalized, safe="-._~")

    safe_segment = path_segment(segment, "segment")
    safe_serial = path_segment(serial_number, "serial_number")
    safe_action = path_segment(action, "action")
    seen: list[str] = []
    for version in troubleshooting_version_order():
        path = (
            f"/network-troubleshooting/{version}/"
            f"{safe_segment}/{safe_serial}/{safe_action}"
        )
        if path not in seen:
            seen.append(path)
    return seen


_CX_TROUBLESHOOTING_BASE = troubleshooting_base("cx")
_AOS_S_BASE = troubleshooting_base("aos-s")
_GATEWAY_BASE = troubleshooting_base("gateways")
# Explicit legacy-version bases, for domain code that wants v1alpha1
# specifically (e.g. building a manual fallback list without the
# candidates helper above).
_CX_TROUBLESHOOTING_BASE_V1ALPHA1 = "/network-troubleshooting/v1alpha1/cx"
_AOS_S_BASE_V1ALPHA1 = "/network-troubleshooting/v1alpha1/aos-s"
_GATEWAY_BASE_V1ALPHA1 = "/network-troubleshooting/v1alpha1/gateways"
_POLL_INTERVAL = 5
_POLL_MAX = 12
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200


def clamp_limit(limit: int | None, default: int = DEFAULT_LIST_LIMIT) -> int:
    """Clamp list/read limits to a safe, bounded range."""
    if limit is None:
        return default
    return max(1, min(limit, MAX_LIST_LIMIT))


def _truncate_text(value: Any, max_chars: int = 240) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... [truncated {len(text) - max_chars} chars]"


def compact_http_error(resp: Any, endpoint: str | None = None, max_chars: int = 240) -> str:
    """Return a compact HTTP error message with bounded payload preview."""
    body = response_payload(resp)
    where = f" at {endpoint}" if endpoint else ""
    return f"HTTP {resp.status_code}{where}: {_truncate_text(body, max_chars=max_chars)}"


class WriteResultError(RuntimeError):
    """Raised when a write result indicates the target rejected the change.

    Central write paths are inconsistent about how they report failure: some
    tools call `client.post`/`.put`/`.patch`/`.delete` (which already raise
    `httpx.HTTPStatusError` on a non-2xx status via `raise_for_status()`),
    others call `client._request(...)` directly (which explicitly does NOT
    raise -- "any other status: return immediately, callers decide") and
    return the raw response/payload regardless of status, and a few attach a
    best-effort `errors` list to an otherwise-2xx envelope. A caller that only
    checks "did this raise?" will silently treat a rejected write as applied.
    """


def validate_write_result(result: Any, *, context: str = "") -> Any:
    """Fail closed on a non-2xx/error write result; return `result` unchanged.

    Handles, in order:
      * raw HTTP response-like objects (anything with a `status_code`
        attribute, e.g. `httpx.Response` or a test double) -- checked via
        `.is_success` when present, else `200 <= status_code < 300`;
      * response envelopes (`Mapping`) carrying a non-empty `errors` list/str/dict
        or a non-empty `error` string;
      * response envelopes with an explicit `success`/`ok` field set to
        `False`;
      * response envelopes with a `status` field of `failed`/`failure`/
        `error`, or a `status_code` field outside the 2xx range (the shape
        `resp_json()` falls back to for a non-JSON body).

    Never rejects a legitimate empty success body: `None`, `{}`, `[]`, or a
    plain string with none of the above failure markers all pass through
    unchanged -- this is intentionally narrow so it never produces a false
    failure on a real 2xx/empty-body success.
    """
    where = f"{context}: " if context else ""
    status_code = getattr(result, "status_code", None)
    if status_code is not None:
        is_success = getattr(result, "is_success", None)
        if not isinstance(is_success, bool):
            try:
                is_success = 200 <= int(status_code) < 300
            except (TypeError, ValueError):
                is_success = False
        if not is_success:
            raise WriteResultError(f"{where}{compact_http_error(result)}")
        return result
    if isinstance(result, Mapping):
        errors = result.get("errors")
        if isinstance(errors, (list, tuple)) and any(
            item not in (None, "", [], {}) for item in errors
        ):
            raise WriteResultError(f"{where}write reported errors: {list(errors)}")
        if isinstance(errors, str) and errors.strip():
            raise WriteResultError(f"{where}write reported error: {errors}")
        if isinstance(errors, Mapping) and len(errors) > 0:
            raise WriteResultError(f"{where}write reported errors: {dict(errors)}")
        error = result.get("error")
        if isinstance(error, str) and error.strip():
            raise WriteResultError(f"{where}write reported error: {error}")
        for flag_name in ("success", "ok"):
            if result.get(flag_name) is False:
                raise WriteResultError(f"{where}write reported {flag_name}=False")
        status_field = result.get("status")
        if (
            isinstance(status_field, str)
            and status_field.strip().lower() in {"failed", "failure", "error"}
        ):
            raise WriteResultError(f"{where}write reported status={status_field!r}")
        result_status_code = result.get("status_code")
        if isinstance(result_status_code, int) and not (200 <= result_status_code < 300):
            raise WriteResultError(
                f"{where}write reported status_code={result_status_code}"
            )
    return result


def response_payload(resp: Any) -> Any:
    """Return JSON response content, falling back to text only for non-JSON bodies."""
    try:
        return resp.json()
    except ValueError:
        return resp.text


def bounded_response_payload(resp: Any, *, max_bytes: int = 131_072) -> Any:
    """Return JSON, bounded text, or bounded base64 metadata for an HTTP response."""
    raw = getattr(resp, "content", None)
    if raw is None:
        raw = str(getattr(resp, "text", "")).encode("utf-8", errors="replace")
    elif not isinstance(raw, bytes):
        raw = bytes(raw)

    headers = getattr(resp, "headers", {}) or {}
    content_type = str(headers.get("content-type", "")).split(";", 1)[0].strip().lower()
    size = len(raw)
    try:
        payload = resp.json()
    except (TypeError, ValueError):
        payload = None
    else:
        encoded = json.dumps(payload, ensure_ascii=False, default=str).encode()
        if len(encoded) <= max_bytes:
            return payload
        collection_page = bound_collection_response(
            payload,
            limit=DEFAULT_LIST_LIMIT,
            offset=0,
        )
        if collection_page is not payload:
            page_encoded = json.dumps(
                collection_page, ensure_ascii=False, default=str
            ).encode()
            if len(page_encoded) <= max_bytes and isinstance(collection_page, dict):
                collection_page["_response_bounds"] = {
                    "content_type": "application/json",
                    "size_bytes": len(encoded),
                    "truncated": True,
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                }
                return collection_page
        raw = encoded
        size = len(raw)
        content_type = "application/json"

    preview = raw[:max_bytes]
    truncated = size > len(preview)
    if content_type == "application/json" or content_type.endswith("+json"):
        return {
            "content_type": content_type,
            "size_bytes": size,
            "truncated": truncated,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "text": preview.decode("utf-8", errors="replace"),
        }
    if content_type.startswith("text/") or content_type in {
        "application/xml",
        "application/yaml",
        "application/x-yaml",
    }:
        return {
            "content_type": content_type or "text/plain",
            "size_bytes": size,
            "truncated": truncated,
            "text": preview.decode("utf-8", errors="replace"),
        }
    return {
        "content_type": content_type or "application/octet-stream",
        "size_bytes": size,
        "truncated": truncated,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "base64": base64.b64encode(preview).decode("ascii"),
    }


def safe_api_path(path: str, allowed_prefixes: tuple[str, ...]) -> str:
    """Validate a user-supplied API path before appending it to an authenticated host."""
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError(
            "path must be a relative API path without scheme, host, query, or fragment"
        )
    if _ENCODED_PATH_DELIMITERS.search(parsed.path):
        raise ValueError("path must not contain encoded query or fragment delimiters")
    if _ENCODED_PATH_RESERVED.search(parsed.path):
        raise ValueError("path must not contain encoded dot, slash, or backslash characters")
    decoded = unquote(parsed.path)
    if _ENCODED_PATH_DELIMITERS.search(decoded) or "?" in decoded or "#" in decoded:
        raise ValueError("path must not contain encoded query or fragment delimiters")
    if _ENCODED_PATH_RESERVED.search(decoded):
        raise ValueError("path must not contain double-encoded dot, slash, or backslash characters")
    if "\\" in decoded:
        raise ValueError("path must not contain backslashes")
    if any(segment in (".", "..") for segment in decoded.split("/")):
        raise ValueError("path must not contain dot segments")
    if not decoded.startswith(allowed_prefixes):
        allowed = ", ".join(f"{prefix}*" for prefix in allowed_prefixes)
        raise ValueError(f"path must begin with one of: {allowed}")
    return decoded


#: ``_pagination`` members this module computes and therefore owns. Anything
#: else found in an incoming ``_pagination`` block belongs to the backend
#: (most importantly an opaque ``next_cursor``) and is preserved verbatim by
#: :func:`bound_collection_response`.
CANONICAL_PAGINATION_KEYS = frozenset(
    {"offset", "limit", "total", "truncated", "list_key"}
)


def bound_collection_response(
    data: Any,
    *,
    limit: int,
    offset: int = 0,
    list_key: str | None = None,
) -> Any:
    """Slice the primary list in a JSON value to reduce MCP tool output size.

    - If ``data`` is a list, wraps as ``{"items": [...], "_pagination": ...}``.
    - If ``data`` is a dict, slices ``list_key`` or the longest top-level list
      (excluding ``_pagination``) and adds ``_pagination`` metadata.

    Any non-canonical key already present in ``data``'s ``_pagination`` (see
    :data:`CANONICAL_PAGINATION_KEYS`) -- for example a backend-issued
    ``next_cursor`` -- is preserved in the returned ``_pagination``.
    """
    lim = clamp_limit(limit)
    off = max(0, offset)
    if isinstance(data, list):
        total = len(data)
        page = data[off : off + lim]
        return {
            "items": page,
            "_pagination": {
                "offset": off,
                "limit": lim,
                "total": total,
                "truncated": total > off + len(page),
            },
        }
    if not isinstance(data, dict):
        return data
    existing_pagination = data.get("_pagination")
    out = {k: v for k, v in data.items() if k != "_pagination"}
    key = list_key
    if key is None:
        candidates = [(k, len(v)) for k, v in out.items() if isinstance(v, list)]
        if not candidates:
            return data
        key = max(candidates, key=lambda kv: (kv[1], kv[0]))[0]
    val = out.get(key)
    if not isinstance(val, list):
        return data
    total = len(val)
    preserve_existing = (
        off == 0
        and isinstance(existing_pagination, dict)
        and existing_pagination.get("offset", 0) == 0
        and existing_pagination.get("list_key", key) == key
        and isinstance(existing_pagination.get("total"), int)
    )
    if preserve_existing:
        # `preserve_existing` already established the dict-ness and the int
        # `total`; restate it so the narrowing survives into the subscript.
        assert isinstance(existing_pagination, dict)
        total = max(total, int(existing_pagination["total"]))
    page = val[off : off + lim]
    was_truncated = isinstance(existing_pagination, dict) and bool(
        existing_pagination.get("truncated")
    )
    out[key] = page
    pagination: dict[str, Any] = {
        "offset": off,
        "limit": lim,
        "total": total,
        "truncated": total > off + len(page) or was_truncated,
        "list_key": key,
    }
    if isinstance(existing_pagination, dict):
        # Carry through every key this function does not own. Backends that
        # page with an opaque continuation token surface it as a
        # non-canonical ``_pagination`` member (``next_cursor`` / ``next`` /
        # ``has_more`` / ...); rebuilding ``_pagination`` from scratch dropped
        # it silently, which made an upstream-paginated collection look
        # complete and left the caller with no way to fetch the next page.
        # The canonical slice keys are always recomputed here and always win.
        carried = {
            key_name: value
            for key_name, value in existing_pagination.items()
            if key_name not in CANONICAL_PAGINATION_KEYS
        }
        if carried:
            pagination = {**carried, **pagination}
    out["_pagination"] = pagination
    return out


# ---------------------------------------------------------------------------
# Feature flag: bound list tool responses (A3)
# ---------------------------------------------------------------------------
#
# When ``HPE_MCP_BOUND_LISTS`` is set to "1"/"true"/"yes", list tools
# that currently return a raw list[dict] wrap their response in
# ``{"items": [...], "_pagination": {...}}`` via
# bound_collection_response. Default OFF so existing clients that
# memoised the list shape don't break on upgrade. Flip on once
# consumers have moved to the wrapped shape.

_BOUND_LISTS_FLAG = "HPE_MCP_BOUND_LISTS"


def _bound_lists_enabled() -> bool:
    return os.environ.get(_BOUND_LISTS_FLAG, "").lower() in ("1", "true", "yes")


def maybe_bound(
    data: Any,
    *,
    limit: int,
    offset: int = 0,
    list_key: str | None = None,
) -> Any:
    """Wrap ``data`` with bound_collection_response when ``HPE_MCP_BOUND_LISTS``
    is enabled; otherwise return ``data`` unchanged.

    Lets list tools opt callers into the wrapped shape without a
    breaking change. Callers that always want the wrap should call
    ``bound_collection_response`` directly (several tools already do).
    """
    if not _bound_lists_enabled():
        return data
    return bound_collection_response(data, limit=limit, offset=offset, list_key=list_key)


async def atroubleshoot_poll(client: CentralClient, poll_url: str) -> dict[str, Any]:
    """Poll a Central troubleshooting async-operation without blocking the event loop."""
    result: dict[str, Any] = {}
    for _ in range(_POLL_MAX):
        await asyncio.sleep(_POLL_INTERVAL)
        try:
            result = await client.aget(poll_url)
        except Exception as exc:
            return {"status": "ERROR", "error": str(exc)}
        if result.get("status", "") in ("COMPLETED", "FAILED"):
            return result
    return result


def _async_response_location(resp: Any) -> str:
    header: str = resp.headers.get("Location", "")
    if header:
        return header
    try:
        body = resp.json()
    except Exception:
        return ""
    if not isinstance(body, dict):
        return ""
    location = body.get("location") or body.get("Location")
    return location if isinstance(location, str) else ""


async def atroubleshoot_async(
    client: CentralClient,
    endpoint: str | list[str],
    payload: dict[str, Any],
    errors: list[str],
    *,
    diagnostic: bool = False,
) -> dict[str, Any]:
    """Start and poll a Central troubleshooting task without blocking the event loop.

    Args:
        endpoint: a single endpoint path, or an ordered list of candidate
            paths (see ``troubleshooting_endpoint_candidates``). When a
            list is given, a 404 on any candidate except the last is
            treated as "this tenant doesn't serve that API version" and
            the next candidate is tried; a non-404 failure or the last
            candidate's failure is returned immediately.
        diagnostic: allow this explicitly non-mutating troubleshooting POST
            through a closed platform write gate. Destructive actions must
            leave this false so the transport remains a defense-in-depth gate.
    """
    candidates = [endpoint] if isinstance(endpoint, str) else list(endpoint)
    if not candidates:
        errors.append("no troubleshooting endpoint candidates provided")
        return {"status": None, "errors": errors}

    poll_url: str | None = None
    for index, candidate in enumerate(candidates):
        is_last = index == len(candidates) - 1
        try:
            request_kwargs: dict[str, Any] = {"json": payload}
            if diagnostic:
                request_kwargs["diagnostic"] = True
            resp = await client._arequest(
                "POST",
                candidate,
                **request_kwargs,
            )
        except Exception as exc:
            errors.append(str(exc))
            return {"status": None, "errors": errors}
        if resp.status_code == 404 and not is_last:
            logger.info(
                "troubleshooting endpoint %s returned 404; trying fallback candidate",
                candidate,
            )
            continue
        if resp.status_code not in (200, 201, 202):
            errors.append(compact_http_error(resp))
            return {"status": None, "errors": errors}
        location = _async_response_location(resp)
        if not location:
            errors.append("no Location header in async response")
            return {"status": None, "errors": errors}
        task_id = location.rstrip("/").split("/")[-1]
        poll_url = f"{candidate}/async-operations/{task_id}"
        break

    if poll_url is None:
        errors.append("no working troubleshooting endpoint among candidates")
        return {"status": None, "errors": errors}

    result = await atroubleshoot_poll(client, poll_url)
    result["errors"] = errors
    result["endpoint_used"] = candidate
    return result


def resp_json(resp: Any) -> dict[str, Any]:
    """Return resp.json() or compact metadata if the body is not JSON."""
    try:
        body: dict[str, Any] = resp.json()
        return body
    except Exception:
        raw_text = resp.text or ""
        return {
            "status_code": resp.status_code,
            "text_preview": _truncate_text(raw_text),
            "text_length": len(raw_text),
        }


_DTYPE_MAP = {
    "AP": "aps",
    "ACCESS_POINT": "aps",
    "CX": "cx",
    "AOS_CX": "cx",
    "AOS-CX": "cx",
    "AOSCX": "cx",
    "AOS_S": "aos-s",
    "AOS-S": "aos-s",
    "AOSS": "aos-s",
    "GATEWAY": "gateways",
    "GW": "gateways",
}

# Model-series prefixes used to disambiguate generic SWITCH deviceTypes when
# firmware/softwareVersion is unavailable. AOS-CX vs AOS-S.
_CX_MODEL_SERIES = (
    "4100", "6000", "6100", "6200", "6300", "6400",
    "8100", "8320", "8325", "8360", "8400", "9300", "10000",
)
_AOS_S_MODEL_SERIES = ("2530", "2540", "2620", "2920", "2930", "3810", "5400")


def _classify_switch(device: dict[str, Any]) -> str:
    """Classify a generic SWITCH inventory record as 'cx' or 'aos-s'.

    Firmware/softwareVersion prefix is the strongest signal: AOS-CX versions
    look like 'FL.10.x'/'10.x'; AOS-S look like 'WC.16.x'/'KB.16.x'/'YA/YB/RA...'.
    Falls back to the model series, then a conservative default of 'cx' with a
    warning when ambiguous.
    """
    fw = (
        device.get("firmwareVersion")
        or device.get("softwareVersion")
        or device.get("swVersion")
        or ""
    )
    fw_upper = str(fw).upper()
    if fw_upper:
        # Strip any platform prefix like "FL." / "WC." to inspect the version.
        # AOS-CX versions have major "10" (e.g. "FL.10.16" / "10.16"); AOS-S
        # versions have major "16" (e.g. "WC.16.11" / "KB.16.10"). The numeric
        # major is the strongest signal, so check it before the prefix.
        parts = fw_upper.split(".")
        prefix = parts[0]
        # The major version is the first part that is all digits.
        major = next((p for p in parts if p.isdigit()), "")
        if major == "10":
            return "cx"
        if major == "16":
            return "aos-s"
        # No recognisable major: a two-letter alpha platform prefix
        # (YA/YB/RA/...) is AOS-S styling.
        if prefix and len(prefix) == 2 and prefix.isalpha():
            return "aos-s"

    model = str(device.get("model") or device.get("deviceModel") or "")
    if any(series in model for series in _CX_MODEL_SERIES):
        return "cx"
    if any(series in model for series in _AOS_S_MODEL_SERIES):
        return "aos-s"

    logging.getLogger(__name__).warning(
        "Ambiguous switch type for serial=%s (firmware=%r model=%r); defaulting to 'cx'",
        device.get("serialNumber", ""),
        fw,
        model,
    )
    return "cx"


def device_type_for_troubleshoot(serial_number: str, device_type: str | None) -> str | None:
    """Auto-detect device type from inventory if not supplied.

    Returns lowercase URL-ready device type: "aps", "cx", "aos-s", "gateways",
    or None.
    """
    if device_type:
        upper = device_type.upper()
        if upper in _DTYPE_MAP:
            return _DTYPE_MAP[upper]
        # "SWITCH"/"SWITCHES" is the generic deviceType value inventory
        # records use (see list_devices' device_type filter). It is not a
        # valid troubleshooting URL segment on its own, so fall through to
        # inventory-based CX/AOS-S disambiguation instead of passing a literal
        # "switch" that the API would reject.
        if upper not in ("SWITCH", "SWITCHES"):
            return upper.lower()
    device = get_mcp_client().get_device_by_serial(serial_number)
    if not device:
        return None
    raw = device.get("deviceType", "")
    if "ACCESS_POINT" in raw or raw == "AP":
        return "aps"
    if "SWITCH" in raw:
        # SWITCH covers both AOS-CX and AOS-S; disambiguate from the record.
        return _classify_switch(device)
    if "GATEWAY" in raw:
        return "gateways"
    return None
