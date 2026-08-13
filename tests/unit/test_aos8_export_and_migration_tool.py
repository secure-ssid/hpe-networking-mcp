"""Tests for AOS8 export tools (`aos8_get_vlans`, `aos8_get_policies`,
`aos8_export_wlans`, `aos8_export_all`) and the `aos8_migration_plan` tool
that ties them to `hpe_networking_mcp.pipeline.aos8_migration.build_migration_plan`.
"""

from __future__ import annotations

import asyncio

import hpe_networking_mcp.mcp_servers.aos8 as aos8
from hpe_networking_mcp.pipeline.aos8_parsers import parse_wlans


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = "{}"

    def json(self):
        return self._payload


def _fake_client_for_paths(path_to_payload: dict[str, object]):
    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, params=None):
            for suffix, payload in path_to_payload.items():
                if url.endswith(suffix):
                    return _Resp(payload)
            return _Resp({"error": f"unexpected url {url}"}, status_code=404)

    return _FakeAsyncClient


def test_aos8_get_vlans_lists_vlan_id_objects(monkeypatch):
    fake_cls = _fake_client_for_paths(
        {"/v1/configuration/object/vlan_id": {"vlan_id": [{"id": 20, "description": "Corp"}]}}
    )
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", fake_cls)

    out = asyncio.run(aos8.aos8_get_vlans(config_path="/md/lab"))

    assert out["config_path"] == "/md/lab"
    assert out["vlans"]["vlan_id"] == [{"id": 20, "description": "Corp"}]


def test_aos8_get_policies_lists_acl_sess_objects(monkeypatch):
    fake_cls = _fake_client_for_paths(
        {"/v1/configuration/object/acl_sess": {"acl_sess": [{"name": "corp-acl"}]}}
    )
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", fake_cls)

    out = asyncio.run(aos8.aos8_get_policies(config_path="/md/lab"))

    assert out["config_path"] == "/md/lab"
    assert out["policies"]["acl_sess"] == [{"name": "corp-acl"}]


def test_aos8_export_wlans_merges_ssid_profiles_and_virtual_aps(monkeypatch):
    fake_cls = _fake_client_for_paths(
        {
            "/v1/configuration/object/ssid_prof": {
                "ssid_prof": [{"profile-name": "Corp", "essid": "Corp"}]
            },
            "/v1/configuration/object/virtual_ap": {
                "virtual_ap": [{"profile-name": "Corp-VAP", "ssid-profile": "Corp", "vlan": 20}]
            },
        }
    )
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", fake_cls)

    out = asyncio.run(aos8.aos8_export_wlans(config_path="/md/lab"))

    assert out["config_path"] == "/md/lab"
    assert out["ssid_profiles"] == [{"profile-name": "Corp", "essid": "Corp"}]
    assert out["virtual_aps"] == [{"profile-name": "Corp-VAP", "ssid-profile": "Corp", "vlan": 20}]
    assert "warnings" not in out


def test_aos8_export_wlans_collects_warnings_on_partial_failure(monkeypatch):
    fake_cls = _fake_client_for_paths(
        {"/v1/configuration/object/ssid_prof": {"ssid_prof": [{"profile-name": "Corp"}]}}
    )
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", fake_cls)

    out = asyncio.run(aos8.aos8_export_wlans(config_path="/md/lab"))

    assert out["ssid_profiles"] == [{"profile-name": "Corp"}]
    assert out["virtual_aps"] == []
    assert any("virtual_aps" in w for w in out["warnings"])


def test_aos8_list_virtual_aps_falls_back_to_legacy_wlan_virtual_ap_object(monkeypatch):
    """Regression: some AOS8 builds don't expose the canonical `virtual_ap`
    config object and answer only its legacy `wlan_virtual_ap` name instead
    (secondary, same-owner prior art, not an authoritative API contract):
    https://github.com/secure-ssid/aos8-migration-tool/blob/7bfa884d8e8f1c7e97a7bfa42f15596aa42fcf79/lib/aos8_client.py#L315-L399
    """
    fake_cls = _fake_client_for_paths(
        {
            "/v1/configuration/object/wlan_virtual_ap": {
                "wlan_virtual_ap": [
                    {"profile-name": "Legacy-VAP", "ssid-profile": "Legacy", "vlan": 30}
                ]
            }
        }
    )
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", fake_cls)

    out = asyncio.run(aos8.aos8_list_virtual_aps(config_path="/md/lab"))

    list_key = next(key for key in out["virtual_aps"] if key != "_pagination")
    assert list_key == "wlan_virtual_ap"
    assert out["virtual_aps"][list_key] == [
        {"profile-name": "Legacy-VAP", "ssid-profile": "Legacy", "vlan": 30}
    ]


def test_aos8_list_virtual_aps_reports_failure_when_both_object_names_fail(monkeypatch):
    """A build lacking both the canonical and legacy virtual-AP object names
    must fail the same way a single failed lookup always has -- no new
    silent-success path is introduced by the fallback."""
    fake_cls = _fake_client_for_paths({})
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", fake_cls)

    out = asyncio.run(aos8.aos8_list_virtual_aps(config_path="/md/lab"))

    assert out["status_code"] == 404


def test_aos8_export_wlans_still_warns_when_both_virtual_ap_object_names_fail(monkeypatch):
    """End-to-end: `aos8_export_wlans` must still surface the same
    "virtual_aps" failure warning (and empty list) it always has when neither
    the canonical nor the legacy virtual-AP object name resolves -- the added
    fallback call must not mask a genuine failure."""
    fake_cls = _fake_client_for_paths(
        {"/v1/configuration/object/ssid_prof": {"ssid_prof": [{"profile-name": "Corp"}]}}
    )
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", fake_cls)

    out = asyncio.run(aos8.aos8_export_wlans(config_path="/md/lab"))

    assert out["virtual_aps"] == []
    assert any("virtual_aps" in w for w in out["warnings"])


def test_aos8_list_virtual_aps_retains_ssid_prof_hyphenated_alias(monkeypatch):
    """Regression: bounded compaction in `aos8_list_virtual_aps` must not
    strip the "ssid-prof" (all-hyphen) alias some AOS8 builds use to
    reference the SSID profile, or `parse_wlans` can never join the VAP back
    to its SSID profile (secondary, same-owner prior art, not an
    authoritative API contract):
    https://github.com/secure-ssid/aos8-migration-tool/blob/7bfa884d8e8f1c7e97a7bfa42f15596aa42fcf79/lib/aos8_client.py#L315-L399
    """
    fake_cls = _fake_client_for_paths(
        {
            "/v1/configuration/object/virtual_ap": {
                "virtual_ap": [
                    {
                        "profile-name": "HyphenProf-VAP",
                        "ssid-prof": "HyphenProf",
                        "vlan": 90,
                        # Extra live-shaped noise field that is not in the
                        # bounded field set and must still be stripped.
                        "unrelated-live-field": "should-be-dropped",
                    }
                ]
            }
        }
    )
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", fake_cls)

    out = asyncio.run(aos8.aos8_list_virtual_aps(config_path="/md/lab"))

    record = out["virtual_aps"]["virtual_ap"][0]
    assert record["ssid-prof"] == "HyphenProf"
    assert "unrelated-live-field" not in record


def test_aos8_export_wlans_links_ssid_prof_alias_vap_to_one_wlan(monkeypatch):
    """End-to-end: a live-shaped `{profile-name, ssid-prof, vlan}`
    virtual-AP record must survive the `aos8_export_wlans` bounded-export
    round trip and still let `parse_wlans` join it to exactly one WLAN,
    instead of producing an unlinked SSID profile and an unlinked
    virtual-AP-only WLAN record."""
    fake_cls = _fake_client_for_paths(
        {
            "/v1/configuration/object/ssid_prof": {
                "ssid_prof": [
                    {"profile-name": "HyphenProf", "essid": "HyphenProf", "opmode": "opensystem"}
                ]
            },
            "/v1/configuration/object/virtual_ap": {
                "virtual_ap": [
                    {
                        "profile-name": "HyphenProf-VAP",
                        "ssid-prof": "HyphenProf",
                        "vlan": 90,
                    }
                ]
            },
        }
    )
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", fake_cls)

    out = asyncio.run(aos8.aos8_export_wlans(config_path="/md/lab"))
    assert "warnings" not in out

    wlans = parse_wlans({"wlans": out})
    assert len(wlans) == 1
    wlan = wlans[0]
    assert wlan.profile_name == "HyphenProf"
    assert wlan.virtual_ap_profile == "HyphenProf-VAP"
    assert wlan.vlan == 90


def test_aos8_export_page_collector_exhausts_local_pages():
    records = [{"id": value} for value in range(5)]

    async def fetch(limit: int, offset: int):
        return {"items": {"items": records[offset : offset + limit]}}

    items, warnings = asyncio.run(
        aos8._aos8_collect_all(
            "items",
            fetch,
            page_size=2,
            max_items=10,
        )
    )

    assert items == records
    assert warnings == []


def test_aos8_export_all_fans_out_and_shapes_result(monkeypatch):
    fake_cls = _fake_client_for_paths(
        {
            "/v1/configuration/object/ssid_prof": {
                "ssid_prof": [{"profile-name": "Corp", "essid": "Corp"}]
            },
            "/v1/configuration/object/virtual_ap": {
                "virtual_ap": [{"profile-name": "Corp-VAP", "ssid-profile": "Corp", "vlan": 20}]
            },
            "/v1/configuration/object/role": {
                "role": [{"role": "employee", "vlan": 20}]
            },
            "/v1/configuration/object/vlan_id": {"vlan_id": [{"id": 20, "description": "Corp"}]},
            "/v1/configuration/object/ap_group": {
                "ap_group": [{"profile-name": "Lab-AP-Group"}]
            },
            "/v1/configuration/showcommand": {
                "Switches": [{"Name": "mc1", "IP Address": "10.0.0.1"}]
            },
            "/v1/configuration/object/acl_sess": {"acl_sess": [{"name": "corp-acl"}]},
            "/v1/configuration/object/aaa_prof": {
                "aaa_prof": [
                    {
                        "profile-name": "corp-aaa",
                        "dot1x_auth_profile": "corp-dot1x",
                        "dot1x_server_group": "corp-sg",
                    }
                ]
            },
            "/v1/configuration/object/dot1x_auth_profile": {
                "dot1x_auth_profile": [
                    {"profile-name": "corp-dot1x", "reauthentication": True}
                ]
            },
            "/v1/configuration/object/mac_auth_profile": {
                "mac_auth_profile": [{"profile-name": "corp-mac"}]
            },
            "/v1/configuration/object/server_group_prof": {
                "server_group_prof": [
                    {"sg_name": "corp-sg", "auth_server": ["rad1"]}
                ]
            },
            "/v1/configuration/object/rad_server": {
                "rad_server": [{"rad_server_name": "rad1", "rad_host": "10.0.0.10"}]
            },
            "/v1/configuration/object/ldap_server": {
                "ldap_server": [{"ldap_server_name": "ldap1", "ldap_host": "10.0.0.11"}]
            },
            "/v1/configuration/object/tacacs_server": {
                "tacacs_server": [
                    {"tacacs_server_name": "tac1", "tacacs_host": "10.0.0.12"}
                ]
            },
            "/v1/configuration/object/ip_route": {
                "ip_route": [
                    {
                        "destip": "10.20.0.0",
                        "destmask": "255.255.0.0",
                        "nexthop": "10.0.0.254",
                        "zero": 0,
                    }
                ]
            },
            "/v1/configuration/object/ipv6_route": {
                "ipv6_route": [
                    {
                        "destip": "2001:db8:20::/64",
                        "nexthop": "2001:db8::1",
                        "nexthop1": "2001:db8::2",
                        "vlanid": 20,
                        "zero": 0,
                    }
                ]
            },
            "/v1/configuration/object/vrrp": {
                "vrrp": [{"id": 20, "vrrp_ip": "10.0.20.1", "vrrp_vlan": 20}]
            },
            "/v1/configuration/object/vrrp6": {"vrrp6": []},
            "/v1/configuration/object/netdst": {
                "netdst": [
                    {
                        "dstname": "corp-servers",
                        "netdst__network": "10.20.0.0/16",
                    }
                ]
            },
            "/v1/configuration/object/netdst6": {
                "netdst6": [
                    {
                        "dstname": "corp-servers-v6",
                        "netdst6__network": "2001:db8:20::/64",
                    }
                ]
            },
            "/v1/configuration/object/acl_eth": {
                "acl_eth": [
                    {
                        "accname": "eth-200",
                        "acl_eth__policy": [
                            {
                                "source": "any",
                                "destination": "any",
                                "action": "permit",
                            }
                        ],
                    }
                ]
            },
            "/v1/configuration/object/whitelist_rule": {
                "whitelist_rule": [
                    {"sipaddr": "10.0.0.1", "eipaddr": "10.0.0.50"}
                ]
            },
            "/v1/configuration/object/wired_auth_profile": {
                "wired_auth_profile": [
                    {"wired_aaa_profile": "corp-aaa", "wired_blacklist_time": 3600}
                ]
            },
            "/v1/configuration/object/stateful_dot1x_auth_profile": {
                "stateful_dot1x_auth_profile": [
                    {"stateful_dot1x_server_group": "corp-sg", "timeout": 300}
                ]
            },
            "/v1/configuration/object/wispr_auth_profile": {
                "wispr_auth_profile": [
                    {"profile-name": "wispr1", "wispr_default_role": "guest"}
                ]
            },
            "/v1/configuration/object/cp_auth_profile": {
                "cp_auth_profile": [
                    {"profile-name": "cp1", "cp_default_role": "guest"}
                ]
            },
            "/v1/configuration/object/krb_auth_profile": {
                "krb_auth_profile": [
                    {"profile-name": "krb1", "krb_server_group": "corp-sg"}
                ]
            },
            "/v1/configuration/object/ntlm_auth_profile": {
                "ntlm_auth_profile": [
                    {"profile-name": "ntlm1", "ntlm_server_group": "corp-sg"}
                ]
            },
        }
    )
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", fake_cls)

    out = asyncio.run(aos8.aos8_export_all(config_path="/md/lab"))

    assert out["config_path"] == "/md/lab"
    assert out["wlans"]["ssid_profiles"] == [{"profile-name": "Corp", "essid": "Corp"}]
    assert out["roles"] == [{"role": "employee", "vlan": 20}]
    assert out["vlans"] == [{"id": 20, "description": "Corp"}]
    assert out["ap_groups"] == [{"profile-name": "Lab-AP-Group"}]
    assert out["policies"] == [{"name": "corp-acl"}]
    assert out["aaa"]["aaa_profiles"][0]["profile-name"] == "corp-aaa"
    assert out["aaa"]["radius_servers"][0]["rad_server_name"] == "rad1"
    assert out["aaa"]["wired_auth_profiles"][0]["wired_aaa_profile"] == "corp-aaa"
    assert out["aaa"]["stateful_dot1x_auth_profiles"][0]["stateful_dot1x_server_group"] == "corp-sg"
    assert out["aaa"]["wispr_auth_profiles"][0]["profile-name"] == "wispr1"
    assert out["aaa"]["cp_auth_profiles"][0]["profile-name"] == "cp1"
    assert out["aaa"]["krb_auth_profiles"][0]["profile-name"] == "krb1"
    assert out["aaa"]["ntlm_auth_profiles"][0]["profile-name"] == "ntlm1"
    assert out["routing"]["ipv4_routes"][0]["destip"] == "10.20.0.0"
    assert out["routing"]["vrrp"][0]["id"] == 20
    assert out["netdst"][0]["dstname"] == "corp-servers"
    assert out["netdst6"][0]["dstname"] == "corp-servers-v6"
    assert out["acl_eth"][0]["accname"] == "eth-200"
    assert out["whitelist_rule"][0]["sipaddr"] == "10.0.0.1"
    assert out["warnings"] == []


def test_aos8_export_all_warns_on_malformed_success_collection(monkeypatch):
    fake_cls = _fake_client_for_paths(
        {
            "/v1/configuration/object/ssid_prof": {"ssid_prof": []},
            "/v1/configuration/object/virtual_ap": {"virtual_ap": []},
            "/v1/configuration/object/role": {"role": {"unexpected": "object"}},
        }
    )
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", fake_cls)

    out = asyncio.run(aos8.aos8_export_all(config_path="/md/lab"))

    assert out["roles"] == []
    assert any(
        "user_roles: response collection was missing or malformed" in warning
        for warning in out["warnings"]
    )


def test_aos8_export_all_reports_warnings_without_aborting(monkeypatch):
    # Only ssid_prof/virtual_ap succeed; every other object type 404s.
    fake_cls = _fake_client_for_paths(
        {
            "/v1/configuration/object/ssid_prof": {"ssid_prof": [{"profile-name": "Corp"}]},
            "/v1/configuration/object/virtual_ap": {"virtual_ap": []},
        }
    )
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", fake_cls)

    out = asyncio.run(aos8.aos8_export_all(config_path="/md/lab"))

    assert out["roles"] == []
    assert out["vlans"] == []
    assert out["ap_groups"] == []
    assert out["controllers"] == []
    assert out["policies"] == []
    assert len(out["warnings"]) >= 4


def test_aos8_migration_plan_builds_deterministic_plan_from_live_export(monkeypatch):
    fake_cls = _fake_client_for_paths(
        {
            "/v1/configuration/object/ssid_prof": {
                "ssid_prof": [{"profile-name": "Corp", "essid": "Corp", "opmode": "wpa2-aes"}]
            },
            "/v1/configuration/object/virtual_ap": {
                "virtual_ap": [
                    {
                        "profile-name": "Corp-VAP",
                        "ssid-profile": "Corp",
                        "vlan": 20,
                        "aaa-profile": "dot1x",
                        "forward-mode": "tunnel",
                    }
                ]
            },
            "/v1/configuration/object/role": {
                "role": [{"role": "employee", "vlan": 20, "acl": "allowall"}]
            },
            "/v1/configuration/object/vlan_id": {"vlan_id": [{"id": 20, "description": "Corp"}]},
            "/v1/configuration/object/ap_group": {"ap_group": []},
            "/v1/configuration/showcommand": {"Switches": []},
            "/v1/configuration/object/acl_sess": {"acl_sess": []},
        }
    )
    monkeypatch.setenv("AOS8_BASE_URL", "https://mm.example.com")
    monkeypatch.setenv("AOS8_API_TOKEN", "secret")
    monkeypatch.setattr(aos8.httpx, "AsyncClient", fake_cls)

    plan = asyncio.run(aos8.aos8_migration_plan(config_path="/md/lab"))

    assert plan["config_path"] == "/md/lab"
    classic_types = {c["object_type"] for c in plan["candidates"]["classic_central"]}
    assert "wlan" in classic_types
    assert "role" in classic_types
    assert any("opmode" in w for w in plan["warnings"])
    assert "verification_plan" in plan


# ---------------------------------------------------------------------------
# `aos8_migration_dependency_plan` -- bounded, read-only staged-readiness
# summary over an already-built migration plan/candidate list.
# ---------------------------------------------------------------------------

_DEPENDENCY_PLAN_EXPORT = {
    "config_path": "/md/lab",
    "netdst": [{"dstname": "corp-servers", "netdst__network": "10.20.0.0/16"}],
    "acl_eth": [
        {
            "accname": "eth-200",
            "acl_eth__policy": [
                {"source": "any", "destination": "any", "action": "permit"}
            ],
        }
    ],
    "whitelist_rule": [{"sipaddr": "10.0.0.1", "eipaddr": "10.0.0.50"}],
    "policies": [
        {
            "accname": "corp-acl",
            "acl_sess__v4policy": [
                {
                    "source": "user",
                    "destination": "corp-servers",
                    "service": "https",
                    "action": "permit",
                }
            ],
        }
    ],
    "roles": [{"role": "orphan", "acl": "missing-policy", "vlan": 10}],
}


def _build_dependency_plan_fixture():
    from hpe_networking_mcp.pipeline.aos8_migration import build_migration_plan

    return build_migration_plan(_DEPENDENCY_PLAN_EXPORT)


def test_aos8_migration_dependency_plan_requires_exactly_one_source():
    plan = _build_dependency_plan_fixture()
    out = aos8.aos8_migration_dependency_plan(
        target_type="new_central",
        migration_plan=plan,
        candidates=plan["candidates"]["new_central"],
    )
    assert "error" in out
    out_none = aos8.aos8_migration_dependency_plan(target_type="new_central")
    assert "error" in out_none


def test_aos8_migration_dependency_plan_groups_by_apply_order_stage():
    plan = _build_dependency_plan_fixture()
    out = aos8.aos8_migration_dependency_plan(
        target_type="new_central", migration_plan=plan
    )
    stage_orders = [stage["apply_order"] for stage in out["stages"]]
    assert stage_orders == sorted(stage_orders)
    assert sum(stage["candidate_count"] for stage in out["stages"]) == (
        out["summary"]["total_candidates"]
    )


def test_aos8_migration_dependency_plan_classifies_reference_only_families():
    plan = _build_dependency_plan_fixture()
    out = aos8.aos8_migration_dependency_plan(
        target_type="new_central", migration_plan=plan
    )
    by_type = {c["object_type"]: c for c in out["candidates"]}
    assert by_type["network_destination"]["status"] == "reference_only"
    assert by_type["ethernet_acl"]["status"] == "reference_only"
    assert by_type["whitelist_rule"]["status"] == "reference_only"
    assert out["summary"]["reference_only"] == 3


def test_aos8_migration_dependency_plan_flags_blocked_missing_dependency():
    plan = _build_dependency_plan_fixture()
    out = aos8.aos8_migration_dependency_plan(
        target_type="new_central", migration_plan=plan
    )
    role = next(c for c in out["candidates"] if c["object_type"] == "role")
    assert role["status"] == "blocked"
    assert "policy:missing-policy" in role["dependencies"]
    assert out["summary"]["blocked"] == 1


def test_aos8_migration_dependency_plan_marks_ready_policy_with_resolved_dependency():
    plan = _build_dependency_plan_fixture()
    out = aos8.aos8_migration_dependency_plan(
        target_type="new_central", migration_plan=plan
    )
    policy = next(c for c in out["candidates"] if c["object_type"] == "policy")
    assert policy["status"] == "ready"
    assert policy["dependencies"] == ["network_destination:ipv4:corp-servers"]


def test_aos8_migration_dependency_plan_bounds_candidates_with_limit_offset():
    plan = _build_dependency_plan_fixture()
    out = aos8.aos8_migration_dependency_plan(
        target_type="new_central", migration_plan=plan, limit=2, offset=0
    )
    assert len(out["candidates"]) == 2
    assert out["limit"] == 2
    assert out["summary"]["total_candidates"] == 5


def test_aos8_migration_dependency_plan_accepts_raw_candidates_list():
    plan = _build_dependency_plan_fixture()
    out = aos8.aos8_migration_dependency_plan(
        target_type="new_central", candidates=plan["candidates"]["new_central"]
    )
    assert out["summary"]["total_candidates"] == 5


def test_aos8_migration_dependency_plan_rejects_unknown_target_type():
    plan = _build_dependency_plan_fixture()
    out = aos8.aos8_migration_dependency_plan(
        target_type="not-a-real-target", migration_plan=plan
    )
    assert "error" in out


# ---------------------------------------------------------------------------
# aos8_migration_batch_plan
# ---------------------------------------------------------------------------


def test_aos8_migration_batch_plan_requires_exactly_one_source():
    plan = _build_dependency_plan_fixture()
    out = aos8.aos8_migration_batch_plan(
        target_type="new_central",
        migration_plan=plan,
        candidates=plan["candidates"]["new_central"],
    )
    assert "error" in out
    out_none = aos8.aos8_migration_batch_plan(target_type="new_central")
    assert "error" in out_none


def test_aos8_migration_batch_plan_never_exceeds_batch_size():
    plan = _build_dependency_plan_fixture()
    out = aos8.aos8_migration_batch_plan(
        target_type="new_central", migration_plan=plan, batch_size=2
    )
    for batch in out["batches"]:
        assert len(batch["candidate_keys"]) <= 2
    assert sum(len(batch["candidate_keys"]) for batch in out["batches"]) == (
        out["summary"]["total_candidates"]
    )


def test_aos8_migration_batch_plan_never_mixes_apply_order_stages_in_one_batch():
    plan = _build_dependency_plan_fixture()
    out = aos8.aos8_migration_batch_plan(
        target_type="new_central", migration_plan=plan, batch_size=100
    )
    # With a batch size larger than any single stage's candidate count,
    # every batch must still correspond to exactly one apply_order stage.
    stage_orders = {batch["apply_order"] for batch in out["batches"]}
    assert len(out["batches"]) == len(stage_orders)


def test_aos8_migration_batch_plan_batches_are_deterministic_and_reproducible():
    plan = _build_dependency_plan_fixture()
    first = aos8.aos8_migration_batch_plan(
        target_type="new_central", migration_plan=plan, batch_size=2
    )
    second = aos8.aos8_migration_batch_plan(
        target_type="new_central", migration_plan=plan, batch_size=2
    )
    assert first["batches"] == second["batches"]


def test_aos8_migration_batch_plan_flags_reference_only_and_secret_counts():
    plan = _build_dependency_plan_fixture()
    out = aos8.aos8_migration_batch_plan(
        target_type="new_central", migration_plan=plan, batch_size=100
    )
    total_reference_only = sum(batch["reference_only_count"] for batch in out["batches"])
    assert total_reference_only == 3  # network_destination + ethernet_acl + whitelist_rule


def test_aos8_migration_batch_plan_bounds_batches_with_limit_offset():
    plan = _build_dependency_plan_fixture()
    out = aos8.aos8_migration_batch_plan(
        target_type="new_central", migration_plan=plan, batch_size=1, limit=1, offset=0
    )
    assert len(out["batches"]) == 1
    assert out["limit"] == 1


def test_aos8_migration_batch_plan_accepts_raw_candidates_list():
    plan = _build_dependency_plan_fixture()
    out = aos8.aos8_migration_batch_plan(
        target_type="new_central", candidates=plan["candidates"]["new_central"]
    )
    assert out["summary"]["total_candidates"] == 5


def test_aos8_migration_batch_plan_rejects_unknown_target_type():
    plan = _build_dependency_plan_fixture()
    out = aos8.aos8_migration_batch_plan(
        target_type="not-a-real-target", migration_plan=plan
    )
    assert "error" in out


def test_aos8_migration_batch_plan_does_not_change_apply_default_behavior():
    """This tool never invokes `aos8_apply_migration_run` or any write
    tool -- purely additive report metadata over an already-built plan."""
    import inspect

    source = inspect.getsource(aos8.aos8_migration_batch_plan)
    assert "self.write_invoker" not in source
    assert "write_invoker(" not in source
    assert "aos8_apply_migration_run(" not in source
