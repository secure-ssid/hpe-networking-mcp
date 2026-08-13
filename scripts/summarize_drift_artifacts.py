#!/usr/bin/env python3
"""Aggregate per-check drift artifacts into one machine-readable summary.

Each drift gate writes its own report (``hpe_networking_mcp.pipeline.
drift_taxonomy``). CI runs those gates as independent jobs so one failure
cannot hide the others; this script then collects whatever reports were
produced -- including from jobs that failed -- and renders:

* ``outputs/drift/summary.json`` -- per-check dominant class, exit code, and
  a per-class count roll-up across every check; and
* an optional GitHub step-summary Markdown table (``--markdown``).

A check whose artifact is missing entirely is reported explicitly as
``missing_artifact`` rather than being quietly omitted -- "the job never
produced a report" and "the job reported no drift" must not look the same.

Exit codes mirror the taxonomy: the summary exits with the highest-precedence
failing class across all checks (``--exit-code-mode legacy`` collapses to 1),
or 0 when every check is fresh/coverage_gap/not_checked. ``--never-fail``
makes it a pure reporter for a summary job that should not re-fail the run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from hpe_networking_mcp.pipeline import drift_taxonomy as taxonomy  # noqa: E402

DEFAULT_INPUT_DIR = ROOT / "outputs" / "drift"
DEFAULT_SUMMARY_PATH = DEFAULT_INPUT_DIR / "summary.json"

#: Checks CI is expected to produce a report for. A missing one is surfaced,
#: never silently dropped.
EXPECTED_CHECKS: tuple[str, ...] = (
    "openapi_registry_drift",
    "mist_openapi_drift",
    "nowireless_community_input_drift",
    "product_spec_freshness",
    "security_lifecycle_drift",
    "rag_source_freshness",
)


def load_reports(input_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Load every ``*.json`` report under ``input_dir`` (recursively)."""
    reports: list[dict[str, Any]] = []
    unreadable: list[str] = []
    for path in sorted(input_dir.rglob("*.json")):
        if path.name == DEFAULT_SUMMARY_PATH.name:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            unreadable.append(f"{path.name}: {exc}")
            continue
        if isinstance(data, dict) and data.get("check") and "counts" in data:
            reports.append(data)
    return reports, unreadable


def build_summary(
    reports: list[dict[str, Any]],
    *,
    unreadable: list[str],
    expected: tuple[str, ...] = EXPECTED_CHECKS,
) -> dict[str, Any]:
    summary = taxonomy.summarize_reports(reports)
    present = {report.get("check") for report in reports}
    summary["missing_artifact"] = sorted(name for name in expected if name not in present)
    summary["unreadable_artifacts"] = unreadable
    summary["expected_checks"] = list(expected)
    return summary


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "## Source/API/RAG drift summary",
        "",
        "| Check | Result class | Exit | Refreshed | Content drift | Incomplete |",
        "|---|---|---|---|---|---|",
    ]
    for check in summary["checks"]:
        lines.append(
            f"| `{check['check']}` | `{check['summary_class']}` | "
            f"{check['exit_code']} | {'yes' if check['refresh_sources'] else 'no'} | "
            f"{'yes' if check['content_drift_detected'] else 'no'} | "
            f"{'yes' if check['check_incomplete'] else 'no'} |"
        )
    lines.extend(["", "### Result-class totals", ""])
    totals = {name: count for name, count in summary["totals"].items() if count}
    lines.append(
        ", ".join(f"`{name}`={count}" for name, count in totals.items()) or "_no findings_"
    )
    if summary["missing_artifact"]:
        lines.extend(
            [
                "",
                "> **Missing artifacts:** "
                + ", ".join(f"`{name}`" for name in summary["missing_artifact"])
                + " -- those checks produced no report (job failed before writing, or "
                "was skipped).",
            ]
        )
    if summary["unreadable_artifacts"]:
        lines.extend(
            ["", "> **Unreadable artifacts:** " + "; ".join(summary["unreadable_artifacts"])]
        )
    return "\n".join(lines) + "\n"


def summary_exit_code(summary: dict[str, Any], *, mode: str = "classified") -> int:
    if summary["missing_artifact"] or summary["unreadable_artifacts"]:
        return taxonomy.EXIT_USAGE if mode == "classified" else taxonomy.EXIT_LEGACY_FAIL
    codes = [check["exit_code"] for check in summary["checks"] if check["failing"]]
    if not codes:
        return taxonomy.EXIT_OK
    if mode == "legacy":
        return taxonomy.EXIT_LEGACY_FAIL
    by_precedence = {taxonomy.EXIT_CODES[name]: index
                     for index, name in enumerate(taxonomy.EXIT_PRECEDENCE)}
    return sorted(codes, key=lambda code: by_precedence.get(code, 99))[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_SUMMARY_PATH)
    parser.add_argument("--markdown", type=Path, default=None)
    parser.add_argument(
        "--expected",
        default=",".join(EXPECTED_CHECKS),
        help="Comma-separated checks that must have produced an artifact.",
    )
    parser.add_argument("--exit-code-mode", choices=taxonomy.EXIT_CODE_MODES, default="classified")
    parser.add_argument(
        "--never-fail",
        action="store_true",
        help="Always exit 0 (summary-only job; the per-check jobs own the failures).",
    )
    args = parser.parse_args(argv)

    if not args.input_dir.is_dir():
        print(f"No drift artifacts directory: {args.input_dir}", file=sys.stderr)
        reports: list[dict[str, Any]] = []
        unreadable: list[str] = []
    else:
        reports, unreadable = load_reports(args.input_dir)

    expected = tuple(name.strip() for name in args.expected.split(",") if name.strip())
    summary = build_summary(reports, unreadable=unreadable, expected=expected)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    markdown = render_markdown(summary)
    print(markdown)
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        with args.markdown.open("a", encoding="utf-8") as handle:
            handle.write(markdown)

    if args.never_fail:
        return 0
    return summary_exit_code(summary, mode=args.exit_code_mode)


if __name__ == "__main__":
    raise SystemExit(main())
