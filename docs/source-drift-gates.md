---
title: "Source drift gates"
nav_order: 8
parent: "Reference"
---

# Source, API, and RAG drift gates

Every upstream input this project depends on -- Aruba developer-portal
OpenAPI registries, the api-next product specs, the official Mist OpenAPI
pin, the community `nowireless4u/hpe-networking-mcp` benchmark inputs, the
official security/lifecycle feeds, and the RAG document corpus -- has a
scheduled gate that answers one question: *is what we ingested still what
upstream publishes?*

This page defines the **result classes** those gates share, the exit code
each class owns, and the deliberately-unrefreshed state some pins are in.

> Companion pages: [security/lifecycle source coverage](source-lifecycle-coverage.md)
> for what the official advisory/lifecycle sources do and do not cover, and
> [RAG architecture](architecture/RAG-ARCHITECTURE.md) for how the corpus is
> built.

## Why classes instead of pass/fail

The gates used to collapse everything onto exit code 1. A GitHub rate limit,
a portal layout change, a JSON parse failure, and a genuine spec update all
produced the same red job, so the only safe reaction was "go look" -- and a
network blip was routinely mistaken for upstream drift (and vice versa: a
real change was written off as flakiness).

Two invariants now hold everywhere:

1. **A check that could not complete is never reported as drift.** Transport
   failures and parser failures have their own classes and their own exit
   codes, and they *outrank* content classes when a run produces several.
2. **An unverified pin is never reported as fresh.** If a reviewed pin was
   not re-checked -- because refresh is disabled, or the run was offline --
   it is reported `stale_pin` or `not_checked`, never `fresh`.

## Result classes

Declared once in `src/hpe_networking_mcp/pipeline/drift_taxonomy.py` and used
by every gate.

| Class | Meaning | Exit code | Fails a gate |
|---|---|---|---|
| `fresh` | Checked and provably unchanged against the recorded baseline | 0 | no |
| `content_drift` | Fetched, parsed, and the digest/count really differs | 3 | yes |
| `source_added` | A source/spec/URL exists that the manifest does not record | 4 | yes |
| `source_removed` | A recorded source/spec/URL is gone (404/410, or required-but-absent) | 4 | yes |
| `pointer_change` | A pointer/layout moved (registry id, `spec_uri`/branch, sidebar section, reviewed URL set); content never compared | 5 | yes |
| `stale_pin` | A reviewed pin is behind upstream, or has not been re-verified | 6 | yes |
| `unavailable` | Transient/blocked: network error, timeout, 403/406/429/5xx | 7 | yes |
| `parser_error` | Retrieved, but unparsable (malformed JSON/XML, missing pointer, malformed manifest) | 8 | yes |
| `coverage_gap` | An explicit, documented, already-reviewed limitation | 0 | no |
| `not_checked` | Deliberately skipped: offline/plan-only, no baseline yet, git-ignored artifact absent | 0 | no |

Exit code `2` keeps its existing meaning across the gates: a usage/config
problem (no manifest to check, inconsistent pins). Pass
`--exit-code-mode legacy` to collapse every failing class back onto `1` for
a caller that predates the classified codes.

### Precedence

When one run produces several classes, the exit code is chosen by this
documented order:

```
parser_error > unavailable > pointer_change > source_removed
             > source_added > content_drift > stale_pin
```

"The check could not complete" deliberately outranks "the check found a
difference": an incomplete run must never be summarized as confirmed drift.
Every class still appears in the JSON report regardless of which one decided
the exit code.

## The gates

| Gate | Watches | Notable classes |
|---|---|---|
| `scripts/check_openapi_drift.py` | ReadMe api-registry documents behind `ingestion/openapi_registry_manifest.json` | `pointer_change` when a reference page points at a new registry id; `content_drift` on a spec sha256 change; `source_added` for an undeclared spec file on disk |
| `scripts/check_product_spec_freshness.py` | `ingestion/product_specs_manifest.json` (api-next product specs) | Fully offline: branch vs `spec_uri`, sidebar-section membership, output-path convention, on-disk `path_count`/sha256 |
| `scripts/check_mist_openapi_drift.py` | `ingestion/fetch_mist_openapi.py` + the reviewed pin record `ingestion/provenance/mist_openapi_pin.json` | `stale_pin` when upstream advanced *or* the pin is unverified; `content_drift` when the pinned ref's blob or the local spec no longer hashes to `reviewed_sha256` |
| `scripts/check_nowireless_source_drift.py` | Community benchmark/input pins (GLP vendored specs, Axis platform source, capability-benchmark paths) | `content_drift` per watched path, plus a standalone `coverage_gap` for the official GLP boundary |
| `scripts/check_security_lifecycle_drift.py` | Official Aruba/HPE/Juniper advisory + lifecycle sources | Keeps its five source-local states and maps them onto the shared classes; see [source-lifecycle coverage](source-lifecycle-coverage.md) |
| `ingestion/check_updates.py` | The RAG document corpus, per known URL | `unavailable` for blocked/unreachable pages; `not_checked` for a first-ever baseline run |
| `scripts/summarize_drift_artifacts.py` | The other gates' artifacts | Aggregates every report and reports a check that produced **no** artifact as `missing_artifact` |

Each gate writes a report to `outputs/drift/<check>.json` with the same
shape: `check`, `refresh_sources`, per-class `counts`, `dominant_class`,
`exit_code`, `content_drift_detected`, `check_incomplete`, and a bounded
list of findings.

## Community input vs official API authority

`scripts/check_nowireless_source_drift.py` watches an MIT-licensed
**community** repository. Its pins are reviewed benchmark/input pins, never
API authority. To keep that boundary machine-readable rather than a comment,
the gate emits a *separate* finding:

```json
{
  "boundary_id": "official_hpe_glp_openapi_registry",
  "authority": "official_hpe_greenlake",
  "state": "no_official_machine_readable_registry_tracked"
}
```

classified as `coverage_gap`. HPE publishes no public, reproducible
machine-readable OpenAPI registry for the GreenLake Platform APIs that could
be pinned and diffed the way the Aruba ReadMe registries are, so a GLP API
change that the community repo has not vendored yet is invisible to this
check. Community-input freshness is **not** HPE GLP API freshness, and the
report says so explicitly instead of implying coverage the project does not
have.

## The intentionally unrefreshed state

External source refresh is **disabled** for the current change: nothing was
fetched, no pin was advanced, and no index was rebuilt. The gates report that
honestly rather than papering over it:

* `ingestion/provenance/mist_openapi_pin.json` carries
  `review_status: "review_needed"` and `refresh_policy: "frozen"`. The Mist
  gate therefore reports `stale_pin` (exit 6) -- the pin was *not* re-verified
  against `mistsys/mist_openapi`, so claiming `fresh` would be a lie. Advancing
  it means reviewing the new spec and committing the new ref **and** digest to
  `ingestion/fetch_mist_openapi.py` and the pin record together; no drift run
  may do it automatically.
* `--offline` on any gate reports `not_checked` (or `stale_pin` for a reviewed
  pin), never `fresh`.
* `scripts/refresh_rag_sources.py` requires an explicit `--refresh-sources`
  before any fetching step runs; without it the freshness check runs offline
  and the orchestrator prints its plan instead of executing it.

### How the orchestrator consumes the freshness check

`run_check()` invokes `ingestion/check_updates.py` with classified exit codes
and treats them as two different things:

| Check exit | Meaning | Orchestrator |
|---|---|---|
| 0, 3, 4, 5, 6 | Completed with an actionable classification | Parsed into a refresh plan |
| 7 (`unavailable`), 8 (`parser_error`), 2 (usage) | The check did not complete | **Fails closed** -- plans nothing, propagates the code |
| any other code, missing/malformed JSON, JSON missing `sources`/`changed_sources` | Output cannot be trusted | **Fails closed** |

A report that classifies itself `check_incomplete` is rejected even when its
exit code was actionable, so a partial result can never drive a re-scrape.
`--allow-incomplete-check` is the explicit opt-in for corpora with permanently
blocked pages: it downgrades `unavailable`/`parser_error` to a loud warning,
but never overrides a usage error, an unrecognized exit code, or unusable
JSON.

## Refresh orchestration

`scripts/refresh_rag_sources.py` is declarative and transactional:

* **Declarative.** Every scraper/extra-script step comes from
  `ingestion/source_manifest.json` (`scraper` + `extra_scripts` +
  `extra_script_phases`); the structured steps (security/lifecycle scrape,
  api-next product specs, generated-tool manifest validation, docs/specs index
  rebuild, tool-catalog rebuild, local manifest reconciliation, RAG eval gate)
  come from one declared table. Adding a source, an extra script, or changing
  a phase changes the plan with no code change.
* **Ordered by declared phase.** `extra_script_phases` maps each extra script
  to `pre` or `post` (unlisted defaults to `post`). Per source the plan is
  `pre` extras -> `scraper` -> `post` extras, because the discovery scripts
  (`discover_vsg_urls.py`, `discover_aos_urls.py`,
  `discover_mist_docs_urls.py`) *write* the URL inventory their scraper then
  reads -- running them afterwards refreshes nothing.
  `scripts/validate_source_manifest.py` fails the manifest if a `discover_*`
  script does not declare `pre`, if a phase value is not `pre`/`post`, or if a
  phase names a script that is not in `extra_scripts`.
* **No silent scraper/RAG mapping gaps.** `validate_source_manifest.py` fails
  any source missing a scraper unless the source is explicitly listed in the
  script's `SCRAPER_PENDING` allowlist with a reason. It also fails any
  manifest `doc_type` that is not registered in `rag.py`'s
  `_DOC_TYPE_TO_SOURCE`, or that maps to the wrong source. Shared legacy
  `doc_type` values (for example `security-advisory` and `lifecycle`) must map
  to every source using that tag, so deprecated `doc_type=` filters still
  narrow instead of searching the full corpus.
* **Declared step environment.** A step's required environment is declared
  next to its command, not inherited from whatever the shell exported: the
  tool-catalog rebuild carries the canonical complete-catalog environment,
  including every aligned write gate and generated-tool flag. Without those
  pins the index can be built from a strictly smaller selection and
  `validate_release.py --strict-tool-index` correctly reports it stale
  against the registered catalog.
* **Downstream chain derived last.** Whether the rebuild/gate chain is planned
  is decided after *all* mutating steps are known -- including
  source-triggered structured steps. The shared security/lifecycle scraper
  (`ingestion/scrape_security_lifecycle.py`) refreshes the Aruba and Juniper
  advisory/lifecycle sources, so those sources still get generated-manifest
  validation, both index rebuilds, local manifest reconciliation, and the eval
  gate last.
* **Planned first.** `--plan` (alias `--dry-run`) prints the whole plan as
  JSON -- steps, triggers, unrunnable entries with reasons, snapshot targets
  -- and exits without executing a command or opening a socket.
* **Transactional.** Before execution, all of `data/docs.lance`,
  `data/tools.lance`, `data/specs.sqlite`, the generated operation manifests
  under `src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests`,
  `data/SOURCE-MANIFEST.json`, and `data/INDEX-MANIFEST.json` are snapshotted.
  Any failing step -- including the eval gate -- restores all of them as a
  unit, and an artifact the failed run *created* is removed rather than left
  behind.

## CI layout

The scheduled drift work in `.github/workflows/ci.yml` is one job per check,
never chained in a shell block, so a failing early check cannot hide a later
one. Each job uploads its own JSON report (`if: always()`), and a final
`drift-summary` job (`if: always()`) downloads every report, writes
`outputs/drift/summary.json`, and renders a step-summary table. Checks that
produced no artifact are listed explicitly -- "the job never reported" and
"the job reported no drift" must not look the same.

The strict release/index/test gate (`scripts/validate_release.py`) stays in
its own `test` job and is never merged into a drift job.
