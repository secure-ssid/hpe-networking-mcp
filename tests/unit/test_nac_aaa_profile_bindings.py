"""Unit tests for `hpe_networking_mcp.mcp_servers.nac.create_aaa_profile`'s nested
`authentication.dot1x-auth` / `authentication.mac-auth` bindings, added to
expose the device-side dot1x/mac authentication-profile references defined
by the committed aaa-profile OpenAPI schema
(`ArubaAaaProfile_AuthenticationConfigGroup.authentication`).

Also covers request method/path/body and dry-run behavior for the existing
`authorization`/`acct-server-group` fields, to guard against regressions
while extending the payload shape.
"""
from __future__ import annotations

from typing import Any

import pytest

from hpe_networking_mcp.mcp_servers import nac
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


def test_create_aaa_profile_dry_run_omits_authentication_when_unset():
    result = nac.create_aaa_profile("hpe-mcp-lab-aaa", dry_run=True)

    assert result["payload"] == {"name": "hpe-mcp-lab-aaa"}
    assert "authentication" not in result["payload"]


def test_create_aaa_profile_nests_dot1x_and_mac_auth_bindings_under_authentication():
    result = nac.create_aaa_profile(
        "hpe-mcp-lab-aaa",
        dot1x_auth_profile="hpe-mcp-lab-dot1x",
        mac_auth_profile="hpe-mcp-lab-mab",
        dry_run=True,
    )

    assert result["payload"]["authentication"] == {
        "dot1x-auth": "hpe-mcp-lab-dot1x",
        "mac-auth": "hpe-mcp-lab-mab",
    }


def test_create_aaa_profile_nests_only_the_binding_that_was_provided():
    dot1x_only = nac.create_aaa_profile(
        "hpe-mcp-lab-aaa", dot1x_auth_profile="hpe-mcp-lab-dot1x", dry_run=True
    )
    assert dot1x_only["payload"]["authentication"] == {"dot1x-auth": "hpe-mcp-lab-dot1x"}

    mac_only = nac.create_aaa_profile(
        "hpe-mcp-lab-aaa", mac_auth_profile="hpe-mcp-lab-mab", dry_run=True
    )
    assert mac_only["payload"]["authentication"] == {"mac-auth": "hpe-mcp-lab-mab"}


def test_create_aaa_profile_combines_authorization_and_authentication_nesting():
    result = nac.create_aaa_profile(
        "hpe-mcp-lab-aaa",
        auth_role="employee",
        fallback_role="guest",
        acct_server_group="hpe-mcp-lab-sg",
        dot1x_auth_profile="hpe-mcp-lab-dot1x",
        mac_auth_profile="hpe-mcp-lab-mab",
        dry_run=True,
    )

    assert result["payload"] == {
        "name": "hpe-mcp-lab-aaa",
        "authorization": {"auth-role": "employee", "fallback-role": "guest"},
        "acct-server-group": "hpe-mcp-lab-sg",
        "authentication": {
            "dot1x-auth": "hpe-mcp-lab-dot1x",
            "mac-auth": "hpe-mcp-lab-mab",
        },
    }


def test_create_aaa_profile_sends_nested_authentication_body_to_client(monkeypatch):
    fake_client = FakeClient(FakeResponse(201, {"name": "hpe-mcp-lab-aaa"}))
    monkeypatch.setattr(nac, "get_client", lambda: fake_client)

    result = nac.create_aaa_profile(
        "hpe-mcp-lab-aaa",
        dot1x_auth_profile="hpe-mcp-lab-dot1x",
        mac_auth_profile="hpe-mcp-lab-mab",
    )

    assert result["name"] == "hpe-mcp-lab-aaa"
    assert fake_client.calls == [
        (
            "POST",
            "/network-config/v1alpha1/aaa-profile/hpe-mcp-lab-aaa",
            {
                "name": "hpe-mcp-lab-aaa",
                "authentication": {
                    "dot1x-auth": "hpe-mcp-lab-dot1x",
                    "mac-auth": "hpe-mcp-lab-mab",
                },
            },
        )
    ]


def test_create_aaa_profile_raises_on_non_2xx_response_with_bindings(monkeypatch):
    fake_client = FakeClient(FakeResponse(404, {"errors": ["not found"]}))
    monkeypatch.setattr(nac, "get_client", lambda: fake_client)
    with pytest.raises(WriteResultError):
        nac.create_aaa_profile(
            "hpe-mcp-lab-aaa",
            dot1x_auth_profile="hpe-mcp-lab-dot1x",
        )


def test_create_aaa_profile_raises_on_error_shaped_2xx_body_with_bindings(monkeypatch):
    fake_client = FakeClient(FakeResponse(200, {"errors": ["conflict"]}))
    monkeypatch.setattr(nac, "get_client", lambda: fake_client)
    with pytest.raises(WriteResultError):
        nac.create_aaa_profile(
            "hpe-mcp-lab-aaa",
            mac_auth_profile="hpe-mcp-lab-mab",
        )
