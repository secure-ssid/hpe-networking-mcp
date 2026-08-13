"""Credential-gated Central v0.7 evaluation harness.

Default mode is fully offline and fixture-backed: it exercises a bounded set
of curated Central read tools (`hpe_networking_mcp.mcp_servers.monitoring` /
`hpe_networking_mcp.mcp_servers.config`) against a `FakeCentralClient` stand-in, never touches
the network, and writes a redacted `live_lifecycle_evidence` artifact via
`hpe_networking_mcp.pipeline.artifact_contracts`.

Two opt-in modes, gated through `hpe_networking_mcp.pipeline.live_test_config` (never inferred
from credential presence alone):

- `--live-read`: swaps in the real `hpe_networking_mcp.mcp_servers.shared.get_client()` and
  runs the *same* bounded set of steps as bounded, GET-only calls against a
  live tenant. Requires `HPE_MCP_LIVE_TEST_CENTRAL_READ=1`. A GET-only
  guard is installed on the client's `_request` before any live call so a
  non-GET verb raises before transmission -- this mode can never write.
- `--live-write`: runs a disposable create/read-back/delete VSF-template
  round trip (`hpe_networking_mcp.mcp_servers.config.build_vsf_template` /
  `delete_vsf_template`) against a lab-owned scope, then cleans up.
  Requires *both* `HPE_MCP_LIVE_TEST_CENTRAL_READ=1` and
  `HPE_MCP_LIVE_TEST_CENTRAL_WRITE=1` (see
  `hpe_networking_mcp.pipeline.live_test_config.live_test_write_enabled`), plus a `--scope-id`
  and `--device-function` naming a real, disposable/lab scope. This mode is
  intentionally never invoked by CI or by the default `main()` flow in this
  repo revision; it exists so a human operator can opt in explicitly later.

Every artifact this script writes is built through
`hpe_networking_mcp.pipeline.artifact_contracts.write_artifact`, which redacts secrets and
replaces scope/serial identifiers with a deterministic
`sha256:<hex12>` placeholder before the file ever touches disk -- no raw
vendor response is ever included.
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hpe_networking_mcp.pipeline import artifact_contracts as contracts
from hpe_networking_mcp.pipeline import live_test_config

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "outputs" / "central-070-live-evidence.json"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_PLATFORM = "central"


# ---------------------------------------------------------------------------
# Fixture-backed offline steps (default mode -- no network I/O)
# ---------------------------------------------------------------------------


class FakeCentralClient:
    """Minimal stand-in for `hpe_networking_mcp.pipeline.clients.central_client.CentralClient`
    used only by the default offline fixture mode below."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("GET", endpoint))
        if endpoint == "/network-monitoring/v1/globalScopeId":
            return {"scopeId": "fixture-global-scope"}
        if endpoint.endswith("/config-health/devices"):
            return {"items": [{"serialNumber": "FIXTURE001", "status": "COMPLIANT"}]}
        if endpoint == "/network-reporting/v1/reports-meta":
            return {"reportTypes": ["INVENTORY"]}
        return {"items": []}

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        raise RuntimeError("FakeCentralClient._request should not be called directly")


def _offline_steps() -> list[dict[str, Any]]:
    """Run the bounded step list against the fixture client and return
    bounded, JSON-shaped step records (never a raw vendor response)."""
    client = FakeCentralClient()
    steps: list[dict[str, Any]] = []

    def _step(name: str, fn: Callable[[], Any]) -> None:
        try:
            data = fn()
            steps.append({
                "name": name,
                "status": "ok",
                "item_count": len(data.get("items", [])) if isinstance(data, dict) else None,
            })
        except Exception as exc:
            steps.append({"name": name, "status": "error", "error": str(exc)})

    _step("get_global_scope_id", lambda: client.get("/network-monitoring/v1/globalScopeId"))
    _step(
        "list_devices_config_health",
        lambda: client.get("/network-config/v1alpha1/config-health/devices"),
    )
    _step("get_reports_metadata", lambda: client.get("/network-reporting/v1/reports-meta"))
    return steps


# ---------------------------------------------------------------------------
# Live, GET-only steps (--live-read)
# ---------------------------------------------------------------------------


def _install_get_only_guard(client: Any) -> None:
    if getattr(client, "_central_070_guard_installed", False):
        return
    original = client._request

    def guarded(method: str, *args: Any, **kwargs: Any) -> Any:
        verb = str(method or "").upper()
        if verb not in _SAFE_METHODS:
            raise RuntimeError(f"Blocked non-read-only HTTP method before transmission: {verb}.")
        return original(method, *args, **kwargs)

    client._request = guarded
    client._central_070_guard_installed = True


def _live_read_steps() -> list[dict[str, Any]]:
    """Bounded GET-only steps against a live tenant. Caller must have
    already verified `live_test_config.live_test_read_enabled("central")`."""
    from hpe_networking_mcp.mcp_servers import config as config_tools
    from hpe_networking_mcp.mcp_servers import monitoring as monitoring_tools
    from hpe_networking_mcp.mcp_servers.shared import get_client

    client = get_client()
    _install_get_only_guard(client)

    steps: list[dict[str, Any]] = []

    def _step(name: str, fn: Callable[[], Any]) -> None:
        try:
            data = fn()
            item_count = None
            if isinstance(data, dict):
                items = data.get("items")
                if isinstance(items, list):
                    item_count = len(items)
            steps.append({"name": name, "status": "ok", "item_count": item_count})
        except Exception as exc:
            steps.append({"name": name, "status": "error", "error": str(exc)})

    _step("get_global_scope_id", monitoring_tools.get_global_scope_id)
    _step(
        "list_devices_config_health",
        lambda: monitoring_tools.list_devices_config_health(limit=5),
    )
    _step("get_reports_metadata", monitoring_tools.get_reports_metadata)
    _step(
        "get_network_profile_vsf_template",
        lambda: config_tools.get_network_profile("vsf-template", limit=5),
    )
    return steps


# ---------------------------------------------------------------------------
# Disposable-write lifecycle harness (--live-write; NOT invoked by default)
# ---------------------------------------------------------------------------


def run_disposable_write_lifecycle(scope_id: str, device_function: str) -> dict[str, Any]:
    """Create -> read back -> delete -> read back one lab-owned, uniquely
    named VSF template. Caller must have already verified
    `live_test_config.live_test_write_enabled("central")`.

    This function is intentionally never called by `main()` in this repo
    revision -- it exists so an operator can opt in explicitly, later, once
    both live-test env gates and the Central write gate
    (`HPE_MCP_CENTRAL_WRITES=1`, already the default) are set on purpose.
    """
    from hpe_networking_mcp.mcp_servers import config as config_tools

    name = f"hpe-mcp-lab-vsf-{uuid.uuid4().hex[:8]}"
    steps: list[dict[str, Any]] = []

    def _step(step_name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any] | None:
        try:
            result = fn()
            steps.append({"name": step_name, "status": "ok"})
            return result
        except Exception as exc:
            steps.append({"name": step_name, "status": "error", "error": str(exc)})
            return None

    create_result = _step(
        "create",
        lambda: config_tools.build_vsf_template(
            name, 2, scope_id=scope_id, device_function=device_function,
            dry_run=False, confirm=True,
        ),
    )
    if create_result is not None:
        _step(
            "read_back_after_create",
            lambda: config_tools.get_network_profile(
                "vsf-template", name=name, scope_id=scope_id, device_function=device_function,
                object_type="LOCAL",
            ),
        )
    _step(
        "delete",
        lambda: config_tools.delete_vsf_template(
            name, scope_id=scope_id, device_function=device_function,
            dry_run=False, confirm=True,
        ),
    )
    return {
        "mode": "disposable_write",
        "target_identifier_hash": contracts.hash_identifier(name),
        "steps": steps,
    }


# ---------------------------------------------------------------------------
# Artifact assembly + CLI
# ---------------------------------------------------------------------------


def _write_evidence(
    mode: str, steps: list[dict[str, Any]], output: Path
) -> contracts.ManifestEntry:
    ok = sum(1 for s in steps if s.get("status") == "ok")
    payload = {
        "platform": _PLATFORM,
        "mode": mode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "steps": steps,
        "summary": {"steps_ok": ok, "steps_total": len(steps)},
        "errors": [s["error"] for s in steps if s.get("status") == "error"],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    return contracts.write_artifact(output, contracts.LIVE_LIFECYCLE_EVIDENCE, payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-read", action="store_true", help="Run bounded live GET-only steps.")
    parser.add_argument(
        "--live-write",
        action="store_true",
        help="Print the disposable-write lifecycle status (never executes it in this script).",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    status = live_test_config.live_test_status(_PLATFORM)

    if args.live_read:
        if not live_test_config.live_test_read_enabled(_PLATFORM):
            blocked = {"status": "blocked", "reason": "read not enabled", **status}
            print(json.dumps(blocked, indent=2))
            return 1
        steps = _live_read_steps()
        mode = "read_only"
    else:
        steps = _offline_steps()
        mode = "read_only"

    entry = _write_evidence(mode, steps, args.output)
    result: dict[str, Any] = {
        "manifest_entry": {
            "filename": entry.filename,
            "kind": entry.kind,
            "schema_version": entry.schema_version,
            "size_bytes": entry.size_bytes,
            "sha256": entry.sha256,
            "redacted": entry.redacted,
        },
        "live_test_status": status,
    }

    if args.live_write:
        # Deliberately not invoked: report gate state only. A future,
        # explicitly-approved revision can call
        # run_disposable_write_lifecycle(scope_id, device_function) once an
        # operator has supplied a real lab scope on the command line.
        result["disposable_write"] = {
            "executed": False,
            "write_enabled": live_test_config.live_test_write_enabled(_PLATFORM),
            "note": (
                "run_disposable_write_lifecycle() exists but is never called by main() "
                "in this revision -- see module docstring."
            ),
        }

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
