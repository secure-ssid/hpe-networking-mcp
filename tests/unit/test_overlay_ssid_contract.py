"""Regression tests for ``build_overlay_ssid``'s dry-run/return contract and
the deduplicated opmode deprecation warning.

Reproduced defects:

- ``build_overlay_ssid`` called ``_fetch_global_scope_id`` unconditionally and
  unguarded, so a tenant where scope discovery fails made the function *raise*
  — including under ``dry_run=True``, whose documented contract is "log
  actions, never write, always return the result dict".
- A second, identical global-scope lookup ran later for policy scope mapping,
  doubling the API calls and letting the two halves of one build disagree.
- ``_normalize_opmode`` claimed to warn "the first time" a stale alias was
  seen but logged on every call, so a bulk build emitted one warning per SSID.

No network calls, no writes.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from hpe_networking_mcp.pipeline.create_ssid import (
    _build_ssid_body,
    _normalize_opmode,
    build_overlay_ssid,
    reset_opmode_deprecation_warnings,
)


def _client(global_scope_id="900", scope_maps_ok=True):
    client = MagicMock()
    if global_scope_id is None:
        client.get.side_effect = RuntimeError("global scope unavailable")
    else:
        client.get.return_value = {"scopeId": global_scope_id}
    if not scope_maps_ok:
        client.post.side_effect = RuntimeError("boom")
    else:
        client.post.return_value = {}
    return client


def _build(client, **kwargs):
    params = dict(
        ssid_name="Overlay-1",
        vlan_ids=["200"],
        scope_id="123",
        cluster_name="cluster-a",
        cluster_scope_id="456",
    )
    params.update(kwargs)
    return build_overlay_ssid(client, **params)


RESULT_KEYS = {
    "ssid_name",
    "vlan_ids",
    "scope_id",
    "cluster_name",
    "created",
    "overlay_created",
    "scope_mapped",
    "aaa_profile_created",
    "errors",
    "warnings",
}


class TestDryRunContract:
    def test_dry_run_returns_the_full_result_dict(self):
        result = _build(_client(), dry_run=True)

        assert RESULT_KEYS <= set(result)
        assert result["created"] is True
        assert result["overlay_created"] is True
        assert result["errors"] == []

    def test_dry_run_performs_no_writes(self):
        client = _client()

        _build(client, dry_run=True)

        client.post.assert_not_called()
        client.put.assert_not_called()
        client.patch.assert_not_called()
        client.delete.assert_not_called()

    def test_dry_run_survives_global_scope_discovery_failure(self):
        """Regression: this used to raise straight out of the function."""
        client = _client(global_scope_id=None)

        result = _build(client, dry_run=True)

        assert RESULT_KEYS <= set(result)
        assert result["errors"] == []
        assert any("resolve_global_scope" in w for w in result["warnings"])
        client.post.assert_not_called()

    def test_live_run_records_scope_failure_as_an_error_and_stops(self):
        client = _client(global_scope_id=None)

        result = _build(client)

        assert any("resolve_global_scope" in e for e in result["errors"])
        assert result["created"] is False
        # Aborts before any write, so no half-built role/SSID is left behind.
        client.post.assert_not_called()
        client.put.assert_not_called()
        client.patch.assert_not_called()

    def test_whitespace_global_scope_fails_before_any_write(self):
        client = _client(global_scope_id="   ")

        result = _build(client)

        assert any("resolve_global_scope" in e for e in result["errors"])
        client.post.assert_not_called()
        client.put.assert_not_called()
        client.patch.assert_not_called()

    def test_nonnumeric_global_scope_fails_before_any_write(self):
        client = _client(global_scope_id="global-2")

        result = _build(client)

        assert any("resolve_global_scope" in e for e in result["errors"])
        client.post.assert_not_called()
        client.put.assert_not_called()
        client.patch.assert_not_called()

    def test_dry_run_still_reports_opmode_deprecation_in_warnings(self):
        reset_opmode_deprecation_warnings()

        result = _build(_client(), dry_run=True, opmode="WPA2_PSK", wpa_passphrase="pw")

        assert any("WPA2_PSK" in w and "deprecated" in w for w in result["warnings"])


class TestSingleGlobalScopeLookup:
    def test_global_scope_is_resolved_exactly_once(self):
        """Regression: a second late lookup ran for policy scope mapping."""
        client = _client()

        _build(client)

        global_gets = [
            call for call in client.get.call_args_list
            if call.args and call.args[0] == "/network-config/v1/global"
        ]
        assert len(global_gets) == 1, f"expected 1 lookup, got {len(global_gets)}"

    def test_policy_scope_maps_use_the_resolved_id(self):
        client = _client(global_scope_id="4242")

        _build(client)

        policy_maps = [
            call for call in client.post.call_args_list
            if "scope-maps" in str(call.args[0]) and "policies/" in str(call.kwargs.get("data"))
        ]
        assert policy_maps, "expected policy scope-maps to be posted"
        for call in policy_maps:
            entry = call.kwargs["data"]["scope-map"][0]
            assert entry["scope-id"] == 4242
            assert entry["scope-name"] == "4242"

    def test_dry_run_resolves_at_most_once_too(self):
        client = _client()

        _build(client, dry_run=True)

        global_gets = [
            call for call in client.get.call_args_list
            if call.args and call.args[0] == "/network-config/v1/global"
        ]
        assert len(global_gets) <= 1


class TestOpmodeDeprecationDeduplication:
    def test_warning_is_logged_once_per_process(self, caplog):
        reset_opmode_deprecation_warnings()

        with caplog.at_level(logging.WARNING, logger="hpe_networking_mcp.pipeline.create_ssid"):
            for _ in range(5):
                _normalize_opmode("WPA2_PSK")

        warnings = [r for r in caplog.records if "WPA2_PSK" in r.message]
        assert len(warnings) == 1, f"expected 1 warning, got {len(warnings)}"

    def test_normalization_still_happens_every_time(self):
        reset_opmode_deprecation_warnings()

        for _ in range(3):
            assert _normalize_opmode("WPA2_PSK") == "WPA2_PERSONAL"

    def test_per_result_warnings_are_not_deduplicated(self):
        """The log is deduplicated; each result must still carry the notice."""
        reset_opmode_deprecation_warnings()
        client = _client()

        first = _build(client, dry_run=True, opmode="WPA2_PSK", wpa_passphrase="pw")
        second = _build(client, dry_run=True, opmode="WPA2_PSK", wpa_passphrase="pw")

        for result in (first, second):
            assert any("WPA2_PSK" in w for w in result["warnings"])

    def test_unknown_opmode_is_untouched_and_silent(self, caplog):
        reset_opmode_deprecation_warnings()

        with caplog.at_level(logging.WARNING, logger="hpe_networking_mcp.pipeline.create_ssid"):
            assert _normalize_opmode("BOGUS") == "BOGUS"

        assert not [r for r in caplog.records if "deprecated" in r.message]

    def test_reset_helper_re_arms_the_warning(self, caplog):
        reset_opmode_deprecation_warnings()
        _normalize_opmode("WPA2_PSK")
        reset_opmode_deprecation_warnings()

        with caplog.at_level(logging.WARNING, logger="hpe_networking_mcp.pipeline.create_ssid"):
            _normalize_opmode("WPA2_PSK")

        assert [r for r in caplog.records if "WPA2_PSK" in r.message]

    def test_build_ssid_body_alias_mapping_unchanged(self):
        body = _build_ssid_body("X", ["1"], opmode="WPA2_PSK", wpa_passphrase="pw")
        assert body["opmode"] == "WPA2_PERSONAL"
