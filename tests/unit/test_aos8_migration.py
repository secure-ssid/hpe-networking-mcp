"""Pure-python tests for deterministic AOS8 migration planning (no MCP/network)."""

from __future__ import annotations

import json

from hpe_networking_mcp.pipeline.aos8_migration import _is_sensitive_key, build_migration_plan

_EXPORT = {
    "config_path": "/md/lab",
    "wlans": {
        "ssid_profiles": [{"profile-name": "Corp", "essid": "Corp", "opmode": "wpa2-aes"}],
        "virtual_aps": [
            {
                "profile-name": "Corp-VAP",
                "ssid-profile": "Corp",
                "aaa-profile": "corp-aaa",
                "vlan": 20,
                "forward-mode": "tunnel",
            }
        ],
    },
    "roles": [
        {"role": "employee", "acl": "allowall", "vlan": 20, "captive-portal-profile": "none"}
    ],
    "vlans": [{"id": 20, "description": "Corp"}],
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
                    "time-range": "business-hours",
                }
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
                "shared-secret": "aaa-profile-secret",
            }
        ],
        "dot1x_auth_profiles": [
            {
                "profile-name": "corp-dot1x",
                "reauthentication": True,
                "keycache_tmout": 600,
                "server_cert": "corp-server-cert",
                "use_session_key": True,
                "password": "dot1x-profile-secret",
            }
        ],
        "mac_auth_profiles": [
            {"profile-name": "corp-mac", "mac_reauthentication": True}
        ],
        "server_groups": [
            {
                "sg_name": "corp-sg",
                "auth_server": [{"name": "rad1", "position": 1}],
                "fail_thru": True,
            }
        ],
        "radius_servers": [
            {
                "rad_server_name": "rad1",
                "rad_host": "10.0.0.10",
                "rad_authport": 1812,
                "rad_key": "radius-shared-secret",
                "cppm_username_password": "cppm-combined-secret",
            }
        ],
        "ldap_servers": [
            {
                "ldap_server_name": "ldap1",
                "ldap_host": "10.0.0.11",
                "ldap_admindn": "cn=bind-user,dc=example,dc=com",
                "ldap_adminpasswd": "ldap-bind-secret",
                "ldap_keyattribute": "sAMAccountName",
            }
        ],
        "tacacs_servers": [
            {
                "tacacs_server_name": "tac1",
                "tacacs_host": "10.0.0.12",
                "tacacs_key": "tacacs-shared-secret",
                "tacacs_timeout": 5,
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
            }
        ],
        "vrrp6": [],
    },
}


def test_build_migration_plan_is_deterministic():
    first = build_migration_plan(_EXPORT)
    second = build_migration_plan(_EXPORT)
    assert first == second


def test_build_migration_plan_surfaces_partial_export_warnings():
    export = {
        "config_path": "/md",
        "wlans": {"ssid_profiles": "bad", "virtual_aps": []},
        "roles": [],
        "vlans": [],
        "ap_groups": [],
        "controllers": [],
        "policies": [None],
        "warnings": ["controllers: HTTP 503"],
    }

    plan = build_migration_plan(export)

    assert "export: controllers: HTTP 503" in plan["warnings"]
    assert (
        "export: wlans.ssid_profiles section is missing or malformed."
        in plan["warnings"]
    )
    assert "export: policies[0] is not an object and was not parsed." in plan["warnings"]


def test_build_migration_plan_produces_classic_and_new_central_candidates():
    plan = build_migration_plan(_EXPORT)
    classic_types = {c["object_type"] for c in plan["candidates"]["classic_central"]}
    new_types = {c["object_type"] for c in plan["candidates"]["new_central"]}
    assert classic_types == {
        "wlan",
        "role",
        "vlan",
        "ap_group",
        "controller",
        "policy",
        "aaa_profile",
        "dot1x_auth_profile",
        "mac_auth_profile",
        "server_group",
        "auth_server",
        "route",
        "vrrp",
    }
    # Controllers have no New Central object equivalent, by design.
    assert new_types == classic_types - {"controller"}


def test_build_migration_plan_warns_on_lossy_wlan_and_role_fields():
    plan = build_migration_plan(_EXPORT)
    joined = " ".join(plan["warnings"])
    assert "opmode" in joined
    assert "captive-portal" in joined
    assert "controllers/Mobility Conductors are not" in joined


def test_build_migration_plan_diff_has_sorted_source_and_candidate_keys():
    plan = build_migration_plan(_EXPORT)
    wlan_diff = plan["diff"]["wlan:Corp"]
    source_keys = [key for key, _ in wlan_diff["source"]]
    candidate_keys = [key for key, _ in wlan_diff["candidate"]]
    assert source_keys == sorted(source_keys)
    assert candidate_keys == sorted(candidate_keys)


def test_build_migration_plan_verification_plan_references_real_tool_names_only():
    plan = build_migration_plan(_EXPORT)
    tool_names = {step["tool"] for step in plan["verification_plan"]}
    assert tool_names == {"list_overlay_wlans", "list_roles", "list_named_vlans", "list_devices"}
    for step in plan["verification_plan"]:
        assert "purpose" in step
        assert "args" in step


def test_build_migration_plan_source_object_counts_match_input():
    plan = build_migration_plan(_EXPORT)
    assert plan["source_object_counts"] == {
        "aaa_profiles": 1,
        "ap_groups": 1,
        "auth_servers": 3,
        "controllers": 1,
        "cp_auth_profiles": 0,
        "dot1x_auth_profiles": 1,
        "ethernet_acls": 0,
        "krb_auth_profiles": 0,
        "mac_auth_profiles": 1,
        "network_destinations": 0,
        "ntlm_auth_profiles": 0,
        "policies": 1,
        "roles": 1,
        "routes": 2,
        "server_groups": 1,
        "stateful_dot1x_auth_profiles": 0,
        "vlans": 1,
        "vrrp": 1,
        "whitelist_rules": 0,
        "wired_auth_profiles": 0,
        "wispr_auth_profiles": 0,
        "wlans": 1,
    }


def test_build_migration_plan_orders_dependencies_before_dependents():
    plan = build_migration_plan(_EXPORT)
    candidates = plan["candidates"]["new_central"]
    assert [candidate["apply_order"] for candidate in candidates] == sorted(
        candidate["apply_order"] for candidate in candidates
    )
    by_key = {
        f"{candidate['object_type']}:{candidate['identifier']}": candidate
        for candidate in candidates
    }
    assert by_key["server_group:corp-sg"]["dependencies"] == [
        "auth_server:radius:rad1"
    ]
    assert by_key["server_group:corp-sg"]["payload"]["auth_server_entries"] == [
        {"name": "rad1", "position": 1}
    ]
    assert by_key["aaa_profile:corp-aaa"]["dependencies"] == [
        "dot1x_auth_profile:corp-dot1x",
        "role:employee",
        "server_group:corp-sg",
    ]
    assert by_key["wlan:Corp"]["dependencies"] == [
        "aaa_profile:corp-aaa",
        "vlan:20",
    ]


def test_build_migration_plan_preserves_unmapped_fields_and_policy_details():
    plan = build_migration_plan(_EXPORT)
    candidates = plan["candidates"]["classic_central"]
    aaa = next(candidate for candidate in candidates if candidate["object_type"] == "aaa_profile")
    radius = next(
        candidate
        for candidate in candidates
        if candidate["object_type"] == "auth_server"
        and candidate["payload"]["server_type"] == "radius"
    )
    policy = next(candidate for candidate in candidates if candidate["object_type"] == "policy")

    assert aaa["unsupported_fields"] == {
        "enforce_dhcp": True,
        "shared-secret": "<redacted:present>",
    }
    assert radius["unsupported_fields"] == {
        "cppm_username_password": "<redacted:present>",
        "rad_authport": 1812,
        "rad_key": "<redacted:present>",
    }
    assert policy["payload"]["rules"][0]["service"] == "https"
    assert policy["unsupported_fields"] == {
        "ipv4_rules[0].time-range": "business-hours"
    }
    ipv6_route = next(
        candidate
        for candidate in candidates
        if candidate["object_type"] == "route"
        and candidate["payload"]["address_family"] == "ipv6"
    )
    vrrp = next(candidate for candidate in candidates if candidate["object_type"] == "vrrp")
    assert ipv6_route["payload"]["secondary_next_hop"] == "2001:db8::2"
    assert ipv6_route["dependencies"] == ["vlan:20"]
    assert vrrp["payload"]["priority"] == 110
    assert vrrp["dependencies"] == ["vlan:20"]
    assert any("exact value is retained" in warning for warning in plan["warnings"])


def test_build_migration_plan_never_serializes_auth_secrets():
    plan = build_migration_plan(_EXPORT)
    serialized = json.dumps(plan, sort_keys=True)
    secret_values = {
        "aaa-profile-secret",
        "dot1x-profile-secret",
        "radius-shared-secret",
        "cppm-combined-secret",
        "ldap-bind-secret",
        "tacacs-shared-secret",
    }
    assert all(secret not in serialized for secret in secret_values)
    # The LDAP admin/bind DN is a non-secret identifier (not a credential) and
    # must remain visible/usable in the candidate payload — only the
    # accompanying bind *password* is redacted. See
    # docs/aos8-migration-contract-matrix.md §3 item 6.
    assert "cn=bind-user,dc=example,dc=com" in serialized

    candidates = plan["candidates"]["classic_central"]
    radius = next(
        candidate
        for candidate in candidates
        if candidate["object_type"] == "auth_server"
        and candidate["payload"]["server_type"] == "radius"
    )
    ldap = next(
        candidate
        for candidate in candidates
        if candidate["object_type"] == "auth_server"
        and candidate["payload"]["server_type"] == "ldap"
    )
    tacacs = next(
        candidate
        for candidate in candidates
        if candidate["object_type"] == "auth_server"
        and candidate["payload"]["server_type"] == "tacacs"
    )
    dot1x = next(
        candidate
        for candidate in candidates
        if candidate["object_type"] == "dot1x_auth_profile"
    )

    assert radius["requires_secret_input"] is True
    assert radius["secret_fields"] == [
        "unsupported_fields.cppm_username_password",
        "unsupported_fields.rad_key",
    ]
    assert (
        ldap["unsupported_fields"]["ldap_admindn"] == "cn=bind-user,dc=example,dc=com"
    )
    assert ldap["unsupported_fields"]["ldap_adminpasswd"] == "<redacted:present>"
    assert ldap["unsupported_fields"]["ldap_keyattribute"] == "sAMAccountName"
    assert ldap["requires_secret_input"] is True
    assert ldap["secret_fields"] == ["unsupported_fields.ldap_adminpasswd"]
    assert tacacs["unsupported_fields"]["tacacs_key"] == "<redacted:present>"
    assert tacacs["unsupported_fields"]["tacacs_timeout"] == 5
    assert dot1x["unsupported_fields"]["password"] == "<redacted:present>"
    assert dot1x["unsupported_fields"]["keycache_tmout"] == 600
    assert dot1x["unsupported_fields"]["server_cert"] == "corp-server-cert"
    assert dot1x["unsupported_fields"]["use_session_key"] is True
    assert any("re-enter this credential" in warning for warning in plan["warnings"])


def test_ldap_admin_dn_stays_visible_while_bind_password_is_redacted():
    """Regression for docs/aos8-migration-contract-matrix.md §3 item 6."""
    export = {
        "config_path": "/md",
        "aaa": {
            "ldap_servers": [
                {
                    "ldap_server_name": "ldap1",
                    "ldap_host": "10.0.0.11",
                    "ldap_admindn": "cn=svc-bind,dc=example,dc=com",
                    "ldap_adminpasswd": "super-secret",
                }
            ],
        },
    }
    plan = build_migration_plan(export)
    ldap = next(
        candidate
        for candidate in plan["candidates"]["new_central"]
        if candidate["object_type"] == "auth_server"
    )
    assert ldap["unsupported_fields"]["ldap_admindn"] == "cn=svc-bind,dc=example,dc=com"
    assert ldap["unsupported_fields"]["ldap_adminpasswd"] == "<redacted:present>"
    assert ldap["requires_secret_input"] is True
    assert ldap["secret_fields"] == ["unsupported_fields.ldap_adminpasswd"]
    serialized = json.dumps(plan, sort_keys=True)
    assert "cn=svc-bind,dc=example,dc=com" in serialized
    assert "super-secret" not in serialized


def test_sensitive_key_detection_covers_credentials_without_false_positives():
    for key in (
        "rad_key",
        "radius-shared-secret",
        "ldap_adminpasswd",
        "bind_password",
        "tacacsKey",
        "sharedSecret",
        "client_secret",
        "api-token",
        "pwd",
        "wpa_hexkey",
        "wepkey1",
    ):
        assert _is_sensitive_key(key)

    for key in (
        "keycache_tmout",
        "ldap_keyattribute",
        "server_cert",
        "token_caching_period",
        "use_session_key",
        "wpa_key_retries",
        "ldap-admin-dn",
        "ldap_admindn",
    ):
        assert not _is_sensitive_key(key)


def test_sensitive_key_detection_evaluates_flattened_path_like_keys_by_leaf():
    """Regression: `_wlan_payload` flattens nested ssid_profile/virtual_ap
    dict keys into path-like strings (e.g. `f"ssid_profile.{key}"`). The
    leading, non-secret path segment must not dilute an otherwise-sensitive
    leaf token out of `_SENSITIVE_EXACT_KEYS`/prefix+suffix matching."""
    for key in (
        "ssid_profile.wpa_hexkey",
        "ssid_profile.wepkey1",
        "ssid_profile.wepkey2",
        "ssid_profile.wepkey3",
        "ssid_profile.wepkey4",
        "ssid_profile.wpa_passphrase",
        "ssid_profile.psk",
        "ssid_profile.key",
        "auth_server.rad_key",
        "auth_server.radius_key",
        "auth_server.tacacs_key",
        "auth_server.ldap_adminpasswd",
        "auth_server.ldap_adminpwd",
        "auth_server.shared_secret",
        # Nested more than one level deep, and using "/" as a separator.
        "ssid_profile/nested/wepkey1",
    ):
        assert _is_sensitive_key(key), f"expected {key!r} to be treated as sensitive"

    # Non-secret path-like keys (including the exact fields this flattening
    # emits today for unmapped, non-secret ssid_profile/virtual_ap settings)
    # must not be masked.
    for key in (
        "ssid_profile.opmode",
        "virtual_ap.forward_mode",
        "ssid_profile.wpa3_transition",
        "auth_server.ldap_admindn",
    ):
        assert not _is_sensitive_key(key), f"expected {key!r} to stay unredacted"


def test_wlan_secret_material_never_appears_in_plan_json():
    """End-to-end regression for the flattened-key secret leak: an AOS8
    ssid_profile carrying WPA passphrase/hex-key and WEP key 1-4 material
    (fields with no dedicated mapping, so they flow through the flattened
    `ssid_profile.*` `unsupported_fields` path) must never surface the actual
    secret values anywhere in the serialized migration plan."""
    wpa_passphrase = "correct-horse-battery-staple"
    wpa_hexkey = "deadbeefcafebabe0011223344556677"
    wep_keys = {
        "wepkey1": "1111111111",
        "wepkey2": "2222222222",
        "wepkey3": "3333333333",
        "wepkey4": "4444444444",
    }
    export = _wlan_export(
        {
            "profile-name": "Legacy",
            "essid": "Legacy",
            "opmode": "wpa2-psk-aes",
            "wpa_passphrase": wpa_passphrase,
            "wpa_hexkey": wpa_hexkey,
            **wep_keys,
        }
    )
    plan = build_migration_plan(export)
    serialized = json.dumps(plan)

    for secret in (wpa_passphrase, wpa_hexkey, *wep_keys.values()):
        assert secret not in serialized

    candidate = next(
        c
        for c in plan["candidates"]["new_central"]
        if c["object_type"] == "wlan" and c["identifier"] == "Legacy"
    )
    unsupported = candidate["unsupported_fields"]
    assert unsupported["ssid_profile.wpa_passphrase"] == "<redacted:present>"
    assert unsupported["ssid_profile.wpa_hexkey"] == "<redacted:present>"
    for key in wep_keys:
        assert unsupported[f"ssid_profile.{key}"] == "<redacted:present>"
    assert candidate["requires_secret_input"] is True
    for key in ("wpa_passphrase", "wpa_hexkey", *wep_keys):
        assert f"unsupported_fields.ssid_profile.{key}" in candidate["secret_fields"]


def test_build_migration_plan_handles_empty_export():
    plan = build_migration_plan({})
    assert plan["candidates"]["classic_central"] == []
    assert plan["candidates"]["new_central"] == []
    assert "export: wlans section is missing or malformed." in plan["warnings"]
    assert "export: roles section is missing or malformed." in plan["warnings"]
    assert plan["source_object_counts"] == {
        "aaa_profiles": 0,
        "ap_groups": 0,
        "auth_servers": 0,
        "controllers": 0,
        "cp_auth_profiles": 0,
        "dot1x_auth_profiles": 0,
        "ethernet_acls": 0,
        "krb_auth_profiles": 0,
        "mac_auth_profiles": 0,
        "network_destinations": 0,
        "ntlm_auth_profiles": 0,
        "policies": 0,
        "roles": 0,
        "routes": 0,
        "server_groups": 0,
        "stateful_dot1x_auth_profiles": 0,
        "vlans": 0,
        "vrrp": 0,
        "whitelist_rules": 0,
        "wired_auth_profiles": 0,
        "wispr_auth_profiles": 0,
        "wlans": 0,
    }


def _wlan_export(ssid_profile: dict, virtual_ap: dict | None = None) -> dict:
    return {
        "config_path": "/md",
        "wlans": {
            "ssid_profiles": [ssid_profile],
            "virtual_aps": [virtual_ap] if virtual_ap else [],
        },
    }


def _wlan_security(plan: dict, name: str) -> dict:
    candidate = next(
        c
        for c in plan["candidates"]["new_central"]
        if c["object_type"] == "wlan" and c["identifier"] == name
    )
    return candidate["payload"]["security"]


def test_wlan_security_intent_uses_aaa_profile_chain_for_enterprise_dot1x():
    """Regression: existing Corp fixture pairs opmode text with a resolved
    dot1x aaa_profile chain; the source signal must classify enterprise/dot1x
    rather than guessing from the raw opmode string alone."""
    plan = build_migration_plan(_EXPORT)
    security = _wlan_security(plan, "Corp")
    assert security["mode"] == "enterprise_dot1x"
    assert security["ambiguous"] is False
    assert security["dot1x_auth_profile"] == "corp-dot1x"
    assert security["mac_auth_profile"] is None
    assert security["opmode"] == "wpa2-aes"


def test_wlan_security_intent_classifies_open():
    export = _wlan_export({"profile-name": "Open", "essid": "Open", "opmode": "opensystem"})
    plan = build_migration_plan(export)
    security = _wlan_security(plan, "Open")
    assert security["mode"] == "open"
    assert security["ambiguous"] is False


def test_wlan_security_intent_classifies_wpa2_personal():
    export = _wlan_export(
        {
            "profile-name": "Personal",
            "essid": "Personal",
            "opmode": "wpa2-psk-aes",
            "wpa_passphrase": "correct-horse-battery-staple",
        }
    )
    plan = build_migration_plan(export)
    security = _wlan_security(plan, "Personal")
    assert security["mode"] == "wpa2_personal"
    assert security["ambiguous"] is False
    assert security["passphrase_present"] is True
    # The passphrase value itself must never appear anywhere in the plan.
    assert "correct-horse-battery-staple" not in json.dumps(plan)


def test_wlan_security_intent_classifies_wpa3_sae():
    export = _wlan_export(
        {
            "profile-name": "SAE",
            "essid": "SAE",
            "opmode": "wpa3-aes-ccm-128-psk",
            "wpa_passphrase": "another-sae-passphrase",
        }
    )
    plan = build_migration_plan(export)
    security = _wlan_security(plan, "SAE")
    assert security["mode"] == "wpa3_sae"
    assert security["ambiguous"] is False


def test_wlan_security_intent_classifies_wpa3_transition_from_flag():
    export = _wlan_export(
        {
            "profile-name": "Transition",
            "essid": "Transition",
            "opmode": "wpa2-psk-aes",
            "wpa3_transition": True,
            "wpa_passphrase": "transition-mode-passphrase",
        }
    )
    plan = build_migration_plan(export)
    security = _wlan_security(plan, "Transition")
    assert security["mode"] == "wpa3_transition_personal"
    assert security["ambiguous"] is False
    assert security["wpa3_transition"] is True


def test_wlan_security_intent_classifies_enhanced_open():
    export = _wlan_export(
        {"profile-name": "OWE", "essid": "OWE", "opmode": "enhanced-open"}
    )
    plan = build_migration_plan(export)
    security = _wlan_security(plan, "OWE")
    assert security["mode"] == "enhanced_open"
    assert security["ambiguous"] is False


def test_wlan_security_intent_classifies_mac_auth_only():
    export = {
        "config_path": "/md",
        "wlans": {
            "ssid_profiles": [
                {"profile-name": "MacOnly", "essid": "MacOnly", "opmode": "opensystem"}
            ],
            "virtual_aps": [
                {
                    "profile-name": "MacOnly-VAP",
                    "ssid-profile": "MacOnly",
                    "aaa-profile": "mac-aaa",
                    "vlan": 40,
                }
            ],
        },
        "aaa": {
            "aaa_profiles": [
                {"profile-name": "mac-aaa", "mac_auth_profile": "corp-mac"},
            ],
        },
    }
    plan = build_migration_plan(export)
    security = _wlan_security(plan, "MacOnly")
    assert security["mode"] == "mac_auth_only"
    assert security["ambiguous"] is False
    assert security["mac_auth_profile"] == "corp-mac"


def test_wlan_security_intent_classifies_mac_auth_psk():
    export = {
        "config_path": "/md",
        "wlans": {
            "ssid_profiles": [
                {
                    "profile-name": "MacPsk",
                    "essid": "MacPsk",
                    "opmode": "wpa2-psk-aes",
                    "wpa_passphrase": "mac-psk-passphrase",
                }
            ],
            "virtual_aps": [
                {
                    "profile-name": "MacPsk-VAP",
                    "ssid-profile": "MacPsk",
                    "aaa-profile": "mac-psk-aaa",
                    "vlan": 41,
                }
            ],
        },
        "aaa": {
            "aaa_profiles": [
                {"profile-name": "mac-psk-aaa", "mac_auth_profile": "corp-mac"},
            ],
        },
    }
    plan = build_migration_plan(export)
    security = _wlan_security(plan, "MacPsk")
    assert security["mode"] == "mac_auth_psk"
    assert security["ambiguous"] is False


def test_wlan_security_intent_role_only_aaa_profile_falls_through_to_opmode_classification():
    """Regression for the companion aos8-migration-tool's role-only AAA +
    WPA2-PSK fixture (secondary, same-owner prior art, not an authoritative
    API contract):
    https://github.com/secure-ssid/aos8-migration-tool/blob/7bfa884d8e8f1c7e97a7bfa42f15596aa42fcf79/tests/test_aos8_parser.py#L15-L34

    A *resolved* aaa_profile that configures neither a dot1x nor a MAC-auth
    profile (e.g. `initial-role` only, used purely for post-auth role
    assignment) must not block opmode/passphrase classification -- unlike an
    aaa_profile reference that cannot be resolved in the export at all, which
    must still stay unknown.
    """
    export = {
        "config_path": "/md",
        "wlans": {
            "ssid_profiles": [
                {
                    "profile-name": "GuestPsk",
                    "essid": "GuestPsk",
                    "opmode": "wpa2-psk-aes",
                    "wpa_passphrase": "guest-psk-passphrase",
                },
                {
                    "profile-name": "Dangling",
                    "essid": "Dangling",
                    "opmode": "wpa2-psk-aes",
                    "wpa_passphrase": "dangling-passphrase",
                },
            ],
            "virtual_aps": [
                {
                    "profile-name": "GuestPsk-VAP",
                    "ssid-profile": "GuestPsk",
                    "aaa-profile": "guest-aaa",
                    "vlan": 60,
                },
                {
                    "profile-name": "Dangling-VAP",
                    "ssid-profile": "Dangling",
                    "aaa-profile": "missing-aaa",
                    "vlan": 61,
                },
            ],
        },
        "aaa": {
            "aaa_profiles": [
                {"profile-name": "guest-aaa", "initial-role": "guest-logon"},
            ],
        },
    }
    plan = build_migration_plan(export)

    guest_security = _wlan_security(plan, "GuestPsk")
    assert guest_security["mode"] == "wpa2_personal"
    assert guest_security["ambiguous"] is False
    assert guest_security["dot1x_auth_profile"] is None
    assert guest_security["mac_auth_profile"] is None
    assert guest_security["aaa_profile"] == "guest-aaa"
    assert any(
        "guest-aaa" in evidence and "role-only" in evidence
        for evidence in guest_security["evidence"]
    )
    # No security-intent-specific warning (aaa_profile resolution issue or
    # "cannot be verified"/"does not match a verified" fallback text) should
    # be raised for a cleanly classified role-only-AAA + verified-WPA2-PSK
    # WLAN -- only the pre-existing, unrelated opmode/passphrase
    # unsupported-field warnings.
    assert not any(
        "aaa_profile" in warning or "does not match a verified" in warning
        for warning in plan["warnings"]
        if warning.startswith("wlan:GuestPsk")
    )

    # An unresolved aaa_profile reference is a genuinely different case and
    # must still be reported unknown rather than falling through to opmode.
    dangling_security = _wlan_security(plan, "Dangling")
    assert dangling_security["mode"] == "unknown"
    assert dangling_security["ambiguous"] is True
    assert any(
        "aaa_profile 'missing-aaa' was not present" in warning
        for warning in plan["warnings"]
    )


def test_wlan_security_intent_stays_unknown_for_dot1x_server_group_without_auth_profile():
    """Regression: an aaa_profile that configures a dot1x_server_group but no
    explicit dot1x_auth_profile still carries authentication intent -- it
    must NOT fall through to opmode classification, even for an `opensystem`
    WLAN that would otherwise look like unambiguous open/no-auth."""
    export = {
        "config_path": "/md",
        "wlans": {
            "ssid_profiles": [
                {"profile-name": "SgOnly", "essid": "SgOnly", "opmode": "opensystem"}
            ],
            "virtual_aps": [
                {
                    "profile-name": "SgOnly-VAP",
                    "ssid-profile": "SgOnly",
                    "aaa-profile": "sg-only-aaa",
                    "vlan": 80,
                }
            ],
        },
        "aaa": {
            "aaa_profiles": [
                {"profile-name": "sg-only-aaa", "dot1x_server_group": "corp-sg"},
            ],
        },
    }
    plan = build_migration_plan(export)
    security = _wlan_security(plan, "SgOnly")
    assert security["mode"] == "unknown"
    assert security["ambiguous"] is True
    assert security["dot1x_auth_profile"] is None
    assert security["mac_auth_profile"] is None
    assert any(
        "sg-only-aaa" in warning
        and "dot1x_server_group" in warning
        and "without an explicit dot1x_auth_profile/mac_auth_profile mapping" in warning
        for warning in plan["warnings"]
    )


def test_wlan_security_intent_stays_unknown_for_mac_server_group_without_auth_profile():
    """Same as the dot1x_server_group case, but for a mac_server_group
    reference with no explicit mac_auth_profile mapping."""
    export = {
        "config_path": "/md",
        "wlans": {
            "ssid_profiles": [
                {"profile-name": "MacSgOnly", "essid": "MacSgOnly", "opmode": "opensystem"}
            ],
            "virtual_aps": [
                {
                    "profile-name": "MacSgOnly-VAP",
                    "ssid-profile": "MacSgOnly",
                    "aaa-profile": "mac-sg-only-aaa",
                    "vlan": 81,
                }
            ],
        },
        "aaa": {
            "aaa_profiles": [
                {"profile-name": "mac-sg-only-aaa", "mac_server_group": "guest-sg"},
            ],
        },
    }
    plan = build_migration_plan(export)
    security = _wlan_security(plan, "MacSgOnly")
    assert security["mode"] == "unknown"
    assert security["ambiguous"] is True
    assert any(
        "mac-sg-only-aaa" in warning
        and "mac_server_group" in warning
        and "without an explicit dot1x_auth_profile/mac_auth_profile mapping" in warning
        for warning in plan["warnings"]
    )


def test_wlan_security_intent_stays_unknown_for_accounting_server_group_without_auth_profile():
    """An accounting-only server-group reference on the aaa_profile still
    indicates external server-group involvement without a verified
    auth-profile mapping, so it must fail closed to unknown rather than
    falling through to opmode classification."""
    export = {
        "config_path": "/md",
        "wlans": {
            "ssid_profiles": [
                {
                    "profile-name": "AcctSgOnly",
                    "essid": "AcctSgOnly",
                    "opmode": "opensystem",
                }
            ],
            "virtual_aps": [
                {
                    "profile-name": "AcctSgOnly-VAP",
                    "ssid-profile": "AcctSgOnly",
                    "aaa-profile": "acct-sg-only-aaa",
                    "vlan": 82,
                }
            ],
        },
        "aaa": {
            "aaa_profiles": [
                {
                    "profile-name": "acct-sg-only-aaa",
                    "rad_acct_sg": "acct-sg",
                },
            ],
        },
    }
    plan = build_migration_plan(export)
    security = _wlan_security(plan, "AcctSgOnly")
    assert security["mode"] == "unknown"
    assert security["ambiguous"] is True
    assert any(
        "acct-sg-only-aaa" in warning and "accounting_server_group" in warning
        for warning in plan["warnings"]
    )


def test_wlan_security_intent_stays_unknown_when_aaa_profile_has_both_dot1x_and_mac_auth():
    """An aaa_profile configuring both a dot1x and a MAC-auth profile is an
    unverified combination; it must stay unknown and must NOT fall through to
    opmode-based PSK classification even though the WLAN itself carries a
    passphrase."""
    export = {
        "config_path": "/md",
        "wlans": {
            "ssid_profiles": [
                {
                    "profile-name": "Mixed",
                    "essid": "Mixed",
                    "opmode": "wpa2-psk-aes",
                    "wpa_passphrase": "mixed-passphrase",
                }
            ],
            "virtual_aps": [
                {
                    "profile-name": "Mixed-VAP",
                    "ssid-profile": "Mixed",
                    "aaa-profile": "mixed-aaa",
                    "vlan": 70,
                }
            ],
        },
        "aaa": {
            "aaa_profiles": [
                {
                    "profile-name": "mixed-aaa",
                    "dot1x_auth_profile": "corp-dot1x",
                    "mac_auth_profile": "corp-mac",
                },
            ],
        },
    }
    plan = build_migration_plan(export)
    security = _wlan_security(plan, "Mixed")
    assert security["mode"] == "unknown"
    assert security["ambiguous"] is True
    assert any(
        "configures both a dot1x and a MAC-auth profile" in warning
        for warning in plan["warnings"]
    )


def test_wlan_security_intent_reports_unknown_when_aaa_profile_unresolved():
    export = {
        "config_path": "/md",
        "wlans": {
            "ssid_profiles": [
                {"profile-name": "Dangling", "essid": "Dangling", "opmode": "opensystem"}
            ],
            "virtual_aps": [
                {
                    "profile-name": "Dangling-VAP",
                    "ssid-profile": "Dangling",
                    "aaa-profile": "missing-aaa",
                    "vlan": 50,
                }
            ],
        },
    }
    plan = build_migration_plan(export)
    security = _wlan_security(plan, "Dangling")
    assert security["mode"] == "unknown"
    assert security["ambiguous"] is True
    assert any(
        "aaa_profile 'missing-aaa' was not present" in warning
        for warning in plan["warnings"]
    )


def test_wlan_security_intent_reports_unknown_for_ambiguous_opmode_without_fabricating():
    export = _wlan_export(
        {"profile-name": "Legacy", "essid": "Legacy", "opmode": "static-wep"}
    )
    plan = build_migration_plan(export)
    security = _wlan_security(plan, "Legacy")
    assert security["mode"] == "unknown"
    assert security["ambiguous"] is True
    assert any(
        "does not match a verified AOS8 security pattern" in warning
        for warning in plan["warnings"]
    )


def test_wlan_security_intent_does_not_classify_legacy_wpa_tkip_psk_as_wpa2():
    """Regression: a legacy WPA1/TKIP opmode carrying a PSK token must not be
    misclassified as verified WPA2-personal -- the opmode itself never says
    "wpa2"."""
    export = _wlan_export(
        {
            "profile-name": "LegacyTkip",
            "essid": "LegacyTkip",
            "opmode": "wpa-psk-tkip",
            "wpa_passphrase": "legacy-tkip-passphrase",
        }
    )
    plan = build_migration_plan(export)
    security = _wlan_security(plan, "LegacyTkip")
    assert security["mode"] == "unknown"
    assert security["ambiguous"] is True
    assert security["passphrase_present"] is True
    assert any(
        "does not match a verified AOS8 security pattern" in warning
        for warning in plan["warnings"]
    )


def test_wlan_security_intent_does_not_classify_wpa_tkip_with_passphrase_present_as_wpa2():
    """Regression: `passphrase_present=True` alone (without an explicit
    "wpa2" opmode token) must not be enough evidence for wpa2_personal."""
    export = _wlan_export(
        {
            "profile-name": "LegacyTkipNoPsk",
            "essid": "LegacyTkipNoPsk",
            "opmode": "wpa-tkip",
            "wpa_passphrase": "another-legacy-passphrase",
        }
    )
    plan = build_migration_plan(export)
    security = _wlan_security(plan, "LegacyTkipNoPsk")
    assert security["mode"] == "unknown"
    assert security["ambiguous"] is True
    assert security["passphrase_present"] is True


def test_wlan_security_intent_does_not_classify_unrecognized_psk_opmode_as_wpa2():
    """Regression: an opmode token containing "psk" but not "wpa2" (and no
    aaa_profile/wpa3/enhanced-open match) must remain unknown."""
    export = _wlan_export(
        {
            "profile-name": "MysteryPsk",
            "essid": "MysteryPsk",
            "opmode": "some-vendor-psk-mode",
            "wpa_hexkey": "abcdef0123456789",
        }
    )
    plan = build_migration_plan(export)
    security = _wlan_security(plan, "MysteryPsk")
    assert security["mode"] == "unknown"
    assert security["ambiguous"] is True
    assert security["psk_hexkey_present"] is True


def test_server_group_dependency_resolution_is_type_aware_across_radius_and_ldap():
    export = {
        "config_path": "/md",
        "aaa": {
            "server_groups": [
                {"sg_name": "mixed-sg", "auth_server": [{"name": "shared-name"}]}
            ],
            "radius_servers": [
                {"rad_server_name": "shared-name", "rad_host": "10.0.0.1"}
            ],
        },
    }
    plan = build_migration_plan(export)
    server_group = next(
        c
        for c in plan["candidates"]["new_central"]
        if c["object_type"] == "server_group"
    )
    assert server_group["dependencies"] == ["auth_server:radius:shared-name"]


def test_server_group_dependency_collision_fails_closed_with_warning():
    """Regression for docs/aos8-migration-contract-matrix.md §3 item 4."""
    export = {
        "config_path": "/md",
        "aaa": {
            "server_groups": [
                {"sg_name": "collide-sg", "auth_server": [{"name": "shared-name"}]}
            ],
            "radius_servers": [
                {"rad_server_name": "shared-name", "rad_host": "10.0.0.1"}
            ],
            "ldap_servers": [
                {"ldap_server_name": "shared-name", "ldap_host": "10.0.0.2"}
            ],
        },
    }
    plan = build_migration_plan(export)
    server_group = next(
        c
        for c in plan["candidates"]["new_central"]
        if c["object_type"] == "server_group"
    )
    # Fail-closed: neither the RADIUS nor the LDAP candidate is guessed.
    assert server_group["dependencies"] == []
    assert any(
        "matches multiple server types" in warning
        and "ldap" in warning
        and "radius" in warning
        for warning in server_group["warnings"]
    )
    assert server_group["unsupported_fields"]["auth_server_type_collisions"] == {
        "shared-name": ["ldap", "radius"]
    }


def test_server_group_dependencies_remain_deterministic_across_runs():
    export = {
        "config_path": "/md",
        "aaa": {
            "server_groups": [
                {
                    "sg_name": "multi-sg",
                    "auth_server": [{"name": "rad-a"}, {"name": "tac-a"}],
                }
            ],
            "radius_servers": [{"rad_server_name": "rad-a", "rad_host": "10.0.0.1"}],
            "tacacs_servers": [{"tacacs_server_name": "tac-a", "tacacs_host": "10.0.0.2"}],
        },
    }
    first = build_migration_plan(export)
    second = build_migration_plan(export)
    assert first == second
    server_group = next(
        c
        for c in first["candidates"]["new_central"]
        if c["object_type"] == "server_group"
    )
    assert server_group["dependencies"] == [
        "auth_server:radius:rad-a",
        "auth_server:tacacs:tac-a",
    ]


def test_ap_group_warns_explicitly_on_unresolved_vap_to_wlan_dependency():
    """Regression for docs/aos8-migration-contract-matrix.md §3 item 5."""
    export = {
        "config_path": "/md",
        "ap_groups": [
            {"profile-name": "Lab-Group", "virtual-ap": ["Missing-VAP"]}
        ],
    }
    plan = build_migration_plan(export)
    ap_group = next(
        c for c in plan["candidates"]["new_central"] if c["object_type"] == "ap_group"
    )
    assert any(
        "virtual AP 'Missing-VAP' does not match any parsed WLAN profile" in warning
        for warning in ap_group["warnings"]
    )
    assert ap_group["dependencies"] == ["wlan:Missing-VAP"]


def test_role_warns_explicitly_on_missing_policy_dependency():
    """Regression for docs/aos8-migration-contract-matrix.md §3 item 5."""
    export = {
        "config_path": "/md",
        "roles": [{"role": "orphan", "acl": "missing-policy", "vlan": 10}],
    }
    plan = build_migration_plan(export)
    role = next(c for c in plan["candidates"]["new_central"] if c["object_type"] == "role")
    assert any(
        "referenced policy 'missing-policy' was not present" in warning
        for warning in role["warnings"]
    )


# ---------------------------------------------------------------------------
# Network destination aliases, Ethernet ACLs, and IP-classification
# whitelist rules -- new, reference-only source families (no deterministic
# Classic/New Central adapter mapping exists for any of these in this repo).
# ---------------------------------------------------------------------------

_NEW_FAMILIES_EXPORT = {
    "config_path": "/md/lab",
    "netdst": [
        {
            "dstname": "corp-servers",
            "netdst__desc": "Corp servers",
            "netdst__network": "10.20.0.0/16",
        },
        {"dstname": "voip-dest", "netdst__host": "10.30.0.5", "netdst__invert": True},
    ],
    "acl_eth": [
        {
            "accname": "eth-200",
            "acl_eth__policy": [
                {"source": "any", "destination": "any", "ethertype": "0x0800", "action": "permit"}
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
}


_AUTH_PROFILE_FAMILIES_EXPORT = {
    "config_path": "/md/lab",
    "aaa": {
        "wired_auth_profiles": [
            {"wired_aaa_profile": "corp-aaa", "wired_blacklist_time": 3600}
        ],
        "stateful_dot1x_auth_profiles": [
            {
                "stateful_dot1x_server_group": "corp-sg",
                "statefuldot1x_default_role": "guest",
                "timeout": 300,
            }
        ],
        "wispr_auth_profiles": [
            {
                "profile-name": "wispr1",
                "wispr_default_role": "guest",
                "wispr_server_group": "corp-sg",
                "wispr_max_delay": 5,
            }
        ],
        "cp_auth_profiles": [
            {
                "profile-name": "cp1",
                "cp_default_role": "guest",
                "cp_default_guest_role": "guest2",
                "cp_server_group": "corp-sg",
                "cp_redirect_url": "http://example.com",
            }
        ],
        "krb_auth_profiles": [
            {
                "profile-name": "krb1",
                "krb_default_role": "guest",
                "krb_server_group": "corp-sg",
                "krb_timeout": 30,
            }
        ],
        "ntlm_auth_profiles": [
            {
                "profile-name": "ntlm1",
                "ntlm_default_role": "guest",
                "ntlm_server_group": "corp-sg",
                "ntlm_enable": True,
                "ntlm_timeout": 60,
            }
        ],
    },
}


def _candidate(plan, target_type, object_type, identifier):
    return next(
        c
        for c in plan["candidates"][target_type]
        if c["object_type"] == object_type and c["identifier"] == identifier
    )


def test_network_destinations_are_emitted_for_both_targets_with_reference_only_warning():
    plan = build_migration_plan(_NEW_FAMILIES_EXPORT)
    for target_type in ("classic_central", "new_central"):
        candidate = _candidate(
            plan, target_type, "network_destination", "ipv4:corp-servers"
        )
        assert candidate["payload"] == {
            "address_family": "ipv4",
            "name": "corp-servers",
            "description": "Corp servers",
            "host": None,
            "network": "10.20.0.0/16",
            "range": None,
            "invert": None,
        }
        assert any(
            "no deterministic Classic/New Central adapter mapping" in warning
            for warning in candidate["warnings"]
        )


def test_network_destination_invert_gets_explicit_lossy_warning():
    plan = build_migration_plan(_NEW_FAMILIES_EXPORT)
    candidate = _candidate(plan, "new_central", "network_destination", "ipv4:voip-dest")
    assert candidate["payload"]["invert"] is True
    assert any(
        "match-polarity negation" in warning for warning in candidate["warnings"]
    )


def test_ethernet_acl_candidate_flattens_rules_and_flags_unsupported_fields():
    plan = build_migration_plan(_NEW_FAMILIES_EXPORT)
    candidate = _candidate(plan, "new_central", "ethernet_acl", "eth-200")
    assert candidate["payload"]["rule_count"] == 1
    assert candidate["payload"]["rules"] == [
        {
            "source": "any",
            "destination": "any",
            "ethertype": "0x0800",
            "vlan": None,
            "action": "permit",
            "log": None,
        }
    ]
    assert any(
        "no deterministic Classic/New Central adapter mapping" in warning
        for warning in candidate["warnings"]
    )


def test_whitelist_rule_candidate_uses_ip_range_identifier():
    plan = build_migration_plan(_NEW_FAMILIES_EXPORT)
    candidate = _candidate(
        plan, "new_central", "whitelist_rule", "10.0.0.1-10.0.0.50"
    )
    assert candidate["payload"] == {"start_ip": "10.0.0.1", "end_ip": "10.0.0.50"}
    assert any(
        "no deterministic Classic/New Central adapter mapping" in warning
        for warning in candidate["warnings"]
    )


def test_policy_destination_resolves_network_destination_dependency():
    """Priority feature: a policy rule's `destination` that matches a parsed
    `netdst` alias name becomes an explicit, type-aware
    `network_destination:{family}:{name}` dependency -- reusing the existing
    `_dependency`/`_dependencies` helpers rather than a new mechanism."""
    plan = build_migration_plan(_NEW_FAMILIES_EXPORT)
    policy = _candidate(plan, "new_central", "policy", "corp-acl")
    assert policy["dependencies"] == ["network_destination:ipv4:corp-servers"]


def test_policy_destination_without_matching_alias_has_no_dependency():
    export = {
        "config_path": "/md",
        "policies": [
            {
                "accname": "no-alias",
                "acl_sess__v4policy": [
                    {"source": "any", "destination": "any", "action": "permit"}
                ],
            }
        ],
    }
    plan = build_migration_plan(export)
    policy = _candidate(plan, "new_central", "policy", "no-alias")
    assert policy["dependencies"] == []


def test_new_families_appear_in_source_object_counts_and_apply_order():
    plan = build_migration_plan(_NEW_FAMILIES_EXPORT)
    assert plan["source_object_counts"]["network_destinations"] == 2
    assert plan["source_object_counts"]["ethernet_acls"] == 1
    assert plan["source_object_counts"]["whitelist_rules"] == 1

    net_dest = _candidate(plan, "new_central", "network_destination", "ipv4:corp-servers")
    policy = _candidate(plan, "new_central", "policy", "corp-acl")
    # network_destination must sort before the policy that depends on it.
    assert net_dest["apply_order"] < policy["apply_order"]


def test_new_families_never_carry_secrets_and_remain_deterministic():
    first = build_migration_plan(_NEW_FAMILIES_EXPORT)
    second = build_migration_plan(_NEW_FAMILIES_EXPORT)
    assert first == second
    serialized = json.dumps(first)
    assert "10.0.0.1" in serialized  # non-secret data still visible


# ---------------------------------------------------------------------------
# Wired / captive-portal / WISPr / Kerberos / NTLM / stateful-802.1X
# authentication-profile families (reference-only; see
# `hpe_networking_mcp.pipeline.aos8_schema.REFERENCE_ONLY_OBJECT_TYPES`).
# ---------------------------------------------------------------------------

_NEW_AUTH_PROFILE_FAMILY_OBJECT_TYPES = (
    "wired_auth_profile",
    "stateful_dot1x_auth_profile",
    "wispr_auth_profile",
    "cp_auth_profile",
    "krb_auth_profile",
    "ntlm_auth_profile",
)


def test_new_auth_profile_families_are_reference_only():
    from hpe_networking_mcp.pipeline.aos8_schema import REFERENCE_ONLY_OBJECT_TYPES

    assert REFERENCE_ONLY_OBJECT_TYPES >= set(_NEW_AUTH_PROFILE_FAMILY_OBJECT_TYPES)


def test_new_auth_profile_families_emit_candidates_for_both_targets_with_reference_only_warning():
    plan = build_migration_plan(_AUTH_PROFILE_FAMILIES_EXPORT)
    identifiers = {
        "wired_auth_profile": "global",
        "stateful_dot1x_auth_profile": "global",
        "wispr_auth_profile": "wispr1",
        "cp_auth_profile": "cp1",
        "krb_auth_profile": "krb1",
        "ntlm_auth_profile": "ntlm1",
    }
    for object_type in _NEW_AUTH_PROFILE_FAMILY_OBJECT_TYPES:
        for target_type in ("classic_central", "new_central"):
            candidate = _candidate(
                plan, target_type, object_type, identifiers[object_type]
            )
            assert any(
                "no deterministic Classic/New Central adapter mapping" in warning
                for warning in candidate["warnings"]
            )
            # Serialization: every candidate is a plain, JSON-safe dict (no
            # dataclass/tuple/set leaking through `to_dict()`).
            json.dumps(candidate)


def test_wired_auth_profile_candidate_carries_aaa_profile_dependency():
    plan = build_migration_plan(_AUTH_PROFILE_FAMILIES_EXPORT)
    candidate = _candidate(plan, "new_central", "wired_auth_profile", "global")
    assert candidate["payload"] == {
        "aaa_profile": "corp-aaa",
        "blacklist_time": 3600,
    }
    assert candidate["dependencies"] == ["aaa_profile:corp-aaa"]
    assert candidate["apply_order"] == 45


def test_stateful_dot1x_auth_profile_candidate_carries_role_and_server_group_dependencies():
    plan = build_migration_plan(_AUTH_PROFILE_FAMILIES_EXPORT)
    candidate = _candidate(plan, "new_central", "stateful_dot1x_auth_profile", "global")
    assert candidate["payload"] == {
        "mode": None,
        "server_group": "corp-sg",
        "default_role": "guest",
        "timeout": 300,
    }
    assert candidate["dependencies"] == ["role:guest", "server_group:corp-sg"]
    assert candidate["apply_order"] == 35


def test_wispr_auth_profile_candidate_flags_unsupported_field_and_dependencies():
    plan = build_migration_plan(_AUTH_PROFILE_FAMILIES_EXPORT)
    candidate = _candidate(plan, "new_central", "wispr_auth_profile", "wispr1")
    assert candidate["payload"] == {
        "name": "wispr1",
        "default_role": "guest",
        "server_group": "corp-sg",
    }
    assert candidate["dependencies"] == ["role:guest", "server_group:corp-sg"]
    assert candidate["unsupported_fields"] == {"wispr_max_delay": 5}
    assert any(
        "wispr_max_delay" in warning and "not mapped" in warning
        for warning in candidate["warnings"]
    )


def test_cp_auth_profile_candidate_carries_two_role_dependencies():
    plan = build_migration_plan(_AUTH_PROFILE_FAMILIES_EXPORT)
    candidate = _candidate(plan, "new_central", "cp_auth_profile", "cp1")
    assert candidate["payload"] == {
        "name": "cp1",
        "default_role": "guest",
        "default_guest_role": "guest2",
        "server_group": "corp-sg",
    }
    assert candidate["dependencies"] == [
        "role:guest",
        "role:guest2",
        "server_group:corp-sg",
    ]


def test_krb_auth_profile_candidate_payload_and_dependencies():
    plan = build_migration_plan(_AUTH_PROFILE_FAMILIES_EXPORT)
    candidate = _candidate(plan, "new_central", "krb_auth_profile", "krb1")
    assert candidate["payload"] == {
        "name": "krb1",
        "default_role": "guest",
        "server_group": "corp-sg",
        "timeout": 30,
    }
    assert candidate["dependencies"] == ["role:guest", "server_group:corp-sg"]


def test_ntlm_auth_profile_candidate_payload_and_dependencies():
    plan = build_migration_plan(_AUTH_PROFILE_FAMILIES_EXPORT)
    candidate = _candidate(plan, "new_central", "ntlm_auth_profile", "ntlm1")
    assert candidate["payload"] == {
        "name": "ntlm1",
        "default_role": "guest",
        "server_group": "corp-sg",
        "enabled": True,
        "timeout": 60,
    }
    assert candidate["dependencies"] == ["role:guest", "server_group:corp-sg"]


def test_wired_auth_profile_second_singleton_instance_gets_distinct_identifier():
    export = {
        "aaa": {
            "wired_auth_profiles": [
                {"wired_aaa_profile": "corp-aaa"},
                {"wired_aaa_profile": "guest-aaa"},
            ]
        }
    }
    plan = build_migration_plan(export)
    first = _candidate(plan, "new_central", "wired_auth_profile", "global")
    second = _candidate(plan, "new_central", "wired_auth_profile", "global-1")
    assert first["payload"]["aaa_profile"] == "corp-aaa"
    assert second["payload"]["aaa_profile"] == "guest-aaa"


def test_new_auth_profile_family_dependencies_report_missing_role_and_server_group():
    """These families' role/server_group dependencies are tracked even
    when the referenced object is absent from the export -- the generic
    end-of-plan dependency check must still fire (fail-closed, never
    invented)."""
    plan = build_migration_plan(_AUTH_PROFILE_FAMILIES_EXPORT)
    candidate = _candidate(plan, "new_central", "krb_auth_profile", "krb1")
    assert any(
        "dependency 'role:guest' is not present in this export" in warning
        for warning in candidate["warnings"]
    )
    assert any(
        "dependency 'server_group:corp-sg' is not present in this export" in warning
        for warning in candidate["warnings"]
    )
