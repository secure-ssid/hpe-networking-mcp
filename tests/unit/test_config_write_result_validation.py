"""Regression tests for consistent write-result validation on the non-AOS8
config tools touched by this branch: create_role, update_role, delete_role,
create_config_assignment, delete_config_assignment, delete_overlay_ssid.

These tools previously used an ad hoc
`result.setdefault("errors", []).append(...)` pattern that:
  * crashes if the parsed response body is not a dict (or is a dict without
    list-shaped `errors`), since `.setdefault("errors", [])` on a non-Mapping
    raises `AttributeError`, and `.append(...)` on a non-list `errors` value
    raises `AttributeError` too;
  * never raises on a non-2xx response -- callers that only check "did this
    raise?" see a 2xx-shaped dict with a buried `errors` entry and treat the
    write as applied.

They now call `hpe_networking_mcp.mcp_servers.shared.validate_write_result` on both the raw
response and the parsed envelope, matching the pattern already used by
`hpe_networking_mcp.mcp_servers.aos8._aos8_migration_write_invoker`.
"""

from __future__ import annotations

import pytest

from hpe_networking_mcp.mcp_servers import config
from hpe_networking_mcp.mcp_servers.shared import WriteResultError, validate_write_result


class FakeResponse:
    def __init__(self, status_code, json_body=None, is_success=None):
        self.status_code = status_code
        self._json_body = json_body
        self.is_success = (200 <= status_code < 300) if is_success is None else is_success

    def json(self):
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body

    @property
    def text(self):
        return "" if self._json_body is None else str(self._json_body)


class FakeClient:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def _request(self, method, endpoint, json=None, params=None):
        self.calls.append((method, endpoint, json, params))
        return self._response


# ---------------------------------------------------------------------------
# validate_write_result: dict-shaped `errors` coverage (extends the
# list/str coverage already exercised in
# test_aos8_migration_write_result_validation.py).
# ---------------------------------------------------------------------------


def test_validate_write_result_raises_on_nonempty_errors_dict():
    with pytest.raises(WriteResultError):
        validate_write_result({"errors": {"role_name": "already exists"}})


def test_validate_write_result_accepts_empty_errors_dict():
    validate_write_result({"errors": {}, "id": "abc"})


# ---------------------------------------------------------------------------
# create_role / update_role / delete_role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name, kwargs",
    [
        ("create_role", {"name": "employee"}),
        ("update_role", {"name": "employee"}),
    ],
)
def test_role_write_raises_on_non_2xx_response(monkeypatch, tool_name, kwargs):
    fake_client = FakeClient(FakeResponse(500, {"ok": False}))
    monkeypatch.setattr(config, "get_client", lambda: fake_client)
    tool = getattr(config, tool_name)
    with pytest.raises(WriteResultError):
        tool(**kwargs)


@pytest.mark.parametrize(
    "tool_name, kwargs",
    [
        ("create_role", {"name": "employee"}),
        ("update_role", {"name": "employee"}),
    ],
)
def test_role_write_raises_on_2xx_error_envelope(monkeypatch, tool_name, kwargs):
    fake_client = FakeClient(FakeResponse(200, {"errors": ["name already exists"]}))
    monkeypatch.setattr(config, "get_client", lambda: fake_client)
    tool = getattr(config, tool_name)
    with pytest.raises(WriteResultError):
        tool(**kwargs)


@pytest.mark.parametrize(
    "tool_name, kwargs",
    [
        ("create_role", {"name": "employee"}),
        ("update_role", {"name": "employee"}),
    ],
)
def test_role_write_succeeds_on_clean_2xx(monkeypatch, tool_name, kwargs):
    fake_client = FakeClient(FakeResponse(201, {"name": "employee"}))
    monkeypatch.setattr(config, "get_client", lambda: fake_client)
    tool = getattr(config, tool_name)
    result = tool(**kwargs)
    assert result["name"] == "employee"


@pytest.mark.parametrize(
    "tool_name, kwargs",
    [
        ("create_role", {"name": "employee"}),
        ("update_role", {"name": "employee"}),
    ],
)
def test_role_write_accepts_legitimate_empty_2xx_body(monkeypatch, tool_name, kwargs):
    # A non-JSON/empty 2xx body falls back to resp_json's status_code/text
    # metadata shape; this must not be misread as a failure.
    fake_client = FakeClient(FakeResponse(204, None))
    monkeypatch.setattr(config, "get_client", lambda: fake_client)
    tool = getattr(config, tool_name)
    result = tool(**kwargs)
    assert result["status_code"] == 204


def test_delete_role_raises_on_non_2xx_response(monkeypatch):
    fake_client = FakeClient(FakeResponse(409, {"errors": ["role in use"]}))
    monkeypatch.setattr(config, "get_client", lambda: fake_client)
    with pytest.raises(WriteResultError):
        config.delete_role(name="employee")


def test_delete_role_raises_on_success_false_envelope(monkeypatch):
    fake_client = FakeClient(FakeResponse(200, {"success": False}))
    monkeypatch.setattr(config, "get_client", lambda: fake_client)
    with pytest.raises(WriteResultError):
        config.delete_role(name="employee")


def test_delete_role_succeeds_on_clean_2xx(monkeypatch):
    fake_client = FakeClient(FakeResponse(200, {"name": "employee"}))
    monkeypatch.setattr(config, "get_client", lambda: fake_client)
    result = config.delete_role(name="employee")
    assert result["name"] == "employee"


def test_delete_role_dry_run_never_calls_client(monkeypatch):
    def fail_get_client():
        raise AssertionError("dry_run must never reach get_client()")

    monkeypatch.setattr(config, "get_client", fail_get_client)
    result = config.delete_role(name="employee", dry_run=True)
    assert result == {"dry_run": True, "name": "employee"}


# ---------------------------------------------------------------------------
# create_config_assignment / delete_config_assignment
# ---------------------------------------------------------------------------


_ASSIGNMENT_KWARGS = {
    "scope_id": "1",
    "device_function": "CAMPUS_AP",
    "profile_type": "roles",
    "profile_instance": "employee",
}


@pytest.mark.parametrize("tool_name", ["create_config_assignment", "delete_config_assignment"])
def test_config_assignment_raises_on_non_2xx_response(monkeypatch, tool_name):
    fake_client = FakeClient(FakeResponse(404, {"errors": ["not found"]}))
    monkeypatch.setattr(config, "get_client", lambda: fake_client)
    tool = getattr(config, tool_name)
    with pytest.raises(WriteResultError):
        tool(**_ASSIGNMENT_KWARGS)


@pytest.mark.parametrize("tool_name", ["create_config_assignment", "delete_config_assignment"])
def test_config_assignment_raises_on_dict_shaped_errors(monkeypatch, tool_name):
    fake_client = FakeClient(FakeResponse(200, {"errors": {"profile_instance": "unknown"}}))
    monkeypatch.setattr(config, "get_client", lambda: fake_client)
    tool = getattr(config, tool_name)
    with pytest.raises(WriteResultError):
        tool(**_ASSIGNMENT_KWARGS)


@pytest.mark.parametrize("tool_name", ["create_config_assignment", "delete_config_assignment"])
def test_config_assignment_succeeds_on_clean_2xx(monkeypatch, tool_name):
    fake_client = FakeClient(FakeResponse(200, {"scope_id": "1"}))
    monkeypatch.setattr(config, "get_client", lambda: fake_client)
    tool = getattr(config, tool_name)
    result = tool(**_ASSIGNMENT_KWARGS)
    assert result["scope_id"] == "1"


@pytest.mark.parametrize("tool_name", ["create_config_assignment", "delete_config_assignment"])
def test_config_assignment_dry_run_never_calls_client(monkeypatch, tool_name):
    def fail_get_client():
        raise AssertionError("dry_run must never reach get_client()")

    monkeypatch.setattr(config, "get_client", fail_get_client)
    tool = getattr(config, tool_name)
    result = tool(dry_run=True, **_ASSIGNMENT_KWARGS)
    assert result["dry_run"] is True


def test_create_config_assignment_uses_collection_body_contract(monkeypatch):
    fake_client = FakeClient(FakeResponse(201, {"status": "success"}))
    monkeypatch.setattr(config, "get_client", lambda: fake_client)

    result = config.create_config_assignment(**_ASSIGNMENT_KWARGS)

    assert result["status"] == "success"
    assert fake_client.calls == [
        (
            "POST",
            "/network-config/v1alpha1/config-assignments",
            {
                "config-assignment": [
                    {
                        "scope-id": "1",
                        "device-function": "CAMPUS_AP",
                        "profile-type": "roles",
                        "profile-instance": "employee",
                    }
                ]
            },
            None,
        )
    ]


def test_create_config_assignment_dry_run_previews_collection_body():
    result = config.create_config_assignment(
        dry_run=True,
        **_ASSIGNMENT_KWARGS,
    )

    assert result == {
        "dry_run": True,
        "endpoint": "/network-config/v1alpha1/config-assignments",
        "payload": {
            "config-assignment": [
                {
                    "scope-id": "1",
                    "device-function": "CAMPUS_AP",
                    "profile-type": "roles",
                    "profile-instance": "employee",
                }
            ]
        },
    }


# ---------------------------------------------------------------------------
# delete_overlay_ssid
# ---------------------------------------------------------------------------


def test_delete_overlay_ssid_raises_on_non_2xx_response(monkeypatch):
    fake_client = FakeClient(FakeResponse(500, {"ok": False}))
    monkeypatch.setattr(config, "get_client", lambda: fake_client)
    with pytest.raises(WriteResultError):
        config.delete_overlay_ssid(profile_name="corp-overlay")


def test_delete_overlay_ssid_raises_on_error_string(monkeypatch):
    fake_client = FakeClient(FakeResponse(200, {"error": "profile still bound"}))
    monkeypatch.setattr(config, "get_client", lambda: fake_client)
    with pytest.raises(WriteResultError):
        config.delete_overlay_ssid(profile_name="corp-overlay")


def test_delete_overlay_ssid_succeeds_on_clean_2xx(monkeypatch):
    fake_client = FakeClient(FakeResponse(200, {"profile_name": "corp-overlay"}))
    monkeypatch.setattr(config, "get_client", lambda: fake_client)
    result = config.delete_overlay_ssid(profile_name="corp-overlay")
    assert result["profile_name"] == "corp-overlay"


def test_delete_overlay_ssid_dry_run_never_calls_client(monkeypatch):
    def fail_get_client():
        raise AssertionError("dry_run must never reach get_client()")

    monkeypatch.setattr(config, "get_client", fail_get_client)
    result = config.delete_overlay_ssid(profile_name="corp-overlay", dry_run=True)
    assert result["dry_run"] is True
