"""Regression tests for finding #2: production writes must fail on
non-2xx/error results instead of being marked "applied".

Covers:
- hpe_networking_mcp.mcp_servers.shared.validate_write_result as a standalone helper (raw HTTP
  response/status, response envelopes with errors/success/status fields,
  and NOT rejecting legitimate empty 2xx bodies).
- hpe_networking_mcp.mcp_servers.aos8._aos8_migration_write_invoker's endpoint-invocation
  branch (raw client._request path) and curated-tool-invocation branch,
  both of which must now raise instead of returning a "success" result.
"""

from __future__ import annotations

import pytest

import hpe_networking_mcp.mcp_servers.aos8 as aos8
from hpe_networking_mcp.mcp_servers.shared import WriteResultError, validate_write_result
from hpe_networking_mcp.pipeline.aos8_target_adapters import Operation


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

    def _request(self, method, endpoint, json=None):
        return self._response


# ---------------------------------------------------------------------------
# validate_write_result: standalone helper coverage.
# ---------------------------------------------------------------------------


def test_validate_write_result_raises_on_raw_non_2xx_response():
    with pytest.raises(WriteResultError):
        validate_write_result(FakeResponse(500, {"ok": False}))


def test_validate_write_result_raises_on_raw_response_is_success_false():
    # is_success explicitly False even though status_code looks 2xx-ish --
    # the flag (not just the numeric range) must be honored.
    with pytest.raises(WriteResultError):
        validate_write_result(FakeResponse(200, {"ok": True}, is_success=False))


def test_validate_write_result_accepts_raw_2xx_response():
    validate_write_result(FakeResponse(201, {"id": "abc"}))


def test_validate_write_result_raises_on_nonempty_errors_list():
    with pytest.raises(WriteResultError):
        validate_write_result({"errors": ["bad request: duplicate name"]})


def test_validate_write_result_raises_on_error_string():
    with pytest.raises(WriteResultError):
        validate_write_result({"error": "conflict"})


def test_validate_write_result_raises_on_success_false():
    with pytest.raises(WriteResultError):
        validate_write_result({"success": False, "id": "abc"})


def test_validate_write_result_raises_on_failed_status_field():
    with pytest.raises(WriteResultError):
        validate_write_result({"status": "failed", "id": "abc"})


def test_validate_write_result_raises_on_out_of_range_status_code_field():
    with pytest.raises(WriteResultError):
        validate_write_result({"status_code": 404})


@pytest.mark.parametrize("result", [None, {}, [], "", "created"])
def test_validate_write_result_does_not_reject_legitimate_empty_or_plain_results(result):
    # Must never produce a false failure on a legitimate empty 2xx body or a
    # bare string without any failure marker.
    validate_write_result(result)


def test_validate_write_result_accepts_empty_errors_list():
    validate_write_result({"errors": [], "id": "abc"})


# ---------------------------------------------------------------------------
# _aos8_migration_write_invoker: production endpoint-invocation branch.
# ---------------------------------------------------------------------------


def _endpoint_operation(dry_run=False):
    arguments = {
        "method": "POST",
        "endpoint": "/network-config/v1alpha1/auth-servers/rad1",
        "data": {"name": "rad1", "type": "RADIUS"},
        "dry_run": dry_run,
    }
    return Operation(
        invocation="endpoint",
        name="central_api_request",
        arguments=arguments,
        method="POST",
        endpoint="/network-config/v1alpha1/auth-servers/rad1",
        payload={"name": "rad1", "type": "RADIUS"},
        provenance="test",
    )


def test_write_invoker_endpoint_branch_fails_on_non_2xx_status(monkeypatch):
    monkeypatch.setattr(aos8, "_platform_writes_allowed", lambda _p: True)
    monkeypatch.setattr(
        aos8, "get_client", lambda: FakeClient(FakeResponse(500, {"ok": False}))
    )
    with pytest.raises(WriteResultError):
        aos8._aos8_migration_write_invoker(_endpoint_operation(), confirmation=True)


def test_write_invoker_endpoint_branch_fails_on_2xx_error_envelope(monkeypatch):
    monkeypatch.setattr(aos8, "_platform_writes_allowed", lambda _p: True)
    monkeypatch.setattr(
        aos8,
        "get_client",
        lambda: FakeClient(FakeResponse(200, {"errors": ["profile-type invalid"]})),
    )
    with pytest.raises(WriteResultError):
        aos8._aos8_migration_write_invoker(_endpoint_operation(), confirmation=True)


def test_write_invoker_endpoint_branch_succeeds_on_clean_2xx(monkeypatch):
    monkeypatch.setattr(aos8, "_platform_writes_allowed", lambda _p: True)
    monkeypatch.setattr(
        aos8, "get_client", lambda: FakeClient(FakeResponse(201, {"id": "rad1"}))
    )
    result = aos8._aos8_migration_write_invoker(_endpoint_operation(), confirmation=True)
    assert result["id"] == "rad1"


def test_write_invoker_endpoint_branch_dry_run_never_calls_client(monkeypatch):
    def fail_get_client():
        raise AssertionError("dry_run must never reach get_client()")

    monkeypatch.setattr(aos8, "get_client", fail_get_client)
    result = aos8._aos8_migration_write_invoker(
        _endpoint_operation(dry_run=True), confirmation=False
    )
    assert result["dry_run"] is True


# ---------------------------------------------------------------------------
# _aos8_migration_write_invoker: curated-tool-invocation branch.
# ---------------------------------------------------------------------------


def _tool_operation(name="create_role", dry_run=False):
    return Operation(
        invocation="tool",
        name=name,
        arguments={"role_name": "employee", "dry_run": dry_run},
        provenance="test",
    )


def test_write_invoker_tool_branch_fails_on_error_envelope(monkeypatch):
    monkeypatch.setattr(aos8, "_platform_writes_allowed", lambda _p: True)

    def fake_create_role(**kwargs):
        return {"errors": ["role_name already exists"]}

    import hpe_networking_mcp.mcp_servers.config as config_tools

    monkeypatch.setattr(config_tools, "create_role", fake_create_role)
    with pytest.raises(WriteResultError):
        aos8._aos8_migration_write_invoker(_tool_operation(), confirmation=True)


def test_write_invoker_tool_branch_succeeds_on_clean_result(monkeypatch):
    monkeypatch.setattr(aos8, "_platform_writes_allowed", lambda _p: True)

    def fake_create_role(**kwargs):
        return {"name": "employee"}

    import hpe_networking_mcp.mcp_servers.config as config_tools

    monkeypatch.setattr(config_tools, "create_role", fake_create_role)
    result = aos8._aos8_migration_write_invoker(_tool_operation(), confirmation=True)
    assert result["name"] == "employee"
