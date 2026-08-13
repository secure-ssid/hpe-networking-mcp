"""Regression tests for the GLP MCP server surface touched by the audit.

Covers:
- ``glp_write_status`` reporting the resolved GLP gate and its independence
  from Central's gate.
- Blocked-write responses naming only the GLP flag.
- Server-side ``dry_run`` plumbed through to the manifest-declared
  ``dry-run`` query parameter on the device PATCH tools.
- Audit-log tools forwarding the documented filter/select/sort parameters.

No network calls, no writes.
"""

from __future__ import annotations

import pytest

import hpe_networking_mcp.mcp_servers.glp as glp
from hpe_networking_mcp.pipeline.clients.glp_client import _V2BETA1_WRITES_FLAG


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(_V2BETA1_WRITES_FLAG, raising=False)
    monkeypatch.delenv("HPE_MCP_CENTRAL_WRITES", raising=False)


class _DummyGLP:
    def __init__(self):
        self.calls = []

    def archive_device(self, serial_number, dry_run=False):
        self.calls.append(("archive", serial_number, dry_run))
        return {"status": "dry_run" if dry_run else "SUCCEEDED"}

    def assign_subscription(self, serial_number, subscription_id, dry_run=False):
        self.calls.append(("assign", serial_number, subscription_id, dry_run))
        return {"status": "dry_run" if dry_run else "SUCCEEDED"}

    def list_audit_logs_page(self, limit=100, offset=0, category=None, filter=None, select=None, sort=None):
        self.calls.append(("audit", limit, offset, category, filter, select, sort))
        return {"items": []}

    def list_audit_logs_v2beta1(
        self, limit=100, offset=0, category=None, filter=None, select=None, sort=None
    ):
        self.calls.append(("audit_v2", limit, offset, category, filter, select, sort))
        return []


class TestWriteStatus:
    def test_reports_disabled_gate_and_independence(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")

        status = glp.glp_write_status()

        assert status["enabled"] is False
        assert status["flag"] == _V2BETA1_WRITES_FLAG
        assert status["gate_state"] == "disabled"
        assert "HPE_MCP_CENTRAL_WRITES" in status["independent_of"]
        assert "no effect on GLP writes" in status["message"]

    def test_reports_enabled_gate(self, monkeypatch):
        monkeypatch.setenv(_V2BETA1_WRITES_FLAG, "1")
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "0")

        status = glp.glp_write_status()

        assert status["enabled"] is True
        assert status["gate_state"] == "enabled"
        assert status["gate_source"] == "platform_override"

    def test_blocked_response_names_only_the_glp_flag(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "0")

        result = glp.glp_archive_device("SERIAL1")

        assert result["status"] == "FORBIDDEN"
        assert _V2BETA1_WRITES_FLAG in result["error"]
        assert "independent of HPE_MCP_CENTRAL_WRITES" in result["error"]
        assert result["platform"] == "glp"


class TestDryRunPlumbing:
    def test_archive_forwards_dry_run(self, monkeypatch):
        monkeypatch.setenv(_V2BETA1_WRITES_FLAG, "1")
        dummy = _DummyGLP()
        monkeypatch.setattr(glp, "get_glp_client", lambda: dummy)

        result = glp.glp_archive_device("SERIAL1", dry_run=True)

        assert dummy.calls == [("archive", "SERIAL1", True)]
        assert result["result"]["status"] == "dry_run"
        assert result["errors"] == []

    def test_archive_defaults_to_a_real_write(self, monkeypatch):
        monkeypatch.setenv(_V2BETA1_WRITES_FLAG, "1")
        dummy = _DummyGLP()
        monkeypatch.setattr(glp, "get_glp_client", lambda: dummy)

        glp.glp_archive_device("SERIAL1")

        assert dummy.calls == [("archive", "SERIAL1", False)]

    def test_assign_subscription_forwards_dry_run(self, monkeypatch):
        monkeypatch.setenv(_V2BETA1_WRITES_FLAG, "1")
        dummy = _DummyGLP()
        monkeypatch.setattr(glp, "get_glp_client", lambda: dummy)

        glp.glp_assign_subscription("SERIAL1", "SUBKEY", dry_run=True)

        assert dummy.calls == [("assign", "SERIAL1", "SUBKEY", True)]

    def test_dry_run_still_blocked_when_the_flag_is_off(self):
        result = glp.glp_archive_device("SERIAL1", dry_run=True)

        assert result["would_have_sent"]["dry_run"] is True
        assert _V2BETA1_WRITES_FLAG in result["error"]


class TestAuditToolParameters:
    def test_list_forwards_filter_select_sort(self, monkeypatch):
        dummy = _DummyGLP()
        monkeypatch.setattr(glp, "get_glp_client", lambda: dummy)

        glp.list_glp_audit_logs(
            limit=10,
            offset=5,
            category="User Management",
            filter="region eq 'us-west'",
            select="createdAt",
            sort="createdAt desc",
        )

        assert dummy.calls == [
            ("audit", 10, 5, "User Management", "region eq 'us-west'", "createdAt", "createdAt desc")
        ]

    def test_v2_tool_forwards_the_same_parameters(self, monkeypatch):
        dummy = _DummyGLP()
        monkeypatch.setattr(glp, "get_glp_client", lambda: dummy)

        glp.list_glp_audit_logs_v2(limit=10, offset=0, filter="hasDetails eq 'true'")

        assert dummy.calls == [("audit_v2", 10, 0, None, "hasDetails eq 'true'", None, None)]

    def test_detail_tool_uses_the_manifest_path(self, monkeypatch):
        seen = []

        class _Central:
            def get(self, path, params=None):
                seen.append(path)
                return {}

        class _Wrapper:
            _client = _Central()

        monkeypatch.setattr(glp, "get_glp_client", lambda: _Wrapper())

        glp.get_glp_audit_log_detail("a 1")

        assert seen == ["/audit-log/v2beta1/logs/a%201/details"]
