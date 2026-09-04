---
title: "Spec: router fast-paths, hardware catalog coverage, chatbot session loop"
status: draft
---

# Spec: Router fast-paths, hardware catalog coverage, and chatbot session loop

## Problem Statement

The user runs `hpe-networking-mcp` for their own personal network (Central,
GLP, Mist, AOS8, ClearPass loaded; EdgeConnect/Apstra/UXI/Axis not used) via
two deployments on their NAS: a `minimal`-mode, write-enabled router (port
8010) for coding-agent use, and a `default`-mode, read-only router (port
8011) for a separate chatbot client on the LAN.

Three concrete pain points surfaced:

1. **AOS8 and ClearPass are slow/imprecise compared to Central/Mist.** The
   router already has 13 curated fast-path wrapper tools for Central and 4
   for Mist (`list_sites`, `find_device`, `mist_clients`, `mist_health`,
   etc.) that skip semantic tool search entirely. AOS8 and ClearPass have
   zero such wrappers, so every AOS8/ClearPass question — even common ones
   like "what VLANs are configured" or "look up this MAC's ClearPass
   session" — pays the full `find_tool` semantic-search round trip over
   hundreds of candidate tools per platform.

2. **Hardware/SKU information feels old or incomplete.** The local
   `catalog-core` backend (`hardware_catalog.sqlite`) is a small, hand-
   curated table of 51 SKUs, snapshotted 2026-09-02 from officially
   verified sources. It is accurate for what it contains but has narrow
   *coverage* — most models the user needs are simply not in it. Its
   sources are largely `arubanetworks.com`/`hpe.com` PDFs; this repo has
   already confirmed (via `ingestion/scrape_product_datasheets.py`'s
   documented findings, and a live re-check performed for this spec) that
   both domains hard-block automated fetches at the WAF/HTTP-2 layer, so
   this catalog cannot be kept current by a scraper — it depends on a
   human periodically reviewing and adding entries. Meanwhile, deeper
   Aruba/HPE QuickSpecs content (full technical specs, accessories,
   configuration rules — the kind of detail `product_datasheets` already
   provides for Juniper/Mist) has no ingestion path into the RAG corpus at
   all today. Juniper switch/AP datasheets and all three Mist doc sources
   (`mist_docs`, `mist_api_docs`, `mist_product_updates`) are already
   scraped automatically from the unblocked `juniper.net` domain and
   registered in `ingestion/source_manifest.json`, so they ride the
   existing weekly freshness job — but nobody has confirmed that job is
   actually installed and running.

3. **The chatbot client (port 8011) repeatedly hits `GET /mcp` and gets
   404s with "unknown or expired session ID"**, looping roughly every 3
   seconds, observed directly in the `hpe-networking-mcp-mcp-router-
   chatbot-1` container logs. The main coding-agent router (port 8010)
   shows healthy `POST /mcp 200/202` traffic in the same window, so the
   server's request handling is not obviously broken — but this needs a
   server-side check before being handed off as a client-side bug.

## Solution

Four independent, separately shippable improvements:

1. Add curated fast-path wrapper tools for AOS8 and ClearPass, mirroring
   the existing Central/Mist wrapper pattern, covering the most common
   read-only questions for each platform.
2. Verify (and if missing, install) the existing weekly RAG-source
   freshness job so it actually keeps the already-registered Juniper and
   Mist datasheet/doc sources current. No new scraper needed for these.
3. Build a new, manually-fed ingestion pipeline for Aruba/HPE QuickSpecs
   PDFs into the RAG corpus as a new source (parallel to
   `product_datasheets`), since these domains cannot be scraped live.
   This is additive to RAG, not a change to `hardware_catalog.sqlite`.
4. Diagnose the port-8011 chatbot's session/404 loop from the server side
   first (confirm the container isn't crash-looping or expiring sessions
   unexpectedly); if the server is healthy, document the finding and hand
   the reconnect-logic fix to wherever the chatbot client's own code
   lives, since it is a separate codebase from this repo.

## User Stories

1. As the operator, I want a fast-path wrapper for common AOS8 questions
   (e.g. VLAN/interface status, switch inventory), so that I don't wait on
   full semantic tool search for routine AOS8 lookups.
2. As the operator, I want a fast-path wrapper for common ClearPass
   questions (e.g. endpoint/device lookup by MAC, active session lookup),
   so that I don't wait on full semantic tool search for routine ClearPass
   lookups.
3. As the operator, I want confirmation that Juniper/Mist documentation
   and datasheets are actually being refreshed on a recurring schedule, so
   that "old information" isn't silently happening for sources that are
   already supposed to be automated.
4. As the operator, I want a documented way to add Aruba/HPE QuickSpecs
   PDFs to the RAG corpus, so that `ask_docs`/`search_docs` can answer
   detailed spec/configuration/accessory questions for HPE/Aruba hardware,
   not just the 51 SKUs in the curated catalog.
5. As the operator, I want to know whether the chatbot's connection-loop
   bug is caused by this server or by the chatbot client, so that I know
   where to actually fix it.
6. As the operator, I want the new AOS8/ClearPass wrappers to only ever
   dispatch read-only backend tools, so that adding a fast path never
   silently expands what a `default`-mode, read-only chatbot deployment
   can do.
7. As the operator, I want the QuickSpecs ingestion pipeline to reuse the
   existing chunking/embedding machinery (`ingestion/chunking.py`,
   `scripts/*ingest_docs*`), so that it behaves identically to every other
   RAG source (dedup, provenance, freshness checks) rather than
   introducing a second code path.

## Implementation Decisions

### 1. AOS8 + ClearPass fast-path wrappers

- Follow the existing seam exactly: the `@_dispatching_wrapper_tool
  (READ_ONLY)` decorator plus `_cached_dispatch(ctx, <backend_tool_name>,
  args)`, registered only `if _ROUTER_MODE != "minimal" and "<platform>-
  core" in _BACKENDS:` — the same conditional-registration pattern already
  used for the Mist wrapper block.
- New wrappers dispatch only to backend tools already annotated read-only;
  no new backend tools are created, and no write/destructive capability is
  exposed through a wrapper.
- Candidate AOS8 wrappers: switch/VLAN/interface status lookup (backed by
  the existing generated `aos8_get_object_*` read operations) and a
  migration-run status/list wrapper (backed by the existing
  `aos8_get_migration_run` / `aos8_list_migration_runs` curated tools).
- Candidate ClearPass wrappers: endpoint/device lookup by MAC or IP
  (backed by the existing `DeviceFingerprint*Get` generated operations)
  and active session/session-ACL lookup (backed by the existing
  `SessionAccessControlList*Get` generated operations).
- Exact wrapper names, parameter shapes, and which specific generated
  operations they call are implementation details to finalize during
  `/tdd`, following the Mist wrapper block as the reference for signature
  style (optional org/site-equivalent identifiers with an env-var
  default, a clear error when nothing resolves).

### 2. Freshness job verification

- Check whether `scripts/schedule_freshness_check.sh` has actually been
  installed as a recurring job (cron/launchd/systemd-timer, whichever this
  NAS uses) on the ugreen deployment, not just documented.
- If missing, install it there, scoped to the existing default weekly
  cadence (Sunday 04:00) already documented in the script.
- No changes to `ingestion/source_manifest.json` are needed for Juniper/
  Mist sources — they are already registered entries this job already
  covers.

### 3. QuickSpecs ingestion pipeline (new)

- New source, following the same shape as `product_datasheets`: a
  discovery/scrape step is not viable (confirmed WAF/HTTP-2 block on both
  `hpe.com` and `arubanetworks.com` from this environment), so the
  "discovery" step is replaced by a manually-maintained local drop folder
  of QuickSpecs PDFs the operator fetches themselves.
- A new ingestion script reads that local folder, extracts/chunks PDF
  text (reusing `ingestion/chunking.py`), and feeds the existing
  `ingest_docs.py` pipeline as a new named source (e.g.
  `hpe_quickspecs`), so it gets the same dedup, provenance, and freshness-
  check treatment as every other RAG source.
- Register the new source in `ingestion/source_manifest.json` with
  `scraper` pointed at a script that reads local files instead of the
  network, and mark it explicitly as manually-refreshed input (not
  network-scraped) in that manifest entry's metadata, matching the
  existing precedent of documenting non-scheduled sources (e.g. the
  Aruba hardware EOL PDF entry in `docs/source-lifecycle-coverage.md`).
- This is purely additive: `hardware_catalog.sqlite` and
  `scripts/build_hardware_catalog.py` are unchanged. QuickSpecs answers
  come from `ask_docs`/`search_docs`, not from the exact-match SKU catalog.

### 4. Chatbot session/404 loop

- Server-side diagnosis only, in this repo: check the
  `hpe-networking-mcp-mcp-router-chatbot-1` container's restart history
  and uptime around the observed log window, and check whether any
  session-TTL-relevant configuration (`HPE_MCP_ROUTER_WRAPPER_CACHE_TTL_
  SECONDS` or any MCP-SDK-level session timeout) could explain sessions
  going stale on a timescale that matches the client's retry loop.
- If the server looks healthy (no crash-loop, no unusual session
  expiry), document that finding plainly and treat the reconnect-on-
  expiry logic as a bug in the separate chatbot client codebase — no
  further server-side change is in scope here.

## Testing Decisions

- New AOS8/ClearPass wrappers: unit tests following the existing pattern
  for Mist wrapper tests (dispatch to the correct backend tool name with
  correctly shaped arguments; correct error when a required identifier is
  missing and no env-var default is set) — test only the wrapper's
  external dispatch behavior, not the underlying generated tool's
  internals.
- QuickSpecs ingestion: reuse `tests/eval/rag_eval.yaml` conventions —
  add `howto`/`api-lookup`-style eval cases once real QuickSpecs PDFs are
  ingested, following the existing eval harness in `tests/eval/run_eval.py`
  rather than writing a new one.
- Freshness job: a smoke check that the scheduled job exists and its
  target script (`refresh_rag_sources.py --check-only`) runs successfully
  is sufficient; no new automated test harness needed for a deployment/
  ops verification task.
- Chatbot loop diagnosis: no new repo tests — this is a log-based
  investigation task, not a code change, unless server-side config is
  found to be the cause.

## Out of Scope

- Any change to `EdgeConnect`, `Apstra`, `UXI`, or `Axis` backends — not
  used in this deployment.
- Building a cross-encoder reranker for `find_tool`/`search_tools`
  (`docs/architecture/reranker-plan.md` Phase 3) — explicitly deferred;
  the wrapper approach is the cheaper fix being tried first.
- Automated tool-usage telemetry/analytics — no such system exists today;
  out of scope for this spec, could be a future, separate effort.
- The bigger platform vision (BOM generation, network diagrams,
  PowerPoint/document generation, full read-write device management,
  self-healing network) — explicitly deferred to a future `/wayfinder`
  session once this foundation is solid.
- Any actual fix to the chatbot client's reconnect logic, if the server
  is found to be healthy — that fix lives in a different codebase.
- Expanding `hardware_catalog.sqlite`'s curated SKU list itself — coverage
  gaps for HPE/Aruba hardware are addressed via QuickSpecs-in-RAG (item
  3) instead.

## Further Notes

- The two live router deployments (8010 minimal/write-enabled, 8011
  default/read-only) are configured via `docker-compose.override.yml` on
  the NAS (not tracked upstream) — any AOS8/ClearPass wrapper additions
  need both deployments recreated (`--force-recreate`) to pick up the new
  code, per the existing precedent already documented in that override
  file for secret changes.
- `docs/architecture/reranker-plan.md` already documents extending
  reranking to tool search as an explicit, separately-gated future phase
  — this spec's wrapper-first approach is complementary to, not a
  replacement for, that plan.
