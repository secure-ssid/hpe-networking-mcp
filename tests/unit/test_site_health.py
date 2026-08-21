from __future__ import annotations

import asyncio

import hpe_networking_mcp.mcp_servers.site_health as site_health


def test_site_health_composes_existing_platform_reads(monkeypatch):
    central_calls = []
    mist_calls = []

    def central_read(*, site_id, site_name):
        central_calls.append((site_id, site_name))
        return {"site_id": site_id, "devices": {"total": 2}, "errors": []}

    async def mist_read(site_id, *, limit):
        mist_calls.append((site_id, limit))
        return {
            "site_id": site_id,
            "sections": {"alarms": {"alarms": {"items": []}}},
            "degraded": False,
        }

    monkeypatch.setattr(site_health.monitoring, "get_site_health_summary", central_read)
    monkeypatch.setattr(site_health.mist, "mist_status", lambda: {"configured": True})
    monkeypatch.setattr(
        site_health.mist, "mist_get_site_assurance_snapshot", mist_read
    )

    result = asyncio.run(
        site_health.get_site_health(
            central_site_id="central-1", mist_site_id="mist-1", limit=999
        )
    )

    assert result["status"] == "available"
    assert result["platforms"]["central"]["health"]["site_id"] == "central-1"
    assert result["platforms"]["mist"]["health"]["site_id"] == "mist-1"
    assert central_calls == [("central-1", None)]
    assert mist_calls == [("mist-1", 200)]


def test_site_health_reports_unconfigured_mist_without_health(monkeypatch):
    monkeypatch.setattr(site_health.mist, "mist_status", lambda: {"configured": False})
    monkeypatch.setattr(
        site_health.monitoring,
        "get_site_health_summary",
        lambda **kwargs: {"site_id": kwargs["site_id"], "errors": []},
    )

    result = asyncio.run(
        site_health.get_site_health(central_site_id="central-1", mist_site_id="mist-1")
    )

    mist_result = result["platforms"]["mist"]
    assert result["status"] == "degraded"
    assert mist_result["status"] == "unavailable"
    assert mist_result["health"] is None
    assert any("not configured" in error for error in mist_result["errors"])


def test_site_health_does_not_infer_cross_platform_site_mapping(monkeypatch):
    monkeypatch.setattr(site_health.mist, "mist_status", lambda: {"configured": True})
    monkeypatch.setattr(
        site_health.monitoring,
        "get_site_health_summary",
        lambda **kwargs: {"site_id": kwargs["site_id"], "errors": []},
    )

    result = asyncio.run(site_health.get_site_health(central_site_id="central-1"))

    assert result["platforms"]["mist"]["status"] == "not_requested"
    assert result["platforms"]["mist"]["health"] is None
    assert any("mapping is inferred" in warning for warning in result["warnings"])


def test_site_health_requires_at_least_one_site_identifier():
    result = asyncio.run(site_health.get_site_health())

    assert result["status"] == "unavailable"
    assert result["platforms"]["central"]["health"] is None
    assert result["platforms"]["mist"]["health"] is None
    assert result["errors"]


def test_site_health_does_not_treat_failed_mist_http_sections_as_health():
    result = site_health._mist_result(
        {
            "site_id": "mist-1",
            "sections": {
                "switches": {"status_code": 404},
                "gateways": {"status_code": 503},
            },
        }
    )

    assert result["status"] == "unavailable"
    assert result["health"] is None
    assert any("HTTP 404" in error for error in result["errors"])
