# Third-party notice — vendored OpenAPI corpus

`vendor/openapi/` holds **31 OpenAPI documents from two upstreams under two
different licensing regimes.** They are not interchangeable, and the difference
matters if you redistribute this repository.

| Files | Upstream | Licence | Section |
| --- | --- | --- | --- |
| 30 New Central documents (everything except `mist.openapi.json`) | HPE Aruba Networking developer portal | **Proprietary HPE material. Not open source.** | [A](#a--the-30-new-central-documents) |
| `mist.openapi.json` | `mistsys/mist_openapi` on GitHub | **MIT** — full text reproduced below | [B](#b--mistopenapijson) |

Every document in either group is pinned, and
`scripts/vendor_openapi_corpus.py` refuses to write anything unless *all 31*
verify against their pins. The corpus is replaced all at once or not at all.

```bash
python scripts/vendor_openapi_corpus.py --dry-run   # fetch + check, write nothing
python scripts/vendor_openapi_corpus.py             # rewrite the corpus
```

---

## A — the 30 New Central documents

### What they are

30 OpenAPI 3.x documents describing the HPE Aruba Networking **New Central**
REST API — 779 API paths in total, split across two portal projects:

| Portal project | Documents | Portal version |
| --- | --- | --- |
| `aruba-new-central` | 5 | v26.05 |
| `aruba-new-central-config` | 25 | v26.04 |

They are the API descriptions the developer portal itself serves to render its
reference pages. They are **not** documentation we wrote, and they are not
derived, summarised or regenerated: each file is the upstream document
verbatim, re-serialised with `json.dumps(spec, indent=2, sort_keys=True)` so it
diffs cleanly line by line. No content is added, removed or rewritten.

### Who publishes them

Hewlett Packard Enterprise / Aruba Networking, at
<https://developer.arubanetworks.com/>. The portal runs on ReadMe, and each
reference page points at an api-registry document; the registry ids, the
reference-page URL each was discovered from, and the upstream content hash are
recorded per document in `ingestion/openapi_registry_manifest.json` and in
`MANIFEST.json` beside this file.

### Where they came from, and when

Fetched **2026-08-21** from the ReadMe api-registry endpoint
`https://dash.readme.com/api/v1/api-registry/<registry_id>`, reachable without
credentials. Every document's `source_url` in `MANIFEST.json` is the public
developer-portal reference page it backs.

Each must match `ingestion/openapi_registry_manifest.json` on both its declared
path count and its content fingerprint. A document reworked upstream — even one
that kept the same number of paths — stops the run.

At the 2026-08-21 fetch, all 30 documents were byte-identical in content to the
fingerprints pinned on 2026-07-25 — 0 of 30 had drifted upstream. Because that
check is a precondition of writing, any future run either reproduces the
reviewed corpus or fails loudly.

### Licence and redistribution basis

**These 30 documents are proprietary HPE Aruba Networking material. They are
not open source, and this repository's MIT licence does not extend to them.**

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

---

## B — `mist.openapi.json`

### What it is

One OpenAPI 3.1.0 document describing the entire **Juniper Mist** REST API —
756 API paths, `info.version` `2607.1.0`. It is the upstream file **byte for
byte**: unlike group A it is not re-serialised, because its pin is a SHA-256
over the bytes GitHub serves and any reformatting would break that identity.

### Who publishes it, and where it came from

Mist Systems / Juniper Networks, at <https://github.com/mistsys/mist_openapi>
(default branch `master`).

Pinned to commit
[`315b30ff4fa65c1dc3a2b5c1f27931e1b14ed01e`](https://github.com/mistsys/mist_openapi/commit/315b30ff4fa65c1dc3a2b5c1f27931e1b14ed01e)
(2026-07-24) and fetched **2026-08-21** from the immutable raw URL

```
https://raw.githubusercontent.com/mistsys/mist_openapi/315b30ff4fa65c1dc3a2b5c1f27931e1b14ed01e/mist.openapi.json
```

A branch URL is deliberately not used: it re-resolves under you, so it could
not pin anything. The pin — repo, commit, path, expected digest and licence —
is declared in `COMMIT_PINS` in `scripts/vendor_openapi_corpus.py` and checked
against the bytes received before the file is written.

`ingestion/scrape_mist_openapi.py` fetches the same file from the `master` tip
into the git-ignored scrape directory. That path is convenient, not
reproducible; this vendored copy is the reproducible one.

### Licence

MIT, per the `LICENSE` file at the pinned commit and `info.license` in the
document itself. Reproduced in full as MIT requires:

```
MIT License

Copyright (c) 2020 Thomas Munzer

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Source: <https://raw.githubusercontent.com/mistsys/mist_openapi/315b30ff4fa65c1dc3a2b5c1f27931e1b14ed01e/LICENSE>.
"Juniper", "Mist" and "Marvis" are marks of Juniper Networks, used here only to
identify the API being described. No endorsement by Juniper is claimed.

---

## Integrity

`MANIFEST.json` records five keys for every entry — `file`, `source_url`,
`sha256`, `fetched`, `license` — plus `title` and `path_count`, and then the
keys specific to how that entry is pinned:

| Pinning scheme | Extra keys | Meaning |
| --- | --- | --- |
| ReadMe registry (group A) | `registry_id`, `registry_sha256` | resolves in `ingestion/openapi_registry_manifest.json`, and `registry_sha256` must equal the `sha256` recorded there |
| Commit pin (group B) | `upstream_repo`, `upstream_commit` | the commit must appear in `source_url`, so the exact bytes are re-fetchable forever |

No entry may be unpinned; `tests/unit/test_vendor_corpus.py` fails an entry
that declares neither scheme.

`sha256` means something slightly different in each group, because the two are
serialised differently:

- **Group A** — SHA-256 of the indent=2 bytes of the file on disk. Proves the
  committed file is unmodified. It is *not* comparable to the registry
  manifest's digest; that is `registry_sha256`, the `spec_fingerprint` over the
  same document re-serialised compactly
  (`json.dumps(doc, sort_keys=True, separators=(",", ":"))`). The two are over
  different serialisations and never match. To check `registry_sha256`, reparse
  the file and hash that compact form — hashing the file's bytes will not
  reproduce it.
- **Group B** — SHA-256 of the upstream bytes themselves, which are also the
  bytes on disk. One digest proves both that the file is unmodified and that
  upstream published exactly it. `shasum -a 256 vendor/openapi/mist.openapi.json`
  reproduces it directly.

`tests/unit/test_vendor_corpus.py` fails if any file is missing, undeclared,
modified, is not an OpenAPI/Swagger document, or is not pinned to a verifiable
upstream by one of the two schemes above.
