"""Deterministic, pure-python AOS8 migration candidate planning.

This module performs no target writes. It emits ordered candidate IR for later
Classic/New Central adapters, explicit dependency references, and warnings or
`unsupported_fields` entries for every source field that is not normalized.
"""

from __future__ import annotations

import json
import re
from typing import Any

from hpe_networking_mcp.pipeline.aos8_parsers import parse_export_report
from hpe_networking_mcp.pipeline.aos8_schema import (
    AOS8VRRP,
    UNSUPPORTED_FIELDS,
    AOS8AAAProfile,
    AOS8ApGroup,
    AOS8AuthProfile,
    AOS8AuthServer,
    AOS8CaptivePortalAuthProfile,
    AOS8Controller,
    AOS8EthernetACL,
    AOS8KerberosAuthProfile,
    AOS8NetworkDestination,
    AOS8NTLMAuthProfile,
    AOS8Policy,
    AOS8Role,
    AOS8Route,
    AOS8ServerGroup,
    AOS8StatefulDot1xAuthProfile,
    AOS8Vlan,
    AOS8WhitelistRule,
    AOS8WiredAuthProfile,
    AOS8WisprAuthProfile,
    AOS8Wlan,
    ClassicCentralCandidate,
    NewCentralCandidate,
)

APPLY_ORDER = {
    "vlan": 10,
    "auth_server": 10,
    "network_destination": 15,
    "whitelist_rule": 15,
    "dot1x_auth_profile": 20,
    "mac_auth_profile": 20,
    "server_group": 20,
    "policy": 20,
    "ethernet_acl": 20,
    "role": 30,
    # Wired/captive-portal/WISPr/Kerberos/stateful-dot1x authentication
    # profiles reference `role`/`server_group` (never `aaa_profile`), so
    # they sort after roles but before aaa_profile.
    "stateful_dot1x_auth_profile": 35,
    "wispr_auth_profile": 35,
    "cp_auth_profile": 35,
    "krb_auth_profile": 35,
    "ntlm_auth_profile": 35,
    "aaa_profile": 40,
    # The wired AAA attach point references `aaa_profile` itself, so it
    # sorts after aaa_profile.
    "wired_auth_profile": 45,
    "wlan": 50,
    "ap_group": 60,
    "route": 70,
    "vrrp": 80,
    "controller": 90,
}

# Explicit "no deterministic target mapping" candidate warning text for the
# reference-only families in `hpe_networking_mcp.pipeline.aos8_schema.REFERENCE_ONLY_OBJECT_TYPES`.
# Threaded through `_append_for_both`'s `warnings=` so it lands on every
# candidate for these families (unlike the controller family's message,
# which is only appended to the plan-level `warnings` list because
# controllers are Classic-only and never reach `_append_for_both`).
_REFERENCE_ONLY_WARNING = (
    "{object_type}:{identifier}: no deterministic Classic/New Central adapter "
    "mapping exists in this repository for this AOS8 object family; the "
    "candidate is retained for dependency tracking and operator review only."
)

_SECRET_MARKER = "<redacted:present>"
_EMPTY_SECRET_MARKER = "<redacted:empty>"
# `ldap_admindn`/`ldap_admin_dn` (the LDAP bind/admin distinguished name, e.g.
# `cn=admin,dc=example,dc=com`) is intentionally *not* listed here. It is a
# non-secret identifier needed to reconstruct an LDAP auth-server object, not a
# credential — only the accompanying bind password
# (`ldap_adminpasswd`/`ldap_adminpwd`, still listed below) is a secret. See
# docs/aos8-migration-contract-matrix.md §3 item 6.
_SENSITIVE_EXACT_KEYS = {
    "access_token",
    "admin_dn",
    "admin_password",
    "admin_passwd",
    "adminpwd",
    "bind_credential",
    "bind_credentials",
    "bind_dn",
    "bind_password",
    "bind_passwd",
    "bind_username",
    "bindpwd",
    "api_key",
    "api_token",
    "client_secret",
    "cppm_username_password",
    "credential",
    "credentials",
    "key",
    "ldap_adminpasswd",
    "ldap_adminpwd",
    "password",
    "passphrase",
    "passwd",
    "private_key",
    "presharedkey",
    "psk",
    "pwd",
    "rad_key",
    "radkey",
    "radius_key",
    "radiuskey",
    "radius_secret",
    "secret",
    "sharedkey",
    "sharedsecret",
    "shared_key",
    "shared_secret",
    "tacacs_key",
    "tacacskey",
    "tacacs_secret",
    "token",
    # AOS8 `ssid_prof` PSK/WEP key material
    # (`src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/aos8.json`
    # `aos8_post_object_ssid_prof` request-body properties) — shared secrets,
    # never persisted or returned in candidate payloads.
    "wpa_hexkey",
    "wepkey1",
    "wepkey2",
    "wepkey3",
    "wepkey4",
}
_SENSITIVE_KEY_PREFIXES = (
    "api_",
    "auth_",
    "client_",
    "credential_",
    "encryption_",
    "private_",
    "preshared_",
    "pre_shared_",
    "rad_",
    "radius_",
    "shared_",
    "secret_",
    "tacacs_",
)


def _normalized_key(key: Any) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", str(key).lower())).strip("_")


# Splits on the literal path separators this module uses to flatten nested
# source objects into single dict keys (e.g. `_wlan_payload` building
# `f"ssid_profile.{key}"` / `f"virtual_ap.{key}"` entries in
# `unsupported_fields`). Deliberately narrower than `_normalized_key`'s
# character class so a non-secret prefix segment (like `ssid_profile`) can't
# fuse with a secret leaf token (like `wpa_hexkey`) into a combined string
# that no longer matches `_SENSITIVE_EXACT_KEYS`/suffix checks.
_PATH_SEPARATOR_RE = re.compile(r"[./]")


def _is_sensitive_token(normalized: str) -> bool:
    if normalized in _SENSITIVE_EXACT_KEYS:
        return True
    if normalized.endswith(
        ("_password", "_passwd", "_pwd", "_passphrase", "_secret")
    ):
        return True
    return normalized.endswith("_key") and normalized.startswith(_SENSITIVE_KEY_PREFIXES)


def _is_sensitive_key(key: Any) -> bool:
    text = str(key)
    if _is_sensitive_token(_normalized_key(text)):
        return True
    # Flattened/path-like keys (e.g. "ssid_profile.wpa_hexkey") retain their
    # original "." / "/" separators before generic normalization would
    # collapse them into underscores indistinguishable from a compound word.
    # Evaluate the final path component alone against the same rules.
    segments = _PATH_SEPARATOR_RE.split(text)
    if len(segments) > 1:
        leaf = _normalized_key(segments[-1])
        if leaf:
            return _is_sensitive_token(leaf)
    return False


def _redact_sensitive_values(
    value: Any,
    path: str = "",
) -> tuple[Any, list[str]]:
    """Return a JSON-safe copy with credential values replaced by stable markers."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        secret_fields: list[str] = []
        for key in sorted(value):
            field_path = f"{path}.{key}" if path else str(key)
            field_value = value[key]
            if _is_sensitive_key(key):
                redacted[key] = (
                    _SECRET_MARKER
                    if field_value not in (None, "", [], {})
                    else _EMPTY_SECRET_MARKER
                )
                secret_fields.append(field_path)
                continue
            redacted_value, nested_fields = _redact_sensitive_values(
                field_value, field_path
            )
            redacted[key] = redacted_value
            secret_fields.extend(nested_fields)
        return redacted, secret_fields
    if isinstance(value, list):
        redacted_items: list[Any] = []
        secret_fields: list[str] = []
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]"
            redacted_item, nested_fields = _redact_sensitive_values(item, item_path)
            redacted_items.append(redacted_item)
            secret_fields.extend(nested_fields)
        return redacted_items, secret_fields
    if isinstance(value, tuple):
        redacted_items, secret_fields = _redact_sensitive_values(list(value), path)
        return redacted_items, secret_fields
    return value, []


def _sorted_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return {key: value[key] for key in sorted(value)}


def _sorted_items(payload: dict[str, Any]) -> list[tuple[str, Any]]:
    return sorted(payload.items(), key=lambda pair: pair[0])


def _diff_entry(source: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    safe_source, _ = _redact_sensitive_values(source)
    safe_candidate, _ = _redact_sensitive_values(candidate)
    return {
        "source": _sorted_items(safe_source),
        "candidate": _sorted_items(safe_candidate),
    }


def _remaining(raw: dict[str, Any], mapped_keys: set[str]) -> dict[str, Any]:
    return {key: raw[key] for key in sorted(raw) if key not in mapped_keys}


def _unsupported_warnings(
    object_type: str,
    identifier: str,
    unsupported: dict[str, Any],
    secret_fields: list[str],
) -> list[str]:
    secret_roots = {
        path.removeprefix("unsupported_fields.").split(".", 1)[0].split("[", 1)[0]
        for path in secret_fields
        if path.startswith("unsupported_fields.")
    }
    return [
        (
            f"{object_type}:{identifier}: source field {field!r} is not mapped; "
            "its exact value is retained in `unsupported_fields`."
        )
        for field in sorted(unsupported)
        if field not in secret_roots
    ]


def _dependency(object_type: str, identifier: Any) -> str | None:
    if identifier in (None, ""):
        return None
    return f"{object_type}:{identifier}"


def _dependencies(*values: str | None) -> list[str]:
    return sorted({value for value in values if value})


def _candidate(
    candidate_class: type[ClassicCentralCandidate] | type[NewCentralCandidate],
    object_type: str,
    identifier: str,
    payload: dict[str, Any],
    *,
    warnings: list[str] | None = None,
    dependencies: list[str] | None = None,
    unsupported_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    safe_payload, payload_secret_fields = _redact_sensitive_values(payload, "payload")
    safe_unsupported, unsupported_secret_fields = _redact_sensitive_values(
        _sorted_mapping(unsupported_fields or {}),
        "unsupported_fields",
    )
    secret_fields = sorted(set([*payload_secret_fields, *unsupported_secret_fields]))
    credential_warnings = [
        (
            f"{object_type}:{identifier}: credential field {field!r} was redacted; "
            "re-enter this credential on the target before apply."
        )
        for field in secret_fields
    ]
    local_warnings = sorted(
        set(
            [
                *(warnings or []),
                *_unsupported_warnings(
                    object_type,
                    identifier,
                    safe_unsupported,
                    secret_fields,
                ),
                *credential_warnings,
            ]
        )
    )
    return candidate_class(
        object_type=object_type,
        identifier=identifier,
        payload=safe_payload,
        warnings=local_warnings,
        dependencies=sorted(set(dependencies or [])),
        apply_order=APPLY_ORDER[object_type],
        unsupported_fields=safe_unsupported,
        requires_secret_input=bool(secret_fields),
        secret_fields=secret_fields,
    ).to_dict()


def _append_for_both(
    classic: list[dict[str, Any]],
    new: list[dict[str, Any]],
    object_type: str,
    identifier: str,
    payload: dict[str, Any],
    *,
    warnings: list[str] | None = None,
    dependencies: list[str] | None = None,
    unsupported_fields: dict[str, Any] | None = None,
) -> list[str]:
    classic_candidate = _candidate(
        ClassicCentralCandidate,
        object_type,
        identifier,
        payload,
        warnings=warnings,
        dependencies=dependencies,
        unsupported_fields=unsupported_fields,
    )
    new_candidate = _candidate(
        NewCentralCandidate,
        object_type,
        identifier,
        payload,
        warnings=warnings,
        dependencies=dependencies,
        unsupported_fields=unsupported_fields,
    )
    classic.append(classic_candidate)
    new.append(new_candidate)
    return [*classic_candidate["warnings"], *new_candidate["warnings"]]


_OPEN_OPMODES = {"open", "opensystem"}

# Bounded, fail-closed WLAN security-intent classification. Only literal
# tokens/fields with in-repo evidence are used:
#   - `opmode` exact match {"open", "opensystem"} mirrors the same check
#     already used by `src/hpe_networking_mcp/pipeline/aos8_target_adapters.py` (`_map_wlan`, both
#     adapters) for OPEN.
#   - `wpa3_transition`, `wpa_passphrase`/`wpa_hexkey` presence come from the
#     AOS8 `ssid_prof` object (`src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/aos8.json`
#     `aos8_post_object_ssid_prof` request-body properties).
#   - dot1x/MAC-auth chain comes from cross-referencing the WLAN's attached
#     `aaa_profile` name against parsed `AOS8AAAProfile` objects
#     (`src/hpe_networking_mcp/pipeline/aos8_schema.py`).
# Anything not covered by this evidence is reported as `"unknown"` with an
# explicit warning rather than guessed — never a default/optimistic mapping.
_WLAN_SECURITY_MODES = {
    "open",
    "wpa2_personal",
    "wpa3_sae",
    "wpa3_transition_personal",
    "enhanced_open",
    "enterprise_dot1x",
    "mac_auth_only",
    "mac_auth_psk",
    "unknown",
}

# Modes whose New Central mapping requires a transient, caller-supplied
# passphrase (src/hpe_networking_mcp/pipeline/aos8_target_adapters.py `_WLAN_PASSPHRASE_MODES`).
# The passphrase is NEVER recovered from source state -- only a presence
# boolean (`passphrase_present`/`psk_hexkey_present`) is ever carried in
# `security`. When the source export happened to also expose a raw secret
# value (common in AOS8 CLI output), `_redact_sensitive_values` already
# flags `requires_secret_input`/`secret_fields` for the real field path; this
# set exists to cover the remaining "presence boolean only, no raw value
# captured" case so preview/orchestrator secret-placeholder flows still see
# an accurate `requires_secret_input=True` signal.
_WLAN_PASSPHRASE_MODES = {"wpa2_personal", "wpa3_sae"}


def _wlan_security_intent(
    wlan: AOS8Wlan,
    aaa_profiles_by_name: dict[str, AOS8AAAProfile],
) -> tuple[dict[str, Any], list[str]]:
    """Derive a bounded, JSON-safe security-intent summary for one AOS8 WLAN.

    Never includes a passphrase/PSK value — only presence booleans and
    non-secret profile/role names already visible elsewhere in the candidate.
    Ambiguous or unverifiable combinations are reported as `mode="unknown"`
    with an explicit warning; they are never defaulted to a specific mode.
    """
    warnings: list[str] = []
    evidence: list[str] = []
    opmode_raw = wlan.opmode
    opmode_lower = str(opmode_raw).strip().lower() if opmode_raw not in (None, "") else ""
    aaa_profile_name = wlan.aaa_profile
    dot1x_auth_profile: str | None = None
    mac_auth_profile: str | None = None
    mode = "unknown"
    ambiguous = True
    # Set when the attached aaa_profile itself is unverifiable (missing from
    # the export), configures an unverified dot1x+MAC-auth combination, or
    # configures a dot1x/MAC server-group or accounting/auth server-group
    # reference without a corresponding explicit dot1x_auth_profile/
    # mac_auth_profile mapping. In all of those cases source authentication
    # intent genuinely cannot be determined, so the WLAN must stay "unknown"
    # rather than falling through to opmode/passphrase heuristics. A
    # *resolved* aaa_profile that is truly role-only (no dot1x/MAC-auth
    # profile AND no dot1x/MAC/accounting server-group reference — the common
    # "PSK WLAN with an attached role-assignment-only aaa_profile" AOS8
    # pattern) is NOT blocking: see the role-only + WPA2-PSK fixture at
    # https://github.com/secure-ssid/aos8-migration-tool/blob/7bfa884d8e8f1c7e97a7bfa42f15596aa42fcf79/tests/test_aos8_parser.py#L15-L34
    # (secondary, same-owner prior art — not an authoritative API contract).
    aaa_blocks_classification = False

    if aaa_profile_name:
        profile = aaa_profiles_by_name.get(aaa_profile_name)
        if profile is None:
            warnings.append(
                f"wlan:{wlan.profile_name}: aaa_profile {aaa_profile_name!r} was not "
                "present in the export; WLAN authentication intent (enterprise/MAC-auth) "
                "cannot be verified and is reported as unknown."
            )
            aaa_blocks_classification = True
        else:
            evidence.append(f"aaa_profile:{aaa_profile_name} resolved in export")
            dot1x_auth_profile = profile.dot1x_auth_profile
            mac_auth_profile = profile.mac_auth_profile
            if dot1x_auth_profile and mac_auth_profile:
                warnings.append(
                    f"wlan:{wlan.profile_name}: aaa_profile {aaa_profile_name!r} "
                    "configures both a dot1x and a MAC-auth profile; this combination "
                    "is not a verified AOS8 source pattern in this repository and is "
                    "reported as unknown."
                )
                aaa_blocks_classification = True
            elif dot1x_auth_profile:
                mode = "enterprise_dot1x"
                ambiguous = False
            elif mac_auth_profile:
                if wlan.passphrase_present or wlan.psk_hexkey_present or "psk" in opmode_lower:
                    mode = "mac_auth_psk"
                else:
                    mode = "mac_auth_only"
                ambiguous = False
            else:
                # A resolved aaa_profile with neither a dot1x nor a MAC-auth
                # *profile* mapping can still carry external-authentication
                # intent via a server-group reference (dot1x/MAC server group
                # or an accounting/auth server group) with no corresponding
                # auth-profile mapping to verify how it is applied. That is a
                # genuinely ambiguous/unverifiable source pattern -- distinct
                # from a true role-only aaa_profile (e.g. `initial-role` only)
                # -- and must not fall through to the opmode/passphrase
                # heuristics below.
                server_group_fields = {
                    "dot1x_server_group": profile.dot1x_server_group,
                    "mac_server_group": profile.mac_server_group,
                    "accounting_server_group": profile.accounting_server_group,
                }
                configured_server_groups = sorted(
                    name for name, value in server_group_fields.items() if value
                )
                if configured_server_groups:
                    warnings.append(
                        f"wlan:{wlan.profile_name}: aaa_profile {aaa_profile_name!r} "
                        f"configures {', '.join(configured_server_groups)} without an "
                        "explicit dot1x_auth_profile/mac_auth_profile mapping; this "
                        "indicates external server-group authentication intent that "
                        "cannot be safely mapped from opmode/passphrase alone, so "
                        "security intent is reported as unknown rather than guessed."
                    )
                    aaa_blocks_classification = True
                else:
                    # Role-only aaa_profile: it resolves in the export but
                    # sets neither a dot1x/MAC-auth profile nor any
                    # server-group reference, so it carries no explicit
                    # authentication intent of its own (it is typically only
                    # used for post-auth role assignment). It must not block
                    # the opmode/passphrase classification below.
                    evidence.append(
                        f"aaa_profile:{aaa_profile_name} is role-only (no dot1x or "
                        "mac-auth profile); falling through to opmode classification"
                    )

    if mode == "unknown" and not aaa_blocks_classification:
        if opmode_lower in _OPEN_OPMODES:
            mode = "open"
            ambiguous = False
        elif wlan.wpa3_transition:
            mode = "wpa3_transition_personal"
            ambiguous = False
            evidence.append("wpa3_transition flag present")
        elif "enhanced" in opmode_lower and "open" in opmode_lower:
            mode = "enhanced_open"
            ambiguous = False
        elif "wpa3" in opmode_lower:
            if "psk" in opmode_lower or wlan.passphrase_present or wlan.psk_hexkey_present:
                mode = "wpa3_sae"
                ambiguous = False
        elif "wpa2" in opmode_lower and (
            "psk" in opmode_lower or wlan.passphrase_present or wlan.psk_hexkey_present
        ):
            # Require the opmode to explicitly say "wpa2" before classifying
            # personal/PSK intent. Legacy WPA1/TKIP modes (e.g. "wpa-psk-tkip",
            # "wpa-tkip") or any other opmode that merely happens to carry a
            # passphrase/PSK key are not verified WPA2 evidence and must fall
            # through to the "unknown" warning below rather than being guessed.
            mode = "wpa2_personal"
            ambiguous = False
            evidence.append(f"opmode:{opmode_raw} matched verified wpa2-personal pattern")

    if ambiguous and not warnings:
        warnings.append(
            f"wlan:{wlan.profile_name}: source opmode {opmode_raw!r} does not match a "
            "verified AOS8 security pattern in this repository; security intent is "
            "reported as unknown rather than guessed."
        )

    assert mode in _WLAN_SECURITY_MODES
    security = {
        "mode": mode,
        "opmode": opmode_raw,
        "ambiguous": ambiguous,
        "aaa_profile": aaa_profile_name,
        "dot1x_auth_profile": dot1x_auth_profile,
        "mac_auth_profile": mac_auth_profile,
        "passphrase_present": wlan.passphrase_present,
        "psk_hexkey_present": wlan.psk_hexkey_present,
        "wpa3_transition": wlan.wpa3_transition,
        "evidence": sorted(evidence),
    }
    return security, warnings


def _wlan_payload(
    wlan: AOS8Wlan,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    warnings: list[str] = []
    payload = {
        "name": wlan.profile_name,
        "essid": wlan.essid or wlan.profile_name,
        "vlan": wlan.vlan,
        "aaa_profile": wlan.aaa_profile,
        "virtual_ap_profile": wlan.virtual_ap_profile,
    }
    unsupported: dict[str, Any] = {}
    if wlan.opmode is not None:
        unsupported["ssid_profile.opmode"] = wlan.opmode
        warnings.append(f"wlan:{wlan.profile_name}: {UNSUPPORTED_FIELDS['wlan']['opmode']}")
    if wlan.forward_mode is not None:
        unsupported["virtual_ap.forward_mode"] = wlan.forward_mode
        warnings.append(
            f"wlan:{wlan.profile_name}: {UNSUPPORTED_FIELDS['wlan']['forward_mode']}"
        )
    ssid_raw = wlan.raw.get("ssid_profile", {})
    vap_raw = wlan.raw.get("virtual_ap", {})
    if isinstance(ssid_raw, dict):
        unsupported.update(
            {
                f"ssid_profile.{key}": value
                for key, value in _remaining(
                    ssid_raw,
                    {"profile-name", "name", "essid", "ESSID", "opmode"},
                ).items()
            }
        )
    if isinstance(vap_raw, dict):
        unsupported.update(
            {
                f"virtual_ap.{key}": value
                for key, value in _remaining(
                    vap_raw,
                    {
                        "profile-name",
                        "name",
                        "ssid-profile",
                        "ssid_prof",
                        "ssid-prof",
                        "vlan",
                        "aaa-profile",
                        "aaa_prof",
                        "forward-mode",
                        "forward_mode",
                    },
                ).items()
            }
        )
    return payload, warnings, unsupported


def _role_payload(
    role: AOS8Role,
    *,
    new_central: bool,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    payload: dict[str, Any] = {"name": role.rolename, "vlan": role.vlan}
    if role.acl is not None:
        payload["policies" if new_central else "acl"] = role.acl
    warnings: list[str] = []
    unsupported = _remaining(
        role.raw,
        {
            "rolename",
            "role",
            "name",
            "profile-name",
            "vlan",
            "acl",
            "access-list",
            "captive-portal-profile",
        },
    )
    if role.captive_portal_profile is not None:
        unsupported["captive-portal-profile"] = role.captive_portal_profile
        warnings.append(
            f"role:{role.rolename}: {UNSUPPORTED_FIELDS['role']['captive_portal_profile']}"
        )
    return payload, warnings, unsupported


def _policy_payload(
    policy: AOS8Policy,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    unsupported = _remaining(
        policy.raw,
        {
            "accname",
            "name",
            "profile-name",
            "rule",
            "rules",
            "acl_sess__v4policy",
            "acl_sess__v6policy",
        },
    )
    warnings: list[str] = []
    for family_rules in (policy.ipv4_rules, policy.ipv6_rules):
        for index, rule in enumerate(family_rules):
            rules.append(
                {
                    "address_family": rule.address_family,
                    "source": rule.source,
                    "destination": rule.destination,
                    "service": rule.service,
                    "action": rule.action,
                    "log": rule.log,
                }
            )
            for key, value in rule.unsupported_fields.items():
                field = f"{rule.address_family}_rules[{index}].{key}"
                unsupported[field] = value
                warnings.append(
                    f"policy:{policy.name}: {field}: "
                    f"{UNSUPPORTED_FIELDS['policy']['unsupported_rule_field']}"
                )
    return (
        {"name": policy.name, "rule_count": policy.rule_count, "rules": rules},
        warnings,
        unsupported,
    )


def _network_destination_payload(
    destination: AOS8NetworkDestination,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    prefix = "netdst" if destination.address_family == "ipv4" else "netdst6"
    payload = {
        "address_family": destination.address_family,
        "name": destination.name,
        "description": destination.description,
        "host": destination.host,
        "network": destination.network,
        "range": destination.range,
        "invert": destination.invert,
    }
    unsupported = _remaining(
        destination.raw,
        {
            "dstname",
            f"{prefix}__name",
            f"{prefix}__desc",
            f"{prefix}__host",
            f"{prefix}__network",
            f"{prefix}__range",
            f"{prefix}__invert",
            "name",
            "description",
        },
    )
    warnings: list[str] = []
    if destination.invert:
        identifier = f"{destination.address_family}:{destination.name}"
        warnings.append(
            f"network_destination:{identifier}: "
            f"{UNSUPPORTED_FIELDS['network_destination']['invert']}"
        )
    return payload, warnings, unsupported


def _ethernet_acl_payload(
    acl: AOS8EthernetACL,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    unsupported = _remaining(
        acl.raw,
        {"accname", "name", "profile-name", "rule", "rules", "acl_eth__policy"},
    )
    warnings: list[str] = []
    for index, rule in enumerate(acl.rules):
        rules.append(
            {
                "source": rule.source,
                "destination": rule.destination,
                "ethertype": rule.ethertype,
                "vlan": rule.vlan,
                "action": rule.action,
                "log": rule.log,
            }
        )
        for key, value in rule.unsupported_fields.items():
            field = f"rules[{index}].{key}"
            unsupported[field] = value
            warnings.append(
                f"ethernet_acl:{acl.name}: {field}: "
                f"{UNSUPPORTED_FIELDS['ethernet_acl']['unsupported_rule_field']}"
            )
    return (
        {"name": acl.name, "rule_count": acl.rule_count, "rules": rules},
        warnings,
        unsupported,
    )


def _whitelist_rule_identifier(rule: AOS8WhitelistRule, index: int) -> str:
    start = rule.start_ip or f"unknown-{index}"
    end = rule.end_ip or "unknown"
    return f"{start}-{end}"


def _whitelist_rule_payload(
    rule: AOS8WhitelistRule,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = {"start_ip": rule.start_ip, "end_ip": rule.end_ip}
    unsupported = _remaining(
        rule.raw,
        {"sipaddr", "eipaddr", "start-ip", "start_ip", "end-ip", "end_ip"},
    )
    return payload, unsupported


def _policy_network_destination_dependencies(
    policy: AOS8Policy,
    network_destination_ids: dict[tuple[str, str], str],
) -> list[str]:
    """Resolve policy-rule `destination` values that exactly match a parsed
    `netdst`/`netdst6` alias name into an explicit
    `network_destination:{address_family}:{name}` dependency, type-aware by
    the rule's own IPv4/IPv6 family (same family-keyed lookup pattern as
    `server_ids_by_name` in `build_migration_plan`). A `destination` value
    that is not a string, or does not match a parsed alias, is left alone --
    it may legitimately be `any`, a role name, or a literal address, none of
    which this function guesses at.
    """
    dependencies: set[str] = set()
    for family_rules, family in (
        (policy.ipv4_rules, "ipv4"),
        (policy.ipv6_rules, "ipv6"),
    ):
        for rule in family_rules:
            destination = rule.destination
            if not isinstance(destination, str):
                continue
            identifier = network_destination_ids.get((family, destination))
            if identifier:
                dependencies.add(_dependency("network_destination", identifier))
    return _dependencies(*dependencies)


def _aaa_payload(profile: AOS8AAAProfile) -> dict[str, Any]:
    return {
        "name": profile.profile_name,
        "default_user_role": profile.default_user_role,
        "dot1x_auth_profile": profile.dot1x_auth_profile,
        "dot1x_default_role": profile.dot1x_default_role,
        "dot1x_server_group": profile.dot1x_server_group,
        "mac_auth_profile": profile.mac_auth_profile,
        "mac_default_role": profile.mac_default_role,
        "mac_server_group": profile.mac_server_group,
        "accounting_server_group": profile.accounting_server_group,
    }


def _route_identifier(route: AOS8Route, index: int) -> str:
    destination = route.destination or f"unknown-{index}"
    mask = f"/{route.netmask}" if route.netmask else ""
    next_hop = route.next_hop or "unknown"
    return f"{route.address_family}:{destination}{mask}->{next_hop}"


def _vrrp_identifier(vrrp: AOS8VRRP, index: int) -> str:
    vrid = vrrp.vrid if vrrp.vrid is not None else f"unknown-{index}"
    vlan = vrrp.vlan_id if vrrp.vlan_id is not None else "none"
    return f"{vrrp.address_family}:{vrid}@{vlan}"


def _policy_dependencies(acl: Any) -> list[str]:
    if isinstance(acl, list):
        values = acl
    elif acl in (None, ""):
        values = []
    else:
        values = [acl]
    return _dependencies(*(_dependency("policy", value) for value in values))


def _acl_reference_values(acl: Any) -> list[Any]:
    if isinstance(acl, list):
        return list(acl)
    if acl in (None, ""):
        return []
    return [acl]


def _missing_policy_warnings(
    rolename: str,
    acl: Any,
    policy_names: set[str],
) -> list[str]:
    return [
        (
            f"role:{rolename}: referenced policy {value!r} was not present in the "
            "export; dependency cannot be resolved."
        )
        for value in _acl_reference_values(acl)
        if value not in policy_names
    ]


def _default_verification_plan(config_path: str) -> list[dict[str, Any]]:
    return [
        {
            "tool": "list_overlay_wlans",
            "args": {},
            "purpose": "Confirm migrated WLAN/SSID names and VLANs match the AOS8 export.",
        },
        {
            "tool": "list_roles",
            "args": {},
            "purpose": "Confirm migrated user roles and VLAN assignments match the AOS8 export.",
        },
        {
            "tool": "list_named_vlans",
            "args": {},
            "purpose": "Confirm every AOS8 VLAN ID exists on the target account.",
        },
        {
            "tool": "list_devices",
            "args": {},
            "purpose": (
                f"Confirm AP/controller inventory previously under AOS8 "
                f"config_path {config_path!r} appears healthy on the target account."
            ),
        },
    ]


def build_migration_plan(export: dict[str, Any]) -> dict[str, Any]:
    """Turn an `aos8_export_all()` export into stable, dependency-ordered IR."""
    parsed, parse_warnings = parse_export_report(export)
    config_path = export.get("config_path", "/md") if isinstance(export, dict) else "/md"
    if not isinstance(config_path, str):
        parse_warnings.append("export: config_path is malformed; using '/md'.")
        config_path = "/md"

    classic: list[dict[str, Any]] = []
    new: list[dict[str, Any]] = []
    warnings = list(parse_warnings)
    if isinstance(export, dict):
        source_warnings = export.get("warnings", [])
        if isinstance(source_warnings, list):
            warnings.extend(f"export: {warning}" for warning in source_warnings if warning)
        elif source_warnings:
            warnings.append("export: warnings field was malformed and could not be parsed.")
    diff: dict[str, Any] = {}

    # Keyed by server name -> {server_type: candidate identifier}. AOS8 keeps
    # RADIUS/LDAP/TACACS servers in separate sections
    # (`radius_servers`/`ldap_servers`/`tacacs_servers`), so the same literal
    # name can legitimately exist for more than one server *type*. Keying by
    # type as well as name lets server-group dependency resolution stay
    # type-aware instead of guessing from name alone (see §3 item 4 of
    # docs/aos8-migration-contract-matrix.md).
    server_ids_by_name: dict[str, dict[str, str]] = {}
    for server in parsed["auth_servers"]:
        assert isinstance(server, AOS8AuthServer)
        identifier = f"{server.server_type}:{server.name}"
        payload = {
            "name": server.name,
            "server_type": server.server_type,
            "host": server.host,
        }
        warnings.extend(
            _append_for_both(
                classic,
                new,
                "auth_server",
                identifier,
                payload,
                unsupported_fields=server.settings,
            )
        )
        server_ids_by_name.setdefault(server.name, {})[server.server_type] = (
            f"auth_server:{identifier}"
        )
        diff[f"auth_server:{identifier}"] = _diff_entry(server.to_dict(), payload)

    for vlan in parsed["vlans"]:
        assert isinstance(vlan, AOS8Vlan)
        identifier = str(vlan.vlan_id)
        payload = {"vlan_id": vlan.vlan_id, "description": vlan.description}
        unsupported = _remaining(
            vlan.raw,
            {"id", "vlan-id", "vlan_id", "name", "description"},
        )
        warnings.extend(
            _append_for_both(
                classic,
                new,
                "vlan",
                identifier,
                payload,
                unsupported_fields=unsupported,
            )
        )
        diff[f"vlan:{identifier}"] = _diff_entry(vlan.to_dict(), payload)

    network_destination_ids: dict[tuple[str, str], str] = {}
    for destination in parsed["network_destinations"]:
        assert isinstance(destination, AOS8NetworkDestination)
        identifier = f"{destination.address_family}:{destination.name}"
        payload, local_warnings, unsupported = _network_destination_payload(destination)
        local_warnings = [
            *local_warnings,
            _REFERENCE_ONLY_WARNING.format(
                object_type="network_destination", identifier=identifier
            ),
        ]
        warnings.extend(
            _append_for_both(
                classic,
                new,
                "network_destination",
                identifier,
                payload,
                warnings=local_warnings,
                unsupported_fields=unsupported,
            )
        )
        network_destination_ids[(destination.address_family, destination.name)] = identifier
        diff[f"network_destination:{identifier}"] = _diff_entry(
            destination.to_dict(), payload
        )

    for index, rule in enumerate(parsed["whitelist_rules"]):
        assert isinstance(rule, AOS8WhitelistRule)
        identifier = _whitelist_rule_identifier(rule, index)
        payload, unsupported = _whitelist_rule_payload(rule)
        local_warnings = [
            _REFERENCE_ONLY_WARNING.format(
                object_type="whitelist_rule", identifier=identifier
            )
        ]
        warnings.extend(
            _append_for_both(
                classic,
                new,
                "whitelist_rule",
                identifier,
                payload,
                warnings=local_warnings,
                unsupported_fields=unsupported,
            )
        )
        diff[f"whitelist_rule:{identifier}"] = _diff_entry(rule.to_dict(), payload)

    for profile in parsed["dot1x_auth_profiles"] + parsed["mac_auth_profiles"]:
        assert isinstance(profile, AOS8AuthProfile)
        object_type = f"{profile.auth_type}_auth_profile"
        payload = {"name": profile.profile_name, "auth_type": profile.auth_type}
        warnings.extend(
            _append_for_both(
                classic,
                new,
                object_type,
                profile.profile_name,
                payload,
                unsupported_fields=profile.settings,
            )
        )
        diff[f"{object_type}:{profile.profile_name}"] = _diff_entry(
            profile.to_dict(), payload
        )

    for index, profile in enumerate(parsed["wired_auth_profiles"]):
        assert isinstance(profile, AOS8WiredAuthProfile)
        identifier = "global" if index == 0 else f"global-{index}"
        payload = {
            "aaa_profile": profile.aaa_profile,
            "blacklist_time": profile.blacklist_time,
        }
        dependencies = _dependencies(_dependency("aaa_profile", profile.aaa_profile))
        local_warnings = [
            _REFERENCE_ONLY_WARNING.format(
                object_type="wired_auth_profile", identifier=identifier
            )
        ]
        warnings.extend(
            _append_for_both(
                classic,
                new,
                "wired_auth_profile",
                identifier,
                payload,
                warnings=local_warnings,
                dependencies=dependencies,
                unsupported_fields=profile.settings,
            )
        )
        diff[f"wired_auth_profile:{identifier}"] = _diff_entry(profile.to_dict(), payload)

    for index, profile in enumerate(parsed["stateful_dot1x_auth_profiles"]):
        assert isinstance(profile, AOS8StatefulDot1xAuthProfile)
        identifier = "global" if index == 0 else f"global-{index}"
        payload = {
            "mode": profile.mode,
            "server_group": profile.server_group,
            "default_role": profile.default_role,
            "timeout": profile.timeout,
        }
        dependencies = _dependencies(
            _dependency("server_group", profile.server_group),
            _dependency("role", profile.default_role),
        )
        local_warnings = [
            _REFERENCE_ONLY_WARNING.format(
                object_type="stateful_dot1x_auth_profile", identifier=identifier
            )
        ]
        warnings.extend(
            _append_for_both(
                classic,
                new,
                "stateful_dot1x_auth_profile",
                identifier,
                payload,
                warnings=local_warnings,
                dependencies=dependencies,
                unsupported_fields=profile.settings,
            )
        )
        diff[f"stateful_dot1x_auth_profile:{identifier}"] = _diff_entry(
            profile.to_dict(), payload
        )

    for profile in parsed["wispr_auth_profiles"]:
        assert isinstance(profile, AOS8WisprAuthProfile)
        identifier = profile.profile_name
        payload = {
            "name": profile.profile_name,
            "default_role": profile.default_role,
            "server_group": profile.server_group,
        }
        dependencies = _dependencies(
            _dependency("role", profile.default_role),
            _dependency("server_group", profile.server_group),
        )
        local_warnings = [
            _REFERENCE_ONLY_WARNING.format(
                object_type="wispr_auth_profile", identifier=identifier
            )
        ]
        warnings.extend(
            _append_for_both(
                classic,
                new,
                "wispr_auth_profile",
                identifier,
                payload,
                warnings=local_warnings,
                dependencies=dependencies,
                unsupported_fields=profile.settings,
            )
        )
        diff[f"wispr_auth_profile:{identifier}"] = _diff_entry(profile.to_dict(), payload)

    for profile in parsed["cp_auth_profiles"]:
        assert isinstance(profile, AOS8CaptivePortalAuthProfile)
        identifier = profile.profile_name
        payload = {
            "name": profile.profile_name,
            "default_role": profile.default_role,
            "default_guest_role": profile.default_guest_role,
            "server_group": profile.server_group,
        }
        dependencies = _dependencies(
            _dependency("role", profile.default_role),
            _dependency("role", profile.default_guest_role),
            _dependency("server_group", profile.server_group),
        )
        local_warnings = [
            _REFERENCE_ONLY_WARNING.format(
                object_type="cp_auth_profile", identifier=identifier
            )
        ]
        warnings.extend(
            _append_for_both(
                classic,
                new,
                "cp_auth_profile",
                identifier,
                payload,
                warnings=local_warnings,
                dependencies=dependencies,
                unsupported_fields=profile.settings,
            )
        )
        diff[f"cp_auth_profile:{identifier}"] = _diff_entry(profile.to_dict(), payload)

    for profile in parsed["krb_auth_profiles"]:
        assert isinstance(profile, AOS8KerberosAuthProfile)
        identifier = profile.profile_name
        payload = {
            "name": profile.profile_name,
            "default_role": profile.default_role,
            "server_group": profile.server_group,
            "timeout": profile.timeout,
        }
        dependencies = _dependencies(
            _dependency("role", profile.default_role),
            _dependency("server_group", profile.server_group),
        )
        local_warnings = [
            _REFERENCE_ONLY_WARNING.format(
                object_type="krb_auth_profile", identifier=identifier
            )
        ]
        warnings.extend(
            _append_for_both(
                classic,
                new,
                "krb_auth_profile",
                identifier,
                payload,
                warnings=local_warnings,
                dependencies=dependencies,
                unsupported_fields=profile.settings,
            )
        )
        diff[f"krb_auth_profile:{identifier}"] = _diff_entry(profile.to_dict(), payload)

    for profile in parsed["ntlm_auth_profiles"]:
        assert isinstance(profile, AOS8NTLMAuthProfile)
        identifier = profile.profile_name
        payload = {
            "name": profile.profile_name,
            "default_role": profile.default_role,
            "server_group": profile.server_group,
            "enabled": profile.enabled,
            "timeout": profile.timeout,
        }
        dependencies = _dependencies(
            _dependency("role", profile.default_role),
            _dependency("server_group", profile.server_group),
        )
        local_warnings = [
            _REFERENCE_ONLY_WARNING.format(
                object_type="ntlm_auth_profile", identifier=identifier
            )
        ]
        warnings.extend(
            _append_for_both(
                classic,
                new,
                "ntlm_auth_profile",
                identifier,
                payload,
                warnings=local_warnings,
                dependencies=dependencies,
                unsupported_fields=profile.settings,
            )
        )
        diff[f"ntlm_auth_profile:{identifier}"] = _diff_entry(profile.to_dict(), payload)

    for group in parsed["server_groups"]:
        assert isinstance(group, AOS8ServerGroup)
        dependencies_set: set[str] = set()
        unresolved: list[str] = []
        collisions: dict[str, list[str]] = {}
        for server_name in group.auth_servers:
            matches = server_ids_by_name.get(server_name, {})
            if not matches:
                unresolved.append(server_name)
            elif len(matches) == 1:
                dependencies_set.update(matches.values())
            else:
                collisions[server_name] = sorted(matches)
        dependencies = sorted(dependencies_set)
        unresolved = sorted(unresolved)
        local_warnings = [
            (
                f"server_group:{group.name}: referenced auth server {name!r} was "
                "not present in the export; dependency cannot be resolved."
            )
            for name in unresolved
        ]
        local_warnings.extend(
            (
                f"server_group:{group.name}: referenced auth server {name!r} matches "
                f"multiple server types ({', '.join(types)}) in this export and cannot "
                "be resolved unambiguously without type information; this dependency "
                "is left unresolved (fail-closed) rather than guessed."
            )
            for name, types in sorted(collisions.items())
        )
        group_unsupported = dict(group.settings)
        if collisions:
            group_unsupported["auth_server_type_collisions"] = collisions
        payload = {
            "name": group.name,
            "auth_servers": sorted(group.auth_servers),
            "auth_server_entries": group.auth_server_entries,
            "fail_through": group.fail_through,
            "load_balance": group.load_balance,
            "derivation_rules": group.derivation_rules,
        }
        warnings.extend(
            _append_for_both(
                classic,
                new,
                "server_group",
                group.name,
                payload,
                warnings=local_warnings,
                dependencies=dependencies,
                unsupported_fields=group_unsupported,
            )
        )
        diff[f"server_group:{group.name}"] = _diff_entry(group.to_dict(), payload)

    for policy in parsed["policies"]:
        assert isinstance(policy, AOS8Policy)
        payload, local_warnings, unsupported = _policy_payload(policy)
        dependencies = _policy_network_destination_dependencies(
            policy, network_destination_ids
        )
        warnings.extend(
            _append_for_both(
                classic,
                new,
                "policy",
                policy.name,
                payload,
                warnings=local_warnings,
                dependencies=dependencies,
                unsupported_fields=unsupported,
            )
        )
        diff[f"policy:{policy.name}"] = _diff_entry(policy.to_dict(), payload)

    for acl in parsed["ethernet_acls"]:
        assert isinstance(acl, AOS8EthernetACL)
        payload, local_warnings, unsupported = _ethernet_acl_payload(acl)
        local_warnings = [
            *local_warnings,
            _REFERENCE_ONLY_WARNING.format(
                object_type="ethernet_acl", identifier=acl.name
            ),
        ]
        warnings.extend(
            _append_for_both(
                classic,
                new,
                "ethernet_acl",
                acl.name,
                payload,
                warnings=local_warnings,
                unsupported_fields=unsupported,
            )
        )
        diff[f"ethernet_acl:{acl.name}"] = _diff_entry(acl.to_dict(), payload)

    policy_names = {
        policy.name for policy in parsed["policies"] if isinstance(policy, AOS8Policy)
    }
    for role in parsed["roles"]:
        assert isinstance(role, AOS8Role)
        dependencies = _dependencies(
            _dependency("vlan", role.vlan), *_policy_dependencies(role.acl)
        )
        policy_warnings = _missing_policy_warnings(role.rolename, role.acl, policy_names)
        classic_payload, classic_warnings, classic_unsupported = _role_payload(
            role, new_central=False
        )
        new_payload, new_warnings, new_unsupported = _role_payload(
            role, new_central=True
        )
        classic_warnings = [*classic_warnings, *policy_warnings]
        new_warnings = [*new_warnings, *policy_warnings]
        classic_candidate = _candidate(
            ClassicCentralCandidate,
            "role",
            role.rolename,
            classic_payload,
            warnings=classic_warnings,
            dependencies=dependencies,
            unsupported_fields=classic_unsupported,
        )
        new_candidate = _candidate(
            NewCentralCandidate,
            "role",
            role.rolename,
            new_payload,
            warnings=new_warnings,
            dependencies=dependencies,
            unsupported_fields=new_unsupported,
        )
        classic.append(classic_candidate)
        new.append(new_candidate)
        warnings.extend(classic_candidate["warnings"])
        warnings.extend(new_candidate["warnings"])
        diff[f"role:{role.rolename}"] = _diff_entry(role.to_dict(), new_payload)

    for profile in parsed["aaa_profiles"]:
        assert isinstance(profile, AOS8AAAProfile)
        payload = _aaa_payload(profile)
        dependencies = _dependencies(
            _dependency("role", profile.default_user_role),
            _dependency("role", profile.dot1x_default_role),
            _dependency("role", profile.mac_default_role),
            _dependency("dot1x_auth_profile", profile.dot1x_auth_profile),
            _dependency("mac_auth_profile", profile.mac_auth_profile),
            _dependency("server_group", profile.dot1x_server_group),
            _dependency("server_group", profile.mac_server_group),
            _dependency("server_group", profile.accounting_server_group),
        )
        warnings.extend(
            _append_for_both(
                classic,
                new,
                "aaa_profile",
                profile.profile_name,
                payload,
                dependencies=dependencies,
                unsupported_fields=profile.settings,
            )
        )
        diff[f"aaa_profile:{profile.profile_name}"] = _diff_entry(
            profile.to_dict(), payload
        )

    aaa_profiles_by_name = {
        profile.profile_name: profile
        for profile in parsed["aaa_profiles"]
        if isinstance(profile, AOS8AAAProfile)
    }
    for wlan in parsed["wlans"]:
        assert isinstance(wlan, AOS8Wlan)
        dependencies = _dependencies(
            _dependency("vlan", wlan.vlan),
            _dependency("aaa_profile", wlan.aaa_profile),
        )
        security, security_warnings = _wlan_security_intent(wlan, aaa_profiles_by_name)
        classic_payload, classic_warnings, classic_unsupported = _wlan_payload(wlan)
        new_payload, new_warnings, new_unsupported = _wlan_payload(wlan)
        classic_payload["security"] = security
        new_payload["security"] = security
        classic_warnings = [*classic_warnings, *security_warnings]
        new_warnings = [*new_warnings, *security_warnings]
        classic_candidate = _candidate(
            ClassicCentralCandidate,
            "wlan",
            wlan.profile_name,
            classic_payload,
            warnings=classic_warnings,
            dependencies=dependencies,
            unsupported_fields=classic_unsupported,
        )
        new_candidate = _candidate(
            NewCentralCandidate,
            "wlan",
            wlan.profile_name,
            new_payload,
            warnings=new_warnings,
            dependencies=dependencies,
            unsupported_fields=new_unsupported,
        )
        if security["mode"] in _WLAN_PASSPHRASE_MODES and not new_candidate[
            "requires_secret_input"
        ]:
            # No raw secret value was present in the source export (only the
            # `passphrase_present`/`psk_hexkey_present` booleans), so
            # `_redact_sensitive_values` never set `requires_secret_input`.
            # The target mapping still requires a transient, caller-supplied
            # passphrase (never recovered from source state), so flag it
            # explicitly here for accurate preview/placeholder-secret flows.
            new_candidate["requires_secret_input"] = True
            new_candidate["secret_fields"] = sorted(
                {*new_candidate["secret_fields"], "payload.security.wpa_passphrase"}
            )
            new_candidate["warnings"] = sorted(
                {
                    *new_candidate["warnings"],
                    (
                        f"wlan:{wlan.profile_name}: {security['mode']} requires a "
                        "caller-supplied transient wpa_passphrase secret at apply "
                        "time; it is never recovered from source state."
                    ),
                }
            )

        classic.append(classic_candidate)
        new.append(new_candidate)
        warnings.extend(classic_candidate["warnings"])
        warnings.extend(new_candidate["warnings"])
        diff[f"wlan:{wlan.profile_name}"] = _diff_entry(wlan.to_dict(), new_payload)

    vap_to_wlan = {
        wlan.virtual_ap_profile: wlan.profile_name
        for wlan in parsed["wlans"]
        if isinstance(wlan, AOS8Wlan) and wlan.virtual_ap_profile
    }
    for group in parsed["ap_groups"]:
        assert isinstance(group, AOS8ApGroup)
        payload = {
            "name": group.profile_name,
            "wlan_profiles": sorted(group.virtual_ap_profiles),
        }
        dependencies = _dependencies(
            *(
                _dependency("wlan", vap_to_wlan.get(vap, vap))
                for vap in group.virtual_ap_profiles
            )
        )
        unresolved_vaps = sorted(
            vap for vap in group.virtual_ap_profiles if vap not in vap_to_wlan
        )
        local_warnings = [
            (
                f"ap_group:{group.profile_name}: virtual AP {vap!r} does not match any "
                "parsed WLAN profile in this export; its WLAN dependency cannot be "
                "resolved and is left unapplied rather than invented."
            )
            for vap in unresolved_vaps
        ]
        unsupported = _remaining(
            group.raw,
            {"profile-name", "name", "virtual-ap", "virtual_ap"},
        )
        warnings.extend(
            _append_for_both(
                classic,
                new,
                "ap_group",
                group.profile_name,
                payload,
                warnings=local_warnings,
                dependencies=dependencies,
                unsupported_fields=unsupported,
            )
        )
        diff[f"ap_group:{group.profile_name}"] = _diff_entry(group.to_dict(), payload)

    for index, route in enumerate(parsed["routes"]):
        assert isinstance(route, AOS8Route)
        identifier = _route_identifier(route, index)
        payload = {
            "address_family": route.address_family,
            "destination": route.destination,
            "netmask": route.netmask,
            "next_hop": route.next_hop,
            "secondary_next_hop": route.secondary_next_hop,
            "vlan_id": route.vlan_id,
            "cost": route.cost,
            "secondary_cost": route.secondary_cost,
            "zero": route.zero,
        }
        dependencies = _dependencies(_dependency("vlan", route.vlan_id))
        warnings.extend(
            _append_for_both(
                classic,
                new,
                "route",
                identifier,
                payload,
                dependencies=dependencies,
                unsupported_fields=route.settings,
            )
        )
        diff[f"route:{identifier}"] = _diff_entry(route.to_dict(), payload)

    for index, vrrp in enumerate(parsed["vrrp"]):
        assert isinstance(vrrp, AOS8VRRP)
        identifier = _vrrp_identifier(vrrp, index)
        payload = {
            "address_family": vrrp.address_family,
            "vrid": vrrp.vrid,
            "virtual_ip": vrrp.virtual_ip,
            "vlan_id": vrrp.vlan_id,
            "priority": vrrp.priority,
            "preempt": vrrp.preempt,
            "shutdown": vrrp.shutdown,
            "advertisement_interval": vrrp.advertisement_interval,
            "hold_time": vrrp.hold_time,
            "description": vrrp.description,
            "authentication": vrrp.authentication,
            "tracking": _sorted_mapping(vrrp.tracking),
        }
        dependencies = _dependencies(_dependency("vlan", vrrp.vlan_id))
        warnings.extend(
            _append_for_both(
                classic,
                new,
                "vrrp",
                identifier,
                payload,
                dependencies=dependencies,
                unsupported_fields=vrrp.settings,
            )
        )
        diff[f"vrrp:{identifier}"] = _diff_entry(vrrp.to_dict(), payload)

    for controller in parsed["controllers"]:
        assert isinstance(controller, AOS8Controller)
        identifier = controller.name or controller.ip_address or "unknown"
        payload = {
            "name": controller.name,
            "ip_address": controller.ip_address,
            "model": controller.model,
            "version": controller.version,
        }
        unsupported = _remaining(
            controller.raw,
            {
                "Name",
                "name",
                "hostname",
                "IP Address",
                "ip_address",
                "Model",
                "model",
                "Version",
                "version",
            },
        )
        classic_candidate = _candidate(
            ClassicCentralCandidate,
            "controller",
            identifier,
            payload,
            unsupported_fields=unsupported,
        )
        classic.append(classic_candidate)
        warnings.extend(classic_candidate["warnings"])
        warnings.append(
            f"controller:{identifier}: AOS8 controllers/Mobility Conductors are not "
            "migrated as New Central objects; onboard replacement gateways/APs individually."
        )
        diff[f"controller:{identifier}"] = _diff_entry(controller.to_dict(), payload)

    def sort_key(candidate: dict[str, Any]) -> tuple[int, str, str]:
        serialized_payload = json.dumps(
            candidate["payload"], sort_keys=True, default=str
        )
        return (
            candidate["apply_order"],
            candidate["object_type"],
            f"{candidate['identifier']}:{serialized_payload}",
        )

    for candidates in (classic, new):
        candidate_keys = {
            f"{candidate['object_type']}:{candidate['identifier']}"
            for candidate in candidates
        }
        for candidate in candidates:
            missing = [
                dependency
                for dependency in candidate["dependencies"]
                if dependency not in candidate_keys
            ]
            for dependency in missing:
                warning = (
                    f"{candidate['object_type']}:{candidate['identifier']}: dependency "
                    f"{dependency!r} is not present in this export; the target adapter "
                    "must resolve it as a built-in/external prerequisite or block apply."
                )
                candidate["warnings"] = sorted(set([*candidate["warnings"], warning]))
                warnings.append(warning)
    classic.sort(key=sort_key)
    new.sort(key=sort_key)
    return {
        "config_path": config_path,
        "candidates": {"classic_central": classic, "new_central": new},
        "warnings": sorted(set(warnings)),
        "diff": dict(sorted(diff.items())),
        "verification_plan": _default_verification_plan(config_path),
        "source_object_counts": {
            key: len(value) for key, value in sorted(parsed.items())
        },
    }
