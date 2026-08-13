"""Unit tests for the Named MPSK / visitor update tools (nac.py) — added for
parity with update_mac_registration's PUT-on-collection convention.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from hpe_networking_mcp.mcp_servers import nac


def _resp(is_success=True, payload=None):
    resp = MagicMock()
    resp.is_success = is_success
    resp.json.return_value = dict(payload or {})
    resp.text = "{}"
    resp.status_code = 200 if is_success else 400
    return resp


def test_update_mpsk_registration_dry_run_returns_payload():
    result = nac.update_mpsk_registration(
        "mpsk-1", name="conf-room", network="Corp-Guest", dry_run=True
    )

    assert result["dry_run"] is True
    assert result["payload"] == {
        "input": {
            "id": "mpsk-1",
            "name": "conf-room",
            "network": "Corp-Guest",
            "passwordPolicy": "WORDS",
            "enable": True,
        }
    }


def test_update_mpsk_registration_sends_put_keyed_by_id(monkeypatch):
    client = MagicMock()
    client._request.return_value = _resp(payload={"id": "mpsk-1"})
    monkeypatch.setattr(nac, "get_client", lambda: client)

    result = nac.update_mpsk_registration("mpsk-1", name="conf-room", network="Corp-Guest")

    assert result["id"] == "mpsk-1"
    client._request.assert_called_once_with(
        "PUT",
        "/network-config/v1alpha1/cnac-named-mpsk-reg",
        json={
            "input": {
                "id": "mpsk-1",
                "name": "conf-room",
                "network": "Corp-Guest",
                "passwordPolicy": "WORDS",
                "enable": True,
            }
        },
    )


def test_update_mpsk_registration_surfaces_errors(monkeypatch):
    client = MagicMock()
    client._request.return_value = _resp(is_success=False, payload={"message": "bad"})
    monkeypatch.setattr(nac, "get_client", lambda: client)

    result = nac.update_mpsk_registration("mpsk-1", name="x", network="y")

    assert "errors" in result


def test_update_visitor_dry_run_returns_payload():
    result = nac.update_visitor(
        "visitor-1", display_name="Jane Doe", name="jdoe", email="jane@example.com", dry_run=True
    )

    assert result["dry_run"] is True
    assert result["payload"]["input"]["id"] == "visitor-1"
    assert result["payload"]["input"]["email"] == "jane@example.com"


def test_update_visitor_sends_put_keyed_by_id(monkeypatch):
    client = MagicMock()
    client._request.return_value = _resp(payload={"id": "visitor-1"})
    monkeypatch.setattr(nac, "get_client", lambda: client)

    result = nac.update_visitor("visitor-1", display_name="Jane Doe", name="jdoe")

    assert result["id"] == "visitor-1"
    client._request.assert_called_once_with(
        "PUT",
        "/network-config/v1alpha1/cnac-visitor",
        json={
            "input": {
                "id": "visitor-1",
                "displayName": "Jane Doe",
                "name": "jdoe",
                "enable": True,
            }
        },
    )


def test_list_mpsk_registrations_is_bounded_by_default(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"items": [{"id": f"mpsk-{i}"} for i in range(60)]}
    monkeypatch.setattr(nac, "get_client", lambda: client)

    result = nac.list_mpsk_registrations(limit=10)

    assert len(result["items"]) == 10
    assert result["_pagination"]["total"] == 60


def test_list_visitors_is_bounded_by_default(monkeypatch):
    client = MagicMock()
    client.get.return_value = {"items": [{"id": f"visitor-{i}"} for i in range(60)]}
    monkeypatch.setattr(nac, "get_client", lambda: client)

    result = nac.list_visitors(limit=5)

    assert len(result["items"]) == 5
    assert result["_pagination"]["total"] == 60
