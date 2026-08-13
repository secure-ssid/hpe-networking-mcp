"""Unit tests for the v0.7 config-health remediation workflow added to
src/hpe_networking_mcp/mcp_servers/monitoring.py: plan_config_health_remediation (read-only
planning) and execute_config_health_remediation (chunked resync +
per-chunk read-back, partial-failure safe), plus the new schema-max bound
on resync_device_config.
"""

from __future__ import annotations

import pytest

from hpe_networking_mcp.mcp_servers import monitoring


@pytest.fixture(autouse=True)
def _central_writes_enabled(monkeypatch):
    monkeypatch.delenv("HPE_MCP_CENTRAL_WRITES", raising=False)


# ---------------------------------------------------------------------------
# resync_device_config bound (schema max 50)
# ---------------------------------------------------------------------------


def test_resync_device_config_rejects_over_schema_max(monkeypatch):
    monkeypatch.setattr(
        monitoring, "get_client", lambda: (_ for _ in ()).throw(AssertionError("must not call"))
    )
    serials = [f"CN{i:05d}" for i in range(monitoring._CONFIG_HEALTH_RESYNC_MAX_PER_CALL + 1)]
    with pytest.raises(ValueError, match="cannot exceed 50"):
        monitoring.resync_device_config(serials)


def test_resync_device_config_within_bound_posts(monkeypatch):
    class FakeClient:
        def post(self, endpoint, data=None):
            calls.append((endpoint, data))
            return {"message": "Full configuration sync triggered for 2 devices."}

    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(monitoring, "get_client", lambda: FakeClient())
    result = monitoring.resync_device_config(["CN1", "CN2"])
    assert result["message"].startswith("Full configuration sync")
    assert calls[0][0] == "/network-config/v1alpha1/config-health/devices-resync"


# ---------------------------------------------------------------------------
# plan_config_health_remediation
# ---------------------------------------------------------------------------


def test_plan_config_health_remediation_skips_healthy_devices(monkeypatch):
    summary = {
        "devices": [
            {"serial": "CN-HEALTHY", "configStatus": "SYNCHRONIZED"},
            {"serial": "CN-BAD1", "configStatus": "OUT_OF_SYNC"},
            {"serial": "CN-BAD2", "configStatus": "UNKNOWN"},
        ]
    }
    monkeypatch.setattr(monitoring, "list_devices_config_health", lambda **kw: summary)
    monkeypatch.setattr(
        monitoring, "get_device_config_issues", lambda serial: {"serial": serial, "issues": []}
    )

    result = monitoring.plan_config_health_remediation()

    assert result["scanned"] == 3
    assert result["unhealthy_count"] == 2
    serials = [p["serial_number"] for p in result["plan"]]
    assert serials == ["CN-BAD1", "CN-BAD2"]
    assert all(p["recommended_action"] == "resync_device_config" for p in result["plan"])
    assert "execute_config_health_remediation" in result["next_step"]


def test_plan_config_health_remediation_bounds_scan(monkeypatch):
    with pytest.raises(ValueError, match="max_devices_scanned"):
        monitoring.plan_config_health_remediation(max_devices_scanned=0)
    with pytest.raises(ValueError, match="max_devices_scanned"):
        monitoring.plan_config_health_remediation(max_devices_scanned=500)


def test_plan_config_health_remediation_respects_max_devices_scanned(monkeypatch):
    summary = {
        "devices": [
            {"serial": f"CN{i}", "configStatus": "OUT_OF_SYNC"}
            for i in range(10)
        ]
    }
    monkeypatch.setattr(monitoring, "list_devices_config_health", lambda **kw: summary)
    monkeypatch.setattr(
        monitoring, "get_device_config_issues", lambda serial: {"serial": serial}
    )

    result = monitoring.plan_config_health_remediation(max_devices_scanned=3)

    assert result["unhealthy_count"] == 3
    assert len(result["plan"]) == 3


def test_plan_config_health_remediation_captures_issue_lookup_errors(monkeypatch):
    summary = {"devices": [{"serial": "CN-BAD", "configStatus": "OUT_OF_SYNC"}]}
    monkeypatch.setattr(monitoring, "list_devices_config_health", lambda **kw: summary)

    def _raise(serial):
        raise RuntimeError("upstream 500")

    monkeypatch.setattr(monitoring, "get_device_config_issues", _raise)

    result = monitoring.plan_config_health_remediation()

    assert result["plan"][0]["issues"] is None
    assert "CN-BAD" in result["errors"][0]


# ---------------------------------------------------------------------------
# execute_config_health_remediation
# ---------------------------------------------------------------------------


def test_execute_remediation_dry_run_chunks_without_network(monkeypatch):
    monkeypatch.setattr(
        monitoring, "get_client", lambda: (_ for _ in ()).throw(AssertionError("must not call"))
    )
    serials = [f"CN{i}" for i in range(75)]
    result = monitoring.execute_config_health_remediation(serials, dry_run=True)
    assert result["dry_run"] is True
    assert result["chunk_count"] == 2
    assert len(result["chunks"][0]) == 50
    assert len(result["chunks"][1]) == 25


def test_execute_remediation_requires_confirm(monkeypatch):
    monkeypatch.setattr(
        monitoring, "get_client", lambda: (_ for _ in ()).throw(AssertionError("must not call"))
    )
    result = monitoring.execute_config_health_remediation(
        ["CN1"], dry_run=False, confirm=False
    )
    assert "confirm=True is required" in result["error"]


def test_execute_remediation_blocked_when_writes_disabled(monkeypatch):
    monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "0")
    result = monitoring.execute_config_health_remediation(
        ["CN1"], dry_run=False, confirm=True
    )
    assert result["status"] == "blocked"


def test_execute_remediation_bounds_total_serials():
    serials = [f"CN{i}" for i in range(monitoring._MAX_REMEDIATION_SERIALS + 1)]
    with pytest.raises(ValueError, match="cannot exceed 200"):
        monitoring.execute_config_health_remediation(serials, dry_run=True)


def test_execute_remediation_partial_failure_does_not_abort_other_chunks(monkeypatch):
    """First chunk's resync raises; second chunk still runs and is read back."""
    calls: list[list[str]] = []

    def fake_resync(chunk):
        calls.append(list(chunk))
        if chunk[0] == "CHUNK1-0":
            raise RuntimeError("resync failed")
        return {"message": "ok"}

    monkeypatch.setattr(monitoring, "resync_device_config", fake_resync)
    monkeypatch.setattr(
        monitoring, "get_device_config_issues", lambda serial: {"serial": serial, "status": "OK"}
    )

    chunk1 = [f"CHUNK1-{i}" for i in range(50)]
    chunk2 = [f"CHUNK2-{i}" for i in range(10)]
    result = monitoring.execute_config_health_remediation(
        chunk1 + chunk2, dry_run=False, confirm=True
    )

    assert result["chunks_attempted"] == 2
    assert result["chunks_succeeded"] == 1
    assert result["chunks_failed"] == 1
    failed_entry = next(r for r in result["results"] if "error" in r)
    succeeded_entry = next(r for r in result["results"] if "error" not in r)
    assert failed_entry["serials"] == chunk1
    assert succeeded_entry["serials"] == chunk2
    assert len(succeeded_entry["read_back"]) == 10
    # both chunks must have been attempted despite the first one failing
    assert len(calls) == 2


def test_execute_remediation_captures_read_back_errors_per_serial(monkeypatch):
    monkeypatch.setattr(monitoring, "resync_device_config", lambda chunk: {"message": "ok"})

    def flaky_issues(serial):
        if serial == "CN-BAD":
            raise RuntimeError("timeout")
        return {"serial": serial}

    monkeypatch.setattr(monitoring, "get_device_config_issues", flaky_issues)

    result = monitoring.execute_config_health_remediation(
        ["CN-OK", "CN-BAD"], dry_run=False, confirm=True
    )

    read_back = result["results"][0]["read_back"]
    assert read_back["CN-OK"] == {"serial": "CN-OK"}
    assert "error" in read_back["CN-BAD"]
