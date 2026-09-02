---
title: "Juniper/Mist SKU, citations, and JVD workflow"
nav_order: 4
parent: "Reference"
---

# Juniper/Mist SKU, citations, and JVD workflow

This page is the operating guide for the current structured hardware lookup
and the planned Juniper Validated Design (JVD) integration. Keep it updated
in the same change as new tools, fields, source rules, or artifacts.

## Choose the right path

| Request | Use | Why |
|---|---|---|
| Exact part number, alias, or SKU | `search_hardware_catalog` | Deterministic local SQLite lookup; no RAG or vendor call |
| Select a device from a requirement | `search_hardware_catalog` | Bounded, ranked candidates with explicit ambiguity |
| Compare known devices | `compare_hardware` | Normalized fields and explicit unknowns |
| Explain a feature or broad specification | `ask_docs` / `search_docs` | Prose RAG is appropriate for narrative context |
| Choose a validated Juniper architecture | JVD structured index (local module; MCP tool exposure planned) | JVD ties use case, topology, products, versions, and configs together |
| Assemble a BOM + JVD reference + topology for review | `design_bundle.build_design_bundle` (module; MCP tool planned) | Deterministic composition with explicit official/operator_input/derived/unknown field labels |
| Check whether a design bundle is safe to present as ready | `artifact_validation.validate_for_review` (module; MCP tool planned) | Fail-closed blocking checks vs advisory-only judgment calls |
| Produce an editable topology drawing | `design-core` exporters | Local Draw.io, Graphviz, or NeXt UI artifacts |
| Push configuration to a device | Platform backend only, separately gated | Not part of the catalog/JVD review workflow |

Do not use RAG as the primary resolver for an exact SKU. Embeddings can find
related prose but cannot reliably distinguish ordering variants, regional
suffixes, power options, or products absent from a partial snapshot.

## Exact catalog calls

In default/direct router mode, call the convenience tool directly:

```json
{
  "query": "EX4400-48P",
  "include_specs": true
}
```

For a requirement with multiple valid choices:

```json
{
  "query": "48 port PoE switch",
  "vendor": "juniper",
  "limit": 5
}
```

In the recommended `minimal` router profile, discover and invoke it without
exposing the entire backend catalog:

```text
find_tool("search hardware catalog Juniper EX4400 48 port PoE")
invoke_read_tool("search_hardware_catalog", {
  "query": "EX4400 48 port PoE",
  "vendor": "juniper",
  "include_specs": true
})
```

The result is bounded to five candidates. An exact SKU or alias returns
`match_type: exact_sku`; a requirement returns `match_type: candidate`; no
result returns `match_type: no_match` with guidance. Do not silently choose
among ambiguous variants.

Compare only after selecting distinct SKUs:

```json
{"devices": ["EX4400-48P", "EX4400-48MP"]}
```

A comparison returns `comparison.fields`, including `different: true` when
values differ. Missing values are `unknown`, not inferred. A model family with
multiple products returns `needs_selection` and candidates instead of inventing
a SKU.

## How to read citations

Every catalog result carries:

- `source.url` and `source.title`: the official product source used for the
  snapshot; only Aruba/HPE/Juniper domains are accepted.
- `source.snapshot_at`: when the reviewed record was captured.
- `source.status`: `verified` or `stale`; stale means the last verified record
  is retained because the source could not currently be refreshed.
- `catalog.coverage`: currently `partial`; it must never be described as a
  complete current catalog.
- `lifecycle.status` and `lifecycle.evidence` when the official source
  publishes lifecycle information; otherwise status remains `unknown`.

Treat `source` as identity/product provenance, not proof that a design is
appropriate. A future JVD result will add separate `design_source` citations.
RAG citations explain prose and must not overwrite either source.

## JVD design/build boundary

The official [Juniper JVD repository](https://github.com/Juniper/jvd) is a
validated-design source, not just another prose corpus. Its common structure
contains:

- design READMEs with use cases, hardware tables, topology, and software
  assumptions;
- `configuration/` trees containing `conf`, `set`, and sometimes `apstra`
  artifacts;
- parameterized `snips/` with variables and pairing metadata; and
- portal indexes such as `jvds.json`, `jvd-readmes.json`, and `snips.json`.

### Current status: structured local index (module, not yet an MCP tool)

`src/hpe_networking_mcp/pipeline/clients/jvd_catalog.py` indexes a
commit-pinned snapshot of the official portal catalog
(`portal/src/data/jvds.json`) into a local, read-only SQLite index. Build it
with:

```bash
uv run python scripts/build_jvd_index.py
```

This produces `data/jvd_index.sqlite` (git-ignored, like the hardware
catalog index) from the reviewed seed
[`ingestion/jvd_seed.json`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/ingestion/jvd_seed.json) and its source policy
[`ingestion/jvd_manifest.json`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/ingestion/jvd_manifest.json). No network
call happens at build or query time; the seed is a committed, reviewed
snapshot pinned to a specific JVD commit SHA.

Query it directly today with `search_designs`:

```python
from hpe_networking_mcp.pipeline.clients import jvd_catalog

jvd_catalog.search_designs("EVPN VXLAN data center fabric")
jvd_catalog.search_designs("", area="Security")
```

Each result includes `id`, `name`, `area`, `description`, `platforms`, `os`,
and `source.repo_path`/`source.url` (an exact GitHub path pinned to the
snapshot commit), plus a `provenance` block with `identity_authority`,
`source_repo`, `source_commit`, `source_license`, `coverage`, and
`coverage_note`. A query with no match returns `match_type: no_match` with
guidance to add an area or platform/use-case keyword — it never guesses.

**Known coverage gap:** the JVD portal's structured catalog covers Data
Center, Enterprise WAN, Optical, Security, and Service Provider (21 designs
at the pinned commit). It does **not** include Campus or Branch as
structured entries — those areas are documented only as external pages on
`juniper.net/documentation/validated-designs/`, outside this repository's
portal data. A `search_designs` query for campus/branch topics correctly
returns `no_match` rather than a false positive; this gap is called out in
`provenance.coverage_note` on every response.

Exposing `search_designs` as an MCP tool (e.g. `search_jvd_designs`) is the
next planned increment. This repository tracks an exact total registered
tool count in `project_facts.py`/`project-facts.json` and mirrors it across
several docs and tests, so adding a new backend tool requires running the
facts-regeneration step alongside the change — track that as its own
reviewed slice rather than folding it into index-building work.

### Planned end-to-end flow

The planned integration should index those structures deterministically and
retain the repository commit/version and source path. The intended flow is:

```text
requirements
  -> exact Juniper/Mist SKU candidates
  -> selected JVD and applicability checks
  -> BOM + assumptions + citations
  -> canonical topology model and editable diagram
  -> parameterized, reviewable config artifacts
  -> validation report
```

The first JVD-backed output is a local review package. It may render configs
based on HPE Juniper-published JVD material, but it must label operator inputs
and derived fields, show unresolved choices, and never perform a live push.
Live changes remain subject to platform write gates, dry-run, and confirmation.

### Current status: design bundle composition (module, not yet an MCP tool)

`src/hpe_networking_mcp/pipeline/design_bundle.py` implements the
BOM + topology + JVD-reference stage of that flow as a pure composition
function, `build_design_bundle()`. It does not resolve ambiguity itself:
callers must already hold an **exact** SKU (from `search_hardware_catalog`)
and, if used, an **exact** JVD design id (from `search_jvd_designs` /
`get_design`). Its only job is deterministic assembly and explicit field
labeling:

```python
from hpe_networking_mcp.pipeline.design_bundle import build_design_bundle

bundle = build_design_bundle(
    title="Branch pilot",
    line_items=[{"role": "access_switch", "sku": "EX4400-24P", "quantity": 2}],
    topology={
        "title": "pilot topology",
        "nodes": [{"id": "acc1", "label": "acc1", "role": "access_switch"}],
        "links": [],
    },
    jvd_design_id="3stage_dc",
)
```

Every field in the result is labeled `official`, `operator_input`,
`derived`, or `unknown`:

- `bom.line_items[*]`: SKU/vendor/model/family/summary/source/lifecycle are
  `official` (verbatim from the hardware catalog's provenance record);
  `role`/`quantity` are `operator_input`; `bom.total_units` is `derived`.
- An unresolved SKU (not an exact catalog match) is never silently dropped:
  it is reported under `unresolved_line_items` with the catalog's
  `match_type`, any candidates, and a warning telling the caller to resolve
  it with `search_hardware_catalog` first.
- `topology` reuses the exact same `design_lib.model.parse_model` validation
  as the `design-core` diagram exporters (Draw.io/Graphviz/NeXt UI), so a
  bundle's `topology` field can be handed directly to those exporters. An
  invalid topology is reported as `topology_error` without discarding an
  otherwise-valid BOM.
- `jvd_reference` (when `jvd_design_id` is given) carries the design's
  `provenance` (pinned commit, license, coverage/coverage_note) alongside
  its `platforms`/`os`/`source`. An unknown id is reported under
  `jvd_reference.ok: false` with guidance, again without discarding the BOM.
  When at least one BOM item resolves, `jvd_reference` also carries a
  `compatibility_check`: an **advisory-only** comparison of BOM SKU
  family/model against the JVD's official `platforms` list. It never blocks
  the bundle; when nothing overlaps it adds a warning telling the operator
  to treat the JVD as directional guidance only, not a literal BOM.
- Every bundle carries a fixed `boundary` string restating that it is a
  **review-only artifact**: no configuration is rendered or pushed to a
  device by this module.

Exposing `build_design_bundle` as an MCP tool is deferred for the same
reason as `search_jvd_designs`: this repository pins an exact global
registered tool count that requires a dedicated facts-regeneration pass
(see `docs/juniper-mist-jvd.md`'s JVD section above and `jvd-mcp-tool` in
the project plan).

### Vertical slice: a real, current worked example

`scripts/demo_jvd_design_slice.py` runs the full chain end to end against
the real committed catalog and JVD seeds (not synthetic fixtures):

```bash
uv run python scripts/build_hardware_catalog.py
uv run python scripts/build_jvd_index.py
uv run python scripts/demo_jvd_design_slice.py
```

It resolves an EX4400-24P requirement to its exact SKU, finds the closest
JVD by keyword (`3stage_dc`, the 3-Stage Data Center EVPN/VXLAN design), and
composes a full BOM + topology + JVD-reference bundle.

**This slice surfaces a real, current gap rather than hiding it:** today's
Juniper/Mist hardware catalog seed only covers Campus/Branch EX-series
access switches, while the JVD structured index's designs are Data
Center/Enterprise WAN/Optical/Security/Service Provider, built around
QFX/PTX/ACX/SRX/MX platforms. The `compatibility_check` on the demo's
`jvd_reference` correctly reports no overlap, and the bundle's `warnings`
tell the operator to treat the JVD as directional guidance only. Closing
this gap requires either catalog expansion into JVD-covered hardware
families or JVD expansion into Campus/Branch (currently unavailable as
structured JVD content -- see the coverage gap noted above) -- tracked as
ongoing work, not a bug in this slice.

`tests/unit/test_vertical_slice.py` is the pinned regression version of
this same flow (building fresh indexes into a temp directory from the real
committed seeds, so it never depends on `data/*.sqlite` already existing).

### Fail-closed review gate

`src/hpe_networking_mcp/pipeline/artifact_validation.py` answers exactly one
question: is a design bundle safe to present to a reviewer as "ready"?

```python
from hpe_networking_mcp.pipeline.artifact_validation import validate_for_review

validate_for_review(bundle)
# {"ready_for_review": bool, "blocking_reasons": [...], "advisory_notes": [...], "checked": [...]}
```

It classifies every problem as one of two kinds:

- **Blocking** (`ready_for_review` becomes `False`): an unresolved BOM line
  item, an invalid topology, an empty BOM, a missing/altered review-only
  `boundary` statement, an unresolved `jvd_reference`, or a BOM line item
  missing its `field_labels`. These represent the artifact being incomplete
  or unsafe to show as finished, not a judgment call.
- **Advisory** (never blocks): a JVD/BOM hardware-family mismatch from
  `compatibility_check`, and the JVD's `coverage_note`. These are real,
  worth a reviewer's attention, but inherently require human judgment about
  applicability -- they are not treated as defects in the artifact itself.

`scripts/demo_jvd_design_slice.py` runs this gate as its final step; the
current EX4400-24P / `3stage_dc` demo returns `ready_for_review: true` with
one advisory note about the hardware-family mismatch. See
`tests/unit/test_artifact_validation.py` (10 tests) and the review-gate
regression case in `tests/unit/test_vertical_slice.py`.

## Refresh and coverage rules

Build the current local catalog with:

```bash
uv run python scripts/build_hardware_catalog.py
```

The seed and source policy live in
[`ingestion/hardware_catalog_seed.json`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/ingestion/hardware_catalog_seed.json)
and
[`ingestion/hardware_catalog_manifest.json`](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/ingestion/hardware_catalog_manifest.json).
Use official sources only. If a source is unavailable, retain the last
verified snapshot and mark it stale; never substitute a reseller.

Build the JVD index the same way:

```bash
uv run python scripts/build_jvd_index.py
```

It follows the same rule: the seed pins an exact JVD commit SHA, reports
`coverage`/`coverage_note`, and must retain the previous verified index on a
future refresh failure rather than silently dropping designs. Refresh
deliberately re-pins the commit; it never tracks `main` implicitly. Do not
commit scraped vendor prose or unreviewed customer configurations.

## Documentation contract

When adding a feature, update this page or the most specific canonical page in
the same change. Include the MCP tool name, arguments, one copyable call,
response fields, source/citation meaning, freshness and coverage limits,
output paths, and safety boundary. Add a regression fixture when a documented
field or routing rule is behaviorally important.
