"""Review-fix regression: 0.5.0 must not break 0.4.0 positional call sites for
the public `hpe_networking_mcp.mcp_servers.config.build_underlay_ssid` / `build_overlay_ssid`
MCP tools.

At 0.4.0 (commit 1f79256) the last positional parameter on both tools was
`dry_run`. The 0.5.0 branch inserted the new `wpa3_transition` field *before*
`dry_run`, shifting `dry_run` one slot to the right -- an old positional
caller that passed `True` for `dry_run` would silently bind that value to
`wpa3_transition` instead, leaving `dry_run` at its `False` default and
letting the call fall through to real write APIs.

These tests assert:
  * every 0.4.0 positional parameter keeps its exact 0.4.0 index (in
    particular `dry_run` is still the last positional parameter);
  * `wpa3_transition` is keyword-only, so it can never shift a positional
    argument again;
  * an end-to-end call using the *exact* 0.4.0 positional argument shape
    (with `True` in `dry_run`'s 0.4.0 slot) still sets `dry_run=True` and
    never reaches a write (POST/PUT/PATCH/DELETE) API.
"""

from __future__ import annotations

import inspect

import pytest

from hpe_networking_mcp.mcp_servers import config


class WriteAttemptedError(AssertionError):
    """Raised by WriteGuardClient whenever a write verb is invoked."""


class WriteGuardClient:
    """Fake Central client that fails the test if any write verb is called.

    `dry_run=True` must short-circuit before any of POST/PUT/PATCH/DELETE are
    issued. GET is allowed (used by read-only scope/lookup helpers).
    """

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def get(self, endpoint, params=None):
        self.calls.append(("GET", endpoint))
        return {"scope-map": [{"persona": "SERVICE_PERSONA", "scope-id": 1}]}

    def _request(self, method, endpoint, json=None, params=None):
        self.calls.append((method, endpoint))
        if method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
            raise WriteAttemptedError(
                f"dry_run must never reach a write API — got {method} {endpoint}"
            )
        return _FakeResponse()

    def post(self, endpoint, data=None):
        self.calls.append(("POST", endpoint))
        raise WriteAttemptedError(f"dry_run must never reach a write API — got POST {endpoint}")

    def put(self, endpoint, data=None):
        self.calls.append(("PUT", endpoint))
        raise WriteAttemptedError(f"dry_run must never reach a write API — got PUT {endpoint}")

    def patch(self, endpoint, data=None):
        self.calls.append(("PATCH", endpoint))
        raise WriteAttemptedError(f"dry_run must never reach a write API — got PATCH {endpoint}")

    def delete(self, endpoint):
        self.calls.append(("DELETE", endpoint))
        raise WriteAttemptedError(f"dry_run must never reach a write API — got DELETE {endpoint}")


class _FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {}


# ---------------------------------------------------------------------------
# Signature shape — every 0.4.0 positional parameter keeps its exact index.
# ---------------------------------------------------------------------------


def test_build_underlay_ssid_matches_040_positional_signature():
    params = list(inspect.signature(config.build_underlay_ssid).parameters.values())
    positional = [
        p.name for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    assert positional == [
        "ssid_name",
        "scope_id",
        "persona",
        "opmode",
        "passphrase",
        "vlan_id",
        "vlan_ids",
        "mac_auth_server_group",
        "default_role",
        "dry_run",
    ]
    keyword_only = {p.name for p in params if p.kind is p.KEYWORD_ONLY}
    assert keyword_only == {"wpa3_transition"}


def test_build_overlay_ssid_matches_040_positional_signature():
    params = list(inspect.signature(config.build_overlay_ssid).parameters.values())
    positional = [
        p.name for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    assert positional == [
        "ssid_name",
        "scope_id",
        "cluster_name",
        "cluster_scope_id",
        "vlan_ids",
        "opmode",
        "passphrase",
        "mac_auth_server_group",
        "policy_name",
        "dry_run",
    ]
    keyword_only = {p.name for p in params if p.kind is p.KEYWORD_ONLY}
    assert keyword_only == {"wpa3_transition"}


# ---------------------------------------------------------------------------
# End-to-end — an exact 0.4.0 positional call, with `True` in dry_run's
# 0.4.0 slot, must set dry_run=True and never issue a write.
# ---------------------------------------------------------------------------


def test_build_underlay_ssid_040_positional_call_still_dry_runs(monkeypatch):
    client = WriteGuardClient()
    monkeypatch.setattr(config, "get_client", lambda: client)

    # Exact 0.4.0 positional call shape: ssid_name, scope_id, persona,
    # opmode, passphrase, vlan_id, vlan_ids, mac_auth_server_group,
    # default_role, dry_run. `mac_auth_server_group` defaults to
    # "sys_central_nac" in 0.4.0, which -- if dry_run mis-bound -- would
    # drive the code straight into the post-create PATCH write below.
    result = config.build_underlay_ssid(
        "Positional-040-SSID",
        "100",
        "CAMPUS_AP",
        "OPEN",
        None,
        None,
        [1000],
        "sys_central_nac",
        None,
        True,
    )

    # dry_run's own branch is the only path that populates `will_also_create`
    # (it returns before any post-create MAC-auth PATCH/role/scope-map calls).
    assert "will_also_create" in result
    assert not any(method in ("POST", "PUT", "PATCH", "DELETE") for method, _ in client.calls)


def test_build_overlay_ssid_040_positional_call_still_dry_runs(monkeypatch):
    client = WriteGuardClient()
    monkeypatch.setattr(config, "get_client", lambda: client)

    # Exact 0.4.0 positional call shape: ssid_name, scope_id, cluster_name,
    # cluster_scope_id, vlan_ids, opmode, passphrase,
    # mac_auth_server_group, policy_name, dry_run.
    result = config.build_overlay_ssid(
        "Positional-040-Overlay",
        "100",
        "GW-Cluster",
        "200",
        [200],
        "OPEN",
        None,
        None,
        None,
        True,
    )

    # dry_run reads the global scope-id (GET) but must never write.
    assert not result.get("errors")
    assert not any(method in ("POST", "PUT", "PATCH", "DELETE") for method, _ in client.calls)


# ---------------------------------------------------------------------------
# `wpa3_transition` must be reachable only by keyword, and must never be
# silently inherited as True.
# ---------------------------------------------------------------------------


def test_build_underlay_ssid_rejects_wpa3_transition_as_positional():
    with pytest.raises(TypeError):
        config.build_underlay_ssid(
            "SSID", "100", "CAMPUS_AP", "OPEN", None, None, [1000], None, None, False, True
        )


def test_build_overlay_ssid_rejects_wpa3_transition_as_positional():
    with pytest.raises(TypeError):
        config.build_overlay_ssid(
            "SSID", "100", "GW-Cluster", "200", [200], "OPEN", None, None, None, False, True
        )
