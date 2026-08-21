"""Production-path regression tests for the AOS8 MCP boundary's Classic
Central target resolution and status-aware preflight-read translation.

These tests exercise the *real* `hpe_networking_mcp.mcp_servers.aos8` functions (not the
FakeBackend-based pure adapter tests in `test_aos8_target_adapters.py`):

- `_aos8_migration_read_invoker` must translate a production
  `CentralClient.get()` `httpx.HTTPStatusError` (which is raised on *every*
  non-2xx response, including a normal "this item does not exist yet" 404)
  into a status-carrying `ReadStatusError` instead of losing the status
  code.
- `_aos8_migration_classic_target_resolver` must resolve Classic Central
  targets directly from the caller-declared `scope_name`, and must never
  call the New Central `/scopes` lookup (`list_scopes`/`get_global_scope_id`)
  that `_aos8_migration_scope_resolver` uses.
"""

from __future__ import annotations

import httpx
import pytest

import hpe_networking_mcp.mcp_servers.aos8 as aos8
from hpe_networking_mcp.pipeline.aos8_target_adapters import (
    ClassicCentralAdapter,
    Operation,
    ReadStatusError,
    TargetContext,
    TargetType,
)


class _FakeCentralClient:
    """Stands in for `hpe_networking_mcp.pipeline.clients.central_client.CentralClient`, whose
    production `.get()` calls `response.raise_for_status()` and therefore
    raises `httpx.HTTPStatusError` on any non-2xx status -- including a
    normal, expected 404 for "not found yet"."""

    def __init__(self, status_code: int):
        self._status_code = status_code

    def get(self, endpoint: str):
        request = httpx.Request("GET", f"https://example.invalid{endpoint}")
        response = httpx.Response(self._status_code, request=request)
        if not 200 <= self._status_code < 300:
            raise httpx.HTTPStatusError(
                f"HTTP {self._status_code}", request=request, response=response
            )
        return response


def _read_operation(endpoint: str = "/configuration/full_wlan/Branch/Guest") -> Operation:
    return Operation(
        invocation="endpoint",
        name="central_api_read",
        arguments={},
        method="GET",
        endpoint=endpoint,
        match_identifier="Guest",
    )


# --------------------------------------------------------------------------
# Finding #1: production-path status-aware preflight read
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [404, 401, 403, 500, 503])
def test_read_invoker_translates_production_http_status_error(monkeypatch, status_code):
    monkeypatch.setattr(aos8, "get_client", lambda: _FakeCentralClient(status_code))
    with pytest.raises(ReadStatusError) as excinfo:
        aos8._aos8_migration_read_invoker(_read_operation())
    assert excinfo.value.status_code == status_code


def test_read_invoker_returns_response_on_success(monkeypatch):
    monkeypatch.setattr(aos8, "get_client", lambda: _FakeCentralClient(200))
    response = aos8._aos8_migration_read_invoker(_read_operation())
    assert response.status_code == 200


def test_policy_tools_are_exposed_by_production_migration_dispatchers(monkeypatch):
    monkeypatch.setattr(
        "hpe_networking_mcp.mcp_servers.config.list_gw_policies",
        lambda **arguments: {"read": arguments},
    )
    monkeypatch.setattr(
        "hpe_networking_mcp.mcp_servers.config.create_gw_policy",
        lambda **arguments: {"dry_run": True, "create": arguments},
    )
    monkeypatch.setattr(
        "hpe_networking_mcp.mcp_servers.config.delete_gw_policy",
        lambda **arguments: {"dry_run": True, "delete": arguments},
    )

    read_result = aos8._aos8_migration_read_invoker(
        Operation(
            invocation="tool",
            name="list_gw_policies",
            arguments={"limit": 50, "offset": 0},
        )
    )
    create_result = aos8._aos8_migration_write_invoker(
        Operation(
            invocation="tool",
            name="create_gw_policy",
            arguments={
                "name": "hpe-mcp-lab-policy",
                "rules": [],
                "dry_run": True,
            },
        ),
        confirmation=False,
    )
    delete_result = aos8._aos8_migration_write_invoker(
        Operation(
            invocation="tool",
            name="delete_gw_policy",
            arguments={"name": "hpe-mcp-lab-policy", "dry_run": True},
        ),
        confirmation=False,
    )

    assert read_result["read"] == {"limit": 50, "offset": 0}
    assert create_result["create"]["name"] == "hpe-mcp-lab-policy"
    assert delete_result["delete"]["name"] == "hpe-mcp-lab-policy"


def test_server_group_tools_are_exposed_by_production_migration_dispatchers(
    monkeypatch,
):
    monkeypatch.setattr(
        "hpe_networking_mcp.mcp_servers.nac.get_server_group",
        lambda **arguments: {"read": arguments},
    )
    monkeypatch.setattr(
        "hpe_networking_mcp.mcp_servers.nac.create_server_group",
        lambda **arguments: {"dry_run": True, "create": arguments},
    )
    monkeypatch.setattr(
        "hpe_networking_mcp.mcp_servers.nac.delete_server_group",
        lambda **arguments: {"dry_run": True, "delete": arguments},
    )

    read_result = aos8._aos8_migration_read_invoker(
        Operation(
            invocation="tool",
            name="get_server_group",
            arguments={"name": "hpe-mcp-lab-sg"},
        )
    )
    create_result = aos8._aos8_migration_write_invoker(
        Operation(
            invocation="tool",
            name="create_server_group",
            arguments={
                "name": "hpe-mcp-lab-sg",
                "server_names": ["radius-1"],
                "dry_run": True,
            },
        ),
        confirmation=False,
    )
    delete_result = aos8._aos8_migration_write_invoker(
        Operation(
            invocation="tool",
            name="delete_server_group",
            arguments={"name": "hpe-mcp-lab-sg", "dry_run": True},
        ),
        confirmation=False,
    )

    assert read_result["read"]["name"] == "hpe-mcp-lab-sg"
    assert create_result["create"]["server_names"] == ["radius-1"]
    assert delete_result["delete"]["name"] == "hpe-mcp-lab-sg"


def _classic_context(scope_name: str) -> TargetContext:
    return TargetContext(
        target_type=TargetType.CLASSIC_CENTRAL,
        scope_id=None,
        scope_name=scope_name,
        persona="CAMPUS_AP",
    )


def test_read_invoker_404_classifies_as_absent_end_to_end(monkeypatch):
    # End-to-end: production read invoker + real ClassicCentralAdapter +
    # real Classic target resolver, wired the same way
    # `_aos8_migration_orchestrator()` wires them, confirming a 404 preflight
    # read is treated as "safe to create" (not "blocked").
    monkeypatch.setattr(aos8, "get_client", lambda: _FakeCentralClient(404))
    adapter = ClassicCentralAdapter(
        _classic_context("Branch Group"),
        scope_resolver=aos8._aos8_migration_classic_target_resolver,
        persona_validator=aos8._aos8_migration_persona_validator,
        read_invoker=aos8._aos8_migration_read_invoker,
        write_invoker=aos8._aos8_migration_write_invoker,
        writes_enabled=lambda _target: True,
    )
    wlan = {
        "object_type": "wlan",
        "identifier": "Guest",
        "payload": {
            "name": "Guest",
            "essid": "Guest",
            "vlan": 20,
            "aaa_profile": None,
            "security": {
                "mode": "open",
                "opmode": "open",
                "ambiguous": False,
                "aaa_profile": None,
                "dot1x_auth_profile": None,
                "mac_auth_profile": None,
                "passphrase_present": False,
                "psk_hexkey_present": False,
                "wpa3_transition": False,
                "evidence": [],
            },
        },
        "dependencies": [],
        "apply_order": 10,
        "unsupported_fields": {
            "ssid_profile.opmode": "open",
            "virtual_ap.forward_mode": "bridge",
        },
        "requires_secret_input": False,
        "secret_fields": [],
        "warnings": [],
    }
    preview = adapter.preview([wlan])
    action = preview["operations"][0]
    assert action["status"] == "ready"
    assert action["conflict"] == "absent"
    assert action["operations"][0]["method"] == "POST"


def test_read_invoker_401_classifies_as_unsupported_end_to_end(monkeypatch):
    monkeypatch.setattr(aos8, "get_client", lambda: _FakeCentralClient(401))
    adapter = ClassicCentralAdapter(
        _classic_context("Branch Group"),
        scope_resolver=aos8._aos8_migration_classic_target_resolver,
        persona_validator=aos8._aos8_migration_persona_validator,
        read_invoker=aos8._aos8_migration_read_invoker,
        write_invoker=aos8._aos8_migration_write_invoker,
        writes_enabled=lambda _target: True,
    )
    wlan = {
        "object_type": "wlan",
        "identifier": "Guest",
        "payload": {
            "name": "Guest",
            "essid": "Guest",
            "vlan": 20,
            "aaa_profile": None,
            "security": {
                "mode": "open",
                "opmode": "open",
                "ambiguous": False,
                "aaa_profile": None,
                "dot1x_auth_profile": None,
                "mac_auth_profile": None,
                "passphrase_present": False,
                "psk_hexkey_present": False,
                "wpa3_transition": False,
                "evidence": [],
            },
        },
        "dependencies": [],
        "apply_order": 10,
        "unsupported_fields": {
            "ssid_profile.opmode": "open",
            "virtual_ap.forward_mode": "bridge",
        },
        "requires_secret_input": False,
        "secret_fields": [],
        "warnings": [],
    }
    preview = adapter.preview([wlan])
    action = preview["operations"][0]
    assert action["status"] == "unsupported"
    assert "401" in action["unsupported_warnings"][0]


# --------------------------------------------------------------------------
# Finding #4: dedicated Classic target resolver, isolated from New Central
# `/scopes` lookups
# --------------------------------------------------------------------------


def _forbid_scopes_lookup(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError(
            "New Central /scopes lookup must never be called for a Classic "
            "Central target."
        )

    monkeypatch.setattr("hpe_networking_mcp.mcp_servers.monitoring.list_scopes", _boom)
    monkeypatch.setattr("hpe_networking_mcp.mcp_servers.monitoring.get_global_scope_id", _boom)


@pytest.mark.parametrize(
    "scope_name",
    ["Branch Group", "12345", "550e8400-e29b-41d4-a716-446655440000", "CN12345678"],
)
def test_classic_target_resolver_never_calls_new_central_scopes_api(
    monkeypatch, scope_name
):
    _forbid_scopes_lookup(monkeypatch)
    context = _classic_context(scope_name)
    scope_id, resolved_name = aos8._aos8_migration_classic_target_resolver(context)
    assert resolved_name == scope_name
    assert scope_id  # falls back to scope_name when scope_id is unset


def test_classic_target_resolver_rejects_missing_scope_name(monkeypatch):
    _forbid_scopes_lookup(monkeypatch)
    context = _classic_context("")
    with pytest.raises(ValueError, match="explicit scope_name"):
        aos8._aos8_migration_classic_target_resolver(context)


def test_classic_target_resolver_does_not_infer_from_bare_scope_id(monkeypatch):
    # A caller must never be able to feed a New Central scope_id into the
    # Classic path implicitly -- only an explicitly declared scope_name is
    # accepted as the Classic target string.
    _forbid_scopes_lookup(monkeypatch)
    context = TargetContext(
        target_type=TargetType.CLASSIC_CENTRAL,
        scope_id="99999",
        scope_name=None,
        persona="CAMPUS_AP",
    )
    with pytest.raises(ValueError, match="explicit scope_name"):
        aos8._aos8_migration_classic_target_resolver(context)


def test_adapter_factory_selects_classic_resolver_for_classic_target(monkeypatch):
    _forbid_scopes_lookup(monkeypatch)
    service = aos8._aos8_migration_orchestrator()
    adapter = service.adapter_factory(_classic_context("12345"))
    assert adapter.context.scope_name == "12345"


def test_adapter_factory_selects_new_central_resolver_for_new_central_target(
    monkeypatch,
):
    calls = {"count": 0}

    def fake_list_scopes(full_list=True):
        calls["count"] += 1
        return {"items": [{"scope_id": "100", "scope_name": "Branch"}]}

    monkeypatch.setattr("hpe_networking_mcp.mcp_servers.monitoring.list_scopes", fake_list_scopes)
    service = aos8._aos8_migration_orchestrator()
    context = TargetContext(
        target_type=TargetType.NEW_CENTRAL,
        scope_id="100",
        scope_name="Branch",
        persona="CAMPUS_AP",
    )
    adapter = service.adapter_factory(context)
    assert adapter.context.scope_name == "Branch"
    assert calls["count"] == 1


# --------------------------------------------------------------------------
# aos8-verification-fixes item 1: `list_config_assignments` production
# dispatcher path (previously omitted from `_aos8_migration_read_invoker`'s
# tool allowlist, which made every production role config-assignment
# verification raise `ValueError: Unapproved migration read tool`).
# --------------------------------------------------------------------------


class _FakeConfigAssignmentsClient:
    """Stands in for `hpe_networking_mcp.pipeline.clients.central_client.CentralClient` at the
    `hpe_networking_mcp.mcp_servers.config.list_config_assignments` boundary -- captures the
    exact `params` the curated tool sends and returns a real
    `httpx.Response` (matching production's `resp_json`/`.is_success`
    usage) rather than a hand-shaped dict."""

    def __init__(self, body: dict):
        self._body = body
        self.calls: list[tuple[str, str, dict | None]] = []

    def _request(self, method: str, endpoint: str, params=None):
        self.calls.append((method, endpoint, params))
        request = httpx.Request("GET", f"https://example.invalid{endpoint}")
        return httpx.Response(200, json=self._body, request=request)


def test_read_invoker_dispatches_list_config_assignments_production_path(
    monkeypatch,
):
    """The production dispatcher must accept `list_config_assignments` (not
    just the test-only `FakeBackend`), route it to the real curated tool,
    and pass scope/device-function/profile-type through to the real
    `CentralClient` boundary unchanged, bounded/parsed the same way
    `list_roles` already is.

    aos8-verification-final-fixes item 1: the production mapping declares
    an explicit bounded `limit`/`offset` page -- never `full_list=True` --
    so this exercises the same arguments
    `aos8_target_adapters.NewCentralAdapter._map_role` actually sends."""
    fake_client = _FakeConfigAssignmentsClient(
        {
            "items": [
                {
                    "scope-id": "100",
                    "device-function": "CAMPUS_AP",
                    "profile-type": "roles",
                    "profile-instance": "employee",
                }
            ]
        }
    )
    monkeypatch.setattr("hpe_networking_mcp.mcp_servers.config.get_client", lambda: fake_client)
    operation = Operation(
        invocation="tool",
        name="list_config_assignments",
        arguments={
            "scope_id": "100",
            "device_function": "CAMPUS_AP",
            "profile_type": "roles",
            "limit": 50,
            "offset": 0,
        },
        match_identifier="employee",
    )

    result = aos8._aos8_migration_read_invoker(operation)

    # profile_type is not a server-side query filter in the committed
    # config-assignment spec (GET only accepts scope-id/device-function);
    # `list_config_assignments` sends just those two through and applies
    # profile_type client-side to the returned "config-assignment" list.
    assert fake_client.calls == [
        (
            "GET",
            "/network-config/v1alpha1/config-assignments",
            {
                "scope-id": "100",
                "device-function": "CAMPUS_AP",
            },
        )
    ]
    assert result["items"][0]["profile-instance"] == "employee"


def _new_central_context() -> TargetContext:
    return TargetContext(
        target_type=TargetType.NEW_CENTRAL,
        scope_id="100",
        scope_name="Branch",
        persona="CAMPUS_AP",
    )


def _stub_list_scopes(monkeypatch):
    monkeypatch.setattr(
        "hpe_networking_mcp.mcp_servers.monitoring.list_scopes",
        lambda full_list=True: {
            "items": [{"scope_id": "100", "scope_name": "Branch"}]
        },
    )


def test_role_assignment_verification_via_production_dispatcher_end_to_end(
    monkeypatch,
):
    """End-to-end: the real `NewCentralAdapter` (via
    `_aos8_migration_orchestrator()`'s own `adapter_factory`, exactly as
    production wires it) + the real `_aos8_migration_read_invoker` + the
    real `config_tools.list_config_assignments` + the real
    `hpe_networking_mcp.pipeline.aos8_migration_orchestrator._verify_assignment` must
    together confirm a role's config-assignment, never routing through
    `FakeBackend`."""
    from hpe_networking_mcp.pipeline.aos8_migration_orchestrator import _verify_assignment

    _stub_list_scopes(monkeypatch)
    fake_client = _FakeConfigAssignmentsClient(
        {
            "items": [
                {
                    "scope-id": "100",
                    "device-function": "CAMPUS_AP",
                    "profile-type": "roles",
                    "profile-instance": "employee",
                }
            ]
        }
    )
    monkeypatch.setattr("hpe_networking_mcp.mcp_servers.config.get_client", lambda: fake_client)

    service = aos8._aos8_migration_orchestrator()
    adapter = service.adapter_factory(_new_central_context())
    role_candidate = {
        "object_type": "role",
        "identifier": "employee",
        "payload": {"policies": ["allowall"], "vlan": 20},
    }
    action = adapter._map_candidate(role_candidate)

    result = _verify_assignment(adapter, action)

    assert fake_client.calls[0][0] == "GET"
    assert fake_client.calls[0][1] == "/network-config/v1alpha1/config-assignments"
    assert result["status"] == "verified"


def test_role_assignment_verification_production_dispatcher_reports_mismatch(
    monkeypatch,
):
    """Same production-dispatcher path as above, but the returned
    assignment binds a different device-function -- must surface as a
    mismatch, not a false "verified"."""
    from hpe_networking_mcp.pipeline.aos8_migration_orchestrator import _verify_assignment

    _stub_list_scopes(monkeypatch)
    fake_client = _FakeConfigAssignmentsClient(
        {
            "items": [
                {
                    "scope-id": "100",
                    "device-function": "MOBILITY_GW",
                    "profile-type": "roles",
                    "profile-instance": "employee",
                }
            ]
        }
    )
    monkeypatch.setattr("hpe_networking_mcp.mcp_servers.config.get_client", lambda: fake_client)

    service = aos8._aos8_migration_orchestrator()
    adapter = service.adapter_factory(_new_central_context())
    role_candidate = {
        "object_type": "role",
        "identifier": "employee",
        "payload": {"policies": ["allowall"], "vlan": 20},
    }
    action = adapter._map_candidate(role_candidate)

    result = _verify_assignment(adapter, action)

    assert result["status"] != "verified"


# --------------------------------------------------------------------------
# aos8-verification-final-fixes item 1: production role/config-assignment
# verification reads must never request `full_list=True` -- only an
# explicit bounded `limit`/`offset` page, with verification's own paging
# (`MAX_VERIFICATION_EXTRA_PAGES`/`MAX_VERIFICATION_TOTAL_ITEMS`) owning
# any subsequent pages under a hard cap.
# --------------------------------------------------------------------------


def test_role_assignment_verification_production_dispatcher_bounds_1000_item_backend(
    monkeypatch,
):
    """A production account whose config-assignment collection has 1000
    entries must never be requested/materialized in a single call: the
    real `NewCentralAdapter` mapping, the real `_aos8_migration_read_invoker`
    dispatcher, and the real `config_tools.list_config_assignments` curated
    tool must together request only bounded pages (`limit`/`offset`, never
    `full_list=True`), with the number of backend calls capped at
    `1 + MAX_VERIFICATION_EXTRA_PAGES` and the total items inspected capped
    at `MAX_VERIFICATION_TOTAL_ITEMS` -- proven here by placing the
    candidate's own matching entry beyond that cap, which must never be
    reached: the real backend response is 1000 entries every single call
    (this endpoint has no server-side `limit`/`offset` support in
    production either -- `list_config_assignments` sends only
    scope-id/device-function query params, per the committed
    config-assignment spec; profile-type is filtered client-side), so
    finding the match at all would mean the cap was not enforced.
    """
    from hpe_networking_mcp.pipeline.aos8_migration_orchestrator import (
        MAX_VERIFICATION_EXTRA_PAGES,
        MAX_VERIFICATION_TOTAL_ITEMS,
        _verify_assignment,
    )

    _stub_list_scopes(monkeypatch)
    # The candidate's own identity ("employee") is placed at index 500,
    # deep beyond any page/cap this process could safely fetch, so a
    # correct, bounded implementation can never report it "verified".
    body = {
        "items": [
            {
                "scope-id": "100",
                "device-function": "CAMPUS_AP",
                "profile-type": "roles",
                "profile-instance": "employee" if i == 500 else f"role-{i}",
            }
            for i in range(1000)
        ]
    }
    fake_client = _FakeConfigAssignmentsClient(body)
    monkeypatch.setattr("hpe_networking_mcp.mcp_servers.config.get_client", lambda: fake_client)

    from hpe_networking_mcp.mcp_servers import config as config_tools

    real_list_config_assignments = config_tools.list_config_assignments
    page_item_counts: list[int] = []

    def _counting_list_config_assignments(*args, **kwargs):
        result = real_list_config_assignments(*args, **kwargs)
        page_item_counts.append(len(result.get("items", [])))
        return result

    monkeypatch.setattr(
        "hpe_networking_mcp.mcp_servers.config.list_config_assignments",
        _counting_list_config_assignments,
    )

    service = aos8._aos8_migration_orchestrator()
    adapter = service.adapter_factory(_new_central_context())
    role_candidate = {
        "object_type": "role",
        "identifier": "employee",
        "payload": {"policies": ["allowall"], "vlan": 20},
    }
    action = adapter._map_candidate(role_candidate)

    # The production mapping itself must declare an explicit bounded page,
    # never full_list=True.
    assignment_arguments = action.assignment_read_operation.arguments
    assert assignment_arguments.get("full_list") is not True
    page_size = assignment_arguments["limit"]
    assert isinstance(page_size, int) and 0 < page_size <= 200

    result = _verify_assignment(adapter, action)

    max_calls = 1 + MAX_VERIFICATION_EXTRA_PAGES
    # Only a bounded number of backend calls total -- never one call per
    # entry, and never a single `full_list=True`-shaped call that received
    # (and had to inspect) all 1000 entries at once.
    assert len(fake_client.calls) <= max_calls
    assert len(page_item_counts) == len(fake_client.calls)
    # Every individual call returned only its own bounded page -- never
    # the whole 1000-item backend response.
    assert all(count <= page_size for count in page_item_counts)
    # The aggregate across every page this process actually read never
    # exceeds the hard total-items cap.
    assert sum(page_item_counts) <= MAX_VERIFICATION_TOTAL_ITEMS
    # The match was never reached (it sits beyond the cap), so this must
    # never be reported as a definitive "verified" -- absence/uniqueness
    # cannot be concluded from a still-truncated collection.
    assert result["status"] != "verified"
