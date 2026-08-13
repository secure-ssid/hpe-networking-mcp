# Example prompts

These scenarios are written for the default low-token router profile:

```env
HPE_MCP_ROUTER_MODE=minimal
HPE_MCP_TOOLSETS=central,glp,rag
```

In this profile, ask your MCP client to use `find_tool` first, then dispatch
with `invoke_read_tool` (read), `invoke_tool` (diagnostic/write/destructive).
That keeps the tool list small while still reaching the full backend catalog.
See [tool-router.md](tool-router.md) for how discovery and dispatch work and
what each safety classification means.

<p>
<span class="docs-badge docs-badge--read">read</span>
<span class="docs-badge docs-badge--diagnostic">diagnostic</span>
<span class="docs-badge docs-badge--write">write</span>
<span class="docs-badge docs-badge--destructive">destructive</span>
label each scenario below by its normalized <code>capability</code>.
</p>

<div class="example-grid" markdown="1">

<div class="example-card" markdown="1">
<h3>1. First read</h3>
<p><span class="docs-badge docs-badge--read">read</span></p>
<p><strong>Prompt:</strong> "Use hpe-networking-mcp to find the tool for listing
Aruba Central sites, then call it with a limit of 10."</p>

```text
find_tool("list Aruba Central sites")
invoke_read_tool("list_sites", {"limit": 10, "offset": 0})
```

<p><strong>Expected shape:</strong> a JSON array of site records (id, name,
location fields), or a <code>{"items": [...], "_pagination": {...}}</code>
wrapper only if the result needed clipping.</p>
<p><strong>Variation:</strong> swap in <code>list_devices</code> with
<code>device_type</code>/<code>site_id</code> filters to inventory APs or
switches instead of sites. For wireless troubleshooting, use
<code>list_bssids</code> with <code>site_id</code>,
<code>serial_number</code>, or MAC filters to map each BSSID to its AP,
radio, WLAN, band, and site. Use <code>list_sites_client_health</code> with
<code>site_id</code>, <code>site_name</code>, or health sorting to compare
wired and wireless client experience across sites.</p>
</div>

<div class="example-card" markdown="1">
<h3>2. Exact API lookup</h3>
<p><span class="docs-badge docs-badge--read">read</span></p>
<p><strong>Prompt:</strong> "Look up the exact OpenAPI endpoint or schema for
Central client alerts. Do not guess from prose."</p>

```text
find_tool("exact OpenAPI lookup")
invoke_read_tool("lookup_api", {"query": "Central client alerts", "top_k": 10})
```

<p><strong>Expected shape:</strong> a JSON array of matched OpenAPI
operations/schema fields, authoritative and lossless from the parsed specs;
an empty array means no confident match -- fall back to <code>ask_docs</code>
or <code>search_docs</code>.</p>
<p><strong>Variation:</strong> ask for exact enum values, e.g.
<code>lookup_api("wlan-ssids opmode enum values")</code>, or bypass fuzzy
ranking with an exact route or operation ID:
<code>lookup_api("GET /network-monitoring/v1/sites-client-health")</code> and
<code>lookup_api("listSitesClientHealthV1")</code>.</p>
</div>

<div class="example-card" markdown="1">
<h3>3. RAG answer</h3>
<p><span class="docs-badge docs-badge--read">read</span></p>
<p><strong>Prompt:</strong> "Explain how WPA3 SAE transition mode is
represented in Central config. Include citations."</p>

```text
find_tool("ask Aruba docs with citations")
invoke_read_tool("ask_docs", {"question": "WPA3 SAE transition mode", "top_k": 5})
```

<p><strong>Expected shape:</strong> a compact extractive answer plus a
bounded list of citations (<code>file_path</code> and matched chunk),
instead of several long raw chunks.</p>
<p><strong>Variation:</strong> call <code>search_docs</code> directly for
raw retrieval when you want to see every matched chunk yourself.</p>
</div>

<div class="example-card" markdown="1">
<h3>4. Bounded batch</h3>
<p><span class="docs-badge docs-badge--read">read</span></p>
<p><strong>Prompt:</strong> "Show me the active critical alerts and the
first 25 sites in one round trip."</p>

```text
find_tool("active critical alerts")
invoke_read_tool_batch({
  "calls": [
    {"id": "alerts", "name": "list_active_alerts", "arguments": {"severity": "CRITICAL"}},
    {"id": "sites",  "name": "list_sites",         "arguments": {"limit": 25}}
  ]
})
```

<p><strong>Expected shape:</strong>
<code>{"ok": bool, "results": [{"id": ..., "tool": ..., "status": "ok"|"blocked"|..., "result": {...}}], "counts": {"total": 2, "succeeded": ..., "failed": ...}, "failed_ids": [...], "truncated": bool}</code>.
<code>ok</code> is only <code>true</code> when every entry succeeded; one
failed entry never aborts the rest.</p>
<p><strong>Variation:</strong> outside a batch, resume one oversized single
read by passing the previous response's <code>next_cursor</code> back as
<code>cursor</code>: <code>invoke_read_tool("list_devices", {"site_id": "SITE_ID"}, cursor="eyJ2IjoxLCJl...")</code>.
<code>invoke_read_tool_batch</code> is available outside <code>minimal</code>
mode.</p>
</div>

<div class="example-card" markdown="1">
<h3>5. Diagnostic call</h3>
<p><span class="docs-badge docs-badge--diagnostic">diagnostic</span></p>
<p><strong>Prompt:</strong> "Ping 10.0.0.1 from CX switch CN12ABC456 and show
me the result."</p>

```text
find_tool("ping from a CX switch")
invoke_tool("cx_ping", {"serial_number": "CN12ABC456", "destination": "10.0.0.1"})
```

<p><strong>Expected shape:</strong> an async result dict (the call polls for
roughly 60s) with the ping output and an <code>errors</code> list. Diagnostic
tools dispatch through <code>invoke_tool</code>, not
<code>invoke_read_tool</code>, because they are intentionally not annotated
read-only even though they change no device state.</p>
<p><strong>Variation:</strong> <code>cx_traceroute</code> for a path trace, or
<code>cx_show</code> with a <code>commands</code> list for raw CLI output.</p>
</div>

<div class="example-card" markdown="1">
<h3>6. Dry-run write</h3>
<p><span class="docs-badge docs-badge--write">write</span></p>
<p><strong>Prompt:</strong> "Build a MAC-auth guest SSID on scope SCOPE_ID.
Show me the dry-run payload only -- do not apply it yet."</p>

```text
find_tool("build an SSID")
invoke_tool("build_underlay_ssid", {"ssid_name": "guest-wifi", "scope_id": "SCOPE_ID", "opmode": "OPEN", "dry_run": true})
```

<p><strong>Expected shape:</strong> the preview payload describing what
would be created, plus an <code>execution_contract</code> whose
<code>dry_run.state</code> reports <code>preview</code>. Nothing is written
while <code>dry_run</code> is <code>true</code>.</p>
<p><strong>Variation:</strong> after the user reviews the preview and asks
for the change, repeat the same call with <code>"dry_run": false</code>.</p>
</div>

<div class="example-card" markdown="1">
<h3>7. Confirmation-gated destructive change</h3>
<p><span class="docs-badge docs-badge--destructive">destructive</span></p>
<p><strong>Prompt:</strong> "I reviewed the dry-run output for migration run
RUN_ID -- apply it for real now."</p>

```text
find_tool("AOS8 apply migration run")
invoke_tool("aos8_apply_migration_run", {"run_id": "RUN_ID", "dry_run": false, "confirm": true})
```

<p><strong>Expected shape:</strong> per-candidate apply/resume results. This
tool takes an explicit <code>confirm: bool</code> argument alongside
<code>dry_run</code> and refuses a real write without both a prior dry run
and <code>confirm=true</code>.</p>
<p><strong>Variation:</strong> some destructive ops tools
(<code>reboot_device</code>, <code>port_bounce</code>, <code>poe_bounce</code>,
<code>disconnect_client</code>) have no <code>confirm</code> argument at all
-- they confirm interactively through MCP elicitation instead. Check the
tool's schema before assuming either shape.</p>
</div>

<div class="example-card" markdown="1">
<h3>8. Blocked by a write gate</h3>
<p><span class="docs-badge docs-badge--write">write</span></p>
<p><strong>Prompt:</strong> "Invite jane@example.com to the GreenLake
workspace."</p>

```text
find_tool("invite a GLP user")
invoke_tool("invite_glp_user", {"email": "jane@example.com"})
```

<p><strong>Expected shape (default install):</strong> GLP writes default to
disabled, so this returns a blocked response before any backend call --
<code>{"error": "... glp writes are not enabled ...", "tool": "invite_glp_user", "status": "blocked", "platform": "glp", "execution_contract": {...}}</code>.
See <a href="tool-router.md#safety-gates">the router safety-gates section</a>
for the full shape.</p>
<p><strong>Variation:</strong> set <code>HPE_MCP_GLP_V2BETA1_WRITES=1</code>
and repeat the identical call to actually send the invite.</p>
</div>

<div class="example-card" markdown="1">
<h3>9. Optional-product example</h3>
<p><span class="docs-badge docs-badge--read">read</span></p>
<p><strong>Prompt:</strong> "Check whether the Mist optional backend is
configured, then list the first 10 Apstra blueprints."</p>

```env
HPE_MCP_ACCESS_PROFILE=custom
HPE_MCP_PRODUCTS=mist,apstra
HPE_MCP_PRODUCT_ACCESS=read-only
```

```text
find_tool("Mist backend status")
invoke_read_tool("mist_status", {})
find_tool("Apstra list blueprints")
invoke_read_tool("apstra_list_blueprints", {"limit": 10})
```

<p><strong>Expected shape:</strong> <code>mist_status</code> returns
<code>{"configured": bool, "host": ..., "has_token": bool}</code>;
<code>apstra_list_blueprints</code> returns a limit/offset-paginated list of
blueprint records. Optional products stay disabled unless
<code>HPE_MCP_PRODUCTS</code>/<code>HPE_MCP_TOOLSETS</code> enables
them, and their write tools stay hidden while
<code>HPE_MCP_PRODUCT_ACCESS=read-only</code> under the <code>custom</code>
compatibility profile.</p>
<p><strong>Variation:</strong> swap in UXI --
<code>invoke_read_tool("uxi_list_sensors", {"page_size": 10})</code> then
<code>invoke_read_tool("uxi_get_sensor_status", {"sensor_id": "SENSOR_ID"})</code>.</p>
</div>

</div>

<div class="docs-callout docs-callout--warning" markdown="1">
<strong>Write or destructive work.</strong> Confirm scope, device type, and
(for SSIDs) security mode and VLANs before calling a write tool -- never
assume. Prefer <code>dry_run=true</code> first when the tool supports it,
and use <code>invoke_tool</code> only after the user has given explicit
intent for a write or destructive action.
</div>

<div class="docs-next" markdown="1">

### Next

- [tool-router.md](tool-router.md) -- discovery/dispatch flow, pagination and
  bounded responses, and every safety gate in detail.
- [optional-products.md](optional-products.md) -- the full ClearPass, Mist,
  Apstra, AOS8, EdgeConnect, UXI, and Axis workflow matrix.
- [mcp-client-recipes.md](mcp-client-recipes.md) -- stdio and streamable HTTP
  client setup recipes.

</div>
