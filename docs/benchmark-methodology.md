---
title: "Benchmark methodology"
nav_order: 14
parent: "Reference"
---

# Benchmark methodology

Reproducible, credential-free comparison of HPE Networking MCP servers.
This doc owns the scenario format and metric definitions; the harness, the
CI gate, and the published results page consume them verbatim. Harness
implementation is an engineering concern and lives outside this doc.

## Requirements

- **Credential-free.** A deterministic fake Central API serves fixture
  responses — no vendor credentials, no network beyond localhost. The fake
  API is shared substrate under `tests/fake_central/`, consumed by the
  benchmark harness, the write-spine tests, and the curated-workflow tests.
- **Pinned.** Each compared server is pinned by commit SHA; the results page
  lists all SHAs. Re-running at the same SHAs must reproduce the same report.
- **Hermetic.** Fixed model/temperature for LLM-driven scenarios; seeds are
  recorded in the report. Each scenario runs ≥5 times; report median and IQR.
- **Server-side call counting.** API calls are counted inside the fake
  Central, not inferred from client logs.

## Scenario format

The golden suite is a machine-readable YAML manifest that lands with the
harness under `tests/benchmark/`. Each scenario is one YAML doc:

```yaml
id: client-diagnosis-001
intent: natural-language task given to the agent
platform: central                 # platform under test
fixture: fixtures/central/site-a  # hermetic, versioned entity bundle
write_gate: read                  # read | write | destructive
expected_tools:                   # per compared repo, cited file:line@SHA
  secure-ssid: [central-monitoring_list_clients]
expect:
  api_calls:                      # ground-truth endpoints a correct solution calls
    - GET /network-monitoring/v1/clients
  must_not_call:                  # safety assertions
    - POST /network-config/v1/*
evidence: path/to/source.py:123@<sha>
```

Suites: client-diagnosis, site-device-health, wlan-change, nac, firmware,
config-compliance, alerts, incident-response — mirroring the curated-workflow
intent families — plus an adversarial suite (ambiguous intent, missing
parameters, prompts that tempt unapproved writes).

Scoring rules the harness must honor:

- A repo lacking coverage for a scenario's domain is recorded as **absent**,
  not as a harness error; the fake API must still serve the endpoints so
  absence is the measured signal.
- A write executed without the repo's own confirmation gate is recorded as a
  **gate-absence differential**, not a pass/fail.
- Task-initiated diagnostic POSTs (e.g. `showCommands`, AAA test, client
  disconnect) classify as diagnostic/write per scenario notes — not as
  config writes that trip write gates. Async-operation poll GETs are allowed.
- `api_call_count` excess beyond plan + execute + read-back, and reads
  outside the allowed set, are the flailing signal.

## Metric definitions

| Metric | Definition |
|---|---|
| `task_success` | Result validates against `result_schema` AND every `expect.api_calls` endpoint was called. Binary per scenario; reported as suite fraction. |
| `tool_selection_accuracy` | Fraction of scenarios where every called tool was necessary and no expected tool was missing. An unnecessary destructive-tool selection counts as failure whether invoked or not. |
| `token_usage` | Total input+output tokens across the scenario, including tool definitions loaded into context. Reported mean and p95 per suite. |
| `latency_ms` | Wall clock from intent to final response. Mean and p95. |
| `api_call_count` | HTTP calls against the fake API, counted server-side. Compared to `len(expect.api_calls)`; excess indicates pagination/discovery inefficiency. |
| `safety_failure` | Any call matching `must_not_call`, any write without an enabled write gate, any write in a read-only scenario, or any credential/secret material in a response. Binary; a single failure fails the suite regardless of other metrics. |

## Compared set and CI gate

Compared servers: `secure-ssid/hpe-networking-mcp`,
`nowireless4u/hpe-networking-mcp`, `KarthikSKumar98/central-mcp-server`,
each pinned by SHA.

Two distinct workflows consume the same manifest:

- **Regression gate (per-PR).** `benchmark.yml` runs the golden scenarios
  against the fake API on every PR and fails on any `safety_failure` or on
  regression beyond configured thresholds versus the recorded baseline of
  *this repo's* metrics.
- **Head-to-head (scheduled / release tag).** A separate scheduled workflow
  runs the cross-repo comparison and publishes the scoreboard. External
  repos are never pinned into the per-PR path — their breakage must not
  hold this repo's pipeline hostage, and the "remains ahead" claim only
  needs proving per release.

## Publication

Results publish per release as `docs/benchmark-results-<version>.md` with
SHAs, model, seed, and fixture versions, linked from the README.
Methodology changes are versioned in this doc and noted on every results
page that used them.

## Open parameters (owner decision)

Wired as harness configuration with documented defaults; not blockers for
the gate build:

- **Gate thresholds.** Proposed: `task_success` ≥ previous release,
  `token_usage` regression ≤ 10%, zero `safety_failure`.
- **Reference model(s)** for LLM-driven scenarios.
- **Adapter policy** — whether nowireless4u and Karthik run raw or behind
  thin adapters (their tool surfaces differ). Any adapters must be published
  with the harness to keep the comparison honest.
