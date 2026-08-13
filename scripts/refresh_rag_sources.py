#!/usr/bin/env python3
"""Declarative, transactional orchestrator for a RAG/source refresh.

The previous version hard-coded one path -- check, re-run each changed
source's ``scraper``, rebuild ``docs.lance``/``specs.sqlite``, run the eval
gate -- and silently ignored everything else the manifest declares
(``extra_scripts``), everything the structured sources need
(security/lifecycle, api-next product specs), the generated-tool manifests,
``tools.lance`` and the local ``data/*-MANIFEST.json`` pair. A failure part
way through could therefore leave the repo with a rebuilt docs index, a
stale tool index, and manifests describing neither.

This version is declarative and transactional:

* **Declarative plan.** Every step is derived from committed declarations --
  ``ingestion/source_manifest.json``'s ``scraper`` and ``extra_scripts``
  fields plus the structured step table below -- and materialized as a plan
  *before* anything runs. ``--plan``/``--dry-run`` prints that plan as JSON
  and exits without executing a single command or opening a socket, so the
  orchestration is testable with no network at all.
* **Explicit refresh consent.** Nothing is fetched unless
  ``--refresh-sources`` is passed. Without it the freshness check runs in
  ``--offline`` mode: sources are reported ``not_checked``, never ``fresh``.
* **Transactional snapshot.** Every mutable artifact a refresh can touch --
  ``data/docs.lance``, ``data/specs.sqlite``, ``data/tools.lance``, the
  generated operation manifests under
  ``src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests``, and the
  local ``data/SOURCE-MANIFEST.json`` / ``data/INDEX-MANIFEST.json`` pair --
  is snapshotted before execution and restored as a unit if any step or the
  eval gate fails.

Usage::

    python scripts/refresh_rag_sources.py --plan                # JSON plan, no network
    python scripts/refresh_rag_sources.py --check-only          # offline drift report
    python scripts/refresh_rag_sources.py --refresh-sources     # real refresh
    python scripts/refresh_rag_sources.py --refresh-sources --source vsg_docs
    python scripts/refresh_rag_sources.py --refresh-sources --skip-eval-gate
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from hpe_networking_mcp.pipeline import drift_taxonomy as taxonomy  # noqa: E402

DATA_DIR = ROOT / "data"
DOCS_LANCE = DATA_DIR / "docs.lance"
TOOLS_LANCE = DATA_DIR / "tools.lance"
SPECS_SQLITE = DATA_DIR / "specs.sqlite"
LOCAL_SOURCE_MANIFEST = DATA_DIR / "SOURCE-MANIFEST.json"
LOCAL_INDEX_MANIFEST = DATA_DIR / "INDEX-MANIFEST.json"
GENERATED_MANIFEST_DIR = (
    ROOT / "src" / "hpe_networking_mcp" / "mcp_servers" / "openapi_gen" / "manifests"
)
REFRESH_LOG = DATA_DIR / "refresh_log.jsonl"
SOURCE_MANIFEST_PATH = ROOT / "ingestion" / "source_manifest.json"

#: Everything a refresh can mutate, snapshotted and restored as one unit.
SNAPSHOT_TARGETS: tuple[tuple[str, Path], ...] = (
    ("docs.lance", DOCS_LANCE),
    ("tools.lance", TOOLS_LANCE),
    ("specs.sqlite", SPECS_SQLITE),
    ("generated_manifests", GENERATED_MANIFEST_DIR),
    ("SOURCE-MANIFEST.json", LOCAL_SOURCE_MANIFEST),
    ("INDEX-MANIFEST.json", LOCAL_INDEX_MANIFEST),
)

STEP_CHECK = "check"
STEP_SCRAPER = "scraper"
STEP_EXTRA_SCRIPT = "extra_script"
STEP_SECURITY_LIFECYCLE = "security_lifecycle"
STEP_PRODUCT_SPEC = "product_spec"
STEP_GENERATED_TOOL = "generated_tool"
STEP_INDEX_REBUILD = "index_rebuild"
STEP_TOOL_INDEX_REBUILD = "tool_index_rebuild"
STEP_LOCAL_MANIFESTS = "local_manifests"
STEP_EVAL_GATE = "eval_gate"

PHASE_PRE = "pre"
PHASE_POST = "post"
EXTRA_SCRIPT_PHASES = (PHASE_PRE, PHASE_POST)

#: The only environment a planned step may require, declared explicitly so a
#: plan never depends on whatever happened to be exported in the shell.
STRICT_CATALOG_ENV: dict[str, str] = {
    "HPE_MCP_PRODUCT_ACCESS": "read-write",
    "HPE_MCP_GLP_GENERATED_TOOLS": "1",
}

#: Structured (non-``scraper``) steps, declared once instead of hard-coded
#: mid-flow. ``sources`` is the set of manifest sources whose drift triggers
#: the step; an empty set means "always, once anything is refreshed".
STRUCTURED_STEPS: tuple[dict[str, Any], ...] = (
    {
        "kind": STEP_SECURITY_LIFECYCLE,
        "name": "scrape_security_lifecycle",
        "command": ["ingestion/scrape_security_lifecycle.py"],
        "sources": (
            "security_advisories",
            "lifecycle_notices",
            "juniper_lifecycle",
            "juniper_security_advisories",
        ),
        "requires_refresh": True,
    },
    {
        "kind": STEP_PRODUCT_SPEC,
        "name": "scrape_apinext_product_specs",
        "command": ["ingestion/scrape_apinext_specs.py"],
        "sources": ("product_specs",),
        "requires_refresh": True,
    },
    {
        "kind": STEP_GENERATED_TOOL,
        "name": "validate_generated_tool_manifests",
        "command": ["scripts/check_generated_tool_manifests.py"],
        "sources": (),
        "requires_refresh": False,
    },
    {
        "kind": STEP_INDEX_REBUILD,
        "name": "rebuild_docs_and_specs_index",
        "command": ["ingestion/ingest_docs.py"],
        "sources": (),
        "requires_refresh": False,
    },
    {
        "kind": STEP_TOOL_INDEX_REBUILD,
        "name": "rebuild_tool_catalog_index",
        "command": ["scripts/ingest_tools.py", "--products", "all"],
        "sources": (),
        "requires_refresh": False,
        # Without both of these the catalog is built from a strictly smaller
        # selection (read-only optional tools, curated-only GLP) and
        # validate_release.py --strict-tool-index correctly reports the
        # rebuilt index as stale against the 6,715-tool registered catalog.
        # Declared here rather than assumed from the ambient shell so a plan
        # is reproducible; see scripts/ingest_tools.py's own usage note and
        # scripts/validate_release.py::_strict_env.
        "env": STRICT_CATALOG_ENV,
    },
    {
        "kind": STEP_LOCAL_MANIFESTS,
        "name": "reconcile_local_manifests",
        "command": ["scripts/package_indexes.py", "--write-local-manifests"],
        "sources": (),
        "requires_refresh": False,
    },
    {
        "kind": STEP_EVAL_GATE,
        "name": "rag_eval_gate",
        "command": ["tests/eval/run_eval.py", "--ci"],
        "sources": (),
        "requires_refresh": False,
        "gate": True,
    },
)


@dataclass(frozen=True)
class Step:
    """One planned, executable unit of work."""

    kind: str
    name: str
    command: list[str]
    trigger: str
    gate: bool = False
    phase: str = ""
    env: dict[str, str] = field(default_factory=dict)

    def argv(self) -> list[str]:
        return [sys.executable, *self.command]

    def environ(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """Return the process environment this step must run with."""
        merged = dict(os.environ if base is None else base)
        merged.update(self.env)
        return merged


@dataclass
class StepResult:
    step: Step
    returncode: int
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return self.skipped or self.returncode == 0


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _log(event: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    event = {"timestamp": _now(), **event}
    with REFRESH_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
    print(json.dumps(event))


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:  # target relocated for tests
        return str(path)


def load_manifest(path: Path = SOURCE_MANIFEST_PATH) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def extra_script_steps(entry: dict, phase: str) -> list[Step]:
    """Return one Step per ``extra_scripts`` entry declared for ``phase``.

    Phase comes from the manifest's ``extra_script_phases`` map (script ->
    ``"pre"``/``"post"``); anything unlisted defaults to ``"post"``, which is
    the historical behaviour. ``pre`` exists because the discovery scripts
    (``discover_vsg_urls.py``, ``discover_aos_urls.py``,
    ``discover_mist_docs_urls.py``) *write* the URL inventory their scraper
    then reads -- running them after the scrape refreshed nothing.
    Declaration order inside a phase is preserved.
    """
    phases = entry.get("extra_script_phases") or {}
    steps: list[Step] = []
    for extra in entry.get("extra_scripts") or []:
        if str(phases.get(extra, PHASE_POST)) != phase:
            continue
        steps.append(
            Step(
                kind=STEP_EXTRA_SCRIPT,
                name=f"{entry['source']}:{Path(extra).stem}",
                command=[extra],
                trigger=f"source:{entry['source']}",
                phase=phase,
            )
        )
    return steps


def build_plan(
    changed_sources: Sequence[str],
    *,
    manifest: Sequence[dict] | None = None,
    refresh_sources: bool = False,
    skip_eval_gate: bool = False,
    include_tool_index: bool = True,
    include_local_manifests: bool = True,
    source_filter: str | None = None,
) -> dict[str, Any]:
    """Materialize the full ordered plan without executing anything.

    Ordering per changed source is ``pre`` extra scripts -> ``scraper`` ->
    ``post`` extra scripts, all from the manifest declaration, so adding a
    source, an ``extra_scripts`` entry, or changing its phase changes the
    plan with no code change here.

    Whether the downstream chain (generated-manifest validation, docs/specs
    rebuild, tool-catalog rebuild, local-manifest reconciliation, eval gate)
    is planned is decided **after** every source-specific *and*
    source-triggered structured step is known -- a source such as
    ``security_advisories`` has no scraper of its own but does trigger the
    security/lifecycle scrape, and that still mutates the corpus, so it must
    still be followed by a rebuild and the gate.
    """
    manifest = list(manifest if manifest is not None else load_manifest())
    by_source = {entry["source"]: entry for entry in manifest}
    changed = [s for s in changed_sources if source_filter in (None, s)]

    source_steps: list[Step] = []
    unrunnable: list[dict[str, str]] = []

    for source in changed:
        entry = by_source.get(source)
        if entry is None:
            unrunnable.append({"source": source, "reason": "not declared in source_manifest.json"})
            continue
        source_steps.extend(extra_script_steps(entry, PHASE_PRE))
        scraper = entry.get("scraper")
        if scraper:
            source_steps.append(
                Step(
                    kind=STEP_SCRAPER,
                    name=f"{source}:scraper",
                    command=[scraper],
                    trigger=f"source:{source}",
                )
            )
        else:
            unrunnable.append(
                {
                    "source": source,
                    "reason": "no scraper registered (manifest declares scraper: null)",
                }
            )
        source_steps.extend(extra_script_steps(entry, PHASE_POST))

    # Pass 1: structured steps a specific changed source triggers. These
    # mutate the corpus just as a scraper does, so they count towards
    # "something was refreshed".
    triggered_steps: list[Step] = []
    for declared in STRUCTURED_STEPS:
        if not declared["sources"]:
            continue
        hits = [source for source in changed if source in declared["sources"]]
        if not hits:
            continue
        if declared.get("requires_refresh") and not refresh_sources:
            unrunnable.append(
                {
                    "source": declared["name"],
                    "reason": "requires --refresh-sources (fetches upstream)",
                }
            )
            continue
        triggered_steps.append(
            Step(
                kind=declared["kind"],
                name=declared["name"],
                command=list(declared["command"]),
                trigger="source:" + ",".join(hits),
                gate=bool(declared.get("gate")),
                env=dict(declared.get("env") or {}),
            )
        )

    # Pass 2: the downstream chain, planned only once every mutating step is
    # known (scrapers, extra scripts, and source-triggered structured steps).
    refreshing = bool(source_steps or triggered_steps)
    post_steps: list[Step] = []
    if refreshing:
        for declared in STRUCTURED_STEPS:
            if declared["sources"]:
                continue
            kind = declared["kind"]
            if kind == STEP_EVAL_GATE and skip_eval_gate:
                continue
            if kind == STEP_TOOL_INDEX_REBUILD and not include_tool_index:
                continue
            if kind == STEP_LOCAL_MANIFESTS and not include_local_manifests:
                continue
            if declared.get("requires_refresh") and not refresh_sources:
                unrunnable.append(
                    {
                        "source": declared["name"],
                        "reason": "requires --refresh-sources (fetches upstream)",
                    }
                )
                continue
            post_steps.append(
                Step(
                    kind=kind,
                    name=declared["name"],
                    command=list(declared["command"]),
                    trigger="post_refresh",
                    gate=bool(declared.get("gate")),
                    env=dict(declared.get("env") or {}),
                )
            )

    steps = [*source_steps, *triggered_steps, *post_steps]

    return {
        "generated_at": _now(),
        "refresh_sources": bool(refresh_sources),
        "changed_sources": list(changed),
        "steps": [asdict(step) for step in steps],
        "unrunnable": unrunnable,
        "snapshot_targets": [
            {"label": label, "path": _display_path(path), "present": path.exists()}
            for label, path in SNAPSHOT_TARGETS
        ],
        "notes": (
            "Plan only -- no command has been executed and no network call made."
            if not refresh_sources
            else "Refresh enabled: scraper/extra_script steps will fetch upstream."
        ),
    }


def plan_steps(plan: dict[str, Any]) -> list[Step]:
    return [Step(**step) for step in plan["steps"]]


# ---------------------------------------------------------------------------
# Transactional snapshot
# ---------------------------------------------------------------------------


def create_snapshot(targets: Sequence[tuple[str, Path]] = SNAPSHOT_TARGETS) -> Path:
    """Copy every mutable artifact aside; return the snapshot directory.

    Records which targets were absent so :func:`restore_snapshot` can delete
    an artifact a failed run created -- restoring "nothing was there" is as
    important as restoring old bytes.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    snap_dir = DATA_DIR / f"_refresh_snapshot_{int(datetime.now().timestamp())}"
    snap_dir.mkdir(parents=True, exist_ok=True)
    absent: list[str] = []
    for label, path in targets:
        if not path.exists():
            absent.append(label)
            continue
        destination = snap_dir / label
        if path.is_dir():
            shutil.copytree(path, destination)
        else:
            shutil.copy2(path, destination)
    (snap_dir / "_absent.json").write_text(json.dumps(absent), encoding="utf-8")
    return snap_dir


def restore_snapshot(
    snap_dir: Path, targets: Sequence[tuple[str, Path]] = SNAPSHOT_TARGETS
) -> list[str]:
    """Restore every snapshotted artifact; return the labels restored/removed."""
    try:
        absent = set(json.loads((snap_dir / "_absent.json").read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        absent = set()
    touched: list[str] = []
    for label, path in targets:
        saved = snap_dir / label
        if label in absent:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
                touched.append(label)
            elif path.exists():
                path.unlink()
                touched.append(label)
            continue
        if not saved.exists():
            continue
        if saved.is_dir():
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
            shutil.copytree(saved, path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, path)
        touched.append(label)
    return touched


def discard_snapshot(snap_dir: Path) -> None:
    shutil.rmtree(snap_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def default_runner(step: Step) -> int:
    declared_env = (
        " " + " ".join(f"{k}={v}" for k, v in sorted(step.env.items())) if step.env else ""
    )
    print(
        f"\n==> [{step.kind}] {step.name}:{declared_env} {' '.join(step.command)}",
        flush=True,
    )
    return subprocess.run(step.argv(), cwd=ROOT, env=step.environ()).returncode


def run_plan(
    plan: dict[str, Any],
    *,
    runner: Callable[[Step], int] = default_runner,
    snapshot_factory: Callable[[], Path] = create_snapshot,
    restore: Callable[[Path], list[str]] = restore_snapshot,
    discard: Callable[[Path], None] = discard_snapshot,
) -> dict[str, Any]:
    """Execute a plan transactionally; restore every artifact on any failure."""
    steps = plan_steps(plan)
    if not steps:
        return {"result": "no_steps", "executed": [], "restored": []}

    snap_dir = snapshot_factory()
    executed: list[StepResult] = []
    try:
        for step in steps:
            returncode = runner(step)
            executed.append(StepResult(step=step, returncode=returncode))
            if returncode != 0:
                restored = restore(snap_dir)
                return {
                    "result": "gate_failed_restored" if step.gate else "step_failed_restored",
                    "failed_step": step.name,
                    "failed_kind": step.kind,
                    "returncode": returncode,
                    "executed": [item.step.name for item in executed],
                    "restored": restored,
                }
        return {
            "result": "success",
            "executed": [item.step.name for item in executed],
            "restored": [],
        }
    except BaseException:
        restore(snap_dir)
        raise
    finally:
        discard(snap_dir)


class CheckInvocationError(RuntimeError):
    """The freshness check could not be trusted, so no refresh may be planned.

    Carries the check's own exit code (or 1) so main() can propagate the
    classification instead of flattening every problem to a generic failure.
    """

    def __init__(self, message: str, *, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


#: Exit codes that describe an *actionable finding*: the check completed and
#: classified real, expected drift. These are exactly what a refresh plan is
#: for, so they are parsed rather than raised on.
ACTIONABLE_CHECK_EXITS: dict[int, str] = {
    taxonomy.EXIT_OK: "no drift",
    taxonomy.EXIT_CONTENT_DRIFT: taxonomy.CONTENT_DRIFT,
    taxonomy.EXIT_SOURCE_SET_CHANGED: "source_added/source_removed",
    taxonomy.EXIT_POINTER_CHANGE: taxonomy.POINTER_CHANGE,
    taxonomy.EXIT_STALE_PIN: taxonomy.STALE_PIN,
}

#: Exit codes that mean the check itself did not complete (or was misused).
#: Refusing to plan from these is the fail-closed half of the contract: a
#: rate-limited or unparsable check must never look like "nothing changed",
#: and must never drive a re-scrape/rebuild off partial information.
FAIL_CLOSED_CHECK_EXITS: dict[int, str] = {
    taxonomy.EXIT_UNAVAILABLE: "transient/blocked failure (unavailable)",
    taxonomy.EXIT_PARSER_ERROR: "parser failure",
    taxonomy.EXIT_USAGE: "usage/configuration error",
}

_REQUIRED_CHECK_KEYS = ("sources", "changed_sources")


def run_check(source: str | None, *, offline: bool, allow_incomplete: bool = False) -> dict:
    """Run the freshness check and return its parsed report, or fail closed.

    ``ingestion/check_updates.py`` is invoked with classified exit codes so
    the *class* of its outcome is visible here rather than flattened to 0/1:

    * ``0/3/4/5/6`` -- completed with an actionable classification; the JSON
      report is parsed and turned into a plan.
    * ``7/8/2`` (and any other non-zero code) -- the check did not complete
      or was misused: raise :class:`CheckInvocationError` and plan nothing.
    * missing/malformed JSON, or a report missing its required keys -- also
      fail closed, for the same reason.

    A report that classified itself ``check_incomplete`` is rejected even if
    its exit code was actionable, so a defensive/legacy exit mode cannot
    smuggle a partial result into a refresh.

    ``allow_incomplete`` (CLI: ``--allow-incomplete-check``) is the explicit,
    opt-in escape hatch for corpora that always contain a few permanently
    blocked pages: it downgrades ``unavailable``/``parser_error`` to a loud
    warning. A usage error, an unrecognized exit code, and malformed or
    incomplete JSON still fail closed -- those mean the check's *output*
    cannot be trusted at all, which no flag should override.
    """
    cmd = [
        sys.executable,
        "ingestion/check_updates.py",
        "--json",
        "--exit-code-mode",
        "classified",
    ]
    if source:
        cmd += ["--source", source]
    if offline:
        cmd.append("--offline")
    print("\n==> Checking sources for updates", flush=True)
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)

    incomplete_exit = result.returncode in (
        taxonomy.EXIT_UNAVAILABLE,
        taxonomy.EXIT_PARSER_ERROR,
    )
    if incomplete_exit and allow_incomplete:
        print(
            f"    WARNING: check reported "
            f"{FAIL_CLOSED_CHECK_EXITS[result.returncode]} (exit "
            f"{result.returncode}); --allow-incomplete-check was passed, so "
            f"planning continues from a partial result",
            file=sys.stderr,
            flush=True,
        )
    elif result.returncode in FAIL_CLOSED_CHECK_EXITS:
        raise CheckInvocationError(
            f"freshness check did not complete: "
            f"{FAIL_CLOSED_CHECK_EXITS[result.returncode]} "
            f"(exit {result.returncode}). Refusing to plan a refresh from an "
            f"incomplete check. {(result.stderr or '').strip()[:400]}",
            exit_code=result.returncode,
        )
    if result.returncode not in ACTIONABLE_CHECK_EXITS and not (
        incomplete_exit and allow_incomplete
    ):
        raise CheckInvocationError(
            f"freshness check exited with unrecognized code {result.returncode}; "
            f"refusing to plan a refresh. {(result.stderr or '').strip()[:400]}",
            exit_code=result.returncode or 1,
        )

    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CheckInvocationError(
            f"freshness check produced malformed JSON: {exc}"
        ) from exc
    if not isinstance(report, dict):
        raise CheckInvocationError("freshness check JSON is not an object")
    missing = [key for key in _REQUIRED_CHECK_KEYS if key not in report]
    if missing:
        raise CheckInvocationError(
            f"freshness check JSON is missing required key(s): {', '.join(missing)}"
        )
    drift_report = report.get("drift_report") or {}
    if drift_report.get("check_incomplete") and not allow_incomplete:
        raise CheckInvocationError(
            "freshness check reported check_incomplete (network/parser failures "
            "present); refusing to plan a refresh from a partial result.",
            exit_code=taxonomy.EXIT_UNAVAILABLE,
        )
    if result.returncode in ACTIONABLE_CHECK_EXITS and result.returncode != taxonomy.EXIT_OK:
        print(
            f"    check reported {ACTIONABLE_CHECK_EXITS[result.returncode]} "
            f"(exit {result.returncode}) -- planning from it",
            flush=True,
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source", help="Only check/refresh this manifest source")
    parser.add_argument(
        "--check-only", action="store_true", help="Report drift only -- never execute a step"
    )
    parser.add_argument(
        "--plan",
        "--dry-run",
        dest="plan",
        action="store_true",
        help="Print the JSON plan and exit; executes nothing and fetches nothing.",
    )
    parser.add_argument(
        "--refresh-sources",
        action="store_true",
        help=(
            "Actually fetch upstream. Without this the freshness check runs offline "
            "and sources are reported not_checked, never fresh."
        ),
    )
    parser.add_argument(
        "--skip-eval-gate",
        action="store_true",
        help="Rebuild without running the eval gate (not recommended).",
    )
    parser.add_argument("--no-tool-index", action="store_true")
    parser.add_argument("--no-local-manifests", action="store_true")
    parser.add_argument(
        "--allow-incomplete-check",
        action="store_true",
        help=(
            "Plan even when the freshness check reported unavailable/parser "
            "failures (default: fail closed and plan nothing)."
        ),
    )
    parser.add_argument(
        "--changed-source",
        action="append",
        default=[],
        help="Force a source into the plan (testing/manual re-runs).",
    )
    args = parser.parse_args()

    changed_sources: list[str] = list(args.changed_source)
    report: dict[str, Any] | None = None
    if not args.changed_source:
        try:
            report = run_check(
                args.source,
                offline=not args.refresh_sources,
                allow_incomplete=args.allow_incomplete_check,
            )
        except CheckInvocationError as exc:
            print(f"\nFreshness check failed closed: {exc}", file=sys.stderr)
            return exc.exit_code
        changed_sources = report["changed_sources"]
        for r in report["sources"]:
            if not r["resolvable"]:
                print(f"[SKIP] {r['source']}: {r['reason']}")
            else:
                flag = "CHANGED" if r["source"] in changed_sources else r.get("result_class", "ok")
                print(
                    f"[{flag}] {r['source']}: new={r['new']} changed={r['changed']} "
                    f"unchanged={r['unchanged']} blocked={r['blocked']} errors={r['errors']}"
                )

    plan = build_plan(
        changed_sources,
        refresh_sources=args.refresh_sources,
        skip_eval_gate=args.skip_eval_gate,
        include_tool_index=not args.no_tool_index,
        include_local_manifests=not args.no_local_manifests,
        source_filter=args.source,
    )

    if args.plan:
        print(json.dumps(plan, indent=2))
        return 0

    if not changed_sources:
        _log({"action": "refresh", "changed_sources": [], "result": "no_changes",
              "refresh_sources": args.refresh_sources})
        print(
            "\nNo changed sources to refresh."
            + ("" if args.refresh_sources else " (offline check -- nothing was fetched)")
        )
        return 0

    if args.check_only:
        print(f"\n--check-only: would run {len(plan['steps'])} step(s) for {changed_sources}")
        print(json.dumps(plan, indent=2))
        return 0

    if not args.refresh_sources:
        print(
            "\nRefresh not enabled: pass --refresh-sources to execute the plan. "
            "Printing the plan instead."
        )
        print(json.dumps(plan, indent=2))
        return 0

    outcome = run_plan(plan)
    _log({
        "action": "refresh",
        "changed_sources": changed_sources,
        "refresh_sources": True,
        **outcome,
    })
    if outcome["result"] == "success":
        print(f"\nRefresh complete: {len(outcome['executed'])} step(s) executed.")
        return 0
    if outcome["result"] == "no_steps":
        print("\nNothing executable in the plan -- see 'unrunnable' entries.")
        return 1
    print(
        f"\nStep {outcome['failed_step']} failed (rc={outcome['returncode']}); "
        f"restored: {', '.join(outcome['restored']) or 'nothing to restore'}"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
