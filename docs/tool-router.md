---
title: "Tool router"
nav_order: 4
---

# Low-token tool router

For the MCP + RAG mental model, start with
[How MCP and RAG work](architecture/how-it-works.md). This page is the
router contract.

`src/hpe_networking_mcp/mcp_servers/tool_router.py` is the recommended MCP entrypoint. Instead of
exposing every backend tool to the client up front, it exposes a small
**discovery/dispatch** surface and loads backend tools on demand.

<figure class="docs-figure">
  <img src="assets/diagrams/router-safety-flow.svg" alt="Flowchart titled Router discovery and safety flow. A task request goes to find_tool, which classifies the selected tool's capability. Read capability dispatches through invoke_read_tool; diagnostic capability dispatches through invoke_tool; both go straight to backend dispatch. Write and destructive capability first require a dry-run preview, then a gate check asking whether the tool is enabled by the global and platform gates. A no answer returns a blocked response with the next action; a yes, reviewed answer dispatches to the backend.">
  <figcaption>Every call starts at <code>find_tool</code>. Read and diagnostic capabilities dispatch immediately; write and destructive capabilities need a dry-run preview, the platform write gate, and confirmation before the backend is reached.</figcaption>
</figure>

## Daily workflow

<div class="docs-checkpoint">
  <span class="docs-checkpoint__number">1</span>
  <div class="docs-checkpoint__body" markdown="1">
    <p>Ask <code>find_tool</code> for the action you need. Each result names the exact backend tool plus its normalized <code>capability</code> and <code>recommended_dispatcher</code>.</p>
  </div>
</div>
<div class="docs-checkpoint">
  <span class="docs-checkpoint__number">2</span>
  <div class="docs-checkpoint__body" markdown="1">
    <p>If <code>capability</code> is <code>read</code>, call <code>invoke_read_tool</code>. If it is <code>diagnostic</code>, <code>write</code>, or <code>destructive</code>, call <code>invoke_tool</code>.</p>
  </div>
</div>
<div class="docs-checkpoint">
  <span class="docs-checkpoint__number">3</span>
  <div class="docs-checkpoint__body" markdown="1">
    <p>For a <code>write</code>/<code>destructive</code> tool, pass <code>dry_run=true</code> first when the schema supports it, and only re-run with a real write after explicit user intent.</p>
  </div>
</div>

```text
find_tool("show active critical alerts")
invoke_read_tool("list_active_alerts", {"severity": "CRITICAL"})
```

See [example-prompts.md](example-prompts.md) for complete scenario cards with
router calls, expected result shapes, and safety classifications.

## Error responses: the response envelope

Successful results pass through unchanged. Every **failed or blocked**
result -- a write-gate refusal, an unknown tool name, an invalid cursor, or
a backend error, whether returned as a dict or raised as an exception -- is
wrapped in a deterministic envelope before it reaches the client:

```json
{
  "ok": false,
  "status": 403,
  "data": {
    "error": "Tool 'mist_set_site' is not read-only. Use invoke_tool only after explicit user intent for write/destructive actions.",
    "tool": "mist_set_site",
    "status": "blocked"
  },
  "message": "Tool 'mist_set_site' is not read-only. Use invoke_tool only after explicit user intent for write/destructive actions.",
  "tool": "invoke_read_tool",
  "platform": "mist"
}
```

- Branch on **`ok`**, always `false` for an envelope. Successful payloads
  never carry it.
- `status` is an **integer** HTTP-style code: `403` for write-gate
  `blocked`/`forbidden` refusals, `409` for confirmation or cancellation
  conflicts, `404` for unknown tools, `400` for caller errors such as a
  stale cursor or malformed batch entry, `500` for unpinned failures, and
  the upstream vendor API code whenever one is recoverable.
- `data` is the tool's own error payload, unchanged -- fields such as the
  string `"status": "blocked"` or an `execution_contract` live **inside
  `data`**, not at the top level.
- `message` is a short human-readable summary; `tool` names the router tool
  the client called (the failed backend tool is named inside `data`);
  `platform` is the owning backend's platform. A `hint` key with a
  spec-grounded or generic explanation of the status may also be present.

The JSON error examples on this page show the envelope's **`data` payload**
unless the full envelope is displayed, as above. Standalone backends
(`python -m hpe_networking_mcp.mcp_servers.<x>`) wrap failures in the same
envelope.

## Router tools

<p>
<span class="docs-badge docs-badge--read">read</span>
<span class="docs-badge docs-badge--diagnostic">diagnostic</span>
<span class="docs-badge docs-badge--write">write</span>
<span class="docs-badge docs-badge--destructive">destructive</span>
are the four normalized capabilities <code>find_tool</code> reports, used throughout this page.
</p>

<div class="docs-compact-table" markdown="1">

| Tool | Safety | Use |
|---|---|---|
| `find_tool` | <span class="docs-badge docs-badge--read">read</span> | Search the enabled backend catalog |
| `invoke_read_tool` | <span class="docs-badge docs-badge--read">read</span> | Dispatch only backend tools annotated read-only |
| `invoke_read_tool_batch` | <span class="docs-badge docs-badge--read">read</span> | Bounded, ordered batch of read-only calls in one round trip (outside `minimal` mode) |
| `invoke_tools_batch` | <span class="docs-badge docs-badge--destructive">destructive</span> | Bounded, ordered batch of read AND write calls in one round trip (outside `minimal` mode) |
| `invoke_tool` | <span class="docs-badge docs-badge--destructive">destructive</span> | Generic dispatcher for diagnostic/write/destructive tools |
| Convenience wrappers | mixed | Available only outside `minimal` mode |
| `plan_tool_workflow` | <span class="docs-badge docs-badge--read">read</span> | Deterministic, catalog-backed dependency/order planner (outside `minimal` mode) |
| `plan_reconciliation_schedule` | <span class="docs-badge docs-badge--read">read</span> | Plan-only recurring reconciliation schedule builder (outside `minimal` mode) |
| `evaluate_compliance_policy` | <span class="docs-badge docs-badge--read">read</span> | Bounded, declarative compliance-policy evaluator over caller-supplied observations (outside `minimal` mode) |

</div>

`invoke_tool` is annotated destructive as a router-dispatch safety label, not
a claim that every tool behind it is destructive -- see
[Why `invoke_tool` is destructive](#why-invoke_tool-is-destructive).

## Discover with `find_tool`

`find_tool(query, top_k=5, include_schema=False, platform=None, server=None, capability=None, origin=None, operation_id=None)`
combines semantic search with a tool-name keyword match and returns compact,
deduplicated results. Set `include_schema=true` only when you need the full
JSON schema for one selected tool -- it is omitted by default to keep
discovery responses small.

```text
find_tool("list Aruba Central sites")
```

<div class="docs-callout docs-callout--info" markdown="1">

```json
[
  {
    "name": "list_sites",
    "server": "central-monitoring",
    "platform": "central",
    "capability": "read",
    "recommended_dispatcher": "invoke_read_tool",
    "requires_write_enablement": false,
    "currently_enabled": true,
    "supports_dry_run": false,
    "supports_confirm": false,
    "requires_confirmation": false,
    "read_only": true,
    "destructive": false,
    "idempotent": true
  }
]
```

`recommended_dispatcher` is the field to branch on: `invoke_read_tool` for
`read`, `invoke_tool` for everything else.
</div>

Filter discovery with `platform`, exact `server`, or normalized `capability`
(`read`, `diagnostic`, `write`, `destructive`). Filters apply equally to
keyword and semantic matches:

```text
find_tool("configuration", platform="central", capability="write")
find_tool("health check", server="mist-core", capability="diagnostic")
```

Write/destructive results also carry an `execution_contract` -- the same
compact shape attached to router-dispatched write responses (see
[Safety gates](#safety-gates)):

```json
{
  "platform": "central",
  "capability": "write",
  "gate": {"env_var": "HPE_MCP_CENTRAL_WRITES", "state": "enabled", "source": "platform_override"},
  "dry_run": {"supported": true, "state": "default_preview"},
  "confirm": {"supported": true, "required": true},
  "idempotent": true,
  "next_action": "Call invoke_tool with dry_run=true to preview the change."
}
```

`dry_run.state` becomes `preview` or `execution_requested` at dispatch time,
once the schema and call arguments make that state knowable. Invalid gate
values fail closed. Read and diagnostic responses are never decorated with
write metadata.

If the semantic tool index is unavailable and no keyword fallback matches,
`find_tool` returns a compact error with a rebuild hint instead of an empty
success-shaped result.

### Unknown tool names

Guessing a tool name instead of calling `find_tool` first still gets a
structured answer instead of a bare protocol error. A name with a
recognized optional-product prefix (`mist_`, `clearpass_`, `apstra_`,
`aos8_`, `edgeconnect_`, `uxi_`, `axis_`) whose backend isn't currently
loaded reports that distinctly from an ordinary typo:

<div class="docs-callout docs-callout--info" markdown="1">

*Shown as the envelope's `data` payload (top level is `{ok: false, status: 404, ...}`) -- see [Error responses](#error-responses-the-response-envelope).*

```json
{
  "error": "Unknown tool: mist_get_site_stats",
  "reason": "platform_not_configured",
  "platform": "mist",
  "hint": "The 'mist' backend is not currently enabled. Set HPE_MCP_PRODUCTS=mist (or include it in HPE_MCP_TOOLSETS) and configure Mist credentials, then restart the server.",
  "suggestions": []
}
```

Any other unresolved name -- including a typo of a tool on an
**already-enabled** platform -- instead gets the ordinary fuzzy "did you
mean" fallback (also the envelope's `data` payload):

```json
{
  "error": "Unknown tool: get_devices",
  "hint": "Use find_tool to discover available tools, then invoke_read_tool for read-only results or invoke_tool for intentional writes.",
  "suggestions": [{"name": "list_devices", "score": 0.5}]
}
```
</div>

`design` is intentionally excluded from the prefix check: its tools
(`list_diagram_icons`, `drawio_network_design_diagram`, ...) don't share a
`design_` prefix, so a `design_...` guess falls through to the fuzzy path
above instead of a possibly-wrong platform claim.

## Dispatch reads with `invoke_read_tool`

`invoke_read_tool(name, arguments=None, cursor=None)` refuses any tool that is
not annotated read-only, before the backend is ever called.

```text
invoke_read_tool("list_active_alerts", {"severity": "CRITICAL"})
```

Scope discovery uses Central's official v1 scope-management endpoints and
returns normalized global, site, and device-group records:

```text
invoke_read_tool("list_scopes", {"limit": 100, "offset": 0})
```

If one scope source is unavailable, the usable records are preserved with a
bounded `warnings` list. If all three sources fail, the tool returns an
explicit failed result rather than an empty success.

<div class="docs-callout docs-callout--danger" markdown="1">

Calling a write tool through `invoke_read_tool` is refused, not silently
downgraded (`data` payload shown; the envelope's top-level `status` is
`403` -- see [Error responses](#error-responses-the-response-envelope)):

```json
{
  "error": "Tool 'mist_set_site' is not read-only. Use invoke_tool only after explicit user intent for write/destructive actions.",
  "tool": "mist_set_site",
  "status": "blocked"
}
```
</div>

### Response budgets and continuation metadata

Every result dispatched through `invoke_tool`/`invoke_read_tool` passes
through a deterministic response-bounding step. A response already inside
budget is returned byte-for-byte unchanged. When clipping is required, the
response gains a `_pagination` block plus a `_response_bounds` marker:

```json
{
  "items": ["...bounded..."],
  "_pagination": {"limit": 25, "offset": 0, "truncated": true, "total": 400},
  "_response_bounds": {"truncated": true, "reason": "item_budget", "item_limit": 25, "byte_limit": 200000}
}
```

`reason` is `item_budget`, `byte_budget`, or `item_budget+byte_budget`. If a
result has nothing sliceable and still exceeds the byte budget, the response
falls back to a bounded text `preview` instead of an over-budget payload.
Configure the two budgets with `HPE_MCP_ROUTER_RESPONSE_MAX_ITEMS`
(default 200, range 1-200) and `HPE_MCP_ROUTER_RESPONSE_MAX_BYTES`
(default 200,000, minimum 1024); invalid or missing values fall back to the
defaults rather than raising.

**Continuation cursors (`invoke_read_tool` only).** When a clipped response
has more data remaining, it also gains an opaque `next_cursor` string and a
`resumable: true` flag inside `_response_bounds`. Pass that value back as the
`cursor` argument on a repeated call to the **same tool with the same
arguments** to fetch the next page:

```json
{
  "items": ["...page 1..."],
  "_pagination": {"limit": 40, "offset": 0, "truncated": true, "total": 100},
  "_response_bounds": {"truncated": true, "reason": "item_budget", "item_limit": 40, "byte_limit": 200000, "resumable": true},
  "next_cursor": "eyJ2IjoxLCJl...",
  "cursor_expires_in_seconds": 900
}
```

```text
invoke_read_tool("list_devices", {"site_id": "SITE_ID"}, cursor="eyJ2IjoxLCJl...")
```

- The generic, destructive-annotated `invoke_tool` has no `cursor` parameter
  and never emits or accepts one, even when dispatching a capability-`read`
  tool.
- A cursor is HMAC-signed with a random key generated once per server
  process, and carries only a version, an expiry, the next offset, and short
  digests binding it to the exact tool name and canonical arguments -- never
  raw arguments, identifiers, or result data.
- A server restart invalidates every outstanding cursor. A malformed,
  tampered, expired (`HPE_MCP_ROUTER_CURSOR_TTL_SECONDS`, default 900s,
  clamped to 30-3600s), or mismatched cursor returns an envelope whose
  `data` payload is `{"error": ..., "tool": ..., "status": "invalid_cursor"}`
  (top-level `status` is `400`) **without** calling the backend.
- If a single item can never fit the byte budget, the response is marked
  `"resumable": false` with a `resumable_reason` instead of emitting a cursor
  that would just re-fetch the same oversized item forever.

## Dispatch diagnostics and writes with `invoke_tool`

`invoke_tool(name, arguments=None)` dispatches through the owning backend's
MCPServer tool manager, so arguments get MCPServer validation/coercion and the
router's request `Context` is forwarded -- this is what lets async,
`ctx`-requiring destructive ops tools (`reboot_device`, `port_bounce`,
`poe_bounce`, `disconnect_client`) reach their confirmation elicitation.
Diagnostic tools also go through `invoke_tool` because they are
intentionally not annotated read-only.

Diagnostic call:

```text
invoke_tool("cx_ping", {"serial_number": "CN12ABC456", "destination": "10.0.0.1"})
```

Dry-run write preview -- no state changes yet:

```text
invoke_tool("build_underlay_ssid", {"ssid_name": "guest-wifi", "scope_id": "SCOPE_ID", "dry_run": true})
```

Only re-run with `dry_run=false` after the user has reviewed the preview and
explicitly asked for the change. Some destructive ops tools instead confirm
interactively through MCP elicitation on `ctx` and take no `confirm`
argument at all (`reboot_device`, `port_bounce`, `poe_bounce`,
`disconnect_client`); others, like `aos8_apply_migration_run`, take an
explicit `confirm: bool` argument alongside `dry_run`. Check the tool's own
schema (`find_tool(..., include_schema=true)`) rather than assuming either
shape.

SSID, role, and profile scope-map workflows validate every scope ID before
their first write. Invalid, nonnumeric, or overlong IDs fail the preflight
instead of leaving a partially created resource.

## Safety gates

### Aggregate access profiles

`HPE_MCP_ACCESS_PROFILE` provides one end-to-end switch for the router,
direct-mode registration, standalone backends, stdio, and streamable HTTP:

| Profile | Behavior |
|---|---|
| `custom` | Compatibility default; preserves the existing Central, GLP, optional-product, and per-platform gates |
| `safe-read-only` | Hides writes from router discovery/direct registration and blocks every write/destructive dispatch while leaving reads and diagnostics available |
| `full-read-write` | Enables ordinary writes for every loaded platform |

Full read/write mode changes availability only. It never bypasses tool-level
`dry_run`, `confirm`, MCP elicitation, capability annotations, or dedicated
guards such as `HPE_MCP_AOS8_ROLLBACK_WRITES`. Invalid profile names and
contradictory settings refuse server startup; use `custom` for intentionally
mixed platform access.

<div class="docs-callout docs-callout--danger" markdown="1">
<h3>Global read-only kill switch</h3>

Set `HPE_MCP_READONLY=1` for a server-wide write kill switch under `custom`
or `safe-read-only` (`full-read-write` rejects that contradictory setting).
Every `write`/`destructive` tool on **every**
backend is hidden from `find_tool`, skipped in `direct`-mode registration,
and refused at dispatch -- before the backend is ever reached (`data`
payload shown; the envelope's top-level `status` is `403`):

```json
{
  "error": "Tool 'build_underlay_ssid' is disabled because HPE_MCP_READONLY is set. Unset HPE_MCP_READONLY to allow write/destructive tools.",
  "tool": "build_underlay_ssid",
  "status": "blocked"
}
```

`read` and `diagnostic` tools are unaffected, so troubleshooting flows keep
working. The switch is enforced identically for a backend run standalone
(`python -m hpe_networking_mcp.mcp_servers.<x>`). A platform whose own gate is enabled is still
fully read-only while `HPE_MCP_READONLY` is set.
</div>

<div class="docs-callout docs-callout--warning" markdown="1">
<h3>Per-platform write gates</h3>

Under `custom`, Central defaults to writes **disabled**
(`HPE_MCP_CENTRAL_WRITES=1` opts in), as does GLP
(`HPE_MCP_GLP_V2BETA1_WRITES=1` opts in). A blocked GLP write's
envelope `data` payload looks like this (top-level `status` is `403`):

```json
{
  "error": "Tool 'invite_glp_user' is disabled because glp writes are not enabled. Set HPE_MCP_GLP_V2BETA1_WRITES=1 to allow glp write workflows.",
  "tool": "invite_glp_user",
  "status": "blocked",
  "platform": "glp",
  "execution_contract": {
    "platform": "glp",
    "capability": "write",
    "gate": {"env_var": "HPE_MCP_GLP_V2BETA1_WRITES", "state": "disabled", "source": "platform_default"},
    "dry_run": {"supported": false, "state": "unsupported"},
    "confirm": {"supported": false, "required": false},
    "idempotent": true,
    "next_action": "Set HPE_MCP_GLP_V2BETA1_WRITES=1, then retry only after explicit user approval."
  }
}
```

Unrecognized or contradictory manual gate values fail closed and refuse
server startup.
</div>

<div class="docs-callout docs-callout--warning" markdown="1">
<h3>Optional product write access</h3>

The optional starters (`clearpass`, `mist`, `apstra`, `aos8`,
`edgeconnect`, `uxi`, `axis`, `design`) share `HPE_MCP_PRODUCT_ACCESS`, which
defaults to `read-only`. That hides optional write tools from `find_tool` and
blocks direct dispatch through `invoke_tool`. Set
`HPE_MCP_ACCESS_PROFILE=full-read-write` to open every loaded platform, or
keep `custom` and set `HPE_MCP_PRODUCT_ACCESS=read-write` for optional-product
lab workflows. Those write tools still default to `dry_run=True`. Use
`HPE_MCP_<PLATFORM>_WRITES=1` (e.g. `HPE_MCP_AXIS_WRITES=1`) for a
narrower per-platform override instead of opening every optional write at
once. See [optional-products.md](optional-products.md) for the full matrix.
</div>

## Recommended client profile

```env
HPE_MCP_ROUTER_MODE=minimal
HPE_MCP_TOOLSETS=central,glp,rag
```

This keeps the tool list small while still covering the common Central, GLP,
and RAG workflows. If `HPE_MCP_ROUTER_MODE` is omitted, the router uses
`default` mode and includes convenience wrappers -- keep `minimal` in MCP
client configs when token surface matters. Each convenience wrapper
(`list_sites`, `find_device`, `ask_docs`, ...) fans into exactly one backend
call and draws exactly one rate-limit token for it -- the same token a direct
`invoke_read_tool` would draw, never a second token for the wrapper's own
MCP hop.

<div class="docs-compact-table" markdown="1">

| Profile | Client-visible / indexed tools |
|---|---:|
| Minimal router | 3 client-visible tools |
| Default router | 19 client-visible tools[^compliance-tool] |
| Platform API backend index | 6,711 tools |
| Complete backend index (platform APIs plus the non-platform backends itemized in [`docs/project-facts.json`](project-facts.json)) | 6,728 tools |
| Direct-all router | 6,736 client-visible tools |

</div>

[^compliance-tool]: v0.7 added `plan_tool_workflow` and
    `plan_reconciliation_schedule`; the post-v0.7 compliance expansion adds
    `evaluate_compliance_policy` paired with the batch dispatchers
    `invoke_read_tool_batch` and `invoke_tools_batch`, raising the
    default-mode count to 19.
    `minimal` mode remains the same three-tool surface. This count is
    identical whether every toolset/product is loaded or only the
    documented recommended profile (`HPE_MCP_TOOLSETS=central,glp,rag`) is
    -- both are measured independently in
    [`docs/project-facts.json`](project-facts.json)'s `router_modes` section
    (`tools.default` and `tools.default_recommended_profile`).

The complete catalog spans nine platform surfaces plus the non-platform
backends itemized below. Nine generated manifests contain 6,144 reproducible
operations, of which 6,127 register as active generated tools; 584 platform
curated tools bring the REST/OpenAPI platform API backend total to 6,711.

The remaining registered backends are not platform APIs. They are itemized
per backend under `tools.protocol_only`, `tools.non_platform_aggregators`,
`tools.non_api_local` and `tools.credential_free_local` in
[`docs/project-facts.json`](project-facts.json), which is the generated
source of truth for the split; together with the platform total they yield
the complete 6,728-tool registered backend catalog. Minimal mode does not
expose that schema surface to the MCP client -- it searches the catalog on
demand.

Generated requests preserve OpenAPI query-array serialization metadata:
explicit `style: form` plus `explode: false` arrays are sent as comma-separated
values, while default and exploded arrays retain repeated-key encoding.
Generated schemas also hide authentication, content negotiation, HTTP framing,
host/routing, and proxy-derived identity headers. Executors and HTTP clients
own those values; API-level business headers such as `If-Match`,
`Idempotency-Key`, `Tenant-Acid`, and `Hpe-workspace-id` remain available.
Generated safe-method and idempotent `PUT` retries parse both numeric and
HTTP-date `Retry-After` hints. Hints within the five-second retry budget are
honored; longer hints return the rate-limit/transient response without retrying
early or blocking an MCP call for the full server window.

<div class="docs-compact-table" markdown="1">

| Toolset | Enables |
|---|---|
| `central` | Config, monitoring, NAC, ops |
| `central-generated` | Complete generated Central API surface |
| `config` | Central configuration tools |
| `monitoring` | Health, alerts, events, clients, devices |
| `site-health` | Bounded cross-platform Central/Mist site health |
| `nac` | MAC registration, MPSK, visitors, auth policy tools |
| `ops` | Troubleshooting and operational tools |
| `glp` | GreenLake Platform devices and documented attribute grouping, subscriptions, users, Audit Logs v2beta1, workspaces, reporting, service catalog, and guarded writes |
| `rag` | `ask_docs`, `search_docs`, `lookup_api` |
| `interop` | `interop-core`: Central <-> Mist WLAN/site concept translation and bounded trend normalization. Credential-free and read-only-local, so it is loaded on **every** profile -- name it only when you want *just* these tools. |
| `clearpass`, `mist`, `apstra`, `aos8`, `edgeconnect`, `uxi`, `axis`, `design` | Optional product backends |
| `all` | All core and optional backends |

</div>

Optional products can be enabled either by `HPE_MCP_TOOLSETS` or by
`HPE_MCP_PRODUCTS`; see [optional-products.md](optional-products.md) for
the per-product workflow matrix. Generic optional GET responses are
paginated with `limit` and `offset` when the response contains a list.

```env
HPE_MCP_PRODUCTS=clearpass,mist,apstra,aos8,edgeconnect,uxi,axis,design
```

Set `HPE_MCP_TOKENIZE_SECRETS=1` to install the optional session-scoped
secret-tokenization middleware. Plaintext values remain in bounded TTL vaults
instead of being repeated through model-visible tool arguments and results.

## Observability: audit log and metrics

Both are opt-in and disabled by default -- installing them changes no
existing tool behavior, and stdio mode never gains unsolicited output.

**Audit log.** Set `HPE_MCP_AUDIT_LOG=1` to append one redacted JSONL
record per completed or failed router call to `state/tool-audit.jsonl` (or
set the variable to an explicit path). Each record contains:

- `run_id` -- one random id per server process (`run_<hex>`).
- `session_id` -- one random id per connected MCP client session
  (`sess_<hex>`, or `sess_none` outside a session), held in a bounded map.
- `classification` -- `read` / `write` / `destructive` / `diagnostic` /
  `unknown`, resolved from the dispatched backend tool's own annotations.
- `tool`, `target_tool` (the actual backend tool name for
  `invoke_tool`/`invoke_read_tool` calls), `argument_keys`, a SHA-256
  `argument_digest` of a redacted copy of the arguments, `outcome`
  (`success`/`error`/`blocked`/`cancelled`/`timeout`/`exception`/...),
  `duration_ms`, and `error_type` (never the exception message).

Argument and result *values* are never written -- only key names and a
digest.

**Metrics.** Set `HPE_MCP_METRICS=1` to enable bounded, in-process
request/latency/outcome counters (no external dependency, no network call).
Counters are bucketed by a capped set of allow-listed labels -- `tool`,
`backend`, `capability`, `outcome` -- with a hard ceiling (`max_series`,
default 512 distinct `(tool, backend)` pairs; anything beyond folds into one
overflow bucket). Metrics never read argument values, result values, or
exception messages.

Outcome classification treats a non-empty top-level `errors` list as a failed
call even when a backend also reports a neutral status such as `COMPLETED` or
HTTP `200`; an empty `errors` list remains successful.
Client-facing envelopes use `403` for write-gate `blocked`/`forbidden` policy
refusals and `409` for confirmation or cancellation conflicts.

Set `HPE_MCP_METRICS_HTTP=1` (in addition to `HPE_MCP_METRICS=1`, and
only on the streamable-HTTP transport -- see
[credential-free HTTP quickstart](getting-started.md#2-try-it-credential-free))
to also expose a compact JSON snapshot at `GET /metrics`, protected by the
same loopback/allow-list rules and `MCP_HTTP_BEARER_TOKEN` gate as every
other HTTP path here. With only `HPE_MCP_METRICS_HTTP=1` set, the route
responds `{"enabled": false}`.

## Beyond minimal mode

Outside `minimal` mode, four additional read-only tools support batching
and planning without ever calling a live backend write.

<div class="example-grid" markdown="1">
<div class="example-card" markdown="1">
<h3><code>invoke_read_tool_batch</code></h3>
<p>Dispatches an ordered list of read-only calls in one MCP round trip,
through the same annotation gate, cursor verification, and
response-bounding path <code>invoke_read_tool</code> uses. Max 25 entries;
one failed entry never aborts the batch. Each entry accepts <code>name</code>
(required), <code>arguments</code>, an optional correlation <code>id</code>,
and an optional <code>cursor</code>.</p>
</div>
<div class="example-card" markdown="1">
<h3><code>plan_tool_workflow</code></h3>
<p>Builds a deterministic dependency/order plan across the enabled catalog.
Steps reference an exact <code>tool</code> name or a free-text
<code>hint</code> resolved through the same bounded keyword search
<code>find_tool</code> uses -- never guessed. It never calls
<code>invoke_tool</code>/<code>invoke_read_tool</code> itself.</p>
</div>
<div class="example-card" markdown="1">
<h3><code>plan_reconciliation_schedule</code></h3>
<p>Builds a bounded, read-only recurring-check specification: a validated
cadence plus a bounded set of currently enabled read/diagnostic tools. Write
and destructive tools are always excluded. <code>dry_run</code> is always
<code>true</code>.</p>
</div>
<div class="example-card" markdown="1">
<h3><code>evaluate_compliance_policy</code></h3>
<p>A bounded, read-only, declarative compliance-policy evaluator
(<code>src/hpe_networking_mcp/pipeline/compliance.py</code>) over caller-supplied
<code>observations</code> -- it never fetches data itself. Fetch state first
with <code>invoke_read_tool</code>, then pass the results as
<code>observations</code> alongside a declarative <code>policy</code>.</p>
</div>
</div>

```json
invoke_read_tool_batch({
  "calls": [
    {"id": "alerts", "name": "list_active_alerts", "arguments": {"severity": "CRITICAL"}},
    {"id": "sites",  "name": "list_sites",         "arguments": {"limit": 25}}
  ]
})
```

```text
plan_tool_workflow([
  {"id": "discover", "hint": "list devices"},
  {"id": "inspect", "hint": "find a specific device", "depends_on": ["discover"]}
])
plan_reconciliation_schedule("daily", platforms=["central"], max_entries=25)
evaluate_compliance_policy(
  observations=[{"hostname": "sw1", "firmware": {"version": "8.10.0"}}],
  policy=[{"field": "firmware.version", "operator": "version_gte", "expected": "8.9.0"}]
)
```

Each rule has a `field` (a restricted dotted/indexed path), an `operator`
(one of `eq`, `ne`, `lt`, `le`, `gt`, `ge`, `contains`, `in`,
`regex_fullmatch`, `version_gte`, `version_range`, `exists`, `not_exists`),
and an `expected` value (required except for `exists`/`not_exists`).
`regex_fullmatch` is restricted to a fail-closed safe-regex subset: at most
one quantifier opcode anywhere in the whole pattern, no backreferences, no
lookaround. A structurally invalid policy is rejected with `"ok": false`
before any observation is evaluated; every per-rule result's `"actual"`
value is bounded and recursively redacted for credential/secret/tenant-shaped
field paths.

Both planners and the evaluator produce a bounded, redacted `"artifact"`
payload (`router_dependency_plan`, `router_reconciliation_plan`, or
`compliance_report` -- see [artifact-contracts.md](artifact-contracts.md))
ready for `hpe_networking_mcp.pipeline.artifact_contracts.write_artifact`; none of the three
write to disk themselves.

## Why the router does not adopt FastMCP code-mode

FastMCP code-mode is useful when a model needs to write a sandboxed program that
chains many tool calls. It is not enabled here because it would add a second
execution language, sandbox/runtime dependency, and policy boundary to a router
that already has explicit per-tool annotations, write gates, response budgets,
rate limiting, and audit-oriented dispatch.

The low-risk equivalent needed by this project is already available:
`invoke_read_tool_batch` performs up to 25 ordered, read-only calls in one MCP
round trip. Each entry independently passes the same annotation gate, cursor
validation, response bounds, and per-backend rate gate as a single
`invoke_read_tool` call. It cannot execute writes, diagnostics, arbitrary
Python, or filesystem/network code.

This is an intentional additive choice rather than a FastMCP migration. A
future sandboxed execution feature would need an isolated runtime, a
capability-aware API limited to explicitly selected read tools, instruction
and resource limits, cancellation, provenance for every sub-call, and an
expanded security/evaluation gate. Until those requirements are met, batching
provides the round-trip reduction without weakening the router's safety model.

## Why `invoke_tool` is destructive

The backend catalog contains both read-only tools and tools that can change
state. Since `invoke_tool` can dispatch any enabled backend tool, it is
conservatively annotated as destructive. Use `invoke_read_tool` for normal
investigations, and reserve `invoke_tool` for diagnostics, and for
writes/destructive actions taken after explicit user intent.

<div class="docs-next" markdown="1">

### Next

- [example-prompts.md](example-prompts.md) -- copy/paste scenario cards for
  every safety classification.
- [optional-products.md](optional-products.md) -- the optional-product
  matrix and their write gates.
- [troubleshooting.md](troubleshooting.md) -- outcome-driven fixes for setup,
  auth, transport, catalog, and RAG issues.

</div>
