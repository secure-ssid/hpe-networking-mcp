"""Pure-python parsers for `aos8_export_all()` migration exports.

The parsers preserve source-native settings that are not normalized, and the
reporting entry point returns warnings for malformed sections/items instead of
raising or silently substituting defaults.
"""

from __future__ import annotations

from typing import Any

from hpe_networking_mcp.pipeline.aos8_schema import (
    AOS8VRRP,
    AOS8AAAProfile,
    AOS8ApGroup,
    AOS8AuthProfile,
    AOS8AuthServer,
    AOS8CaptivePortalAuthProfile,
    AOS8Controller,
    AOS8EthernetACL,
    AOS8EthernetACLRule,
    AOS8KerberosAuthProfile,
    AOS8NetworkDestination,
    AOS8NTLMAuthProfile,
    AOS8Policy,
    AOS8PolicyRule,
    AOS8Role,
    AOS8Route,
    AOS8ServerGroup,
    AOS8StatefulDot1xAuthProfile,
    AOS8Vlan,
    AOS8WhitelistRule,
    AOS8WiredAuthProfile,
    AOS8WisprAuthProfile,
    AOS8Wlan,
)


def _warn(warnings: list[str] | None, message: str) -> None:
    if warnings is not None:
        warnings.append(message)


def _dict_items(
    value: Any,
    path: str,
    warnings: list[str] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        _warn(warnings, f"export: {path} section is missing or malformed.")
        return []
    out: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if isinstance(item, dict):
            out.append(item)
        else:
            _warn(warnings, f"export: {path}[{index}] is not an object and was not parsed.")
    return out


def _optional_dict_items(
    export: dict[str, Any],
    key: str,
    warnings: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Like `_dict_items`, but tolerant of a section that is simply absent.

    `netdst`/`netdst6`/`acl_eth`/`whitelist_rule` are not yet fetched by
    `hpe_networking_mcp.mcp_servers.aos8.aos8_export_all()` (see
    `docs/aos8-migration-contract-matrix.md`), so an export missing these
    keys entirely is the expected, common case today -- not a malformed
    export. A key that *is* present but the wrong shape is still reported via
    `_dict_items`'s existing "section is missing or malformed" warning,
    matching every other AOS8 object family.
    """
    if key not in export:
        return []
    return _dict_items(export.get(key), key, warnings)


def _optional_aaa_items(
    export: dict[str, Any],
    key: str,
    warnings: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Like `_optional_dict_items`, but for a section nested one level under
    `export["aaa"]`: the wired/WISPr/captive-portal/Kerberos/NTLM/stateful
    802.1X authentication-profile families
    (`aos8_export_all()` fetches them alongside the existing
    `aaa.dot1x_auth_profiles`/`aaa.mac_auth_profiles`). An export whose
    `aaa` section entirely omits this key is the expected, common case for
    any export predating these families (same "not yet fetched" tolerance
    as `_optional_dict_items`), not a malformed export.
    """
    aaa = export.get("aaa")
    if not isinstance(aaa, dict) or key not in aaa:
        return []
    return _dict_items(aaa.get(key), f"aaa.{key}", warnings)


def _first(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def _remaining(item: dict[str, Any], consumed: set[str]) -> dict[str, Any]:
    return {key: item[key] for key in sorted(item) if key not in consumed}


def _identifier(
    item: dict[str, Any],
    keys: tuple[str, ...],
    path: str,
    index: int,
    warnings: list[str] | None,
) -> str:
    value = _first(item, keys)
    if value is not None:
        return str(value)
    generated = f"unknown-{index}"
    _warn(
        warnings,
        f"export: {path}[{index}] has no supported identifier; using {generated!r}.",
    )
    return generated


_TRUE_FLAG_TOKENS = {"true", "1", "yes", "enable", "enabled"}
_FALSE_FLAG_TOKENS = {"false", "0", "no", "disable", "disabled"}
_MAX_FLAG_UNWRAP_DEPTH = 4


def _normalize_optional_bool(value: Any, *, _depth: int = 0) -> bool | None:
    """Defensively coerce a loosely-typed AOS8 boolean/flag value to ``bool | None``.

    AOS8 config exports do not guarantee that boolean-ish fields (like
    ``wpa3_transition``) arrive as JSON booleans: some builds emit integer
    ``0``/``1``, an explicit true/false-ish string, or wrap the scalar in a
    single-key dict — the same "double-wrapped" ``{key: {key: val}}`` quirk
    documented for other AOS8 config fields (secondary, same-owner prior art,
    not an authoritative API contract):
    https://github.com/secure-ssid/aos8-migration-tool/blob/7bfa884d8e8f1c7e97a7bfa42f15596aa42fcf79/docs/API-NOTES.md#L57-L64

    Any other shape — a multi-key dict, a list, or an unrecognized string —
    is ambiguous and returns ``None`` rather than guessing. An *empty* dict is
    ambiguous too, not automatically ``True``/``False``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        # Only literal 0/1 are a documented AOS8 boolean encoding; any other
        # integer (e.g. an enum ordinal) is not a verified flag value.
        return bool(value) if value in (0, 1) else None
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_FLAG_TOKENS:
            return True
        if token in _FALSE_FLAG_TOKENS:
            return False
        return None
    if isinstance(value, dict):
        if len(value) != 1 or _depth >= _MAX_FLAG_UNWRAP_DEPTH:
            return None
        (only_value,) = value.values()
        return _normalize_optional_bool(only_value, _depth=_depth + 1)
    return None


def _wlan_security_signals(profile: dict[str, Any]) -> tuple[bool | None, bool, bool]:
    """Return (wpa3_transition, passphrase_present, psk_hexkey_present) from an ssid_prof.

    These are the only AOS8 ``ssid_prof`` fields with in-repo evidence for
    security-mode disambiguation beyond ``opmode``
    (``src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/aos8.json``
    `aos8_post_object_ssid_prof` request-body properties: ``wpa3_transition``,
    ``wpa_passphrase``, ``wpa_hexkey``). Only presence is recorded; the raw
    passphrase/hex-key values are never read here and remain redacted wherever
    the full ssid_prof is otherwise retained (`src/hpe_networking_mcp/pipeline/aos8_migration.py`
    `_wlan_payload`'s `unsupported_fields` pass).
    """
    wpa3_transition = _normalize_optional_bool(profile.get("wpa3_transition"))
    passphrase_present = bool(profile.get("wpa_passphrase"))
    psk_hexkey_present = bool(profile.get("wpa_hexkey"))
    return wpa3_transition, passphrase_present, psk_hexkey_present


def parse_wlans(
    export: dict[str, Any],
    warnings: list[str] | None = None,
) -> list[AOS8Wlan]:
    """Merge SSID profiles with their linked virtual AP (matched by name)."""
    wlans_section = export.get("wlans")
    if not isinstance(wlans_section, dict):
        _warn(warnings, "export: wlans section is missing or malformed.")
        return []
    ssid_profiles = _dict_items(
        wlans_section.get("ssid_profiles"), "wlans.ssid_profiles", warnings
    )
    virtual_aps = _dict_items(
        wlans_section.get("virtual_aps"), "wlans.virtual_aps", warnings
    )

    vap_by_ssid: dict[str, dict[str, Any]] = {}
    for index, vap in enumerate(virtual_aps):
        # AOS8 returns the SSID-profile reference under different keys
        # depending on firmware build — "ssid_prof", "ssid-profile", or
        # "ssid-prof" have all been observed (secondary, same-owner prior
        # art, not an authoritative API contract):
        # https://github.com/secure-ssid/aos8-migration-tool/blob/7bfa884d8e8f1c7e97a7bfa42f15596aa42fcf79/lib/aos8_client.py#L315-L399
        ssid_ref = _first(vap, ("ssid-profile", "ssid_prof", "ssid-prof"))
        if ssid_ref:
            vap_by_ssid[str(ssid_ref)] = vap
        else:
            _warn(
                warnings,
                f"export: wlans.virtual_aps[{index}] has no SSID-profile reference.",
            )

    out: list[AOS8Wlan] = []
    for index, profile in enumerate(ssid_profiles):
        name = _identifier(
            profile,
            ("profile-name", "name"),
            "wlans.ssid_profiles",
            index,
            warnings,
        )
        vap = vap_by_ssid.get(name, {})
        wpa3_transition, passphrase_present, psk_hexkey_present = _wlan_security_signals(profile)
        out.append(
            AOS8Wlan(
                profile_name=name,
                essid=_first(profile, ("essid", "ESSID")),
                opmode=_first(profile, ("opmode",)),
                vlan=_first(vap, ("vlan",)),
                forward_mode=_first(vap, ("forward-mode", "forward_mode")),
                aaa_profile=_first(vap, ("aaa-profile", "aaa_prof")),
                virtual_ap_profile=_first(vap, ("profile-name", "name")),
                wpa3_transition=wpa3_transition,
                passphrase_present=passphrase_present,
                psk_hexkey_present=psk_hexkey_present,
                raw={"ssid_profile": profile, "virtual_ap": vap},
            )
        )

    referenced = {wlan.virtual_ap_profile for wlan in out if wlan.virtual_ap_profile}
    for index, vap in enumerate(virtual_aps):
        vap_name = _identifier(
            vap,
            ("profile-name", "name"),
            "wlans.virtual_aps",
            index,
            warnings,
        )
        if vap_name in referenced:
            continue
        out.append(
            AOS8Wlan(
                profile_name=vap_name,
                vlan=_first(vap, ("vlan",)),
                forward_mode=_first(vap, ("forward-mode", "forward_mode")),
                aaa_profile=_first(vap, ("aaa-profile", "aaa_prof")),
                virtual_ap_profile=vap_name,
                raw={"ssid_profile": {}, "virtual_ap": vap},
            )
        )
    return out


def parse_roles(
    export: dict[str, Any],
    warnings: list[str] | None = None,
) -> list[AOS8Role]:
    items = _dict_items(export.get("roles"), "roles", warnings)
    return [
        AOS8Role(
            rolename=_identifier(
                item,
                ("rolename", "role", "name", "profile-name"),
                "roles",
                index,
                warnings,
            ),
            vlan=_first(item, ("vlan",)),
            acl=_first(item, ("acl", "access-list")),
            captive_portal_profile=_first(item, ("captive-portal-profile",)),
            raw=item,
        )
        for index, item in enumerate(items)
    ]


def parse_vlans(
    export: dict[str, Any],
    warnings: list[str] | None = None,
) -> list[AOS8Vlan]:
    items = _dict_items(export.get("vlans"), "vlans", warnings)
    out: list[AOS8Vlan] = []
    for index, item in enumerate(items):
        vlan_id = _first(item, ("id", "vlan-id", "vlan_id", "name"))
        if vlan_id is None:
            vlan_id = f"unknown-{index}"
            _warn(
                warnings,
                f"export: vlans[{index}] has no VLAN identifier; using {vlan_id!r}.",
            )
        out.append(
            AOS8Vlan(
                vlan_id=vlan_id,
                description=_first(item, ("description",)),
                raw=item,
            )
        )
    return out


def parse_ap_groups(
    export: dict[str, Any],
    warnings: list[str] | None = None,
) -> list[AOS8ApGroup]:
    items = _dict_items(export.get("ap_groups"), "ap_groups", warnings)
    out: list[AOS8ApGroup] = []
    for index, item in enumerate(items):
        vaps = item.get("virtual-ap", item.get("virtual_ap", []))
        if not isinstance(vaps, list):
            _warn(
                warnings,
                f"export: ap_groups[{index}].virtual-ap is malformed; no references parsed.",
            )
            vaps = []
        out.append(
            AOS8ApGroup(
                profile_name=_identifier(
                    item,
                    ("profile-name", "name"),
                    "ap_groups",
                    index,
                    warnings,
                ),
                virtual_ap_profiles=[str(value) for value in vaps],
                raw=item,
            )
        )
    return out


def parse_controllers(
    export: dict[str, Any],
    warnings: list[str] | None = None,
) -> list[AOS8Controller]:
    items = _dict_items(export.get("controllers"), "controllers", warnings)
    out: list[AOS8Controller] = []
    for index, item in enumerate(items):
        name = _first(item, ("Name", "name", "hostname"))
        ip_address = _first(item, ("IP Address", "ip_address"))
        if name is None and ip_address is None:
            _warn(
                warnings,
                f"export: controllers[{index}] has no name or IP address.",
            )
        out.append(
            AOS8Controller(
                name=name,
                ip_address=ip_address,
                model=_first(item, ("Model", "model")),
                version=_first(item, ("Version", "version")),
                raw=item,
            )
        )
    return out


def _parse_policy_rules(
    value: Any,
    address_family: str,
    path: str,
    warnings: list[str] | None,
) -> list[AOS8PolicyRule]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        _warn(warnings, f"export: {path} is malformed; expected a list of rules.")
        return [
            AOS8PolicyRule(
                address_family=address_family,  # type: ignore[arg-type]
                unsupported_fields={"raw_rule": value},
                raw=value,
            )
        ]
    rules: list[AOS8PolicyRule] = []
    aliases = {
        "source": ("source", "src", "source-address", "source_alias", "srcalias"),
        "destination": (
            "destination",
            "dst",
            "destination-address",
            "destination_alias",
            "dstalias",
        ),
        "service": ("service", "svc", "protocol", "application", "app"),
        "action": ("action", "permit", "deny"),
        "log": ("log", "logging"),
    }
    consumed = {key for values in aliases.values() for key in values}
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            _warn(warnings, f"export: {path}[{index}] is not an object.")
            rules.append(
                AOS8PolicyRule(
                    address_family=address_family,  # type: ignore[arg-type]
                    unsupported_fields={"raw_rule": item},
                    raw=item,
                )
            )
            continue
        rules.append(
            AOS8PolicyRule(
                address_family=address_family,  # type: ignore[arg-type]
                source=_first(item, aliases["source"]),
                destination=_first(item, aliases["destination"]),
                service=_first(item, aliases["service"]),
                action=_first(item, aliases["action"]),
                log=_first(item, aliases["log"]),
                unsupported_fields=_remaining(item, consumed),
                raw=item,
            )
        )
    return rules


def parse_policies(
    export: dict[str, Any],
    warnings: list[str] | None = None,
) -> list[AOS8Policy]:
    items = _dict_items(export.get("policies"), "policies", warnings)
    out: list[AOS8Policy] = []
    for index, item in enumerate(items):
        name = _identifier(
            item,
            ("accname", "name", "profile-name"),
            "policies",
            index,
            warnings,
        )
        legacy_rules = item.get("rule", item.get("rules"))
        ipv4_value = item.get("acl_sess__v4policy", legacy_rules)
        ipv6_value = item.get("acl_sess__v6policy")
        ipv4_rules = _parse_policy_rules(
            ipv4_value,
            "ipv4",
            f"policies[{index}].ipv4_rules",
            warnings,
        )
        ipv6_rules = _parse_policy_rules(
            ipv6_value,
            "ipv6",
            f"policies[{index}].ipv6_rules",
            warnings,
        )
        out.append(
            AOS8Policy(
                name=name,
                rule_count=len(ipv4_rules) + len(ipv6_rules),
                ipv4_rules=ipv4_rules,
                ipv6_rules=ipv6_rules,
                raw=item,
            )
        )
    return out


def parse_aaa_profiles(
    export: dict[str, Any],
    warnings: list[str] | None = None,
) -> list[AOS8AAAProfile]:
    aaa = export.get("aaa")
    if not isinstance(aaa, dict):
        _warn(warnings, "export: aaa section is missing or malformed.")
        return []
    items = _dict_items(aaa.get("aaa_profiles"), "aaa.aaa_profiles", warnings)
    aliases = {
        "profile_name": ("profile-name", "name"),
        "default_user_role": ("default_user_role", "default-user-role"),
        "dot1x_auth_profile": ("dot1x_auth_profile", "dot1x-auth-profile"),
        "dot1x_default_role": ("dot1x_default_role", "dot1x-default-role"),
        "dot1x_server_group": ("dot1x_server_group", "dot1x-server-group"),
        "mac_auth_profile": ("mac_auth_profile", "mac-auth-profile"),
        "mac_default_role": ("mac_default_role", "mac-default-role"),
        "mac_server_group": (
            "mac_server_group",
            "mba_server_group",
            "mac-server-group",
        ),
        "accounting_server_group": ("rad_acct_sg", "radius-accounting-server-group"),
    }
    consumed = {key for values in aliases.values() for key in values}
    out: list[AOS8AAAProfile] = []
    for index, item in enumerate(items):
        out.append(
            AOS8AAAProfile(
                profile_name=_identifier(
                    item, aliases["profile_name"], "aaa.aaa_profiles", index, warnings
                ),
                default_user_role=_first(item, aliases["default_user_role"]),
                dot1x_auth_profile=_first(item, aliases["dot1x_auth_profile"]),
                dot1x_default_role=_first(item, aliases["dot1x_default_role"]),
                dot1x_server_group=_first(item, aliases["dot1x_server_group"]),
                mac_auth_profile=_first(item, aliases["mac_auth_profile"]),
                mac_default_role=_first(item, aliases["mac_default_role"]),
                mac_server_group=_first(item, aliases["mac_server_group"]),
                accounting_server_group=_first(item, aliases["accounting_server_group"]),
                settings=_remaining(item, consumed),
                raw=item,
            )
        )
    return out


def parse_auth_profiles(
    export: dict[str, Any],
    auth_type: str,
    warnings: list[str] | None = None,
) -> list[AOS8AuthProfile]:
    aaa = export.get("aaa")
    if not isinstance(aaa, dict):
        return []
    section = f"{auth_type}_auth_profiles"
    items = _dict_items(aaa.get(section), f"aaa.{section}", warnings)
    out: list[AOS8AuthProfile] = []
    for index, item in enumerate(items):
        out.append(
            AOS8AuthProfile(
                profile_name=_identifier(
                    item,
                    ("profile-name", "name"),
                    f"aaa.{section}",
                    index,
                    warnings,
                ),
                auth_type=auth_type,  # type: ignore[arg-type]
                settings=_remaining(item, {"profile-name", "name"}),
                raw=item,
            )
        )
    return out


def _server_reference(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        reference = _first(
            value,
            ("name", "server", "auth_server", "rad_server_name", "ldap_server_name"),
        )
        return str(reference) if reference is not None else None
    return None


def parse_server_groups(
    export: dict[str, Any],
    warnings: list[str] | None = None,
) -> list[AOS8ServerGroup]:
    aaa = export.get("aaa")
    if not isinstance(aaa, dict):
        return []
    items = _dict_items(aaa.get("server_groups"), "aaa.server_groups", warnings)
    out: list[AOS8ServerGroup] = []
    consumed = {
        "sg_name",
        "profile-name",
        "name",
        "auth_server",
        "auth-server",
        "fail_thru",
        "fail-through",
        "load_balance",
        "load-balance",
        "derivation_rules_vlan_role",
    }
    for index, item in enumerate(items):
        raw_servers = item.get("auth_server", item.get("auth-server", []))
        if raw_servers in (None, ""):
            raw_servers = []
        elif not isinstance(raw_servers, list):
            raw_servers = [raw_servers]
        servers: list[str] = []
        for server_index, value in enumerate(raw_servers):
            reference = _server_reference(value)
            if reference:
                servers.append(reference)
            else:
                _warn(
                    warnings,
                    f"export: aaa.server_groups[{index}].auth_server[{server_index}] "
                    "has no server reference.",
                )
        out.append(
            AOS8ServerGroup(
                name=_identifier(
                    item,
                    ("sg_name", "profile-name", "name"),
                    "aaa.server_groups",
                    index,
                    warnings,
                ),
                auth_servers=servers,
                auth_server_entries=list(raw_servers),
                fail_through=_first(item, ("fail_thru", "fail-through")),
                load_balance=_first(item, ("load_balance", "load-balance")),
                derivation_rules=item.get("derivation_rules_vlan_role"),
                settings=_remaining(item, consumed),
                raw=item,
            )
        )
    return out


def parse_auth_servers(
    export: dict[str, Any],
    warnings: list[str] | None = None,
) -> list[AOS8AuthServer]:
    aaa = export.get("aaa")
    if not isinstance(aaa, dict):
        return []
    specs = (
        ("radius_servers", "radius", ("rad_server_name", "name"), ("rad_host", "host")),
        ("ldap_servers", "ldap", ("ldap_server_name", "name"), ("ldap_host", "host")),
        (
            "tacacs_servers",
            "tacacs",
            ("tacacs_server_name", "name"),
            ("tacacs_host", "host"),
        ),
    )
    out: list[AOS8AuthServer] = []
    for section, server_type, name_keys, host_keys in specs:
        items = _dict_items(aaa.get(section), f"aaa.{section}", warnings)
        consumed = {*name_keys, *host_keys}
        for index, item in enumerate(items):
            out.append(
                AOS8AuthServer(
                    name=_identifier(
                        item, name_keys, f"aaa.{section}", index, warnings
                    ),
                    server_type=server_type,  # type: ignore[arg-type]
                    host=_first(item, host_keys),
                    settings=_remaining(item, consumed),
                    raw=item,
                )
            )
    return out


def parse_routes(
    export: dict[str, Any],
    warnings: list[str] | None = None,
) -> list[AOS8Route]:
    routing = export.get("routing")
    if not isinstance(routing, dict):
        _warn(warnings, "export: routing section is missing or malformed.")
        return []
    out: list[AOS8Route] = []
    for section, address_family in (("ipv4_routes", "ipv4"), ("ipv6_routes", "ipv6")):
        items = _dict_items(routing.get(section), f"routing.{section}", warnings)
        consumed = {
            "destip",
            "destination",
            "destmask",
            "netmask",
            "nexthop",
            "next-hop",
            "nexthop1",
            "secondary-next-hop",
            "vlanid",
            "vlan",
            "cost",
            "cost1",
            "zero",
        }
        for index, item in enumerate(items):
            destination = _first(item, ("destip", "destination"))
            next_hop = _first(item, ("nexthop", "next-hop"))
            if destination is None:
                _warn(
                    warnings,
                    f"export: routing.{section}[{index}] has no destination.",
                )
            if next_hop is None:
                _warn(
                    warnings,
                    f"export: routing.{section}[{index}] has no primary next hop.",
                )
            out.append(
                AOS8Route(
                    address_family=address_family,  # type: ignore[arg-type]
                    destination=str(destination) if destination is not None else None,
                    netmask=_first(item, ("destmask", "netmask")),
                    next_hop=str(next_hop) if next_hop is not None else None,
                    secondary_next_hop=_first(
                        item, ("nexthop1", "secondary-next-hop")
                    ),
                    vlan_id=_first(item, ("vlanid", "vlan")),
                    cost=item.get("cost"),
                    secondary_cost=item.get("cost1"),
                    zero=item.get("zero"),
                    settings=_remaining(item, consumed),
                    raw=item,
                )
            )
    return out


def parse_vrrp(
    export: dict[str, Any],
    warnings: list[str] | None = None,
) -> list[AOS8VRRP]:
    routing = export.get("routing")
    if not isinstance(routing, dict):
        return []
    out: list[AOS8VRRP] = []
    for section, address_family, prefix in (
        ("vrrp", "ipv4", "vrrp"),
        ("vrrp6", "ipv6", "vrrp6"),
    ):
        items = _dict_items(routing.get(section), f"routing.{section}", warnings)
        keys = {
            "id",
            f"{prefix}_ip",
            f"{prefix}_vlan",
            f"{prefix}_priority",
            f"{prefix}_preempt",
            f"{prefix}_shut",
            f"{prefix}_adv_interval",
            f"{prefix}_holdtime",
            f"{prefix}_desc",
            f"{prefix}_auth",
            f"{prefix}_track_intf",
            f"{prefix}_track_master",
            f"{prefix}_track_uptime",
            f"{prefix}_track_vlan",
        }
        for index, item in enumerate(items):
            vrid = item.get("id")
            if vrid is None:
                _warn(warnings, f"export: routing.{section}[{index}] has no VRRP ID.")
            tracking = {
                key.removeprefix(f"{prefix}_track_"): item[key]
                for key in sorted(item)
                if key.startswith(f"{prefix}_track_")
            }
            out.append(
                AOS8VRRP(
                    address_family=address_family,  # type: ignore[arg-type]
                    vrid=vrid,
                    virtual_ip=item.get(f"{prefix}_ip"),
                    vlan_id=item.get(f"{prefix}_vlan"),
                    priority=item.get(f"{prefix}_priority"),
                    preempt=item.get(f"{prefix}_preempt"),
                    shutdown=item.get(f"{prefix}_shut"),
                    advertisement_interval=item.get(f"{prefix}_adv_interval"),
                    hold_time=item.get(f"{prefix}_holdtime"),
                    description=item.get(f"{prefix}_desc"),
                    authentication=item.get(f"{prefix}_auth"),
                    tracking=tracking,
                    settings=_remaining(item, keys),
                    raw=item,
                )
            )
    return out


_NETDST_FAMILIES: tuple[tuple[str, str, str], ...] = (
    ("netdst", "ipv4", "netdst"),
    ("netdst6", "ipv6", "netdst6"),
)


def parse_network_destinations(
    export: dict[str, Any],
    warnings: list[str] | None = None,
) -> list[AOS8NetworkDestination]:
    """Parse AOS8 IPv4/IPv6 destination aliases (`netdst`/`netdst6`) into a
    single combined, `address_family`-tagged list (same pattern as
    `parse_routes`/`parse_vrrp` for their IPv4/IPv6 section pairs).
    """
    out: list[AOS8NetworkDestination] = []
    for section, family, prefix in _NETDST_FAMILIES:
        items = _optional_dict_items(export, section, warnings)
        name_keys = ("dstname", f"{prefix}__name", "name")
        for index, item in enumerate(items):
            name = _identifier(item, name_keys, section, index, warnings)
            out.append(
                AOS8NetworkDestination(
                    address_family=family,  # type: ignore[arg-type]
                    name=name,
                    description=_first(item, (f"{prefix}__desc", "description")),
                    host=item.get(f"{prefix}__host"),
                    network=item.get(f"{prefix}__network"),
                    range=item.get(f"{prefix}__range"),
                    invert=_normalize_optional_bool(item.get(f"{prefix}__invert")),
                    raw=item,
                )
            )
    return out


def _parse_ethernet_acl_rules(
    value: Any,
    path: str,
    warnings: list[str] | None,
) -> list[AOS8EthernetACLRule]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        _warn(warnings, f"export: {path} is malformed; expected a list of rules.")
        return [AOS8EthernetACLRule(unsupported_fields={"raw_rule": value}, raw=value)]
    aliases = {
        "source": ("source", "src", "source-mac", "smac", "source-address"),
        "destination": (
            "destination",
            "dst",
            "destination-mac",
            "dmac",
            "destination-address",
        ),
        "ethertype": ("ethertype", "ether-type", "frame-type", "type"),
        "vlan": ("vlan", "vlan-id"),
        "action": ("action", "permit", "deny"),
        "log": ("log", "logging"),
    }
    consumed = {key for values in aliases.values() for key in values}
    rules: list[AOS8EthernetACLRule] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            _warn(warnings, f"export: {path}[{index}] is not an object.")
            rules.append(
                AOS8EthernetACLRule(unsupported_fields={"raw_rule": item}, raw=item)
            )
            continue
        rules.append(
            AOS8EthernetACLRule(
                source=_first(item, aliases["source"]),
                destination=_first(item, aliases["destination"]),
                ethertype=_first(item, aliases["ethertype"]),
                vlan=_first(item, aliases["vlan"]),
                action=_first(item, aliases["action"]),
                log=_first(item, aliases["log"]),
                unsupported_fields=_remaining(item, consumed),
                raw=item,
            )
        )
    return rules


def parse_ethernet_acls(
    export: dict[str, Any],
    warnings: list[str] | None = None,
) -> list[AOS8EthernetACL]:
    """Parse AOS8 Ethernet ACLs (`acl_eth`, 200-299 range) the same
    defensive way `parse_policies` handles `acl_sess`: a flexible per-rule
    alias set plus an `unsupported_fields` catch-all, since no nested
    `acl_eth__policy` rule schema is available locally beyond the property
    name (`src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/aos8.json`
    `aos8_post_object_acl_eth`).
    """
    items = _optional_dict_items(export, "acl_eth", warnings)
    out: list[AOS8EthernetACL] = []
    for index, item in enumerate(items):
        name = _identifier(
            item, ("accname", "name", "profile-name"), "acl_eth", index, warnings
        )
        legacy_rules = item.get("rule", item.get("rules"))
        rule_value = item.get("acl_eth__policy", legacy_rules)
        rules = _parse_ethernet_acl_rules(
            rule_value, f"acl_eth[{index}].rules", warnings
        )
        out.append(
            AOS8EthernetACL(name=name, rule_count=len(rules), rules=rules, raw=item)
        )
    return out


def parse_whitelist_rules(
    export: dict[str, Any],
    warnings: list[str] | None = None,
) -> list[AOS8WhitelistRule]:
    """Parse AOS8 IP-classification whitelist rules (`whitelist_rule`):
    start/end IP address ranges (`sipaddr`/`eipaddr`). The separate, global
    `whitelist` object (Activate-sync provisioning URL/credentials) has no
    per-item shape to normalize and is intentionally not parsed here; see
    `AOS8WhitelistRule`'s docstring and
    `docs/aos8-migration-contract-matrix.md`.
    """
    items = _optional_dict_items(export, "whitelist_rule", warnings)
    out: list[AOS8WhitelistRule] = []
    for index, item in enumerate(items):
        start_ip = _first(item, ("sipaddr", "start-ip", "start_ip"))
        end_ip = _first(item, ("eipaddr", "end-ip", "end_ip"))
        if start_ip is None or end_ip is None:
            _warn(
                warnings,
                f"export: whitelist_rule[{index}] is missing a start or end "
                "IP address.",
            )
        out.append(AOS8WhitelistRule(start_ip=start_ip, end_ip=end_ip, raw=item))
    return out


def parse_wired_auth_profiles(
    export: dict[str, Any],
    warnings: list[str] | None = None,
) -> list[AOS8WiredAuthProfile]:
    """Parse the AOS8 singleton wired-auth AAA attach object
    (`wired_auth_profile`, `aaa.wired_auth_profiles` in
    `aos8_export_all()`'s export). AOS8 defines this as an unnamed,
    single-instance object (no `profile-name` in its request-body schema);
    every parsed instance is identified `"global"` (`"global-{n}"` for any
    additional entry beyond the first, which is unexpected and always
    warned about rather than silently dropped or overwritten).
    """
    items = _optional_aaa_items(export, "wired_auth_profiles", warnings)
    consumed = {"wired_aaa_profile", "wired_blacklist_time"}
    out: list[AOS8WiredAuthProfile] = []
    for index, item in enumerate(items):
        if index > 0:
            _warn(
                warnings,
                "export: aaa.wired_auth_profiles has more than one entry; "
                "AOS8 defines this as a singleton object -- every entry is "
                "retained for review rather than guessed at.",
            )
        out.append(
            AOS8WiredAuthProfile(
                aaa_profile=_first(item, ("wired_aaa_profile",)),
                blacklist_time=item.get("wired_blacklist_time"),
                settings=_remaining(item, consumed),
                raw=item,
            )
        )
    return out


def parse_stateful_dot1x_auth_profiles(
    export: dict[str, Any],
    warnings: list[str] | None = None,
) -> list[AOS8StatefulDot1xAuthProfile]:
    """Parse the AOS8 singleton stateful (captive-portal-style) 802.1X
    auth config (`stateful_dot1x_auth_profile`,
    `aaa.stateful_dot1x_auth_profiles` in the export). Same singleton
    identifier convention as `parse_wired_auth_profiles`.
    """
    items = _optional_aaa_items(export, "stateful_dot1x_auth_profiles", warnings)
    consumed = {
        "stateful_dot1x_mode",
        "stateful_dot1x_server_group",
        "statefuldot1x_default_role",
        "timeout",
    }
    out: list[AOS8StatefulDot1xAuthProfile] = []
    for index, item in enumerate(items):
        if index > 0:
            _warn(
                warnings,
                "export: aaa.stateful_dot1x_auth_profiles has more than one "
                "entry; AOS8 defines this as a singleton object -- every "
                "entry is retained for review rather than guessed at.",
            )
        out.append(
            AOS8StatefulDot1xAuthProfile(
                mode=item.get("stateful_dot1x_mode"),
                server_group=_first(item, ("stateful_dot1x_server_group",)),
                default_role=_first(item, ("statefuldot1x_default_role",)),
                timeout=item.get("timeout"),
                settings=_remaining(item, consumed),
                raw=item,
            )
        )
    return out


def parse_wispr_auth_profiles(
    export: dict[str, Any],
    warnings: list[str] | None = None,
) -> list[AOS8WisprAuthProfile]:
    """Parse AOS8 WISPr authentication profiles (`wispr_auth_profile`,
    `aaa.wispr_auth_profiles` in the export), named by `profile-name`.
    """
    items = _optional_aaa_items(export, "wispr_auth_profiles", warnings)
    consumed = {"profile-name", "name", "wispr_default_role", "wispr_server_group"}
    out: list[AOS8WisprAuthProfile] = []
    for index, item in enumerate(items):
        name = _identifier(
            item, ("profile-name", "name"), "aaa.wispr_auth_profiles", index, warnings
        )
        out.append(
            AOS8WisprAuthProfile(
                profile_name=name,
                default_role=_first(item, ("wispr_default_role",)),
                server_group=_first(item, ("wispr_server_group",)),
                settings=_remaining(item, consumed),
                raw=item,
            )
        )
    return out


def parse_cp_auth_profiles(
    export: dict[str, Any],
    warnings: list[str] | None = None,
) -> list[AOS8CaptivePortalAuthProfile]:
    """Parse AOS8 captive-portal authentication profiles
    (`cp_auth_profile`, `aaa.cp_auth_profiles` in the export), named by
    `profile-name`.
    """
    items = _optional_aaa_items(export, "cp_auth_profiles", warnings)
    consumed = {
        "profile-name",
        "name",
        "cp_default_role",
        "cp_default_guest_role",
        "cp_server_group",
    }
    out: list[AOS8CaptivePortalAuthProfile] = []
    for index, item in enumerate(items):
        name = _identifier(
            item, ("profile-name", "name"), "aaa.cp_auth_profiles", index, warnings
        )
        out.append(
            AOS8CaptivePortalAuthProfile(
                profile_name=name,
                default_role=_first(item, ("cp_default_role",)),
                default_guest_role=_first(item, ("cp_default_guest_role",)),
                server_group=_first(item, ("cp_server_group",)),
                settings=_remaining(item, consumed),
                raw=item,
            )
        )
    return out


def parse_krb_auth_profiles(
    export: dict[str, Any],
    warnings: list[str] | None = None,
) -> list[AOS8KerberosAuthProfile]:
    """Parse AOS8 stateful Kerberos authentication profiles
    (`krb_auth_profile`, `aaa.krb_auth_profiles` in the export), named by
    `profile-name`.
    """
    items = _optional_aaa_items(export, "krb_auth_profiles", warnings)
    consumed = {"profile-name", "name", "krb_default_role", "krb_server_group", "krb_timeout"}
    out: list[AOS8KerberosAuthProfile] = []
    for index, item in enumerate(items):
        name = _identifier(
            item, ("profile-name", "name"), "aaa.krb_auth_profiles", index, warnings
        )
        out.append(
            AOS8KerberosAuthProfile(
                profile_name=name,
                default_role=_first(item, ("krb_default_role",)),
                server_group=_first(item, ("krb_server_group",)),
                timeout=item.get("krb_timeout"),
                settings=_remaining(item, consumed),
                raw=item,
            )
        )
    return out


def parse_ntlm_auth_profiles(
    export: dict[str, Any],
    warnings: list[str] | None = None,
) -> list[AOS8NTLMAuthProfile]:
    """Parse AOS8 stateful NTLM authentication profiles
    (`ntlm_auth_profile`, `aaa.ntlm_auth_profiles` in the export), named
    by `profile-name`.
    """
    items = _optional_aaa_items(export, "ntlm_auth_profiles", warnings)
    consumed = {
        "profile-name",
        "name",
        "ntlm_default_role",
        "ntlm_server_group",
        "ntlm_enable",
        "ntlm_timeout",
    }
    out: list[AOS8NTLMAuthProfile] = []
    for index, item in enumerate(items):
        name = _identifier(
            item, ("profile-name", "name"), "aaa.ntlm_auth_profiles", index, warnings
        )
        out.append(
            AOS8NTLMAuthProfile(
                profile_name=name,
                default_role=_first(item, ("ntlm_default_role",)),
                server_group=_first(item, ("ntlm_server_group",)),
                enabled=item.get("ntlm_enable"),
                timeout=item.get("ntlm_timeout"),
                settings=_remaining(item, consumed),
                raw=item,
            )
        )
    return out


def parse_export_report(
    export: dict[str, Any],
) -> tuple[dict[str, list[Any]], list[str]]:
    """Parse an export and return normalized objects plus explicit parse warnings."""
    warnings: list[str] = []
    if not isinstance(export, dict):
        return _empty_parse(), ["export: expected an object; no source objects were parsed."]
    parsed = {
        "wlans": parse_wlans(export, warnings),
        "roles": parse_roles(export, warnings),
        "vlans": parse_vlans(export, warnings),
        "ap_groups": parse_ap_groups(export, warnings),
        "controllers": parse_controllers(export, warnings),
        "policies": parse_policies(export, warnings),
        "aaa_profiles": parse_aaa_profiles(export, warnings),
        "dot1x_auth_profiles": parse_auth_profiles(export, "dot1x", warnings),
        "mac_auth_profiles": parse_auth_profiles(export, "mac", warnings),
        "server_groups": parse_server_groups(export, warnings),
        "auth_servers": parse_auth_servers(export, warnings),
        "routes": parse_routes(export, warnings),
        "vrrp": parse_vrrp(export, warnings),
        "network_destinations": parse_network_destinations(export, warnings),
        "ethernet_acls": parse_ethernet_acls(export, warnings),
        "whitelist_rules": parse_whitelist_rules(export, warnings),
        "wired_auth_profiles": parse_wired_auth_profiles(export, warnings),
        "stateful_dot1x_auth_profiles": parse_stateful_dot1x_auth_profiles(
            export, warnings
        ),
        "wispr_auth_profiles": parse_wispr_auth_profiles(export, warnings),
        "cp_auth_profiles": parse_cp_auth_profiles(export, warnings),
        "krb_auth_profiles": parse_krb_auth_profiles(export, warnings),
        "ntlm_auth_profiles": parse_ntlm_auth_profiles(export, warnings),
    }
    return parsed, sorted(set(warnings))


def _empty_parse() -> dict[str, list[Any]]:
    return {
        "wlans": [],
        "roles": [],
        "vlans": [],
        "ap_groups": [],
        "controllers": [],
        "policies": [],
        "aaa_profiles": [],
        "dot1x_auth_profiles": [],
        "mac_auth_profiles": [],
        "server_groups": [],
        "auth_servers": [],
        "routes": [],
        "vrrp": [],
        "network_destinations": [],
        "ethernet_acls": [],
        "whitelist_rules": [],
        "wired_auth_profiles": [],
        "stateful_dot1x_auth_profiles": [],
        "wispr_auth_profiles": [],
        "cp_auth_profiles": [],
        "krb_auth_profiles": [],
        "ntlm_auth_profiles": [],
    }


def parse_export(export: dict[str, Any]) -> dict[str, list[Any]]:
    """Backward-compatible normalized parse without the companion warning list."""
    if not isinstance(export, dict):
        return _empty_parse()
    return parse_export_report(export)[0]
