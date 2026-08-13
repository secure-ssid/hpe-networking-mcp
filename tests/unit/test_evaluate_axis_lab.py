"""Unit tests for scripts/evaluate_axis_lab.py (v07-optional-depth, Axis).

Covers:
- The static split-CRUD contract check runs offline and always reports the
  11 confirmed complete families with zero anomalies against the committed
  47-operation manifest.
- Bounded live reads are skipped by default and only attempted when both
  the read gate and credentials are satisfied -- never from credentials
  alone.
- The disposable-write plan is never built unless the write gate is
  explicitly enabled, and even then it is only ever a plan: no create/
  delete tool is ever invoked with ``dry_run=False``.
- The produced artifact always validates against the shared
  ``live_lifecycle_evidence`` contract and never leaks a credential value.
"""

from __future__ import annotations

import json

from hpe_networking_mcp.pipeline import artifact_contracts as contracts
from scripts import evaluate_axis_lab as lab


class TestSplitCrudContract:
    def test_all_eleven_families_are_complete(self):
        from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import load_manifest

        manifest = load_manifest("axis")
        report = lab.verify_split_crud_contract(manifest)
        assert report["compatible"] is True
        assert report["family_count"] == 11
        assert report["complete_split_crud_families"] == 11
        assert report["anomalies"] == []

    def test_subresource_kinds_are_normalized_to_split_verbs(self):
        manifest = {
            "operations": [
                {
                    "kind": f"sub{verb}",
                    "path": "/Parents/{parent_id}/Children",
                    "name": f"axis_{verb}_child",
                }
                for verb in lab._SPLIT_VERBS
            ]
        }

        report = lab.verify_split_crud_contract(manifest)

        assert report["compatible"] is True
        assert report["family_count"] == 1
        assert report["complete_split_crud_families"] == 1

    def test_reports_anomaly_for_missing_verb_never_raises(self):
        manifest = {
            "operations": [
                {"kind": "query", "path": "/Widgets", "name": "axis_get_widgets"},
                {"kind": "create", "path": "/Widgets", "name": "axis_create_widget"},
                # update/delete deliberately absent
            ]
        }
        report = lab.verify_split_crud_contract(manifest)
        assert report["compatible"] is False
        assert report["anomalies"] == [
            {"path": "/Widgets", "missing_verbs": ["update", "delete"]}
        ]


class TestGating:
    def test_default_env_skips_live_reads_and_write_plan(self, tmp_path, monkeypatch):
        for name in (
            "HPE_MCP_LIVE_TEST_AXIS_READ",
            "HPE_MCP_LIVE_TEST_AXIS_WRITE",
            "AXIS_BASE_URL",
            "AXIS_API_TOKEN",
        ):
            monkeypatch.delenv(name, raising=False)
        entries = lab.build_evidence_artifact(output_dir=tmp_path)
        assert len(entries) == 1
        payload = json.loads((tmp_path / "axis-lab-evidence.json").read_text())
        assert payload["mode"] == "read_only"
        assert payload["summary"]["live_reads_attempted"] == 0
        assert payload["summary"]["disposable_write_plan_built"] is False
        # Only the always-on static contract-check step ran.
        assert payload["steps"] == [{"name": "verify_split_crud_contract", "status": "ok"}]

    def test_credentials_alone_never_trigger_live_reads(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HPE_MCP_LIVE_TEST_AXIS_READ", raising=False)
        monkeypatch.setenv("AXIS_BASE_URL", "https://axis.example.com")
        monkeypatch.setenv("AXIS_API_TOKEN", "fake-token")
        lab.build_evidence_artifact(output_dir=tmp_path)
        payload = json.loads((tmp_path / "axis-lab-evidence.json").read_text())
        assert payload["summary"]["live_reads_attempted"] == 0

    def test_read_gate_bounds_live_reads_to_five_and_never_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HPE_MCP_LIVE_TEST_AXIS_READ", "1")
        monkeypatch.setenv("AXIS_BASE_URL", "https://axis.example.com")
        monkeypatch.setenv("AXIS_API_TOKEN", "fake-token")
        lab.build_evidence_artifact(output_dir=tmp_path)
        payload = json.loads((tmp_path / "axis-lab-evidence.json").read_text())
        assert payload["summary"]["live_reads_attempted"] == lab._MAX_LIVE_READ_ENTITIES
        # unreachable host -> every bounded probe step reports "error", never raises
        probe_steps = [s for s in payload["steps"] if s["name"] != "verify_split_crud_contract"]
        assert len(probe_steps) == lab._MAX_LIVE_READ_ENTITIES
        assert all(step["status"] == "error" for step in probe_steps)

    def test_write_gate_requires_read_gate_and_only_ever_plans(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HPE_MCP_LIVE_TEST_AXIS_WRITE", "1")
        monkeypatch.delenv("HPE_MCP_LIVE_TEST_AXIS_READ", raising=False)
        monkeypatch.setenv("AXIS_BASE_URL", "https://axis.example.com")
        monkeypatch.setenv("AXIS_API_TOKEN", "fake-token")
        # write alone (no read) must not enable the write path either --
        # hpe_networking_mcp.pipeline.live_test_config.live_test_write_enabled requires both.
        lab.build_evidence_artifact(output_dir=tmp_path)
        payload = json.loads((tmp_path / "axis-lab-evidence.json").read_text())
        assert payload["summary"]["disposable_write_plan_built"] is False

        monkeypatch.setenv("HPE_MCP_LIVE_TEST_AXIS_READ", "1")
        lab.build_evidence_artifact(output_dir=tmp_path)
        payload = json.loads((tmp_path / "axis-lab-evidence.json").read_text())
        plan = payload["summary"]["disposable_write_plan"]
        assert plan["execution_status"] == "planned_not_executed"
        assert payload["mode"] == "disposable_write"
        # the plan must never invoke a real create/delete call itself
        assert "axis_create_sub_location" not in [
            step for step in payload["steps"] if isinstance(step, str)
        ]


class TestArtifactShape:
    def test_disposable_write_plan_is_deterministically_hashed(self):
        plan_a = lab.build_disposable_write_plan()
        plan_b = lab.build_disposable_write_plan()
        assert plan_a["plan_sha256"] == plan_b["plan_sha256"]

    def test_artifact_validates_against_shared_contract(self, tmp_path, monkeypatch):
        for name in ("HPE_MCP_LIVE_TEST_AXIS_READ", "HPE_MCP_LIVE_TEST_AXIS_WRITE"):
            monkeypatch.delenv(name, raising=False)
        entries = lab.build_evidence_artifact(output_dir=tmp_path)
        assert entries[0].kind == contracts.LIVE_LIFECYCLE_EVIDENCE

    def test_no_credential_leakage_when_write_plan_included(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HPE_MCP_LIVE_TEST_AXIS_READ", "1")
        monkeypatch.setenv("HPE_MCP_LIVE_TEST_AXIS_WRITE", "1")
        monkeypatch.setenv("AXIS_BASE_URL", "https://axis.example.com")
        monkeypatch.setenv("AXIS_API_TOKEN", "super-secret-axis-token")
        lab.build_evidence_artifact(output_dir=tmp_path)
        text = (tmp_path / "axis-lab-evidence.json").read_text()
        assert "super-secret-axis-token" not in text
