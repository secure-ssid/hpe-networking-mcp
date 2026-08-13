"""GLP v0.7 credential-gated live evaluator + disposable-write harness.

Read-only evaluator
--------------------
Exercises a bounded sample of the v0.7 GLP-depth curated tools (compute,
storage-fleet, block-storage, virtualization, backup-recovery, data
services) plus the pre-existing device/subscription/user/RBAC reads and
the new ``plan_glp_reconciliation`` composite against a real GLP workspace,
and writes a redacted ``live_lifecycle_evidence`` artifact
(``src/hpe_networking_mcp/pipeline/artifact_contracts.py``).

Every live call is gated through ``src/hpe_networking_mcp/pipeline/live_test_config.py`` -- **not**
through the MCP write-tool gate (``HPE_MCP_GLP_V2BETA1_WRITES``), which
this script never sets on its own:

* ``HPE_MCP_LIVE_TEST_GLP_READ=1`` is required before any network call
  is made at all (read or write). Credentials merely being configured in
  the environment/`config/credentials.yaml` never implies authorization.
* ``HPE_MCP_LIVE_TEST_GLP_WRITE=1`` (in addition to the read flag) is
  required for the disposable-write probe (see ``--write-probe`` below);
  it also still requires the existing ``HPE_MCP_GLP_V2BETA1_WRITES=1``
  MCP write-tool gate, so a write probe needs *three* independent,
  explicit opt-ins layered together.

Disposable-write harness
------------------------
``--write-probe --vm-id <id>`` power-cycles one explicitly-named,
lab-owned virtual machine (power-off, read back its state, power-on,
read back again) via ``set_glp_virtual_machine_power`` /
``get_glp_virtual_machine``, and always attempts the power-on cleanup
step even if a prior step failed. This is implemented in full but is
never invoked by this repository's own test/CI/release flows -- it is
for a human operator to run only after confirming the target VM is
lab-owned and disposable.

Usage
-----
    # Status only -- never touches the network:
    python scripts/evaluate_glp_070_depth.py --status

    # Bounded read-only evaluation (requires the read env var above):
    python scripts/evaluate_glp_070_depth.py --output outputs/glp-070-evidence.json

    # Disposable-write VM power-cycle probe (requires all three gates above):
    python scripts/evaluate_glp_070_depth.py --write-probe --vm-id <lab-vm-id> \\
        --output outputs/glp-070-write-evidence.json
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from hpe_networking_mcp.pipeline import artifact_contracts as contracts  # noqa: E402
from hpe_networking_mcp.pipeline import live_test_config as live_config  # noqa: E402

PLATFORM = "glp"
_MAX_SAMPLE = 5


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _run_read_step(name: str, fn: Any, /, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Run one bounded read step, sync or async, and return a step record.

    Never raises -- a failing step is recorded as ``status: "error"`` so one
    bad call never aborts the rest of the evaluation (partial-failure
    reporting).
    """
    started = time.monotonic()
    try:
        result = fn(*args, **kwargs)
        if asyncio.iscoroutine(result):
            result = await result
        errors: list[str] = []
        status = "ok"
        if isinstance(result, dict):
            raw_errors = result.get("errors")
            if isinstance(raw_errors, list):
                errors.extend(str(error) for error in raw_errors)
            elif raw_errors:
                errors.append(str(raw_errors))
            if result.get("error"):
                errors.append(str(result["error"]))
            status_code = result.get("status_code")
            if isinstance(status_code, int) and not 200 <= status_code < 300:
                errors.append(f"HTTP status {status_code}")
            result_status = str(result.get("status") or "").lower()
            if result_status in {"blocked", "error", "failed", "failure"}:
                errors.append(f"result status {result_status}")
            if errors:
                status = "error" if result.get("error") or status_code else "partial"
        summary: dict[str, Any] = {"errors": errors}
        if isinstance(result, dict):
            data = result.get("data") if "data" in result else result
            if isinstance(data, dict):
                items = data.get("items")
                if isinstance(items, list):
                    summary["item_count"] = len(items)
                pagination = data.get("_pagination")
                if isinstance(pagination, dict):
                    summary["pagination"] = pagination
                counts = data.get("counts") if name == "plan_glp_reconciliation" else None
                if counts:
                    summary["counts"] = counts
                findings = data.get("findings") if name == "plan_glp_reconciliation" else None
                if isinstance(findings, list):
                    summary["finding_count"] = len(findings)
        return {
            "name": name,
            "status": status,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "summary": summary,
        }
    except Exception as exc:  # defensive: one step must never abort the run
        return {
            "name": name,
            "status": "error",
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "summary": {"errors": [str(exc)]},
        }


async def run_readonly_evaluation() -> dict[str, Any]:
    """Run the bounded read-only evaluation. Caller must have already
    confirmed ``live_test_config.live_test_read_enabled("glp")``.
    """
    from hpe_networking_mcp.mcp_servers import glp

    steps: list[dict[str, Any]] = []
    steps.append(
        {
            "name": "glp_write_status",
            "status": "ok",
            "duration_ms": 0.0,
            "summary": {"enabled": glp.glp_write_status()["enabled"]},
        }
    )
    steps.append(
        await _run_read_step("list_glp_devices", glp.list_glp_devices, limit=_MAX_SAMPLE)
    )
    steps.append(
        await _run_read_step(
            "list_glp_subscriptions", glp.list_glp_subscriptions, limit=_MAX_SAMPLE
        )
    )
    steps.append(
        await _run_read_step("list_glp_users", glp.list_glp_users, limit=_MAX_SAMPLE)
    )
    steps.append(
        await _run_read_step(
            "list_glp_role_assignments", glp.list_glp_role_assignments, limit=_MAX_SAMPLE
        )
    )
    steps.append(
        await _run_read_step("list_glp_scope_groups", glp.list_glp_scope_groups, limit=_MAX_SAMPLE)
    )
    # Region-aware v0.7 families -- each individually error-isolated, since
    # GLP_GENERATED_REGION may not be valid for every family in one run.
    steps.append(
        await _run_read_step(
            "list_glp_compute_servers", glp.list_glp_compute_servers, limit=_MAX_SAMPLE
        )
    )
    steps.append(
        await _run_read_step(
            "list_glp_storage_systems", glp.list_glp_storage_systems, limit=_MAX_SAMPLE
        )
    )
    steps.append(
        await _run_read_step(
            "list_glp_block_storage_volumes", glp.list_glp_block_storage_volumes, limit=_MAX_SAMPLE
        )
    )
    steps.append(
        await _run_read_step(
            "list_glp_virtual_machines", glp.list_glp_virtual_machines, limit=_MAX_SAMPLE
        )
    )
    steps.append(
        await _run_read_step(
            "list_glp_backup_protection_jobs",
            glp.list_glp_backup_protection_jobs,
            limit=_MAX_SAMPLE,
        )
    )
    steps.append(
        await _run_read_step(
            "list_glp_data_services_issues", glp.list_glp_data_services_issues, limit=_MAX_SAMPLE
        )
    )
    steps.append(
        await _run_read_step("plan_glp_reconciliation", glp.plan_glp_reconciliation, sample_size=20)
    )

    errors = [f"{s['name']}: {e}" for s in steps for e in s["summary"].get("errors", [])]
    ok_count = sum(1 for s in steps if s["status"] == "ok")
    return {
        "platform": PLATFORM,
        "mode": "read_only",
        "generated_at": _now_iso(),
        "steps": steps,
        "summary": {
            "step_count": len(steps),
            "ok_count": ok_count,
            "partial_count": sum(1 for s in steps if s["status"] == "partial"),
            "error_count": sum(1 for s in steps if s["status"] == "error"),
        },
        "errors": errors,
        "secrets_included": False,
        "raw_response_included": False,
    }


async def run_write_probe(vm_id: str) -> dict[str, Any]:
    """Disposable-write VM power-cycle probe against one lab-owned VM.

    Requires ``live_test_config.live_test_write_enabled("glp")`` (checked by
    the caller) *and* the existing ``HPE_MCP_GLP_V2BETA1_WRITES=1`` MCP
    write-tool gate (checked inside ``set_glp_virtual_machine_power``
    itself -- this function does not bypass it). Always attempts the
    power-on cleanup step even if power-off or the read-backs fail.
    """
    from hpe_networking_mcp.mcp_servers import glp

    steps: list[dict[str, Any]] = []
    target_hash = contracts.hash_identifier(vm_id)

    steps.append(
        await _run_read_step(
            "get_glp_virtual_machine_before", glp.get_glp_virtual_machine, vm_id
        )
    )
    steps.append(
        await _run_read_step(
            "power_off",
            glp.set_glp_virtual_machine_power,
            vm_id,
            "power-off",
            dry_run=False,
            confirm=True,
        )
    )
    steps.append(
        await _run_read_step(
            "get_glp_virtual_machine_after_power_off", glp.get_glp_virtual_machine, vm_id
        )
    )
    try:
        # Cleanup always attempted, even if a prior step raised/failed.
        steps.append(
            await _run_read_step(
                "power_on_cleanup",
                glp.set_glp_virtual_machine_power,
                vm_id,
                "power-on",
                dry_run=False,
                confirm=True,
            )
        )
    finally:
        steps.append(
            await _run_read_step(
                "get_glp_virtual_machine_after_cleanup", glp.get_glp_virtual_machine, vm_id
            )
        )

    errors = [f"{s['name']}: {e}" for s in steps for e in s["summary"].get("errors", [])]
    return {
        "platform": PLATFORM,
        "mode": "disposable_write",
        "generated_at": _now_iso(),
        "steps": steps,
        "summary": {"step_count": len(steps), "vm_id_hash": target_hash},
        "errors": errors,
        "target_identifier_hash": target_hash,
        "secrets_included": False,
        "raw_response_included": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print the live-test gate status and exit (no network calls).",
    )
    parser.add_argument(
        "--output", default=None, help="Path to write the live_lifecycle_evidence artifact."
    )
    parser.add_argument(
        "--write-probe",
        action="store_true",
        help="Run the disposable-write VM power-cycle probe instead of the read-only evaluation.",
    )
    parser.add_argument("--vm-id", default=None, help="Lab-owned VM id for --write-probe.")
    args = parser.parse_args(argv)

    status = live_config.live_test_status(PLATFORM)

    if args.status:
        import json

        print(json.dumps(status, indent=2, sort_keys=True))
        return 0

    if args.write_probe:
        if not args.vm_id:
            print("--write-probe requires --vm-id <lab-vm-id>", file=sys.stderr)
            return 2
        if not live_config.live_test_write_enabled(PLATFORM):
            print(
                "Disposable-write probe not enabled. Set "
                f"{live_config.live_test_read_env_var(PLATFORM)}=1 and "
                f"{live_config.live_test_write_env_var(PLATFORM)}=1 (both required), "
                "plus HPE_MCP_GLP_V2BETA1_WRITES=1 for the underlying MCP write gate.",
                file=sys.stderr,
            )
            return 3
        payload = asyncio.run(run_write_probe(args.vm_id))
        kind = contracts.LIVE_LIFECYCLE_EVIDENCE
    else:
        if not live_config.live_test_read_enabled(PLATFORM):
            print(
                "Read-only live evaluation not enabled. Set "
                f"{live_config.live_test_read_env_var(PLATFORM)}=1 to run it "
                "(credentials being configured is never sufficient by itself).",
                file=sys.stderr,
            )
            return 3
        payload = asyncio.run(run_readonly_evaluation())
        kind = contracts.LIVE_LIFECYCLE_EVIDENCE

    output = args.output or "outputs/glp-070-live-evidence.json"
    entry = contracts.write_artifact(output, kind, payload)
    print(f"wrote {entry.filename} ({entry.size_bytes} bytes, sha256={entry.sha256[:12]}...)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
