# hpe-networking-mcp — RAG Architecture & Source Provenance (updated 2026-07-25)

**Repo:** https://github.com/secure-ssid/hpe-networking-mcp

---

## TL;DR decision

> **Current default backend = embedded, no Docker, no background services:**
> - **LanceDB** — prose docs (developer/tech/NAC/VSG/aos), with native **hybrid (vector + BM25) + reranking**.
> - **SQLite** — OpenAPI specs as **exact structured lookup** (method/path, operation IDs, endpoints, schemas, fields, and enums), *not* embeddings.
> - **fastembed** — embeddings in-process (ONNX); no Ollama required. Can run the same `nomic-embed-text-v1.5`.
> - **Ship a prebuilt index** as a GitHub Release asset so `git clone → uv sync → run` works with zero ingest.
> - **The portal consumes via the MCP** (`search_docs` / `ask_docs` over stdio or streamable-HTTP) — it never touches the store directly, so no shared server is needed.
>
> **Redis Stack** remains a documented, supported *server option* for anyone who wants it — but it is **not** the default for the cloned-and-run experience.

## July 2026 OpenAPI source migration

Aruba's developer portal moved to ReadMe SuperHub in July 2026. The retired
`internal-ui.central.arubanetworks.com/cnxconfig/docs/*.json` URLs now return
portal error pages, and reference pages no longer embed a complete
`oasDefinition` object.

`ingestion/readme_registry.py` now parses each page's `oasPublicUrl`, resolves
the registry identifier through
`https://dash.readme.com/api/v1/api-registry/{id}`, validates the OpenAPI
payload, and records project/version/hash/source metadata. Both Aruba OpenAPI
scrapers share this implementation.

```bash
uv run python ingestion/scrape_openapi.py
uv run python ingestion/scrape_cnac_spec.py
uv run python ingestion/fetch_mist_openapi.py
uv run python ingestion/scrape_security_lifecycle.py
uv run python scripts/check_openapi_drift.py
uv run python scripts/check_mist_openapi_drift.py
uv run python ingestion/ingest_docs.py
```

The generated `ingestion/openapi_registry_manifest.json` provides rebuild
provenance. Raw scraped sources and `data/*` indexes remain generated
artifacts. Any detected drift requires a source refresh and index rebuild
before `lookup_api` is described as current.

The same OpenAPI source folder also includes a reproducible snapshot from the
official `mistsys/mist_openapi` repository. The fetcher pins Mist API version
2606.1.1 at commit `f374cffdd5a275c7954645a306fcab7f1227e7a3`, verifies the
expected SHA-256, and feeds the result into exact SQLite API lookup. OpenAPI
records are deliberately excluded from the prose embedding table. A scheduled
GitHub Actions job checks Aruba registry hashes and whether the Mist source file
has advanced.

Generated tool manifests extend this provenance model beyond the exact RAG
index. The current catalog records 6,144 operations across Aruba Central, GLP,
Mist, ClearPass, AOS8, EdgeConnect, UXI, Apstra, and Axis. Central/GLP preserve
per-source digests, Mist and EdgeConnect have deterministic pinned generators,
and Apstra records the official `aos-sdk-api==6.1.2.post1` wheel URL and
SHA-256. Manifest schema v2 preserves deprecation, sunset, security,
parameter-serialization, response-code, format, and required-body metadata
when supplied by the source specification.

### Why this and not Redis (reconciling the audit)
The audit recommended **Redis Stack** — correctly, *for its scope*: "two backends are running and the git history is mid-flip; converge on one with the least code change." Redis is already wired in the working tree and holds both the docs and tool indexes.

But the project's primary goal is **"anyone can download the repo and run it,"** with the portal as a *consumer of the MCP*. Against that goal, a Redis/Docker service is the exact friction we want to remove. LanceDB delivers the same capabilities the audit credited to Redis (hybrid BM25+vector, one store for docs+tools, metadata filtering) **without a server**, and uniquely allows shipping a prebuilt index file. `fastembed` removes the last service (Ollama).

| Axis | Redis Stack (audit pick) | **LanceDB + fastembed (current default)** |
|---|---|---|
| Docker / services | Redis container (or local install) + Ollama | **none** (in-process) |
| "clone → run" UX | install Docker, start Redis, ingest 40k docs | **`uv sync` → run** (ship prebuilt index) |
| Hybrid (BM25+vector) | yes (RediSearch) | **yes (native)** |
| Reranking | build it | **built-in (RRF default)** |
| One store for docs + tools | yes | **yes** |
| Embeddings | Ollama service | **fastembed in-process (same nomic model)** |
| Migration effort | none (already wired) | one-time storage-layer rewrite + re-ingest |

Net: Redis wins only on "no work today." For a distributable tool, the one-time migration buys a dramatically simpler install for every future user.

---

## Why retrieval quality goes **up**, not down

Deployment (embedded vs server) does not affect retrieval quality — the
*design* does. The implemented design is strictly better than the historical
vector-only Redis path:

1. **API/field/enum/endpoint questions → exact SQLite lookup, not vectors.** A large slice of the corpus is OpenAPI specs (structured JSON). Embedding them is lossy; vector search returns *fuzzy-similar* prose instead of the authoritative enum/field list. `lookup_api` resolves literal `METHOD /path` and `operationId` identifiers before its structured endpoint/schema/field fallback, so exact identifiers cannot be displaced by similar enum or schema text.
2. **Prose questions → hybrid (BM25 + vector) + rerank.** Today's path is vector-only and *misses exact identifiers* (`WPA3_SAE`, endpoint paths, error codes). BM25 catches those; a cross-encoder rerank promotes the truly relevant chunk. (~+15–30% precision in practice; Anthropic measured up to **67%** retrieval-failure reduction with contextual + hybrid + rerank.)
3. **Same embeddings, fixed prefixes.** fastembed can run `nomic-embed-text-v1.5` in-process — identical semantics to today — while fixing the **missing `search_query:`/`search_document:` prefixes** (see fix R3).
4. **Agentic safety net.** `search_docs`/`ask_docs` are called by an LLM that can re-query when results are thin.

---

## Backend-agnostic RAG fixes (from the audit — apply regardless of backend)

These are correctness/quality fixes; most are inherited or simplified by the LanceDB move.

- **R1 — Cosine math (Redis only).** Resolved for the optional Redis backend: `redis_client.py` converts RediSearch COSINE distance to similarity with `clamp(1 - distance, 0, 1)`, and `tests/unit/test_redis_client.py` covers both document and tool search scoring. *N/A under LanceDB* — it returns distance/score directly.
- **R2 — OpenAPI specs missing from the index.** Resolved by design: specs go
  to SQLite structured lookup, not the vector index. The current exact index
  contains 4,106 endpoints, 8,890 schemas, and 50,675 fields,
  plus 104 advisories and 346 lifecycle records.
- **R3 — nomic task prefixes.** Resolved in `embed_document()` and
  `embed_query()`: passages use `search_document:` and queries use
  `search_query:` consistently.
- **R4 — Batched embeddings.** The default embedded path batches through fastembed (ONNX), and the optional Redis/Ollama path uses Ollama `/api/embed` with `{"input":[...]}` before falling back to legacy `/api/embeddings`. Full re-ingests now use batched embedding paths instead of serial per-chunk requests.
- **R5 — Hybrid + rerank.** Native in LanceDB (`.search(..., query_type="hybrid")` + a reranker; RRF default). Replaces the brittle static `_SOURCE_BOOST`.
- **R6 — Chunking.** The prose build uses header-aware chunking with bounded
  overlap. OpenAPI parameter tables and enums are not chunked because they are
  parsed into exact SQLite records.
- **R7 — `ask_docs(question)` tool.** Retrieve hybrid top-k → synthesize a **cited** answer with a small local model → return `{answer, citations}`. Keeps `search_docs` for raw chunks; cuts the per-question token cost the CLAUDE.md RAG-first rule otherwise forces.

---

## Target module layout

```
src/hpe_networking_mcp/pipeline/clients/
  lance_client.py     # open table, hybrid search(query, k, source_filter) -> hits for the default embedded path
  embed_client.py     # fastembed wrapper: embed_document(list) / embed_query(str); model nomic-embed-text-v1.5
  specs_index.py      # build + query SQLite over OpenAPI specs: get_endpoint / get_schema / get_field / get_enum
src/hpe_networking_mcp/mcp_servers/
  rag.py              # search_docs (hybrid) + ask_docs (cited) + lookup_api (exact)  — all READ_ONLY
ingestion/
  ingest_docs.py      # chunk prose -> embed_document -> LanceDB ; parse specs -> SQLite ; emit prebuilt artifacts
data/                 # prebuilt, shippable: docs.lance/  +  specs.sqlite   (attach to GitHub Release)
```

No `docker-compose.yml` requirement for the default path. `redis-stack` stays
available as an optional localhost-only "Server backend" for power users, with
Docker named volumes so Redis/Ollama state does not clutter the repository.
Compose is allowed to generate project-scoped container names, which avoids
container-name collisions when multiple local checkouts are tested side by side.

---

## Implemented migration sequence

This sequence is complete for the default local path. Redis remains optional; Qdrant is historical context only.

1. Add deps: `lancedb`, `fastembed`; keep `redis` only for the optional server backend.
2. `embed_client.py` (fastembed, `nomic-embed-text-v1.5`, `embed_document`/`embed_query` with prefixes — R3).
3. `lance_client.py`: create a hybrid table (vector + FTS on `text`), `search()` with `source` filter + reranker (R5).
4. `specs_index.py`: parse `ingestion/sources/openapi_specs/*.json` → SQLite (`endpoints`, `schemas`, `fields` tables) with FTS; query helpers (R2 resolved).
5. Rewrite `ingest_docs.py`: prose → LanceDB (header-aware chunking R6, batched embeds R4); specs → SQLite. Emit `data/docs.lance` + `data/specs.sqlite`.
6. `rag.py`: `search_docs` (hybrid), `lookup_api` (exact), `ask_docs` (cited R7). Point `tool_router`'s `aruba_tools` index at LanceDB too.
7. Re-ingest once; run the eval harness (below) to confirm quality ≥ current.
8. Keep generated `data/*` out of git; use release assets or local ingest for prebuilt indexes. Redis remains an optional backend.

---

## Eval harness (measure "is it selecting correct info" — before/after)

A small, labeled question set + runner so the backend swap is **proven**, not asserted. Lives at `tests/eval/`.

- `tests/eval/rag_eval.yaml` — 33 questions, each tagged `api-lookup` (expects an exact field/enum/endpoint via `lookup_api`), `howto` (expects a prose chunk via `search_docs`), or one of the structured tags (`advisory`, `lifecycle`, `list-advisories`, `list-lifecycle`, `correlate`, `diagnostics`), with `expect_sources` (file_path substrings) and `expect_keywords`.
- `tests/eval/run_eval.py` — calls the RAG tools, computes **recall@k**, **source-hit@k**, and keyword presence; prints a per-question pass/fail table and an aggregate score. Run before and after migration; require no regression.

Metrics: `recall@5` (did an expected source appear in top-5), `mrr` (rank of first correct), `api_exact` (did `lookup_api` return the exact enum/field). Target: api-lookup `api_exact` = 100% (it's structured), howto `recall@5` ≥ today's baseline.

**Baseline measured 2026-06-03** (historical Redis, vector-only, no prefixes,
specs missing from index), then re-measured after wiring `lookup_api` and the
embedded LanceDB design. The current release gate is measured on the full
33-question set with `uv run --with pyyaml python tests/eval/run_eval.py`:

| Metric | Baseline (Redis, vector-only) | After `lookup_api` (2026-06-03) | **Current: embedded LanceDB hybrid (33 questions)** | Target |
|---|---|---|---|---|
| `howto_recall@k` (prose) | 0.80 | 0.80 | **1.00** | ≥ 0.85 ✅ |
| `api_exact` (API lookups) | **0.50** | 0.90 | **1.00** | ≥ 0.95 ✅ |
| `structured_exact` / `structured_list_exact` | — | — | **1.00** / **1.00** | 1.00 ✅ |
| `source_hit@k` (overall) | 0.50 | 0.80 | **0.97** | ≥ 0.85 ✅ |
| `mrr` | 0.339 | 0.679 | **0.923** | ≥ 0.85 ✅ |
| `keyword_hit` | — | 0.80 | **1.00** | — |

**Current evaluated corpus:** 126,292 prose chunks across the released
documentation sources (see [`docs/project-facts.json`](../project-facts.json),
regenerated by `scripts/project_facts.py` -- never hand-entered). The 5,419
OpenAPI vector records from the pre-LanceDB build were intentionally removed
because structured API lookup is authoritative. The rebuilt SQLite index
contains 4,106 endpoints, 8,890 schemas, 50,675 fields, 104 advisories, and
346 lifecycle records. The rebuilt router index contains 6,717 backend
tools. Minimal mode keeps this
catalog behind the three-tool discovery/dispatch surface; direct-all mode
exposes 6,724 tools including the router itself. The current 33-question eval
set (expanded from 24 to add structured list/correlate/diagnostics and
negative coverage-gap questions) resolves 32 of 33 questions from an expected
source, 30 of them at rank 1 (`source_hit@k` 0.97, `mrr` 0.923). Standard catalog
profiles contain 370 core tools / 2831 read-only optional starters / 5811 read-write optional starters; those optional profiles now map to
`safe-read-only` and `full-read-write`, respectively. The complete index also enables generated GLP.

Tracked RAG refresh targets live in `ingestion/source_manifest.json`. The
current manifest covers 23 rebuild sources, including DevHub, Switching Feature
Navigator, Juniper EX-series/AP product datasheets, the complete HPE Aruba
Networking CSAF advisory archive, HPE Networking end-of-sale notices, official
Mist/Apstra lifecycle and security-advisory pages, full-history AOS-CX switch
and Mist cloud-platform release notes, AOS-CX Fundamentals/CLI Reference
guides, the standalone ClearPass Policy Manager admin guide, and Juniper
EX-series hardware install/maintenance + EX-tagged Junos release notes. Keep
those inputs represented in local
`ingestion/sources/` before packaging public RAG indexes.

The API-lookup rows almost all missed the spec sources at baseline — direct empirical evidence of **R2** (OpenAPI specs absent from the active index). `howto` retrieval is already decent, confirming the redesign's value is concentrated in (a) structured API lookup and (b) hybrid+rerank for exact identifiers, not in replacing vector search wholesale. Re-run `uv run --with pyyaml python tests/eval/run_eval.py` after each change.

The historical `mac-reg-update-url` miss is closed. The Central NAC Service
spec (cnac-mac-reg, visitor, named MPSK, DPP, certificates, and jobs) resolves
from the reference page's `oasPublicUrl` through the ReadMe API registry.
`ingestion/scrape_cnac_spec.py` writes `cnac-client-registration.json` plus
provenance metadata for the current 239-spec rebuild. With it indexed,
**`api_exact` = 1.00**: all API-lookup evaluation questions resolve through
`lookup_api` without prose fallback.

---

## v0.7 — structured security/lifecycle intelligence expansion

Building on the exact `lookup_advisory`/`check_product_lifecycle` tools and
the content-hash incremental LanceDB ingest, `rag-core` (`src/hpe_networking_mcp/mcp_servers/rag.py`)
adds four more tools, all bounded and read-only, backed by
`src/hpe_networking_mcp/pipeline/clients/advisory_index.py` and `src/hpe_networking_mcp/pipeline/clients/rag_diagnostics.py`:

- **`list_advisories` / `list_lifecycle_events`** — paginated (`limit` ≤ 200,
  plus `offset` and a `total_matched` count) listing with exact filters:
  product/model text, CVE, advisory/notice ID, severity floor, product/
  replacement SKU, category, event type, an authoritative `source_family`,
  and a `[since, until]` date range parsed only from known-exact formats
  (`YYYY-MM-DD` or the legacy notices' `Month D, YYYY`) — an unparseable date
  excludes a record from range filtering rather than guessing at it. These
  complement (not replace) the identifier-required `lookup_advisory`/
  `check_product_lifecycle`.
- **`correlate_advisory_lifecycle`** — links an advisory's listed products to
  lifecycle records only on exact, normalized (case/whitespace-only) string
  equality against `product_skus`/`replacement_skus`. Every response carries
  an explicit `match_basis` string and separates `exact_matches` from
  `unresolved_products` — there is no fuzzy/semantic scoring, and an
  unresolved product is never presented as "not affected". Empirically, real
  current advisory product names largely do not literally match the legacy
  lifecycle archive's SKUs, which is expected given the current-Aruba
  lifecycle coverage gap below — most correlations report `unresolved`
  rather than a match, and that is the honest answer.
- **`rag_diagnostics`** — combines three read-only, network-free checks
  scoped to the security-advisory/lifecycle sources: `citation_completeness`
  (per `source_family`, what fraction of records have populated
  `source_url`/severity/date/SKU citation fields — this is how the Juniper
  Mist/Apstra table-rendered pages' 0%-populated severity/date/SKU fields
  are surfaced, rather than silently returned as `null`), `source_freshness`
  (reduces the `source_freshness_result` artifact from
  `scripts/check_security_lifecycle_drift.py` to per-status counts, via
  `src/hpe_networking_mcp/pipeline/artifact_contracts`), and `ingestion_delta` (new/changed/removed/
  unchanged content-hash counts versus the current LanceDB `docs` table,
  reusing `ingestion/ingest_docs.py`'s `collect_points`/`content_hash` purely
  as a diff — no embedding, no writes).
- **`ask_docs`** now recognizes a literal CVE ID or vendor advisory ID in the
  question and routes to `lookup_advisory` first (exact), the same way it
  already routes API-shaped questions to `lookup_api` — never a guessed
  product-name filter. Citations were also extended to include `status`,
  `category`, `event_type`, and bounded (≤5) `cves`/`product_skus`/
  `replacement_skus` lists when the underlying record has them.

The eval harness (`tests/eval/rag_eval.yaml` + `tests/eval/run_eval.py`) grew
from 24 to 31 questions to cover this: two negative queries (a nonexistent
CVE, a nonexistent SKU), one explicit current-Aruba-lifecycle coverage-gap
check (querying a real current AP model correctly returns empty, not a
fabricated "still supported"), and one `list-advisories`/`list-lifecycle`/
`correlate`/`diagnostics` row each. A row tagged `expect_empty: true` scores
correctness on emptiness rather than keyword/source presence — a fabricated
non-empty answer to a negative/coverage-gap query is a failure, not a pass.
A new `structured_list_exact` metric (alongside the existing
`structured_exact` for `lookup_advisory`/`check_product_lifecycle`) tracks
the four new structured tool types separately so neither dilutes the other's
baseline expectation.

See also [Source coverage, freshness, and provenance](../source-lifecycle-coverage.md)
for the current-Aruba-lifecycle coverage gap this correlation/diagnostics
work deliberately does not paper over.

---

## Original open questions and current defaults

These questions were captured during the migration decision. The current repository defaults are embedded LanceDB + SQLite, `fastembed`, release/ignored `data/*` indexes, and Redis as an optional server backend.

1. **Embedding model:** keep `nomic-embed-text-v1.5` (via fastembed) for identical semantics, or move to `bge-base-en-v1.5`? (Both good; nomic = no quality change, just drops Ollama.)
2. **Ship prebuilt index in-repo or as a Release asset?** Release asset keeps the repo small; in-repo is zero-step but bloats clones.
3. **Keep a Redis "server option" appendix**, or go all-in embedded and remove Redis entirely? Current default: embedded LanceDB + SQLite, with Redis still available as an optional backend.

---

## Source freshness checking and scaffolding new sources

Added after the initial corpus build to address two gaps: no way to detect
when upstream docs/specs changed since the last scrape, and no consistent
way to register a brand-new source across `source_manifest.json`,
`ingest_docs.py`, and `rag.py` without drift.

**Freshness detection (`ingestion/check_updates.py`)** is tiered per known
URL, not a blind full re-scrape:

1. A conditional `GET` carries the last stored `ETag`/`Last-Modified`
   validator. A `304` response is the cheap path (server declines to
   resend the body) — marked unchanged, no hashing needed.
2. Any other response (200, or a site with no conditional-GET support) falls
   back to a SHA-256 content-hash compare against the last stored hash —
   this is the authoritative signal, since several docs sites (readme.io,
   Hugo Doks) don't implement conditional requests reliably.
3. Sites that block plain HTTP clients outright (403/406 — already
   documented per-source as needing Playwright) are reported as `blocked`
   rather than silently skipped or treated as unreachable errors.

Per-URL state (`etag`, `last_modified`, `content_hash`, timestamps) persists
in `data/source_state.sqlite` via `src/hpe_networking_mcp/pipeline/clients/source_state.py` — same
connection/schema conventions as `src/hpe_networking_mcp/pipeline/state_store.py`.

Known-URL resolution deliberately reuses what already exists instead of a
new registry: the `<!-- source: URL -->` header every prose scraper
(`scrape.py`, `scrape_nac_docs.py`, `scrape_vsg.py`, `scrape_techdocs_pw.py`)
already writes into scraped files, plus each source's `url_seed_file` when
present (`techdocs_paths.json`), plus the committed manifest resolvers for
the two spec sources: `openapi_specs` resolves to each ReadMe registry's
reference page **and** its `dash.readme.com` api-registry document from
`ingestion/openapi_registry_manifest.json`, and `product_specs` resolves to
the api-next reference pages recorded in
`ingestion/product_specs_manifest.json`. The former filename-based
reconstruction of `internal-ui.central.arubanetworks.com/cnxconfig/docs/*`
URLs was removed: that host was retired by the July 2026 SuperHub migration,
so every one of those checks was a dead-host error being counted as
freshness signal. Sources with no resolvable inventory yet (`aos_techdocs`'s
ephemeral `/tmp/aos_*_urls.json`, `feature_navigator` with no scraper) are
reported with a concrete next step instead of failing.

Every per-URL outcome is also classified with the shared drift taxonomy
(`fresh` / `content_drift` / `source_added` / `source_removed` /
`unavailable` / `not_checked`), so a blocked or unreachable page can never be
counted as a content change -- see
[source/API/RAG drift gates](../source-drift-gates.md) for the full class
list, exit codes, and precedence.

**Orchestration (`scripts/refresh_rag_sources.py`)** is declarative over the
manifest (`scraper` + `extra_scripts` + `extra_script_phases`, which orders
URL-discovery scripts before their scraper) plus a declared table of structured
steps (security/lifecycle scrape, api-next product specs, generated-tool
manifest validation, docs/specs rebuild, tool-catalog rebuild, local manifest
reconciliation, and the existing eval gate `tests/eval/run_eval.py --ci` that
`validate_release.py` also uses). `--plan`/`--dry-run` prints the whole plan
as JSON without executing a command or opening a socket, and fetching steps
require an explicit `--refresh-sources`; a freshness check that could not
complete (`unavailable`/`parser_error`/usage, malformed JSON) fails closed and
plans nothing rather than refreshing from a partial result. The transaction now
covers
`data/docs.lance`, `data/tools.lance`, `data/specs.sqlite`, the generated
operation manifests, and both `data/*-MANIFEST.json` files: any failing step
restores all of them as a unit — a refresh can never leave a worse index, or
a half-updated set of manifests, in place than what was there before it ran.

This is intentionally local-only (no GitHub Actions/CI component) — the index
it rebuilds lives only on the machine that runs it, so scheduling belongs
there too. `scripts/schedule_freshness_check.sh install` registers a weekly
check, preferring a launchd agent and falling back to crontab when
`~/Library/LaunchAgents` is not writable (it is root-owned on MDM-managed
Macs). `status`, `run`, and `uninstall` round out the interface; installs are
idempotent.

**Adding sources (`scripts/add_rag_source.py`)** is an interactive wizard
supporting 4 scraper strategies (`simple_http_pandoc`, `playwright_headless`,
`openapi_json`, `sitemap_crawl`), each templated from the corresponding
existing scraper pattern in this repo. It writes the new
`ingestion/scrape_<key>.py`, appends the manifest entry, and best-effort
AST-patches `ingest_docs.py`'s `SOURCE_META` and `rag.py`'s
`_DOC_TYPE_TO_SOURCE` dict literals — falling back to printed manual-edit
instructions when a file's structure isn't a plain dict literal the patcher
can safely edit.

`scripts/validate_source_manifest.py` is the drift check: every entry's
`scraper` path must exist, `output_dir` must follow the
`ingestion/sources/<source>` convention, every source must have a matching
`SOURCE_META` key, and source keys must be unique (`_DOC_TYPE_TO_SOURCE`
gaps are WARN-only, since not every source needs custom rank boosting).

**Worked example — Juniper Mist-family sources:** added via this tooling to
extend the corpus beyond Aruba/HPE. `mist_docs` uses a variant of
`sitemap_crawl`: `discover_mist_docs_urls.py` walks juniper.net's public
sitemap index (`documentation/sitemap/sitemap.xml` → ~8 per-URL sitemaps),
filters to 5 path prefixes covering the whole Mist/Marvis product family —
`/documentation/us/en/software/mist/*`, `/jvd/*` (Juniper Validated
Designs), `/hpe-mist-networking-data-center-assurance/*`,
`/juniper-data-center-assurance/*`, and `/juniper-routing-assurance/*` —
and excludes the JS-only API reference widget path and unrelated Juniper
product lines (Junos core OS, Contrail, session-smart-router, vSRX, etc.,
~32k pages, explicitly out of scope), then `scrape_mist_docs.py` fetches
each page with plain `urllib` + pandoc (confirmed server-rendered DITA
HTML — no Playwright needed here, unlike the Aruba techdocs/AOS sources),
using a 4-worker pool with a 0.4s per-request delay to stay polite/avoid
IP bans, and extracts the `<div class="topicBody">` article container to
strip nav/TOC/survey-banner noise. The Mist OpenAPI 3.0 spec
([mistsys/mist_openapi on GitHub](https://github.com/Mist-Automation-Programmability/mist_openapi))
is pulled by `scrape_mist_openapi.py` directly into the existing
`openapi_specs` output folder (registered as an `extra_scripts` entry, same
pattern as `scrape_cnac_spec.py`) so it flows through the existing
`collect_openapi_points()` → `specs_index.py` exact-lookup pipeline
unchanged. Verified end-to-end: 2,014 doc pages discovered (2,011 scraped
OK, 3 stale-sitemap 404s) across mist + jvd + the 3 Mist/Marvis-based
assurance products, plus the 2.6&nbsp;MB Mist API spec file.

**Worked example — Aruba Central Config API (public dev-portal
workaround):** the original `scrape_openapi.py` target
(`internal-ui.central.arubanetworks.com`) resolves to a private/internal
AWS ELB IP and is unreachable outside HPE's corporate network. Instead,
`ingestion/scrape_config_reference_specs.py` discovers all
`developer.arubanetworks.com/new-central-config/reference/<slug>` pages
(1,561 slugs from the reference index), and for each page extracts the
embedded `"schema":{"components":...}` JSON fragment readme.io ships
inline (a full self-contained OpenAPI document per functional *category*,
e.g. Security, Wireless, VLANs & Networks — not one file per slug/endpoint,
since many slugs share the same category bundle). Runs single-threaded
with a mandatory 1.5s delay between requests, de-duplicates bundles by
`info.title`, and checkpoints progress every 25 slugs (resumable via
`--resume`). Verified end-to-end: all 1,561 slugs fetched (1 transient
timeout), yielding 25 unique category bundles — these plus the Mist spec
and CNAC spec bring `openapi_specs` to 27 files / 5,834 chunks (2,390
endpoints, 5,191 schemas, 27,377 fields in `specs.sqlite`). Because the
content now ships as per-category bundles rather than per-slug files, a
few `tests/eval/rag_eval.yaml` `expect_sources` entries were updated to
match the new (correct) bundle filenames — content correctness
(`keyword_hit`/`api_exact`) was unaffected throughout.



## Vendor-aware source boosting

`_SOURCE_BOOST` ranks sources by authority so that, when relevance scores are
close, the more definitive source wins. That ordering only holds *within* a
vendor, and the corpus now spans two.

The failure this caused was concrete: asking "switch template multicast IGMP
snooping fields" returned `ArubaMgmdInterface_MgmdSnoopingEthConfig` — an Aruba
schema — ahead of every Juniper source. `openapi_specs` carries the highest
boost (0.16), and because every OpenAPI file ingests under that single source
label, Aruba's 26 specs and Juniper's `mist.openapi.json` were indistinguishable
to the re-ranker. The Aruba spec won on boost alone.

Two mechanisms fix it, both in `src/hpe_networking_mcp/mcp_servers/rag.py`:

- `_boost_key()` keys the boost table by filename rather than source label for
  OpenAPI hits, so the Mist spec draws Juniper's weight (`mist_specs`) without
  needing the corpus re-ingested under new source folders.
- `_detect_vendor()` infers the queried vendor from brand-specific tokens. When
  exactly one vendor is named, hits from the other forfeit their boost and take
  `_CROSS_VENDOR_PENALTY`.

Three deliberate constraints:

1. **Demote, don't filter.** Vendor detection is a heuristic and migration
   material legitimately cites both vendors, so a strong cross-vendor match can
   still surface.
2. **Ambiguity disables gating.** A query naming both vendors or neither is left
   ungated — comparisons and generic networking questions should rank on
   relevance alone rather than on a guess.
3. **Whole-token matching only.** Generic networking terms say nothing about
   vendor, and substring matching would read "ex" out of "excessive".

Every manifest source needs a `_SOURCE_VENDOR` entry: gating silently no-ops for
sources it does not know, so an unmapped source quietly opts out of cross-vendor
ranking. `tests/unit/test_rag_source_boost.py` asserts the manifest and the
vendor map stay in sync.

Juniper prose (`mist_docs`) is deliberately left unboosted. Giving it the 0.10
that `developer_docs` carries measurably cost eval mrr (0.654 → 0.629), because
the fixtures are Aruba-only — that boost's harm is measurable while its benefit
is not. Within a Juniper query the ordering that matters, `mist_specs` above
`mist_docs`, already mirrors Aruba's `openapi_specs` above `tech_docs`.
