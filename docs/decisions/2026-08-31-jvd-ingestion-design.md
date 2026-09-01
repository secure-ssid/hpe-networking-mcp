# JVD ingestion design

Date: 2026-08-31
Status: Approved approach A, full scope — pending spec review
Owner: Stephen Choate

## Goal

Ingest Juniper Validated Designs from [`Juniper/jvd`](https://github.com/Juniper/jvd)
into the RAG corpus as a new source family, so `ask_docs` / `search_docs` answer
design and architecture questions from validated, citable reference
architectures instead of only product manuals.

## Source facts (verified 2026-08-31)

- License: **Apache-2.0** with a top-level `NOTICE` file; redistribution with
  attribution is permitted. A THIRD_PARTY_NOTICES entry is required.
- 21 designs across `data_center/`, `enterprise_wan/`, `optical/`,
  `security/`, `service_provider/`.
- Per design: `README.md`, `documentation/*.md` (design guides up to ~90 KB,
  solution overviews, datasheets, test reports), `configuration/conf/` (full
  per-device validated configs), `configuration/snips/` (validated config
  building blocks), `images/`.
- `portal/public/byoai/<design>/README.md` — per-design grounded-assistant
  prompts; plus `portal/public/BYOAI-TIPS.md` and `USING-BYOAI.md`.
- All prose and configs are plain text/markdown. No Playwright, no pandoc.
- Repo HEAD moves; a pinned commit is required for reproducible ingestion.

## Chosen mechanism

A new manifest source family shaped exactly like `vsg_docs`
(discover → seed file → scrape), minus the browser:

1. `ingestion/discover_jvd_urls.py` (`pre` extra script) queries the GitHub
   API (`repos/Juniper/jvd/commits/main`, then one `git/trees?recursive=1`)
   for the HEAD commit sha and enumerates blob paths. It writes
   `ingestion/jvd_urls.json` (committed, like `vsg_urls.json`):

   ```json
   {"repo": "Juniper/jvd", "ref": "main", "commit": "<sha>",
   "files": [{"path": "...", "url": "https://raw.githubusercontent.com/...", "size": N}]}
   ```

2. `ingestion/scrape_jvd.py` reads the seed file and fetches each raw
   `documentation/**`, `configuration/conf/**`, `configuration/snips/**`) and
   `portal/public/byoai/**` + `BYOAI-TIPS.md` + `USING-BYOAI.md`. Exclude
   `images/**`, binaries by extension, and everything else under `portal/`
   (the SPA, assets, `_fetch-probe`, SEO files).

2. `ingestion/scrape_jvd_urls.py` reads the seed file and fetches each raw
   URL (raw.githubusercontent.com is CDN-served and does not consume GitHub
   API rate quota; ~2–3 API calls total for discovery). Every file is written
   under `ingestion/sources/jvd/<original-path>` with a provenance header:
   source URL, design track, pinned commit, license, and retrieval date.
   Config text (`.conf`/snips) is wrapped in a fenced block so the chunker
   treats it as content. Binary or oversized files (> 2 MB) are skipped and
   reported.

   Note: the approved approach described a git clone; the raw-fetch realizes
   the same pinned-commit guarantee without a git binary dependency and is
   the mechanism the plan implements.

3. Registration:
   - `ingestion/source_manifest.json`: source `jvd`, doc_type `jvd`,
     seed_urls `[repo URL, portal URL]`, `url_seed_file`, pre-phase discover.
   - `ingestion/ingest_docs.py`: `SOURCE_META["jvd"] = "jvd"`.
   - `src/hpe_networking_mcp/mcp_servers/rag.py`: `_DOC_TYPE_TO_SOURCE` gains
     `"jvd": "jvd"`.
   - Vendor/product/platform derivation is verified in the plan; expected to
     resolve from the `github.com/Juniper` source URL with no new code.

4. Downstream count artifacts: declared sources go 31 → 32.
   `docs/project-facts.json` regenerated via `scripts/project_facts.py`;
   prose counts updated (`docs/release-indexes.md`); any test asserting the
   source count updated. Nothing else is edited.

5. `THIRD_PARTY_NOTICES.md` gains the Juniper/jvd Apache-2.0 entry with
   NOTICE preservation.

## Data flow

GitHub API (2–3 calls) → `jvd_urls.json` (pinned sha + file list) → raw
fetches → `ingestion/sources/jvd/**` → existing `ingest_docs.py` chunk/embed
pipeline → `data/docs.lance` → `search_docs` / `ask_docs` with citations.
No new runtime code paths; the router and RAG tools are untouched.

## Error handling

- Discovery API failure: classified `NOT_CHECKED` by the existing refresh
  taxonomy; existing committed seed file stays authoritative.
- Individual raw fetch failure: file skipped, reported, non-zero exit at end.
- Unknown track dir appearing upstream: excluded by the allowlist; discovery
  reports it as a new-path note so coverage gaps are visible.
- Re-runs: fetches are idempotent per pinned sha; the pin advancing is a
  deliberate re-review step (regenerate seed file, commit, re-ingest).

## Testing

- New `tests/unit/test_jvd_sources.py`: seed-file parsing, path allow/deny
  policy, provenance header, binary/size skip, fenced config wrapping — all
  from fixtures, no network.
- Existing generic validators cover the manifest (scraper exists, output-dir
  convention, duplicate keys, SOURCE_META/`_DOC_TYPE_TO_SOURCE` registration)
  and run as part of the ladder.
- Verification for the change: targeted unit tests + the manifest/facts
  validators from `validate_release.py --skip-rag` on the facts path.

## Non-goals (later phases)

- MCP prompt surface built from BYOAI files.
- Curated config-building-block lookup tool with exact-quote grounding.
- Parameterized config generation.
- Embedding/refreshing the local corpus (operator-run command).
