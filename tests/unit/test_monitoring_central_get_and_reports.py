from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from hpe_networking_mcp.mcp_servers import monitoring


def _response(status_code: int = 200, payload: dict | None = None, errors=None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    body = dict(payload or {})
    if errors is not None:
        body["errors"] = errors
    response.json.side_effect = lambda: dict(body)
    response.text = "{}"
    return response


class _AcceptCtx:
    async def elicit(self, message, schema):
        return SimpleNamespace(action="accept", data=schema(confirm=True))


class _DeclineCtx:
    async def elicit(self, message, schema):
        return SimpleNamespace(action="decline", data=schema(confirm=False))


# ---------------------------------------------------------------------------
# central_get
# ---------------------------------------------------------------------------


def test_central_get_rejects_unsupported_prefix():
    result = monitoring.central_get("/network-config/v1alpha1/overlay-wlan/foo")

    assert "path must begin" in result["error"]


def test_central_get_rejects_dot_segments():
    result = monitoring.central_get("/network-monitoring/v1/../secrets")

    assert "dot segments" in result["error"]


def test_central_get_rejects_absolute_url():
    result = monitoring.central_get("https://evil.example/network-monitoring/v1/aps")

    assert "Invalid path" in result["error"]


def test_central_get_calls_guarded_path(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"path": "/network-troubleshooting/v1/foo"}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.central_get("/network-troubleshooting/v1/foo", {"a": 1})

    assert result == {
        "data": {"path": "/network-troubleshooting/v1/foo"},
        "endpoint_used": "/network-troubleshooting/v1/foo",
    }
    client.get.assert_called_once_with("/network-troubleshooting/v1/foo", params={"a": 1})


def test_central_get_bounds_list_payloads(monkeypatch):
    client = MagicMock()
    client.get.return_value = [{"id": 1}, {"id": 2}, {"id": 3}]
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.central_get("/network-services/v1/webhooks", limit=2, offset=1)

    assert result["data"]["items"] == [{"id": 2}, {"id": 3}]
    assert result["data"]["_pagination"]["total"] == 3


def test_central_get_surfaces_client_errors(monkeypatch):
    client = MagicMock()
    client.get.side_effect = RuntimeError("boom")
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.central_get("/network-monitoring/v1/aps")

    assert result["error"] == "boom"
    assert result["endpoint_used"] == "/network-monitoring/v1/aps"


def test_list_central_get_prefixes_reports_all_registries():
    result = monitoring.list_central_get_prefixes()

    assert set(result["guarded_get_prefixes"]) == {
        "/network-monitoring/v1/",
        "/network-notifications/v1/",
        "/network-reporting/v1/",
        "/network-services/v1/",
        "/network-troubleshooting/v1/",
    }
    assert result["manifest_registries"] == [
        "Monitoring",
        "Notifications",
        "Reporting",
        "Services",
        "Troubleshooting",
    ]


# ---------------------------------------------------------------------------
# Report lifecycle (manifest-confirmed)
# ---------------------------------------------------------------------------


def test_get_report_calls_expected_endpoint(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"id": "report-1", "name": "My Report"}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.get_report("report-1")

    assert result["id"] == "report-1"
    assert result["endpoint_used"] == "/network-reporting/v1/reports/report-1"
    client.get.assert_called_once_with("/network-reporting/v1/reports/report-1")


def test_get_report_surfaces_errors(monkeypatch):
    client = MagicMock()
    client.get.side_effect = RuntimeError("not found")
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.get_report("missing")

    assert result["error"] == "not found"


def test_create_report_dry_run_returns_payload_without_sending(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(
        monitoring.create_report(_AcceptCtx(), {"name": "r1"}, dry_run=True)
    )

    assert result == {
        "dry_run": True,
        "endpoint": "/network-reporting/v1/reports",
        "payload": {"name": "r1"},
    }
    client._arequest.assert_not_called()


def test_create_report_requires_confirmation(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(monitoring.create_report(_DeclineCtx(), {"name": "r1"}))

    assert result["status"] == "CANCELLED"
    client._arequest.assert_not_called()


def test_create_report_posts_when_confirmed(monkeypatch):
    client = MagicMock()
    client._arequest = AsyncMock(
        return_value=_response(status_code=201, payload={"id": "report-1"})
    )
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(monitoring.create_report(_AcceptCtx(), {"name": "r1"}))

    assert result["id"] == "report-1"
    assert result["endpoint_used"] == "/network-reporting/v1/reports"
    client._arequest.assert_awaited_once_with(
        "POST", "/network-reporting/v1/reports", json={"name": "r1"}
    )


def test_create_report_fails_closed_on_error_status(monkeypatch):
    client = MagicMock()
    client._arequest = AsyncMock(
        return_value=_response(status_code=409, payload={"message": "duplicate"})
    )
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(monitoring.create_report(_AcceptCtx(), {"name": "r1"}))

    assert "error" in result
    assert "409" in result["error"]


def test_create_report_fails_closed_on_error_envelope(monkeypatch):
    client = MagicMock()
    client._arequest = AsyncMock(
        return_value=_response(status_code=200, payload={"id": "r1"}, errors=["bad filter"])
    )
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(monitoring.create_report(_AcceptCtx(), {"name": "r1"}))

    assert "error" in result
    assert "bad filter" in result["error"]


def test_update_report_dry_run(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(
        monitoring.update_report(_AcceptCtx(), "report-1", {"name": "renamed"}, dry_run=True)
    )

    assert result["dry_run"] is True
    client._arequest.assert_not_called()


def test_update_report_puts_when_confirmed(monkeypatch):
    client = MagicMock()
    client._arequest = AsyncMock(
        return_value=_response(status_code=200, payload={"id": "report-1", "name": "renamed"})
    )
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(
        monitoring.update_report(_AcceptCtx(), "report-1", {"name": "renamed"})
    )

    assert result["name"] == "renamed"
    client._arequest.assert_awaited_once_with(
        "PUT", "/network-reporting/v1/reports/report-1", json={"name": "renamed"}
    )


def test_delete_report_dry_run(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(monitoring.delete_report(_AcceptCtx(), "report-1", dry_run=True))

    assert result["dry_run"] is True
    client._arequest.assert_not_called()


def test_delete_report_requires_confirmation(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(monitoring.delete_report(_DeclineCtx(), "report-1"))

    assert result["status"] == "CANCELLED"
    client._arequest.assert_not_called()


def test_delete_report_deletes_when_confirmed(monkeypatch):
    client = MagicMock()
    client._arequest = AsyncMock(return_value=_response(status_code=200, payload={}))
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(monitoring.delete_report(_AcceptCtx(), "report-1"))

    assert result["deleted"] is True
    client._arequest.assert_awaited_once_with(
        "DELETE", "/network-reporting/v1/reports/report-1"
    )


def test_delete_report_fails_closed_without_deleted_flag(monkeypatch):
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 404
    resp.json.side_effect = ValueError()
    resp.text = "not found"
    client._arequest = AsyncMock(return_value=resp)
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(monitoring.delete_report(_AcceptCtx(), "missing"))

    assert "error" in result
    assert "deleted" not in result


def test_delete_report_run_deletes_when_confirmed(monkeypatch):
    client = MagicMock()
    client._arequest = AsyncMock(return_value=_response(status_code=200, payload={}))
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(
        monitoring.delete_report_run(_AcceptCtx(), "report-1", "run-1")
    )

    assert result["deleted"] is True
    client._arequest.assert_awaited_once_with(
        "DELETE", "/network-reporting/v1/reports/report-1/report-runs/run-1"
    )


def test_delete_report_run_dry_run(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(
        monitoring.delete_report_run(_AcceptCtx(), "report-1", "run-1", dry_run=True)
    )

    assert result["dry_run"] is True
    client._arequest.assert_not_called()


def test_get_report_run_download_link_rejects_invalid_export_type(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(
        monitoring.get_report_run_download_link(
            _AcceptCtx(), "report-1", "run-1", export_type="ZIP"
        )
    )

    assert "error" in result
    client._arequest.assert_not_called()


def test_get_report_run_download_link_dry_run(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(
        monitoring.get_report_run_download_link(
            _AcceptCtx(), "report-1", "run-1", export_type="csv", dry_run=True
        )
    )

    assert result["dry_run"] is True
    assert result["payload"] == {"exportType": "CSV"}
    client._arequest.assert_not_called()


def test_get_report_run_download_link_requires_confirmation(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(
        monitoring.get_report_run_download_link(_DeclineCtx(), "report-1", "run-1")
    )

    assert result["status"] == "CANCELLED"
    client._arequest.assert_not_called()


def test_get_report_run_download_link_posts_when_confirmed(monkeypatch):
    client = MagicMock()
    client._arequest = AsyncMock(
        return_value=_response(
            status_code=200,
            payload={"url": "https://example.test/download", "mimeType": "application/zip"},
        )
    )
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(
        monitoring.get_report_run_download_link(
            _AcceptCtx(), "report-1", "run-1", export_type="CSV"
        )
    )

    assert result["url"] == "https://example.test/download"
    client._arequest.assert_awaited_once_with(
        "POST",
        "/network-reporting/v1/reports/report-1/report-runs/run-1/download-link",
        json={"exportType": "CSV"},
    )
