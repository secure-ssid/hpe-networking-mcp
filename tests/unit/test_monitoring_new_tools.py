from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from hpe_networking_mcp.mcp_servers import monitoring


def _response(status_code: int = 202, payload: dict | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.side_effect = lambda: dict(payload or {})
    response.text = "{}"
    return response


class _AcceptCtx:
    async def elicit(self, message, schema):
        return SimpleNamespace(action="accept", data=schema(confirm=True))


class _DeclineCtx:
    async def elicit(self, message, schema):
        return SimpleNamespace(action="decline", data=schema(confirm=False))


def test_list_active_alerts_calls_expected_endpoint(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"items": []}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.list_active_alerts(
        site_id="site-1",
        severity="CRITICAL",
        limit=25,
        offset=10,
    )

    assert result == {"items": []}
    client.get.assert_called_once_with(
        "/network-notifications/v1/alerts",
        params={
            "limit": 25,
            "next": "11",
            "filter": "status eq 'Active' and siteId eq 'site-1' and severity eq 'CRITICAL'",
            "sort": "severity desc",
        },
    )


def test_legacy_list_alerts_forwards_offset(monkeypatch):
    mcp_client = MagicMock()
    mcp_client.get_alerts.return_value = []
    monkeypatch.setattr(monitoring, "get_mcp_client", lambda: mcp_client)

    result = monitoring.list_alerts(severity="CRITICAL", limit=25, offset=-10)

    assert result == []
    mcp_client.get_alerts.assert_called_once_with(
        site_id=None,
        severity="CRITICAL",
        limit=25,
        offset=0,
        next_cursor=None,
    )


def test_list_clients_forwards_offset(monkeypatch):
    mcp_client = MagicMock()
    mcp_client.get_clients_page.return_value = ([], None)
    monkeypatch.setattr(monitoring, "get_mcp_client", lambda: mcp_client)

    result = monitoring.list_clients(site_id="site-1", limit=25, offset=10)

    assert result == []
    mcp_client.get_clients_page.assert_called_once_with(
        site_id="site-1",
        serial_number=None,
        ssid=None,
        connection_type=None,
        limit=25,
        next_cursor="11",
    )


def test_list_bssids_builds_structured_filters_and_cursor(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"items": []}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.list_bssids(
        site_id="site-1",
        site_name="Branch O'Hare",
        serial_number="AP123",
        mac_address="aa:bb:cc:dd:ee:ff",
        radio_mac_address="aa:bb:cc:dd:ee:00",
        sort="wlanName desc",
        limit=500,
        offset=9,
    )

    assert result == {"items": []}
    client.get.assert_called_once_with(
        "/network-monitoring/v1/bssids",
        params={
            "limit": 200,
            "next": "10",
            "filter": (
                "siteId eq 'site-1' and siteName eq 'Branch O''Hare' "
                "and serialNumber eq 'AP123' and macAddress eq 'aa:bb:cc:dd:ee:ff' "
                "and radioMacAddress eq 'aa:bb:cc:dd:ee:00'"
            ),
            "sort": "wlanName desc",
        },
    )


def test_list_bssids_combines_raw_filter_with_structured_filter(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"items": []}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    monitoring.list_bssids(
        site_id="site-1",
        filter="serialNumber in ('AP1','AP2')",
        next_cursor="opaque-cursor",
    )

    client.get.assert_called_once_with(
        "/network-monitoring/v1/bssids",
        params={
            "limit": 20,
            "next": "opaque-cursor",
            "filter": "serialNumber in ('AP1','AP2') and siteId eq 'site-1'",
        },
    )


def test_list_bssids_is_registered_read_only():
    tool = monitoring.mcp._tool_manager._tools["list_bssids"]

    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False


def test_list_sites_client_health_forwards_official_filters_and_sort(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"items": []}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.list_sites_client_health(
        site_id="site-1",
        site_name="Branch O'Hare",
        filter="siteId in ('site-1','site-2')",
        sort="wirelessClientHealth DESC",
        limit=500,
        offset=-10,
    )

    assert result == {"items": []}
    client.get.assert_called_once_with(
        "/network-monitoring/v1/sites-client-health",
        params={
            "limit": 200,
            "offset": 0,
            "filter": (
                "siteId in ('site-1','site-2') and siteId eq 'site-1' "
                "and siteName eq 'Branch O''Hare'"
            ),
            "sort": "wirelessClientHealth DESC",
        },
    )


def test_list_sites_client_health_omits_blank_optional_params(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"items": []}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    monitoring.list_sites_client_health(
        site_id=" ",
        site_name="",
        filter=" ",
        sort=" ",
        limit=0,
        offset=25,
    )

    client.get.assert_called_once_with(
        "/network-monitoring/v1/sites-client-health",
        params={"limit": 1, "offset": 25},
    )
    tool = monitoring.mcp._tool_manager._tools["list_sites_client_health"]
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False


def test_list_sites_client_health_preserves_positional_pagination(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"items": []}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    monitoring.list_sites_client_health(25, 10)

    client.get.assert_called_once_with(
        "/network-monitoring/v1/sites-client-health",
        params={"limit": 25, "offset": 10},
    )


def test_list_alert_classifications_calls_expected_endpoint(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"Critical": 2}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.list_alert_classifications(
        classify_by="severity",
        filter="status eq 'Active'",
        search="uplink",
    )

    assert result == {"Critical": 2}
    client.get.assert_called_once_with(
        "/network-notifications/v1/alerts/classification",
        params={"type": "severity", "filter": "status eq 'Active'", "search": "uplink"},
    )


def test_list_alert_configs_calls_expected_endpoint(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"items": []}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.list_alert_configs(scope_id=" global-scope ", scope_type="global")

    assert result == {"items": []}
    client.get.assert_called_once_with(
        "/network-notifications/v1/alert-config",
        params={"scope-id": "global-scope", "scope-type": "GLOBAL"},
    )


def test_list_alert_configs_validates_scope_type(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    with pytest.raises(ValueError, match="scope_type must be one of"):
        monitoring.list_alert_configs(scope_id="scope-1", scope_type="GROUP")

    client.get.assert_not_called()


def test_list_insights_calls_expected_endpoint(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"items": []}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.list_insights(limit=500, offset=5)

    assert result == {"items": []}
    client.get.assert_called_once_with(
        "/network-notifications/v1/insights",
        params={"limit": 200, "offset": 5},
    )


def test_get_tenant_health_collects_both_summaries(monkeypatch):
    client = MagicMock()
    client.get.side_effect = [{"score": 99}, {"score": 88}]
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.get_tenant_health()

    assert result["device_health"] == {"score": 99}
    assert result["client_health"] == {"score": 88}
    assert result["errors"] == []
    assert client.get.call_count == 2


def test_get_alert_action_status_uses_quoted_task_path(monkeypatch):
    client = MagicMock()
    client._request.return_value = _response(200, {"status": "COMPLETED"})
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.get_alert_action_status("task/123")

    assert result == {
        "status": "COMPLETED",
        "endpoint_used": "/network-notifications/v1/alerts/async-operations/task%2F123",
    }
    client._request.assert_called_once_with(
        "GET",
        "/network-notifications/v1/alerts/async-operations/task%2F123",
    )


def test_clear_alerts_confirms_then_posts_expected_payload(monkeypatch):
    client = MagicMock()
    client._request.return_value = _response(202, {"task_id": "task-1"})
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(
        monitoring.clear_alerts(
            _AcceptCtx(),
            keys=[" alert-1 ", "alert-2"],
            reason="Problem was resolved",
            notes="fixed upstream",
        )
    )

    assert result == {
        "task_id": "task-1",
        "endpoint_used": "/network-notifications/v1/alerts/clear",
    }
    client._request.assert_called_once_with(
        "POST",
        "/network-notifications/v1/alerts/clear",
        json={
            "keys": ["alert-1", "alert-2"],
            "reason": "Problem was resolved",
            "notes": "fixed upstream",
        },
    )


def test_alert_actions_reject_empty_keys():
    with pytest.raises(ValueError, match="keys must contain"):
        asyncio.run(monitoring.reactivate_alerts(_AcceptCtx(), keys=[]))


def test_set_alert_priority_validates_priority(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    with pytest.raises(ValueError, match="priority must be one of"):
        asyncio.run(
            monitoring.set_alert_priority(_AcceptCtx(), keys=["alert-1"], priority="Urgent")
        )

    client._arequest.assert_not_called()


def test_alert_action_decline_does_not_post(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(
        monitoring.set_alert_priority(_DeclineCtx(), keys=["alert-1"], priority="Low")
    )

    assert result == {"status": "CANCELLED", "detail": "user declined confirmation"}
    client._arequest.assert_not_called()


def test_defer_and_reactivate_alerts_confirm_then_post_expected_payloads(monkeypatch):
    client = MagicMock()
    client._request.return_value = _response(202, {"task_id": "task-2"})
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    deferred = asyncio.run(
        monitoring.defer_alerts(
            _AcceptCtx(),
            keys=["alert-1"],
            defer_until="2026-07-01T10:00:00Z",
        )
    )
    reactivated = asyncio.run(monitoring.reactivate_alerts(_AcceptCtx(), keys=["alert-1"]))

    assert deferred["endpoint_used"] == "/network-notifications/v1/alerts/defer"
    assert reactivated["endpoint_used"] == "/network-notifications/v1/alerts/active"
    assert client._request.call_args_list[0].args == (
        "POST",
        "/network-notifications/v1/alerts/defer",
    )
    assert client._request.call_args_list[0].kwargs == {
        "json": {"keys": ["alert-1"], "deferUntil": "2026-07-01T10:00:00Z"}
    }
    assert client._request.call_args_list[1].args == (
        "POST",
        "/network-notifications/v1/alerts/active",
    )
    assert client._request.call_args_list[1].kwargs == {"json": {"keys": ["alert-1"]}}


def test_config_health_tools_call_expected_endpoints(monkeypatch):
    client = MagicMock()
    client.get.side_effect = [{"issues": []}, {"items": []}]
    client.post.return_value = {"message": "Full configuration sync triggered for 1 devices."}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    issues = monitoring.get_device_config_issues(" SN123 ")
    health = monitoring.list_devices_config_health(
        limit=500,
        offset=5,
        sort="activeIssues desc",
        filter="configStatus eq 'OUT_OF_SYNC'",
        search="SN1",
    )
    resync = monitoring.resync_device_config([" SN123 "])

    assert issues == {"issues": []}
    assert health == {"items": []}
    assert resync == {"message": "Full configuration sync triggered for 1 devices."}
    client.get.assert_any_call(
        "/network-config/v1alpha1/config-health/active-issue",
        params={"serial": "SN123"},
    )
    client.get.assert_any_call(
        "/network-config/v1alpha1/config-health/devices",
        params={
            "limit": 200,
            "offset": 5,
            "sort": "activeIssues desc",
            "filter": "configStatus eq 'OUT_OF_SYNC'",
            "search": "SN1",
        },
    )
    client.post.assert_called_once_with(
        "/network-config/v1alpha1/config-health/devices-resync",
        data={"serials": ["SN123"]},
    )


def test_find_scope_matches_name_and_type(monkeypatch):
    monkeypatch.setattr(
        monitoring,
        "list_scopes",
        lambda full_list=False: {
            "items": [
                {"scope_id": "global", "scope_name": "Global", "scope_type": "GLOBAL"},
                {"scope_id": "site-1", "scope_name": "Austin Lab", "scope_type": "SITE"},
            ]
        },
    )

    result = monitoring.find_scope("austin", scope_type="site")

    assert result["items"] == [
        {
            "scope_id": "site-1",
            "scope_name": "Austin Lab",
            "scope_type": "SITE",
            "raw": {"scope_id": "site-1", "scope_name": "Austin Lab", "scope_type": "SITE"},
        }
    ]


def test_find_scope_propagates_scope_discovery_failure(monkeypatch):
    failure = {
        "status": 403,
        "error": "Central scope discovery failed",
        "warnings": ["access denied"],
    }
    monkeypatch.setattr(
        monitoring,
        "list_scopes",
        lambda full_list=False: failure,
    )

    assert monitoring.find_scope("austin") is failure


def test_find_scope_preserves_partial_discovery_warnings(monkeypatch):
    monkeypatch.setattr(
        monitoring,
        "list_scopes",
        lambda full_list=False: {
            "items": [
                {"scope_id": "site-1", "scope_name": "Austin Lab", "scope_type": "SITE"}
            ],
            "warnings": ["device groups unavailable"],
            "_pagination": {"truncated": True},
        },
    )

    result = monitoring.find_scope("austin")

    assert result["warnings"] == ["device groups unavailable"]
    assert result["_pagination"]["truncated"] is True


def test_list_scopes_uses_official_scope_management_endpoints(monkeypatch):
    client = MagicMock()

    def get(endpoint, params=None):
        if endpoint == "/network-config/v1/global":
            assert params is None
            return {"scopeId": "1000"}
        if endpoint == "/network-config/v1/sites":
            assert params == {"limit": 100, "offset": 0}
            return {
                "items": [
                    {"scopeId": "site-1", "scopeName": "Austin"},
                    {"id": "site-2", "name": "Boston"},
                ]
            }
        if endpoint == "/network-config/v1/device-groups":
            assert params == {"limit": 100, "offset": 0}
            return {"items": [{"scopeId": "group-1", "scopeName": "Branch APs"}]}
        raise AssertionError(endpoint)

    client.get.side_effect = get
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.list_scopes(full_list=True)

    assert result == {
        "items": [
            {"scope_id": "1000", "scope_name": "Global", "scope_type": "GLOBAL"},
            {"scope_id": "site-1", "scope_name": "Austin", "scope_type": "SITE"},
            {"scope_id": "site-2", "scope_name": "Boston", "scope_type": "SITE"},
            {
                "scope_id": "group-1",
                "scope_name": "Branch APs",
                "scope_type": "DEVICE_GROUP",
            },
        ],
        "_pagination": {
            "offset": 0,
            "limit": 4,
            "total": 4,
            "truncated": False,
            "list_key": "items",
        },
    }


def test_list_scopes_preserves_partial_results_with_warnings(monkeypatch):
    client = MagicMock()

    def get(endpoint, params=None):
        if endpoint == "/network-config/v1/global":
            return {"scopeId": "1000"}
        if endpoint == "/network-config/v1/sites":
            raise RuntimeError("site API unavailable")
        if endpoint == "/network-config/v1/device-groups":
            return {"items": [{"scopeId": "group-1", "scopeName": "Branch APs"}]}
        raise AssertionError(endpoint)

    client.get.side_effect = get
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.list_scopes(full_list=True)

    assert [item["scope_id"] for item in result["items"]] == ["1000", "group-1"]
    assert result["warnings"] == [
        "/network-config/v1/sites: site API unavailable"
    ]


def test_list_scopes_fails_closed_when_every_source_fails(monkeypatch):
    client = MagicMock()
    client.get.side_effect = RuntimeError("Central unavailable")
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.list_scopes()

    assert result["status"] == "failed"
    assert "no authoritative source returned data" in result["error"]
    assert len(result["warnings"]) == 3
    assert {call.args[0] for call in client.get.call_args_list} == {
        "/network-config/v1/global",
        "/network-config/v1/sites",
        "/network-config/v1/device-groups",
    }


def test_list_scopes_preserves_authorization_status_when_all_sources_refuse(monkeypatch):
    client = MagicMock()
    forbidden = RuntimeError("access denied")
    forbidden.response = SimpleNamespace(status_code=403)
    client.get.side_effect = forbidden
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.list_scopes()

    assert result["status"] == 403
    assert "no authoritative source returned data" in result["error"]


def test_list_scopes_does_not_report_partial_authorization_as_global_refusal(monkeypatch):
    client = MagicMock()
    forbidden = RuntimeError("access denied")
    forbidden.response = SimpleNamespace(status_code=403)

    def get(endpoint, params=None):
        if endpoint == "/network-config/v1/global":
            raise forbidden
        return {"items": []}

    client.get.side_effect = get
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.list_scopes()

    assert result["status"] == "failed"


def test_list_scopes_pages_each_collection_and_bounds_output(monkeypatch):
    client = MagicMock()
    first_site_page = [
        {"scopeId": f"site-{index}", "scopeName": f"Site {index}"}
        for index in range(100)
    ]

    def get(endpoint, params=None):
        if endpoint == "/network-config/v1/global":
            return {"scopeId": "1000"}
        if endpoint == "/network-config/v1/sites":
            if params["offset"] == 0:
                return {"items": first_site_page}
            return {"items": [{"scopeId": "site-100", "scopeName": "Site 100"}]}
        if endpoint == "/network-config/v1/device-groups":
            return {"items": []}
        raise AssertionError(endpoint)

    client.get.side_effect = get
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.list_scopes(limit=2, offset=100)

    assert [item["scope_id"] for item in result["items"]] == ["site-99", "site-100"]
    assert result["_pagination"] == {
        "offset": 100,
        "limit": 2,
        "total": 102,
        "truncated": False,
        "list_key": "items",
    }


def test_list_scopes_follows_short_page_continuation_offset(monkeypatch):
    client = MagicMock()
    first_page = [
        {"scopeId": f"site-{index}", "scopeName": f"Site {index}"}
        for index in range(50)
    ]

    def get(endpoint, params=None):
        if endpoint == "/network-config/v1/global":
            return {"scopeId": "1000"}
        if endpoint == "/network-config/v1/sites":
            if params["offset"] == 0:
                return {"items": first_page, "offset": "50", "total": 51}
            return {
                "items": [{"scopeId": "site-50", "scopeName": "Site 50"}],
                "offset": None,
                "total": 51,
            }
        if endpoint == "/network-config/v1/device-groups":
            return {"items": [], "offset": None, "total": 0}
        raise AssertionError(endpoint)

    client.get.side_effect = get
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.list_scopes(full_list=True)

    assert len(result["items"]) == 52
    assert result["items"][-1]["scope_id"] == "site-50"
    site_calls = [
        call
        for call in client.get.call_args_list
        if call.args[0] == "/network-config/v1/sites"
    ]
    assert [call.kwargs["params"]["offset"] for call in site_calls] == [0, 50]


def test_list_scopes_marks_missing_continuation_with_remaining_total_partial(monkeypatch):
    client = MagicMock()

    def get(endpoint, params=None):
        if endpoint == "/network-config/v1/global":
            return {"scopeId": "1000"}
        if endpoint == "/network-config/v1/sites":
            return {
                "items": [{"scopeId": "site-0", "scopeName": "Site 0"}],
                "total": 2,
            }
        if endpoint == "/network-config/v1/device-groups":
            return {"items": [], "offset": None, "total": 0}
        raise AssertionError(endpoint)

    client.get.side_effect = get
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.list_scopes(full_list=True)

    assert result["_pagination"]["truncated"] is True
    assert any("omitted continuation offset" in item for item in result["warnings"])


def test_list_scopes_validates_total_on_terminal_page(monkeypatch):
    client = MagicMock()

    def get(endpoint, params=None):
        if endpoint == "/network-config/v1/global":
            return {"scopeId": "1000"}
        if endpoint == "/network-config/v1/sites":
            return {
                "items": [{"scopeId": "site-0", "scopeName": "Site 0"}],
                "offset": None,
                "total": 2,
            }
        if endpoint == "/network-config/v1/device-groups":
            return {"items": [], "offset": None, "total": 0}
        raise AssertionError(endpoint)

    client.get.side_effect = get
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.list_scopes(full_list=True)

    assert result["_pagination"]["truncated"] is True
    assert any(
        "terminal page reported total 2 after 1 records" in item for item in result["warnings"]
    )


def test_list_scopes_rejects_regressing_continuation_and_marks_partial(monkeypatch):
    client = MagicMock()

    def get(endpoint, params=None):
        if endpoint == "/network-config/v1/global":
            return {"scopeId": "1000"}
        if endpoint == "/network-config/v1/sites":
            if params["offset"] == 0:
                return {
                    "items": [{"scopeId": "site-0", "scopeName": "Site 0"}],
                    "offset": "100",
                    "total": 3,
                }
            return {
                "items": [{"scopeId": "site-1", "scopeName": "Site 1"}],
                "offset": "50",
                "total": 3,
            }
        if endpoint == "/network-config/v1/device-groups":
            return {"items": [], "offset": None, "total": 0}
        raise AssertionError(endpoint)

    client.get.side_effect = get
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.list_scopes(full_list=True)

    assert result["_pagination"]["truncated"] is True
    assert any("non-increasing continuation offset 50" in item for item in result["warnings"])
    site_calls = [
        call
        for call in client.get.call_args_list
        if call.args[0] == "/network-config/v1/sites"
    ]
    assert [call.kwargs["params"]["offset"] for call in site_calls] == [0, 100]


def test_list_scopes_marks_later_page_failure_as_partial(monkeypatch):
    client = MagicMock()

    def get(endpoint, params=None):
        if endpoint == "/network-config/v1/global":
            return {"scopeId": "1000"}
        if endpoint == "/network-config/v1/sites":
            if params["offset"] == 0:
                return {
                    "items": [{"scopeId": "site-0", "scopeName": "Site 0"}],
                    "offset": "1",
                    "total": 2,
                }
            raise RuntimeError("second page unavailable")
        if endpoint == "/network-config/v1/device-groups":
            return {"items": [], "offset": None, "total": 0}
        raise AssertionError(endpoint)

    client.get.side_effect = get
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.list_scopes(full_list=True)

    assert result["_pagination"]["truncated"] is True
    assert [item["scope_id"] for item in result["items"]] == ["1000", "site-0"]


def test_get_global_scope_id_uses_official_endpoint(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"scopeId": 12345}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.get_global_scope_id()

    assert result == {"global_scope_id": "12345", "errors": []}
    client.get.assert_called_once_with("/network-config/v1/global")


def test_get_global_scope_id_strips_and_falls_back_to_id(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"scopeId": "global-2", "id": " 67890 "}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    assert monitoring.get_global_scope_id() == {
        "global_scope_id": "67890",
        "errors": [],
    }


@pytest.mark.parametrize(
    "payload",
    [{"scopeId": "   "}, {"scopeId": "global-2"}, {"scopeId": []}, {"scopeId": False}],
)
def test_get_global_scope_id_surfaces_malformed_response(monkeypatch, payload):
    client = MagicMock()
    client.get.return_value = payload
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.get_global_scope_id()

    assert result["global_scope_id"] is None
    assert result["errors"] == [
        "/network-config/v1/global: response omitted a valid numeric scopeId"
    ]


def test_list_scope_devices_filters_known_scope_fields(monkeypatch):
    mcp_client = MagicMock()
    mcp_client.get_devices_page.return_value = (
        [
            {"serialNumber": "AP1", "siteId": "site-1", "deviceType": "ACCESS_POINT"},
            {"serialNumber": "SW1", "siteId": "site-1", "deviceType": "SWITCH"},
            {"serialNumber": "AP2", "siteId": "site-2", "deviceType": "ACCESS_POINT"},
        ],
        None,
    )
    monkeypatch.setattr(monitoring, "get_mcp_client", lambda: mcp_client)

    result = monitoring.list_scope_devices("site-1", device_type="AP", limit=10)

    assert result["items"] == [
        {"serialNumber": "AP1", "siteId": "site-1", "deviceType": "ACCESS_POINT"}
    ]
    mcp_client.get_devices_page.assert_called_once_with(
        {"siteId": "site-1"}, limit=200, next_cursor=None
    )


def test_site_health_summary_uses_site_id_inventory_filter(monkeypatch):
    mcp_client = MagicMock()
    mcp_client.get_devices.return_value = [
        {"serialNumber": "SW1", "siteId": "site-1", "deviceType": "SWITCH", "status": "UP"}
    ]
    mcp_client.get_clients.return_value = []
    mcp_client.get_alerts.return_value = []
    mcp_client.get_events.return_value = []
    monkeypatch.setattr(monitoring, "get_mcp_client", lambda: mcp_client)

    result = monitoring.get_site_health_summary(site_id="site-1")

    assert result["site_id"] == "site-1"
    mcp_client.get_devices.assert_called_once_with(filters={"siteId": "site-1"}, limit=200)


def test_list_devices_config_health_validates_search_length(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    with pytest.raises(ValueError, match="search must be"):
        monitoring.list_devices_config_health(search="ab")

    client.get.assert_not_called()


# ---------------------------------------------------------------------------
# Topology / swarms / AP tunnel telemetry / applications / reporting
# ---------------------------------------------------------------------------


def test_get_topology_calls_expected_endpoint(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"nodes": [], "links": []}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.get_topology("site-1")

    assert result == {"nodes": [], "links": []}
    client.get.assert_called_once_with("/network-monitoring/v1/topology/site-1")


def test_list_swarms_uses_cursor_not_offset(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"swarms": []}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    monitoring.list_swarms(limit=50, offset=19)

    client.get.assert_called_once_with(
        "/network-monitoring/v1/swarms", params={"limit": 50, "next": "20"}
    )


def test_get_swarm_calls_expected_endpoint(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"clusterId": "c1"}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.get_swarm("c1")

    assert result == {"clusterId": "c1"}
    client.get.assert_called_once_with("/network-monitoring/v1/swarms/c1")


def test_list_ap_tunnels_uses_cursor_not_offset(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"tunnels": []}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    monitoring.list_ap_tunnels("AP1", site_id="site-1", limit=20, offset=5)

    client.get.assert_called_once_with(
        "/network-monitoring/v1/aps/AP1/tunnels",
        params={"limit": 20, "next": "6", "site-id": "site-1"},
    )


def test_get_ap_tunnel_calls_expected_endpoint(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"tunnelId": "t1"}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.get_ap_tunnel("AP1", "t1")

    assert result == {"tunnelId": "t1"}
    client.get.assert_called_once_with("/network-monitoring/v1/aps/AP1/tunnels/t1")


def test_list_applications_uses_true_offset_pagination(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"items": []}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    monitoring.list_applications(
        site_id="site-1", start_time="2026-01-01T00:00:00Z", end_time="2026-01-02T00:00:00Z",
        client_id="aa:bb", limit=50, offset=10,
    )

    client.get.assert_called_once_with(
        "/network-monitoring/v1/applications",
        params={
            "site-id": "site-1",
            "start-at": "2026-01-01T00:00:00Z",
            "end-at": "2026-01-02T00:00:00Z",
            "limit": 50,
            "offset": 10,
            "client-id": "aa:bb",
        },
    )


def test_list_reports_uses_cursor(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"items": []}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    monitoring.list_reports(search="wireless", next_cursor="5")

    client.get.assert_called_once_with(
        "/network-reporting/v1/reports",
        params={"limit": 10, "next": "5", "search": "wireless"},
    )


def test_list_report_runs_calls_expected_endpoint(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"items": []}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    monitoring.list_report_runs(report_id="42", sort="modifiedAt desc")

    client.get.assert_called_once_with(
        "/network-reporting/v1alpha1/reports/42/report-runs",
        params={"limit": 10, "sort": "modifiedAt desc"},
    )


def test_get_reports_metadata_calls_expected_endpoint(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"reportTypes": []}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.get_reports_metadata()

    assert result == {"reportTypes": []}
    client.get.assert_called_once_with("/network-reporting/v1alpha1/reports-metadata")


def test_get_reporting_service_health_calls_expected_endpoint(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"status": "UP"}
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = monitoring.get_reporting_service_health()

    assert result == {"status": "UP"}
    client.get.assert_called_once_with("/network-reporting/v1alpha1/reports/health")


# ---------------------------------------------------------------------------
# Client onboarding events
# ---------------------------------------------------------------------------


def test_list_client_onboarding_events_filters_by_event_name(monkeypatch):
    mcp_client = MagicMock()
    mcp_client.get_events.return_value = [
        {"eventName": "Client Onboarding", "clientMacAddress": "aa:bb"},
        {"eventName": "Interface Down"},
        {"eventName": "Client Onboarding", "clientMacAddress": "cc:dd"},
    ]
    monkeypatch.setattr(monitoring, "get_mcp_client", lambda: mcp_client)

    result = monitoring.list_client_onboarding_events("SW1", hours=12)

    assert result["items"] == [
        {"eventName": "Client Onboarding", "clientMacAddress": "aa:bb"},
        {"eventName": "Client Onboarding", "clientMacAddress": "cc:dd"},
    ]
    mcp_client.get_events.assert_called_once_with("SW1", hours=12, api_limit=1000)


# ---------------------------------------------------------------------------
# Notification-rule CRUD (best-effort, unconfirmed endpoint shape)
# ---------------------------------------------------------------------------


def test_create_notification_rule_dry_run_returns_payload_without_sending(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(
        monitoring.create_notification_rule(_AcceptCtx(), {"name": "rule-1"}, dry_run=True)
    )

    assert result["dry_run"] is True
    client._request.assert_not_called()


def test_create_notification_rule_requires_confirmation(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(
        monitoring.create_notification_rule(_DeclineCtx(), {"name": "rule-1"})
    )

    assert result["status"] == "CANCELLED"
    client._request.assert_not_called()


def test_create_notification_rule_posts_when_confirmed(monkeypatch):
    client = MagicMock()
    client._arequest = AsyncMock(
        return_value=_response(status_code=201, payload={"id": "rule-1"})
    )
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(
        monitoring.create_notification_rule(_AcceptCtx(), {"name": "rule-1"})
    )

    assert result["id"] == "rule-1"
    client._arequest.assert_awaited_once_with(
        "POST", "/network-notifications/v1/alert-config", json={"name": "rule-1"}
    )


def test_delete_notification_rule_dry_run(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(monitoring.delete_notification_rule(_AcceptCtx(), "rule-1", dry_run=True))

    assert result["dry_run"] is True
    client._arequest.assert_not_called()


def test_delete_notification_rule_deletes_when_confirmed(monkeypatch):
    client = MagicMock()
    client._arequest = AsyncMock(return_value=_response(status_code=204, payload={}))
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(monitoring.delete_notification_rule(_AcceptCtx(), "rule-1"))

    assert result["deleted"] is True
    client._arequest.assert_awaited_once_with(
        "DELETE", "/network-notifications/v1/alert-config/rule-1"
    )


def test_set_notification_rule_enabled_surfaces_404_cleanly(monkeypatch):
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 404
    resp.json.side_effect = ValueError()
    resp.text = "not found"
    client._arequest = AsyncMock(return_value=resp)
    monkeypatch.setattr(monitoring, "get_client", lambda: client)

    result = asyncio.run(
        monitoring.set_notification_rule_enabled(_AcceptCtx(), "rule-1", enabled=True)
    )

    assert "error" in result
    assert result["endpoint_used"] == "/network-notifications/v1/alert-config/rule-1"
