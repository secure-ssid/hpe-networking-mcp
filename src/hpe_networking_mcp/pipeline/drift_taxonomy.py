"""Shared result taxonomy for every source/API/RAG drift gate.

Before this module each drift check invented its own vocabulary
(``unchanged``/``changed``/``fetch_failed``, ``current``/``drift``/``error``,
``fresh``/``stale``/``unavailable``/``changed``/``coverage_gap``) and
collapsed everything non-zero onto exit code 1. That made three different
failure modes indistinguishable in CI:

* a real upstream **content** change,
* a **transient** network/rate-limit failure, and
* a **parser/layout** break where the check itself no longer understands
  the page it fetched.

A network blip must never be reported (or exit) as confirmed content drift,
and a reviewed pin that nobody has re-verified must never be reported as
fresh. This module is the single declaration of the result classes, their
exit codes, their precedence, and the machine-readable report shape that
every gate writes.

Result classes
--------------

``fresh``
    Checked and provably unchanged against the recorded baseline.
``content_drift``
    Fetched and parsed successfully, and the content digest/count really
    differs from the recorded baseline. The only class that means "upstream
    content changed".
``source_added``
    A source/spec/URL exists upstream (or on disk) that the manifest does
    not record yet.
``source_removed``
    A recorded source/spec/URL no longer exists upstream (404/410) or is
    gone from disk when it was required.
``pointer_change``
    The *pointer* or layout moved -- a ReadMe registry id, an api-next
    ``spec_uri``/branch, a sidebar/section membership, or a reviewed source
    URL set -- without a confirmed content comparison. Needs a resolver or
    pin update, not necessarily an ingest.
``stale_pin``
    A reviewed pin is behind upstream, or has not been re-verified (for
    example while external source refresh is deliberately disabled). Never
    reported as ``fresh``, and never silently advanced.
``unavailable``
    Transient/blocked: network error, timeout, 403/406/429/5xx, or refresh
    disabled at the transport level. The check did not complete; this is
    NOT drift.
``parser_error``
    Content was retrieved but could not be parsed/extracted (malformed
    JSON/XML, missing pointer, schema break in a local manifest). The check
    did not complete; this is NOT drift.
``coverage_gap``
    An explicit, documented, already-reviewed limitation (no official
    machine-readable source exists). Expected; never a failure on its own.
``not_checked``
    Deliberately skipped -- offline/plan-only mode, no local baseline, or
    an artifact that is git-ignored and legitimately absent. Expected;
    never a failure on its own.

Exit codes
----------

Each failing class owns a distinct exit code so a CI job's numeric result
alone identifies *what kind* of problem occurred (see :data:`EXIT_CODES`).
When one run produces several classes, :func:`exit_code_for` applies
:data:`EXIT_PRECEDENCE`, which deliberately ranks "the check could not
complete" (parser/unavailable) above "the check completed and found a
difference" -- an incomplete check must never be summarized as confirmed
drift. Every class is still present in the JSON report regardless of which
one won the exit code.

``--exit-code-mode legacy`` collapses any failing class onto ``1`` for
callers that predate the classified codes.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA_VERSION = 1

FRESH = "fresh"
CONTENT_DRIFT = "content_drift"
SOURCE_ADDED = "source_added"
SOURCE_REMOVED = "source_removed"
POINTER_CHANGE = "pointer_change"
STALE_PIN = "stale_pin"
UNAVAILABLE = "unavailable"
PARSER_ERROR = "parser_error"
COVERAGE_GAP = "coverage_gap"
NOT_CHECKED = "not_checked"

RESULT_CLASSES: tuple[str, ...] = (
    FRESH,
    CONTENT_DRIFT,
    SOURCE_ADDED,
    SOURCE_REMOVED,
    POINTER_CHANGE,
    STALE_PIN,
    UNAVAILABLE,
    PARSER_ERROR,
    COVERAGE_GAP,
    NOT_CHECKED,
)

#: Classes that are expected, reviewed states -- they never fail a gate.
PASSING_CLASSES: frozenset[str] = frozenset({FRESH, COVERAGE_GAP, NOT_CHECKED})

#: Classes that mean "upstream content really differs from the baseline".
CONTENT_CLASSES: frozenset[str] = frozenset({CONTENT_DRIFT, SOURCE_ADDED, SOURCE_REMOVED})

#: Classes that mean "the check itself could not complete" -- never drift.
INCOMPLETE_CLASSES: frozenset[str] = frozenset({UNAVAILABLE, PARSER_ERROR})

EXIT_OK = 0
EXIT_LEGACY_FAIL = 1
EXIT_USAGE = 2
EXIT_CONTENT_DRIFT = 3
EXIT_SOURCE_SET_CHANGED = 4
EXIT_POINTER_CHANGE = 5
EXIT_STALE_PIN = 6
EXIT_UNAVAILABLE = 7
EXIT_PARSER_ERROR = 8

EXIT_CODES: dict[str, int] = {
    FRESH: EXIT_OK,
    COVERAGE_GAP: EXIT_OK,
    NOT_CHECKED: EXIT_OK,
    CONTENT_DRIFT: EXIT_CONTENT_DRIFT,
    SOURCE_ADDED: EXIT_SOURCE_SET_CHANGED,
    SOURCE_REMOVED: EXIT_SOURCE_SET_CHANGED,
    POINTER_CHANGE: EXIT_POINTER_CHANGE,
    STALE_PIN: EXIT_STALE_PIN,
    UNAVAILABLE: EXIT_UNAVAILABLE,
    PARSER_ERROR: EXIT_PARSER_ERROR,
}

#: "Could not complete" first, then pointer/layout, then set changes, then
#: content, then an unverified pin. Documented and unit-tested so nobody
#: reorders it into "a 503 looks like drift".
EXIT_PRECEDENCE: tuple[str, ...] = (
    PARSER_ERROR,
    UNAVAILABLE,
    POINTER_CHANGE,
    SOURCE_REMOVED,
    SOURCE_ADDED,
    CONTENT_DRIFT,
    STALE_PIN,
)

EXIT_CODE_MODES: tuple[str, ...] = ("classified", "legacy")

MAX_DETAIL_CHARS = 500
MAX_FINDINGS = 2000


class DriftTaxonomyError(ValueError):
    """An invalid result class or malformed finding was supplied."""


def _bounded(text: Any, limit: int = MAX_DETAIL_CHARS) -> str:
    return " ".join(str(text or "").split())[:limit]


@dataclass(frozen=True)
class Finding:
    """One classified observation about one watched target."""

    target: str
    result_class: str
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    legacy_status: str | None = None

    def __post_init__(self) -> None:
        if self.result_class not in RESULT_CLASSES:
            raise DriftTaxonomyError(
                f"unknown result class {self.result_class!r}; "
                f"expected one of {', '.join(RESULT_CLASSES)}"
            )

    @property
    def failing(self) -> bool:
        return self.result_class not in PASSING_CLASSES

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "target": _bounded(self.target, 300),
            "result_class": self.result_class,
            "detail": _bounded(self.detail),
            "failing": self.failing,
        }
        if self.evidence:
            payload["evidence"] = _bounded_evidence(self.evidence)
        if self.legacy_status:
            payload["legacy_status"] = self.legacy_status
        return payload


def _bounded_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    bounded: dict[str, Any] = {}
    for key, value in list(evidence.items())[:40]:
        if isinstance(value, (int, float, bool)) or value is None:
            bounded[str(key)] = value
        elif isinstance(value, (list, tuple)):
            bounded[str(key)] = [_bounded(item, 200) for item in list(value)[:25]]
        else:
            bounded[str(key)] = _bounded(value)
    return bounded


def class_counts(findings: Iterable[Finding]) -> dict[str, int]:
    """Return a count for every class (zeros included, stable key order)."""
    counts = dict.fromkeys(RESULT_CLASSES, 0)
    for finding in findings:
        counts[finding.result_class] += 1
    return counts


def dominant_class(findings: Iterable[Finding]) -> str | None:
    """Return the class that decides the exit code, or None when all pass."""
    present = {finding.result_class for finding in findings}
    for candidate in EXIT_PRECEDENCE:
        if candidate in present:
            return candidate
    return None


def summary_class(counts: dict[str, int], dominant: str | None) -> str:
    """Return the class that best labels a whole check.

    ``dominant`` when the run failed; otherwise the *weakest* passing claim
    present, so a run made entirely of ``not_checked`` findings is never
    labelled ``fresh``.
    """
    if dominant:
        return dominant
    for candidate in (NOT_CHECKED, COVERAGE_GAP, FRESH):
        if counts.get(candidate):
            return candidate
    return FRESH


def exit_code_for(findings: Iterable[Finding], *, mode: str = "classified") -> int:
    """Map findings to a process exit code.

    Args:
        findings: classified observations.
        mode: ``classified`` (default) gives each failing class its own
            code; ``legacy`` collapses every failing class onto ``1``.
    """
    if mode not in EXIT_CODE_MODES:
        raise DriftTaxonomyError(f"unknown exit-code mode {mode!r}")
    findings = list(findings)
    winner = dominant_class(findings)
    if winner is None:
        return EXIT_OK
    if mode == "legacy":
        return EXIT_LEGACY_FAIL
    return EXIT_CODES[winner]


def build_report(
    check: str,
    findings: Sequence[Finding],
    *,
    refresh_sources: bool = False,
    exit_code_mode: str = "classified",
    notes: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the machine-readable report every drift gate emits.

    ``refresh_sources`` records, in the artifact itself, whether the run was
    allowed to fetch/advance anything. A report produced while refresh is
    disabled is explicitly not evidence of freshness.
    """
    findings = list(findings)[:MAX_FINDINGS]
    counts = class_counts(findings)
    winner = dominant_class(findings)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "check": check,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "refresh_sources": bool(refresh_sources),
        "exit_code_mode": exit_code_mode,
        "result_classes": list(RESULT_CLASSES),
        "counts": counts,
        "dominant_class": winner,
        "exit_code": exit_code_for(findings, mode=exit_code_mode),
        "failing": bool(winner),
        "content_drift_detected": any(
            finding.result_class in CONTENT_CLASSES for finding in findings
        ),
        "check_incomplete": any(
            finding.result_class in INCOMPLETE_CLASSES for finding in findings
        ),
        "notes": _bounded(notes, 1000),
        "findings": [finding.to_dict() for finding in findings],
    }
    if extra:
        report["extra"] = extra
    return report


def write_report(path: Path | str, report: dict[str, Any]) -> Path:
    """Atomically write ``report`` as pretty JSON, creating parent dirs."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, indent=2, sort_keys=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path


def summarize_reports(reports: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate several per-check reports into one summary payload."""
    totals = dict.fromkeys(RESULT_CLASSES, 0)
    checks: list[dict[str, Any]] = []
    for report in reports:
        counts = report.get("counts", {}) or {}
        for result_class in RESULT_CLASSES:
            totals[result_class] += int(counts.get(result_class, 0) or 0)
        counts_int = {k: int(counts.get(k, 0) or 0) for k in RESULT_CLASSES}
        checks.append(
            {
                "check": report.get("check", "unknown"),
                "dominant_class": report.get("dominant_class"),
                "summary_class": summary_class(counts_int, report.get("dominant_class")),
                "exit_code": report.get("exit_code", EXIT_OK),
                "failing": bool(report.get("failing")),
                "refresh_sources": bool(report.get("refresh_sources")),
                "content_drift_detected": bool(report.get("content_drift_detected")),
                "check_incomplete": bool(report.get("check_incomplete")),
                "counts": {k: int(counts.get(k, 0) or 0) for k in RESULT_CLASSES},
            }
        )
    failing = [entry for entry in checks if entry["failing"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "drift_summary",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checks": sorted(checks, key=lambda entry: entry["check"]),
        "totals": totals,
        "failing_checks": sorted(entry["check"] for entry in failing),
        "content_drift_detected": any(entry["content_drift_detected"] for entry in checks),
        "check_incomplete": any(entry["check_incomplete"] for entry in checks),
        "result_classes": list(RESULT_CLASSES),
    }


def add_common_arguments(parser: Any, *, default_artifact: Path | str | None = None) -> Any:
    """Register the flags every drift gate shares (artifact path, exit mode)."""
    parser.add_argument(
        "--json-artifact",
        type=Path,
        default=Path(default_artifact) if default_artifact else None,
        help="Write the machine-readable drift report to this path.",
    )
    parser.add_argument(
        "--no-artifact",
        action="store_true",
        help="Do not write the drift report artifact (status output only).",
    )
    parser.add_argument(
        "--exit-code-mode",
        choices=EXIT_CODE_MODES,
        default="classified",
        help=(
            "classified (default): one exit code per result class; "
            "legacy: any failing class exits 1."
        ),
    )
    return parser


def print_report(report: dict[str, Any], *, stream: Any = None) -> None:
    """Print a compact, deterministic human summary of a report."""
    import sys

    stream = stream or sys.stdout
    print(f"== {report['check']} ==", file=stream)
    for finding in report["findings"]:
        detail = f" -- {finding['detail']}" if finding.get("detail") else ""
        print(f"  {finding['result_class']:<15} {finding['target']}{detail}", file=stream)
    counts = ", ".join(
        f"{name}={count}" for name, count in report["counts"].items() if count
    )
    print(f"  counts: {counts or 'none'}", file=stream)
    print(
        f"  dominant_class={report['dominant_class']} exit_code={report['exit_code']} "
        f"refresh_sources={report['refresh_sources']}",
        file=stream,
    )
