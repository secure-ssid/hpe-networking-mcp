#!/usr/bin/env python3
"""Axis split-CRUD contract verification + disposable-write planning harness.

Three independent, additive layers, from safest to most gated:

1. **Static contract check** (always runs, offline, no network, no
   credentials required): verifies the committed 47-operation Axis manifest
   really does split every upstream fused ``manage_entity`` action into
   distinct ``axis_create_*``/``axis_update_*``/``axis_delete_*`` operations
   (plus a matching ``axis_get_*``/list query) per entity, as documented in
   ``src/hpe_networking_mcp/mcp_servers/axis.py``'s module docstring. This never touches the
   network and always runs, including in CI.
2. **Bounded read-only live check** (opt-in only): when
   ``HPE_MCP_LIVE_TEST_AXIS_READ=1`` is set *and* ``AXIS_BASE_URL``/
   ``AXIS_API_TOKEN`` are configured, calls each split-CRUD entity's own
   list-style query tool once with a small page size and records only
   per-step ok/error status -- never the raw response body.
3. **Disposable-write plan** (opt-in only, PLAN ONLY -- never executed):
   when ``HPE_MCP_LIVE_TEST_AXIS_WRITE=1`` is also set, builds a
   reviewable create/read-back/delete plan for one low-risk entity (a
   Location sub-location) with a placeholder payload and a SHA-256 digest
   of the plan. This harness NEVER calls the corresponding
   ``axis_create_*``/``axis_delete_*`` tools with ``dry_run=False``; it
   only ever constructs and records the plan that a human operator would
   review and execute manually, outside of this harness, in their own lab.
   This intentionally matches the v0.7 instruction that no write may be
   executed autonomously by this repository's automation.

Usage::

    uv run python scripts/evaluate_axis_lab.py
    uv run python scripts/evaluate_axis_lab.py --output-dir outputs/optional-product-evidence
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from hpe_networking_mcp.mcp_servers import axis as axis_tools  # noqa: E402
from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import load_manifest  # noqa: E402
from hpe_networking_mcp.pipeline import artifact_contracts as contracts  # noqa: E402
from hpe_networking_mcp.pipeline import live_test_config  # noqa: E402

PLATFORM = "axis"
_SPLIT_VERBS = ("query", "create", "update", "delete")
_KIND_TO_SPLIT_VERB = {
    **{verb: verb for verb in _SPLIT_VERBS},
    "subquery": "query",
    "subcreate": "create",
    "subupdate": "update",
    "subdelete": "delete",
}
_MAX_LIVE_READ_ENTITIES = 5
_DISPOSABLE_WRITE_ENTITY_PATH = "/Locations/{location_id}/SubLocations"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _entity_families(manifest: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """Group manifest operations by shared path into one family per entity."""
    families: dict[str, dict[str, dict[str, Any]]] = {}
    for op in manifest.get("operations", []):
        kind = _KIND_TO_SPLIT_VERB.get(op.get("kind"))
        if kind is None:
            continue
        families.setdefault(op["path"], {})[kind] = op
    return families


def verify_split_crud_contract(manifest: dict[str, Any]) -> dict[str, Any]:
    """Static, offline verification that every entity has all four split verbs.

    Returns a bounded, JSON-shaped report -- never raises for a coverage gap
    (a family missing a verb is recorded as an anomaly, not an exception),
    so this always produces evidence even when the manifest is incomplete.
    """
    families = _entity_families(manifest)
    complete: list[str] = []
    anomalies: list[dict[str, Any]] = []
    for path, verbs in sorted(families.items()):
        missing = [verb for verb in _SPLIT_VERBS if verb not in verbs]
        if missing:
            anomalies.append({"path": path, "missing_verbs": missing})
        else:
            complete.append(path)
    return {
        "family_count": len(families),
        "complete_split_crud_families": len(complete),
        "anomalies": anomalies,
        "compatible": not anomalies,
    }


async def _bounded_live_reads(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Call each split-CRUD entity's list query once, bounded, read-only."""
    families = _entity_families(manifest)
    steps: list[dict[str, Any]] = []
    for _path, verbs in sorted(families.items())[:_MAX_LIVE_READ_ENTITIES]:
        query_op = verbs.get("query")
        if query_op is None:
            continue
        tool_name = query_op["name"]
        tool_fn = getattr(axis_tools, tool_name, None)
        if tool_fn is None:
            steps.append({"name": tool_name, "status": "error"})
            continue
        try:
            result = await tool_fn(page_number=1, page_size=1)
            status = "ok" if isinstance(result, dict) and "error" not in result else "error"
        except Exception:  # noqa: BLE001 - a live probe failure is evidence, not a crash
            status = "error"
        steps.append({"name": tool_name, "status": status})
    return steps


def build_disposable_write_plan() -> dict[str, Any]:
    """Build (never execute) a reviewable create/read-back/delete plan.

    Uses the confirmed sub-location split-CRUD family (the lowest-risk Axis
    entity: a child grouping node under a Location) with an explicit
    placeholder payload -- never a guessed real-world value.
    """
    plan = {
        "entity": "sub_location",
        "path": _DISPOSABLE_WRITE_ENTITY_PATH,
        "steps": [
            {
                "action": "create",
                "tool": "axis_create_sub_location",
                "payload": {"name": "hpe-mcp-lab-placeholder"},
            },
            {"action": "read_back", "tool": "axis_get_sub_locations"},
            {"action": "delete", "tool": "axis_delete_sub_location"},
        ],
        "execution_status": "planned_not_executed",
        "note": (
            "This plan is generated only; evaluate_axis_lab.py never calls the "
            "create/delete tools with dry_run=False. A human operator must "
            "review and execute each step manually against their own lab tenant."
        ),
    }
    return {**plan, "plan_sha256": _digest(plan)}


def build_evidence_artifact(*, output_dir: Path) -> list[contracts.ManifestEntry]:
    manifest = load_manifest(PLATFORM)
    contract_report = verify_split_crud_contract(manifest)

    read_enabled = live_test_config.live_test_read_enabled(PLATFORM)
    credentials_ok = live_test_config.credentials_configured(PLATFORM)
    write_enabled = live_test_config.live_test_write_enabled(PLATFORM)

    steps: list[dict[str, Any]] = [
        {
            "name": "verify_split_crud_contract",
            "status": "ok" if contract_report["compatible"] else "error",
        }
    ]
    summary: dict[str, Any] = {"split_crud_contract": contract_report}

    mode = "read_only"
    if read_enabled and credentials_ok:
        live_steps = asyncio.run(_bounded_live_reads(manifest))
        steps.extend(live_steps)
        summary["live_reads_attempted"] = len(live_steps)
    else:
        summary["live_reads_attempted"] = 0
        summary["live_reads_skip_reason"] = (
            "read gate disabled" if not read_enabled else "credentials not configured"
        )

    if write_enabled:
        plan = build_disposable_write_plan()
        steps.append({"name": "build_disposable_write_plan", "status": "ok"})
        summary["disposable_write_plan"] = plan
        mode = "disposable_write"
    else:
        summary["disposable_write_plan_built"] = False

    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "platform": PLATFORM,
        "mode": mode,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "steps": steps,
        "summary": summary,
    }
    entry = contracts.write_artifact(
        output_dir / "axis-lab-evidence.json", contracts.LIVE_LIFECYCLE_EVIDENCE, payload
    )
    return [entry]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_REPO_ROOT / "outputs" / "optional-product-evidence",
    )
    args = parser.parse_args()
    entries = build_evidence_artifact(output_dir=args.output_dir)
    for entry in entries:
        print(f"Wrote {args.output_dir / entry.filename} ({entry.kind}, {entry.size_bytes}B).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
