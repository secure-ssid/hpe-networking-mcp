---
title: "0.10.0"
nav_order: 1
parent: "Releases"
---

# hpe-networking-mcp 0.10.0 - offline-first lookup, deny-by-default writes, and gates that check what they claim

Version 0.10.0 is the first published release after 0.8.0. It makes the
structured API corpus work on a clean clone with no network, denies platform
writes unless they are asked for, removes the standalone client in favour of a
real MCP host, and replaces a set of CI gates that reported success over
comparisons they never made.

**0.9.0 was never published as a GitHub Release.** If you are upgrading from
0.8.0 you receive its changes too — see [Upgrading from 0.8.0](#upgrading-from-080)
below.

## Highlights

### Offline API lookup — the OpenAPI corpus ships in the repository

The New Central and Juniper Mist OpenAPI corpora are vendored into
`vendor/openapi/`, digest-pinned in `vendor/openapi/MANIFEST.json`, and the
spec index is built from those committed documents rather than fetched. A clean
checkout with no network and no local build can answer `lookup_api`.

- **31 vendored specs, 1,535 API paths**, each with its `source_url`, SHA-256,
  fetch date and upstream registry pin (`vendor/openapi/MANIFEST.json`).
  Licences stay separated: 30 documents are proprietary HPE Aruba Networking
  material redistributed verbatim for reference, 1 is MIT — see
  `vendor/openapi/NOTICE.md`.
- `scripts/build_spec_index.py` builds `data/specs.sqlite` from that corpus
  offline. A missing index now raises one actionable error naming the build
  command instead of degrading silently.
- The index is **baked into the published container image** and published as a
  reproducible `spec-index-<tag>.tar.gz` release archive. The router overlay no
  longer shadows the baked index.
- **That shipped index is roughly a fifth of its former size.** Schema identity
  is a fingerprint that was repeated verbatim on every field row, so a
  200-field schema stored its whole field list 200 times. Storing a 128-bit
  `blake2b` digest of the same serialization instead — `hashlib`, never the
  per-process-salted `hash()`, so builds stay deterministic — took
  `data/specs.sqlite` from **243.2 MB to 45.6 MB** as measured by the change
  that made it (`4cbdccc`), with `idx_fields_identity` alone falling 107.6 MB
  to 3.6 MB. Nothing read the inlined value: every remaining use compares it
  for equality. The image bake precedes that change, so the 0.10.0 image and
  archive are built from the reduced database.

### Writes are denied by default — BREAKING

`HPE_MCP_CENTRAL_WRITES` must be set to a truthy value (`1`, `true`, `yes` or
`on`, case-insensitive) — or `HPE_MCP_ACCESS_PROFILE=full-read-write` — to
expose Central write and destructive tools. Previously Central alone defaulted
to enabled, so
`import hpe_networking_mcp` with no configuration produced a server willing to
dispatch `reboot_device` and `disconnect_client`.

Two adjacent holes closed with it: a backend with **no registered write gate**
now denies writes rather than allowing them, and a dispatcher **re-install that
would drop the write gate** is refused. The three write-gate mechanisms are
pinned to each other by test, so removing one fails the suite instead of
quietly widening the surface.

### The standalone `hpe-mcp` client is removed — BREAKING

Use an MCP host — VS Code/Copilot, Copilot CLI, Claude, or any other stdio or
streamable-HTTP client — against `hpe-mcp-router`. Personal document ingestion
remains available through `scripts/ingest_personal_docs.py`. The environment
variables that only `cli_client/` consumed
(`HPE_MCP_CLIENT_CONFIG`, `HPE_MCP_HTTP_URL`, `HPE_MCP_MAX_RESULT_CHARS`,
`HPE_MCP_SHELL_HISTORY`, `HPE_MCP_AI_PROVIDER`, and the
`ANTHROPIC_*`/`OPENAI_*`/`OLLAMA_*` keys) are gone from the documented set.

### `invoke_tools_batch` — a write-capable batch dispatcher

The mixed read+write sibling of `invoke_read_tool_batch`. Every step dispatches
through the same `_dispatch_tool` path a single `invoke_tool` call uses, so
global read-only gates, per-platform deny-by-default write gates, per-call rate
charging and response bounding all apply per step with no duplicated
enforcement. Destructive steps keep their per-step confirmation elicitation.

`on_error="stop"` (the default) halts at the first non-ok step — including a
gate-blocked write — and reports `halted`/`remaining`; `on_error="continue"`
reproduces the read batch's collect-all semantics. Batch metrics and audit
labels resolve to the **most severe** step, so a batch containing a write is
never labelled read.

### Prometheus text exposition on `/metrics`

The bounded metrics snapshot renders as Prometheus text (version 0.0.4) by
default; the JSON snapshot (`schema_version` 1, still unstable) is retained via
`?format=json` or a JSON-only `Accept` header. Zero new runtime dependencies —
`render_prometheus()` is a pure function over the snapshot dict.

Outcomes and capabilities render as two **separate marginal counter families**:
the registry never stores a joint capability×outcome count, so no joint series
is fabricated. Exclusive per-bin latency buckets convert to cumulative `le`
counts, with `latency_over_max` folded into `le="+Inf"` so `sum(le)` equals
`latency_count`. Route registration and auth posture are unchanged — still
gated by `HPE_MCP_METRICS_HTTP` plus `HPE_MCP_METRICS`, still inheriting bearer
auth and allow-lists. Design and the emitted name set are documented in
[observability.md](observability.md).

### `corpus_provenance` — weigh an answer by what produced it

One read-only `rag-core` tool reporting what backs a result, for both corpora in
one call, because a caller cannot know which one served it. For the committed
spec corpus it answers on a clean checkout with no network: document count, API
paths, fetch dates and the per-licence split, with each document's `source_url`,
SHA-256 and upstream pin available on request (`detail=True`, or `spec=` taking
a `lookup_api` `file_path` verbatim). For the locally built prose corpus it
reports per-source chunk counts and refresh times when the index exists, and
names the command that builds it when it does not.

Three absences stay three distinct answers, each with its own remedy: no corpus,
a corpus whose declared files are gone (a `git restore`, not a re-fetch), and a
corpus with no index built over it. A malformed manifest degrades rather than
raising.

### Errors are envelopes everywhere, and credentials do not ride in them

- **Response envelope on every backend.** A tool that raises now produces the
  same `{ok, status, data, message, tool, platform}` payload a returned error
  dict gets — on every standalone backend chain and through the router alike
  — delivered as an `isError=true` result instead of bare `ToolError` text.
  Elicitation, protocol errors and cancellation still propagate untouched.
- **Credential redaction on the raised-exception paths.** The SDK reframes a
  raise as `ToolError('Error executing tool <name>: <message>')`, embedding a
  bearer credential mid-string where the prefix rule missed it. Both the router
  dispatch path and the standalone middleware `on_error` path now redact before
  the envelope is built, with the two duplicate helpers consolidated into one
  `shared.redact_tool_error_text` — exactly one such regex repo-wide. The mask
  requires 8+ characters so ordinary 401 prose is not over-masked; a shorter
  credential would slip past it, which is an accepted tradeoff because the
  vault tokenizer still catches known secrets.
- **Pooled HTTP clients are drained on shutdown and restart.**
  `aclose_pooled_clients()` had a docstring saying "call from server shutdown"
  and zero production call sites; every transport leaked pooled
  `httpx.AsyncClient` objects to GC, and an in-process restart inherited a
  registry of unclosable dead-loop clients. Every transport now runs under a
  serve-plus-finally-drain wrapper.
- **Reads retry, writes never do.** A bounded retry on safe verbs only:
  429/502/503/504 and transport errors retry up to twice, with exponential
  backoff on a base capped at 8s and ±20% jitter — or, when the response
  carries `Retry-After` (delta-seconds or HTTP-date), that hint as-is, hard
  capped at 60s with no jitter. No write is ever retried. The guardrail is
  structural rather than a policy flag: the helper takes no method parameter,
  and the generic-executor dispatcher routes only a bodiless GET into it, so a
  caller whose verb is decided at runtime cannot re-send a mutation.
- **The token cache fails closed.** An unwritable cache directory raises with a
  remedy instead of silently falling back to the working directory, where a
  token file could leak into a checkout, archive or container layer. Point
  `TOKEN_CACHE_DIR` somewhere writable.

### Container distribution

`main` and `v*` tag pushes build `linux/amd64` + `linux/arm64`, push under a
non-promoted `sha-<short>` tag, apply the fixable CRITICAL/HIGH Trivy policy to
that exact manifest list, and only then promote the digest to `:latest` and the
semver tags via `imagetools`. **A dirty scan never yields a pullable release
tag.** Base images are digest-pinned and inlined so Dependabot can see them
(a `${VAR}` tag is skipped outright by its Docker parser), sidecars are
digest-pinned, and the secrets bridge exports only recognized families,
announcing every fill and skipping unknown `*_FILE` vars loudly.

The README quickstart is now pull-first against
`ghcr.io/secure-ssid/hpe-networking-mcp`, with the `<host>:*` port-wildcard
allow-list form the router's non-loopback bind check actually requires.

### Benchmark harness and CI regression gate

`tools/benchmark`, `tests/benchmark` and a fake Central API substrate land with
a scheduled/per-PR workflow, a recorded baseline, and the methodology contract
in [benchmark-methodology.md](benchmark-methodology.md).

Four gate defects were fixed in the same range, all of the same class — a
comparison that silently passes by not looking. The allowances were wired to
undefined repository variables (GitHub substitutes an empty string, so the
default never fired and `float('')` raised after all scenarios had run — and at
an allowance of 1.0 a deliberately broken scenario still printed "gate
passed"); and `compare_run` defaulted four different missing baseline keys to
permissive values. Strictness is now a diff, not a settings-UI lever.

The scorer's secret-material safety check no longer defaults to hardcoded
literals: the token URL and secret material are required, keyword-only and
fixture-derived, so a fixture bundle that moves the OAuth route or changes the
token value cannot silently stop matching.

### Docs site rebuilt, and prose numbers pinned to generated facts

The Pages site moved off the stock cayman theme onto tag-pinned
just-the-docs — sidebar navigation, search and heading anchors — with every
page carrying `title`/`nav_order`/`parent` frontmatter and the flat page set
organized into Guides, Architecture, Reference, Releases and Archive.

More durably, **hand-maintained restatements of the catalog were replaced with
pointers at `docs/project-facts.json` rather than corrected in place.** Several
had drifted: prose that walked out a 6,726 sum against its own stated 6,728,
a `design-core` row still printing 7 against a tracked 8, a hand-typed 3,156
capability figure against the generated matrix's 3,159, and four enumerations
that omitted the local `corpus_provenance` and `glp_preflight` tools. New tests
assert the facts rather than the sentences — including one that reconstructs
`registered_total` from the published members, and a hero-banner drift guard.

New reference pages: [known-limitations.md](known-limitations.md),
[workflow-authoring-standard.md](workflow-authoring-standard.md),
[benchmark-methodology.md](benchmark-methodology.md),
[observability.md](observability.md), plus
[PRIVACY.md](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/PRIVACY.md)
and a private security contact in `SECURITY.md`.

### First-run diagnosis is state-aware

On a fresh clone every layer used to say "run `ingest_docs.py`" — a command that
ingests nothing without a scraped corpus. `hpe-mcp-doctor` now reports the
structured spec index and the prose index as separate checks, each with the
remedy matching the on-disk state: offline build, `git checkout -- vendor/openapi`
restore, fetch-then-build, or plain build. `search_docs`/`ask_docs` render a
missing prose index as the structured `{error, degraded, hint}` shape
`lookup_api` already used, and the ingest guard names
`scripts/refresh_rag_sources.py --refresh-sources` explicitly.

### Lint, typing and supply-chain gates

- **Strict typing runs in CI on a named, deliberately scoped set.** The
  `Type check (strict, scoped)` job runs `uv run mypy` against the four targets
  `[tool.mypy]` names — `mcp_servers/shared.py`, `mcp_servers/_middleware/`,
  `pipeline/clients/specs_index.py` and `pipeline/project_facts.py` — under
  `strict` with no baseline file, no blanket `ignore_missing_imports`, and no
  `# type: ignore` in the covered set — with one deliberate concession,
  `follow_imports = "silent"`, which lets types flow in from the unchecked
  majority without reporting their errors. The rest of the tree is not
  type-checked and the config does not pretend otherwise: measured 2026-08-21
  across 150 modules, `--strict` reports 397 errors in 71 of them. Widening is
  done one annotated module at a time, never by relaxing the gate.
- **The lint gate looks at the whole tree.** The `Ruff lint` step named about
  twenty paths, so anything added after that list was written was unlinted and
  nothing said so. `ruff check .` surfaced 443 E501 plus 72 autofixable
  I001/F401 findings — all fixed, with **zero exclusions, zero
  `per-file-ignores` and zero added `# noqa`**. The reflow passes compared
  `ast.dump(ast.parse())` before and after and refused to write on any
  difference.
- **The select set widened** to `E,F,I,B,UP,ASYNC`, fixing a further 161
  violations.
- **`uv sync` is frozen on every CI leg**, so a drifting lock fails the build.
  Bandit extends to `scripts/` and `ingestion/` with four audited skips;
  pre-commit runs ruff, gitleaks and bandit locally with fixture-aware
  allowlists.
- **The release gates check what they claim to check.** The spec index was
  built *after* the validation meant to check it, so every release validated a
  checkout with no `data/specs.sqlite` and the published endpoint/schema/field
  counts were never compared against the database the release then archived.
  The enforced tool floor was typed as 6,703 in both workflows while
  `platform_backend_total` said 6,711 — a gate 8 tools more permissive than the
  floor promised to operators. Both are fixed, `--strict-index-facts` requires
  the index rather than passing by not looking, and the drift guard now parses
  `.github/workflows/*.yml` instead of globbing `*.md`.
- **Facts cannot be republished unmeasured.** Index facts split into
  offline-derivable (rebuildable from committed inputs on any clone) and
  locally-built (a property of the machine that ran the generator). On a
  no-data checkout the offline counts are now **omitted rather than carried
  forward**, so a wrong offline number can only enter `project-facts.json` by
  being measured.

### Authorship guards

The commit-authorship guards run at `fetch-depth: 0` in both `ci.yml` and
`release-artifacts.yml`. They previously ran on a default depth-1 checkout,
inspecting a single record and passing vacuously. Three preconditions now guard
the guard: a shallow checkout, an empty record list, and an exemption set that
does not intersect the scanned history each fail on their own line. The
GitHub web-UI committer identity is exempted only when the commit's author is
not itself agent-flagged.

### Cross-platform and Windows correctness

- Relative paths normalize to POSIX at the chunk-id, manifest and tree-digest
  boundaries, restoring reproducible vector-row ids and artifact manifests
  across operating systems.
- Manifest IO pins `utf-8`; the ambient `cp1252` locale used to raise
  `UnicodeDecodeError` out of the router's generated-record path and lose all
  generated-tool provenance. An undecodable manifest is now skipped, not fatal.
- A bounded `os.replace` retry absorbs transient AV/indexer sharing violations
  without weakening the atomic-replacement contract.
- POSIX-only permission assertions skip on Windows, and shell-behavior tests
  gate behind a functional-bash probe rather than the System32 WSL launcher.

### Performance

- The tool catalog embeds on a **capped parallel session pool**
  (`HPE_MCP_INGEST_PARALLEL`, default `min(cpu_count, 8)`); an uncapped
  `cpu_count` default died with an onnxruntime `bad_alloc` at 24 sessions.
- The strict tool-index rebuild is **path-gated** behind a detect job that
  diffs against the merge-base over the paths the index derives from, with an
  `always()` gate job giving branch protection one stable required check.
- The ingestion stack became an optional extra, and `ingest_docs.py` resolves
  the Redis client only when `--backend redis` is chosen — so the documented
  `uv run --extra ingestion python ingestion/ingest_docs.py` no longer dies on
  a `ModuleNotFoundError` before argparse runs.

### RAG evaluation

The eval set expands to 42 questions with graded `nDCG` scoring, `graded_sources`
`{match, gain}` rows, Juniper fixtures grounded in the manifest families, and a
`--min-ndcg` threshold flag with no default bar until measured. A legitimately
`None` opt-in metric now fails the gate cleanly instead of raising `TypeError`.

## Catalog snapshot

Every value below is `docs/project-facts.json` at this release, generated by
`scripts/project_facts.py`, except the two vendored-OpenAPI rows, which are
summed from `vendor/openapi/MANIFEST.json` at the same commit. Locally built
index counts — prose chunks,
advisories, lifecycle rows — are deliberately excluded: they describe the
machine that ran the generator, not the release.

| Artifact | Count |
|---|---:|
| Registered backend tools | 6,728 |
| Platform API backend tools | 6,711 |
| Curated tools | 601 |
| Platform curated tools | 584 |
| Generated tools registered | 6,127 |
| Generated tools excluded | 17 |
| Generated manifest operations | 6,144 |
| Generated manifests | 9 |
| Backends (server ids) | 18 |
| Optional platform products | 8 |
| Declared RAG sources (rebuild manifest) | 31 |
| Vendored OpenAPI specs | 31 |
| Vendored OpenAPI API paths | 1,535 |
| Spec-index endpoints (offline-derivable) | 2,734 |
| Spec-index schemas (offline-derivable) | 6,363 |
| Spec-index fields (offline-derivable) | 31,432 |

Client-visible tool counts by router mode: **minimal 3**, **default 19**,
**direct-all 6,736** (6,728 registered plus 8 router-native tools).

## Validation

- Full unit suite on the `push` event on `main`: **4,955 passed, 5 skipped**
  (Ubuntu, Python 3.12 leg).
- `ruff check .` clean tree-wide, with no exclusions, no `per-file-ignores` and
  no added `# noqa`.
- Benchmark gate passed against the recorded baseline: 28 scenarios,
  `task_success=1.00`, `safety_failures=0`. The golden manifest is replayed by a
  deterministic solver, so this measures harness and gate coherence, not model
  performance.
- The authorship guards ran at full depth over the complete history rather than
  a depth-1 checkout.

## Upgrading from 0.8.0

**0.9.0 was cut in-tree but never published as a GitHub Release.** Its tag
survives only as `archive/release-v0.9.0`, which is **not an ancestor of
`main`**, so the published sequence goes 0.8.0 → 0.10.0 and an upgrading 0.8.0
user receives both sets of changes.

The 0.9.0 work — RAG corpus expansion, ANN and metadata indexes, content-hash
deduplication, bounded LRU caches, `compare_aoscx_releases`, Docker packaging,
the Milvus Lite pilot and the operator runbooks — is recorded in
[release-notes-0.9.0.md](release-notes-0.9.0.md) and is not re-narrated here.
Read that page as part of this upgrade. Its numbers are a point-in-time record
of 0.9.0 and are not the current catalog; use the snapshot above for that.

Between the archived 0.9.0 release commit and the 0.10.0 version bump, `main`
also carried the vendored offline OpenAPI corpus, the image-baked spec index
and its five-fold size reduction, deny-by-default Central writes, the
offline/locally-built facts split, the ingestion-extra refactor and the
resulting retrieval-client split in the published image. Those changes are
described in the Highlights above
and appear in neither the 0.9.0 page nor the 0.9.0 tag.

### Action required

- **Set `HPE_MCP_CENTRAL_WRITES=1`** (or `HPE_MCP_ACCESS_PROFILE=full-read-write`)
  if you relied on Central writes being implicitly enabled; the shipped
  `.env.example` enumerates every router and platform variable.
- **Replace the `hpe-mcp` / `hpe-mcp-client` frontends** with an MCP host
  pointed at `hpe-mcp-router`.
- **Set `TOKEN_CACHE_DIR`** to a writable location if your previous deployment
  relied on the silent working-directory fallback.
- **Use the `<host>:*` port-wildcard form** in `MCP_ALLOWED_HOSTS` for any
  non-loopback bind; bare hostnames make every real `/mcp` request fail with
  421 while `/livez` still passes.
- **Rebuild your image with `--build-arg INSTALL_EXTRAS=ingestion`** if you
  relied on the published image for prose retrieval; the default image ships no
  retrieval client. See
  [docs/production-deployment.md](production-deployment.md) for the supported
  build matrix.

## Known boundaries

- The prose RAG corpus is **not distributed**. It is built locally; the
  license-safe starter is the committed spec index, which `hpe-mcp-doctor` now
  surfaces separately.
- **That bullet is about the corpus; the client is a separate gap.** The
  retrieval clients live in the optional extras (LanceDB in `ingestion`, the
  Redis backend in `redis`), so the default image installs neither and a corpus
  alone will not help. **The mount itself is fine** — Docker creates
  `/app/data/docs.lance` as root, so a RAG-enabled build can bind a corpus
  there — **what is absent is the code that reads it.** Separately, and for a
  different reason, `/app/data` is root-owned `0555` so the runtime user cannot
  swap the baked spec index out.
- `release-artifacts.yml` is `workflow_dispatch` only — releases are
  deliberately operator-triggered and no merge cuts one.
- Milvus Lite remains opt-in, behind LanceDB on hybrid retrieval quality.
- `docs/README.md` is outside `_expected_snippets()`, so its source-count
  figures are not covered by the docs drift guard (capability totals in it
  *are* covered).
- `tools/benchmark` has no unit tests of its own; the gate-branch coverage is
  mutation-probed, not regression-tested.

See the [CHANGELOG](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/CHANGELOG.md)
and the [prebuilt index guide](release-indexes.md) for release assets and
restore instructions.
