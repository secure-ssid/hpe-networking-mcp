"""Normalized ArubaOS 8 dataclasses and Classic/New Central migration candidates.

Pure-python, no network calls and no dependency on `src/hpe_networking_mcp/mcp_servers/`. These
dataclasses are the shared vocabulary between `src/hpe_networking_mcp/pipeline/aos8_parsers.py`
(export -> normalized objects) and `src/hpe_networking_mcp/pipeline/aos8_migration.py` (normalized
objects -> deterministic migration candidates).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Normalized AOS8 source objects
# ---------------------------------------------------------------------------


@dataclass
class AOS8Wlan:
    """A merged AOS8 WLAN: one SSID profile plus its linked virtual AP, if any.

    ``wpa3_transition``, ``passphrase_present``, and ``psk_hexkey_present`` are bounded,
    evidenced signals extracted directly from the AOS8 ``ssid_prof`` object
    (``wpa3_transition``, ``wpa_passphrase``, ``wpa_hexkey`` — see
    ``src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/aos8.json`` `aos8_post_object_ssid_prof`
    request-body properties). Only *presence* of a passphrase/PSK hex key is
    recorded here, never the secret value itself, so this dataclass is always
    safe to serialize and never needs redaction on its own.
    """

    profile_name: str
    essid: str | None = None
    opmode: str | None = None
    vlan: str | int | None = None
    forward_mode: str | None = None
    aaa_profile: str | None = None
    virtual_ap_profile: str | None = None
    wpa3_transition: bool | None = None
    passphrase_present: bool = False
    psk_hexkey_present: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AOS8Role:
    rolename: str
    vlan: str | int | None = None
    acl: str | None = None
    captive_portal_profile: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AOS8Vlan:
    vlan_id: str | int
    description: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AOS8ApGroup:
    profile_name: str
    virtual_ap_profiles: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AOS8Controller:
    name: str | None = None
    ip_address: str | None = None
    model: str | None = None
    version: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AOS8Policy:
    """An AOS8 session ACL (`acl_sess`), referred to as a "policy" in the GUI."""

    name: str
    rule_count: int | None = None
    ipv4_rules: list["AOS8PolicyRule"] = field(default_factory=list)
    ipv6_rules: list["AOS8PolicyRule"] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AOS8PolicyRule:
    """Normalized details shared by the AOS8 IPv4 and IPv6 session-ACL formats."""

    address_family: Literal["ipv4", "ipv6"]
    source: Any = None
    destination: Any = None
    service: Any = None
    action: Any = None
    log: Any = None
    unsupported_fields: dict[str, Any] = field(default_factory=dict)
    raw: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AOS8NetworkDestination:
    """A named IPv4/IPv6 destination alias (AOS8 `netdst`/`netdst6`).

    Fields mirror the `netdst__*`/`netdst6__*` request-body properties from
    `src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/aos8.json`
    (`aos8_post_object_netdst`/`aos8_post_object_netdst6`): a name, an
    optional description, and one (or more) of a single host, a
    network/prefix, or a range, plus an optional match-polarity `invert`
    flag. AOS8 does not document which of host/network/range are mutually
    exclusive, so all three are carried through verbatim rather than
    normalized into one canonical shape.
    """

    address_family: Literal["ipv4", "ipv6"]
    name: str
    description: str | None = None
    host: Any = None
    network: Any = None
    range: Any = None
    invert: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AOS8EthernetACLRule:
    """Normalized details from one AOS8 Ethernet ACL (`acl_eth`) rule.

    Bounded and alias-based like `AOS8PolicyRule`, but there is no local
    OpenAPI evidence for the nested `acl_eth__policy` rule schema beyond the
    named request-body property (`src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/aos8.json`
    `aos8_post_object_acl_eth`) -- every field not matched by a known L2
    alias (source/destination MAC, ethertype, VLAN, action, log) is retained
    verbatim in `unsupported_fields` rather than guessed.
    """

    source: Any = None
    destination: Any = None
    ethertype: Any = None
    vlan: Any = None
    action: Any = None
    log: Any = None
    unsupported_fields: dict[str, Any] = field(default_factory=dict)
    raw: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AOS8EthernetACL:
    """An AOS8 Ethernet ACL (`acl_eth`), numbered in the 200-299 range."""

    name: str
    rule_count: int | None = None
    rules: list["AOS8EthernetACLRule"] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AOS8WhitelistRule:
    """An AOS8 IP-classification whitelist rule (`whitelist_rule`): a start/end
    IP address range (`sipaddr`/`eipaddr` in
    `aos8_post_object_whitelist_rule`'s request body). This models
    `whitelist_rule` only -- the separate, global `whitelist` object
    (Activate-sync provisioning URL/credentials) has no per-item shape to
    normalize and is intentionally not parsed as a migration candidate; see
    `docs/aos8-migration-contract-matrix.md`.
    """

    start_ip: str | None = None
    end_ip: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AOS8AAAProfile:
    profile_name: str
    default_user_role: str | None = None
    dot1x_auth_profile: str | None = None
    dot1x_default_role: str | None = None
    dot1x_server_group: str | None = None
    mac_auth_profile: str | None = None
    mac_default_role: str | None = None
    mac_server_group: str | None = None
    accounting_server_group: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AOS8AuthProfile:
    profile_name: str
    auth_type: Literal["dot1x", "mac"]
    settings: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AOS8ServerGroup:
    name: str
    auth_servers: list[str] = field(default_factory=list)
    auth_server_entries: list[Any] = field(default_factory=list)
    fail_through: Any = None
    load_balance: Any = None
    derivation_rules: Any = None
    settings: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AOS8AuthServer:
    name: str
    server_type: Literal["radius", "ldap", "tacacs"]
    host: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AOS8WiredAuthProfile:
    """The AOS8 global wired 802.1X/MAC-auth AAA attach point (`wired_auth_profile`).

    Unlike `AOS8AuthProfile` (per-profile-name device dot1x/mac-auth
    profiles), this is a singleton, unnamed object: AOS8 defines exactly
    one instance per config node, referencing the AAA profile applied to
    wired ports plus a blacklist duration. Fields mirror the two
    request-body properties documented for
    `aos8_post_object_wired_auth_profile`
    (`src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/aos8.json`): `wired_aaa_profile`,
    `wired_blacklist_time`. Because it has no name field at all, every
    parsed instance is given the fixed identifier `"global"`
    (`"global-{n}"` for any additional, unexpected instance --
    `hpe_networking_mcp.pipeline.aos8_parsers.parse_wired_auth_profiles` always warns rather
    than silently overwriting or dropping one). Reference-only: see
    `REFERENCE_ONLY_OBJECT_TYPES`.
    """

    aaa_profile: str | None = None
    blacklist_time: Any = None
    settings: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AOS8StatefulDot1xAuthProfile:
    """The AOS8 global stateful (captive-portal-style) 802.1X auth config
    (`stateful_dot1x_auth_profile`). Also a singleton, unnamed object --
    same identifier convention as `AOS8WiredAuthProfile`. Fields mirror
    `aos8_post_object_stateful_dot1x_auth_profile`'s request-body
    properties: `stateful_dot1x_mode`, `stateful_dot1x_server_group`,
    `statefuldot1x_default_role`, `timeout`. Reference-only: see
    `REFERENCE_ONLY_OBJECT_TYPES`.
    """

    mode: Any = None
    server_group: str | None = None
    default_role: str | None = None
    timeout: Any = None
    settings: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AOS8WisprAuthProfile:
    """An AOS8 WISPr (Wireless ISP Roaming) authentication profile
    (`wispr_auth_profile`), named by `profile-name`. Fields mirror the
    subset of `aos8_post_object_wispr_auth_profile`'s request-body
    properties with a clear post-auth/authentication meaning
    (`wispr_default_role`, `wispr_server_group`); every other documented
    property (`agent_string`, the `wispr_id_*`/`wispr_name_*` location
    fields, `wispr_load_thresh`, `wispr_max_delay`, `wispr_maxf`,
    `wispr_min_delay`, `wispr_auth_profile_clone`) is retained verbatim in
    `.settings`. Reference-only: see `REFERENCE_ONLY_OBJECT_TYPES`.
    """

    profile_name: str
    default_role: str | None = None
    server_group: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AOS8CaptivePortalAuthProfile:
    """An AOS8 captive-portal authentication profile (`cp_auth_profile`),
    named by `profile-name`. Fields mirror the subset of
    `aos8_post_object_cp_auth_profile`'s request-body properties with a
    clear post-auth/authentication meaning (`cp_default_role`,
    `cp_default_guest_role`, `cp_server_group`); every other documented
    property (redirect/branding/proxy/AUP/session/black-white-list
    settings) is retained verbatim in `.settings`. Reference-only: see
    `REFERENCE_ONLY_OBJECT_TYPES`.
    """

    profile_name: str
    default_role: str | None = None
    default_guest_role: str | None = None
    server_group: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AOS8KerberosAuthProfile:
    """An AOS8 stateful Kerberos authentication profile
    (`krb_auth_profile`), named by `profile-name`. Fields mirror
    `aos8_post_object_krb_auth_profile`'s request-body properties:
    `krb_default_role`, `krb_server_group`, `krb_timeout`;
    `krb_auth_profile_clone` is retained in `.settings`. Reference-only:
    see `REFERENCE_ONLY_OBJECT_TYPES`.
    """

    profile_name: str
    default_role: str | None = None
    server_group: str | None = None
    timeout: Any = None
    settings: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AOS8NTLMAuthProfile:
    """An AOS8 stateful NTLM authentication profile (`ntlm_auth_profile`),
    named by `profile-name`. Fields mirror
    `aos8_post_object_ntlm_auth_profile`'s request-body properties:
    `ntlm_default_role`, `ntlm_server_group`, `ntlm_enable`,
    `ntlm_timeout`; `ntlm_auth_profile_clone` is retained in `.settings`.
    Reference-only: see `REFERENCE_ONLY_OBJECT_TYPES`.
    """

    profile_name: str
    default_role: str | None = None
    server_group: str | None = None
    enabled: Any = None
    timeout: Any = None
    settings: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AOS8Route:
    address_family: Literal["ipv4", "ipv6"]
    destination: str | None = None
    netmask: str | None = None
    next_hop: str | None = None
    secondary_next_hop: str | None = None
    vlan_id: str | int | None = None
    cost: Any = None
    secondary_cost: Any = None
    zero: Any = None
    settings: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AOS8VRRP:
    address_family: Literal["ipv4", "ipv6"]
    vrid: str | int | None = None
    virtual_ip: Any = None
    vlan_id: str | int | None = None
    priority: Any = None
    preempt: Any = None
    shutdown: Any = None
    advertisement_interval: Any = None
    hold_time: Any = None
    description: str | None = None
    authentication: Any = None
    tracking: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Migration candidates
# ---------------------------------------------------------------------------


@dataclass
class ClassicCentralCandidate:
    """A candidate object for migration into Aruba Central (Classic)."""

    object_type: str
    identifier: str
    payload: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    apply_order: int = 100
    unsupported_fields: dict[str, Any] = field(default_factory=dict)
    requires_secret_input: bool = False
    secret_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NewCentralCandidate:
    """A candidate object for migration into HPE Aruba Networking Central (New Central)."""

    object_type: str
    identifier: str
    payload: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    apply_order: int = 100
    unsupported_fields: dict[str, Any] = field(default_factory=dict)
    requires_secret_input: bool = False
    secret_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Known-lossy fields — explicit warnings, never a silent drop.
# ---------------------------------------------------------------------------

UNSUPPORTED_FIELDS: dict[str, dict[str, str]] = {
    "wlan": {
        "opmode": (
            "AOS8 `opmode` cipher suites (e.g. WPA-TKIP-only, mixed WEP) have no "
            "direct Central/New Central WLAN security equivalent; map manually."
        ),
        "forward_mode": (
            "AOS8 per-virtual-AP forward mode (tunnel/bridge/split-tunnel) is "
            "controlled differently in Central/New Central; verify VLAN and "
            "gateway-role assignment after migration."
        ),
    },
    "role": {
        "captive_portal_profile": (
            "AOS8 captive-portal profiles bound to a user role are not migrated "
            "automatically; recreate the captive portal policy on the target."
        ),
    },
    "policy": {
        "unsupported_rule_field": (
            "This AOS8 session-ACL rule field has no deterministic target-neutral "
            "mapping and is retained in `unsupported_fields` for adapter/manual review."
        ),
    },
    "network_destination": {
        "invert": (
            "AOS8 destination-alias match-polarity negation (`invert`) has no "
            "direct Central/New Central destination-alias equivalent; verify "
            "match polarity manually after migration."
        ),
    },
    "ethernet_acl": {
        "unsupported_rule_field": (
            "This AOS8 Ethernet ACL rule field has no deterministic target-neutral "
            "mapping and is retained in `unsupported_fields` for adapter/manual review."
        ),
    },
}


# ---------------------------------------------------------------------------
# Reference-only object families (candidates emitted for dependency tracking
# and operator review, no adapter write mapping exists in this repository).
# ---------------------------------------------------------------------------

# `object_type` values for which `src/hpe_networking_mcp/pipeline/aos8_migration.py` always emits an
# explicit "no deterministic Classic/New Central adapter mapping exists"
# warning on every candidate (see `_append_for_both` callers for these
# families). This is the single source of truth other modules key off of
# (e.g. `src/hpe_networking_mcp/mcp_servers/aos8.py`'s `aos8_migration_dependency_plan`) instead of
# re-deriving or duplicating the same set from candidate warning text.
REFERENCE_ONLY_OBJECT_TYPES: frozenset[str] = frozenset(
    {
        "network_destination",
        "ethernet_acl",
        "whitelist_rule",
        # Wired/captive-portal/WISPr/Kerberos/NTLM/stateful-802.1X
        # authentication-profile families (`aos8-migration-contract-matrix.md`
        # §6.14): normalized and dependency-tracked, but no target adapter
        # write mapping is proven for any of them yet.
        "wired_auth_profile",
        "stateful_dot1x_auth_profile",
        "wispr_auth_profile",
        "cp_auth_profile",
        "krb_auth_profile",
        "ntlm_auth_profile",
    }
)
