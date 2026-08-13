# hpe-networking-mcp 0.6.0

Version 0.6.0 expands verified platform coverage, adds exact security and
product-lifecycle lookup, and strengthens provenance, auditing, reporting, and
release automation without widening default write access.

## Catalog snapshot

| Metric | Count |
|---|---:|
| Generated manifest operations | 6,056 |
| Active generated tools | 6,039 |
| Curated tools | 506 |
| Complete backend tools | 6,545 |
| Direct-all client-visible tools | 6,548 |
| Core profile | 319 |
| Optional read-only profile | 2,712 |
| Optional read-write profile | 5,641 |

The default minimal router still exposes only `find_tool`,
`invoke_read_tool`, and `invoke_tool`.

## Security, lifecycle, and RAG

- Added structured `lookup_advisory` and `check_product_lifecycle` tools.
- Indexed 102 official security advisories and 346 lifecycle records alongside
  244 OpenAPI specs, 3,796 endpoints, 11,293 schemas, and 60,568 fields.
- Added official HPE Aruba and Juniper security/lifecycle source discovery,
  freshness checks, scheduled CI drift detection, and exact structured evals.
- Added content-hash incremental LanceDB ingestion with bounded upsert/delete
  behavior and structured counts in packaged index manifests.

## Platform coverage

- Expanded the official Central manifest to 1,678 operations and added a
  guarded, allow-listed Central GET escape hatch plus report lifecycle tools.
- Added 14 curated GLP RBAC, scope-group, identity, and auto-subscription
  workflows, and corrected the subscription preview query to `dry-run`.
- Added AOS8 destination aliases, Ethernet ACLs, whitelist rules, dependency
  planning, live export collection, and readiness/staged-migration prompts.
- Split Axis fused CRUD entries into exact create/update/delete operations,
  growing its deterministic manifest to 47 operations.
- Added a bounded Mist site-assurance snapshot workflow with per-section
  degraded-result reporting.

## Router and operator improvements

- `find_tool` can filter curated/generated origin and exact generated
  operation IDs while returning provenance details.
- Optional redacted JSONL audit logging records tool names, argument keys,
  digests, outcomes, durations, and exception types without raw values.
- Migration reports now support CSV, structured JSON, and escaped HTML through
  `--report-formats`.
- CI now covers Python 3.10 and 3.12 on Ubuntu plus Python 3.12 on macOS.

## Provenance and safety

- Added digest-pinned, offline-verifiable source provenance for ClearPass,
  AOS8, UXI, and Apstra generated manifests.
- Optional writes remain hidden in read-only mode and retain platform gates,
  dry-run previews, confirmation, bounded responses, and fail-closed errors.
- Generic dispatch remains explicitly destructive-capable; read-only dispatch
  continues through `invoke_read_tool`.

See the [capability gap matrix](capability-gap-matrix.md), [tool
catalog](tool-catalog.md), and [release index guide](release-indexes.md) for
reproducible counts and packaging details.
