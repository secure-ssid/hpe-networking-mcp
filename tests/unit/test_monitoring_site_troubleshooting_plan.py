"""Unit tests for the read-only per-site troubleshooting planner."""

from __future__ import annotations

import pytest

from hpe_networking_mcp.mcp_servers import monitoring


def test_plan_site_troubleshooting_requires_site():
    with pytest.raises(ValueError, match="site_id or site_name"):
        monitoring.plan_site_troubleshooting()


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"max_devices": 0}, "max_devices"),
        ({"max_devices": 11}, "max_devices"),
        ({"max_alerts": 0}, "max_alerts"),
        ({"max_alerts": 51}, "max_alerts"),
    ],
)
def test_plan_site_troubleshooting_rejects_out_of_bounds(kwargs, match):
    with pytest.raises(ValueError, match=match):
        monitoring.plan_site_troubleshooting(site_id="site-1", **kwargs)


def test_plan_site_troubleshooting_ranks_offline_and_alerted_devices(monkeypatch):
    monkeypatch.setattr(
        monitoring,
        "get_site_health_summary",
        lambda site_id=None, site_name=None: {
            "site": "Lab",
            "site_id": "site-1",
            "devices": {"total": 3, "by_status": {"UP": 2, "DOWN": 1}},
            "alerts": {"total": 2, "by_severity": {"CRITICAL": 1, "MAJOR": 1}},
        },
    )
    monkeypatch.setattr(
        monitoring,
        "list_devices",
        lambda **kwargs: [
            {"serialNumber": "AP-UP", "deviceType": "ACCESS_POINT", "status": "UP"},
            {"serialNumber": "SW-DOWN", "deviceType": "ACCESS_SWITCH", "status": "DOWN"},
            {"serialNumber": "AP-ALERT", "deviceType": "ACCESS_POINT", "status": "UP"},
        ],
    )
    monkeypatch.setattr(
        monitoring,
        "list_active_alerts",
        lambda **kwargs: {
            "items": [
                {
                    "key": "a-crit",
                    "serialNumber": "AP-ALERT",
                    "severity": "CRITICAL",
                    "name": "Radio down",
                },
                {
                    "key": "a-other",
                    "serialNumber": "OTHER",
                    "severity": "CRITICAL",
                    "name": "Ignore me",
                },
            ]
        },
    )

    def _fail_device_plan(*_args, **_kwargs):
        raise AssertionError("nested device plans are opt-in")

    monkeypatch.setattr(monitoring, "plan_device_troubleshooting", _fail_device_plan)

    result = monitoring.plan_site_troubleshooting(site_name="Lab")

    assert result["site_id"] == "site-1"
    serials = [item["serial_number"] for item in result["priority_devices"]]
    assert serials == ["SW-DOWN", "AP-ALERT"]
    assert result["priority_devices"][0]["score"] == 100
    assert result["priority_devices"][1]["score"] == 50
    assert result["device_plans"] == []
    assert result["priority_devices"][0]["next_tool"]["name"] == "plan_device_troubleshooting"
    assert result["priority_devices"][0]["next_tool"]["execute"] is False


def test_plan_site_troubleshooting_can_attach_nested_device_plans(monkeypatch):
    monkeypatch.setattr(
        monitoring,
        "get_site_health_summary",
        lambda **kwargs: {
            "site": "Lab",
            "site_id": "site-1",
            "devices": {"total": 1},
            "alerts": {"total": 0},
        },
    )
    monkeypatch.setattr(
        monitoring,
        "list_devices",
        lambda **kwargs: [
            {"serialNumber": "SW-DOWN", "deviceType": "ACCESS_SWITCH", "status": "DOWN"}
        ],
    )
    monkeypatch.setattr(monitoring, "list_active_alerts", lambda **kwargs: {"items": []})
    monkeypatch.setattr(
        monitoring,
        "plan_device_troubleshooting",
        lambda serial, **kwargs: {
            "serial_number": serial,
            "recommended_destructive": [
                {"name": "reboot_device", "execute": False, "requires_confirmation": True}
            ],
        },
    )

    result = monitoring.plan_site_troubleshooting(site_id="site-1", include_device_plans=True)

    assert len(result["device_plans"]) == 1
    nested = result["device_plans"][0]["plan"]["recommended_destructive"][0]
    assert nested["execute"] is False


def test_plan_site_troubleshooting_returns_site_lookup_error(monkeypatch):
    monkeypatch.setattr(
        monitoring,
        "get_site_health_summary",
        lambda **kwargs: {"error": "Site not found: Missing"},
    )
    result = monitoring.plan_site_troubleshooting(site_name="Missing")
    assert result["error"] == "Site not found: Missing"
    assert result["priority_devices"] == []
