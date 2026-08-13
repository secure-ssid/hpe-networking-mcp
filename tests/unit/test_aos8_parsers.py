"""Pure-python tests for AOS8 export parsing (no MCP/network dependency)."""

from __future__ import annotations

from hpe_networking_mcp.pipeline.aos8_parsers import (
    _normalize_optional_bool,
    parse_aaa_profiles,
    parse_ap_groups,
    parse_auth_profiles,
    parse_auth_servers,
    parse_controllers,
    parse_cp_auth_profiles,
    parse_ethernet_acls,
    parse_export,
    parse_export_report,
    parse_krb_auth_profiles,
    parse_network_destinations,
    parse_ntlm_auth_profiles,
    parse_policies,
    parse_roles,
    parse_routes,
    parse_server_groups,
    parse_stateful_dot1x_auth_profiles,
    parse_vlans,
    parse_vrrp,
    parse_whitelist_rules,
    parse_wired_auth_profiles,
    parse_wispr_auth_profiles,
    parse_wlans,
)

_EXPORT = {
    "config_path": "/md/lab",
    "wlans": {
        "ssid_profiles": [
            {"profile-name": "Corp", "essid": "Corp", "opmode": "wpa2-aes"},
            {"profile-name": "Open-Only", "essid": "Open-Only"},
        ],
        "virtual_aps": [
            {
                "profile-name": "Corp-VAP",
                "ssid-profile": "Corp",
                "aaa-profile": "dot1x",
                "vlan": 20,
                "forward-mode": "tunnel",
            },
            {
                "profile-name": "Orphan-VAP",
                "ssid-profile": "Missing-SSID",
                "vlan": 40,
            },
        ],
    },
    "roles": [
        {"role": "employee", "acl": "allowall", "vlan": 20, "captive-portal-profile": "none"},
        {"rolename": "guest", "vlan": 30},
    ],
    "vlans": [{"id": 20, "description": "Corp"}, {"id": 30, "description": "Guest"}],
    "ap_groups": [{"profile-name": "Lab-AP-Group", "virtual-ap": ["Corp-VAP"]}],
    "controllers": [{"Name": "mc1", "IP Address": "10.0.0.1", "Model": "7210"}],
    "policies": [
        {
            "accname": "corp-acl",
            "acl_sess__v4policy": [
                {
                    "source": "user",
                    "destination": "any",
                    "service": "https",
                    "action": "permit",
                    "log": True,
                    "time-range": "business-hours",
                }
            ],
            "acl_sess__v6policy": [
                {"src": "any", "dst": "any", "protocol": "icmpv6", "action": "permit"}
            ],
        }
    ],
    "aaa": {
        "aaa_profiles": [
            {
                "profile-name": "corp-aaa",
                "default_user_role": "employee",
                "dot1x_auth_profile": "corp-dot1x",
                "dot1x_server_group": "corp-sg",
                "enforce_dhcp": True,
            }
        ],
        "dot1x_auth_profiles": [
            {"profile-name": "corp-dot1x", "reauthentication": True, "quiet_period": 30}
        ],
        "mac_auth_profiles": [
            {"profile-name": "corp-mac", "mac_reauthentication": True}
        ],
        "server_groups": [
            {
                "sg_name": "corp-sg",
                "auth_server": [{"name": "rad1"}],
                "fail_thru": True,
                "load_balance": "least-outstanding",
            }
        ],
        "radius_servers": [
            {
                "rad_server_name": "rad1",
                "rad_host": "10.0.0.10",
                "rad_authport": 1812,
            }
        ],
        "ldap_servers": [
            {"ldap_server_name": "ldap1", "ldap_host": "10.0.0.11", "ldap_authport": 389}
        ],
        "tacacs_servers": [
            {
                "tacacs_server_name": "tac1",
                "tacacs_host": "10.0.0.12",
                "tacacs_tcpport": 49,
            }
        ],
    },
    "routing": {
        "ipv4_routes": [
            {
                "destip": "10.20.0.0",
                "destmask": "255.255.0.0",
                "nexthop": "10.0.0.254",
                "cost": 10,
                "zero": 0,
            }
        ],
        "ipv6_routes": [
            {
                "destip": "2001:db8:20::/64",
                "nexthop": "2001:db8::1",
                "nexthop1": "2001:db8::2",
                "vlanid": 20,
                "cost": 20,
                "cost1": 30,
                "zero": 0,
            }
        ],
        "vrrp": [
            {
                "id": 20,
                "vrrp_ip": "10.0.20.1",
                "vrrp_vlan": 20,
                "vrrp_priority": 110,
                "vrrp_preempt": True,
                "vrrp_track_vlan": 30,
            }
        ],
        "vrrp6": [
            {
                "id": 21,
                "vrrp6_ip": "2001:db8:20::1",
                "vrrp6_vlan": 20,
                "vrrp6_priority": 105,
            }
        ],
    },
}


def test_parse_wlans_merges_ssid_and_virtual_ap_by_name():
    wlans = parse_wlans(_EXPORT)
    corp = next(w for w in wlans if w.profile_name == "Corp")
    assert corp.opmode == "wpa2-aes"
    assert corp.vlan == 20
    assert corp.forward_mode == "tunnel"
    assert corp.aaa_profile == "dot1x"
    assert corp.virtual_ap_profile == "Corp-VAP"


def test_parse_wlans_keeps_ssid_profile_without_virtual_ap():
    wlans = parse_wlans(_EXPORT)
    open_only = next(w for w in wlans if w.profile_name == "Open-Only")
    assert open_only.vlan is None
    assert open_only.virtual_ap_profile is None


def test_parse_wlans_includes_virtual_ap_with_no_matching_ssid_profile():
    wlans = parse_wlans(_EXPORT)
    names = {w.profile_name for w in wlans}
    assert "Orphan-VAP" in names
    orphan = next(w for w in wlans if w.profile_name == "Orphan-VAP")
    assert orphan.vlan == 40


def test_parse_roles_probes_role_and_rolename_keys():
    roles = parse_roles(_EXPORT)
    names = {r.rolename for r in roles}
    assert names == {"employee", "guest"}
    employee = next(r for r in roles if r.rolename == "employee")
    assert employee.acl == "allowall"
    assert employee.captive_portal_profile == "none"


def test_parse_vlans_extracts_id_and_description():
    vlans = parse_vlans(_EXPORT)
    assert {(v.vlan_id, v.description) for v in vlans} == {(20, "Corp"), (30, "Guest")}


def test_parse_ap_groups_collects_virtual_ap_profiles():
    groups = parse_ap_groups(_EXPORT)
    assert len(groups) == 1
    assert groups[0].profile_name == "Lab-AP-Group"
    assert groups[0].virtual_ap_profiles == ["Corp-VAP"]


def test_parse_controllers_reads_display_field_names():
    controllers = parse_controllers(_EXPORT)
    assert len(controllers) == 1
    assert controllers[0].name == "mc1"
    assert controllers[0].ip_address == "10.0.0.1"
    assert controllers[0].model == "7210"


def test_parse_policies_counts_rules():
    policies = parse_policies(_EXPORT)
    assert len(policies) == 1
    assert policies[0].name == "corp-acl"
    assert policies[0].rule_count == 2
    assert policies[0].ipv4_rules[0].service == "https"
    assert policies[0].ipv4_rules[0].unsupported_fields == {
        "time-range": "business-hours"
    }


def test_parse_aaa_and_auth_objects_preserves_source_settings():
    aaa = parse_aaa_profiles(_EXPORT)[0]
    assert aaa.profile_name == "corp-aaa"
    assert aaa.dot1x_server_group == "corp-sg"
    assert aaa.settings == {"enforce_dhcp": True}

    dot1x = parse_auth_profiles(_EXPORT, "dot1x")[0]
    assert dot1x.settings == {"quiet_period": 30, "reauthentication": True}
    servers = parse_auth_servers(_EXPORT)
    assert {(server.server_type, server.name) for server in servers} == {
        ("radius", "rad1"),
        ("ldap", "ldap1"),
        ("tacacs", "tac1"),
    }
    assert parse_server_groups(_EXPORT)[0].auth_servers == ["rad1"]


def test_parse_aaa_profiles_recognizes_literal_mac_server_group_alias():
    """Regression for docs/aos8-migration-contract-matrix.md §3 item 1."""
    export = {
        "aaa": {
            "aaa_profiles": [
                {
                    "profile-name": "literal-key-aaa",
                    "mac_server_group": "literal-mac-sg",
                }
            ],
        },
    }
    profile = parse_aaa_profiles(export)[0]
    assert profile.mac_server_group == "literal-mac-sg"
    assert "mac_server_group" not in profile.settings


def test_parse_aaa_profiles_still_recognizes_legacy_mba_server_group_alias():
    export = {
        "aaa": {
            "aaa_profiles": [
                {"profile-name": "legacy-aaa", "mba_server_group": "legacy-mac-sg"}
            ],
        },
    }
    profile = parse_aaa_profiles(export)[0]
    assert profile.mac_server_group == "legacy-mac-sg"


def test_parse_wlans_extracts_bounded_security_signals_from_ssid_profile():
    export = {
        "wlans": {
            "ssid_profiles": [
                {
                    "profile-name": "Secure",
                    "essid": "Secure",
                    "opmode": "wpa3-aes-ccm-128-psk",
                    "wpa3_transition": True,
                    "wpa_passphrase": "super-secret-passphrase",
                    "wpa_hexkey": "deadbeef",
                }
            ],
            "virtual_aps": [],
        }
    }
    wlan = parse_wlans(export)[0]
    assert wlan.passphrase_present is True
    assert wlan.psk_hexkey_present is True
    assert wlan.wpa3_transition is True
    # These are presence-only booleans, never the secret value itself.
    assert isinstance(wlan.passphrase_present, bool)
    assert isinstance(wlan.psk_hexkey_present, bool)
    # The actual secret value still lives in `raw` for forward compatibility
    # (as with every other AOS8 field) and is redacted downstream by
    # `hpe_networking_mcp.pipeline.aos8_migration` when candidates are built — never by the
    # parser layer.
    assert wlan.raw["ssid_profile"]["wpa_passphrase"] == "super-secret-passphrase"


def test_parse_wlans_defaults_security_signals_when_absent():
    export = {
        "wlans": {
            "ssid_profiles": [{"profile-name": "Plain", "essid": "Plain"}],
            "virtual_aps": [],
        }
    }
    wlan = parse_wlans(export)[0]
    assert wlan.wpa3_transition is None
    assert wlan.passphrase_present is False
    assert wlan.psk_hexkey_present is False


def test_normalize_optional_bool_accepts_actual_booleans():
    assert _normalize_optional_bool(True) is True
    assert _normalize_optional_bool(False) is False


def test_normalize_optional_bool_accepts_integer_zero_or_one():
    assert _normalize_optional_bool(1) is True
    assert _normalize_optional_bool(0) is False


def test_normalize_optional_bool_rejects_other_integers_as_ambiguous():
    # e.g. an unrelated enum ordinal is not a documented AOS8 boolean shape.
    assert _normalize_optional_bool(2) is None
    assert _normalize_optional_bool(-1) is None


def test_normalize_optional_bool_accepts_explicit_true_false_strings():
    for token in ("true", "TRUE", " True ", "1", "yes", "enable", "enabled"):
        assert _normalize_optional_bool(token) is True, token
    for token in ("false", "FALSE", " False ", "0", "no", "disable", "disabled"):
        assert _normalize_optional_bool(token) is False, token


def test_normalize_optional_bool_rejects_ambiguous_strings():
    assert _normalize_optional_bool("") is None
    assert _normalize_optional_bool("maybe") is None
    assert _normalize_optional_bool("wpa2-psk-aes") is None


def test_normalize_optional_bool_unwraps_single_key_and_double_wrapped_dicts():
    """AOS8 sometimes double-wraps a scalar as ``{key: {key: val}}`` (secondary,
    same-owner prior art, not an authoritative API contract):
    https://github.com/secure-ssid/aos8-migration-tool/blob/7bfa884d8e8f1c7e97a7bfa42f15596aa42fcf79/docs/API-NOTES.md#L57-L64
    """
    assert _normalize_optional_bool({"wpa3_transition": True}) is True
    assert _normalize_optional_bool({"wpa3_transition": {"wpa3_transition": True}}) is True
    assert _normalize_optional_bool({"enabled": "false"}) is False


def test_normalize_optional_bool_treats_empty_or_multi_key_dicts_as_ambiguous():
    # An empty dict is ambiguous, not automatically True/False.
    assert _normalize_optional_bool({}) is None
    assert _normalize_optional_bool({"a": True, "b": False}) is None


def test_normalize_optional_bool_treats_lists_and_none_as_ambiguous():
    assert _normalize_optional_bool([True]) is None
    assert _normalize_optional_bool([]) is None
    assert _normalize_optional_bool(None) is None


def test_parse_wlans_wpa3_transition_accepts_documented_flag_variants():
    def _wlan_with_transition(name: str, raw_transition) -> dict:
        return {
            "profile-name": name,
            "essid": name,
            "opmode": "wpa2-psk-aes",
            "wpa3_transition": raw_transition,
            "wpa_passphrase": "flag-variant-passphrase",
        }

    export = {
        "wlans": {
            "ssid_profiles": [
                _wlan_with_transition("BoolTrue", True),
                _wlan_with_transition("BoolFalse", False),
                _wlan_with_transition("IntOne", 1),
                _wlan_with_transition("StringYes", "yes"),
                _wlan_with_transition("StringNo", "no"),
                _wlan_with_transition("Nested", {"wpa3_transition": True}),
                _wlan_with_transition("AmbiguousDict", {"a": True, "b": False}),
                _wlan_with_transition("AmbiguousEmptyDict", {}),
                _wlan_with_transition("AmbiguousList", [True]),
                _wlan_with_transition("AmbiguousString", "unspecified"),
            ],
            "virtual_aps": [],
        }
    }
    wlans = {w.profile_name: w for w in parse_wlans(export)}
    assert wlans["BoolTrue"].wpa3_transition is True
    assert wlans["BoolFalse"].wpa3_transition is False
    assert wlans["IntOne"].wpa3_transition is True
    assert wlans["StringYes"].wpa3_transition is True
    assert wlans["StringNo"].wpa3_transition is False
    assert wlans["Nested"].wpa3_transition is True
    # Ambiguous shapes must never be guessed as True -- they stay None, same
    # as a genuinely absent field.
    assert wlans["AmbiguousDict"].wpa3_transition is None
    assert wlans["AmbiguousEmptyDict"].wpa3_transition is None
    assert wlans["AmbiguousList"].wpa3_transition is None
    assert wlans["AmbiguousString"].wpa3_transition is None


def test_parse_wlans_recognizes_ssid_prof_hyphenated_alias():
    """Regression: some AOS8 builds reference the SSID profile under
    "ssid-prof" (all-hyphen) rather than "ssid_prof"/"ssid-profile"
    (secondary, same-owner prior art, not an authoritative API contract):
    https://github.com/secure-ssid/aos8-migration-tool/blob/7bfa884d8e8f1c7e97a7bfa42f15596aa42fcf79/lib/aos8_client.py#L315-L399
    """
    export = {
        "wlans": {
            "ssid_profiles": [
                {"profile-name": "HyphenProf", "essid": "HyphenProf", "opmode": "opensystem"}
            ],
            "virtual_aps": [
                {
                    "profile-name": "HyphenProf-VAP",
                    "ssid-prof": "HyphenProf",
                    "vlan": 90,
                }
            ],
        }
    }
    wlan = next(w for w in parse_wlans(export) if w.profile_name == "HyphenProf")
    assert wlan.vlan == 90
    assert wlan.virtual_ap_profile == "HyphenProf-VAP"


def test_parse_routes_and_vrrp_capture_ipv4_and_ipv6_fields():
    routes = parse_routes(_EXPORT)
    assert routes[0].netmask == "255.255.0.0"
    assert routes[1].secondary_next_hop == "2001:db8::2"
    vrrp = parse_vrrp(_EXPORT)
    assert vrrp[0].tracking == {"vlan": 30}
    assert vrrp[1].address_family == "ipv6"


def test_parse_export_returns_all_object_types():
    parsed = parse_export(_EXPORT)
    assert set(parsed) == {
        "wlans",
        "roles",
        "vlans",
        "ap_groups",
        "controllers",
        "policies",
        "aaa_profiles",
        "dot1x_auth_profiles",
        "mac_auth_profiles",
        "server_groups",
        "auth_servers",
        "routes",
        "vrrp",
        "network_destinations",
        "ethernet_acls",
        "whitelist_rules",
        "wired_auth_profiles",
        "stateful_dot1x_auth_profiles",
        "wispr_auth_profiles",
        "cp_auth_profiles",
        "krb_auth_profiles",
        "ntlm_auth_profiles",
    }
    assert len(parsed["wlans"]) == 3
    assert len(parsed["roles"]) == 2


def test_parse_export_tolerates_non_dict_input():
    parsed = parse_export(None)  # type: ignore[arg-type]
    assert parsed["wlans"] == []
    assert parsed["roles"] == []


def test_parse_export_report_warns_for_malformed_partial_objects():
    parsed, warnings = parse_export_report(
        {
            "wlans": {"ssid_profiles": [{"essid": "missing-name"}], "virtual_aps": []},
            "roles": [None],
            "vlans": [],
            "ap_groups": [],
            "controllers": [],
            "policies": [{"accname": "bad", "acl_sess__v4policy": "not-a-list"}],
            "aaa": {
                "aaa_profiles": [],
                "dot1x_auth_profiles": [],
                "mac_auth_profiles": [],
                "server_groups": [],
                "radius_servers": [],
                "ldap_servers": [],
                "tacacs_servers": [],
            },
            "routing": {
                "ipv4_routes": [{"cost": 1}],
                "ipv6_routes": [],
                "vrrp": [{"vrrp_ip": "10.0.0.1"}],
                "vrrp6": [],
            },
        }
    )

    assert parsed["wlans"][0].profile_name == "unknown-0"
    assert any("roles[0] is not an object" in warning for warning in warnings)
    assert any("expected a list of rules" in warning for warning in warnings)
    assert any("has no destination" in warning for warning in warnings)
    assert any("has no VRRP ID" in warning for warning in warnings)


# ---------------------------------------------------------------------------
# Network destination aliases (netdst/netdst6), Ethernet ACLs (acl_eth), and
# IP-classification whitelist rules (whitelist_rule) -- new source families.
# ---------------------------------------------------------------------------

_NETDST_EXPORT = {
    "netdst": [
        {
            "dstname": "corp-servers",
            "netdst__desc": "Corp servers",
            "netdst__network": "10.20.0.0/16",
        },
        {
            "dstname": "voip-dest",
            "netdst__host": "10.30.0.5",
            "netdst__invert": "1",
        },
    ],
    "netdst6": [
        {"dstname": "corp-servers-v6", "netdst6__network": "2001:db8::/64"},
    ],
}


def test_parse_network_destinations_reads_ipv4_and_ipv6_prefixed_fields():
    destinations = parse_network_destinations(_NETDST_EXPORT)
    assert len(destinations) == 3
    corp = next(d for d in destinations if d.name == "corp-servers")
    assert corp.address_family == "ipv4"
    assert corp.description == "Corp servers"
    assert corp.network == "10.20.0.0/16"

    voip = next(d for d in destinations if d.name == "voip-dest")
    assert voip.host == "10.30.0.5"
    assert voip.invert is True

    corp_v6 = next(d for d in destinations if d.name == "corp-servers-v6")
    assert corp_v6.address_family == "ipv6"
    assert corp_v6.network == "2001:db8::/64"


def test_parse_network_destinations_warns_on_missing_identifier():
    warnings: list[str] = []
    destinations = parse_network_destinations(
        {"netdst": [{"netdst__desc": "no name"}]}, warnings
    )
    assert destinations[0].name == "unknown-0"
    assert any("netdst[0]" in warning for warning in warnings)


def test_parse_network_destinations_tolerates_entirely_absent_sections():
    """Older saved exports may omit these sections without being malformed."""
    warnings: list[str] = []
    destinations = parse_network_destinations({}, warnings)
    assert destinations == []
    assert warnings == []


def test_parse_network_destinations_still_warns_when_section_is_malformed():
    warnings: list[str] = []
    destinations = parse_network_destinations({"netdst": "not-a-list"}, warnings)
    assert destinations == []
    assert any("netdst section is missing or malformed" in warning for warning in warnings)


_ACL_ETH_EXPORT = {
    "acl_eth": [
        {
            "accname": "eth-200",
            "acl_eth__policy": [
                {
                    "source": "any",
                    "destination": "any",
                    "ethertype": "0x0800",
                    "action": "permit",
                    "priority": 5,
                },
                {"src": "host-a", "dst": "host-b", "type": "0x86dd", "deny": True},
            ],
        }
    ]
}


def test_parse_ethernet_acls_counts_rules_and_probes_aliases():
    acls = parse_ethernet_acls(_ACL_ETH_EXPORT)
    assert len(acls) == 1
    acl = acls[0]
    assert acl.name == "eth-200"
    assert acl.rule_count == 2
    assert acl.rules[0].ethertype == "0x0800"
    assert acl.rules[0].action == "permit"
    assert acl.rules[0].unsupported_fields == {"priority": 5}
    assert acl.rules[1].source == "host-a"
    assert acl.rules[1].destination == "host-b"
    assert acl.rules[1].ethertype == "0x86dd"


def test_parse_ethernet_acls_tolerates_entirely_absent_section():
    warnings: list[str] = []
    acls = parse_ethernet_acls({}, warnings)
    assert acls == []
    assert warnings == []


def test_parse_ethernet_acls_warns_on_malformed_rule_list():
    warnings: list[str] = []
    acls = parse_ethernet_acls(
        {"acl_eth": [{"accname": "bad", "acl_eth__policy": "not-a-list"}]}, warnings
    )
    assert acls[0].rule_count == 1
    assert acls[0].rules[0].unsupported_fields == {"raw_rule": "not-a-list"}
    assert any("expected a list of rules" in warning for warning in warnings)


_WHITELIST_RULE_EXPORT = {
    "whitelist_rule": [
        {"sipaddr": "10.0.0.1", "eipaddr": "10.0.0.50"},
        {"sipaddr": "10.0.1.1"},
    ]
}


def test_parse_whitelist_rules_reads_start_and_end_ip():
    rules = parse_whitelist_rules(_WHITELIST_RULE_EXPORT)
    assert len(rules) == 2
    assert rules[0].start_ip == "10.0.0.1"
    assert rules[0].end_ip == "10.0.0.50"


def test_parse_whitelist_rules_warns_when_end_ip_is_missing():
    warnings: list[str] = []
    rules = parse_whitelist_rules(_WHITELIST_RULE_EXPORT, warnings)
    assert rules[1].start_ip == "10.0.1.1"
    assert rules[1].end_ip is None
    assert any("missing a start or end IP address" in warning for warning in warnings)


def test_parse_whitelist_rules_tolerates_entirely_absent_section():
    warnings: list[str] = []
    rules = parse_whitelist_rules({}, warnings)
    assert rules == []
    assert warnings == []


def test_parse_export_report_never_warns_for_new_families_when_absent():
    """Regression: the new-family parsers must not add noise to every
    existing export that predates `aos8_export_all()` fetching them."""
    _, warnings = parse_export_report(
        {
            "wlans": {"ssid_profiles": [], "virtual_aps": []},
            "roles": [],
            "vlans": [],
            "ap_groups": [],
            "controllers": [],
            "policies": [],
            "aaa": {
                "aaa_profiles": [],
                "dot1x_auth_profiles": [],
                "mac_auth_profiles": [],
                "server_groups": [],
                "radius_servers": [],
                "ldap_servers": [],
                "tacacs_servers": [],
            },
            "routing": {"ipv4_routes": [], "ipv6_routes": [], "vrrp": [], "vrrp6": []},
        }
    )
    assert not any(
        family in warning
        for warning in warnings
        for family in (
            "netdst",
            "acl_eth",
            "whitelist_rule",
            "wired_auth_profile",
            "stateful_dot1x_auth_profile",
            "wispr_auth_profile",
            "cp_auth_profile",
            "krb_auth_profile",
            "ntlm_auth_profile",
        )
    )


# ---------------------------------------------------------------------------
# Wired / captive-portal / WISPr / Kerberos / NTLM / stateful-802.1X
# authentication-profile families (reference-only; see
# `hpe_networking_mcp.pipeline.aos8_schema.REFERENCE_ONLY_OBJECT_TYPES`).
# ---------------------------------------------------------------------------

_WIRED_AUTH_PROFILE_EXPORT = {
    "aaa": {
        "wired_auth_profiles": [
            {"wired_aaa_profile": "corp-aaa", "wired_blacklist_time": 3600}
        ]
    }
}


def test_parse_wired_auth_profiles_reads_singleton_and_settings():
    profiles = parse_wired_auth_profiles(_WIRED_AUTH_PROFILE_EXPORT)
    assert len(profiles) == 1
    assert profiles[0].aaa_profile == "corp-aaa"
    assert profiles[0].blacklist_time == 3600
    assert profiles[0].settings == {}


def test_parse_wired_auth_profiles_warns_on_more_than_one_entry():
    warnings: list[str] = []
    export = {
        "aaa": {
            "wired_auth_profiles": [
                {"wired_aaa_profile": "corp-aaa"},
                {"wired_aaa_profile": "guest-aaa"},
            ]
        }
    }
    profiles = parse_wired_auth_profiles(export, warnings)
    assert len(profiles) == 2
    assert any("singleton object" in warning for warning in warnings)


def test_parse_wired_auth_profiles_tolerates_entirely_absent_section():
    warnings: list[str] = []
    assert parse_wired_auth_profiles({}, warnings) == []
    assert warnings == []


def test_parse_stateful_dot1x_auth_profiles_reads_singleton_fields():
    export = {
        "aaa": {
            "stateful_dot1x_auth_profiles": [
                {
                    "stateful_dot1x_mode": "enabled",
                    "stateful_dot1x_server_group": "corp-sg",
                    "statefuldot1x_default_role": "guest",
                    "timeout": 300,
                    "unmapped_field": "kept",
                }
            ]
        }
    }
    profiles = parse_stateful_dot1x_auth_profiles(export)
    assert len(profiles) == 1
    assert profiles[0].mode == "enabled"
    assert profiles[0].server_group == "corp-sg"
    assert profiles[0].default_role == "guest"
    assert profiles[0].timeout == 300
    assert profiles[0].settings == {"unmapped_field": "kept"}


def test_parse_stateful_dot1x_auth_profiles_tolerates_entirely_absent_section():
    assert parse_stateful_dot1x_auth_profiles({}) == []


def test_parse_wispr_auth_profiles_reads_named_profiles_and_settings():
    export = {
        "aaa": {
            "wispr_auth_profiles": [
                {
                    "profile-name": "wispr1",
                    "wispr_default_role": "guest",
                    "wispr_server_group": "corp-sg",
                    "wispr_max_delay": 5,
                }
            ]
        }
    }
    profiles = parse_wispr_auth_profiles(export)
    assert len(profiles) == 1
    assert profiles[0].profile_name == "wispr1"
    assert profiles[0].default_role == "guest"
    assert profiles[0].server_group == "corp-sg"
    assert profiles[0].settings == {"wispr_max_delay": 5}


def test_parse_wispr_auth_profiles_warns_on_missing_identifier():
    warnings: list[str] = []
    export = {"aaa": {"wispr_auth_profiles": [{"wispr_default_role": "guest"}]}}
    profiles = parse_wispr_auth_profiles(export, warnings)
    assert profiles[0].profile_name == "unknown-0"
    assert any("wispr_auth_profiles[0]" in warning for warning in warnings)


def test_parse_wispr_auth_profiles_tolerates_entirely_absent_section():
    assert parse_wispr_auth_profiles({}) == []


def test_parse_cp_auth_profiles_reads_named_profiles_and_settings():
    export = {
        "aaa": {
            "cp_auth_profiles": [
                {
                    "profile-name": "cp1",
                    "cp_default_role": "guest",
                    "cp_default_guest_role": "guest2",
                    "cp_server_group": "corp-sg",
                    "cp_redirect_url": "http://example.com",
                }
            ]
        }
    }
    profiles = parse_cp_auth_profiles(export)
    assert len(profiles) == 1
    assert profiles[0].profile_name == "cp1"
    assert profiles[0].default_role == "guest"
    assert profiles[0].default_guest_role == "guest2"
    assert profiles[0].server_group == "corp-sg"
    assert profiles[0].settings == {"cp_redirect_url": "http://example.com"}


def test_parse_cp_auth_profiles_tolerates_entirely_absent_section():
    assert parse_cp_auth_profiles({}) == []


def test_parse_krb_auth_profiles_reads_named_profiles_and_settings():
    export = {
        "aaa": {
            "krb_auth_profiles": [
                {
                    "profile-name": "krb1",
                    "krb_default_role": "guest",
                    "krb_server_group": "corp-sg",
                    "krb_timeout": 30,
                    "krb_auth_profile_clone": "template1",
                }
            ]
        }
    }
    profiles = parse_krb_auth_profiles(export)
    assert len(profiles) == 1
    assert profiles[0].profile_name == "krb1"
    assert profiles[0].default_role == "guest"
    assert profiles[0].server_group == "corp-sg"
    assert profiles[0].timeout == 30
    assert profiles[0].settings == {"krb_auth_profile_clone": "template1"}


def test_parse_krb_auth_profiles_tolerates_entirely_absent_section():
    assert parse_krb_auth_profiles({}) == []


def test_parse_ntlm_auth_profiles_reads_named_profiles_and_settings():
    export = {
        "aaa": {
            "ntlm_auth_profiles": [
                {
                    "profile-name": "ntlm1",
                    "ntlm_default_role": "guest",
                    "ntlm_server_group": "corp-sg",
                    "ntlm_enable": True,
                    "ntlm_timeout": 60,
                }
            ]
        }
    }
    profiles = parse_ntlm_auth_profiles(export)
    assert len(profiles) == 1
    assert profiles[0].profile_name == "ntlm1"
    assert profiles[0].default_role == "guest"
    assert profiles[0].server_group == "corp-sg"
    assert profiles[0].enabled is True
    assert profiles[0].timeout == 60
    assert profiles[0].settings == {}


def test_parse_ntlm_auth_profiles_tolerates_entirely_absent_section():
    assert parse_ntlm_auth_profiles({}) == []
