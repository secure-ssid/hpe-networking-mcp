"""Write-gate enforcement invariants -- offline, no live API calls.

Every assertion here is either a pure signature/annotation contract read off
the registered tool catalog, or a behavioral check whose HTTP boundary is
replaced by a spy that fails the test if a request is ever attempted. Nothing
in this module reaches a real Central/GLP/ClearPass/Mist/UXI endpoint.

The suite pins six invariants:

1. Two-step confirmation. Every write/destructive tool is classified into
   exactly one confirmation mechanism, and the *inventory* of tools per
   mechanism is pinned -- a new write tool cannot silently join the
   "platform-gate only" bucket.
2. Platform write flags. Central/GLP gates resolve from their own env vars
   only; ``HPE_MCP_PRODUCT_ACCESS`` must not bleed into them.
3. ``invoke_read_tool`` never reaches a write/destructive backend tool, and
   cannot be tricked by case/whitespace/alias variants or the generated
   namespace.
4. Guarded generic GET tools are method-locked to GET and path-prefix-guarded.
5. Access-profile x product-access policy matrix, written out as a table.
6. The unit suite's ambient-env neutralization list stays complete.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import os
import re
import textwrap
from functools import lru_cache
from typing import Any, NamedTuple

import pytest
from mcp.server.mcpserver import Context, MCPServer

import hpe_networking_mcp.mcp_servers.tool_router as router
from hpe_networking_mcp.mcp_servers.shared import (
    ACCESS_PROFILE_ENV_VAR,
    DESTRUCTIVE,
    DIAGNOSTIC,
    IDEMPOTENT_WRITE,
    PLATFORM_WRITE_GATE_NAMES,
    READ_ONLY,
    access_profile,
    global_readonly_enabled,
    install_platform_write_gate,
    optional_product_writes_allowed,
    platform_write_gate_state,
    platform_writes_allowed,
    tool_write_capability,
)
from tests.unit.conftest import (
    HPE_MCP_ENV_PREFIX,
    PRESERVED_ENV_VARS,
    WRITE_ACCESS_ENV_VARS,
)

# ---------------------------------------------------------------------------
# Catalog enumeration -- imported directly (not through the router) so the
# inventory is identical no matter which HPE_MCP_TOOLSETS/HPE_MCP_PRODUCTS
# profile the developer happens to have exported.
# ---------------------------------------------------------------------------

BACKEND_MODULES: dict[str, str] = {
    "central-config": "config",
    "central-monitoring": "monitoring",
    "central-nac": "nac",
    "central-ops": "ops",
    "central-generated": "central_generated",
    "glp-core": "glp",
    "clearpass-core": "clearpass",
    "mist-core": "mist",
    "apstra-core": "apstra",
    "aos8-core": "aos8",
    "edgeconnect-core": "edgeconnect",
    "uxi-core": "uxi",
    "axis-core": "axis",
    "design-core": "design",
    "rag-core": "rag",
    "interop-core": "interop",
}

#: Backends whose whole write surface must be uniformly two-step.
OPTIONAL_PRODUCT_SERVERS = (
    "clearpass-core",
    "mist-core",
    "apstra-core",
    "aos8-core",
    "edgeconnect-core",
    "uxi-core",
    "axis-core",
)


class WriteTool(NamedTuple):
    server: str
    name: str
    capability: str
    dry_run_default: Any
    confirm_default: Any
    elicits: bool

    @property
    def mechanism(self) -> str:
        if self.dry_run_default is True and self.confirm_default is False:
            return "dry_run_confirm"
        if self.elicits:
            return "elicited_confirm"
        return "platform_gate_only"


def _param_default(tool: Any, name: str) -> Any:
    """Schema default for ``name``, or the sentinel ``"ABSENT"``."""
    properties = (tool.parameters or {}).get("properties") or {}
    field = properties.get(name)
    if not isinstance(field, dict):
        return "ABSENT"
    return field.get("default")


def _force_register_optin_namespaces() -> None:
    """Register generated namespaces that are opt-in / default-OFF.

    ``glp-core`` keeps its ~906 generated tools behind
    ``HPE_MCP_GLP_GENERATED_TOOLS`` (default OFF), so a plain import of
    ``glp`` registers only the 105 curated tools. Auditing just those would
    leave the entire generated GLP write surface unverified, so we opt in
    explicitly here. ``_register_generated_glp_tools`` is documented as
    idempotent and safe to re-call from tests, which makes this independent
    of whichever module got imported first in the pytest session.
    """
    glp = importlib.import_module("hpe_networking_mcp.mcp_servers.glp")
    previous = os.environ.get("HPE_MCP_GLP_GENERATED_TOOLS")
    os.environ["HPE_MCP_GLP_GENERATED_TOOLS"] = "1"
    try:
        glp._register_generated_glp_tools()
    finally:
        if previous is None:
            os.environ.pop("HPE_MCP_GLP_GENERATED_TOOLS", None)
        else:
            os.environ["HPE_MCP_GLP_GENERATED_TOOLS"] = previous


@lru_cache(maxsize=1)
def write_catalog() -> tuple[WriteTool, ...]:
    """Every registered write/destructive tool across every backend.

    Covers the *complete* catalog -- including default-OFF generated
    namespaces -- so the pinned inventories below cannot be satisfied by a
    partially-registered surface.
    """
    _force_register_optin_namespaces()
    records: list[WriteTool] = []
    for server, module_stem in BACKEND_MODULES.items():
        module = importlib.import_module(f"hpe_networking_mcp.mcp_servers.{module_stem}")
        for name, tool in sorted(module.mcp._tool_manager._tools.items()):
            capability = tool_write_capability(tool)
            if capability not in {"write", "destructive"}:
                continue
            try:
                source = inspect.getsource(tool.fn)
            except (OSError, TypeError):  # pragma: no cover - defensive
                source = ""
            records.append(
                WriteTool(
                    server=server,
                    name=name,
                    capability=capability,
                    dry_run_default=_param_default(tool, "dry_run"),
                    confirm_default=_param_default(tool, "confirm"),
                    elicits="ctx.elicit" in source,
                )
            )
    return tuple(records)


# ---------------------------------------------------------------------------
# Invariant 1 -- two-step confirmation
# ---------------------------------------------------------------------------
#
# Three sanctioned confirmation mechanisms exist in this repo. Anything that
# mutates must use one of them, and which one it uses is pinned below so a new
# write tool cannot quietly land in the weakest bucket.
#
#   dry_run_confirm   `dry_run: bool = True` + `confirm: bool = False`;
#                     executing needs BOTH dry_run=False and confirm=True.
#                     Used by every optional product and every generated tool.
#   elicited_confirm  no dry_run/confirm parameters; the tool body raises an
#                     MCP elicitation the operator must accept, and fails
#                     closed ("CONFIRMATION_UNAVAILABLE") if the client cannot
#                     elicit. Used by the disruptive central-ops actions.
#   platform_gate_only
#                     legacy curated Central/GLP tools that execute on one
#                     call once their platform write gate is open. Compensating
#                     controls: the per-platform gate, the aggregate read-only
#                     gate, and the router exposing them only through
#                     `invoke_tool`, which is itself annotated DESTRUCTIVE so
#                     MCP clients prompt. This inventory is FROZEN -- see
#                     `test_platform_gate_only_inventory_is_pinned`.

ELICITED_CONFIRMATION_TOOLS = frozenset(
    {
        "disconnect_client",
        "gateway_halt",
        "poe_bounce",
        "port_bounce",
        "reboot_ap_swarm",
        "reboot_device",
    }
)

PLATFORM_GATE_ONLY_TOOLS: dict[str, frozenset[str]] = {
    # Local-state only: creates a resumable migration run row in the local
    # state store. Reaches no device API, so there is nothing to preview.
    "aos8-core": frozenset({"aos8_create_migration_run"}),
    "central-config": frozenset(
        {
            "add_devices_to_group",
            "assign_device_to_site",
            "build_bgp_overlay",
            "build_config_checkpoint_policy",
            "build_ospf_overlay",
            "build_overlay_ssid",
            "build_underlay_ssid",
            "build_vrf_overlay",
            "configure_application_experience",
            "configure_high_availability",
            "create_aaa_dot1xauth_profile",
            "create_aaa_macauth_profile",
            "create_allow_all_role",
            "create_config_assignment",
            "create_device_group",
            "create_gw_cluster",
            "create_gw_policy",
            "create_port_profile",
            "create_role",
            "create_site",
            "create_vlan",
            "create_vlan_interface",
            "create_webhook",
            "delete_config_assignment",
            "delete_device_groups",
            "delete_gw_policy",
            "delete_network_profile",
            "delete_overlay_ssid",
            "delete_role",
            "delete_role_acl",
            "delete_underlay_ssid",
            "delete_webhook",
            "enable_telemetry",
            "gateway_config_interface",
            "gateway_config_static_route",
            "gateway_join_cluster",
            "push_aruba_device_profiles",
            "remove_devices_from_group",
            "rotate_webhook_key",
            "set_firmware_compliance",
            "set_hostname",
            "set_network_profile",
            "set_port_auth",
            "trigger_device_upgrade",
            "update_device_settings",
            "update_port_config",
            "update_role",
            "update_ssid",
            "update_webhook",
        }
    ),
    "central-monitoring": frozenset(
        {
            "clear_alerts",
            "create_notification_rule",
            "create_report",
            "defer_alerts",
            "delete_notification_rule",
            "delete_report",
            "delete_report_run",
            "get_report_run_download_link",
            "reactivate_alerts",
            "resync_device_config",
            "set_alert_priority",
            "set_notification_rule_enabled",
            "update_notification_rule",
            "update_report",
        }
    ),
    "central-nac": frozenset(
        {
            "add_mac_registration",
            "add_mpsk_registration",
            "add_visitor",
            "create_aaa_profile",
            "create_auth_server",
            "create_authz_policy",
            "create_dot1x_auth_profile",
            "create_mac_auth_profile",
            "create_server_group",
            "create_static_tag",
            "delete_aaa_profile",
            "delete_auth_profile",
            "delete_auth_server",
            "delete_authz_policy",
            "delete_mac_registration",
            "delete_mpsk_registration",
            "delete_server_group",
            "delete_static_tag",
            "delete_visitor",
            "update_mac_registration",
            "update_mpsk_registration",
            "update_visitor",
        }
    ),
    "central-ops": frozenset(
        {
            "acknowledge_alert",
            "delete_device_notes",
            "update_device_notes",
        }
    ),
    "glp-core": frozenset(
        {
            "add_glp_scope_group_scopes",
            "create_glp_role_assignment",
            "create_glp_scope_group",
            "delete_glp_role_assignment",
            "delete_glp_scope_group",
            "delete_glp_scope_group_scopes",
            "disassociate_glp_user",
            "glp_add_device",
            "glp_add_devices_bulk",
            "glp_add_subscriptions",
            "glp_archive_device",
            "glp_assign_subscription",
            "invite_glp_user",
            "update_glp_auto_subscription_settings",
            "update_glp_role_assignment",
            "update_glp_scope_group",
            "update_glp_user_preferences",
            "update_glp_workspace_contact",
        }
    ),
}


class TestTwoStepConfirmationContract:
    def test_catalog_is_non_trivial(self):
        catalog = write_catalog()
        # Guard against a silently empty enumeration making every assertion
        # below vacuously true.
        assert len(catalog) > 3300
        assert {record.server for record in catalog} >= set(OPTIONAL_PRODUCT_SERVERS)

    def test_catalog_covers_the_optin_generated_glp_namespace(self):
        """Regression guard: the default-OFF GLP namespace must be audited.

        ``HPE_MCP_GLP_GENERATED_TOOLS`` defaults OFF, so a plain import of
        ``glp`` yields only the 105 curated tools and would leave ~420
        generated GLP write tools unverified by every invariant in this
        module. If ``_force_register_optin_namespaces`` ever stops working
        (renamed private helper, changed opt-in flag), coverage would shrink
        silently and the rest of this file would still pass -- so assert the
        expanded surface explicitly.
        """
        glp_writes = [r for r in write_catalog() if r.server == "glp-core"]
        assert len(glp_writes) > 400, (
            "generated GLP write tools are missing from the audit; "
            "opt-in namespace registration regressed"
        )
        # And they must all be two-step, not gate-only.
        gate_only = {r.name for r in glp_writes if r.mechanism == "platform_gate_only"}
        assert gate_only == set(PLATFORM_GATE_ONLY_TOOLS.get("glp-core", frozenset()))

    def test_no_write_tool_defaults_confirm_to_true(self):
        """A confirm flag that defaults on would collapse two steps into one."""
        offenders = [
            record.name
            for record in write_catalog()
            if record.confirm_default is True
        ]
        assert offenders == []

    def test_confirm_parameter_always_implies_dry_run_preview_default(self):
        """`confirm` only ever appears alongside `dry_run` defaulting to True."""
        offenders = [
            (record.server, record.name, record.dry_run_default)
            for record in write_catalog()
            if record.confirm_default != "ABSENT" and record.dry_run_default is not True
        ]
        assert offenders == []

    def test_dry_run_parameter_defaults_to_preview_whenever_confirm_exists(self):
        """No tool offers a one-step `dry_run=False` execution *plus* confirm."""
        offenders = [
            (record.server, record.name)
            for record in write_catalog()
            if record.dry_run_default is False and record.confirm_default is False
        ]
        assert offenders == []

    @pytest.mark.parametrize("server", OPTIONAL_PRODUCT_SERVERS)
    def test_optional_product_write_surface_is_uniformly_two_step(self, server):
        allowed = PLATFORM_GATE_ONLY_TOOLS.get(server, frozenset())
        offenders = sorted(
            record.name
            for record in write_catalog()
            if record.server == server
            and record.mechanism != "dry_run_confirm"
            and record.name not in allowed
        )
        assert offenders == []

    def test_generated_tool_namespace_is_uniformly_two_step(self):
        """Every generated OpenAPI write tool carries dry_run+confirm."""
        generated = [
            record
            for record in write_catalog()
            if record.server == "central-generated"
        ]
        assert len(generated) > 500
        assert all(record.mechanism == "dry_run_confirm" for record in generated)

    def test_platform_gate_only_inventory_is_pinned(self):
        """FROZEN inventory: adding a one-call write tool must fail this test.

        These tools execute on a single call once their platform write gate is
        open. That is the repo's documented legacy Central/GLP posture, not an
        invitation to add more -- a new entry here needs an explicit decision.
        """
        observed: dict[str, set[str]] = {}
        for record in write_catalog():
            if record.mechanism == "platform_gate_only":
                observed.setdefault(record.server, set()).add(record.name)
        expected = {
            server: set(names) for server, names in PLATFORM_GATE_ONLY_TOOLS.items()
        }
        assert observed == expected

    def test_elicited_confirmation_inventory_is_pinned(self):
        observed = {
            record.name
            for record in write_catalog()
            if record.mechanism == "elicited_confirm"
        }
        assert observed == set(ELICITED_CONFIRMATION_TOOLS)

    @pytest.mark.parametrize("tool_name", sorted(ELICITED_CONFIRMATION_TOOLS))
    def test_elicited_confirmation_tools_fail_closed(self, tool_name):
        """No elicitation support => the operation is refused, never performed."""
        import hpe_networking_mcp.mcp_servers.ops as ops

        source = inspect.getsource(getattr(ops, tool_name))
        assert "await ctx.elicit(" in source
        assert "CONFIRMATION_UNAVAILABLE" in source
        assert 'result.action != "accept" or not result.data.confirm' in source
        # The elicit call and its refusal branch strictly precede every call
        # that could issue the write, so a declined/unavailable confirmation
        # returns before anything reaches the device.
        dispatch_starts = [
            source.index(marker)
            for marker in (
                "atroubleshoot_async(",
                "_arequest_troubleshooting(",
                "client.post(",
                "client._arequest(",
            )
            if marker in source
        ]
        assert dispatch_starts, f"{tool_name} issues no recognized write call"
        assert source.index("ctx.elicit") < min(dispatch_starts)
        assert source.index("CONFIRMATION_UNAVAILABLE") < min(dispatch_starts)


class TestNoBackendEscapesAWriteGate:
    """A backend that ships write tools must resolve to a real write gate.

    ``_write_is_enabled`` returns True for any server that is neither a known
    platform nor a registered optional product, so a backend added to
    ``_BACKENDS`` without a ``_SERVER_PLATFORMS``/``_OPTIONAL_BACKENDS`` entry
    would ship completely ungated. Today only the credential-free
    read-only-local backends (rag/interop/design) fall in that bucket, and they
    have zero write tools -- this test keeps it that way.
    """

    def test_every_backend_with_write_tools_resolves_to_a_gate(self):
        """Strict form: the platform must be a *named* write gate.

        ``_dispatch_tool`` only consults the platform gate when
        ``_server_platform(server) in PLATFORM_WRITE_GATE_NAMES``. A backend
        that is merely an "optional product" (e.g. ``design-core``, whose
        platform key ``design`` has no gate) would have its writes filtered at
        catalog-load time but never re-checked at dispatch. Requiring a named
        gate keeps both layers in agreement.
        """
        ungated = sorted(
            server
            for server in {record.server for record in write_catalog()}
            if router._server_platform(server) not in PLATFORM_WRITE_GATE_NAMES
        )
        assert ungated == []

    def test_ungated_backends_ship_no_write_tools(self):
        servers_with_writes = {record.server for record in write_catalog()}
        for server in ("rag-core", "interop-core", "design-core"):
            assert server not in servers_with_writes

    def test_closed_gates_disable_every_writing_backend(self, monkeypatch):
        """With every gate explicitly shut, no backend can dispatch a write."""
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "0")
        monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")
        still_open = sorted(
            server
            for server in {record.server for record in write_catalog()}
            if router._write_is_enabled(server, "destructive")
        )
        assert still_open == []


class TestNoReadOnlyToolCanMutate:
    """A READ_ONLY annotation is what ``invoke_read_tool`` trusts.

    If a tool were annotated read-only but issued a mutating HTTP verb -- in
    its own body or in any same-module helper it calls -- the read-only
    dispatcher would happily execute it. This walks the intra-module call
    graph of every read-annotated tool and asserts none can reach POST/PUT/
    PATCH/DELETE.
    """

    MUTATING_VERB = re.compile(
        r"""\.(?:post|put|patch|delete)\s*\(|"""
        r"""request\(\s*["'](?:POST|PUT|PATCH|DELETE)["']"""
    )

    #: Reviewed exceptions: same-module helpers that issue a non-GET verb which
    #: does not mutate managed infrastructure. Each entry is a deliberate,
    #: audited decision -- the inventory itself is pinned by
    #: ``test_non_get_read_helper_allowlist_is_pinned`` so a new one cannot be
    #: waved through by editing only the walk.
    NON_MUTATING_NON_GET_HELPERS = frozenset(
        {
            # Session/credential acquisition against the product's own auth
            # endpoint. Creates a token, never touches device or fabric state.
            "_aos8_session_login",
            "_apstra_login",
            "_uxi_access_token",
            # Apstra exposes several *query* endpoints as POST (the request body
            # carries the query, not a mutation). Fixed, typed wrapper: its only
            # caller passes a hardcoded /api/blueprints/{id}/... template, and
            # the path still goes through safe_api_path("/api/").
            "_apstra_read_post",
        }
    )

    @staticmethod
    def _same_module_callees(module: Any, fn: Any) -> list[Any]:
        try:
            source = textwrap.dedent(inspect.getsource(fn))
            tree = ast.parse(source)
        except (OSError, TypeError, SyntaxError):  # pragma: no cover - defensive
            return []
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    names.add(func.id)
                elif isinstance(func, ast.Attribute):
                    names.add(func.attr)
        callees = []
        for name in names:
            helper = getattr(module, name, None)
            if callable(helper) and getattr(helper, "__module__", "") == module.__name__:
                callees.append(helper)
        return callees

    @pytest.mark.parametrize("server", sorted(BACKEND_MODULES))
    def test_read_only_tools_never_reach_a_mutating_verb(self, server):
        module = importlib.import_module(
            f"hpe_networking_mcp.mcp_servers.{BACKEND_MODULES[server]}"
        )
        offenders: list[tuple[str, str]] = []
        for name, tool in sorted(module.mcp._tool_manager._tools.items()):
            if tool_write_capability(tool) != "read":
                continue
            seen: set[str] = set()
            stack = [tool.fn]
            while stack:
                fn = stack.pop()
                key = getattr(fn, "__qualname__", repr(fn))
                if key in seen:
                    continue
                seen.add(key)
                try:
                    source = textwrap.dedent(inspect.getsource(fn))
                except (OSError, TypeError):  # pragma: no cover - defensive
                    continue
                if (
                    self.MUTATING_VERB.search(source)
                    and key not in self.NON_MUTATING_NON_GET_HELPERS
                ):
                    offenders.append((name, key))
                    break
                stack.extend(self._same_module_callees(module, fn))
        assert offenders == []

    def test_non_get_read_helper_allowlist_is_pinned(self):
        """Every allowlisted helper still exists and is still non-mutating."""
        located: set[str] = set()
        for stem in ("aos8", "apstra", "uxi"):
            module = importlib.import_module(f"hpe_networking_mcp.mcp_servers.{stem}")
            for helper in self.NON_MUTATING_NON_GET_HELPERS:
                fn = getattr(module, helper, None)
                if fn is None:
                    continue
                located.add(helper)
                source = textwrap.dedent(inspect.getsource(fn))
                # An auth/query helper must never take a caller-chosen verb.
                assert "method" not in inspect.signature(fn).parameters, helper
                # ...and must never reach a DELETE/PUT/PATCH.
                assert not re.search(r"\.(?:put|patch|delete)\s*\(", source), helper
        assert located == set(self.NON_MUTATING_NON_GET_HELPERS)


class TestGlpWritesHaveASecondLayer:
    """GLP writes fail closed at the HTTP-client layer too, not just the gate."""

    def test_client_layer_reads_the_glp_gate_not_product_access(self, monkeypatch):
        from hpe_networking_mcp.pipeline.clients import glp_client

        monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
        assert optional_product_writes_allowed() is True
        assert glp_client._writes_enabled() is False

        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
        assert glp_client._writes_enabled() is False

        monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
        assert glp_client._writes_enabled() is True

    def test_client_write_refuses_before_any_request(self, monkeypatch):
        from unittest.mock import MagicMock

        from hpe_networking_mcp.pipeline.clients.glp_client import GLPClient

        monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
        client = GLPClient.__new__(GLPClient)
        client.workspace_id = "ws"
        client._device_id_cache = {}
        inner = MagicMock()
        inner.patch.side_effect = AssertionError("PATCH issued behind a closed gate")
        inner.post.side_effect = AssertionError("POST issued behind a closed gate")
        inner._request.side_effect = AssertionError("request behind a closed gate")
        client._client = inner

        with pytest.raises(NotImplementedError, match="HPE_MCP_GLP_V2BETA1_WRITES"):
            client.archive_device("SERIAL1")


# ---------------------------------------------------------------------------
# Invariant 1 (behavioral) -- executing needs BOTH dry_run=False and confirm=True
# ---------------------------------------------------------------------------


class _NoHttp:
    """Stand-in for httpx.AsyncClient that fails the test if instantiated."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("network call attempted on a refused write")


def _block_http(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _NoHttp)
    monkeypatch.setattr(httpx, "Client", _NoHttp)


WRITE_CHOKE_POINTS = (
    ("clearpass", "_clearpass_write_request", ("POST", "/api/endpoint")),
    ("mist", "_mist_write_request", ("POST", "/api/v1/sites/abc/devices")),
    ("aos8", "_aos8_write_request", ("POST", "/v1/configuration/object/x")),
    ("uxi", "_uxi_write_request", ("POST", "/groups")),
)


class TestExecutionRequiresBothSteps:
    @pytest.mark.parametrize(("module_stem", "func_name", "call"), WRITE_CHOKE_POINTS)
    def test_write_helper_refuses_without_confirm_and_makes_no_request(
        self, module_stem, func_name, call, monkeypatch
    ):
        monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
        monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cppm.example.com")
        monkeypatch.setenv("CLEARPASS_API_TOKEN", "t")
        monkeypatch.setenv("MIST_HOST", "https://api.mist.example.com")
        monkeypatch.setenv("MIST_API_TOKEN", "t")
        monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
        monkeypatch.setenv("AOS8_API_TOKEN", "t")
        monkeypatch.setenv("UXI_CLIENT_ID", "id")
        monkeypatch.setenv("UXI_CLIENT_SECRET", "secret")
        _block_http(monkeypatch)
        module = importlib.import_module(f"hpe_networking_mcp.mcp_servers.{module_stem}")
        func = getattr(module, func_name)
        method, path = call

        # Step 1 only (the default): a preview, never a request.
        preview = asyncio.run(func(method, path, body={"x": 1}))
        assert preview["dry_run"] is True
        assert "error" not in preview

        # dry_run=False alone is NOT enough.
        refused = asyncio.run(func(method, path, body={"x": 1}, dry_run=False))
        assert refused["error"] == "confirm=True is required when dry_run=False."
        assert refused["dry_run"] is True

        # confirm=True alone is NOT enough either -- dry_run still previews.
        confirmed_only = asyncio.run(func(method, path, body={"x": 1}, confirm=True))
        assert confirmed_only["dry_run"] is True
        assert "error" not in confirmed_only

    def test_generated_write_executor_refuses_without_confirm(self, monkeypatch):
        import hpe_networking_mcp.mcp_servers.clearpass as clearpass

        monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
        monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cppm.example.com")
        monkeypatch.setenv("CLEARPASS_API_TOKEN", "t")
        _block_http(monkeypatch)

        out = asyncio.run(
            clearpass._clearpass_generated_write(
                "clearpass_delete_endpoint",
                "DELETE",
                "/endpoint/1",
                {},
                {},
                None,
                "application/json",
                False,  # dry_run
                False,  # confirm
            )
        )

        assert out["error"] == "confirm=True is required when dry_run=False."
        assert out["dry_run"] is True

    def test_generated_write_executor_blocked_when_product_access_read_only(
        self, monkeypatch
    ):
        import hpe_networking_mcp.mcp_servers.clearpass as clearpass

        monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")
        _block_http(monkeypatch)

        out = asyncio.run(
            clearpass._clearpass_generated_write(
                "clearpass_delete_endpoint",
                "DELETE",
                "/endpoint/1",
                {},
                {},
                None,
                "application/json",
                False,
                True,  # confirm=True must not matter when the gate is shut
            )
        )

        assert out["status"] == "blocked"


# ---------------------------------------------------------------------------
# Invariant 2 -- platform write flags, and no bleed from product access
# ---------------------------------------------------------------------------


def _gated_server(name: str) -> MCPServer:
    srv = MCPServer(name)

    @srv.tool(annotations=READ_ONLY)
    def read_probe() -> dict[str, Any]:
        return {"ok": True}

    @srv.tool(annotations=IDEMPOTENT_WRITE)
    def write_probe() -> dict[str, Any]:
        raise AssertionError("write body executed behind a closed gate")

    @srv.tool(annotations=DESTRUCTIVE)
    def destroy_probe() -> dict[str, Any]:
        raise AssertionError("destructive body executed behind a closed gate")

    return srv


def _call(server: MCPServer, name: str, arguments: dict[str, Any] | None = None) -> Any:
    return asyncio.run(server._tool_manager.call_tool(name, arguments or {}))


class TestPlatformWriteFlagsDefaults:
    def test_glp_writes_fail_closed_with_no_env_set(self):
        state = platform_write_gate_state("glp")
        assert state == {
            "env_var": "HPE_MCP_GLP_V2BETA1_WRITES",
            "state": "disabled",
            "enabled": False,
            "source": "platform_default",
        }
        assert platform_writes_allowed("glp") is False

    def test_product_access_read_write_does_not_unlock_glp(self, monkeypatch):
        """The user's real .env posture: product access open, platform gates shut."""
        monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")

        assert optional_product_writes_allowed() is True
        state = platform_write_gate_state("glp")
        assert state["enabled"] is False
        # Resolution must come from GLP's own default, never the shared toggle.
        assert state["source"] == "platform_default"

    def test_product_access_read_write_does_not_change_central_resolution(
        self, monkeypatch
    ):
        before = platform_write_gate_state("central")
        monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
        after = platform_write_gate_state("central")

        assert before == after
        assert after["source"] == "platform_default"

    def test_product_access_read_only_does_not_shut_central(self, monkeypatch):
        """The axes are independent in both directions."""
        monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-only")

        assert optional_product_writes_allowed() is False
        assert platform_writes_allowed("central") is True

    def test_central_documented_default_is_open_and_opt_out_works(self, monkeypatch):
        assert platform_writes_allowed("central") is True
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "0")
        assert platform_writes_allowed("central") is False

    @pytest.mark.parametrize("platform", sorted(PLATFORM_WRITE_GATE_NAMES))
    def test_no_platform_gate_is_open_by_shared_toggle_alone_when_disabled(
        self, platform, monkeypatch
    ):
        """With everything unset, only Central is open; nothing else is."""
        expected_open = platform == "central"
        assert platform_writes_allowed(platform) is expected_open


class TestStandaloneGateBlocksBeforeToolBody:
    def test_glp_backend_gate_blocks_writes_by_default(self):
        server = _gated_server("glp-core")
        assert install_platform_write_gate(server) is True

        blocked_write = _call(server, "write_probe")
        blocked_destroy = _call(server, "destroy_probe")

        for blocked in (blocked_write, blocked_destroy):
            assert blocked["status"] == "blocked"
            assert blocked["platform"] == "glp"
            assert "HPE_MCP_GLP_V2BETA1_WRITES" in blocked["error"]
        # Reads are untouched.
        assert _call(server, "read_probe") == {"ok": True}

    def test_product_access_read_write_cannot_open_the_glp_gate(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
        server = _gated_server("glp-core")
        install_platform_write_gate(server)

        blocked = _call(server, "destroy_probe")

        assert blocked["status"] == "blocked"
        assert blocked["platform"] == "glp"

    @pytest.mark.parametrize(
        ("server_name", "platform", "env_var"),
        [
            ("mist-core", "mist", "HPE_MCP_MIST_WRITES"),
            ("clearpass-core", "clearpass", "HPE_MCP_CLEARPASS_WRITES"),
            ("uxi-core", "uxi", "HPE_MCP_UXI_WRITES"),
        ],
    )
    def test_optional_backend_gate_blocks_before_the_tool_body(
        self, server_name, platform, env_var
    ):
        server = _gated_server(server_name)
        install_platform_write_gate(server)

        blocked = _call(server, "destroy_probe")

        assert blocked["status"] == "blocked"
        assert blocked["platform"] == platform

    def test_aggregate_read_only_overrides_an_open_platform_gate(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
        monkeypatch.setenv("HPE_MCP_READONLY", "1")
        server = _gated_server("central-config")
        install_platform_write_gate(server)

        blocked = _call(server, "write_probe")

        assert blocked["status"] == "blocked"
        assert "HPE_MCP_READONLY" in blocked["error"]


# ---------------------------------------------------------------------------
# Invariant 3 -- invoke_read_tool can never reach a write/destructive tool
# ---------------------------------------------------------------------------


class _SpyBackend:
    """Minimal MCPServer stand-in whose call_tool must never be reached."""

    def __init__(self, tools: dict[str, Any]) -> None:
        self.calls: list[str] = []
        outer = self

        class _Manager:
            async def call_tool(self, name, arguments, context=None, convert_result=False):  # noqa: ANN001,ANN202
                outer.calls.append(name)
                return {"reached_backend": name}

            def get_tool(self, name):  # noqa: ANN001,ANN202
                return tools.get(name)

        self._tool_manager = _Manager()


@pytest.fixture
def spy_router(monkeypatch):
    """Wire the router at a fake backend with one tool per capability."""
    srv = MCPServer("central-nac")

    @srv.tool(annotations=READ_ONLY)
    def read_probe(value: int = 1) -> dict[str, Any]:
        return {"cap": "read"}

    @srv.tool(annotations=IDEMPOTENT_WRITE)
    def write_probe(value: int = 1) -> dict[str, Any]:
        return {"cap": "write"}

    @srv.tool(annotations=DESTRUCTIVE)
    def destroy_probe(value: int = 1) -> dict[str, Any]:
        return {"cap": "destructive"}

    @srv.tool()
    async def diag_probe(ctx: Context, value: int = 1) -> dict[str, Any]:
        return {"cap": "diagnostic"}

    srv._tool_manager._tools["diag_probe"].annotations = DIAGNOSTIC

    tools = dict(srv._tool_manager._tools)
    backend = _SpyBackend(tools)
    monkeypatch.setattr(router, "_tool_index", tools, raising=True)
    monkeypatch.setattr(router, "_tool_servers", {n: backend for n in tools}, raising=True)
    monkeypatch.setattr(
        router, "_tool_backend_names", {n: "central-nac" for n in tools}, raising=True
    )
    monkeypatch.setattr(router, "_load_all_backends", lambda: None, raising=True)
    return backend


def _read_dispatch(name: str, arguments: dict[str, Any] | None = None) -> Any:
    ctx = Context(mcp_server=router.mcp)
    return asyncio.run(router.invoke_read_tool(ctx, name, arguments))


class TestReadOnlyDispatcherCannotReachWrites:
    @pytest.mark.parametrize("name", ["write_probe", "destroy_probe"])
    def test_refuses_write_capability_before_any_backend_call(self, spy_router, name):
        out = _read_dispatch(name, {"value": 1})

        assert out["status"] == "blocked"
        assert out["tool"] == name
        assert "not read-only" in out["error"]
        assert spy_router.calls == []

    def test_refuses_diagnostic_tools_too(self, spy_router):
        out = _read_dispatch("diag_probe", {})

        assert out["status"] == "blocked"
        assert spy_router.calls == []

    def test_allows_read_tools(self, spy_router):
        out = _read_dispatch("read_probe", {"value": 2})

        assert spy_router.calls == ["read_probe"]
        assert out["reached_backend"] == "read_probe"

    @pytest.mark.parametrize(
        "variant",
        [
            "DESTROY_PROBE",
            "Destroy_Probe",
            " destroy_probe",
            "destroy_probe ",
            "\tdestroy_probe\n",
            "destroy_probe\u200b",
            "destroy-probe",
            "central-nac.destroy_probe",
            "destroy_probe()",
            "destroy_probe%00",
        ],
    )
    def test_name_variants_never_resolve_to_a_write_tool(self, spy_router, variant):
        out = _read_dispatch(variant, {})

        assert out.get("status") != "ok"
        assert "error" in out
        assert out.get("reached_backend") is None
        assert spy_router.calls == []

    def test_write_dispatcher_still_reaches_the_backend(self, spy_router, monkeypatch):
        """Control: the refusal above is the read gate, not a broken fixture."""
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
        ctx = Context(mcp_server=router.mcp)

        out = asyncio.run(router.invoke_tool(ctx, "destroy_probe", {}))

        assert spy_router.calls == ["destroy_probe"]
        assert out["reached_backend"] == "destroy_probe"

    def test_shared_read_dispatch_path_refuses_writes(self, spy_router):
        """``_dispatch_read_tool`` is the single seam both read entry points use.

        ``invoke_read_tool`` and ``invoke_read_tool_batch`` both call it
        directly, so proving the seam refuses writes proves it for the batch
        fan-out too -- including in ``minimal`` router mode, where the batch
        tool is not registered at all.
        """

        async def scenario() -> list[Any]:
            ctx = Context(mcp_server=router.mcp)
            return [
                await router._dispatch_read_tool(ctx, "read_probe", {}),
                await router._dispatch_read_tool(ctx, "write_probe", {}),
                await router._dispatch_read_tool(ctx, "destroy_probe", {}),
            ]

        read_out, write_out, destroy_out = asyncio.run(scenario())

        assert read_out["reached_backend"] == "read_probe"
        assert write_out["status"] == "blocked"
        assert destroy_out["status"] == "blocked"
        assert spy_router.calls == ["read_probe"]

    def test_batch_read_dispatcher_refuses_writes_too(self, spy_router):
        batch = getattr(router, "invoke_read_tool_batch", None)
        if batch is None:  # minimal router mode registers no batch tool
            return
        ctx = Context(mcp_server=router.mcp)

        out = asyncio.run(
            batch(
                ctx,
                [
                    {"name": "read_probe", "arguments": {}},
                    {"name": "destroy_probe", "arguments": {}},
                ],
            )
        )

        statuses = [item["status"] for item in out["results"]]
        assert statuses == ["ok", "blocked"]
        assert spy_router.calls == ["read_probe"]

    def test_refusal_never_reaches_the_http_client_of_a_real_backend(
        self, monkeypatch
    ):
        """End-to-end: a real destructive Central tool, spied at the HTTP seam.

        ``central-nac``'s ``delete_authz_policy`` is one of the pinned
        platform-gate-only tools, and Central's gate is open by default -- so
        this proves the *read-only dispatcher* is what stops it, not the gate.
        """
        nac = importlib.import_module("hpe_networking_mcp.mcp_servers.nac")

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("HTTP client constructed for a refused read call")

        monkeypatch.setattr(nac, "get_client", _boom)
        assert platform_writes_allowed("central") is True

        tools = dict(nac.mcp._tool_manager._tools)
        assert tool_write_capability(tools["delete_authz_policy"]) == "destructive"

        monkeypatch.setattr(router, "_tool_index", tools, raising=True)
        monkeypatch.setattr(
            router, "_tool_servers", {n: nac.mcp for n in tools}, raising=True
        )
        monkeypatch.setattr(
            router, "_tool_backend_names", {n: "central-nac" for n in tools}, raising=True
        )
        monkeypatch.setattr(router, "_load_all_backends", lambda: None, raising=True)

        out = _read_dispatch("delete_authz_policy", {"policy_name": "x"})

        assert out["status"] == "blocked"
        assert "not read-only" in out["error"]

    def test_router_read_dispatcher_refuses_a_real_generated_destructive_tool(self):
        """The generated namespace is not a side door."""
        generated = importlib.import_module(
            "hpe_networking_mcp.mcp_servers.central_generated"
        )
        destructive = sorted(
            name
            for name, tool in generated.mcp._tool_manager._tools.items()
            if tool_write_capability(tool) == "destructive"
        )
        assert destructive, "no generated destructive tools to test against"
        tools = dict(generated.mcp._tool_manager._tools)
        backend = _SpyBackend(tools)

        async def scenario() -> Any:
            ctx = Context(mcp_server=router.mcp)
            return await router._dispatch_read_tool(ctx, destructive[0], {})

        original_index = dict(router._tool_index)
        try:
            router._tool_index.clear()
            router._tool_index.update(tools)
            router._tool_servers.update({n: backend for n in tools})
            router._tool_backend_names.update({n: "central-generated" for n in tools})
            out = asyncio.run(scenario())
        finally:
            router._tool_index.clear()
            router._tool_index.update(original_index)

        assert out["status"] == "blocked"
        assert "not read-only" in out["error"]
        assert backend.calls == []

    def test_invoke_read_tool_is_annotated_read_only(self):
        tool = router.mcp._tool_manager._tools["invoke_read_tool"]
        assert tool.annotations.read_only_hint is True
        assert router.mcp._tool_manager._tools["invoke_tool"].annotations.destructive_hint is True


# ---------------------------------------------------------------------------
# Invariant 4 -- guarded generic GET tools are method- and path-locked
# ---------------------------------------------------------------------------

HOSTILE_PATHS = (
    "../../etc/passwd",
    "/api/../../secrets",
    "/api/v1/../../secrets",
    "https://evil.example.com/api/v1/self",
    "http://evil.example.com/api/self",
    "//evil.example.com/api/v1/self",
    "/api/v1/self?unredacted=true",
    "/api/v1/self#frag",
    "/api/v1/%2e%2e/%2e%2e/secrets",
    "\\\\evil.example.com\\share",
    "/api/v1/self\\..\\secrets",
)


def _configure_products(monkeypatch) -> None:
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://cppm.example.com")
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "t")
    monkeypatch.setenv("MIST_HOST", "https://api.mist.example.com")
    monkeypatch.setenv("MIST_API_TOKEN", "t")
    monkeypatch.setenv("APSTRA_BASE_URL", "https://apstra.example.com")
    monkeypatch.setenv("APSTRA_API_TOKEN", "t")
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "t")
    monkeypatch.setenv("EDGECONNECT_BASE_URL", "https://orch.example.com")
    monkeypatch.setenv("EDGECONNECT_API_TOKEN", "t")
    monkeypatch.setenv("UXI_CLIENT_ID", "id")
    monkeypatch.setenv("UXI_CLIENT_SECRET", "secret")


ASYNC_GUARDED_GETS = (
    ("clearpass", "clearpass_get"),
    ("mist", "mist_get"),
    ("apstra", "apstra_get"),
    ("aos8", "aos8_get"),
    ("edgeconnect", "edgeconnect_get"),
    ("uxi", "uxi_get"),
)


class TestGuardedGetToolsAreLocked:
    @pytest.mark.parametrize(("module_stem", "tool_name"), ASYNC_GUARDED_GETS)
    @pytest.mark.parametrize("path", HOSTILE_PATHS)
    def test_async_guarded_get_rejects_hostile_paths_without_a_request(
        self, module_stem, tool_name, path, monkeypatch
    ):
        _configure_products(monkeypatch)
        _block_http(monkeypatch)
        module = importlib.import_module(f"hpe_networking_mcp.mcp_servers.{module_stem}")

        out = asyncio.run(getattr(module, tool_name)(path))

        assert isinstance(out, dict)
        assert "error" in out or out.get("errors"), out

    @pytest.mark.parametrize("path", HOSTILE_PATHS)
    def test_glp_get_rejects_hostile_paths_without_a_request(self, path, monkeypatch):
        import hpe_networking_mcp.mcp_servers.glp as glp

        def _boom():
            raise AssertionError("GLP client built for a rejected path")

        monkeypatch.setattr(glp, "get_glp_client", _boom)

        out = glp.glp_get(path)

        assert "error" in out
        assert "Invalid path" in out["error"]

    @pytest.mark.parametrize("path", HOSTILE_PATHS)
    def test_central_get_rejects_hostile_paths_without_a_request(
        self, path, monkeypatch
    ):
        import hpe_networking_mcp.mcp_servers.monitoring as monitoring

        def _boom():
            raise AssertionError("Central client built for a rejected path")

        monkeypatch.setattr(monitoring, "get_client", _boom)

        out = monitoring.central_get(path)

        assert "error" in out
        assert "Invalid path" in out["error"]

    def test_glp_get_is_method_locked_to_get(self, monkeypatch):
        import hpe_networking_mcp.mcp_servers.glp as glp

        seen: list[tuple[str, str]] = []

        class _Inner:
            def get(self, path, params=None):  # noqa: ANN001,ANN202
                seen.append(("GET", path))
                return {"items": []}

            def _request(self, method, path, **kwargs):  # noqa: ANN001,ANN202
                raise AssertionError(f"non-GET verb {method} reached the GLP client")

        class _Client:
            _client = _Inner()

        monkeypatch.setattr(glp, "get_glp_client", lambda: _Client())

        glp.glp_get("/devices/")

        assert seen == [("GET", "/devices/")]

    def test_central_get_is_method_locked_to_get(self, monkeypatch):
        import hpe_networking_mcp.mcp_servers.monitoring as monitoring

        seen: list[tuple[str, str]] = []

        class _Client:
            def get(self, path, params=None):  # noqa: ANN001,ANN202
                seen.append(("GET", path))
                return {"items": []}

            def post(self, *args, **kwargs):  # noqa: ANN002,ANN003,ANN202
                raise AssertionError("POST reached the Central client via central_get")

            def delete(self, *args, **kwargs):  # noqa: ANN002,ANN003,ANN202
                raise AssertionError("DELETE reached the Central client via central_get")

        monkeypatch.setattr(monitoring, "get_client", lambda: _Client())

        monitoring.central_get("/network-monitoring/v1/devices")

        assert seen == [("GET", "/network-monitoring/v1/devices")]

    @pytest.mark.parametrize(("module_stem", "tool_name"), ASYNC_GUARDED_GETS)
    def test_async_guarded_get_uses_the_get_verb(
        self, module_stem, tool_name, monkeypatch
    ):
        """Happy path: the only verb the guarded GET can emit is GET."""
        _configure_products(monkeypatch)
        module = importlib.import_module(f"hpe_networking_mcp.mcp_servers.{module_stem}")
        source = inspect.getsource(module)
        # The read helper each of these tools funnels into calls client.get()
        # (or request("GET", ...)) -- never a caller-supplied verb.
        assert "client.get(" in source or 'request("GET"' in source

    def test_guarded_get_tools_are_annotated_read_only(self):
        for module_stem, tool_name in ASYNC_GUARDED_GETS:
            module = importlib.import_module(
                f"hpe_networking_mcp.mcp_servers.{module_stem}"
            )
            tool = module.mcp._tool_manager._tools[tool_name]
            assert tool_write_capability(tool) == "read", tool_name
        import hpe_networking_mcp.mcp_servers.glp as glp
        import hpe_networking_mcp.mcp_servers.monitoring as monitoring

        assert tool_write_capability(glp.mcp._tool_manager._tools["glp_get"]) == "read"
        assert (
            tool_write_capability(monitoring.mcp._tool_manager._tools["central_get"])
            == "read"
        )


# ---------------------------------------------------------------------------
# Invariant 5 -- access-profile x product-access policy matrix
# ---------------------------------------------------------------------------
#
# Columns: platform read / platform write (Central) / platform write (GLP) /
# product read / product write / router invoke_read_tool / router invoke_tool.
# Reads are never gated anywhere, so they are asserted as constants; the write
# columns are the policy that must not drift.

ACCESS_MATRIX = [
    # (profile, product_access, central_write, glp_write, product_write,
    #  aggregate_read_only)
    ("safe-read-only", None, False, False, False, True),
    ("safe-read-only", "read-only", False, False, False, True),
    ("custom", None, True, False, False, False),
    ("custom", "read-only", True, False, False, False),
    ("custom", "read-write", True, False, True, False),
    ("full-read-write", None, True, True, True, False),
    ("full-read-write", "read-write", True, True, True, False),
]


class TestAccessProfileMatrix:
    @pytest.mark.parametrize(
        (
            "profile",
            "product_access",
            "central_write",
            "glp_write",
            "product_write",
            "aggregate_read_only",
        ),
        ACCESS_MATRIX,
    )
    def test_policy_matrix(
        self,
        profile,
        product_access,
        central_write,
        glp_write,
        product_write,
        aggregate_read_only,
        monkeypatch,
    ):
        monkeypatch.setenv(ACCESS_PROFILE_ENV_VAR, profile)
        if product_access is not None:
            monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", product_access)

        assert access_profile() == profile
        assert global_readonly_enabled() is aggregate_read_only
        assert platform_writes_allowed("central") is central_write
        assert platform_writes_allowed("glp") is glp_write
        assert platform_writes_allowed("mist") is product_write
        assert optional_product_writes_allowed() is product_write

    @pytest.mark.parametrize(
        (
            "profile",
            "product_access",
            "central_write",
            "glp_write",
            "product_write",
            "aggregate_read_only",
        ),
        ACCESS_MATRIX,
    )
    def test_router_dispatchers_follow_the_matrix(
        self,
        spy_router,
        profile,
        product_access,
        central_write,
        glp_write,
        product_write,
        aggregate_read_only,
        monkeypatch,
    ):
        """invoke_read_tool always reads; invoke_tool follows the write column."""
        monkeypatch.setenv(ACCESS_PROFILE_ENV_VAR, profile)
        if product_access is not None:
            monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", product_access)

        # Reads are allowed under every cell of the matrix.
        read_out = _read_dispatch("read_probe", {})
        assert read_out["reached_backend"] == "read_probe"

        # invoke_read_tool refuses writes under every cell of the matrix.
        assert _read_dispatch("destroy_probe", {})["status"] == "blocked"

        # The fake backend is registered as central-nac -> Central's gate.
        spy_router.calls.clear()
        ctx = Context(mcp_server=router.mcp)
        write_out = asyncio.run(router.invoke_tool(ctx, "destroy_probe", {}))
        if central_write and not aggregate_read_only:
            assert spy_router.calls == ["destroy_probe"]
        else:
            assert spy_router.calls == []
            assert write_out["status"] == "blocked"


# ---------------------------------------------------------------------------
# Invariant 6 -- ambient env neutralization stays complete
# ---------------------------------------------------------------------------


class TestAmbientEnvNeutralization:
    def test_every_platform_gate_env_var_is_neutralized(self):
        missing = [
            platform_write_gate_state(platform)["env_var"]
            for platform in PLATFORM_WRITE_GATE_NAMES
            if platform_write_gate_state(platform)["env_var"]
            not in WRITE_ACCESS_ENV_VARS
        ]
        assert missing == []

    def test_aggregate_selectors_are_neutralized(self):
        assert {
            ACCESS_PROFILE_ENV_VAR,
            "HPE_MCP_PRODUCT_ACCESS",
            "HPE_MCP_READONLY",
        } <= set(WRITE_ACCESS_ENV_VARS)

    def test_prefix_scan_covers_every_registration_and_gating_knob(self):
        """The scrub is a prefix scan, so every documented knob is covered.

        Enumerated explicitly because these are the ones with a *known* history
        of leaking from a developer .env and silently changing results.
        """
        known_leakers = {
            "HPE_MCP_ROUTER_MODE",       # deregisters router convenience wrappers
            "HPE_MCP_ACCESS_PROFILE",    # aggregate write posture
            "HPE_MCP_PRODUCT_ACCESS",    # optional-product write toggle
            "HPE_MCP_PRODUCTS",          # which optional backends load
            "HPE_MCP_TOOLSETS",          # which toolsets load
            "HPE_MCP_READONLY",          # emergency kill switch
            "HPE_MCP_ALLOW_LOCAL_PRODUCT_URLS",
            "HPE_MCP_BOUND_LISTS",
            "HPE_MCP_RAG_BACKEND",
            *(
                platform_write_gate_state(platform)["env_var"]
                for platform in PLATFORM_WRITE_GATE_NAMES
            ),
        }
        not_covered = {
            name
            for name in known_leakers
            if not name.startswith(HPE_MCP_ENV_PREFIX) or name in PRESERVED_ENV_VARS
        }
        assert not_covered == set()

    def test_no_hpe_mcp_env_leaks_into_a_test(self):
        """Nothing outside the justified opt-out list survives into a test."""
        leaked = {
            name for name in os.environ if name.startswith(HPE_MCP_ENV_PREFIX)
        } - set(PRESERVED_ENV_VARS)
        assert leaked == set()

    def test_preserved_optout_list_stays_minimal_and_justified(self):
        """Every opt-out is a deliberate, reviewed exception -- not a dumping ground."""
        assert PRESERVED_ENV_VARS == frozenset({"HPE_MCP_ALLOW_PLACEHOLDER_URLS"})

    def test_router_mode_is_neutral_so_wrappers_register(self):
        """Regression: `HPE_MCP_ROUTER_MODE=minimal` in a .env deregistered wrappers.

        The router reads the mode at *import* time, so this can only be
        guaranteed by the conftest's import-time scrub, not by a fixture.
        """
        assert os.environ.get("HPE_MCP_ROUTER_MODE") is None
        assert router._ROUTER_MODE == "default"

    def test_suite_starts_from_the_documented_shipped_default(self):
        """Whatever the developer's shell exports, tests see stock defaults."""
        assert access_profile() == "custom"
        assert global_readonly_enabled() is False
        assert optional_product_writes_allowed() is False
        assert platform_writes_allowed("glp") is False
        assert platform_writes_allowed("central") is True
