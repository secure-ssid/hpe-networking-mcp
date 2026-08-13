from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hpe_networking_mcp.pipeline.create_ssid import (
    build_overlay_ssid,
    build_underlay_ssid,
    create_allow_all_role,
)
from hpe_networking_mcp.pipeline.scope_ids import normalize_scope_id
from hpe_networking_mcp.pipeline.stages.s6_configure import _profile_scope_ids


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("123", "123"),
        (" 123 ", "123"),
        (123, "123"),
        ("0", "0"),
    ],
)
def test_normalize_scope_id_accepts_numeric_values(value, expected):
    assert normalize_scope_id(value) == expected


@pytest.mark.parametrize(
    "value",
    [None, "", " ", "site-1", "1.5", -1, False, [], {}, "1" * 21, "١٢٣"],
)
def test_normalize_scope_id_rejects_values_scope_maps_cannot_use(value):
    with pytest.raises(ValueError, match="ASCII decimal digits"):
        normalize_scope_id(value)


def test_underlay_scope_preflight_stops_before_write():
    client = MagicMock()

    result = build_underlay_ssid(client, "Guest", ["100"], "site-1")

    assert result["errors"] == [
        "validate_scope_id: scope_id must contain 1-20 ASCII decimal digits"
    ]
    client.post.assert_not_called()


@pytest.mark.parametrize(
    ("scope_id", "cluster_scope_id", "field_name"),
    [
        ("site-1", "200", "scope_id"),
        ("100", "cluster-1", "cluster_scope_id"),
    ],
)
def test_overlay_scope_preflight_stops_before_lookup_or_write(
    scope_id, cluster_scope_id, field_name
):
    client = MagicMock()

    result = build_overlay_ssid(
        client,
        "Corp",
        ["200"],
        scope_id,
        "cluster-a",
        cluster_scope_id,
    )

    assert result["errors"] == [
        f"validate_scope_id: {field_name} must contain 1-20 ASCII decimal digits"
    ]
    client.get.assert_not_called()
    client.post.assert_not_called()


def test_role_scope_preflight_stops_before_write():
    client = MagicMock()

    result = create_allow_all_role(client, "Allow", "group-1")

    assert result["errors"] == [
        "validate_scope_id: scope_id must contain 1-20 ASCII decimal digits"
    ]
    client.post.assert_not_called()


def test_profile_scope_preflight_rejects_cached_global_before_write():
    client = MagicMock()
    target = SimpleNamespace(
        global_scope_id="global-1",
        switch_group_name=None,
    )

    with pytest.raises(ValueError, match="global_scope_id"):
        _profile_scope_ids(client, target)

    client.get.assert_not_called()
    client.post.assert_not_called()
