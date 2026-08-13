"""Unit tests for Passpoint config tools."""

from __future__ import annotations

from unittest.mock import MagicMock

from hpe_networking_mcp.mcp_servers import config


def test_list_passpoint_profiles_bounds_and_filters(monkeypatch):
    client = MagicMock()
    # The server honored limit=1/offset=1 and returned just the second page.
    client.get.return_value = {
        "profile": [
            {"name": "guest-passpoint"},
        ]
    }
    monkeypatch.setattr(config, "get_client", lambda: client)

    result = config.list_passpoint_profiles(
        limit=1,
        offset=1,
        view_type="LOCAL",
        scope_id="79244870000394240",
        device_function="CAMPUS_AP",
        effective=True,
        detailed=True,
    )

    client.get.assert_called_once_with(
        "/network-config/v1alpha1/passpoint",
        params={
            "view-type": "LOCAL",
            "scope-id": "79244870000394240",
            "device-function": "CAMPUS_AP",
            "effective": "true",
            "detailed": "true",
            "limit": 1,
            "offset": 1,
        },
    )
    assert result["profile"] == [{"name": "guest-passpoint"}]
    assert result["_pagination"]["offset"] == 1
    assert result["_pagination"]["total"] == 1


def test_list_passpoint_profiles_does_not_reapply_offset_to_server_page(monkeypatch):
    client = MagicMock()
    # 60 profiles total; the server honored limit=50/offset=50 and returned
    # the 10 remaining. The tool must not slice those 10 again with offset=50.
    client.get.return_value = {"profile": [{"name": f"p{i}"} for i in range(50, 60)]}
    monkeypatch.setattr(config, "get_client", lambda: client)

    result = config.list_passpoint_profiles(limit=50, offset=50)

    assert len(result["profile"]) == 10
    assert result["_pagination"]["offset"] == 50
    assert result["_pagination"]["truncated"] is False


def test_list_passpoint_profiles_full_list_preserves_payload(monkeypatch):
    client = MagicMock()
    client.get.return_value = {
        "profile": [{"name": "sys_cnac_air_pass"}],
        "meta": {"source": "library"},
    }
    monkeypatch.setattr(config, "get_client", lambda: client)

    result = config.list_passpoint_profiles(full_list=True, object_type="SHARED")

    assert result["profile"] == [{"name": "sys_cnac_air_pass"}]
    assert result["meta"] == {"source": "library"}


def test_get_passpoint_profile_requests_named_endpoint(monkeypatch):
    client = MagicMock()
    response = MagicMock()
    response.text = '{"name":"sys_cnac_air_pass"}'
    response.json.return_value = {"name": "sys_cnac_air_pass"}
    client._request.return_value = response
    monkeypatch.setattr(config, "get_client", lambda: client)

    result = config.get_passpoint_profile(
        "sys cnac air pass",
        view_type="LOCAL",
        scope_id="79244870000394240",
        device_function="CAMPUS_AP",
    )

    client._request.assert_called_once_with(
        "GET",
        "/network-config/v1alpha1/passpoint/sys%20cnac%20air%20pass",
        params={
            "view-type": "LOCAL",
            "scope-id": "79244870000394240",
            "device-function": "CAMPUS_AP",
        },
    )
    assert result == {"name": "sys_cnac_air_pass"}


def test_list_passpoint_identity_profiles_accepts_items_shape(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"items": [{"name": "example.com"}]}
    monkeypatch.setattr(config, "get_client", lambda: client)

    result = config.list_passpoint_identity_profiles(limit=10)

    assert result["profile"] == [{"name": "example.com"}]


def test_get_passpoint_identity_profile_requests_named_endpoint(monkeypatch):
    client = MagicMock()
    response = MagicMock()
    response.text = '{"name":"example.com"}'
    response.json.return_value = {"name": "example.com"}
    client._request.return_value = response
    monkeypatch.setattr(config, "get_client", lambda: client)

    result = config.get_passpoint_identity_profile("example.com", object_type="SHARED")

    client._request.assert_called_once_with(
        "GET",
        "/network-config/v1alpha1/passpoint-identity/example.com",
        params={"object-type": "SHARED"},
    )
    assert result == {"name": "example.com"}
