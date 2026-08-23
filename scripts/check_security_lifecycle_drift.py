#!/usr/bin/env python3
"""Check official security/lifecycle sources for freshness and provenance.

Distinguishes five states per source (never a success-shaped fallback):

- ``fresh`` -- fetched, parsed, and met its committed minimum count.
- ``stale`` -- fetched and parsed, but the count regressed below the
  committed minimum.
- ``unavailable`` -- the source could not be fetched at all (network,
  timeout, HTTP error).
- ``changed`` -- fetched, but no longer parses the way its reviewed
  provenance pin (``ingestion/provenance/*.json``) expects -- a
  structural/schema break, or the source's own URLs no longer match the
  pin -- and needs human review before ``write_pin``/``--update-provenance``.
- ``coverage_gap`` -- an explicit, documented limitation (no reliable
  official machine-readable source exists yet), never silently reported
  as ``fresh``.

Writes a bounded, redacted ``source_freshness_result`` artifact (see
``src/hpe_networking_mcp/pipeline/artifact_contracts.py``) and exits non-zero if any source is
``stale``, ``unavailable``, or ``changed``. ``coverage_gap`` is an expected,
already-reviewed state and does not fail the check on its own.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from hpe_networking_mcp.pipeline import artifact_contracts as contracts  # noqa: E402
from hpe_networking_mcp.pipeline import drift_taxonomy as taxonomy  # noqa: E402
from ingestion import lifecycle_provenance as provenance  # noqa: E402
from ingestion import scrape_security_lifecycle as sources  # noqa: E402

MIN_ARUBA_ADVISORIES = provenance.minimum_count(provenance.SECURITY_ADVISORIES)
MIN_HPE_LIFECYCLE_NOTICES = provenance.minimum_count(
    provenance.HPE_LIFECYCLE_NOTICES
)
MIN_JUNIPER_LIFECYCLE_PAGES = provenance.minimum_count(
    provenance.JUNIPER_LIFECYCLE_PAGES
)
MIN_JUNIPER_SECURITY_BULLETINS = provenance.minimum_count(
    provenance.JUNIPER_SECURITY_ADVISORIES
)

STATUS_FRESH = "fresh"
STATUS_STALE = "stale"
STATUS_UNAVAILABLE = "unavailable"
STATUS_CHANGED = "changed"
STATUS_COVERAGE_GAP = "coverage_gap"

_FAILING_STATUSES = (STATUS_STALE, STATUS_UNAVAILABLE, STATUS_CHANGED)
_MAX_DETAIL_CHARS = contracts.MAX_FRESHNESS_DETAIL_CHARS
_DEFAULT_ARTIFACT_PATH = ROOT / "outputs" / "source-freshness.json"
_DEFAULT_DRIFT_ARTIFACT_PATH = ROOT / "outputs" / "drift" / "security-lifecycle-drift.json"
CHECK_NAME = "security_lifecycle_drift"

# The five source-local statuses above stay the vocabulary of this check's
# own artifact; ``result_class`` maps them onto the shared taxonomy so CI can
# compare this gate with the OpenAPI/Mist/community gates. ``changed``
# deliberately fans out into two classes: a provenance *identity* break is a
# pointer/layout change, while a parser blowing up on already-fetched content
# is a parser failure -- different remediation, different exit code.
_RESULT_CLASS_BY_STATUS = {
    STATUS_FRESH: taxonomy.FRESH,
    STATUS_STALE: taxonomy.CONTENT_DRIFT,
    STATUS_UNAVAILABLE: taxonomy.UNAVAILABLE,
    STATUS_CHANGED: taxonomy.POINTER_CHANGE,
    STATUS_COVERAGE_GAP: taxonomy.COVERAGE_GAP,
}


def _bounded_detail(text: str) -> str:
    text = " ".join(str(text).split())
    return text[:_MAX_DETAIL_CHARS]


def _entry(
    source: str,
    count: int,
    minimum: int,
    status: str,
    detail: str = "",
    *,
    result_class: str | None = None,
) -> dict[str, Any]:
    return {
        "source": source,
        "count": max(0, int(count)),
        "minimum": max(0, int(minimum)),
        "status": status,
        "result_class": result_class or _RESULT_CLASS_BY_STATUS[status],
        "drift_detected": status not in (STATUS_FRESH, STATUS_COVERAGE_GAP),
        "detail": _bounded_detail(detail),
    }


def _evaluate(
    *,
    source: str,
    minimum: int,
    fetch: Callable[[], Any],
    parse: Callable[[Any], Any],
    count_of: Callable[[Any], int] = len,
) -> dict[str, Any]:
    """Evaluate one source with fetch/parse failures classified separately.

    A failure while *fetching* raw bytes/text is reported as
    ``unavailable`` (connectivity); a failure while *parsing* already-
    fetched content is reported as ``changed`` (schema/structure), since
    the two need different remediation.
    """
    try:
        raw = fetch()
    except sources.SourceFetchError as exc:
        return _entry(source, 0, minimum, STATUS_UNAVAILABLE, str(exc))
    try:
        parsed = parse(raw)
    except provenance.SourceProvenanceError as exc:
        # Reviewed source identity/structural markers no longer match: the
        # pointer/layout moved, the content was never compared.
        return _entry(
            source, 0, minimum, STATUS_CHANGED, str(exc), result_class=taxonomy.POINTER_CHANGE
        )
    except sources.SourceFetchError as exc:
        # Already-fetched content that will not parse is a parser failure,
        # not evidence that the upstream content itself drifted.
        return _entry(
            source, 0, minimum, STATUS_CHANGED, str(exc), result_class=taxonomy.PARSER_ERROR
        )
    count = count_of(parsed)
    if count < minimum:
        return _entry(
            source, count, minimum, STATUS_STALE, f"count {count} below minimum {minimum}"
        )
    return _entry(source, count, minimum, STATUS_FRESH)


def _changed_from(entry: dict[str, Any], exc: Exception) -> dict[str, Any]:
    """Downgrade an already-fresh entry to ``changed`` after a provenance mismatch.

    A provenance mismatch is a reviewed-identity/pointer change, so it maps
    to ``pointer_change`` -- never to ``content_drift``.
    """
    return _entry(
        entry["source"],
        entry["count"],
        entry["minimum"],
        STATUS_CHANGED,
        str(exc),
        result_class=taxonomy.POINTER_CHANGE,
    )


def evaluate_aruba_security() -> dict[str, Any]:
    entry = _evaluate(
        source="aruba_advisories",
        minimum=MIN_ARUBA_ADVISORIES,
        fetch=lambda: sources.fetch_text(sources.ARUBA_CSAF_CHANGES),
        parse=sources.parse_changes_csv,
    )
    if entry["status"] == STATUS_FRESH:
        try:
            provenance.validate_source_identity(
                provenance.SECURITY_ADVISORIES,
                [sources.ARUBA_CSAF_BASE, sources.ARUBA_CSAF_CHANGES],
            )
        except provenance.SourceProvenanceError as exc:
            return _changed_from(entry, exc)
    return entry


def evaluate_hpe_lifecycle() -> dict[str, Any]:
    def _fetch_all() -> dict[str, Any]:
        return {
            "xml": sources.fetch_text(sources.HPE_LIFECYCLE_XML),
            "policy": sources.fetch_text(sources.HPE_LIFECYCLE_POLICY),
            "hardware_pdf": sources.fetch_bytes(sources.ARUBA_HARDWARE_EOS_PDF),
        }

    def _parse_with_marker_check(raw: dict[str, Any]) -> list[dict[str, Any]]:
        xml_text = raw["xml"]
        provenance.validate_markers(
            provenance.HPE_LIFECYCLE_NOTICES,
            xml_text,
        )
        sources.extract_hpe_lifecycle_policy_text(raw["policy"])
        sources.extract_aruba_hardware_eos_text(raw["hardware_pdf"])
        return sources.parse_hpe_lifecycle_xml(xml_text)

    entry = _evaluate(
        source="hpe_lifecycle_notices",
        minimum=MIN_HPE_LIFECYCLE_NOTICES,
        fetch=_fetch_all,
        parse=_parse_with_marker_check,
    )
    if entry["status"] == STATUS_FRESH:
        try:
            provenance.validate_source_identity(
                provenance.HPE_LIFECYCLE_NOTICES,
                [
                    sources.HPE_LIFECYCLE_XML,
                    sources.HPE_LIFECYCLE_POLICY,
                    sources.ARUBA_HARDWARE_EOS_PDF,
                ],
            )
        except provenance.SourceProvenanceError as exc:
            return _changed_from(entry, exc)
    return entry


def evaluate_juniper_lifecycle() -> dict[str, Any]:
    discovered_urls: list[str] = []

    def _fetch_all() -> dict[str, str]:
        urls = sources.discover_juniper_lifecycle_urls()
        discovered_urls.extend(urls.values())
        return {url: sources.fetch_text(url) for url in urls.values()}

    def _parse_all(pages: dict[str, str]) -> list[str]:
        return [
            sources.render_juniper_lifecycle_page(html_text, url)
            for url, html_text in pages.items()
        ]

    entry = _evaluate(
        source="juniper_lifecycle_pages",
        minimum=MIN_JUNIPER_LIFECYCLE_PAGES,
        fetch=_fetch_all,
        parse=_parse_all,
    )
    if entry["status"] == STATUS_FRESH:
        try:
            provenance.validate_source_identity(
                provenance.JUNIPER_LIFECYCLE_PAGES,
                [sources.JUNIPER_EOL_INDEX_URL, *discovered_urls],
            )
        except provenance.SourceProvenanceError as exc:
            return _changed_from(entry, exc)
    return entry


def evaluate_juniper_security() -> dict[str, Any]:
    """Evaluate the Juniper security-bulletin sitemap-index discovery chain.

    ``discover_juniper_security_sitemaps()`` fetches and parses the official
    sitemap index (``JUNIPER_SECURITY_SITEMAP_INDEX_URL``) into its current
    topic-article child sitemap URLs; any index network failure or
    structural break (malformed XML, wrong root, off-host/non-HTTPS/unsafe
    child URL, zero matching children) raises ``SourceFetchError`` from
    inside the ``fetch`` phase and is reported as ``unavailable`` here, the
    same as an unreachable child sitemap fetch -- both are "could not
    assemble the set of official child sitemaps to read" failures. A
    malformed *child* sitemap body, in contrast, is only discovered once its
    (already-fetched) text is parsed in the ``parse`` phase, so it is
    reported as ``changed`` (a structural/schema break in already-reachable
    content) rather than ``unavailable``.
    """

    def _fetch_all() -> dict[str, str]:
        children = sources.discover_juniper_security_sitemaps()
        return {child: sources.fetch_text(child) for child in children}

    def _parse_all(children_xml: dict[str, str]) -> set[str]:
        urls: set[str] = set()
        for xml_text in children_xml.values():
            urls |= sources.parse_juniper_security_sitemap(xml_text)
        return urls

    entry = _evaluate(
        source="juniper_security_bulletins",
        minimum=MIN_JUNIPER_SECURITY_BULLETINS,
        fetch=_fetch_all,
        parse=_parse_all,
    )
    if entry["status"] == STATUS_FRESH:
        try:
            provenance.validate_source_identity(
                provenance.JUNIPER_SECURITY_ADVISORIES,
                [sources.JUNIPER_SECURITY_SITEMAP_INDEX_URL],
            )
        except provenance.SourceProvenanceError as exc:
            return _changed_from(entry, exc)
    return entry


def evaluate_hpe_aruba_coverage_gap() -> dict[str, Any]:
    """Return the explicit, evidenced coverage-gap state (never attempted-fresh)."""
    gap = sources.HPE_ARUBA_CURRENT_LIFECYCLE_COVERAGE_GAP
    detail = gap["reason"] + " " + " ".join(gap["evidence"])
    return _entry(gap["source"], 0, 0, STATUS_COVERAGE_GAP, detail)


EVALUATORS: tuple[Callable[[], dict[str, Any]], ...] = (
    evaluate_aruba_security,
    evaluate_hpe_lifecycle,
    evaluate_juniper_lifecycle,
    evaluate_juniper_security,
    evaluate_hpe_aruba_coverage_gap,
)


def evaluate_sources() -> list[dict[str, Any]]:
    """Evaluate every tracked source and return one entry dict per source."""
    return [evaluator() for evaluator in EVALUATORS]


def check_sources() -> dict[str, int]:
    """Backward-compatible plain count summary (raises on any failing state)."""
    entries = evaluate_sources()
    counts = {entry["source"]: entry["count"] for entry in entries}
    failing = [
        f"{entry['source']}={entry['status']} ({entry['detail']})"
        for entry in entries
        if entry["status"] in _FAILING_STATUSES
    ]
    if failing:
        raise SystemExit("Security/lifecycle source coverage regressed: " + "; ".join(failing))
    return counts


def build_freshness_artifact(
    entries: list[dict[str, Any]], *, output_path: Path = _DEFAULT_ARTIFACT_PATH
) -> contracts.ManifestEntry:
    """Write the bounded, redacted, deterministic source-freshness artifact."""
    # The source_freshness_result contract is strict and versioned: project
    # each entry down to exactly its declared fields rather than widening the
    # contract. The taxonomy ``result_class`` travels in this check's separate
    # drift report (see build_drift_report), so neither artifact loses
    # information and the strict gate stays strict.
    contract_fields = ("source", "count", "minimum", "status", "drift_detected", "detail")
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entries": [
            {key: entry[key] for key in contract_fields if key in entry} for entry in entries
        ],
    }
    return contracts.write_artifact(output_path, contracts.SOURCE_FRESHNESS_RESULT, payload)


def build_drift_report(
    entries: list[dict[str, Any]], *, exit_code_mode: str = "classified"
) -> dict[str, Any]:
    """Render the evaluated entries as a shared-taxonomy drift report."""
    findings = [
        taxonomy.Finding(
            target=entry["source"],
            result_class=entry["result_class"],
            detail=entry["detail"],
            legacy_status=entry["status"],
            evidence={"count": entry["count"], "minimum": entry["minimum"]},
        )
        for entry in entries
    ]
    return taxonomy.build_report(
        CHECK_NAME,
        findings,
        refresh_sources=True,
        exit_code_mode=exit_code_mode,
        notes=(
            "Official security/lifecycle sources. coverage_gap entries are "
            "expected, reviewed boundaries (see docs/source-lifecycle-coverage.md)."
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact-path",
        type=Path,
        default=_DEFAULT_ARTIFACT_PATH,
        help="Where to write the source_freshness_result artifact.",
    )
    parser.add_argument(
        "--no-artifact",
        action="store_true",
        help="Skip writing the freshness artifact (status output only).",
    )
    parser.add_argument(
        "--drift-artifact-path",
        type=Path,
        default=_DEFAULT_DRIFT_ARTIFACT_PATH,
        help="Where to write the shared-taxonomy drift report.",
    )
    parser.add_argument(
        "--no-drift-artifact",
        action="store_true",
        help="Skip writing the shared-taxonomy drift report.",
    )
    parser.add_argument(
        "--exit-code-mode",
        choices=taxonomy.EXIT_CODE_MODES,
        default="classified",
        help=(
            "classified (default): one exit code per result class; "
            "legacy: any failing state exits 1."
        ),
    )
    args = parser.parse_args()

    entries = evaluate_sources()
    for entry in entries:
        detail = f" -- {entry['detail']}" if entry["detail"] else ""
        print(
            f"{entry['source']}: {entry['status']}/{entry['result_class']} "
            f"({entry['count']}/{entry['minimum']}){detail}"
        )

    if not args.no_artifact:
        manifest_entry = build_freshness_artifact(entries, output_path=args.artifact_path)
        print(f"wrote {manifest_entry.filename} ({manifest_entry.sha256[:12]}...)")

    report = build_drift_report(entries, exit_code_mode=args.exit_code_mode)
    if args.drift_artifact_path and not args.no_drift_artifact:
        print(f"wrote {taxonomy.write_report(args.drift_artifact_path, report)}")

    failing = [entry for entry in entries if entry["status"] in _FAILING_STATUSES]
    if failing:
        detail = "; ".join(
            f"{entry['source']}={entry['status']}/{entry['result_class']}" for entry in failing
        )
        print(f"Security/lifecycle source coverage regressed: {detail}", file=sys.stderr)
    if report["check_incomplete"]:
        print(
            "Some sources could not be checked (network/parser failure); that is not "
            "confirmed content drift.",
            file=sys.stderr,
        )
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
