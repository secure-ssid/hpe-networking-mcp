"""Unit tests for the curated auth server-group lifecycle tools
(`list_server_groups`, `get_server_group`, `create_server_group`,
`delete_server_group`) added to `hpe_networking_mcp.mcp_servers.nac`.

Covers request method/path/body against the committed auth-server-group
OpenAPI spec, dry-run behavior (no client call, previewed payload), bounded
list output, and error-envelope validation via `validate_write_result`.
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
    def __init__(self, response: FakeResponse, *, get_payload: Any = None):
        self.response = response
        self._get_payload = get_payload
        self.calls: list[tuple[str, str, Any]] = []

    def _request(self, method: str, endpoint: str, *, json: Any = None) -> FakeResponse:
        self.calls.append((method, endpoint, json))
        return self.response

    def get(self, endpoint: str, params: Any = None) -> Any:
        self.calls.append(("GET", endpoint, params))
        return self._get_payload


# ---------------------------------------------------------------------------
# create_server_group: method/path/body contract
# ---------------------------------------------------------------------------


def test_create_server_group_sends_expected_method_path_body(monkeypatch):
    fake_client = FakeClient(FakeResponse(201, {"name": "hpe-mcp-lab-sg"}))
    monkeypatch.setattr(nac, "get_client", lambda: fake_client)

    result = nac.create_server_group(
        "hpe-mcp-lab-sg",
        ["radius-1", "radius-2"],
        group_type="RADIUS",
        fail_through=True,
    )

    assert result["name"] == "hpe-mcp-lab-sg"
    assert fake_client.calls == [
        (
            "POST",
            "/network-config/v1alpha1/server-groups/hpe-mcp-lab-sg",
            {
                "name": "hpe-mcp-lab-sg",
                "type": "RADIUS",
                "servers": [
                    {"server-name": "radius-1", "position": 1},
                    {"server-name": "radius-2", "position": 2},
                ],
                "fail-through": True,
                "load-balance": False,
            },
        )
    ]


def test_create_server_group_dry_run_previews_payload_and_skips_client():
    def fail_get_client():
        raise AssertionError("dry_run must never reach get_client()")

    result = nac.create_server_group(
        "hpe-mcp-lab-sg",
        ["radius-1"],
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["endpoint"] == "/network-config/v1alpha1/server-groups/hpe-mcp-lab-sg"
    assert result["payload"]["servers"] == [{"server-name": "radius-1", "position": 1}]


def test_create_server_group_dry_run_never_calls_client(monkeypatch):
    def fail_get_client():
        raise AssertionError("dry_run must never reach get_client()")

    monkeypatch.setattr(nac, "get_client", fail_get_client)
    nac.create_server_group("hpe-mcp-lab-sg", ["radius-1"], dry_run=True)


def test_create_server_group_raises_on_non_2xx_response(monkeypatch):
    fake_client = FakeClient(FakeResponse(409, {"errors": ["already exists"]}))
    monkeypatch.setattr(nac, "get_client", lambda: fake_client)
    with pytest.raises(WriteResultError):
        nac.create_server_group("hpe-mcp-lab-sg", ["radius-1"])


def test_create_server_group_raises_on_error_shaped_2xx_body(monkeypatch):
    fake_client = FakeClient(FakeResponse(200, {"success": False}))
    monkeypatch.setattr(nac, "get_client", lambda: fake_client)
    with pytest.raises(WriteResultError):
        nac.create_server_group("hpe-mcp-lab-sg", ["radius-1"])


# ---------------------------------------------------------------------------
# delete_server_group: method/path, dry-run, error-envelope validation
# ---------------------------------------------------------------------------


def test_delete_server_group_sends_expected_method_and_path(monkeypatch):
    fake_client = FakeClient(FakeResponse(204))
    monkeypatch.setattr(nac, "get_client", lambda: fake_client)

    result = nac.delete_server_group("hpe-mcp-lab-sg")

    assert result["status_code"] == 204
    assert fake_client.calls == [
        ("DELETE", "/network-config/v1alpha1/server-groups/hpe-mcp-lab-sg", None)
    ]


def test_delete_server_group_dry_run_never_calls_client(monkeypatch):
    def fail_get_client():
        raise AssertionError("dry_run must never reach get_client()")

    monkeypatch.setattr(nac, "get_client", fail_get_client)
    result = nac.delete_server_group("hpe-mcp-lab-sg", dry_run=True)
    assert result == {"dry_run": True, "name": "hpe-mcp-lab-sg"}


def test_delete_server_group_raises_on_non_2xx_response(monkeypatch):
    fake_client = FakeClient(FakeResponse(409, {"errors": ["group in use"]}))
    monkeypatch.setattr(nac, "get_client", lambda: fake_client)
    with pytest.raises(WriteResultError):
        nac.delete_server_group("hpe-mcp-lab-sg")


# ---------------------------------------------------------------------------
# list_server_groups / get_server_group: read path + bounded output
# ---------------------------------------------------------------------------


def test_list_server_groups_bounds_output_by_default(monkeypatch):
    items = [{"name": f"sg-{i}"} for i in range(75)]
    fake_client = FakeClient(FakeResponse(200), get_payload={"server-group": items})
    monkeypatch.setattr(nac, "get_client", lambda: fake_client)

    result = nac.list_server_groups()

    assert fake_client.calls == [("GET", "/network-config/v1alpha1/server-groups", None)]
    assert len(result["server-group"]) == 50
    assert result["_pagination"]["total"] == 75
    assert result["_pagination"]["truncated"] is True


def test_list_server_groups_full_list_returns_everything(monkeypatch):
    items = [{"name": f"sg-{i}"} for i in range(75)]
    fake_client = FakeClient(FakeResponse(200), get_payload={"server-group": items})
    monkeypatch.setattr(nac, "get_client", lambda: fake_client)

    result = nac.list_server_groups(full_list=True)

    assert len(result["server-group"]) == 75


def test_get_server_group_sends_expected_path(monkeypatch):
    fake_client = FakeClient(FakeResponse(200), get_payload={"name": "hpe-mcp-lab-sg"})
    monkeypatch.setattr(nac, "get_client", lambda: fake_client)

    result = nac.get_server_group("hpe-mcp-lab-sg")

    assert result["name"] == "hpe-mcp-lab-sg"
    assert fake_client.calls == [
        ("GET", "/network-config/v1alpha1/server-groups/hpe-mcp-lab-sg", None)
    ]
