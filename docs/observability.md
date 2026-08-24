---
title: "Observability"
nav_order: 15
parent: "Reference"
---

# Observability

Health, readiness, and metrics surfaces. All of them live on the
streamable-HTTP transport (`MCP_TRANSPORT=streamable-http`); the stdio
transport exposes none of this — supervise a stdio deployment by its
process, not by endpoints.

| Surface | Path / flag | Auth |
|---|---|---|
| Liveness | `GET /livez` | None (always exempt) |
| Health | `GET /healthz` | None (always exempt) |
| Readiness | `GET /readyz` | None (always exempt) |
| Metrics | `GET /metrics` | Inherits bearer token + allow-lists |

When `MCP_HTTP_BEARER_TOKEN` is set, every HTTP path except the three
health paths requires `Authorization: Bearer <token>` — including
`/metrics`. The metrics route additionally inherits the loopback default
and the `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` DNS-rebinding
allow-lists, because it is registered on the same Starlette app as every
other route. Setting the bearer token on any transport other than
`streamable-http` refuses startup (`UnsafeHttpBindingError`) rather than
running an apparently-protected listener that is not.

## Health and readiness

Always registered on HTTP transports; they bypass MCP protocol negotiation
and never call an external API, so an orchestrator or load balancer can
poll them every few seconds.

- `GET /livez` → `200 {"status": "ok"}`. The process answers HTTP.
- `GET /healthz` → `200 {"status": "ok"}`. Alias of liveness.
- `GET /readyz` → `200 {"status": "ok", "detail": {...}}` when ready,
  `503 {"status": "not_ready", "detail": {...}}` otherwise. Readiness is
  process-local (no network calls): the credentials file at `CREDS_PATH`
  (default `config/credentials.yaml`) must exist AND parse via the same
  account-context builder the server uses. `detail` carries `creds_path`,
  `creds_path_exists`, `credentials_loadable`, and on failure a
  `credentials_error` string that never includes file contents or secrets
  (parse failures say "credential configuration is invalid"; unexpected
  errors name only the exception type).

A 503 from `/readyz` means "fix configuration", not "the process is down" —
liveness still returns 200, so orchestrators will not restart-loop a
misconfigured pod.

## Metrics

Two independent opt-ins:

| Flag | Effect |
|---|---|
| `HPE_MCP_METRICS=1` | Enables in-process collection (`1`/`true`/`yes`/`on`). |
| `HPE_MCP_METRICS_HTTP=1` | Registers `GET /metrics`. Without it nothing is served. |

Route without collection → `{"enabled": false, "hint": "..."}`. Collection
without the route → data stays in-process (tests and embedded use).

### What is collected

Per `(tool, backend)` series, bounded:

- `requests`, `errors` counters
- `outcomes` — closed vocabulary (`success` plus the classified error
  outcomes from `_middleware/_outcome.py`)
- `capabilities` — `read` / `write` / `destructive` / `diagnostic` /
  `unknown`
- latency: count, sum, max, and 9 fixed millisecond buckets
  (10/25/50/100/250/500/1000/2500/5000) plus an over-max counter
- `truncated_events` — responses the bounded-output path cut down

Plus a process-global rate-limit-wait aggregate (count/sum/max ms) and
`uptime_seconds`.

Cardinality is bounded by construction: at most 512 series, overflow folds
into an `_overflow_` bucket, label values are sanitized
(`[^a-z0-9_.-]` → `_`, 64-char cap), and outcome/capability vocabularies
are closed sets. The snapshot contains only labels and numeric aggregates —
never arguments, results, identifiers, or exception messages.

### JSON snapshot (current)

`GET /metrics` today returns the bounded JSON snapshot,
`schema_version: 1`:

```json
{
  "schema_version": 1,
  "enabled": true,
  "uptime_seconds": 123.4,
  "series_count": 17,
  "series_cap": 512,
  "series": [{"tool": "...", "backend": "...", "requests": 3, "...": "..."}],
  "rate_limit": {"wait_count": 0, "wait_sum_ms": 0.0, "wait_max_ms": 0.0}
}
```

The JSON schema is **explicitly unstable** — it is not part of any
compatibility promise.

### Prometheus exposition (ratified design, implementation pending)

Ratified 2026-08-24 (owner + engineering): `/metrics` will serve Prometheus
text exposition rendered from the same registry, with the JSON snapshot
retained via `?format=json` (or `Accept` negotiation). The renderer PR is
pending implementation; until it ships, the text format below is the
design contract, not shipped behavior.

Mapping from the registry:

| Prometheus metric | Type | Source |
|---|---|---|
| `hpe_mcp_tool_calls_total{tool,backend,capability,outcome}` | counter | requests split by capability × outcome |
| `hpe_mcp_tool_call_latency_ms{tool,backend}` | histogram | latency buckets, `le` cumulative |
| `hpe_mcp_tool_call_truncated_total{tool,backend}` | counter | truncated_events |
| `hpe_mcp_rate_limit_wait_ms` count/sum/max | counters/gauge | rate_limit aggregate |
| `hpe_mcp_metrics_series` / `hpe_mcp_metrics_series_cap` | gauges | series_count / series_cap |
| `hpe_mcp_metrics_uptime_seconds` | gauge | uptime_seconds |

One conversion rule matters: the registry records **per-bin** (exclusive)
bucket counts — `record_call` stops at the first matching edge — while
Prometheus histograms require **cumulative** `le` counts. The renderer must
accumulate left to right, and the over-max counter maps to `le="+Inf"`.
The implementation test asserts `sum(le counts) == latency_count`.

Metric names and labels join the 1.0 stability surface only when the
renderer ships; until then both the names above and the JSON schema are
unstable.

Full OTel SDK adoption (traces, OTLP push) is deliberately deferred until
distributed tracing is scoped, post-1.0. OTLP-only stacks are covered in
the meantime by pointing the OTel Collector's Prometheus receiver at this
endpoint.
