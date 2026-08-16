"""Reusable MCP prompts for common Central/GLP workflows.

Prompts are guidance templates, not tools: they add no API calls and do not
increase the router tool list. They steer clients toward the low-token router
pattern (`find_tool` -> `invoke_read_tool` for reads, `invoke_tool` for writes)
and the compact RAG tools.
"""

from __future__ import annotations

from collections.abc import Collection

from mcp.server.mcpserver import MCPServer

#: Prompts that instruct the model to call ``aos8_*`` tools by name. They are
#: useless (and actively misleading) when the AOS 8 backend is not enabled,
#: because every tool they name is absent from the tool list.
AOS8_PROMPT_NAMES = ("aos8_migration_readiness", "aos8_staged_migration_plan")

#: Backend server name that must be enabled for AOS8_PROMPT_NAMES to register.
AOS8_BACKEND_SERVER = "aos8-core"

#: Prompts that name design/diagram export tools. Skipped unless design-core is
#: enabled (HPE_MCP_PRODUCTS=design or TOOLSETS includes design).
DESIGN_PROMPT_NAMES = ("network_design_diagram",)

#: Backend server name required for DESIGN_PROMPT_NAMES.
DESIGN_BACKEND_SERVER = "design-core"

#: ClearPass-oriented prompts (need clearpass-core tools in the catalog).
CLEARPASS_PROMPT_NAMES = ("clearpass_policy_review",)
CLEARPASS_BACKEND_SERVER = "clearpass-core"

#: Mist-oriented prompts (need mist-core tools in the catalog).
MIST_PROMPT_NAMES = ("mist_scope_audit",)
MIST_BACKEND_SERVER = "mist-core"

#: UXI-oriented prompts (need uxi-core tools in the catalog).
UXI_PROMPT_NAMES = ("uxi_diagnostics",)
UXI_BACKEND_SERVER = "uxi-core"

#: Core operator prompts always registered (Central/GLP/interop/rag path).
CORE_OPERATOR_PROMPT_NAMES = (
    "morning_report",
    "central_scope_resolve",
    "central_scope_audit",
    "wlan_sync_check",
    "cross_platform_rf_check",
)


def register_router_prompts(
    mcp: MCPServer, enabled_backends: Collection[str] | None = None
) -> None:
    """Register guided workflows on the unified tool router.

    Args:
        enabled_backends: Optional collection of enabled backend server names
            (the keys of ``tool_router._BACKENDS``). When supplied, prompts
            that depend on a backend which is not enabled are skipped --
            AOS8, design, ClearPass, Mist, and UXI prompt groups. When
            omitted (the default), every prompt is registered, preserving
            the previous behavior for any caller that does not know its backend
            set.
    """
    enabled = None if enabled_backends is None else set(enabled_backends)
    aos8_enabled = enabled is None or AOS8_BACKEND_SERVER in enabled
    design_enabled = enabled is None or DESIGN_BACKEND_SERVER in enabled
    clearpass_enabled = enabled is None or CLEARPASS_BACKEND_SERVER in enabled
    mist_enabled = enabled is None or MIST_BACKEND_SERVER in enabled
    uxi_enabled = enabled is None or UXI_BACKEND_SERVER in enabled

    @mcp.prompt(
        name="network_health_overview",
        description="Summarize tenant/site health, active alerts, and worst affected scopes.",
    )
    def network_health_overview() -> str:
        return """Create a concise Aruba Central health overview.

Use `find_tool` and `invoke_read_tool` for read-only checks rather than guessing direct tool names.
Workflow:
1. Find and call tools for tenant health, sites/client health, and active alerts.
2. Prioritize scopes with health below 80, critical alerts, or many offline devices.
3. Drill into the top 3 worst scopes with scope/device helpers if available.
4. Return a short table: scope/site, health, critical alerts, likely cause, next action.
Keep raw payloads out of the answer unless a detail is needed for troubleshooting."""

    @mcp.prompt(
        name="troubleshoot_site",
        description="Investigate health, alerts, devices, and likely causes for one site.",
    )
    def troubleshoot_site(site_name: str) -> str:
        return f"""Troubleshoot Aruba Central site `{site_name}`.

Use `find_tool` and `invoke_read_tool` for read-only checks.
Workflow:
1. Find the site/scope by name and capture its scope ID.
2. Pull site/client health, active alerts, and devices for that scope.
3. Separate AP, switch, gateway, and client symptoms.
4. If config-health tools are available, check affected devices for config issues.
5. Summarize probable root cause, impacted devices/clients, and safe next actions.
Do not run destructive tools unless the user explicitly asks and confirms."""

    @mcp.prompt(
        name="client_connectivity_check",
        description="Investigate one client by MAC/name/IP and correlate AP/site symptoms.",
    )
    def client_connectivity_check(client_query: str) -> str:
        return f"""Investigate client connectivity for `{client_query}`.

Use `find_tool` and `invoke_read_tool` for read-only checks.
Workflow:
1. Find the client by MAC, IP, hostname, or username.
2. Identify connected/last-connected AP, site/scope, status, RSSI/SNR,
   VLAN/SSID, and last seen time.
3. Check active alerts and site health for the same scope.
4. Check the AP/device health and radios if available.
5. If event tools are available, inspect events around last seen time +/- 2 hours.
Return: current state, likely failure domain, evidence, and next safe troubleshooting step."""

    @mcp.prompt(
        name="investigate_device_events",
        description="Investigate one device's recent events and related health indicators.",
    )
    def investigate_device_events(serial_number: str, time_range: str = "last 2 hours") -> str:
        return f"""Investigate recent events for device `{serial_number}` over `{time_range}`.

Use `find_tool` and `invoke_read_tool` for read-only checks.
Workflow:
1. Find the device and note site/scope, device type, firmware, and status.
2. Pull device health/details and recent alerts for its scope.
3. Find event or audit tools and query the requested window.
4. Group events by severity/category and identify repeated or correlated failures.
5. Recommend next read-only checks first; avoid destructive actions unless explicitly requested."""

    @mcp.prompt(
        name="compare_site_health",
        description="Compare multiple sites and rank them by health/risk.",
    )
    def compare_site_health(site_names: str) -> str:
        return f"""Compare Aruba Central health for these comma-separated sites: `{site_names}`.

Use `find_tool` and `invoke_read_tool` for read-only checks.
Workflow:
1. Resolve each site/scope name.
2. Pull site/client health, active alert counts, and device counts for each.
3. Normalize results into one table sorted worst-first.
4. Highlight outliers: high client failures, critical alerts, offline infrastructure, config drift.
5. End with the top 3 recommended follow-up investigations."""

    @mcp.prompt(
        name="critical_alerts_review",
        description="Review active critical/high alerts and group them by category and scope.",
    )
    def critical_alerts_review() -> str:
        return """Review active critical/high Aruba Central alerts.

Use `find_tool` and `invoke_read_tool` for read-only checks.
Workflow:
1. Pull active alerts, filtering to critical/high severity or priority when possible.
2. Group alerts by site/scope, category, and impacted device type.
3. For the top groups, pull scope/device context to avoid listing isolated noise.
4. Return a compact action board: alert group, impacted scope, count,
   first seen, likely owner, next action.
Do not clear/defer/reactivate alerts unless the user explicitly asks and confirms."""

    @mcp.prompt(
        name="failed_clients_investigation",
        description="Investigate failed clients at a site and correlate to infrastructure.",
    )
    def failed_clients_investigation(site_name: str) -> str:
        return f"""Investigate failed clients at site `{site_name}`.

Use `find_tool` and `invoke_read_tool` for read-only checks.
Workflow:
1. Resolve the site/scope.
2. Pull client/site health and identify failed or unhealthy clients.
3. Group failed clients by SSID, VLAN, band, AP, and failure reason when available.
4. Check the top 5 implicated APs/devices for health, radio, and alert signals.
5. Return probable pattern, supporting evidence, and the safest next checks."""

    @mcp.prompt(
        name="explain_how_it_works",
        description=(
            "Answer how-to / concept questions via RAG first "
            "(ask_docs, lookup_api), never from model memory alone."
        ),
    )
    def explain_how_it_works(topic: str) -> str:
        return f"""Explain how `{topic}` works in HPE Networking / Aruba / GreenLake / Mist.

Always use MCP tools. Do **not** answer from model memory when RAG or API specs exist.

Router pattern (minimal mode):
1. `find_tool` for the need (e.g. "ask docs", "lookup api", "list skills").
2. `invoke_read_tool` on the chosen tool.

RAG-first order:
1. Exact API / schema / enum / endpoint questions → `lookup_api` first.
2. How-to, concepts, design guidance → `ask_docs` (preferred) or `search_docs`.
3. Optional: `list_skills` / `load_skill` when a runbook matches the topic.
4. Cite `file_path` / sources from tool results. If tools return empty, say so
   and only then give a cautious generic answer labeled as uncited.

Live device/tenant state is **not** a RAG question — use monitoring tools after
`find_tool`. Config writes still require explicit user confirmation and dry_run.

Return: short cited explanation, key caveats, and the safest next MCP call."""

    @mcp.prompt(
        name="morning_report",
        description=(
            "Last-24h ops digest across enabled platforms "
            "(engineer detail or executive summary)."
        ),
    )
    def morning_report(mode: str = "engineer") -> str:
        return f"""Build a morning operations report (mode hint: `{mode}`).

Prefer the bundled runbook:
1. `find_tool` / `list_skills` then `invoke_read_tool("load_skill", {{"name": "morning-report"}})`.
2. Follow that skill exactly with `find_tool` + `invoke_read_tool` only.

If skills are unavailable, approximate the same read-only flow:
1. Tenant/site health and worst scopes.
2. Active critical/high alerts (grouped).
3. Recent audit activity (bounded).
4. Optional Mist alarms/SLE, UXI sensor status, GLP reporting failures — only
   when those tools exist; skip missing products with a one-line note.
5. Lead with GREEN/YELLOW/RED. Engineer mode = structured sections; executive
   mode = short business language without tool names.
Never clear alerts or run destructive tools."""

    @mcp.prompt(
        name="central_scope_resolve",
        description="Resolve a Central site/group/global name to scope_id metadata.",
    )
    def central_scope_resolve(scope_query: str) -> str:
        return f"""Resolve Central scope reference `{scope_query}`.

Prefer `load_skill(name="central-scope-walker")` via `invoke_read_tool`, then:
1. org-wide/global → `get_global_scope_id`.
2. else `find_scope` / `list_scopes` (and sites/groups helpers if needed).
3. Match exact id, exact name, then unique substring; if ambiguous, list candidates.
Return scope_id, scope_name, type, and any device-count fields. Read-only."""

    @mcp.prompt(
        name="central_scope_audit",
        description="Bounded Central config hygiene audit for scopes, WLANs, roles, auth.",
    )
    def central_scope_audit(scope_name: str = "org-wide") -> str:
        return f"""Run a bounded Aruba Central configuration scope audit for `{scope_name}`.

Prefer `load_skill(name="central-scope-audit")`.
Workflow (read-only):
1. Resolve scope (`central-scope-walker` / `find_scope` / global).
2. Inventory SSIDs/WLANs and scope maps.
3. Sample roles, auth servers/groups, AAA profiles, named VLANs.
4. Sample config assignments when available.
5. Rank findings REGRESSION / DRIFT / INFO. Do not claim a full VSG tree audit —
   this catalog lacks committed/effective scope-tree APIs.
No config writes."""

    @mcp.prompt(
        name="wlan_sync_check",
        description="Compare Central and Mist WLAN/SSID inventories for drift (read-only).",
    )
    def wlan_sync_check(scope_name: str = "org-wide") -> str:
        return f"""Compare WLAN/SSID configuration between Central and Mist for `{scope_name}`.

Prefer `load_skill(name="wlan-sync-validation")`.
Workflow:
1. Confirm which platforms are enabled; partial results if only one side exists.
2. List Central SSIDs/WLANs for the scope; list Mist WLANs for the site/org.
3. Classify: in-sync / drift / Central-only / Mist-only.
4. Optional field mapping via interop `translate_central_wlan_to_mist` /
   `translate_mist_wlan_to_central` — honor translator warnings.
Read-only. Never print PSK values. No WLAN create/delete."""

    @mcp.prompt(
        name="cross_platform_rf_check",
        description="Site RF/channel health using Central and optional Mist assurance tools.",
    )
    def cross_platform_rf_check(site_name: str) -> str:
        return f"""Assess RF/channel health for site `{site_name}`.

Prefer `load_skill(name="cross-platform-rf-check")`.
Workflow (read-only):
1. Resolve the site on Central and/or Mist.
2. Central: sample AP radios, channel utilization, air quality, neighbors/rogues.
3. Mist (if enabled): site SLE/assurance snapshot and alarms.
4. Summarize per-band pressure and AP outliers. Do not change channels or power.
If Mist channel-planning APIs are absent, say so — do not invent planner data."""

    if clearpass_enabled:

        @mcp.prompt(
            name="clearpass_policy_review",
            description=(
                "Review ClearPass services, enforcement policies, roles, "
                "and recent auth failures. Requires clearpass backend."
            ),
        )
        def clearpass_policy_review(service_name: str = "") -> str:
            focus = service_name or "catalog"
            return f"""Review ClearPass policy/service posture (focus: `{focus}`).

Prefer `load_skill(name="clearpass-policy-audit")`.
Workflow (read-only):
1. Confirm ClearPass tools exist (`clearpass_status` / find_tool).
2. List services; detail the named service when provided.
3. List/get enforcement policies and roles (bounded).
4. Sample auth failures / access-tracker sessions.
Refer to services by name in operator output. No disconnect/guest/write calls.
There is no policy-flow Mermaid compiler in this catalog — say so if asked to draw one."""

    if mist_enabled:

        @mcp.prompt(
            name="mist_scope_audit",
            description=(
                "Bounded Mist site/WLAN/assurance audit. Requires mist backend."
            ),
        )
        def mist_scope_audit(site_name: str = "org-wide") -> str:
            return f"""Run a bounded Juniper Mist configuration/assurance audit for `{site_name}`.

Prefer `load_skill(name="mist-scope-audit")`.
Workflow (read-only):
1. `mist_status` / list sites.
2. WLAN inventory for the scope (avoid huge N+1 site walks unless asked).
3. Org inventory sample, alarms, SLE/assurance.
4. Optional NAC tags/portals/IdPs/user MACs when relevant.
Do not claim full template-governance coverage — org RF/WLAN template list tools
are not wrapped here. No Mist writes."""

    if uxi_enabled:

        @mcp.prompt(
            name="uxi_diagnostics",
            description=(
                "UXI sensor/synthetic-test diagnostics with optional "
                "Central/Mist/AOS8 correlation. Requires uxi backend."
            ),
        )
        def uxi_diagnostics(focus: str = "") -> str:
            topic = focus or "unhealthy sensors"
            return f"""Diagnose UXI synthetic sensors/tests (focus: `{topic}`).

Prefer `load_skill(name="uxi-diagnostics")`.
Workflow (read-only):
1. `uxi_status`, list sensors/agents/tests/networks (bounded).
2. Status detail only for unhealthy/offline sensors.
3. Correlate sensor MAC/network/group to Central/Mist/AOS8 only if those
   backends are enabled; skip others with INFO.
4. Verdict GO / DEGRADED / CRITICAL.
Sensor MACs are synthetic — not end-user devices. No UXI assignment writes."""

    if design_enabled:
        @mcp.prompt(
            name="network_design_diagram",
            description=(
                "Draw an editable network topology diagram (Draw.io primary; "
                "optional Graphviz/NeXt). Requires the design backend."
            ),
        )
        def network_design_diagram(
            site_name: str = "",
            prefer: str = "drawio",
        ) -> str:
            return f"""Produce a network design / topology diagram.

Enablement: design tools only exist when HPE_MCP_PRODUCTS includes `design`
(or HPE_MCP_TOOLSETS includes `design`). If find_tool returns nothing useful,
tell the operator to enable the design product and rebuild the tool catalog.

Prefer the skill path when available:
1. `list_skills` (tag/platform filters optional) → `load_skill(name="network-design-diagram")`.
2. Follow that runbook exactly.

Otherwise use `find_tool` + `invoke_read_tool` (all design tools are read-only):
0. If preferences are unstated, ask the operator for format (Draw.io/Graphviz/NeXt),
   icon style (generic vs vendor icons), and target site/scope.
1. If a live site is in scope (`{site_name or "unspecified"}`), resolve the site and call
   monitoring `get_topology` for nodes/links.
2. Or build a structured model: nodes[{{id,label,role,vendor}}], links[{{source,target}}],
   optional groups. Discover roles via `list_diagram_roles_and_vendors`.
3. `validate_diagram_model` on the model before export.
4. Primary export (prefer={prefer}): `drawio_network_design_diagram` with save=true for editable
   Draw.io / diagrams.net XML under outputs/diagrams/.
5. Optional: `export_graphviz_topology` (render_format=svg|png if Graphviz dot is installed).
6. Optional: `export_next_ui_topology` for interactive NeXt UI JSON + HTML stub.
7. Icons: `list_diagram_icons` / `resolve_diagram_icon`; do not invent vendor logo URLs.

Do not confuse diagram export with monitoring `get_topology` alone — topology fetch is an
input; Draw.io/Graphviz/NeXt tools are the diagram outputs.
Return: which export ran, saved paths (if any), and how to open the artifact."""

    if not aos8_enabled:
        return

    @mcp.prompt(
        name="aos8_migration_readiness",
        description="Assess AOS8-to-Central migration readiness, flagging blockers/secrets.",
    )
    def aos8_migration_readiness(config_path: str = "/md") -> str:
        return f"""Assess ArubaOS 8 -> Aruba Central migration readiness for node `{config_path}`.

Prefer `load_skill(name="aos8-migration-readiness")` when skills are available.
Use `find_tool`/`invoke_read_tool` (or the aos8 tool names directly, if known) for every step
below -- this is read-only discovery and planning, never a write.
Workflow:
1. Confirm the AOS8 backend is reachable: call `aos8_status` (and `aos8_login` only if it
   reports no active session).
2. Export source configuration: call `aos8_export_all(config_path="{config_path}")`. Note any
   `warnings` -- a partial export is still usable, but call out which object types failed.
3. Build the deterministic plan: call `aos8_migration_plan(config_path="{config_path}")`. This
   returns `candidates` for both `classic_central` and `new_central`, a per-object `diff`, and a
   `warnings` list for every lossy/unsupported field.
4. Summarize staged readiness: call `aos8_migration_dependency_plan(target_type="new_central",
   migration_plan=<step 3 result>)` (repeat with `target_type="classic_central"` if that target
   is also in scope). Report the `stages` (apply-order tiers) and `summary` counts: `ready`,
   `blocked`, `reference_only`, `requires_secret_input`.
5. Call out every `blocked` candidate's unresolved dependency, and every `reference_only` family
   (currently network-destination aliases, Ethernet ACLs, and IP-classification whitelist rules
   -- normalized and dependency-tracked, but with no automatic target write in this repository;
   they must be recreated manually).
6. Flag every candidate with `requires_secret_input=True` -- a human must supply the actual
   secret at apply time; never invent or guess one.
Return a compact readiness report: overall totals, the blocking dependencies to resolve first,
the reference-only/manual-recreation list, and the safest next read-only step. Do not call
`aos8_preview_migration_run`, `aos8_create_migration_run`, or `aos8_apply_migration_run` unless
the user explicitly asks to proceed."""

    @mcp.prompt(
        name="aos8_staged_migration_plan",
        description="Walk a dependency-ordered AOS8 migration through preview stages before write.",
    )
    def aos8_staged_migration_plan(
        target_type: str = "new_central", config_path: str = "/md"
    ) -> str:
        return f"""Walk through a staged, dependency-ordered ArubaOS 8 -> `{target_type}`
migration for hierarchy node `{config_path}`, preview-only.

Workflow:
1. Build (or reuse) a migration plan: `aos8_migration_plan(config_path="{config_path}")`.
2. Call `aos8_migration_dependency_plan(target_type="{target_type}", migration_plan=<plan>)` to
   get ordered `stages` (by `apply_order`) and each candidate's `status`
   (`ready`/`blocked`/`reference_only`).
3. Process stages in ascending `apply_order` order, lowest first -- never skip ahead, since a
   later stage's candidates may depend on an earlier stage's objects (e.g. a `policy` candidate
   that depends on a `network_destination` alias, or a `role` that depends on a `policy`).
4. For each stage's `ready` candidates only, call `aos8_preview_migration_run(
   target_type="{target_type}", migration_plan=<plan>, selected_candidates=[...], scope_id=...,
   scope_name=..., persona=...)` to preview the operations without persisting anything. Never
   include `blocked` candidates in a preview batch until their dependency is resolved.
5. Review each preview's compatibility errors/blockers, required secret inputs, and dry-run
   payloads before moving to the next stage.
6. Summarize which stages are fully previewable now, which are blocked and on what, and which
   candidates are `reference_only` and must be recreated manually on the target instead.
This prompt only walks through read-only planning and stateless preview; it never calls
`aos8_create_migration_run` or `aos8_apply_migration_run` -- creating or applying a real
migration run requires the user's explicit, separate confirmation naming the exact target."""
