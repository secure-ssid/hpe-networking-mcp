"""Generate versioned router-automation report artifacts (v0.7).

Fully offline and read-only: this script never calls a live Central/GLP/RAG
API and never executes/dispatches any tool. It exercises the router's two
plan-only automation tools --
`hpe_networking_mcp.mcp_servers.tool_router.plan_tool_workflow` and
`hpe_networking_mcp.mcp_servers.tool_router.plan_reconciliation_schedule` -- against the
currently enabled backend catalog (whatever `HPE_MCP_TOOLSETS` /
`HPE_MCP_PRODUCTS` resolve to in the running environment; the base
Central+GLP+RAG set when unset), and writes their resulting plan payloads
as two versioned, redacted, atomically-written artifacts via
`hpe_networking_mcp.pipeline.artifact_contracts.write_artifact`:

  - `router-automation-dependency-plan.json`   (kind: router_dependency_plan)
  - `router-automation-reconciliation-plan.json` (kind: router_reconciliation_plan)

The dependency-plan example steps are a small, fixed, deterministic example
(list-then-inspect a device) chosen to resolve against tools that exist in
every base toolset; it degrades gracefully (still writes a valid, ok=False
artifact reporting the unresolved step) if a given environment's enabled
catalog doesn't include one of the example tool names.

Both plans are already produced read-only/dry-run by the router tools
themselves (`dry_run: True`, never scheduling or executing anything) --
this script only persists them to disk with a manifest-ready summary.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import hpe_networking_mcp.mcp_servers.tool_router as router
from hpe_networking_mcp.pipeline import artifact_contracts as contracts

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEPENDENCY_OUTPUT = REPO_ROOT / "outputs" / "router-automation-dependency-plan.json"
DEFAULT_RECONCILIATION_OUTPUT = REPO_ROOT / "outputs" / "router-automation-reconciliation-plan.json"

# A small, fixed example workflow: discover devices, then look up one by
# name. Chosen because `find_tool`/`list_devices`-shaped tools exist in the
# base Central toolset; unresolved steps are still reported explicitly
# (never silently dropped) if the running environment's enabled catalog
# differs.
_EXAMPLE_DEPENDENCY_STEPS: list[dict[str, Any]] = [
    {"id": "discover", "hint": "list devices"},
    {"id": "inspect", "hint": "find a specific device", "depends_on": ["discover"]},
]

_DEFAULT_CADENCE = "daily"
_DEFAULT_RECONCILIATION_MAX_ENTRIES = 25


def _call(tool_fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call a @mcp.tool()-wrapped function by its underlying callable."""
    target = getattr(tool_fn, "fn", tool_fn)
    return target(*args, **kwargs)


def generate_dependency_plan_artifact(output: Path) -> contracts.ManifestEntry | None:
    """Run the fixed example workflow through `plan_tool_workflow` and
    persist its artifact payload. Returns None (and prints why) if the
    planner itself is unavailable (router in `minimal` mode) or the
    resulting plan failed to validate."""
    plan_fn = getattr(router, "plan_tool_workflow", None)
    if plan_fn is None:
        print(
            json.dumps(
                {
                    "status": "skipped",
                    "reason": "plan_tool_workflow not registered (HPE_MCP_ROUTER_MODE=minimal)",
                },
                indent=2,
            )
        )
        return None
    result = _call(plan_fn, list(_EXAMPLE_DEPENDENCY_STEPS), include_candidates=False)
    artifact_payload = result.get("artifact")
    if artifact_payload is None:
        print(
            json.dumps(
                {"status": "skipped", "reason": result.get("artifact_error") or "no artifact"},
                indent=2,
            )
        )
        return None
    output.parent.mkdir(parents=True, exist_ok=True)
    return contracts.write_artifact(output, contracts.ROUTER_DEPENDENCY_PLAN, artifact_payload)


def generate_reconciliation_plan_artifact(
    output: Path,
    *,
    cadence: str = _DEFAULT_CADENCE,
    max_entries: int = _DEFAULT_RECONCILIATION_MAX_ENTRIES,
) -> contracts.ManifestEntry | None:
    """Build a reconciliation schedule across every currently enabled
    backend tool via `plan_reconciliation_schedule` and persist its
    artifact payload."""
    plan_fn = getattr(router, "plan_reconciliation_schedule", None)
    if plan_fn is None:
        print(
            json.dumps(
                {
                    "status": "skipped",
                    "reason": (
                        "plan_reconciliation_schedule not registered "
                        "(HPE_MCP_ROUTER_MODE=minimal)"
                    ),
                },
                indent=2,
            )
        )
        return None
    result = _call(plan_fn, cadence, max_entries=max_entries)
    artifact_payload = result.get("artifact")
    if artifact_payload is None:
        print(
            json.dumps(
                {"status": "skipped", "reason": result.get("artifact_error") or "no artifact"},
                indent=2,
            )
        )
        return None
    output.parent.mkdir(parents=True, exist_ok=True)
    return contracts.write_artifact(output, contracts.ROUTER_RECONCILIATION_PLAN, artifact_payload)


def _entry_summary(entry: contracts.ManifestEntry | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    return {
        "filename": entry.filename,
        "kind": entry.kind,
        "schema_version": entry.schema_version,
        "size_bytes": entry.size_bytes,
        "sha256": entry.sha256,
        "generated_at": entry.generated_at,
        "redacted": entry.redacted,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dependency-output", type=Path, default=DEFAULT_DEPENDENCY_OUTPUT)
    parser.add_argument("--reconciliation-output", type=Path, default=DEFAULT_RECONCILIATION_OUTPUT)
    parser.add_argument("--cadence", default=_DEFAULT_CADENCE)
    parser.add_argument("--max-entries", type=int, default=_DEFAULT_RECONCILIATION_MAX_ENTRIES)
    args = parser.parse_args(argv)

    router._load_all_backends()

    dependency_entry = generate_dependency_plan_artifact(args.dependency_output)
    reconciliation_entry = generate_reconciliation_plan_artifact(
        args.reconciliation_output, cadence=args.cadence, max_entries=args.max_entries
    )

    result = {
        "dependency_plan": _entry_summary(dependency_entry),
        "reconciliation_plan": _entry_summary(reconciliation_entry),
        "enabled_backends": sorted(router._BACKENDS.keys()),
    }
    print(json.dumps(result, indent=2))
    return 0 if dependency_entry is not None and reconciliation_entry is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
