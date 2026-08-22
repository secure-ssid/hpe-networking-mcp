"""Reverse-dependency-order rollback/compensation planning and separately
gated execution for previously-*applied* AOS8 migration-run candidates.

This is the follow-on to the "0.5: there is no rollback execution path"
posture recorded throughout `src/hpe_networking_mcp/pipeline/aos8_target_adapters.py` and
`src/hpe_networking_mcp/pipeline/aos8_migration_orchestrator.py`. It never invents a new inverse
operation: every rollback step is derived from the *exact same*,
already-reviewed/tested target-adapter mapping used at apply time
(`CandidateAction.delete_operations` for New Central,
`CandidateAction.rollback_operations` for Classic Central). A candidate
whose object type has no verified inverse today (for example `vlan`, whose
`NewCentralAdapter._map_vlan` sets neither field) is always explicitly
refused -- never silently skipped, never approximated with an unrelated
operation, and never guessed at.

Design summary (see the docstrings below for the full contract):

- ``plan_rollback`` is pure, injectable planning: given a set of applied
  candidates and an ``action_for`` callable (normally
  ``adapter.candidate_action``), it orders them in *reverse* dependency
  order (a candidate is rolled back before anything it depends on) and
  classifies each as ``supported`` (a verified inverse exists) or refused
  (with an explicit, specific reason).
- ``execute_rollback_plan`` mirrors
  ``BaseCentralTargetAdapter.execute``'s safety gates: real execution
  requires ``dry_run=False`` *and* ``confirmation=True``, and additionally
  requires the dedicated :func:`rollback_writes_enabled` gate -- a
  separate opt-in from whatever gate an ordinary migration-apply
  ``write_invoker`` already enforces (e.g. ``HPE_MCP_CENTRAL_WRITES``).
  Rollback execution is never authorized by the ordinary migration write
  gate alone.
- Partial-failure handling is governed by :class:`RollbackConflictPolicy`:
  ``ABORT`` (default) stops at the first failed/refused step and marks
  every later step ``"not_attempted"`` (safe default: reverse-dependency
  order means a failed delete leaves its own dependencies, rolled back
  later in the plan, still referenced by it); ``CONTINUE`` attempts every
  remaining step regardless of an earlier failure.
- Resumability is caller-owned: ``execute_rollback_plan`` accepts an
  optional per-candidate resume map. It skips fully applied candidates and
  resumes a partially completed multi-operation candidate after its last
  confirmed operation, so a resumed rollback never re-issues a delete
  against an object it already confirmed gone. This module performs no
  persistence itself; the caller supplies and stores `resume_from`.

No network calls; no dependency on `src/hpe_networking_mcp/mcp_servers/` (same convention as
every other `src/hpe_networking_mcp/pipeline/aos8_*.py` module).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from hpe_networking_mcp.pipeline.aos8_target_adapters import (
    AdapterError,
    CandidateAction,
    Operation,
    WriteGateError,
    WriteInvoker,
)

_TRUTHY = {"1", "true", "yes", "on"}

# Deliberately distinct from every existing per-platform write gate
# (`HPE_MCP_CENTRAL_WRITES`, `HPE_MCP_AOS8_WRITES`, ...): rollback is a
# destructive, separate capability from ordinary migration-apply writes and
# must never be authorized merely because those gates are already open.
ROLLBACK_WRITE_GATE_ENV_VAR = "HPE_MCP_AOS8_ROLLBACK_WRITES"


def rollback_writes_enabled() -> bool:
    """Fail-closed gate distinct from the ordinary migration-apply write gate.

    Defaults to disabled (unset/any non-truthy value). A caller's
    ``write_invoker`` is expected to already enforce whatever gate governs
    ordinary migration-apply writes (e.g. ``HPE_MCP_CENTRAL_WRITES``) --
    this module never reads that gate itself -- but real rollback execution
    additionally always requires this one, checked directly by
    :func:`execute_rollback_plan`.
    """
    return os.environ.get(ROLLBACK_WRITE_GATE_ENV_VAR, "").strip().lower() in _TRUTHY


class RollbackConflictPolicy(str, Enum):
    """How :func:`execute_rollback_plan` reacts to a per-step failure."""

    ABORT = "abort"
    CONTINUE = "continue"


@dataclass(frozen=True)
class RollbackStep:
    """One planned rollback action for a single previously-applied candidate."""

    key: str
    object_type: str
    identifier: str
    # "delete_operations" | "rollback_operations" | None (None means refused).
    source: str | None
    operations: tuple[Operation, ...]
    supported: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.key,
            "object_type": self.object_type,
            "identifier": self.identifier,
            "source": self.source,
            "supported": self.supported,
            "reason": self.reason,
            "operations": [
                operation.with_dry_run(True).preview_dict() for operation in self.operations
            ],
        }


@dataclass(frozen=True)
class RollbackPlan:
    """A reverse-dependency-ordered rollback plan for a set of applied candidates."""

    steps: tuple[RollbackStep, ...]

    def to_dict(self) -> dict[str, Any]:
        supported = sum(1 for step in self.steps if step.supported)
        return {
            "steps": [step.to_dict() for step in self.steps],
            "summary": {
                "total": len(self.steps),
                "supported": supported,
                "refused": len(self.steps) - supported,
            },
        }


def _candidate_key(candidate: Mapping[str, Any]) -> str:
    return f"{candidate.get('object_type')}:{candidate.get('identifier')}"


def _forward_dependency_order(
    candidates: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Deterministic dependencies-before-dependents order, scoped to only the
    given candidate set. A dependency referencing a candidate outside this
    set is ignored -- rollback planning only ever concerns what was actually
    applied and handed to it, never the full original migration plan.

    Never raises on a dependency cycle: falls back to a stable, sorted
    tie-break for whatever candidates remain, so a rollback plan can always
    be produced -- including for a pathological/legacy persisted run --
    rather than blocking rollback entirely on a planning-time exception.
    """
    by_key = {_candidate_key(candidate): candidate for candidate in candidates}
    remaining = set(by_key)
    ordered: list[Mapping[str, Any]] = []
    while remaining:
        ready = sorted(
            key
            for key in remaining
            if not (
                {str(dependency) for dependency in by_key[key].get("dependencies", [])}
                & remaining
            )
        )
        if not ready:
            # Dependency cycle among the remaining candidates.
            ready = sorted(remaining)
        for key in ready:
            ordered.append(by_key[key])
            remaining.discard(key)
    return ordered


def reverse_dependency_order(
    candidates: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Return candidates ordered so each one is rolled back before anything
    it depends on -- the exact reverse of the dependencies-before-dependents
    apply order."""
    return list(reversed(_forward_dependency_order(list(candidates))))


def _verified_inverse(
    action: CandidateAction,
) -> tuple[str | None, tuple[Operation, ...]]:
    if action.delete_operations:
        return "delete_operations", tuple(action.delete_operations)
    if action.rollback_operations:
        return "rollback_operations", tuple(action.rollback_operations)
    return None, ()


def plan_rollback(
    applied_candidates: Iterable[Mapping[str, Any]],
    action_for: Callable[[Mapping[str, Any]], CandidateAction],
) -> RollbackPlan:
    """Build a reverse-dependency-order rollback plan for previously-*applied*
    migration candidates.

    Args:
        applied_candidates: candidate dicts (the same shape produced by
            `hpe_networking_mcp.pipeline.aos8_migration.build_migration_plan` and persisted by
            `hpe_networking_mcp.pipeline.aos8_migration_orchestrator.MigrationRunStore`) for
            candidates whose migration-run entry status is `"applied"`.
            Never pass a `"skipped"`/`"pending"`/`"failed"`/`"blocked"`
            candidate -- there is nothing on the target to roll back for
            those, and this function does not itself check run status.
        action_for: normally `adapter.candidate_action` -- the exact same
            per-candidate target-adapter mapping function used at apply
            time, so a rollback plan is derived from the identical,
            injectable target-adapter contract, never a separate/duplicated
            mapping.

    Every step is either backed by the adapter's own verified
    `delete_operations` (New Central) or `rollback_operations` (Classic) --
    both already-reviewed/tested `CandidateAction` metadata fields -- or is
    explicitly marked `supported=False` with a specific reason. Nothing is
    guessed: a candidate whose object type has no verified inverse today
    (e.g. `vlan`) is always refused.
    """
    ordered = reverse_dependency_order(applied_candidates)
    steps: list[RollbackStep] = []
    for candidate in ordered:
        key = _candidate_key(candidate)
        object_type = str(candidate.get("object_type"))
        identifier = str(candidate.get("identifier"))
        try:
            action = action_for(candidate)
        except AdapterError as exc:
            steps.append(
                RollbackStep(
                    key=key,
                    object_type=object_type,
                    identifier=identifier,
                    source=None,
                    operations=(),
                    supported=False,
                    reason=(
                        "target adapter could not re-derive a mapping for this "
                        f"candidate: {exc}"
                    ),
                )
            )
            continue
        source, operations = _verified_inverse(action)
        if source is None:
            steps.append(
                RollbackStep(
                    key=key,
                    object_type=object_type,
                    identifier=identifier,
                    source=None,
                    operations=(),
                    supported=False,
                    reason=(
                        "no verified inverse (delete/rollback) operation exists "
                        f"for object_type={object_type!r} in this repository; "
                        "manual cleanup is required (see "
                        "docs/aos8-migration-contract-matrix.md)"
                    ),
                )
            )
            continue
        steps.append(
            RollbackStep(
                key=key,
                object_type=object_type,
                identifier=identifier,
                source=source,
                operations=operations,
                supported=True,
            )
        )
    return RollbackPlan(steps=tuple(steps))


@dataclass
class RollbackStepResult:
    key: str
    status: str
    errors: list[str] = field(default_factory=list)
    operation_results: list[dict[str, Any]] = field(default_factory=list)
    completed_operations: int = 0
    operation_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.key,
            "status": self.status,
            "errors": list(self.errors),
            "results": list(self.operation_results),
            "completed_operations": self.completed_operations,
            "operation_count": self.operation_count,
        }


def execute_rollback_plan(
    plan: RollbackPlan,
    *,
    dry_run: bool,
    confirmation: bool,
    write_invoker: WriteInvoker,
    conflict_policy: RollbackConflictPolicy = RollbackConflictPolicy.ABORT,
    resume_from: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute (or dry-run) a rollback plan's supported steps, in plan order
    (already reverse-dependency-ordered by :func:`plan_rollback`).

    Safety gates mirror `BaseCentralTargetAdapter.execute`:
      - `dry_run=False` always requires `confirmation=True`
        (`WriteGateError` otherwise).
      - `dry_run=False` additionally requires :func:`rollback_writes_enabled`
        -- checked here directly, never left only to the caller's
        `write_invoker`.
    Every refused step (no verified inverse -- `RollbackStep.supported is
    False`) is reported `status="refused"` and its operations (there are
    none) are never attempted, regardless of `dry_run`/`confirmation`/gate
    state.

    Args:
        resume_from: optional per-candidate state from a prior partial run.
            Legacy `"applied"` string values remain accepted. Structured
            values use `{"status": ..., "completed_operations": N}` so a
            multi-operation candidate resumes at its first unfinished
            operation instead of repeating already-confirmed deletes.
        conflict_policy: `ABORT` (default) stops at the first step that
            fails or is refused and marks every later step
            `"not_attempted"` -- reverse-dependency order means a failed
            delete leaves its own dependencies (rolled back later) still
            referenced, so continuing past a failure risks an inconsistent
            partial state unless the caller explicitly opts into
            `CONTINUE`, which attempts every remaining step regardless.

    Returns a bounded dict: `{"dry_run", "conflict_policy", "results": [...],
    "summary": {...}}`. Never raises for a per-step failure -- only for the
    upfront gate violations above.
    """
    if not dry_run:
        if not confirmation:
            raise WriteGateError("Real rollback execution requires confirmation=True.")
        if not rollback_writes_enabled():
            raise WriteGateError(
                "Rollback writes are disabled; set "
                f"{ROLLBACK_WRITE_GATE_ENV_VAR}=1."
            )

    resume = dict(resume_from or {})
    results: list[RollbackStepResult] = []
    aborted = False
    for step in plan.steps:
        if aborted and conflict_policy is RollbackConflictPolicy.ABORT:
            results.append(RollbackStepResult(key=step.key, status="not_attempted"))
            continue
        if not step.supported:
            results.append(
                RollbackStepResult(
                    key=step.key, status="refused", errors=[step.reason or ""]
                )
            )
            if conflict_policy is RollbackConflictPolicy.ABORT:
                aborted = True
            continue
        prior_state = resume.get(step.key)
        prior_status = (
            str(prior_state.get("status") or "")
            if isinstance(prior_state, Mapping)
            else str(prior_state or "")
        )
        completed_operations = (
            int(prior_state.get("completed_operations") or 0)
            if isinstance(prior_state, Mapping)
            else 0
        )
        completed_operations = max(
            0,
            min(completed_operations, len(step.operations)),
        )
        if prior_status == "applied":
            results.append(
                RollbackStepResult(
                    key=step.key,
                    status="already_applied",
                    completed_operations=len(step.operations),
                    operation_count=len(step.operations),
                )
            )
            continue

        operation_results: list[dict[str, Any]] = []
        errors: list[str] = []
        for index, operation in enumerate(step.operations):
            invoked = operation.with_dry_run(dry_run)
            if index < completed_operations:
                operation_results.append(
                    {
                        "operation": invoked.preview_dict(),
                        "status": "already_applied",
                    }
                )
                continue
            try:
                value = write_invoker(invoked, confirmation=confirmation)
            except Exception as exc:  # noqa: BLE001 - surfaced as a bounded error string
                errors.append(f"{operation.name}: {exc}")
                break
            operation_results.append(
                {
                    "operation": invoked.preview_dict(),
                    "status": "dry-run" if dry_run else "applied",
                    "result": value,
                }
            )
            if not dry_run:
                completed_operations = index + 1
        status = "failed" if errors else ("dry-run" if dry_run else "applied")
        results.append(
            RollbackStepResult(
                key=step.key,
                status=status,
                errors=errors,
                operation_results=operation_results,
                completed_operations=completed_operations,
                operation_count=len(step.operations),
            )
        )
        if errors and conflict_policy is RollbackConflictPolicy.ABORT:
            aborted = True

    return {
        "dry_run": dry_run,
        "conflict_policy": conflict_policy.value,
        "results": [result.to_dict() for result in results],
        "summary": {
            "total": len(results),
            "applied": sum(1 for result in results if result.status == "applied"),
            "dry_run_ok": sum(1 for result in results if result.status == "dry-run"),
            "failed": sum(1 for result in results if result.status == "failed"),
            "refused": sum(1 for result in results if result.status == "refused"),
            "already_applied": sum(
                1 for result in results if result.status == "already_applied"
            ),
            "not_attempted": sum(
                1 for result in results if result.status == "not_attempted"
            ),
        },
    }
