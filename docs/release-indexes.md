# Building the RAG/OpenAPI indexes

The router tool catalog and the exact-API database are derived from OpenAPI
specs committed to this repository, so they rebuild deterministically and are
safe to publish. The RAG prose corpus is different: it is scraped vendor
documentation, and this project does **not** distribute it.

## Current 0.8.0 snapshot

Every number below is derived, not hand-entered: it comes from
[`docs/project-facts.json`](project-facts.json), which
`scripts/project_facts.py` regenerates from the code, the committed
generated manifests, and the local indexes. Regenerate the facts file
whenever an index is rebuilt, and verify with
`uv run python scripts/project_facts.py --require-indexes`.

| Artifact content | Count |
|---|---:|
| LanceDB prose chunks | 392,471 |
| Indexed prose sources | 30 |
| Declared RAG sources | 31 |
| Exact endpoints | 4,106 |
| Schemas | 8,890 |
| Fields | 50,675 |
| Security advisories | 104 |
| Lifecycle records | 345 |
| Generated operation manifests | 6,144 |
| Platform API backend catalog | 6,711 |
| Complete backend catalog (+ Central Streaming, `site-health`, local GLP preflight, `design-core`, `interop-core`) | 6,726 |
| Registered router tool index rows | 6,726 |

The prose index covers 30 of the 31 declared sources: `openapi_specs` is
parsed only into `data/specs.sqlite` (see below), while several declared
sources are metadata/API families or are not present in this local snapshot.
The registered tool index matches the
complete backend catalog exactly: both include the two credential-free
local backends (`design-core`, `interop-core`), which are searchable but
make no vendor API call, matching [`docs/tool-catalog.md`](tool-catalog.md).

OpenAPI documents are parsed only into SQLite exact lookup. They are not
embedded into LanceDB, which keeps prose retrieval smaller and avoids lossy
semantic matching for endpoint paths, fields, and enum values.

## Build the indexes

### Tool catalog (derived from committed code, reproducible)

```bash
uv run python scripts/ingest_tools.py --complete-catalog
```

This regenerates `data/tools.lance` by importing the MCP server modules in
`src/hpe_networking_mcp/mcp_servers/` and walking their tool registries. Every
input is committed Python source, so it needs no network access and no
scraping. CI rebuilds it on each commit and validates it with
`--strict-tool-index`.

### Exact-API database (needs fetched specs)

`data/specs.sqlite` is built from the OpenAPI documents under
`ingestion/sources/`, which is gitignored — the specs are fetched, not
committed. Populate the sources first, then:

```bash
uv run python ingestion/ingest_docs.py
```

The same command rebuilds the SQLite exact-lookup tables and, unless the prose
sources are absent, the embedded corpus below. Because its inputs are not in
the repository, CI cannot rebuild `specs.sqlite` and does not require it.

### RAG prose corpus (scraped, local-only)

```bash
uv run python ingestion/ingest_docs.py
```

This crawls the vendor documentation portals declared in
[`ingestion/source_manifest.json`](../ingestion/source_manifest.json). Run it
only under your own acceptance of each vendor's terms of use. Expect it to
take hours, and expect the first query afterwards to download the ~250 MB
`nomic-embed-text-v1.5` embedding model into your Hugging Face cache.

Then check the local setup:

```bash
uv run hpe-mcp-doctor
```

### Why the corpus is not a release asset

`data/docs.lance` holds 392,471 verbatim chunks of HPE, Aruba, Juniper, and
Mist documentation. Publishing it would redistribute third-party content this
project has no licence to redistribute, and `ingestion/source_manifest.json`
has always instructed "Do not commit scraped content". Two of its corpora are
additionally obtained by driving Playwright with `headless=False` specifically
to get past Akamai bot protection, which is not something to republish at
scale on anyone's behalf.

Releases therefore ship the wheel, source distribution, SBOM, provenance, and
evidence bundle only. CI rebuilds the tool index from committed specs and does
not assert `--strict-rag`, because no corpus is present.

### Moving an index between your own machines

`scripts/download_indexes.py` remains available as a hardened restorer for
archives **you** host: it verifies a SHA-256 digest, rejects absolute paths,
parent traversal, symlinks, and non-regular members, then stages and atomically
replaces `data/`. Pass `--url`, and pin integrity with `--expected-sha256` or a
`--manifest` file of your own. A digest supplied by `--manifest` or
`--expected-sha256` is always verified; `--skip-checksum` only skips a
downloaded sidecar.

## Refresh and reconcile local indexes

Build or refresh local indexes first:

```bash
uv run python ingestion/scrape_openapi.py
uv run python ingestion/scrape_cnac_spec.py
uv run python ingestion/fetch_mist_openapi.py
uv run python ingestion/scrape_security_lifecycle.py
uv run python scripts/check_openapi_drift.py
uv run python scripts/check_mist_openapi_drift.py
uv run python ingestion/ingest_docs.py
uv run python scripts/ingest_tools.py --complete-catalog
```

Package them:

```bash
uv run python scripts/package_indexes.py
```

Then reconcile the local manifest pair and the canonical facts so
`data/SOURCE-MANIFEST.json`, `data/INDEX-MANIFEST.json`, and
`docs/project-facts.json` describe the artifacts that were just built:

```bash
uv run python scripts/package_indexes.py --write-local-manifests
uv run python scripts/project_facts.py --write
```

Verify with the gates that release validation runs:

```bash
uv run python scripts/package_indexes.py --check-local-manifests
uv run python scripts/project_facts.py --require-indexes
```

Reconciliation never fetches a source. The generated `INDEX-MANIFEST.json`
records `provenance.source_refresh_performed: false` plus each artifact's own
`modified_at`, so a regenerated manifest can never be mistaken for evidence
that upstream sources were re-scraped.

The script writes:

```text
dist/hpe-networking-mcp-rag-index-v<project-version>.tar.gz
dist/hpe-networking-mcp-rag-index-v<project-version>.tar.gz.sha256
dist/hpe-networking-mcp-rag-index-latest.tar.gz
dist/hpe-networking-mcp-rag-index-latest.tar.gz.sha256
```

Keep these archives local. Do not upload `hpe-networking-mcp-rag-index-*` to a
GitHub Release: the versioned and `latest` archives both contain
`data/docs.lance`, which is the scraped prose corpus described above. Use
`--skip-latest-copy` when you only want the versioned archive.

A release ships the wheel, source distribution, evidence bundle, SBOM, and
provenance. It does not ship an index. `scripts/package_indexes.py` exists so
you can snapshot, checksum, and move an index between machines you control, and
so `--check-local-manifests` can prove that `data/SOURCE-MANIFEST.json`,
`data/INDEX-MANIFEST.json`, and `docs/project-facts.json` agree with the
artifacts actually on disk.

## What is inside

| Artifact | Used by | Purpose |
|---|---|---|
| `data/docs.lance` | `search_docs`, `ask_docs` | Embedded docs retrieval |
| `data/specs.sqlite` | `lookup_api` | Exact OpenAPI method/path, operation ID, endpoint, schema, field, and enum lookup |
| `data/tools.lance` | `find_tool` | Semantic router tool discovery |
| `data/SOURCE-MANIFEST.json` | humans / release audit | Byte-identical copy of the tracked RAG source manifest (all declared sources) |
| `data/INDEX-MANIFEST.json` | humans / doctor output / release gate | Schema-versioned artifact sizes, content hashes, per-artifact modification times, exact `specs.sqlite` table counts, LanceDB row/server counts, and the source-manifest checksum and source names |

`scripts/package_indexes.py --check-local-manifests` fails when that pair
drifts apart -- for example a downloaded 9-source `SOURCE-MANIFEST.json`
sitting beside a 16-source `INDEX-MANIFEST.json`, or a manifest describing an
index that has since been rebuilt. `scripts/validate_release.py` runs it on
every invocation and requires the artifacts themselves in strict mode.

OpenAPI-only rebuilds replace their owned endpoint/schema/field tables
atomically while preserving the advisory and lifecycle tables that share
`data/specs.sqlite`. The full ingestion command starts a fresh shared SQLite
artifact and then rebuilds all structured tables, so it is also the recovery
path for a corrupt index.

Feature Navigator history is also stored in `data/specs.sqlite`. The
`compare_aoscx_releases` tool resolves release-family inputs such as `10.13`
and `10.16` to indexed platform snapshots, compares feature support exactly,
and complements that result with release-note enhancements, resolved issues,
and caveats selected by exact platform/version file paths. Latest patch notes
are not treated as cumulative.

To recover only the shared structured artifact without touching LanceDB:

```bash
uv run python -m hpe_networking_mcp.pipeline.clients.specs_index --rebuild-shared
```

This command requires the git-ignored OpenAPI plus all four Aruba/Juniper
security-advisory and lifecycle source folders described below. It fails
closed without replacing the live artifact if any required structured source
family is absent or empty.

## Refresh RAG source inputs

Scraped source files live under git-ignored `ingestion/sources/`; keep the
tracked source list in [`ingestion/source_manifest.json`](../ingestion/source_manifest.json)
current before rebuilding public indexes. The table below mirrors the tracked
manifest so release rebuilds can cite the exact source seeds used for DevHub,
New Central, techdocs, Feature Navigator, and OpenAPI lookup.

| Source | Seed / target | Destination |
|---|---|---|
| DevHub | `https://devhub.arubanetworks.com` | `ingestion/sources/devhub` |
| New Central developer docs | `https://developer.arubanetworks.com/new-central/docs/getting-started-with-rest-apis` and `https://developer.arubanetworks.com/new-central/docs/introduction-to-configuration-apis` | `ingestion/sources/developer_docs` |
| Tech docs | `https://arubanetworking.hpe.com/techdocs/` | `ingestion/sources/tech_docs` |
| NAC docs | `https://developer.arubanetworks.com/new-central-config/reference/mac-registration` | `ingestion/sources/nac_docs` |
| Validated Solution Guides | `https://arubanetworking.hpe.com/techdocs/VSG/docs/` | `ingestion/sources/vsg_docs` |
| New Central techdocs | `https://arubanetworking.hpe.com/techdocs/new-central/content/home.htm` plus `ingestion/techdocs_paths.json` | `ingestion/sources/techdocs_html` |
| Switching Feature Navigator | `https://feature-navigator.arubanetworking.hpe.com/wired?mode=explore` | `ingestion/sources/feature_navigator` |
| OpenAPI specs | Aruba reference pages resolved through ReadMe plus the pinned official `mistsys/mist_openapi` snapshot; refreshed by `scrape_openapi.py`, `scrape_cnac_spec.py`, and `fetch_mist_openapi.py` | `ingestion/sources/openapi_specs` |
| AOS techdocs | `https://arubanetworking.hpe.com/techdocs/aos/` | `ingestion/sources/aos_techdocs` |
| Security advisories | Complete official HPE Aruba Networking CSAF archive from `https://csaf.arubanetworking.hpe.com/changes.csv` | `ingestion/sources/security_advisories` |
| HPE lifecycle notices | Historical all-product End of Sale XML, HPE Networking lifecycle policy, and the official hardware SKU End of Sale PDF | `ingestion/sources/lifecycle_notices` |
| Mist / Apstra lifecycle | Official Juniper hardware/software milestone tables used by the optional Mist and Apstra backends | `ingestion/sources/juniper_lifecycle` |
| Mist / Apstra security | Official Juniper support sitemaps plus Playwright-rendered Security Bulletin articles | `ingestion/sources/juniper_security_advisories` |

The New Central techdocs host can block plain HTTP clients, so use the paced
Playwright scraper (`ingestion/scrape_techdocs_pw.py`) when refreshing that
source. Do not commit scraped content; rebuild `data/docs.lance` and package the
index archive instead.

`ingestion/scrape_security_lifecycle.py` converts the official machine-readable
Aruba CSAF archive into searchable advisory documents containing advisory IDs,
CVEs, severity, affected products and versions, remediation, and references.
It also converts HPE's networking End of Sale XML archive into one searchable
notice per announcement, including affected/replacement SKUs, extracts the
official hardware SKU End of Sale PDF, and captures the official Mist/Apstra
lifecycle milestone tables. HPE does not expose a crawlable current index for
every individual modern notice, so lifecycle answers must cite source dates
rather than implying the historical archive is current or exhaustive.
Juniper advisory discovery uses the official support sitemap and renders only
Mist/Apstra Security Bulletin articles because the Salesforce page body is
client-side.

On macOS, `ingestion/ingest_docs.py` disables fastembed subprocess parallelism
to avoid forkserver deadlocks. The rebuild remains batched but runs in one
process. Linux release builders may use the normal parallel path.

Aruba's July 2026 ReadMe SuperHub migration retired the former internal-UI JSON
spec source and the embedded `oasDefinition` page blob. The current scrapers
resolve `oasPublicUrl` through
`https://dash.readme.com/api/v1/api-registry/{id}` and generate
`ingestion/openapi_registry_manifest.json` with the source page, project,
portal/spec version, path count, hash, and fetch timestamp. Run
`scripts/check_openapi_drift.py` on a schedule; its exit code now identifies
the result class (3 confirmed content drift, 4 a spec added/removed, 5 a
pointer/layout move, 7 a transient fetch failure, 8 a parse failure -- see
[drift gates](source-drift-gates.md)), and only the content/pointer classes
mean refresh and rebuild before publishing indexes. Exit code 2 still means no
registry manifest has been generated yet.

`ingestion/fetch_mist_openapi.py` pins the official Mist 2606.1.1 spec to
commit `f374cffdd5a275c7954645a306fcab7f1227e7a3` and verifies its SHA-256
before writing the git-ignored RAG source. `scripts/check_mist_openapi_drift.py`
reads the reviewed-pin record `ingestion/provenance/mist_openapi_pin.json`
(cross-checked against those module constants) and reports `stale_pin` when
upstream advances *or* when the pin has not been re-verified -- it never
advances the pin itself. Each drift check runs as its own scheduled GitHub
Actions job with its own JSON artifact, aggregated by a `drift-summary` job.
