"""Pure, network-free planning/scheduling logic for the router automation workstream.

This module backs two `hpe_networking_mcp.mcp_servers.tool_router` read-only tools --
`plan_tool_workflow` (dependency ordering) and `plan_reconciliation_schedule`
(recurring reconciliation planning) -- but never imports the MCP server SDK or any
backend server module itself. Callers hand in an already-resolved catalog
(name -> bounded metadata dict, sourced from the router's own loaded tool
index) so this module only ever does:

- deterministic dependency-graph ordering + cycle detection (Kahn's
  algorithm, ties always broken by caller-supplied order -- never a
  hash/set iteration order),
- structural cadence validation (named cadences, a bounded interval-minutes
  window, or a 5-field cron expression validated syntactically only --
  this never registers an OS/GitHub schedule; it is validation, not
  scheduling),
- shaping a bounded payload suitable for
  `hpe_networking_mcp.pipeline.artifact_contracts.build_artifact`/`write_artifact` under the
  `router_dependency_plan` / `router_reconciliation_plan` kinds.

Nothing here executes a tool, infers a tool name that the caller didn't
supply, or performs any file/network I/O -- writing an artifact to disk is
always the caller's explicit choice via
`hpe_networking_mcp.pipeline.artifact_contracts.write_artifact`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

MAX_PLAN_STEPS = 25
MAX_PLAN_CANDIDATES_PER_STEP = 5
MAX_CYCLES_REPORTED = 10
MAX_RECONCILIATION_ENTRIES = 100
# Detail-list cap for excluded candidates -- independent of, and paired
# with, hpe_networking_mcp.pipeline.artifact_contracts.MAX_ROUTER_RECONCILIATION_EXCLUDED so a
# reconciliation plan can never grow the "excluded" detail list past what
# the artifact contract will accept. The *true* excluded count is always
# reported separately (see partition_reconciliation_candidates) even when
# the detail list itself is capped, so the plan never silently pretends
# fewer tools were excluded than actually were.
MAX_RECONCILIATION_EXCLUDED_DETAIL = 200

CADENCE_KINDS: tuple[str, ...] = ("interval_minutes", "hourly", "daily", "weekly", "cron")
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 10_080  # one week
MAX_CADENCE_EXPRESSION_CHARS = 200
# One 5-field cron field: "*", "N", "N-M", any of those with a "/step", or a
# comma-separated list of the above. Structural validation only -- this
# module never parses a field into an actual next-run time or registers
# any schedule.
_CRON_SUBFIELD = r"(?:\*|[0-9]+(?:-[0-9]+)?)(?:/[0-9]+)?"
_CRON_FIELD_RE = re.compile(rf"^{_CRON_SUBFIELD}(?:,{_CRON_SUBFIELD})*$")


class RouterAutomationError(ValueError):
    """Raised for a structurally invalid planner/scheduler request."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Dependency graph ordering
# ---------------------------------------------------------------------------


def resolve_dependency_order(
    step_ids: Sequence[str],
    edges: Mapping[str, Sequence[str]],
) -> tuple[list[str] | None, list[list[str]]]:
    """Deterministic topological order across ``step_ids`` via Kahn's algorithm.

    ``edges[step_id]`` lists the step ids that must run *before* ``step_id``
    (its declared dependencies). Any dependency id not present in
    ``step_ids`` is silently ignored here -- the caller is responsible for
    reporting unresolved dependencies explicitly (this function only ever
    orders the nodes it was given).

    Returns ``(order, cycles)``:
      - ``order`` is the full stable list of ``step_ids`` if the graph is
        acyclic, else ``None``.
      - ``cycles`` is a bounded (``MAX_CYCLES_REPORTED``) list of one or
        more cycles detected (each a list of step ids forming the cycle),
        empty when acyclic.

    Ties are always broken by the caller-supplied ``step_ids`` order, never
    by set/dict iteration order, so the same input always produces the same
    output.
    """
    ids = list(step_ids)
    if len(ids) != len(set(ids)):
        return None, []
    id_set = set(ids)
    indegree = {i: 0 for i in ids}
    forward: dict[str, list[str]] = {i: [] for i in ids}
    for node, deps in edges.items():
        if node not in id_set:
            continue
        for dep in deps:
            if dep not in id_set:
                continue
            forward[dep].append(node)
            indegree[node] += 1

    order: list[str] = []
    indegree_work = dict(indegree)
    ready = [i for i in ids if indegree_work[i] == 0]
    while ready:
        ready.sort(key=ids.index)
        current = ready.pop(0)
        order.append(current)
        for nxt in forward[current]:
            indegree_work[nxt] -= 1
            if indegree_work[nxt] == 0:
                ready.append(nxt)

    if len(order) == len(ids):
        return order, []

    remaining = [i for i in ids if i not in order]
    cycles = _find_cycles(remaining, edges)
    return None, cycles[:MAX_CYCLES_REPORTED]


def _find_cycles(
    remaining: Sequence[str],
    edges: Mapping[str, Sequence[str]],
) -> list[list[str]]:
    """Bounded DFS cycle enumeration restricted to nodes that never reached
    a topological position (i.e. nodes actually involved in a cycle)."""
    remaining_set = set(remaining)
    cycles: list[list[str]] = []
    visited: set[str] = set()

    def dfs(node: str, path: list[str], on_path: set[str]) -> None:
        if len(cycles) >= MAX_CYCLES_REPORTED:
            return
        for dep in edges.get(node, []):
            if dep not in remaining_set:
                continue
            if len(cycles) >= MAX_CYCLES_REPORTED:
                return
            if dep in on_path:
                start = path.index(dep)
                cycles.append(path[start:] + [dep])
                continue
            if dep in visited:
                continue
            dfs(dep, path + [dep], on_path | {dep})
        visited.add(node)

    for node in remaining:
        if node not in visited:
            dfs(node, [node], {node})
    return cycles[:MAX_CYCLES_REPORTED]


# ---------------------------------------------------------------------------
# Cadence validation -- structural only, never scheduled/executed here.
# ---------------------------------------------------------------------------


def validate_cadence(cadence: Mapping[str, Any] | str) -> dict[str, Any]:
    """Validate a bounded cadence spec into a normalized descriptor.

    Accepts either a bare string (one of the named cadences: ``hourly``,
    ``daily``, ``weekly``) or a mapping with a ``kind`` plus, for
    ``interval_minutes``, an integer ``interval_minutes`` within
    ``[MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES]``, or for ``cron``, a
    5-field ``expression`` validated structurally (syntax only -- this
    never computes an actual next-run time or registers any OS/GitHub
    schedule).

    Always returns a dict with a ``valid`` bool; never raises, so a caller
    can safely fold this into a bounded plan response instead of crashing
    on a malformed cadence.
    """
    if isinstance(cadence, str):
        spec: Mapping[str, Any] = {"kind": cadence}
    elif isinstance(cadence, Mapping):
        spec = cadence
    else:
        return {"valid": False, "reason": "cadence must be a string or object"}

    kind = str(spec.get("kind", "")).strip().lower()
    if kind not in CADENCE_KINDS:
        return {
            "valid": False,
            "reason": f"unknown cadence kind {kind!r}; expected one of {CADENCE_KINDS}",
        }

    if kind == "interval_minutes":
        interval = spec.get("interval_minutes")
        if not isinstance(interval, int) or isinstance(interval, bool):
            return {"valid": False, "reason": "interval_minutes must be an int"}
        if not (MIN_INTERVAL_MINUTES <= interval <= MAX_INTERVAL_MINUTES):
            return {
                "valid": False,
                "reason": (
                    f"interval_minutes must be between {MIN_INTERVAL_MINUTES} and "
                    f"{MAX_INTERVAL_MINUTES}"
                ),
            }
        return {"valid": True, "kind": kind, "interval_minutes": interval}

    if kind == "cron":
        expression = spec.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            return {"valid": False, "reason": "cron expression must be a non-empty string"}
        normalized_expression = expression.strip()
        if len(normalized_expression) > MAX_CADENCE_EXPRESSION_CHARS:
            return {
                "valid": False,
                "reason": (
                    "cron expression cannot exceed "
                    f"{MAX_CADENCE_EXPRESSION_CHARS} characters"
                ),
            }
        fields = normalized_expression.split()
        if len(fields) != 5:
            return {
                "valid": False,
                "reason": (
                    "cron expression must have exactly 5 fields "
                    "(minute hour day-of-month month day-of-week)"
                ),
            }
        if not all(_CRON_FIELD_RE.match(field) for field in fields):
            return {"valid": False, "reason": "cron expression contains an invalid field"}
        return {"valid": True, "kind": kind, "expression": normalized_expression}

    # Named cadences (hourly / daily / weekly) need no further parameters.
    return {"valid": True, "kind": kind}


# ---------------------------------------------------------------------------
# Reconciliation entry filtering -- read-only guarantee lives here so both
# the router tool and any offline report-generation script apply the exact
# same rule: only "read"/"diagnostic" capability tools are ever scheduled.
# ---------------------------------------------------------------------------

RECONCILIATION_ELIGIBLE_CAPABILITIES: tuple[str, ...] = ("read", "diagnostic")


def partition_reconciliation_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    max_entries: int = MAX_RECONCILIATION_ENTRIES,
    max_excluded_detail: int = MAX_RECONCILIATION_EXCLUDED_DETAIL,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Split resolved tool candidates into (entries, excluded_detail, excluded_total).

    ``entries`` contains only ``read``/``diagnostic`` capability tools, up
    to ``max_entries`` (stable input order, first-come). Every other
    candidate (``write``/``destructive``/``unknown`` capability, or
    anything beyond the ``max_entries`` bound) counts toward
    ``excluded_total`` and is described in ``excluded_detail`` -- up to
    ``max_excluded_detail`` -- so the reported total is always accurate even
    when the caller-visible detail list itself must stay bounded (matching
    ``hpe_networking_mcp.pipeline.artifact_contracts.MAX_ROUTER_RECONCILIATION_EXCLUDED``).
    Nothing is ever silently dropped from the *count*, only from the
    verbose per-item detail list.
    """
    entries: list[dict[str, Any]] = []
    excluded_detail: list[dict[str, Any]] = []
    excluded_total = 0

    def _exclude(name: Any, capability: Any, reason: str) -> None:
        nonlocal excluded_total
        excluded_total += 1
        if len(excluded_detail) < max_excluded_detail:
            excluded_detail.append({"tool": name, "capability": capability, "reason": reason})

    for candidate in candidates:
        capability = candidate.get("capability")
        name = candidate.get("tool")
        if capability not in RECONCILIATION_ELIGIBLE_CAPABILITIES:
            _exclude(name, capability, "capability_not_eligible_for_reconciliation")
            continue
        if len(entries) >= max_entries:
            _exclude(name, capability, "reconciliation_entry_bound_exceeded")
            continue
        entries.append(dict(candidate))
    return entries, excluded_detail, excluded_total


# ---------------------------------------------------------------------------
# Artifact payload shaping -- plain dicts ready for
# hpe_networking_mcp.pipeline.artifact_contracts.build_artifact/write_artifact. Kept here
# (rather than duplicated at each call site) so the router tool and any
# offline report-generation script always build the identical shape.
# ---------------------------------------------------------------------------


def build_dependency_plan_payload(
    *,
    steps: Sequence[Mapping[str, Any]],
    order: Sequence[str] | None,
    acyclic: bool,
    cycles: Sequence[Sequence[str]],
    unresolved_step_ids: Sequence[str],
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Shape a bounded ``router_dependency_plan`` artifact payload.

    ``acyclic`` is a graph property (``not cycles``) and is independent of
    whether ``order`` itself was withheld for another reason (an unresolved
    step/dependency) -- callers should pass ``order=None`` whenever ordering
    should not be considered authoritative, even on an otherwise-acyclic
    graph, and this still reports the true ``acyclic``/``cycles`` state.
    """
    return {
        "generated_at": generated_at or now_iso(),
        "steps": [dict(step) for step in steps],
        "order": list(order) if order is not None else [],
        "acyclic": acyclic,
        "cycles": [list(cycle) for cycle in cycles],
        "unresolved_step_ids": list(unresolved_step_ids),
    }


def build_reconciliation_plan_payload(
    *,
    cadence: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
    excluded: Sequence[Mapping[str, Any]],
    excluded_count: int | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Shape a bounded ``router_reconciliation_plan`` artifact payload.

    ``excluded_count`` is the *true* total excluded count and may exceed
    ``len(excluded)`` when the detail list itself had to stay within
    ``hpe_networking_mcp.pipeline.artifact_contracts.MAX_ROUTER_RECONCILIATION_EXCLUDED`` --
    pass the ``excluded_total`` returned by
    :func:`partition_reconciliation_candidates` here rather than
    recomputing ``len(excluded)``. Defaults to ``len(excluded)`` for
    callers that already know their ``excluded`` list is complete.

    ``dry_run`` is always ``True`` -- this plan is never executed by this
    module or by the router tool that calls it; it is a read-only schedule
    *specification* only.
    """
    return {
        "generated_at": generated_at or now_iso(),
        "cadence": dict(cadence),
        "entries": [dict(entry) for entry in entries],
        "excluded_count": excluded_count if excluded_count is not None else len(excluded),
        "excluded": [dict(item) for item in excluded],
        "dry_run": True,
    }
