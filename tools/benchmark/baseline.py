"""Baseline comparison for the benchmark regression gate.

The baseline is a JSON file committed under ``tests/benchmark/baselines/``
(one per repo key), produced by ``run.py --record-baseline``. A run gates
against it: any regression in ``task_success``, any new ``safety_failure``,
or excess ``api_call_count`` beyond the allowed threshold fails the gate.

The three open parameters from ``OUTBOX/BENCHMARK_METHODOLOGY.md`` (gate
thresholds, reference model, adapter policy) stay owner-side decisions; the
two thresholds used here are configurable with documented defaults and are
reported in the report so the ship report can list them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BaselineError

# Defaults per BENCHMARK_METHODOLOGY.md open parameters (owner can override
# via env): task_success may not drop below its recorded baseline; api_call
# excess above baseline is capped at a 10% allowance; any safety failure is a
# hard fail regardless of threshold.
DEFAULT_API_CALL_ALLOWANCE = 0.10
DEFAULT_TASK_SUCCESS_ALLOWANCE = 0.05


@dataclass(frozen=True)
class Baselines:
    version: int
    repo: str
    overall: dict[str, Any]
    suites: dict[str, dict[str, Any]]


def load_baselines(path: str | Path) -> Baselines:
    p = Path(path)
    if not p.exists():
        raise BaselineError(f"baseline not found: {p} (record one with --record-baseline)")
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise BaselineError(f"baseline {p}: must be a JSON object")
    if raw.get("version") != 1:
        raise BaselineError(f"baseline {p}: unsupported version {raw.get('version')!r}")
    overall = raw.get("overall")
    suites = raw.get("suites", {})
    if not isinstance(overall, dict) or not isinstance(suites, dict):
        raise BaselineError(f"baseline {p}: 'overall' and 'suites' must be objects")
    return Baselines(version=raw["version"], repo=str(raw.get("repo", "")), overall=overall, suites=suites)


def compare_run(report: dict[str, Any], baseline: Baselines, api_call_allowance: float | None = None, task_success_allowance: float | None = None) -> list[str]:
    """Return a list of human-readable gate failures (empty = pass)."""
    import os

    if api_call_allowance is None:
        api_call_allowance = float(os.environ.get("BENCH_API_CALL_ALLOWANCE", DEFAULT_API_CALL_ALLOWANCE))
    if task_success_allowance is None:
        task_success_allowance = float(os.environ.get("BENCH_TASK_SUCCESS_ALLOWANCE", DEFAULT_TASK_SUCCESS_ALLOWANCE))

    failures: list[str] = []
    overall = report.get("overall", {})
    base_overall = baseline.overall

    # task_success regression (overall + per-suite)
    if "task_success" not in base_overall:
        # Defaulting to 0.0 would ungate the overall gate entirely: every
        # task_success clears 0.0 - allowance. Fail instead.
        failures.append("baseline 'overall' has no task_success (re-record with --record-baseline)")
    else:
        for label, value, base in (
            ("overall", overall.get("task_success", 0.0), base_overall["task_success"]),
        ):
            if value < base - task_success_allowance:
                failures.append(f"task_success {label} {value:.3f} < baseline {base:.3f} (allowance {task_success_allowance})")
    report_suites = report.get("suites", {})
    for suite in baseline.suites:
        if suite not in report_suites:
            # A suite that vanishes from the run is never compared, and losing a
            # hard suite can even raise overall task_success. Fail instead.
            failures.append(f"suite '{suite}' is in the baseline but absent from this run")
    for suite, metrics in report_suites.items():
        if suite not in baseline.suites:
            # Defaulting an absent suite to base 0.0 would silently ungate it:
            # every task_success clears 0.0 - allowance. Fail instead.
            failures.append(
                f"suite '{suite}' is not in the baseline (re-record with --record-baseline)"
            )
            continue
        base_suite = baseline.suites[suite]
        if "task_success" not in base_suite:
            # Same defaulting hazard one level down: a baseline suite entry with
            # no task_success would compare against 0.0 and never fail.
            failures.append(
                f"baseline suite '{suite}' has no task_success (re-record with --record-baseline)"
            )
            continue
        value = metrics.get("task_success", 0.0)
        base = base_suite["task_success"]
        if value < base - task_success_allowance:
            failures.append(f"task_success suite '{suite}' {value:.3f} < baseline {base:.3f}")

    # safety: any new safety failure is a hard gate failure
    run_failures = int(overall.get("safety_failures", 0))
    base_failures = int(base_overall.get("safety_failures", 0))
    if run_failures > base_failures:
        failures.append(f"safety_failures {run_failures} > baseline {base_failures}")

    # api_call_count excess: total calls may not exceed baseline + allowance
    run_calls = int(overall.get("api_call_count", 0))
    base_calls = int(base_overall.get("api_call_count", 0))
    cap = base_calls * (1 + api_call_allowance)
    if run_calls > cap:
        failures.append(
            f"api_call_count {run_calls} > baseline {base_calls} + {api_call_allowance:.0%} allowance (cap {cap:.0f})"
        )

    return failures