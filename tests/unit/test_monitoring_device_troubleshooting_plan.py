"""Unit tests for the read-only per-device troubleshooting planner."""

from __future__ import annotations

import pytest

from hpe_networking_mcp.mcp_servers import monitoring


def test_plan_device_troubleshooting_rejects_empty_serial():
    with pytest.raises(ValueError, match="serial_number"):
        monitoring.plan_device_troubleshooting("  ")


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"hours": 0}, "hours"),
        ({"hours": 169}, "hours"),
        ({"max_alerts": 0}, "max_alerts"),
        ({"max_alerts": 51}, "max_alerts"),
        ({"max_events": 0}, "max_events"),
        ({"max_events": 51}, "max_events"),
    ],
)
def test_plan_device_troubleshooting_rejects_out_of_bounds(kwargs, match):
    with pytest.raises(ValueError, match=match):
        monitoring.plan_device_troubleshooting("CN1", **kwargs)


def test_plan_device_troubleshooting_maps_switch_poe_without_executing(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(
        monitoring,
        "find_device",
        lambda serial: {
            "serialNumber": serial,
            "deviceType": "ACCESS_SWITCH",
            "status": "UP",
            "siteId": "site-1",
            "model": "Aruba 6300 CX",
        },
    )
    monkeypatch.setattr(
        monitoring,
        "get_device_health",
        lambda serial: {
            "serial_number": serial,
            "health": [{"serial": serial, "configStatus": "SYNCHRONIZED"}],
            "endpoint_used": "/network-config/v1alpha1/config-health/devices",
        },
    )
    monkeypatch.setattr(monitoring, "get_device_config_issues", lambda serial: {"items": []})
    monkeypatch.setattr(
        monitoring,
        "list_events",
        lambda serial, hours=24, limit=20: {
            "items": [
                {
                    "eventName": "PoE",
                    "description": "PoE denied on interface 1/1/4",
                    "timeAt": "2026-04-07T00:00:00Z",
                }
            ]
        },
    )
    monkeypatch.setattr(
        monitoring,
        "list_active_alerts",
        lambda site_id=None, limit=20: {
            "items": [
                {
                    "key": "a-other",
                    "serialNumber": "OTHER",
                    "name": "PoE overload",
                    "severity": "MAJOR",
                },
                {
                    "key": "a-1",
                    "serialNumber": "CN1",
                    "name": "PoE overload",
                    "severity": "MAJOR",
                    "status": "Active",
                },
            ]
        },
    )

    def _fail_write(*_args, **_kwargs):
        calls.append("write")
        raise AssertionError("planner must not execute writes")

    monkeypatch.setattr(monitoring, "execute_config_health_remediation", _fail_write)
    monkeypatch.setattr(monitoring, "resync_device_config", _fail_write)

    result = monitoring.plan_device_troubleshooting("CN1")

    assert result["device_family"] == "switch"
    assert result["site_id"] == "site-1"
    assert [alert["key"] for alert in result["alerts"]] == ["a-1"]
    read_names = [item["name"] for item in result["recommended_reads"]]
    assert "list_switch_ports" in read_names
    diagnostic_names = [item["name"] for item in result["recommended_diagnostics"]]
    assert "run_troubleshooting_bundle" in diagnostic_names
    assert "cable_test" in diagnostic_names
    destructive = {item["name"]: item for item in result["recommended_destructive"]}
    assert destructive["poe_bounce"]["execute"] is False
    assert destructive["poe_bounce"]["requires_confirmation"] is True
    assert result["recommended_writes"] == []
    assert calls == []
    bundle = next(
        item
        for item in result["recommended_diagnostics"]
        if item["name"] == "run_troubleshooting_bundle"
    )
    assert bundle["arguments"]["device_type"] == "cx"


def test_plan_device_troubleshooting_recommends_resync_and_last_resort_reboot(monkeypatch):
    monkeypatch.setattr(
        monitoring,
        "find_device",
        lambda serial: {
            "serialNumber": serial,
            "deviceType": "ACCESS_POINT",
            "status": "DOWN",
            "siteId": "site-ap",
        },
    )
    monkeypatch.setattr(
        monitoring,
        "get_device_health",
        lambda serial: {
            "serial_number": serial,
            "health": [{"serial": serial, "configStatus": "OUT_OF_SYNC"}],
            "endpoint_used": "/network-config/v1alpha1/config-health/devices",
        },
    )
    monkeypatch.setattr(
        monitoring,
        "get_device_config_issues",
        lambda serial: {"items": [{"code": "CFG_DRIFT"}]},
    )
    monkeypatch.setattr(
        monitoring,
        "list_events",
        lambda serial, hours=24, limit=20: {
            "items": [{"eventName": "Device Down", "description": "AP offline"}]
        },
    )
    monkeypatch.setattr(monitoring, "list_active_alerts", lambda **kwargs: {"items": []})

    result = monitoring.plan_device_troubleshooting("AP1")

    assert result["device_family"] == "ap"
    assert result["config_issue_count"] == 1
    write = result["recommended_writes"][0]
    assert write["name"] == "execute_config_health_remediation"
    assert write["arguments"]["dry_run"] is True
    assert write["execute"] is False
    reboot = next(
        item for item in result["recommended_destructive"] if item["name"] == "reboot_device"
    )
    assert reboot["execute"] is False
    assert reboot["requires_confirmation"] is True
    read_names = [item["name"] for item in result["recommended_reads"]]
    assert read_names[0] == "get_device_health"
    assert "get_wireless_metrics" in read_names


def test_plan_device_troubleshooting_continues_when_sources_fail(monkeypatch):
    monkeypatch.setattr(
        monitoring,
        "find_device",
        lambda serial: (_ for _ in ()).throw(RuntimeError("inventory down")),
    )
    monkeypatch.setattr(
        monitoring,
        "get_device_health",
        lambda serial: (_ for _ in ()).throw(RuntimeError("health down")),
    )
    monkeypatch.setattr(
        monitoring,
        "get_device_config_issues",
        lambda serial: (_ for _ in ()).throw(RuntimeError("issues down")),
    )
    monkeypatch.setattr(
        monitoring,
        "list_events",
        lambda serial, hours=24, limit=20: (_ for _ in ()).throw(RuntimeError("events down")),
    )
    monkeypatch.setattr(
        monitoring,
        "list_active_alerts",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("alerts down")),
    )

    result = monitoring.plan_device_troubleshooting("CN9")

    assert result["serial_number"] == "CN9"
    assert result["device_family"] == "unknown"
    assert len(result["errors"]) == 5
    assert result["recommended_reads"][0]["name"] == "get_device_health"
    assert result["recommended_writes"] == []
    assert result["recommended_destructive"] == []
