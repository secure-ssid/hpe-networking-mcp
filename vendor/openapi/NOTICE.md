# Third-party notice — vendored OpenAPI corpus

## What these files are

`vendor/openapi/*.json` are 30 OpenAPI 3.x documents describing the HPE Aruba
Networking **New Central** REST API — 779 API paths in total, split across two
portal projects:

| Portal project | Documents | Portal version |
| --- | --- | --- |
| `aruba-new-central` | 5 | v26.05 |
| `aruba-new-central-config` | 25 | v26.04 |

They are the API descriptions the developer portal itself serves to render its
reference pages. They are **not** documentation we wrote, and they are not
derived, summarised or regenerated: each file is the upstream document
verbatim, re-serialised with `json.dumps(spec, indent=2, sort_keys=True)` so it
diffs cleanly line by line. No content is added, removed or rewritten.

## Who publishes them

Hewlett Packard Enterprise / Aruba Networking, at
<https://developer.arubanetworks.com/>. The portal runs on ReadMe, and each
reference page points at an api-registry document; the registry ids, the
reference-page URL each was discovered from, and the upstream content hash are
recorded per document in `ingestion/openapi_registry_manifest.json` and in
`MANIFEST.json` beside this file.

## Where they came from, and when

Fetched **2026-08-21** from the ReadMe api-registry endpoint
`https://dash.readme.com/api/v1/api-registry/<registry_id>`, reachable without
credentials, by `scripts/vendor_openapi_corpus.py`. Every document's `source_url`
in `MANIFEST.json` is the public developer-portal reference page it backs.

To re-fetch and verify:

```bash
python scripts/vendor_openapi_corpus.py --dry-run   # fetch + check, write nothing
python scripts/vendor_openapi_corpus.py             # rewrite the corpus
```

The script refuses to write anything unless all 30 documents fetch *and* each
one matches `ingestion/openapi_registry_manifest.json` on both its declared
path count and its content fingerprint. A document reworked upstream — even one
that kept the same number of paths — stops the run. The corpus is replaced all
at once or not at all.

At the 2026-08-21 fetch, all 30 documents were byte-identical in content to the
fingerprints pinned on 2026-07-25 — 0 of 30 had drifted upstream. Because that
check is now a precondition of writing, any future run either reproduces the
reviewed corpus or fails loudly.

## Licence and redistribution basis

**These documents are proprietary HPE Aruba Networking material. They are not
open source, and this repository's MIT licence does not extend to them.**

HPE publishes them without an accompanying licence grant and without an
authentication barrier, as the machine-readable form of public API reference
documentation whose entire purpose is to be consumed by API clients. They are
redistributed here verbatim, with attribution and provenance, so that
`lookup_api` answers exact API questions from a clean clone with no network
access — the same use the publisher intends, moved offline.

This is a good-faith reliance on published-for-integration intent, not a
licence. Specifically:

- No warranty and no endorsement by HPE is claimed or implied.
- "HPE", "Aruba", "Aruba Networking" and "New Central" are marks of Hewlett
  Packard Enterprise, used here only to identify the API being described.
- If HPE asks for these documents to be removed, remove them. Nothing outside
  this directory depends on the files being *committed* —
  `scripts/vendor_openapi_corpus.py` reproduces them from the upstream portal,
  and `ingestion/openapi_registry_manifest.json` keeps the pointers.

Downstream users redistributing this repository inherit that position and
should make their own assessment.

## Integrity

`MANIFEST.json` records, per document: `file`, `source_url`, `fetched`,
`license`, plus `title`, `registry_id`, `path_count` and `registry_sha256`
carried through from the registry manifest.

It carries **two digests over different serialisations, which never match**:

- `sha256` — SHA-256 of the indent=2 bytes of the file on disk. Proves the
  committed file is unmodified.
- `registry_sha256` — the registry manifest's `spec_fingerprint`, SHA-256 of
  the same document re-serialised compactly
  (`json.dumps(doc, sort_keys=True, separators=(",", ":"))`). Proves the
  document is the one upstream published. To check it, reparse the file and
  hash that compact form — hashing the file's bytes will not reproduce it.

`tests/unit/test_vendor_corpus.py` fails if any file is missing, undeclared,
modified, is not an OpenAPI/Swagger document, or lacks an upstream fingerprint.
