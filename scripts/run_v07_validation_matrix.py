#!/usr/bin/env python3
"""Unified, credential-gated v0.7 validation matrix runner.

Classifies every v0.7-covered category -- ``central``, ``glp``, ``aos8``,
``clearpass``, ``mist``, ``apstra``, ``edgeconnect``, ``uxi``, ``axis``,
``rag_source_freshness``, and ``router_automation`` -- into exactly one of
six bounded states (see
``hpe_networking_mcp.pipeline.artifact_contracts.VALIDATION_MATRIX_CLASSIFICATIONS``):

- ``offline_fixture`` -- a bounded, network-free fixture/self-check ran and
  passed.
- ``live_read`` -- the platform's read live-test opt-in is set and its
  credentials are configured, so bounded live reads are *authorized*.
- ``disposable_write`` -- both the read and write live-test opt-ins are set
  and credentials are configured, so a disposable create/read-back/delete
  round trip is *authorized*.
- ``blocked`` -- the default, safe state: no offline self-check exists for
  this category and its read opt-in is not set.
- ``unavailable`` -- an opt-in was set but credentials are not configured,
  or an offline self-check itself raised/failed.
- ``coverage_gap`` -- a permanent, already-documented limitation (e.g. no
  live write API exists for this platform at all), not merely an unset
  opt-in.

This runner is deliberately never the thing that performs a live call: for
every one of the nine platforms it only ever (a) runs that platform's own
already-existing, always-safe offline/fixture helper (imported, never
duplicated) and (b) inspects ``hpe_networking_mcp.pipeline.live_test_config``'s status API.
It never itself invokes a live-read HTTP call or a disposable-write
lifecycle -- that stays exactly where each dedicated v0.7 evaluator script
already puts it (``scripts/evaluate_central_070_readonly.py``,
``scripts/evaluate_glp_070_depth.py``,
``scripts/evaluate_aos8_070_disposable_lifecycle.py``,
``scripts/evaluate_axis_lab.py``, ``src/hpe_networking_mcp/pipeline/optional_product_evidence.py``),
so ``live_read``/``disposable_write`` here are authorization-state labels,
never execution reports. An operator who wants an actual bounded live read
or disposable-write round trip runs that platform's own evaluator script
directly.

Usage::

    uv run python scripts/run_v07_validation_matrix.py
    uv run python scripts/run_v07_validation_matrix.py --output outputs/validation-matrix.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from hpe_networking_mcp.pipeline import artifact_contracts as contracts  # noqa: E402
from hpe_networking_mcp.pipeline import live_test_config  # noqa: E402

DEFAULT_OUTPUT = REPO_ROOT / "outputs" / "validation-matrix.json"

# Additional, non-platform categories folded into the same matrix.
_EXTRA_CATEGORIES: tuple[str, ...] = ("rag_source_freshness", "router_automation")
CATEGORIES: tuple[str, ...] = live_test_config.LIVE_TEST_PLATFORMS + _EXTRA_CATEGORIES

# Platforms whose disposable-write capability is a permanent, already
# documented upstream API omission (see
# hpe_networking_mcp.pipeline.optional_product_evidence.PERMANENT_OMISSIONS) rather than
# merely an unset opt-in -- reported as coverage_gap even if both live-test
# opt-ins happen to be set.
_PERMANENT_WRITE_GAP_PLATFORMS: tuple[str, ...] = ("uxi",)

_MAX_DETAIL_CHARS = contracts.MAX_VALIDATION_MATRIX_DETAIL_CHARS


def _truncate(text: str) -> str:
    text = " ".join(str(text).split())
    if len(text) <= _MAX_DETAIL_CHARS:
        return text
    return text[: _MAX_DETAIL_CHARS - 3] + "..."


def _entry(
    category: str,
    classification: str,
    detail: str,
    *,
    read_enabled: bool,
    write_enabled: bool,
    credentials_configured: bool,
) -> dict[str, Any]:
    return {
        "category": category,
        "classification": classification,
        "detail": _truncate(detail),
        "read_enabled": bool(read_enabled),
        "write_enabled": bool(write_enabled),
        "credentials_configured": bool(credentials_configured),
    }


# ---------------------------------------------------------------------------
# Per-platform offline/fixture self-checks -- each one imports and calls an
# existing, already-safe helper (never a duplicated implementation).
# ---------------------------------------------------------------------------


def _offline_central() -> tuple[bool, str]:
    from scripts import evaluate_central_070_readonly as central_eval

    steps = central_eval._offline_steps()
    ok = bool(steps) and all(step.get("status") == "ok" for step in steps)
    return ok, f"evaluate_central_070_readonly offline fixture: {len(steps)} step(s), ok={ok}"


def _offline_aos8() -> tuple[bool, str]:
    from scripts import evaluate_aos8_070_disposable_lifecycle as aos8_eval

    report = aos8_eval.status_report()
    kinds = report.get("kinds", {})
    supported = sum(1 for k in kinds.values() if k.get("supports_write"))
    return True, (
        f"evaluate_aos8_070_disposable_lifecycle status_report: {len(kinds)} lifecycle "
        f"kind(s) described, {supported} support disposable-write mapping"
    )


def _offline_optional_product(platform: str) -> tuple[bool, str]:
    from hpe_networking_mcp.pipeline import optional_product_evidence as evidence

    entry = evidence.build_compatibility_entry(platform)
    return entry.compatible, (
        f"optional_product_evidence compatibility check: compatible={entry.compatible}, "
        f"+{entry.operations_added}/-{entry.operations_removed} op(s), "
        f"{len(entry.reasons)} reason(s)"
    )


def _offline_axis() -> tuple[bool, str]:
    from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import load_manifest
    from scripts import evaluate_axis_lab as axis_eval

    manifest = load_manifest("axis")
    report = axis_eval.verify_split_crud_contract(manifest)
    return bool(report["compatible"]), (
        f"evaluate_axis_lab split-CRUD contract check: "
        f"{report['complete_split_crud_families']}/{report['family_count']} families complete"
    )


_OFFLINE_CHECKS: dict[str, Callable[[], tuple[bool, str]]] = {
    "central": _offline_central,
    "aos8": _offline_aos8,
    "apstra": lambda: _offline_optional_product("apstra"),
    "clearpass": lambda: _offline_optional_product("clearpass"),
    "edgeconnect": lambda: _offline_optional_product("edgeconnect"),
    "mist": lambda: _offline_optional_product("mist"),
    "uxi": lambda: _offline_optional_product("uxi"),
    "axis": _offline_axis,
}


def _classify_platform(platform: str) -> dict[str, Any]:
    status = live_test_config.live_test_status(platform)
    read_enabled = bool(status["read_enabled"])
    write_enabled = bool(status["write_enabled"])
    credentials_ok = bool(status["credentials_configured"])

    if platform in _PERMANENT_WRITE_GAP_PLATFORMS and write_enabled and credentials_ok:
        return _entry(
            platform,
            "coverage_gap",
            "disposable-write is a permanent, already-documented upstream API gap for "
            "this platform even though both live-test opt-ins are set and credentials "
            "are configured (see optional_product_evidence.PERMANENT_OMISSIONS)",
            read_enabled=read_enabled,
            write_enabled=write_enabled,
            credentials_configured=credentials_ok,
        )

    if write_enabled and credentials_ok:
        return _entry(
            platform,
            "disposable_write",
            f"{live_test_config.live_test_read_env_var(platform)}=1 and "
            f"{live_test_config.live_test_write_env_var(platform)}=1 are both set and "
            "credentials are configured; the disposable-write harness is authorized "
            "but is never invoked by this matrix runner -- run that platform's "
            "dedicated evaluator script directly to execute it.",
            read_enabled=read_enabled,
            write_enabled=write_enabled,
            credentials_configured=credentials_ok,
        )

    if read_enabled and credentials_ok:
        return _entry(
            platform,
            "live_read",
            f"{live_test_config.live_test_read_env_var(platform)}=1 is set and "
            "credentials are configured; bounded live reads are authorized but are "
            "never invoked by this matrix runner -- run that platform's dedicated "
            "evaluator script directly to execute them.",
            read_enabled=read_enabled,
            write_enabled=write_enabled,
            credentials_configured=credentials_ok,
        )

    # An explicit read opt-in with no configured credentials is reported as
    # "unavailable" even for a platform that also has a working offline
    # fixture path below -- the operator opted into live testing and that
    # specific request cannot succeed, which is the more actionable signal.
    if read_enabled and not credentials_ok:
        return _entry(
            platform,
            "unavailable",
            f"{live_test_config.live_test_read_env_var(platform)}=1 is set but "
            "credentials are not configured",
            read_enabled=read_enabled,
            write_enabled=write_enabled,
            credentials_configured=credentials_ok,
        )

    check = _OFFLINE_CHECKS.get(platform)
    if check is not None:
        try:
            ok, detail = check()
        except Exception as exc:  # noqa: BLE001 - one platform failing must never crash the matrix
            return _entry(
                platform,
                "unavailable",
                f"offline self-check raised: {exc}",
                read_enabled=read_enabled,
                write_enabled=write_enabled,
                credentials_configured=credentials_ok,
            )
        if ok:
            return _entry(
                platform,
                "offline_fixture",
                detail,
                read_enabled=read_enabled,
                write_enabled=write_enabled,
                credentials_configured=credentials_ok,
            )
        return _entry(
            platform,
            "unavailable",
            f"offline self-check reported failure: {detail}",
            read_enabled=read_enabled,
            write_enabled=write_enabled,
            credentials_configured=credentials_ok,
        )

    return _entry(
        platform,
        "blocked",
        "no offline self-check is available for this platform and "
        f"{live_test_config.live_test_read_env_var(platform)} is not set",
        read_enabled=read_enabled,
        write_enabled=write_enabled,
        credentials_configured=credentials_ok,
    )


# ---------------------------------------------------------------------------
# RAG / source-freshness category
# ---------------------------------------------------------------------------


def _classify_rag_source_freshness() -> dict[str, Any]:
    from scripts.validate_release import _rag_indexes_available

    if not _rag_indexes_available(REPO_ROOT):
        return _entry(
            "rag_source_freshness",
            "coverage_gap",
            "local RAG index (data/docs.lance, data/specs.sqlite) is not present; "
            "download via scripts/download_indexes.py or build via "
            "ingestion/ingest_docs.py",
            read_enabled=False,
            write_enabled=False,
            credentials_configured=False,
        )

    freshness_path = REPO_ROOT / "outputs" / "source-freshness.json"
    if not freshness_path.is_file():
        return _entry(
            "rag_source_freshness",
            "blocked",
            "RAG index is present but no local outputs/source-freshness.json snapshot "
            "exists yet; run scripts/check_security_lifecycle_drift.py (requires network "
            "access to public security/lifecycle sources) or rely on the scheduled "
            "security-lifecycle-drift CI job",
            read_enabled=False,
            write_enabled=False,
            credentials_configured=False,
        )

    try:
        payload = json.loads(freshness_path.read_text(encoding="utf-8"))
        artifact = contracts.build_artifact(contracts.SOURCE_FRESHNESS_RESULT, payload)
    except Exception as exc:  # noqa: BLE001 - a malformed snapshot is evidence, not a crash
        return _entry(
            "rag_source_freshness",
            "unavailable",
            f"outputs/source-freshness.json failed schema validation: {exc}",
            read_enabled=False,
            write_enabled=False,
            credentials_configured=False,
        )

    counts = Counter(source_entry.status for source_entry in artifact.entries)
    detail = f"{len(artifact.entries)} source(s): " + ", ".join(
        f"{status}={count}" for status, count in sorted(counts.items())
    )
    return _entry(
        "rag_source_freshness",
        "offline_fixture",
        detail,
        read_enabled=False,
        write_enabled=False,
        credentials_configured=False,
    )


# ---------------------------------------------------------------------------
# Router automation category
# ---------------------------------------------------------------------------


def _classify_router_automation() -> dict[str, Any]:
    import hpe_networking_mcp.mcp_servers.tool_router as router
    from scripts.generate_router_automation_report import _EXAMPLE_DEPENDENCY_STEPS, _call

    router._load_all_backends()
    plan_fn = getattr(router, "plan_tool_workflow", None)
    if plan_fn is None:
        return _entry(
            "router_automation",
            "coverage_gap",
            "plan_tool_workflow is not registered in this environment "
            "(HPE_MCP_ROUTER_MODE=minimal)",
            read_enabled=False,
            write_enabled=False,
            credentials_configured=False,
        )
    try:
        result = _call(plan_fn, list(_EXAMPLE_DEPENDENCY_STEPS), include_candidates=False)
    except Exception as exc:  # noqa: BLE001 - a planner failure is evidence, not a crash
        return _entry(
            "router_automation",
            "unavailable",
            f"plan_tool_workflow raised: {exc}",
            read_enabled=False,
            write_enabled=False,
            credentials_configured=False,
        )
    if result.get("artifact") is None:
        return _entry(
            "router_automation",
            "unavailable",
            str(result.get("artifact_error") or "plan_tool_workflow returned no artifact"),
            read_enabled=False,
            write_enabled=False,
            credentials_configured=False,
        )
    return _entry(
        "router_automation",
        "offline_fixture",
        "plan_tool_workflow produced a valid dry-run dependency plan against the "
        f"currently enabled backend catalog ({len(router._BACKENDS)} server(s)); "
        "no network call, no execution",
        read_enabled=False,
        write_enabled=False,
        credentials_configured=False,
    )


def classify_category(category: str) -> dict[str, Any]:
    """Classify one category. Never raises: every failure mode is folded
    into an ``unavailable``/``coverage_gap`` entry instead."""
    if category in live_test_config.LIVE_TEST_PLATFORMS:
        return _classify_platform(category)
    if category == "rag_source_freshness":
        return _classify_rag_source_freshness()
    if category == "router_automation":
        return _classify_router_automation()
    raise ValueError(f"unknown validation matrix category: {category!r}; expected {CATEGORIES}")


def build_validation_matrix_payload() -> dict[str, Any]:
    """Build the full, plain-dict ``validation_matrix_result`` payload."""
    entries = [classify_category(category) for category in CATEGORIES]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entries": entries,
    }


def write_validation_matrix(output: Path = DEFAULT_OUTPUT) -> contracts.ManifestEntry:
    payload = build_validation_matrix_payload()
    output.parent.mkdir(parents=True, exist_ok=True)
    return contracts.write_artifact(output, contracts.VALIDATION_MATRIX_RESULT, payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    entry = write_validation_matrix(args.output)
    payload = json.loads(args.output.read_text(encoding="utf-8"))
    summary = Counter(item["classification"] for item in payload["entries"])

    print(
        json.dumps(
            {
                "manifest_entry": {
                    "filename": entry.filename,
                    "kind": entry.kind,
                    "schema_version": entry.schema_version,
                    "size_bytes": entry.size_bytes,
                    "sha256": entry.sha256,
                    "redacted": entry.redacted,
                },
                "classification_summary": dict(sorted(summary.items())),
                "entries": payload["entries"],
            },
            indent=2,
        )
    )
    # An unexpected runtime failure ("unavailable") is worth a non-zero exit
    # for CI visibility; every other classification (including "blocked",
    # which is the expected default with no live-test opt-ins set) is a
    # healthy result.
    return 1 if summary.get("unavailable") else 0


if __name__ == "__main__":
    raise SystemExit(main())
