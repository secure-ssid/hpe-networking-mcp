# Security/lifecycle source coverage, freshness, and provenance

This page defines exactly what hpe-networking-mcp's security-advisory and
product-lifecycle sources cover, what they do not, and how the scheduled
freshness/provenance check verifies them. It exists so `ask_docs`,
`lookup_advisory`, `check_product_lifecycle`, and their v0.7 companions
(`list_advisories`, `list_lifecycle_events`, `correlate_advisory_lifecycle`,
`rag_diagnostics` — see
[RAG architecture](architecture/RAG-ARCHITECTURE.md#v07--structured-securitylifecycle-intelligence-expansion))
are never presented as more complete than their authoritative sources
actually are. In particular, `correlate_advisory_lifecycle` only links an
advisory to a lifecycle record on an exact, normalized product/SKU string
match — given the coverage gap below, most current advisories correlate to
no lifecycle record at all, and that is reported as `unresolved`, not
guessed at or silently omitted.

## Authoritative sources and their boundaries

Only official HPE Aruba Networking, HPE, Juniper, or product-vendor sources
are used. Each is refreshed by `ingestion/scrape_security_lifecycle.py` and
indexed into both the prose RAG corpus and the structured `advisories` /
`lifecycle_events` SQLite tables (`src/hpe_networking_mcp/pipeline/clients/advisory_index.py`).

| Source family | Coverage | Known boundary |
|---|---|---|
| Aruba CSAF security advisories | Complete official archive, discovered incrementally via `changes.csv` | None known -- grows as HPE Aruba publishes new advisories |
| HPE Networking End of Sale XML archive | All legacy HP/H3C/3Com/ProCurve networking categories | **Historical.** No current Aruba-branded entries; most recent published date is 2020 |
| Aruba hardware End of Sale PDF | SKU-level EoS dates and replacements | Static snapshot (document metadata records a 2020-05-06 last modification); not refreshed on a schedule |
| Juniper Mist/Apstra EOL pages | The 3 official Mist/Apstra hardware+software lifecycle tables, plus any page the official EOL index nav adds | Juniper renders these server-side rather than via a public structured feed |
| Juniper Mist/Apstra security bulletins | Discovered via the official sitemap index's topic-article child sitemaps, filtered to Mist/Apstra Security Bulletin articles | Limited to sitemap-discoverable articles |

## The current-Aruba-lifecycle coverage gap

There is **no reliable, reproducible, official machine-readable source** for
current (post-2020) Aruba-branded HPE Networking product lifecycle notices,
beyond the historical archive and static PDF above. This was verified, not
assumed:

- The HPE Networking End of Sale XML archive contains zero Aruba-branded
  product names and nothing published after 2020.
- The official Aruba hardware End of Sale PDF's own document metadata
  records a last-modified date of 2020-05-06.
- Current per-product Aruba EoS/EoL bulletins are published to unpredictable
  `asp-documents.arubanetworks.com/portals/0/<varying-filename>.pdf` URLs
  with no official sitemap, index, or feed enumerating them.
- The authenticated Aruba Support Portal (`asp.arubanetworks.com`) requires
  login and is not a reproducible offline/CI source.
- HPE publishes no public RSS/JSON feed for Aruba Networking EOL
  notifications.

Rather than scrape an unstable or authentication-gated page and present it
as authoritative, this is recorded as an explicit, evidenced coverage gap
(`ingestion.scrape_security_lifecycle.HPE_ARUBA_CURRENT_LIFECYCLE_COVERAGE_GAP`,
pinned at `ingestion/provenance/hpe_aruba_current_lifecycle.json`). It always
reports the `coverage_gap` state below -- never `fresh`. Revisit this if
HPE/Aruba publishes a reproducible machine-readable current lifecycle feed.

## Freshness states

`scripts/check_security_lifecycle_drift.py` evaluates every source family
and reports one of five states, never a success-shaped fallback:

| State | Meaning |
|---|---|
| `fresh` | Fetched, parsed, and met its committed minimum count |
| `stale` | Fetched and parsed, but the count regressed below the committed minimum |
| `unavailable` | The source could not be fetched at all (network, timeout, HTTP error) |
| `changed` | Fetched, but no longer parses the way its reviewed provenance pin expects (a structural/schema break, or the source's own URLs no longer match the pin) |
| `coverage_gap` | An explicit, already-documented limitation (see above) -- never silently reported as `fresh` |

The check exits non-zero if any source is `stale`, `unavailable`, or
`changed`. `coverage_gap` is an expected, already-reviewed state and does not
fail the check on its own.

### Mapping onto the shared drift taxonomy

These five states stay this check's own vocabulary (and the vocabulary of the
`source_freshness_result` artifact, whose strict schema is unchanged), but
each one also carries a `result_class` from the taxonomy every drift gate
shares -- see [source/API/RAG drift gates](source-drift-gates.md):

| State here | Shared `result_class` | Classified exit code |
|---|---|---|
| `fresh` | `fresh` | 0 |
| `stale` (count regressed) | `content_drift` | 3 |
| `changed` from a provenance identity/marker mismatch | `pointer_change` | 5 |
| `changed` from a parser raising on already-fetched content | `parser_error` | 8 |
| `unavailable` | `unavailable` | 7 |
| `coverage_gap` | `coverage_gap` | 0 |

`changed` deliberately fans out into two classes because the remediation
differs: a reviewed-identity change needs a pin review, while a parser
blowing up needs a parser fix -- and neither is evidence that the upstream
*content* drifted. Pass `--exit-code-mode legacy` to collapse every failing
state back onto exit code 1.

The check also writes a second, taxonomy-shaped report to
`outputs/drift/security-lifecycle-drift.json` (`--drift-artifact-path`,
`--no-drift-artifact`) so the aggregate CI summary can compare this gate with
the OpenAPI/Mist/product-spec/community gates. The `result_class` travels in
that report only: the versioned `source_freshness_result` contract is
projected down to exactly its declared fields rather than widened.

## Provenance pins

`ingestion/lifecycle_provenance.py` pins, under the committed
`ingestion/provenance/*.json` files, each source family's:

- **Source identity** -- its exact reviewed endpoint URLs. A code change to
  a source URL without updating the pin is rejected
  (`SourceProvenanceError`) as `changed` until reviewed and re-pinned.
- **Structural markers** -- literal substrings (e.g. XML tag names) a
  parser depends on. This catches a silent schema change for parsers that
  do not themselves raise on a missing/renamed field (the HPE lifecycle XML
  parser, for example, silently skips a record instead of raising).
- **Minimum coverage count** -- raising a minimum is itself an explicit,
  reviewed, committed change to the pin, not a hardcoded module constant.

Coverage counts naturally grow over time (new advisories, new lifecycle
notices), so pins never hash full content -- only source identity and
parser-dependent structure, mirroring the existing
`scripts/build_optional_product_manifests.py` source-pin pattern but
generalized for continuously growing sources.

To review and accept an intentional upstream change, inspect the diff, then
rebuild the pin with `ingestion.lifecycle_provenance.build_pin(...)` +
`write_pin(...)` for the affected family.

## Freshness artifact

Running `scripts/check_security_lifecycle_drift.py` writes a bounded,
redacted, deterministic (aside from its timestamp) `source_freshness_result`
artifact (see [Artifact contracts](artifact-contracts.md)) to
`outputs/source-freshness.json` by default. Each entry has a `source`,
`count`, `minimum`, `status`, `drift_detected`, and a length-bounded
`detail`. Pass `--artifact-path` to write elsewhere, or `--no-artifact` to
skip writing and only print status lines.

## Juniper Mist/Apstra discovery

`ingestion.scrape_security_lifecycle.discover_juniper_lifecycle_urls()`
merges the three human-reviewed Mist/Apstra EOL page URLs with whatever the
official Juniper EOL index page (`support.juniper.net/support/eol/`)
currently discloses under a Mist/Apstra label, deduplicated by absolute URL
so a future officially-added page is picked up automatically without a code
change, while the reviewed slugs for already-known pages are preserved.

`ingestion.scrape_security_lifecycle.discover_juniper_security_sitemaps()`
fetches and parses the official Juniper support-portal sitemap index
(`supportportal.juniper.net/s/sitemap.xml`) into its current topic-article
child sitemap URLs (`sitemap-topicarticle-*.xml`). Only this index URL is
provenance-pinned; individual child filenames are not, because Juniper has
changed one without notice before (a previously hardcoded
`sitemap-topicarticle-weekly.xml` child started 404ing -- GitHub Actions
run 30218562473). `parse_juniper_security_sitemap_index()` fails closed on
malformed XML, an unexpected root element, an implausible number of
children, any child URL off the reviewed host/scheme or carrying
credentials/query/fragment/path-traversal, and zero matching topic-article
children -- it never falls back to a previously known child URL.
`discover_juniper_security_urls()` then reads each discovered child
sitemap and filters to Mist/Apstra Security Bulletin articles.
