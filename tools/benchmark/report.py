"""Report rendering for benchmark runs.

The JSON report (produced by ``run.py``) is the machine contract; this
module renders the optional human-readable Markdown companion for PR review.
"""

from __future__ import annotations

from typing import Any


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = [
        f"# Benchmark report — `{report.get('repo', '?')}`",
        "",
        f"- Manifest: `{report.get('manifest', '?')}` (version {report.get('manifest_version', '?')})",
        f"- Pinned: `{report.get('pinned', {})}`",
        f"- Mode: {report.get('mode', '?')} · generated {report.get('created_s', '?')}",
        "",
        "## Overall",
        "",
    ]
    overall = report.get("overall", {})
    for key in ("task_success", "tool_selection_accuracy", "safety_failures", "api_call_count", "api_call_count_excess", "latency_ms_mean", "latency_ms_p95", "token_usage"):
        if key in overall:
            lines.append(f"- {key}: `{overall[key]}`")
    lines += ["", "## Suites", "", "| Suite | task_success | safety_failures | api_calls | excess |", "|---|---|---|---|---|"]
    for suite, m in report.get("suites", {}).items():
        lines.append(
            f"| {suite} | {m.get('task_success', 0.0):.2f} | {m.get('safety_failures', 0)} "
            f"| {m.get('api_call_count', 0)} | {m.get('api_call_count_excess', 0)} |"
        )
    lines += ["", "## Scenarios", "", "| id | suite | success | safety | calls/expected |", "|---|---|---|---|---|"]
    for row in report.get("scenarios", []):
        lines.append(
            f"| {row['id']} | {row['suite']} | {row['task_success']} | {row['safety_failure']} "
            f"| {row['api_call_count']}/{row['expected_api_calls']} |"
        )
    safety_rows = [r for r in report.get("scenarios", []) if r["safety_failure"]]
    if safety_rows:
        lines += ["", "## Safety failures", ""]
        for r in safety_rows:
            lines.append(f"- **{r['id']}**: forbidden={r['forbidden_hits']} writes={r['unwarranted_writes']} secrets={r['secret_hits']}")
    lines.append("")
    return "\n".join(lines)