---
title: "Known limitations"
nav_order: 9
---

# Known limitations

Current as of 2026-08-24 (`main`, package version 0.10.0; latest published
release v0.8.0). Counts derive from `docs/project-facts.json`, which is
regenerated from code and committed manifests, never hand-edited. Each entry
states the limitation, its impact, and the planned remediation.

## RAG is inert on a fresh install

The `ask_docs` prose corpus is deliberately not shipped (vendor content
licensing). On a fresh clone the RAG tools return no results until you build
the indexes locally, and there is no bundled license-safe starter index and
no guided first-run flow. Provenance and freshness tooling does exist:
`corpus_provenance` reports source/version/license citations and pins, and
`rag_diagnostics` reports index freshness and ingestion deltas — but both
operate on a corpus you must first produce.
*Remediation:* license-safe starter index (the offline spec-index build) plus
a guided first-run flow, with doctor reporting each index separately (plan
item 4).

## Curated Aruba Central coverage trails generated coverage

Central exposes 1,927 tools, of which 250 are curated (monitoring 90,
config 80, ops 41, nac 38, streaming 1) and 1,677 are generated from the
OpenAPI manifest. Generated tools provide endpoint reachability, not
workflows: no parameter coercion, no filter builders, no LLM-oriented
docstrings. The nearest comparable community project ships 669 curated
Central tools.
*Remediation:* 25–50 intent-level curated workflows in the first 90 days,
then continued expansion (plan item 1).

## No transactional write model yet

Writes support dry-run and confirmation elicitation, but there is no
standardized plan → dry-run → diff → approval → apply → verify pipeline, no
idempotency keys, no concurrency locks, and no compensating rollback.
*Remediation:* transactional write standard for curated write workflows;
raw/generated endpoints keep the existing write gates (plan item 2).

## Distribution is container/source only

The package is not published to PyPI, so there is no `uvx` one-command
install. Deployment paths today: Dockerfile, two compose files, or a source
checkout with the four console scripts.
*Remediation:* PyPI publication plus opinionated `read-only` / `Central` /
`full` profiles (plan item 6).

## Metrics are custom, not OpenTelemetry

`HPE_MCP_METRICS=1` enables a bounded in-process registry (request counts,
latency aggregates, outcome counts) exposed as JSON over HTTP alongside
`/livez`, `/readyz`, `/healthz`. There is no OTel exporter, so integration
with existing observability stacks requires scraping the custom format.
Metrics and health endpoints exist only on the streamable-HTTP transport;
stdio exposes neither.
*Remediation:* OpenTelemetry metrics and documented health/readiness
behavior (plan item 6).

## No automated upstream spec-drift sync

Generated tools regenerate from committed OpenAPI manifests, but nothing
detects when HPE or Juniper publish updated specs. A drift-check script
(`scripts/check_nowireless_source_drift.py`) and gate documentation
([source-drift-gates.md](source-drift-gates.md)) exist; the scheduled
sync-and-PR automation does not.
*Remediation:* scheduled OpenAPI sync with contract tests and a provenance
PR (plan item 5).

## Operational complexity

The router fronts 18 backend MCP servers. This buys constant token cost
regardless of catalog size, but it is more moving parts than a single-process
server and its failure modes are correspondingly broader.
*Remediation:* reference deployments and health/readiness documentation
(plan item 6).

## Project maturity

The repository is days old with a single author and a minimal public track
record. Engineering lineage predates the repo (see
[MIGRATION.md](https://github.com/secure-ssid/hpe-networking-mcp/blob/main/MIGRATION.md)),
but multi-author maintenance is unproven. This is the project's largest risk
and is addressed only by sustained releases, published benchmark evidence,
and trust documentation (plan item 7).
