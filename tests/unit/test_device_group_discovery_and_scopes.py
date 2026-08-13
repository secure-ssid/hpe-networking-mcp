"""Regression tests for device-group discovery (S2) and runtime scope
resolution (S6).

Reproduced defects:

- ``_get_group_names`` requested a single ``limit=100`` page, so any account
  with more than 100 device groups silently failed validation for groups on
  later pages. Both manifest operations (getDeviceGroupsV1 /
  getDeviceGroups) require limit+offset and cap limit at 100.
- The ``/v1/`` -> ``/v1alpha1/`` fallback never triggered: an unrecognized
  response shape defaulted to ``[]``, which is not ``None``, so the function
  returned an empty set and validation reported "group does not exist".
- ``g.get("scopeName", g.get("group", ...))`` returned ``None`` when the key
  was present with a null value, poisoning the name set.
- A total lookup failure was swallowed into an empty set, which validation
  reported as a missing group rather than an unreachable API.
- ``_ensure_device_profiles`` scope-mapped every library profile onto two
  hardcoded scope IDs belonging to one lab tenant.

No network calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hpe_networking_mcp.pipeline.models import StageStatus
from hpe_networking_mcp.pipeline.stages import s6_configure
from hpe_networking_mcp.pipeline.stages.s2_validate import (
    DEVICE_GROUP_PATHS,
    DeviceGroupLookupError,
    ValidateStage,
    _extract_group_items,
    _fetch_device_groups,
    _get_group_names,
    _group_name,
)
from hpe_networking_mcp.pipeline.stages.s6_configure import (
    DEFAULT_SWITCH_GROUP_NAME,
    SWITCH_GROUP_NAME_ENV,
    _fetch_global_scope_id,
    _profile_scope_ids,
    _resolve_device_group_scope_id,
)

MANIFEST = json.loads(
    (Path(__file__).resolve().parents[2] / "src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/central.json").read_text()
)


def _paging_client(pages_by_path):
    """Client whose device-group GET replays ``pages_by_path[path][offset]``."""
    client = MagicMock()

    def _get(path, params=None):
        params = params or {}
        if path not in pages_by_path:
            raise RuntimeError(f"404 not found: {path}")
        pages = pages_by_path[path]
        offset = params.get("offset", 0)
        index = offset // 100
        return pages[index] if index < len(pages) else {"items": []}

    client.get.side_effect = _get
    return client


# ---------------------------------------------------------------------------
# Manifest expectations
# ---------------------------------------------------------------------------


def test_manifest_requires_limit_and_offset_and_caps_at_100():
    op = next(o for o in MANIFEST["operations"] if o["key"] == "GET /network-config/v1/device-groups")
    params = {p["name"]: p for p in op["parameters"]}
    assert params["limit"]["required"] is True
    assert params["offset"]["required"] is True
    assert "Maximum limit per query is 100" in params["limit"]["description"]


def test_paths_are_tried_current_first():
    assert DEVICE_GROUP_PATHS == (
        "/network-config/v1/device-groups",
        "/network-config/v1alpha1/device-groups",
    )


# ---------------------------------------------------------------------------
# Envelope parsing
# ---------------------------------------------------------------------------


class TestExtractGroupItems:
    @pytest.mark.parametrize("key", ["items", "data", "device-groups", "deviceGroups", "groups"])
    def test_known_envelope_keys(self, key):
        assert _extract_group_items({key: [{"scopeName": "A"}]}) == [{"scopeName": "A"}]

    def test_recognized_but_empty_is_an_empty_list(self):
        assert _extract_group_items({"items": []}) == []

    def test_explicit_null_collection_is_an_empty_list(self):
        assert _extract_group_items({"items": None}) == []

    def test_bare_list_is_accepted(self):
        assert _extract_group_items([{"scopeName": "A"}]) == [{"scopeName": "A"}]

    def test_unrecognized_shape_is_none_not_empty(self):
        """Regression: this used to become ``[]`` and suppress the fallback."""
        assert _extract_group_items({"error": "unsupported version"}) is None
        assert _extract_group_items("nope") is None
        assert _extract_group_items({"items": "not-a-list"}) is None

    def test_non_dict_entries_are_dropped(self):
        assert _extract_group_items({"items": [{"scopeName": "A"}, "junk", None]}) == [
            {"scopeName": "A"}
        ]


class TestGroupName:
    def test_prefers_scope_name(self):
        assert _group_name({"scopeName": "Switches", "name": "other"}) == "Switches"

    def test_explicit_null_falls_through(self):
        """Regression: ``get(key, fallback)`` returned ``None`` for a null."""
        assert _group_name({"scopeName": None, "group": "Onboarding"}) == "Onboarding"

    def test_blank_values_fall_through(self):
        assert _group_name({"scopeName": "   ", "name": "Real"}) == "Real"

    def test_no_usable_name_is_empty_string(self):
        assert _group_name({"scopeId": "1"}) == ""

    def test_names_are_stripped(self):
        assert _group_name({"scopeName": "  Switches  "}) == "Switches"


# ---------------------------------------------------------------------------
# Pagination + fallback
# ---------------------------------------------------------------------------


class TestFetchDeviceGroups:
    def test_pages_until_a_short_page(self):
        v1 = DEVICE_GROUP_PATHS[0]
        client = _paging_client(
            {
                v1: [
                    {"items": [{"scopeName": f"G{i}"} for i in range(100)]},
                    {"items": [{"scopeName": f"H{i}"} for i in range(100)]},
                    {"items": [{"scopeName": "Tail"}]},
                ]
            }
        )

        groups = _fetch_device_groups(client)

        assert len(groups) == 201
        offsets = [c.kwargs["params"]["offset"] for c in client.get.call_args_list]
        assert offsets == [0, 100, 200]

    def test_group_past_the_first_page_is_found(self):
        """The reproduced symptom: a valid target group was rejected."""
        v1 = DEVICE_GROUP_PATHS[0]
        client = _paging_client(
            {
                v1: [
                    {"items": [{"scopeName": f"G{i}"} for i in range(100)]},
                    {"items": [{"scopeName": "Onboarding"}]},
                ]
            }
        )

        assert "Onboarding" in _get_group_names(client)

    def test_single_short_page_makes_one_call(self):
        v1 = DEVICE_GROUP_PATHS[0]
        client = _paging_client({v1: [{"items": [{"scopeName": "Only"}]}]})

        assert _get_group_names(client) == {"Only"}
        assert client.get.call_count == 1

    def test_falls_back_when_v1_errors(self):
        client = _paging_client({DEVICE_GROUP_PATHS[1]: [{"items": [{"scopeName": "Legacy"}]}]})

        assert _get_group_names(client) == {"Legacy"}

    def test_falls_back_when_v1_returns_an_unrecognized_shape(self):
        """Regression: this silently returned an empty set from ``/v1/``."""
        client = _paging_client(
            {
                DEVICE_GROUP_PATHS[0]: [{"message": "not supported on this tenant"}],
                DEVICE_GROUP_PATHS[1]: [{"items": [{"scopeName": "Legacy"}]}],
            }
        )

        assert _get_group_names(client) == {"Legacy"}

    def test_recognized_empty_v1_does_not_fall_back(self):
        """An account with zero groups is a real answer, not a version problem."""
        calls = []
        client = MagicMock()

        def _get(path, params=None):
            calls.append(path)
            if path == DEVICE_GROUP_PATHS[0]:
                return {"items": []}
            raise AssertionError("must not fall back on a recognized empty response")

        client.get.side_effect = _get

        assert _get_group_names(client) == set()
        assert calls == [DEVICE_GROUP_PATHS[0]]

    def test_total_failure_raises_instead_of_returning_empty(self):
        client = _paging_client({})

        with pytest.raises(DeviceGroupLookupError) as exc:
            _get_group_names(client)

        assert "could not list device groups" in str(exc.value)
        for path in DEVICE_GROUP_PATHS:
            assert path in str(exc.value)

    def test_blank_names_are_dropped_from_the_name_set(self):
        v1 = DEVICE_GROUP_PATHS[0]
        client = _paging_client({v1: [{"items": [{"scopeName": None}, {"scopeName": "Real"}]}]})

        assert _get_group_names(client) == {"Real"}


class TestValidateStageGroupCheck:
    def test_unreachable_group_api_fails_with_a_lookup_error(
        self, record_unmanaged, source_ctx, target_ctx, state, run_id
    ):
        """Regression: an unreachable API used to be reported as a missing group."""
        target_ctx.mcp_client.get_device_by_serial.return_value = {"isProvisioned": "NO"}
        target_ctx.mcp_client.get_site_by_name.return_value = None
        target_ctx.mcp_client.get_alerts.return_value = []
        target_ctx.central_client = _paging_client({})

        result = ValidateStage()._execute(
            record_unmanaged, run_id, source_ctx, target_ctx, state, False
        )

        assert result.status == StageStatus.FAILED
        assert "could not list device groups" in result.error
        assert "does not exist" not in result.error

    def test_group_on_a_later_page_passes_validation(
        self, record_unmanaged, source_ctx, target_ctx, state, run_id
    ):
        target_ctx.mcp_client.get_device_by_serial.return_value = {"isProvisioned": "NO"}
        target_ctx.mcp_client.get_site_by_name.return_value = {"siteId": "s1"}
        target_ctx.mcp_client.get_alerts.return_value = []
        target_ctx.central_client = _paging_client(
            {
                DEVICE_GROUP_PATHS[0]: [
                    {"items": [{"scopeName": f"G{i}"} for i in range(100)]},
                    {"items": [{"scopeName": record_unmanaged.target_group}]},
                ]
            }
        )

        result = ValidateStage()._execute(
            record_unmanaged, run_id, source_ctx, target_ctx, state, False
        )

        assert result.status == StageStatus.SUCCESS


# ---------------------------------------------------------------------------
# S6 — no hardcoded tenant scope IDs
# ---------------------------------------------------------------------------


LEGACY_HARDCODED_SCOPE_IDS = ("79236221864456192", "79244358948933632")


def test_no_hardcoded_tenant_scope_ids_remain_in_source():
    source = Path(s6_configure.__file__).read_text()
    for literal in LEGACY_HARDCODED_SCOPE_IDS:
        assert literal not in source, f"hardcoded tenant scope-id {literal} still present"


class TestScopeResolution:
    def _ctx(self, global_scope_id="900"):
        ctx = MagicMock()
        ctx.global_scope_id = global_scope_id
        ctx.switch_group_name = None
        return ctx

    def test_global_scope_uses_authoritative_endpoint(self):
        client = MagicMock()
        client.get.return_value = {"scopeId": 12345}

        assert _fetch_global_scope_id(client) == "12345"
        client.get.assert_called_once_with("/network-config/v1/global")

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"scopeId": ""},
            {"scopeId": "   "},
            {"scopeId": "global-2"},
            {"scopeId": False},
            {"scopeId": []},
            {"scopeId": -1},
            [],
            None,
        ],
    )
    def test_global_scope_rejects_malformed_response(self, payload):
        client = MagicMock()
        client.get.return_value = payload

        with pytest.raises(RuntimeError, match="/network-config/v1/global"):
            _fetch_global_scope_id(client)

    def test_global_scope_falls_back_to_compatible_id_field(self):
        client = MagicMock()
        client.get.return_value = {"scopeId": "   ", "id": " 67890 "}

        assert _fetch_global_scope_id(client) == "67890"

    def test_resolves_group_scope_id_by_name(self):
        client = _paging_client(
            {
                DEVICE_GROUP_PATHS[0]: [
                    {"items": [{"scopeName": "Switches", "scopeId": "555"}, {"scopeName": "APs", "scopeId": "666"}]}
                ]
            }
        )

        assert _resolve_device_group_scope_id(client, "Switches") == "555"

    def test_group_match_is_case_insensitive(self):
        client = _paging_client(
            {DEVICE_GROUP_PATHS[0]: [{"items": [{"scopeName": "switches", "scopeId": "555"}]}]}
        )

        assert _resolve_device_group_scope_id(client, "Switches") == "555"

    def test_missing_group_returns_none(self):
        client = _paging_client({DEVICE_GROUP_PATHS[0]: [{"items": [{"scopeName": "APs", "scopeId": "666"}]}]})

        assert _resolve_device_group_scope_id(client, "Switches") is None

    def test_lookup_failure_propagates(self):
        with pytest.raises(DeviceGroupLookupError):
            _resolve_device_group_scope_id(_paging_client({}), "Switches")

    def test_scope_ids_are_global_plus_resolved_group(self):
        client = _paging_client(
            {DEVICE_GROUP_PATHS[0]: [{"items": [{"scopeName": "Switches", "scopeId": "555"}]}]}
        )

        assert _profile_scope_ids(client, self._ctx()) == ["900", "555"]

    def test_missing_group_degrades_to_global_only(self, caplog):
        import logging

        client = _paging_client({DEVICE_GROUP_PATHS[0]: [{"items": []}]})

        with caplog.at_level(logging.WARNING, logger="hpe_networking_mcp.pipeline.stages.s6_configure"):
            assert _profile_scope_ids(client, self._ctx()) == ["900"]

        assert any(SWITCH_GROUP_NAME_ENV in r.message for r in caplog.records)

    def test_group_name_is_overridable_by_env(self, monkeypatch):
        monkeypatch.setenv(SWITCH_GROUP_NAME_ENV, "CX-Switches")
        client = _paging_client(
            {DEVICE_GROUP_PATHS[0]: [{"items": [{"scopeName": "CX-Switches", "scopeId": "777"}]}]}
        )

        assert _profile_scope_ids(client, self._ctx()) == ["900", "777"]

    def test_default_group_name(self):
        assert DEFAULT_SWITCH_GROUP_NAME == "Switches"

    def test_duplicate_scope_id_is_not_repeated(self):
        client = _paging_client(
            {DEVICE_GROUP_PATHS[0]: [{"items": [{"scopeName": "Switches", "scopeId": "900"}]}]}
        )

        assert _profile_scope_ids(client, self._ctx("900")) == ["900"]

    def test_global_scope_resolution_failure_raises(self, monkeypatch):
        monkeypatch.setattr(
            s6_configure,
            "_fetch_global_scope_id",
            lambda client: (_ for _ in ()).throw(RuntimeError("no global scope")),
        )
        ctx = self._ctx(global_scope_id=None)

        with pytest.raises(RuntimeError, match="no global scope"):
            _profile_scope_ids(MagicMock(), ctx)

    def test_scope_resolution_failure_fails_the_stage(
        self, record_unmanaged, source_ctx, target_ctx, state, run_id, monkeypatch
    ):
        from hpe_networking_mcp.pipeline.stages.s6_configure import ConfigureStage

        record_unmanaged.scope_id = "12345"
        target_ctx.global_scope_id = "900"
        target_ctx.device_profiles_created = False
        target_ctx.central_client = _paging_client({})

        result = ConfigureStage()._execute(
            record_unmanaged, run_id, source_ctx, target_ctx, state, False
        )

        assert result.status == StageStatus.FAILED
        assert "device-profile scope resolution" in result.error
