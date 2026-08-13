from __future__ import annotations

from typing import Any, Callable

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

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json: Any = None,
    ) -> FakeResponse:
        self.calls.append((method, endpoint, json))
        return self.response


WriteCall = Callable[[], dict[str, Any]]


def _calls() -> list[tuple[str, WriteCall]]:
    return [
        (
            "create_auth_server",
            lambda: nac.create_auth_server(
                "hpe-mcp-lab-radius",
                "192.0.2.10",
                "transient-secret",
            ),
        ),
        (
            "delete_auth_server",
            lambda: nac.delete_auth_server("hpe-mcp-lab-radius"),
        ),
        (
            "create_aaa_profile",
            lambda: nac.create_aaa_profile("hpe-mcp-lab-aaa"),
        ),
        (
            "delete_aaa_profile",
            lambda: nac.delete_aaa_profile("hpe-mcp-lab-aaa"),
        ),
        (
            "create_server_group",
            lambda: nac.create_server_group(
                "hpe-mcp-lab-sg", ["hpe-mcp-lab-radius"]
            ),
        ),
        (
            "delete_server_group",
            lambda: nac.delete_server_group("hpe-mcp-lab-sg"),
        ),
    ]


@pytest.mark.parametrize(("_name", "call"), _calls())
def test_migration_nac_writes_raise_on_rejected_http_response(
    monkeypatch,
    _name: str,
    call: WriteCall,
):
    client = FakeClient(FakeResponse(403, {"error": "forbidden"}))
    monkeypatch.setattr(nac, "get_client", lambda: client)

    with pytest.raises(WriteResultError):
        call()


@pytest.mark.parametrize(("_name", "call"), _calls())
def test_migration_nac_writes_raise_on_error_shaped_2xx_body(
    monkeypatch,
    _name: str,
    call: WriteCall,
):
    client = FakeClient(FakeResponse(200, {"success": False}))
    monkeypatch.setattr(nac, "get_client", lambda: client)

    with pytest.raises(WriteResultError):
        call()


@pytest.mark.parametrize(("_name", "call"), _calls())
def test_migration_nac_writes_accept_clean_empty_success(
    monkeypatch,
    _name: str,
    call: WriteCall,
):
    client = FakeClient(FakeResponse(204))
    monkeypatch.setattr(nac, "get_client", lambda: client)

    result = call()

    assert result["status_code"] == 204
