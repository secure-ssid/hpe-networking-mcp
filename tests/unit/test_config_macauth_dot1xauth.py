"""Unit tests for `hpe_networking_mcp.mcp_servers.config.create_aaa_macauth_profile` and
`create_aaa_dot1xauth_profile`.

Regression coverage for two fixes:
  * endpoint version corrected from `/network-config/v1/{macauth,dot1xauth}`
    to `/network-config/v1alpha1/...`, matching the committed
    aaa-macauth.json / aaa-dot1xauth.json specs (and the generated central.json
    manifest derived from them);
  * both tools now call `validate_write_result` on the raw response and the
    parsed envelope instead of returning `resp_json(resp)` unconditionally,
    so a rejected write raises `WriteResultError` instead of coming back as a
    success-shaped envelope.
"""
from __future__ import annotations

from typing import Any

import pytest

from hpe_networking_mcp.mcp_servers import config
from hpe_networking_mcp.mcp_servers.shared import WriteResultError


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None):
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload

    @property
    def text(self) -> str:
        return "" if self._payload is None else str(self._payload)


class FakeClient:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[tuple[str, str, Any]] = []

    def _request(self, method: str, endpoint: str, *, json: Any = None) -> FakeResponse:
        self.calls.append((method, endpoint, json))
        return self.response


@pytest.mark.parametrize(
    "tool_name, resource",
    [
        ("create_aaa_macauth_profile", "macauth"),
        ("create_aaa_dot1xauth_profile", "dot1xauth"),
    ],
)
def test_create_device_auth_profile_uses_v1alpha1_endpoint(monkeypatch, tool_name, resource):
    fake_client = FakeClient(FakeResponse(201, {"name": "hpe-mcp-lab"}))
    monkeypatch.setattr(config, "get_client", lambda: fake_client)
    tool = getattr(config, tool_name)

    result = tool("hpe-mcp-lab", body={"key": "value"})

    assert result["name"] == "hpe-mcp-lab"
    assert fake_client.calls == [
        (
            "POST",
            f"/network-config/v1alpha1/{resource}/hpe-mcp-lab",
            {"key": "value"},
        )
    ]


@pytest.mark.parametrize(
    "tool_name",
    ["create_aaa_macauth_profile", "create_aaa_dot1xauth_profile"],
)
def test_create_device_auth_profile_dry_run_never_calls_client(monkeypatch, tool_name):
    def fail_get_client():
        raise AssertionError("dry_run must never reach get_client()")

    monkeypatch.setattr(config, "get_client", fail_get_client)
    tool = getattr(config, tool_name)
    result = tool("hpe-mcp-lab", dry_run=True)
    assert result["dry_run"] is True
    assert "v1alpha1" in result["endpoint"]


@pytest.mark.parametrize(
    "tool_name",
    ["create_aaa_macauth_profile", "create_aaa_dot1xauth_profile"],
)
def test_create_device_auth_profile_raises_on_non_2xx_response(monkeypatch, tool_name):
    fake_client = FakeClient(FakeResponse(422, {"errors": ["invalid"]}))
    monkeypatch.setattr(config, "get_client", lambda: fake_client)
    tool = getattr(config, tool_name)
    with pytest.raises(WriteResultError):
        tool("hpe-mcp-lab")


@pytest.mark.parametrize(
    "tool_name",
    ["create_aaa_macauth_profile", "create_aaa_dot1xauth_profile"],
)
def test_create_device_auth_profile_raises_on_error_shaped_2xx_body(monkeypatch, tool_name):
    fake_client = FakeClient(FakeResponse(200, {"success": False}))
    monkeypatch.setattr(config, "get_client", lambda: fake_client)
    tool = getattr(config, tool_name)
    with pytest.raises(WriteResultError):
        tool("hpe-mcp-lab")


@pytest.mark.parametrize(
    "tool_name",
    ["create_aaa_macauth_profile", "create_aaa_dot1xauth_profile"],
)
def test_create_device_auth_profile_succeeds_on_clean_2xx(monkeypatch, tool_name):
    fake_client = FakeClient(FakeResponse(200, {"name": "hpe-mcp-lab"}))
    monkeypatch.setattr(config, "get_client", lambda: fake_client)
    tool = getattr(config, tool_name)
    result = tool("hpe-mcp-lab")
    assert result["name"] == "hpe-mcp-lab"


# ---------------------------------------------------------------------------
# list_config_assignments: profile_type is not a server-side query filter in
# the committed config-assignment spec (GET only accepts
# scope-id/device-function); it must be applied client-side.
# ---------------------------------------------------------------------------


class FakeGetClient:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[tuple[str, str, Any]] = []

    def _request(self, method: str, endpoint: str, *, params: Any = None) -> FakeResponse:
        self.calls.append((method, endpoint, params))
        return self.response


def test_list_config_assignments_sends_only_spec_supported_query_params(monkeypatch):
    body = {
        "config-assignment": [
            {
                "scope-id": "100",
                "device-function": "CAMPUS_AP",
                "profile-type": "roles",
                "profile-instance": "employee",
            },
            {
                "scope-id": "100",
                "device-function": "CAMPUS_AP",
                "profile-type": "wlan-ssids",
                "profile-instance": "corp-ssid",
            },
        ]
    }
    fake_client = FakeGetClient(FakeResponse(200, body))
    monkeypatch.setattr(config, "get_client", lambda: fake_client)

    result = config.list_config_assignments(
        scope_id="100", device_function="CAMPUS_AP", profile_type="roles"
    )

    assert fake_client.calls == [
        (
            "GET",
            "/network-config/v1alpha1/config-assignments",
            {"scope-id": "100", "device-function": "CAMPUS_AP"},
        )
    ]
    assert len(result["config-assignment"]) == 1
    assert result["config-assignment"][0]["profile-instance"] == "employee"


def test_list_config_assignments_without_profile_type_returns_all_items(monkeypatch):
    body = {
        "config-assignment": [
            {
                "scope-id": "100",
                "device-function": "CAMPUS_AP",
                "profile-type": "roles",
                "profile-instance": "employee",
            },
            {
                "scope-id": "100",
                "device-function": "CAMPUS_AP",
                "profile-type": "wlan-ssids",
                "profile-instance": "corp-ssid",
            },
        ]
    }
    fake_client = FakeGetClient(FakeResponse(200, body))
    monkeypatch.setattr(config, "get_client", lambda: fake_client)

    result = config.list_config_assignments(full_list=True)

    assert len(result["config-assignment"]) == 2
