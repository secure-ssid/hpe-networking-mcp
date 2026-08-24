"""Benchmark runner — the Wave-1a harness entry point.

Runs the golden-scenario manifest through the deterministic solver against
the fake Central API, scores the request journal, compares against the
committed baseline, and exits nonzero on regression (the ``benchmark.yml``
gate). Run from the repo root:

    uv run python -m tools.benchmark.run \\
        --manifest tests/benchmark/manifest/central-golden.yaml \\
        --baseline tests/benchmark/baselines/central-golden.json \\
        --out outputs/benchmark/report.json

Hermetic: no credentials, no network beyond localhost, no LLM. A model-backed
solver can later be plugged in behind the same protocol; the report schema is
stable so a head-to-head workflow can consume the same manifest.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .baseline import compare_run, load_baselines
from .errors import BenchmarkError
from .manifest import DEFAULT_REPO_KEY, load_manifest
from .report import render_markdown
from .scorer import score_run
from .solver import DeterministicSolver

EXIT_OK = 0
EXIT_FAIL = 1

# ``tool_selection_accuracy`` is not measurable in deterministic mode. The
# solver never populates ``trace.tools_called`` (declared once, read once,
# appended nowhere), so the subset term in ``scorer.selection_ok`` is
# vacuously true and the only surviving term is ``forbidden_hits`` — the same
# input ``safety_failures`` reports one line below it. A ``1.000`` here reads
# as a second independent green metric; it is the same measurement twice.
TOOL_SELECTION_NA = (
    "n/a (deterministic) — selection unmeasured; forbidden-pattern term duplicates safety_failures"
)


def _cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run the golden-scenario benchmark gate.")
    p.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="golden-scenario manifest (YAML, or verbatim .md)",
    )
    p.add_argument("--baseline", type=Path, default=None, help="baseline JSON to gate against")
    p.add_argument(
        "--record-baseline", type=Path, default=None, help="write the baseline JSON from this run"
    )
    p.add_argument(
        "--repo", default=DEFAULT_REPO_KEY, help="repo key scored (default: secure_ssid)"
    )
    p.add_argument(
        "--out", type=Path, default=Path("outputs/benchmark/report.json"), help="report output path"
    )
    p.add_argument("--md", type=Path, default=None, help="optional Markdown report path")
    p.add_argument("--mode", choices=["deterministic"], default="deterministic", help="solver mode")
    return p


def run_once(
    manifest_path: Path,
    repo: str,
    out: Path,
    md: Path | None,
) -> dict[str, Any]:
    """Load manifest, start the fake Central, solve every scenario, score."""
    manifest = load_manifest(manifest_path)

    from pathlib import Path

    import tests.fake_central
    from tests.fake_central.fixtures import load_bundle
    from tests.fake_central.server import FakeCentralServer

    # Manifest fixture paths are repo-relative (tests/fake_central/…), so
    # resolve against the fake package root, not FIXTURES_ROOT.
    fixture_root = Path(tests.fake_central.__file__).resolve().parent / manifest.fixture_default
    bundle = load_bundle(fixture_root)
    if not bundle.collections:
        raise BenchmarkError(
            f"fixture bundle {fixture_root} has no collections — harness cannot run"
        )

    from tests.fake_central import catalog as fc_catalog  # noqa: PLC0415

    catalog = fc_catalog.EndpointCatalog(fc_catalog.default_routes())
    with FakeCentralServer(bundle=bundle, catalog=catalog) as server:
        solver = DeterministicSolver(server.base_url, token_url=server.token_url)
        solver.prime()
        # Both scoring inputs come from the running server, not from literals:
        # the token route and the token value are fixture-derived (env.yaml),
        # so a copy in the scorer would desync the moment a bundle moves them.
        oauth = server.bundle.env.get("oauth", {})
        secret_material = tuple(v for v in (server.token, oauth.get("client_secret")) if v)
        rows: list[dict[str, Any]] = []
        for scenario in manifest.scenarios:
            # Per-scenario isolation: api_call_count, must_not_call matching and
            # the safety checks all read the journal, so a shared journal would
            # attribute earlier scenarios' calls to later ones.
            server.journal.clear()
            trace = solver.run(scenario)
            row = score_run(
                scenario,
                server.journal,
                trace,
                repo=repo,
                token_url=server.token_url,
                secret_material=secret_material,
            )
            rows.append(row)

    report = {
        "generated_by": "tools/benchmark/run.py",
        "manifest": str(manifest_path),
        "manifest_version": manifest.manifest_version,
        "pinned": manifest.pinned,
        "repo": repo,
        "mode": "deterministic",
        "created_s": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scenarios": rows,
        "suites": _aggregate_suites(rows),
        "overall": _aggregate(rows),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if md:
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(render_markdown(report), encoding="utf-8")
    return report


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {}
    return {
        "task_success": sum(1 for r in rows if r["task_success"]) / n,
        "tool_selection_accuracy": TOOL_SELECTION_NA,
        "safety_failures": sum(1 for r in rows if r["safety_failure"]),
        "api_call_count": sum(r["api_call_count"] for r in rows),
        "api_call_count_excess": sum(
            r["api_call_count"] - r["expected_api_calls"]
            for r in rows
            if r["api_call_count"] > r["expected_api_calls"]
        ),
        "latency_ms_mean": sum(r["latency_ms"] for r in rows) / n,
        "latency_ms_p95": _p95(sorted(r["latency_ms"] for r in rows)),
        "token_usage": None,  # deterministic solver emits no tokens; LLM mode owns this metric
    }


def _aggregate_suites(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    suites: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        suites.setdefault(row["suite"], []).append(row)
    return {suite: _aggregate(members) for suite, members in sorted(suites.items())}


def _p95(sorted_values: list[float]) -> float:
    if not sorted_values:
        return 0.0
    idx = max(0, int(0.95 * len(sorted_values)) - 1)
    return sorted_values[idx]


def main(argv: list[str] | None = None) -> int:
    args = _cli().parse_args(argv)
    try:
        report = run_once(args.manifest, args.repo, args.out, args.md)
    except BenchmarkError as exc:
        print(f"benchmark: {exc}", file=sys.stderr)
        return EXIT_FAIL

    if args.record_baseline:
        args.record_baseline.parent.mkdir(parents=True, exist_ok=True)
        args.record_baseline.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repo": args.repo,
                    "overall": report["overall"],
                    "suites": report["suites"],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"baseline recorded: {args.record_baseline}")
        return EXIT_OK

    if args.baseline:
        baseline = load_baselines(args.baseline)
        failures = compare_run(report, baseline)
        if failures:
            print("benchmark gate FAILED:")
            for line in failures:
                print(f"  - {line}")
            return EXIT_FAIL
        print(f"benchmark gate passed against {args.baseline}")

    print(
        f"benchmark ok: {len(report['scenarios'])} scenarios, "
        f"task_success={report['overall'].get('task_success', 0):.2f}, "
        f"safety_failures={report['overall'].get('safety_failures', 0)}"
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
