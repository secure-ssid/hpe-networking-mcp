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
    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, Any]] = []

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json: Any = None,
        params: Any = None,
    ) -> FakeResponse:
        self.calls.append((method, endpoint, json))
        return self.responses.pop(0)


def test_create_gw_policy_dry_run_is_bounded_and_sends_nothing(monkeypatch):
    monkeypatch.setattr(
        config,
        "get_client",
        lambda: (_ for _ in ()).throw(AssertionError("must not send")),
    )

    result = config.create_gw_policy("hpe-mcp-lab-policy", dry_run=True)

    assert result["dry_run"] is True
    assert result["payload"]["type"] == "POLICY_TYPE_SECURITY"
    rules = result["payload"]["security-policy"]["policy-rule"]
    assert rules[0]["action"]["type"] == "ACTION_ALLOW"


def test_create_gw_policy_stops_before_group_patch_when_create_fails(monkeypatch):
    client = FakeClient([FakeResponse(409, {"errors": ["already exists"]})])
    monkeypatch.setattr(config, "get_client", lambda: client)

    with pytest.raises(WriteResultError):
        config.create_gw_policy("hpe-mcp-lab-policy")

    assert len(client.calls) == 1
    assert client.calls[0][0] == "POST"


def test_create_gw_policy_raises_when_group_patch_fails(monkeypatch):
    client = FakeClient(
        [
            FakeResponse(201, {"name": "hpe-mcp-lab-policy"}),
            FakeResponse(500, {"success": False}),
        ]
    )
    monkeypatch.setattr(config, "get_client", lambda: client)

    with pytest.raises(WriteResultError):
        config.create_gw_policy("hpe-mcp-lab-policy")

    assert [call[0] for call in client.calls] == ["POST", "PATCH"]


def test_create_gw_policy_returns_both_clean_results(monkeypatch):
    client = FakeClient(
        [
            FakeResponse(201, {"name": "hpe-mcp-lab-policy"}),
            FakeResponse(200, {"status": "success"}),
        ]
    )
    monkeypatch.setattr(config, "get_client", lambda: client)

    result = config.create_gw_policy("hpe-mcp-lab-policy")

    assert result == {
        "policy": {"name": "hpe-mcp-lab-policy"},
        "policy_group": {"status": "success"},
    }


@pytest.mark.parametrize("tool_name", ["delete_role_acl", "delete_gw_policy"])
def test_policy_delete_raises_on_rejected_write(
    monkeypatch,
    tool_name: str,
):
    client = FakeClient([FakeResponse(403, {"error": "forbidden"})])
    monkeypatch.setattr(config, "get_client", lambda: client)

    with pytest.raises(WriteResultError):
        getattr(config, tool_name)("hpe-mcp-lab-policy")


@pytest.mark.parametrize("tool_name", ["delete_role_acl", "delete_gw_policy"])
def test_policy_delete_accepts_clean_empty_2xx(
    monkeypatch,
    tool_name: str,
):
    client = FakeClient([FakeResponse(204)])
    monkeypatch.setattr(config, "get_client", lambda: client)

    result = getattr(config, tool_name)("hpe-mcp-lab-policy")

    assert result["status_code"] == 204
