# Release artifact automation

This page documents the credential-gated validation matrix, the release
bundle packaging pipeline, and the restore/smoke-test tooling that produce
and verify release artifacts. The underlying evidence schemas and validation
matrix were introduced in v0.7 and remain versioned independently of the
package release; see
[Artifact contracts and live-test configuration](artifact-contracts.md).

None of the tooling on this page makes a live vendor API call or writes to
a real platform by default. Every read/write live probe stays gated behind
`hpe_networking_mcp.pipeline.live_test_config` (`HPE_MCP_LIVE_TEST_<PLATFORM>_READ=1` /
`_WRITE=1`), and this page's scripts never flip those flags themselves.

## Validation matrix

```bash
uv run python scripts/run_v07_validation_matrix.py
```

Classifies every v0.7 coverage category -- Central, GLP, AOS8, the optional
product starters (ClearPass, Mist, Apstra, EdgeConnect, UXI), Axis, RAG/
source freshness, and router automation -- into exactly one of six states,
without ever making a live call itself:

| Classification | Meaning |
|---|---|
| `offline_fixture` | Only the offline evaluator self-check ran; no credentials or opt-in. |
| `live_read` | Read opt-in (`_READ=1`) and credentials are both present. |
| `disposable_write` | Read **and** write opt-in are both set (write alone is never sufficient) and credentials are present. |
| `blocked` | Safe default when no offline self-check exists and live-read opt-in is not enabled. |
| `unavailable` | Read opt-in is enabled but credentials are missing, or a required offline helper failed. |
| `coverage_gap` | A documented permanent or currently unverified capability gap, such as a missing live write API or unavailable planner surface. |

The runner delegates every product's actual classification logic to that
product's existing evaluator/report script (`scripts/evaluate_central_070_readonly.py`,
`scripts/evaluate_axis_lab.py`, `scripts/build_optional_product_evidence.py`,
`scripts/generate_router_automation_report.py`, and friends) instead of
duplicating any of it. The result is a `VALIDATION_MATRIX_RESULT` artifact
(`src/hpe_networking_mcp/pipeline/artifact_contracts.py`), written via `contracts.write_artifact`
like every other v0.7 artifact kind.

```bash
uv run python scripts/run_v07_validation_matrix.py --output outputs/validation-matrix.json
```

## Release bundle packaging

```bash
uv run python scripts/build_release_bundle.py --output-dir dist
```

Assembles one release-artifacts bundle end to end:

1. Validation matrix (`evidence/validation-matrix.json`).
2. Capability snapshot (`evidence/capability-snapshot.json`, the
   reproducible core of `scripts/report_capability_gaps.py`).
3. Source-freshness snapshot, only if a prior local
   `outputs/source-freshness.json` already exists -- never fetched here.
4. Optional-product-backend compatibility/evidence artifacts.
5. Axis lab evidence, router dependency/reconciliation plan artifacts.
6. Prebuilt RAG/OpenAPI indexes under `indexes/`, only if `data/` already
   contains them locally (skip with `--no-indexes`).
7. `release-manifest.json` (a `RELEASE_ARTIFACT_MANIFEST` artifact) listing
   every staged file's kind, schema version, size, SHA-256, and redaction
   status.
8. `sbom.json` -- a deterministic CycloneDX 1.5 SBOM generated from
   `uv.lock` by `src/hpe_networking_mcp/pipeline/sbom.py` (component name/version/purl only; no
   network resolution).
9. `CHECKSUMS.txt` -- a `sha256sum`-compatible checksums file covering
   every staged file (never lists itself).
10. `provenance.json` -- a provenance manifest (`src/hpe_networking_mcp/pipeline/release_packaging.py`
    `build_provenance_manifest`) recording the release version, builder
    identity (`local` or `github-actions`), and the SHA-256 subject list.
    It is explicitly **not** a signed attestation; GitHub artifact
    attestation happens separately, in CI, over the final archive.
11. A deterministic `.tar.gz` archive (sorted member order; fixed
    `mtime=0`/`uid=0`/`gid=0`/`mode=0o644` tar metadata; fixed gzip header)
    plus its own `.sha256` sidecar.

"Deterministic" describes the archive **packaging mechanics**, not the
staged content byte-for-byte across time: evidence files legitimately embed
a fresh `generated_at` timestamp on every run, exactly like every other
artifact kind. Given byte-identical staged input, `build_deterministic_archive`
always produces a byte-identical archive.

`src/hpe_networking_mcp/pipeline/release_packaging.py` intentionally never imports anything from
`scripts/` (the repository's `src/hpe_networking_mcp/pipeline/` → `scripts/` layering rule), so all
of the multi-step orchestration above -- which does need several sibling
`scripts/*` evidence generators -- lives in `scripts/build_release_bundle.py`
instead.

## Restore and smoke-test

```bash
uv run python scripts/restore_release_bundle.py dist/hpe-networking-mcp-release-artifacts-v<version>.tar.gz
```

Generalizes `scripts/download_indexes.py`'s safe-extraction pattern
(`src/hpe_networking_mcp/pipeline/release_restore.py`) and adds:

- File-count / per-file / total-size bounds, enforced **before** any bytes
  are written (defaults: 1000 members, 1 GiB per file, 2 GiB total -- sized
  for this repo's prebuilt RAG indexes, which can be several hundred MB).
- Rejection of path traversal, absolute paths, and any non-regular-file /
  non-directory archive member (symlinks, hardlinks, devices).
- A hard refusal to extract into the repository root or any guarded
  top-level source directory (`pipeline`, `scripts`, `tests`, `docs`,
  `mcp_servers`, `config`, `ingestion`, `resources`, `inputs`, `.git`) --
  restore/smoke-testing a bundle never overwrites repository data.
- Checksum verification against a sibling `.sha256` file when present.
- Post-extraction schema validation: every file the bundle's own
  `release-manifest.json` lists is located, its size/SHA-256 are re-checked
  against the manifest record, and its JSON payload is re-validated against
  `hpe_networking_mcp.pipeline.artifact_contracts.build_artifact` for that entry's `kind`.
  `sbom.json`/`provenance.json` get a lighter structural sanity check.
- Extraction only into a caller-managed temporary directory
  (`tempfile.TemporaryDirectory`), always cleaned up -- even on failure.

## GitHub Actions

`.github/workflows/release-artifacts.yml` is an operator-triggered
`workflow_dispatch` release gate. It validates that the requested `vX.Y.Z`
tag matches `pyproject.toml`, rebuilds the tool catalog and exact-API database
from the OpenAPI specs committed to this repository, runs the strict
API/tool-index contract, builds wheel and source distributions, builds and
restores the release evidence bundle, attests the application/evidence
artifacts produced by the workflow, and creates or updates the GitHub Release
and tag.

No RAG prose corpus is restored or published. `data/docs.lance` is scraped
vendor documentation that this project has no licence to redistribute, so it
is never a release asset and `--strict-rag` is never asserted in CI.

The workflow uses least-required permissions (`contents: read` by default;
`contents: write`, `id-token: write`, and `attestations: write` only on the
release job). Its inputs explicitly control draft and prerelease state. It
never runs on a schedule and never contacts a vendor API; the scheduled
source-freshness jobs remain independent in `ci.yml`.

The main CI workflow has two release-related tiers:

- The Python matrix runs every unit/protocol test and the artifact-free
  catalog/facts gate on Python 3.10, 3.11, and 3.12 on Linux plus Python 3.12
  on macOS.
- `Strict tool index` rebuilds the tool catalog from committed OpenAPI specs
  and runs `--strict-tool-index`. Repository administrators enable it with the
  Actions variable `HPE_MCP_STRICT_INDEX_ENABLED=true`; it then runs on
  pushes, pull requests, and manual CI dispatches.

The package job also builds wheel/sdist, installs the wheel into a fresh
environment, and smoke-runs all four `hpe-mcp-*` console scripts.

Strict index validation stays reproducible because every input it needs is
committed. A package-version bump or a change to
`ingestion/source_manifest.json` requires rebuilding and reconciling the local
indexes so `data/SOURCE-MANIFEST.json`, `data/INDEX-MANIFEST.json`, and
`docs/project-facts.json` agree, but no release asset has to be republished to
make CI pass.

## Testing and linting

```bash
uv run pytest \
  tests/unit/test_artifact_contracts.py \
  tests/unit/test_run_v07_validation_matrix.py \
  tests/unit/test_sbom.py \
  tests/unit/test_release_packaging.py \
  tests/unit/test_build_release_bundle.py \
  tests/unit/test_release_restore.py \
  tests/unit/test_release_artifacts_workflow.py

uv run ruff check src/hpe_networking_mcp/pipeline/sbom.py src/hpe_networking_mcp/pipeline/release_packaging.py src/hpe_networking_mcp/pipeline/release_restore.py \
  scripts/run_v07_validation_matrix.py scripts/build_release_bundle.py scripts/restore_release_bundle.py
```

These tests never make a network call and never enable a live-test flag;
`tests/unit/test_release_artifacts_workflow.py` parses the workflow YAML
offline and asserts the least-privilege/pinning/gating properties above
without invoking GitHub Actions.
