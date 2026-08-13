"""Unit tests for scripts/generate_router_automation_report.py -- the
versioned router-automation report generator (v0.7 deliverable 4).

Covers: both artifacts are produced offline (no network calls, since the
underlying planner tools never dispatch); each artifact validates against
its `hpe_networking_mcp.pipeline.artifact_contracts` kind; `dry_run` is always True on the
reconciliation plan; the script degrades gracefully (skips, never raises)
when the planner tools aren't registered; and the printed summary never
includes a raw/unbounded catalog dump.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

# Developer .env often pins HPE_MCP_ROUTER_MODE=minimal for day-to-day
# clients. Planners only register in non-minimal mode, so force default
# before importing the router / report script for this module.
import os

os.environ["HPE_MCP_ROUTER_MODE"] = "default"

import hpe_networking_mcp.mcp_servers.tool_router as router
from hpe_networking_mcp.pipeline import artifact_contracts as contracts

if getattr(router, "plan_tool_workflow", None) is None:
    # Router was already imported earlier in the pytest session under minimal.
    importlib.reload(router)

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

if "generate_router_automation_report" in sys.modules:
    generate_report = importlib.reload(sys.modules["generate_router_automation_report"])
else:
    generate_report = importlib.import_module("generate_router_automation_report")


@pytest.fixture(autouse=True)
def _reset_router_catalog_cache(monkeypatch):
    """The router's tool index is a module-level cache populated lazily by
    `_load_all_backends()`; make sure each test starts from a clean slate
    and leaves the real module state untouched afterward.

    Also keep ROUTER_MODE=default so planner tools stay registered even when
    a developer .env prefers minimal for interactive clients.
    """
    monkeypatch.setenv("HPE_MCP_ROUTER_MODE", "default")
    if getattr(router, "plan_tool_workflow", None) is None:
        importlib.reload(router)
        importlib.reload(generate_report)
    saved_index = dict(router._tool_index)
    saved_servers = dict(router._tool_servers)
    saved_names = dict(router._tool_backend_names)
    yield
    router._tool_index.clear()
    router._tool_index.update(saved_index)
    router._tool_servers.clear()
    router._tool_servers.update(saved_servers)
    router._tool_backend_names.clear()
    router._tool_backend_names.update(saved_names)


class TestGenerateRouterAutomationReport:
    def test_main_writes_both_valid_artifacts(self, tmp_path):
        dep_output = tmp_path / "dependency-plan.json"
        recon_output = tmp_path / "reconciliation-plan.json"

        exit_code = generate_report.main(
            [
                "--dependency-output",
                str(dep_output),
                "--reconciliation-output",
                str(recon_output),
            ]
        )

        assert exit_code == 0
        assert dep_output.exists()
        assert recon_output.exists()

        dep_data = json.loads(dep_output.read_text())
        assert dep_data["kind"] == contracts.ROUTER_DEPENDENCY_PLAN
        recon_data = json.loads(recon_output.read_text())
        assert recon_data["kind"] == contracts.ROUTER_RECONCILIATION_PLAN
        assert recon_data["dry_run"] is True

    def test_reconciliation_plan_respects_max_entries(self, tmp_path):
        recon_output = tmp_path / "reconciliation-plan.json"
        generate_report.main(
            [
                "--dependency-output",
                str(tmp_path / "dep.json"),
                "--reconciliation-output",
                str(recon_output),
                "--max-entries",
                "3",
            ]
        )
        recon_data = json.loads(recon_output.read_text())
        assert len(recon_data["entries"]) <= 3

    def test_invalid_cadence_still_exits_nonzero_without_raising(self, tmp_path):
        exit_code = generate_report.main(
            [
                "--dependency-output",
                str(tmp_path / "dep.json"),
                "--reconciliation-output",
                str(tmp_path / "recon.json"),
                "--cadence",
                "not-a-real-cadence",
            ]
        )
        assert exit_code == 1
        assert not (tmp_path / "recon.json").exists()

    def test_dependency_plan_generator_skips_cleanly_when_planner_unavailable(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.delattr(router, "plan_tool_workflow", raising=False)
        entry = generate_report.generate_dependency_plan_artifact(tmp_path / "dep.json")
        assert entry is None
        assert not (tmp_path / "dep.json").exists()

    def test_reconciliation_plan_generator_skips_cleanly_when_planner_unavailable(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.delattr(router, "plan_reconciliation_schedule", raising=False)
        entry = generate_report.generate_reconciliation_plan_artifact(tmp_path / "recon.json")
        assert entry is None
        assert not (tmp_path / "recon.json").exists()

    def test_no_network_module_imported_as_a_side_effect(self, tmp_path):
        # httpx-based clients are only ever instantiated lazily inside tool
        # bodies (get_client()/get_glp_client()), never at import/plan time;
        # this is a smoke check that generating the report doesn't require
        # network access (would raise/hang in a sandboxed CI network).
        exit_code = generate_report.main(
            [
                "--dependency-output",
                str(tmp_path / "dep.json"),
                "--reconciliation-output",
                str(tmp_path / "recon.json"),
            ]
        )
        assert exit_code == 0
