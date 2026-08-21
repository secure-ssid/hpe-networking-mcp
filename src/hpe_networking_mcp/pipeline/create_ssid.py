"""Underlay SSID builder for HPE Aruba New Central.

Workflow (per the Configuration APIs runbook v2026.0331):
  Step 1 — Discover the org-level global scope-id (reuses s6_configure logic).
  Step 2 — POST /network-config/v1/wlan-ssids/{essid_name}  (create SSID).
  Step 3 — POST /network-config/v1/scope-maps  (map SSID to CAMPUS_AP persona
            at the requested scope — global by default, or a specific device group).

Notes:
  - forward-mode is always FORWARD_MODE_BRIDGE for underlay.
  - A default role with the same name as the ESSID is auto-created by Central.
  - SSID names with spaces must be %20-encoded in the URL path but left as-is
    in the JSON body fields.
  - Deletion does NOT auto-remove the default role — use delete_underlay_ssid()
    which only deletes the wlan-ssid resource (role cleanup is caller's responsibility).
"""

from __future__ import annotations

import logging
import threading
from typing import Any
from urllib.parse import quote

from hpe_networking_mcp.pipeline.scope_ids import normalize_scope_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Deprecated opmode aliases
# ---------------------------------------------------------------------------

# The 0.4.0 CLI/tooling accepted `WPA2_PSK` as the personal-PSK security
# mode. The New Central `wlan.json`
# (`ArubaWlanSecurity_WlanSecurityConfig.opmode`) enum has no `WPA2_PSK`
# member -- the authoritative value is `WPA2_PERSONAL`. `WPA2_PSK` is kept
# here as a deprecated alias so 0.4.0 scripts/CSV files/direct callers that
# still pass it keep working; see docs/aos8-migration-contract-matrix.md §4.
DEPRECATED_OPMODE_ALIASES: dict[str, str] = {
    "WPA2_PSK": "WPA2_PERSONAL",
}

# Aliases already warned about in this process. Bulk SSID builds call
# `_normalize_opmode` once per SSID (and twice per overlay build), so an
# undeduplicated warning turned a single stale CSV column into hundreds of
# identical log lines. The returned `warnings` list on each result still
# reports the normalization every time -- only the *log* is deduplicated.
_warned_opmode_aliases: set[str] = set()
_warned_opmode_lock = threading.Lock()


def reset_opmode_deprecation_warnings() -> None:
    """Clear the process-wide opmode deprecation-warning cache (tests only)."""
    with _warned_opmode_lock:
        _warned_opmode_aliases.clear()


def _normalize_opmode(opmode: str) -> str:
    """Normalize a caller-supplied opmode token to the authoritative
    New Central WLAN security enum value, logging a concise deprecation
    warning the first time a stale alias (currently only `WPA2_PSK`) is
    seen in this process. Unrecognized tokens are returned unchanged --
    normalization only ever touches a token in `DEPRECATED_OPMODE_ALIASES`.
    """
    canonical = DEPRECATED_OPMODE_ALIASES.get(opmode)
    if canonical is None:
        return opmode
    with _warned_opmode_lock:
        first_time = opmode not in _warned_opmode_aliases
        if first_time:
            _warned_opmode_aliases.add(opmode)
    if first_time:
        logger.warning(
            "opmode '%s' is deprecated — use '%s' instead. Normalizing automatically "
            "(this warning is logged once per process).",
            opmode,
            canonical,
        )
    return canonical


# ---------------------------------------------------------------------------
# Default SSID body template (all tunable fields exposed as parameters)
# ---------------------------------------------------------------------------

def _build_ssid_body(
    ssid_name: str,
    vlan_ids: list[str],
    *,
    enabled: bool = True,
    opmode: str = "OPEN",
    rf_band: str = "BAND_ALL",
    hide_ssid: bool = False,
    max_clients: int = 1024,
    wpa3_transition: bool = False,
    wpa_passphrase: str | None = None,
    client_isolation: bool = False,
    dmo_enable: bool = True,
    dmo_channel_threshold: int = 90,
    dmo_clients_threshold: int = 6,
    inactivity_timeout: int = 1000,
    dtim_period: int = 1,
) -> dict[str, Any]:
    """Return the full WLAN SSID POST body for an underlay SSID.

    wpa_passphrase is required when opmode is WPA3_SAE or WPA2_PERSONAL.
    It maps to personal-security.wpa-passphrase in the API body.

    Note: the New Central WLAN security schema (`wlan.json`,
    `ArubaWlanSecurity_WlanSecurityConfig.opmode`) has no `WPA2_PSK` value —
    the correct enum member is `WPA2_PERSONAL`. `WPA2_PSK` is still accepted
    here as a deprecated alias (see `DEPRECATED_OPMODE_ALIASES`/
    `_normalize_opmode`) and is normalized to `WPA2_PERSONAL` — with a
    logged deprecation warning — before either the payload `opmode` field
    or the personal-security branch below is evaluated, so passphrase
    handling is identical to passing `WPA2_PERSONAL` directly. Any other
    unrecognized opmode is left untouched and silently gets an
    open/no-passphrase body, as before.

    `wpa3_transition` defaults to False: every currently supported/verified
    pure security mode (OPEN, WPA2_PERSONAL, WPA3_SAE, ENHANCED_OPEN) must
    never silently inherit a WPA3-transition-mode SSID. Callers building a
    real WPA3-transition SSID must opt in explicitly and are responsible for
    the live-validation caveat documented in
    docs/aos8-migration-contract-matrix.md §6.2.
    """
    opmode = _normalize_opmode(opmode)
    body: dict[str, Any] = {
        "ssid": ssid_name,
        "enable": enabled,
        "forward-mode": "FORWARD_MODE_BRIDGE",
        "dmo": {
            "enable": dmo_enable,
            "channel-utilization-threshold": dmo_channel_threshold,
            "clients-threshold": dmo_clients_threshold,
        },
        "broadcast-filter-ipv4": "BCAST_FILTER_ARP",
        "broadcast-filter-ipv6": "UCAST_FILTER_RA",
        "optimize-mcast-rate": False,
        "ssid-utf8": True,
        "essid": {
            "use-alias": False,
            "name": ssid_name,
        },
        "advertise-apname": False,
        "disable-on-6ghz-mesh": False,
        "dot11k": True,
        "dtim-period": dtim_period,
        "ftm-responder": False,
        "hide-ssid": hide_ssid,
        "auth-req-thresh": 0,
        "explicit-ageout-client": False,
        "inactivity-timeout": inactivity_timeout,
        "local-probe-req-thresh": 0,
        "max-clients-threshold": max_clients,
        "rf-band": rf_band,
        "rrm-quiet-ie": False,
        "high-throughput": {
            "enable": True,
            "very-high-throughput": True,
        },
        "g-legacy-rates": {
            "basic-rates": ["RATE_12MB", "RATE_24MB"],
            "tx-rates": [
                "RATE_12MB", "RATE_18MB", "RATE_24MB", "RATE_36MB", "RATE_48MB", "RATE_54MB"
            ],
        },
        "a-legacy-rates": {
            "basic-rates": ["RATE_12MB", "RATE_24MB"],
            "tx-rates": [
                "RATE_12MB", "RATE_18MB", "RATE_24MB", "RATE_36MB", "RATE_48MB", "RATE_54MB"
            ],
        },
        "high-efficiency": {
            "enable": True,
        },
        "extremely-high-throughput": {
            "enable": True,
            "mlo": False,
            "beacon-protection": False,
        },
        "wmm-cfg": {
            "uapsd": True,
        },
        "advertise-timing": False,
        "opmode": opmode,
        "use-ip-for-calling-station-id": False,
        "called-station-id": {
            "type": "MAC_ADDRESS",
            "include-ssid": False,
        },
        "cloud-auth": False,
        "wpa3-transition-mode-enable": wpa3_transition,
        "denylist": True,
        "max-authentication-failures": 0,
        "enforce-dhcp": False,
        "pan": False,
        "vlan-selector": "VLAN_RANGES",
        "vlan-id-range": vlan_ids,
        "out-of-service": "NONE",
        "client-isolation": client_isolation,
    }
    if wpa_passphrase and opmode in ("WPA3_SAE", "WPA2_PERSONAL"):
        body["personal-security"] = {
            "passphrase-format": "STRING",
            "wpa-passphrase": wpa_passphrase,
        }
    return body


# ---------------------------------------------------------------------------
# Core workflow
# ---------------------------------------------------------------------------

def build_underlay_ssid(
    central_client: Any,
    ssid_name: str,
    vlan_ids: list[str],
    scope_id: str,
    *,
    persona: str = "CAMPUS_AP",
    # Optional overrides
    enabled: bool = True,
    opmode: str = "OPEN",
    rf_band: str = "BAND_ALL",
    hide_ssid: bool = False,
    max_clients: int = 1024,
    wpa3_transition: bool = False,
    wpa_passphrase: str | None = None,
    client_isolation: bool = False,
    dmo_enable: bool = True,
    dmo_channel_threshold: int = 90,
    dmo_clients_threshold: int = 6,
    inactivity_timeout: int = 1000,
    dtim_period: int = 1,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create an underlay SSID and scope-map it to the specified persona.

    Args:
        central_client: CentralClient instance with valid credentials.
        ssid_name:      SSID name as it will be broadcast (spaces allowed).
        vlan_ids:       List of VLAN IDs to assign (e.g. ["1000"]).
        scope_id:       Scope-id to map the SSID to. Pass the global scope-id
                        to apply org-wide, or a device-group scope-id for a
                        narrower scope.
        persona:        Device-function persona for the scope-map. Default CAMPUS_AP.
                        Valid values: CAMPUS_AP, MOBILITY_GW, ACCESS_SWITCH,
                        AGG_SWITCH, CORE_SWITCH.
        dry_run:        If True, log actions but do not call any write APIs.

    Returns:
        Dict with keys: ssid_name, vlan_ids, scope_id, persona, created (bool),
        scope_mapped (bool), errors (list[str]), warnings (list[str]).
        `warnings` carries a deprecation notice when `opmode='WPA2_PSK'` is
        passed (see `DEPRECATED_OPMODE_ALIASES`); the payload opmode is
        still normalized to `WPA2_PERSONAL`.
    """
    canonical_opmode = _normalize_opmode(opmode)
    warnings: list[str] = []
    if canonical_opmode != opmode:
        warnings.append(
            f"opmode '{opmode}' is deprecated — normalized to '{canonical_opmode}'."
        )
    opmode = canonical_opmode

    url_name = quote(ssid_name, safe="")  # %20-encode spaces for URL path
    result: dict[str, Any] = {
        "ssid_name": ssid_name,
        "vlan_ids": vlan_ids,
        "scope_id": scope_id,
        "persona": persona,
        "created": False,
        "scope_mapped": False,
        "errors": [],
        "warnings": warnings,
    }
    try:
        scope_id = normalize_scope_id(scope_id)
        result["scope_id"] = scope_id
    except ValueError as exc:
        result["errors"].append(f"validate_scope_id: {exc}")
        return result

    body = _build_ssid_body(
        ssid_name,
        vlan_ids,
        enabled=enabled,
        opmode=opmode,
        rf_band=rf_band,
        hide_ssid=hide_ssid,
        max_clients=max_clients,
        wpa3_transition=wpa3_transition,
        wpa_passphrase=wpa_passphrase,
        client_isolation=client_isolation,
        dmo_enable=dmo_enable,
        dmo_channel_threshold=dmo_channel_threshold,
        dmo_clients_threshold=dmo_clients_threshold,
        inactivity_timeout=inactivity_timeout,
        dtim_period=dtim_period,
    )

    # ------------------------------------------------------------------
    # Step 2: Create wlan-ssid
    # ------------------------------------------------------------------
    endpoint = f"/network-config/v1/wlan-ssids/{url_name}"

    if dry_run:
        logger.info(
            "[dry-run] Would POST %s with vlan_ids=%s opmode=%s", endpoint, vlan_ids, opmode
        )
        result["created"] = True  # pretend success for reporting
    else:
        try:
            central_client.post(endpoint, data=body)
            result["created"] = True
            logger.info("Created underlay SSID '%s'", ssid_name)
        except Exception as exc:
            resp_text = getattr(getattr(exc, "response", None), "text", "") or str(exc)
            if "duplicate" in resp_text.lower() or "already exists" in resp_text.lower():
                logger.warning(
                    "SSID '%s' already exists — skipping create, continuing to scope-map", ssid_name
                )
                result["created"] = True  # treat as success
            else:
                result["errors"].append(f"create_ssid: {exc}")
                logger.error("Failed to create SSID '%s': %s", ssid_name, exc)
                return result

    # ------------------------------------------------------------------
    # Step 3: Scope-map to persona
    # ------------------------------------------------------------------
    scope_map_body = {
        "scope-map": [
            {
                "scope-name": scope_id,
                "scope-id": int(scope_id),
                "persona": persona,
                "resource": f"wlan-ssids/{ssid_name}",
            }
        ]
    }

    if dry_run:
        logger.info(
            "[dry-run] Would POST /network-config/v1/scope-maps — %s scope=%s "
            "resource=wlan-ssids/%s",
            persona, scope_id, ssid_name,
        )
        result["scope_mapped"] = True
    else:
        try:
            central_client.post("/network-config/v1/scope-maps", data=scope_map_body)
            result["scope_mapped"] = True
            logger.info("Scope-mapped SSID '%s' → %s scope-id=%s", ssid_name, persona, scope_id)
        except Exception as exc:
            resp_text = getattr(getattr(exc, "response", None), "text", "") or str(exc)
            if "already exists" in resp_text.lower():
                logger.warning("Scope-map for '%s' already exists — skipping", ssid_name)
                result["scope_mapped"] = True
            else:
                result["errors"].append(f"scope_map: {exc}")
                logger.error("Failed to scope-map SSID '%s': %s", ssid_name, exc)

    return result


def build_overlay_ssid(
    central_client: Any,
    ssid_name: str,
    vlan_ids: list[str],
    scope_id: str,
    cluster_name: str,
    cluster_scope_id: str,
    *,
    opmode: str = "OPEN",
    rf_band: str = "BAND_ALL",
    wpa_passphrase: str | None = None,
    wpa3_transition: bool = False,
    mac_auth_server_group: str | None = None,
    policy_name: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create an overlay SSID that tunnels client traffic through a Mobility Gateway.

    Args:
        central_client:        CentralClient instance with valid credentials.
        ssid_name:             SSID name to broadcast.
        vlan_ids:              List of VLAN IDs to assign (e.g. ["200"]).
        scope_id:              Device Group scope-id (overlay WLANs cannot use global scope).
        cluster_name:          Name of the gateway cluster to tunnel through.
        cluster_scope_id:      Scope-id of the gateway cluster.
        mac_auth_server_group: If set, creates an AAA profile named after the SSID pointing
                               to this Central NAC server group and attaches MAC auth to the SSID.
        policy_name:           Name of an existing GW security policy to use. If omitted, an
                               allow-all policy named after the SSID is created automatically.
        dry_run:               If True, log actions but do not call any write APIs.

    Returns:
        Dict with keys: ssid_name, vlan_ids, scope_id, cluster_name,
        created (bool), overlay_created (bool), scope_mapped (bool),
        aaa_profile_created (bool), errors (list[str]), warnings (list[str]).
        `warnings` carries a deprecation notice when `opmode='WPA2_PSK'` is
        passed (see `DEPRECATED_OPMODE_ALIASES`); the payload opmode is
        still normalized to `WPA2_PERSONAL`.

        This function always returns that dict — including when `dry_run=True`
        and org-level scope discovery is unavailable, which is reported in
        `warnings` instead of propagating an exception. Outside dry-run the
        same failure is recorded in `errors` and the build stops before any
        write is attempted.
    """
    canonical_opmode = _normalize_opmode(opmode)
    warnings: list[str] = []
    if canonical_opmode != opmode:
        warnings.append(
            f"opmode '{opmode}' is deprecated — normalized to '{canonical_opmode}'."
        )
    opmode = canonical_opmode

    url_name = quote(ssid_name, safe="")
    result: dict[str, Any] = {
        "ssid_name": ssid_name,
        "vlan_ids": vlan_ids,
        "scope_id": scope_id,
        "cluster_name": cluster_name,
        "created": False,
        "overlay_created": False,
        "scope_mapped": False,
        "aaa_profile_created": False,
        "errors": [],
        "warnings": warnings,
    }
    try:
        scope_id = normalize_scope_id(scope_id)
        cluster_scope_id = normalize_scope_id(
            cluster_scope_id,
            field_name="cluster_scope_id",
        )
        result["scope_id"] = scope_id
    except ValueError as exc:
        result["errors"].append(f"validate_scope_id: {exc}")
        return result

    # ------------------------------------------------------------------
    # Step 0: Resolve the org-level global scope-id exactly once
    # ------------------------------------------------------------------
    # It is needed by both the role scope-maps (Step 1) and the policy
    # scope-maps (Step 1c). Three things were wrong before:
    #   1. The lookup ran unconditionally and unguarded, so a tenant where
    #      scope discovery fails made this function *raise* — including under
    #      dry_run, whose documented contract is "never write, always return
    #      the result dict".
    #   2. A second, identical lookup ran later for the policy scope-maps,
    #      doubling the API calls and letting the two halves of one build
    #      disagree if the account changed in between.
    #   3. It ran *after* the role had already been created, so a failure left
    #      a half-built SSID behind. It is now resolved before any write.
    from hpe_networking_mcp.pipeline.stages.s6_configure import _fetch_global_scope_id

    global_scope_id: str | None = None
    try:
        global_scope_id = _fetch_global_scope_id(central_client)
    except Exception as exc:
        message = f"resolve_global_scope: {exc}"
        if dry_run:
            # Preview only — report it and keep going so the caller still gets
            # the full plan (and the result dict) back.
            result["warnings"].append(
                f"{message} — global-scope scope-maps cannot be previewed."
            )
            logger.warning("[dry-run] Could not resolve global scope-id: %s", exc)
        else:
            result["errors"].append(message)
            logger.error(
                "Could not resolve global scope-id — aborting before any write: %s", exc
            )
            return result

    # ------------------------------------------------------------------
    # Step 1: Create allow-all role first (must exist before SSID references it)
    # ------------------------------------------------------------------
    role_body = {
        "name": ssid_name,
        "utf8": True,
    }
    role_endpoint = f"/network-config/v1/roles/{url_name}"
    if dry_run:
        logger.info("[dry-run] Would POST %s (allow-all wireless role)", role_endpoint)
    else:
        try:
            central_client.post(role_endpoint, data=role_body)
            logger.info("Created allow-all wireless role '%s'", ssid_name)
        except Exception as exc:
            resp_text = getattr(getattr(exc, "response", None), "text", "") or str(exc)
            if "duplicate" in resp_text.lower() or "already exists" in resp_text.lower():
                logger.warning("Role '%s' already exists — continuing", ssid_name)
            else:
                result["errors"].append(f"create_role: {exc}")
                logger.error("Failed to create role '%s': %s", ssid_name, exc)

    # Scope-map the role at global scope (CAMPUS_AP + MOBILITY_GW) AND at the
    # device group scope for MOBILITY_GW — gateways only resolve roles scoped
    # to their own device group, not just global.
    # Uses the scope-id resolved once in Step 0 above.
    role_scope_targets = [
        (scope_id, "MOBILITY_GW"),  # device group scope — required for GW role resolution
    ]
    if global_scope_id:
        role_scope_targets = [
            (global_scope_id, "CAMPUS_AP"),
            (global_scope_id, "MOBILITY_GW"),
        ] + role_scope_targets
    for r_scope_id, persona in role_scope_targets:
        for resource in (f"roles/{ssid_name}", f"role-gpids/{ssid_name}"):
            role_scope_map = {
                "scope-map": [
                    {
                        "scope-name": r_scope_id,
                        "scope-id": int(r_scope_id),
                        "persona": persona,
                        "resource": resource,
                    }
                ]
            }
            if dry_run:
                logger.info(
                    "[dry-run] Would scope-map %s → %s scope=%s", resource, persona, r_scope_id
                )
            else:
                try:
                    central_client.post("/network-config/v1/scope-maps", data=role_scope_map)
                    logger.info("Scope-mapped %s → %s scope-id=%s", resource, persona, r_scope_id)
                except Exception as exc:
                    resp_text = getattr(getattr(exc, "response", None), "text", "") or str(exc)
                    if "already exists" in resp_text.lower():
                        logger.warning(
                            "Scope-map for %s (%s) already exists — skipping", resource, persona
                        )
                    else:
                        result["errors"].append(f"scope_map_role ({resource}/{persona}): {exc}")
                        logger.error("Failed to scope-map %s (%s): %s", resource, persona, exc)

    # ------------------------------------------------------------------
    # Step 1b: Create AAA profile for MAC auth (if requested)
    # ------------------------------------------------------------------
    aaa_profile_name = ssid_name  # profile name matches SSID name
    if mac_auth_server_group:
        # Step 1b-i: Create macauth server object (required before AAA profile can reference it)
        macauth_endpoint = f"/network-config/v1alpha1/macauth/{quote(aaa_profile_name, safe='')}"
        macauth_payload = {"name": aaa_profile_name}
        if dry_run:
            logger.info("[dry-run] Would POST %s (macauth server object)", macauth_endpoint)
        else:
            try:
                central_client.post(macauth_endpoint, data=macauth_payload)
                logger.info("Created macauth object '%s'", aaa_profile_name)
            except Exception as exc:
                resp_text = getattr(getattr(exc, "response", None), "text", "") or str(exc)
                if "duplicate" in resp_text.lower() or "already exists" in resp_text.lower():
                    logger.warning(
                        "macauth object '%s' already exists — continuing", aaa_profile_name
                    )
                else:
                    result["errors"].append(f"create_macauth: {exc}")
                    logger.error("Failed to create macauth object '%s': %s", aaa_profile_name, exc)

        # Step 1b-ii: Create AAA profile referencing the macauth object and server group
        aaa_payload = {
            "name": aaa_profile_name,
            "authentication": {
                "mac-auth": aaa_profile_name,
                "mac-default-role": ssid_name,
                "macauth-server-group": mac_auth_server_group,
            },
        }
        aaa_endpoint = f"/network-config/v1alpha1/aaa-profile/{quote(aaa_profile_name, safe='')}"
        if dry_run:
            logger.info(
                "[dry-run] Would POST %s (AAA profile, server-group=%s)",
                aaa_endpoint,
                mac_auth_server_group,
            )
            result["aaa_profile_created"] = True
        else:
            try:
                central_client.post(aaa_endpoint, data=aaa_payload)
                result["aaa_profile_created"] = True
                logger.info(
                    "Created AAA profile '%s' → server-group '%s'",
                    aaa_profile_name,
                    mac_auth_server_group,
                )
            except Exception as exc:
                resp_text = getattr(getattr(exc, "response", None), "text", "") or str(exc)
                if "duplicate" in resp_text.lower() or "already exists" in resp_text.lower():
                    logger.warning("AAA profile '%s' already exists — continuing", aaa_profile_name)
                    result["aaa_profile_created"] = True
                else:
                    result["errors"].append(f"create_aaa_profile: {exc}")
                    logger.error("Failed to create AAA profile '%s': %s", aaa_profile_name, exc)

    # Build WLAN body with overlay-specific overrides
    body = _build_ssid_body(
        ssid_name,
        vlan_ids,
        opmode=opmode,
        rf_band=rf_band,
        wpa_passphrase=wpa_passphrase,
        wpa3_transition=wpa3_transition if opmode != "ENHANCED_OPEN" else False,
        dmo_enable=False,
    )
    body["forward-mode"] = "FORWARD_MODE_L2"
    body["out-of-service"] = "TUNNEL_DOWN"
    body["cluster-preemption"] = False
    body["type"] = "EMPLOYEE"
    body["default-role"] = ssid_name
    if mac_auth_server_group:
        body["mac-authentication"] = True
        body["called-station-id"] = {
            "type": "MAC_ADDRESS",
            "include-ssid": True,
        }
        body["auth-server-group"] = mac_auth_server_group
        body["acct-server-group"] = mac_auth_server_group
        body["cloud-auth"] = True
        body["radius-accounting"] = True
        body["radius-interim-accounting-interval"] = 10
        body["denylist"] = False

    # ------------------------------------------------------------------
    # Step 1c: Create allow-all security policy + add to policy group + scope-map
    # (Required so the role shows "Referenced By 1 policy" and GW can enforce it)
    # If policy_name is provided, skip creation and use the existing policy instead.
    # ------------------------------------------------------------------
    effective_policy = policy_name or ssid_name
    policy_endpoint = f"/network-config/v1alpha1/policies/{quote(effective_policy, safe='')}"
    policy_payload = {
        "name": ssid_name,
        "type": "POLICY_TYPE_SECURITY",
        "security-policy": {
            "type": "SECURITY_POLICY_TYPE_DEFAULT",
            "policy-rule": [
                {
                    "position": 1,
                    "description": "Allow All",
                    "condition": {
                        "type": "CONDITION_DEFAULT",
                        "rule-type": "RULE_ANY",
                        "source": {"type": "ADDRESS_ROLE", "role": ssid_name},
                        "destination": {"type": "ADDRESS_ANY"},
                    },
                    "action": {"type": "ACTION_ALLOW"},
                }
            ],
        },
    }
    if dry_run:
        if not policy_name:
            logger.info("[dry-run] Would POST %s (allow-all security policy)", policy_endpoint)
            logger.info("[dry-run] Would PATCH policy-groups to add '%s'", effective_policy)
        else:
            logger.info("[dry-run] Using existing policy '%s' (skipping creation)", policy_name)
        logger.info(
            "[dry-run] Would scope-map policies/%s → CAMPUS_AP + MOBILITY_GW", effective_policy
        )
    else:
        if not policy_name:
            # Create a new allow-all policy named after the SSID
            try:
                central_client.post(policy_endpoint, data=policy_payload)
                logger.info("Created allow-all policy '%s'", effective_policy)
            except Exception as exc:
                resp_text = getattr(getattr(exc, "response", None), "text", "") or str(exc)
                if "duplicate" in resp_text.lower() or "already exists" in resp_text.lower():
                    logger.warning("Policy '%s' already exists — continuing", effective_policy)
                else:
                    result["errors"].append(f"create_policy: {exc}")
                    logger.error("Failed to create policy '%s': %s", effective_policy, exc)

            try:
                # Use the validated `.patch()` wrapper (raises via
                # `response.raise_for_status()` on any non-2xx) rather than
                # the raw `._request()` primitive -- a raw `_request` call
                # returns the httpx.Response unchecked, so a non-2xx would
                # be silently logged as success below instead of failing
                # the migration step.
                central_client.patch(
                    "/network-config/v1alpha1/policy-groups",
                    data={
                        "policy-group": {
                            "policy-group-list": [
                                {"name": effective_policy, "position": 3}
                            ]
                        }
                    },
                )
                logger.info("Added '%s' to policy group", effective_policy)
            except Exception as exc:
                result["errors"].append(f"add_policy_group: {exc}")
                logger.error("Failed to add '%s' to policy group: %s", effective_policy, exc)
        else:
            logger.info("Using existing policy '%s' — skipping creation", policy_name)

        # Reuse the scope-id resolved once at the top of this function rather
        # than issuing a second, late lookup.
        global_scope_id_pol = global_scope_id
        for persona in ("CAMPUS_AP", "MOBILITY_GW") if global_scope_id_pol else ():
            pol_scope_map = {
                "scope-map": [{
                    "scope-name": global_scope_id_pol,
                    "scope-id": int(global_scope_id_pol),
                    "persona": persona,
                    "resource": f"policies/{effective_policy}",
                }]
            }
            try:
                central_client.post("/network-config/v1/scope-maps", data=pol_scope_map)
                logger.info("Scope-mapped policies/%s → %s global", effective_policy, persona)
            except Exception as exc:
                resp_text = getattr(getattr(exc, "response", None), "text", "") or str(exc)
                if "already exists" in resp_text.lower():
                    logger.warning(
                        "Scope-map for policies/%s (%s) already exists", effective_policy, persona
                    )
                else:
                    result["errors"].append(f"scope_map_policy ({persona}): {exc}")
                    logger.error(
                        "Failed to scope-map policy '%s' (%s): %s", effective_policy, persona, exc
                    )

    # ------------------------------------------------------------------
    # Step 2: Create wlan-ssid
    # ------------------------------------------------------------------
    endpoint = f"/network-config/v1/wlan-ssids/{url_name}"
    if dry_run:
        logger.info("[dry-run] Would POST %s (overlay, FORWARD_MODE_L2)", endpoint)
        result["created"] = True
    else:
        try:
            central_client.post(endpoint, data=body)
            result["created"] = True
            logger.info("Created overlay SSID '%s'", ssid_name)
        except Exception as exc:
            resp_text = getattr(getattr(exc, "response", None), "text", "") or str(exc)
            if "duplicate" in resp_text.lower() or "already exists" in resp_text.lower():
                logger.warning("SSID '%s' already exists — continuing", ssid_name)
                result["created"] = True
            else:
                result["errors"].append(f"create_ssid: {exc}")
                logger.error("Failed to create overlay SSID '%s': %s", ssid_name, exc)
                return result

    # The API silently drops default-role on POST — patch it in after creation
    if not dry_run and result["created"]:
        try:
            central_client.patch(endpoint, data={"default-role": ssid_name})
            logger.info("Patched default-role='%s' on SSID '%s'", ssid_name, ssid_name)
        except Exception as exc:
            result["errors"].append(f"patch_default_role: {exc}")
            logger.error("Failed to patch default-role on SSID '%s': %s", ssid_name, exc)

    # Keep MAC-auth SSIDs in a known-good state to avoid sticky denylist rejects
    # and missing SSID context in Called-Station-ID during NAC authorization.
    if not dry_run and result["created"] and mac_auth_server_group:
        try:
            central_client.patch(
                endpoint,
                data={
                    "called-station-id": {"type": "MAC_ADDRESS", "include-ssid": True},
                    "denylist": False,
                },
            )
            logger.info(
                "Patched MAC-auth defaults on SSID '%s' (include-ssid=true, denylist=false)",
                ssid_name,
            )
        except Exception as exc:
            result["errors"].append(f"patch_macauth_defaults: {exc}")
            logger.error("Failed to patch MAC-auth defaults on SSID '%s': %s", ssid_name, exc)

    # Central binds overlay SSIDs to an auto-generated gw-profile name. Normalize
    # that active profile instead of leaving a stale manual AAA profile reference.
    if not dry_run and result["created"] and mac_auth_server_group:
        try:
            ssid_obj = central_client.get(endpoint)
            gw_profile = str(ssid_obj.get("gw-profile", "")).strip()
            if gw_profile:
                result["gw_profile"] = gw_profile
                gw_profile_encoded = quote(gw_profile, safe="")
                gw_aaa_endpoint = f"/network-config/v1alpha1/aaa-profile/{gw_profile_encoded}"
                aaa_obj = central_client.get(gw_aaa_endpoint)
                auth = aaa_obj.setdefault("authentication", {})
                auth["mac-auth"] = gw_profile
                auth["mac-default-role"] = ssid_name
                auth["macauth-server-group"] = mac_auth_server_group
                if "auth-precedence" not in auth:
                    auth["auth-precedence"] = ["MAC_AUTH", "DOT1X"]
                central_client.put(gw_aaa_endpoint, data=aaa_obj)
                logger.info(
                    "Normalized active GW AAA profile '%s' for SSID '%s'", gw_profile, ssid_name
                )

                # Clean up helper profile created by this function when Central chose
                # a different active profile name.
                if aaa_profile_name != gw_profile:
                    helper_ep = (
                        f"/network-config/v1alpha1/aaa-profile/"
                        f"{quote(aaa_profile_name, safe='')}"
                    )
                    try:
                        central_client.delete(helper_ep)
                        logger.info(
                            "Deleted helper AAA profile '%s' (active profile is '%s')",
                            aaa_profile_name,
                            gw_profile,
                        )
                    except Exception as exc:
                        resp_text = getattr(getattr(exc, "response", None), "text", "") or str(exc)
                        if "404" in resp_text or "not found" in resp_text.lower():
                            logger.info("Helper AAA profile '%s' already absent", aaa_profile_name)
                        else:
                            result["errors"].append(f"cleanup_helper_aaa: {exc}")
                            logger.error(
                                "Failed to delete helper AAA profile '%s': %s",
                                aaa_profile_name,
                                exc,
                            )
            else:
                result["errors"].append(
                    "normalize_gw_profile: missing gw-profile on created overlay SSID"
                )
        except Exception as exc:
            result["errors"].append(f"normalize_gw_profile: {exc}")
            logger.error("Failed to normalize active GW AAA profile for '%s': %s", ssid_name, exc)

    # ------------------------------------------------------------------
    # Step 4: Create overlay-wlan profile
    # ------------------------------------------------------------------
    overlay_body = {
        "profile": ssid_name,
        "overlay-profile-type": "WIRELESS_PROFILE",
        "essid-name": ssid_name,
        "gw-cluster-list": [
            {
                "cluster-redundancy-type": "PRIMARY",
                "cluster": cluster_name,
                "cluster-scope-id": cluster_scope_id,
                "cluster-type": "CLUSTER_ID",
                "tunnel-type": "GRE",
            }
        ],
    }
    overlay_endpoint = f"/network-config/v1/overlay-wlan/{url_name}"
    if dry_run:
        logger.info("[dry-run] Would POST %s with cluster=%s", overlay_endpoint, cluster_name)
        result["overlay_created"] = True
    else:
        try:
            central_client.post(overlay_endpoint, data=overlay_body)
            result["overlay_created"] = True
            logger.info("Created overlay-wlan profile '%s'", ssid_name)
        except Exception as exc:
            resp_text = getattr(getattr(exc, "response", None), "text", "") or str(exc)
            if "duplicate" in resp_text.lower() or "already exists" in resp_text.lower():
                logger.warning("overlay-wlan '%s' already exists — continuing", ssid_name)
                result["overlay_created"] = True
            else:
                result["errors"].append(f"create_overlay_wlan: {exc}")
                logger.error("Failed to create overlay-wlan '%s': %s", ssid_name, exc)
                return result

    # ------------------------------------------------------------------
    # Steps 3-4: Scope-map wlan-ssid and overlay-wlan (role already mapped above)
    # ------------------------------------------------------------------
    scope_maps = [
        ("CAMPUS_AP", f"wlan-ssids/{ssid_name}"),
        ("CAMPUS_AP", f"overlay-wlan/{ssid_name}"),
    ]

    all_mapped = True
    for persona, resource in scope_maps:
        scope_map_body = {
            "scope-map": [
                {
                    "scope-name": scope_id,
                    "scope-id": int(scope_id),
                    "persona": persona,
                    "resource": resource,
                }
            ]
        }
        if dry_run:
            logger.info(
                "[dry-run] Would POST scope-maps — %s scope=%s resource=%s",
                persona,
                scope_id,
                resource,
            )
        else:
            try:
                central_client.post("/network-config/v1/scope-maps", data=scope_map_body)
                logger.info("Scope-mapped %s → %s scope-id=%s", resource, persona, scope_id)
            except Exception as exc:
                resp_text = getattr(getattr(exc, "response", None), "text", "") or str(exc)
                if "already exists" in resp_text.lower():
                    logger.warning("Scope-map for '%s' already exists — skipping", resource)
                else:
                    result["errors"].append(f"scope_map ({resource}): {exc}")
                    logger.error("Failed to scope-map %s: %s", resource, exc)
                    all_mapped = False

    result["scope_mapped"] = all_mapped
    return result


def create_allow_all_role(
    central_client: Any,
    role_name: str,
    scope_id: str,
    *,
    persona: str = "CAMPUS_AP",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create a wireless role that allows all traffic and scope-map it to the specified persona.

    The role is created with:
      - No ACL/policy attached (open permit-all behaviour by default in Central)
      - captive-portal: disabled
      - VLAN derived from the SSID's vlan-selector (Central inherits from SSID)

    Returns dict with keys: role_name, created (bool), scope_mapped (bool), errors (list[str]).
    """
    url_name = quote(role_name, safe="")
    result: dict[str, Any] = {
        "role_name": role_name,
        "created": False,
        "scope_mapped": False,
        "errors": [],
    }
    try:
        scope_id = normalize_scope_id(scope_id)
    except ValueError as exc:
        result["errors"].append(f"validate_scope_id: {exc}")
        return result

    role_body = {
        "name": role_name,
        "type": "WIRELESS",
        "captive-portal-profile": "disabled",
    }

    endpoint = f"/network-config/v1/roles/{url_name}"

    if dry_run:
        logger.info("[dry-run] Would POST %s (allow-all wireless role)", endpoint)
        result["created"] = True
    else:
        try:
            central_client.post(endpoint, data=role_body)
            result["created"] = True
            logger.info("Created allow-all wireless role '%s'", role_name)
        except Exception as exc:
            resp_text = getattr(getattr(exc, "response", None), "text", "") or str(exc)
            if "duplicate" in resp_text.lower() or "already exists" in resp_text.lower():
                logger.warning("Role '%s' already exists — continuing to scope-map", role_name)
                result["created"] = True
            else:
                result["errors"].append(f"create_role: {exc}")
                logger.error("Failed to create role '%s': %s", role_name, exc)
                return result

    # Scope-map role to CAMPUS_AP
    scope_map_body = {
        "scope-map": [
            {
                "scope-name": scope_id,
                "scope-id": int(scope_id),
                "persona": persona,
                "resource": f"roles/{role_name}",
            }
        ]
    }

    if dry_run:
        logger.info(
            "[dry-run] Would POST /network-config/v1/scope-maps — %s scope=%s resource=roles/%s",
            persona, scope_id, role_name,
        )
        result["scope_mapped"] = True
    else:
        try:
            central_client.post("/network-config/v1/scope-maps", data=scope_map_body)
            result["scope_mapped"] = True
            logger.info("Scope-mapped role '%s' → %s scope-id=%s", role_name, persona, scope_id)
        except Exception as exc:
            resp_text = getattr(getattr(exc, "response", None), "text", "") or str(exc)
            if "already exists" in resp_text.lower():
                logger.warning("Scope-map for role '%s' already exists — skipping", role_name)
                result["scope_mapped"] = True
            else:
                result["errors"].append(f"scope_map_role: {exc}")
                logger.error("Failed to scope-map role '%s': %s", role_name, exc)

    return result


def delete_underlay_ssid(
    central_client: Any,
    ssid_name: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete an underlay SSID.

    NOTE: Deletion does NOT remove the auto-created default role with the same
    name. The caller must delete the role separately if desired.

    Returns dict with keys: ssid_name, deleted (bool), errors (list[str]).
    """
    url_name = quote(ssid_name, safe="")
    result: dict[str, Any] = {
        "ssid_name": ssid_name,
        "deleted": False,
        "errors": [],
    }

    endpoint = f"/network-config/v1/wlan-ssids/{url_name}"

    if dry_run:
        logger.info("[dry-run] Would DELETE %s", endpoint)
        result["deleted"] = True
        return result

    try:
        central_client.delete(endpoint)
        result["deleted"] = True
        logger.info("Deleted underlay SSID '%s'", ssid_name)
    except Exception as exc:
        result["errors"].append(f"delete_ssid: {exc}")
        logger.error("Failed to delete SSID '%s': %s", ssid_name, exc)

    return result


def get_underlay_ssid(
    central_client: Any,
    ssid_name: str,
) -> dict[str, Any] | None:
    """Fetch an existing underlay SSID configuration, or None if not found."""
    url_name = quote(ssid_name, safe="")
    try:
        return central_client.get(f"/network-config/v1/wlan-ssids/{url_name}")
    except Exception as exc:
        resp_status = getattr(getattr(exc, "response", None), "status_code", None)
        if resp_status == 404:
            return None
        logger.warning("get_underlay_ssid('%s') failed: %s", ssid_name, exc)
        return None


def list_underlay_ssids(central_client: Any) -> list[dict[str, Any]]:
    """Return all wlan-ssid objects from Central."""
    try:
        result = central_client.get("/network-config/v1/wlan-ssids")
        # API returns singular "wlan-ssid" key (not plural)
        items = result.get("wlan-ssid", result.get("wlan-ssids", result.get("items", [])))
        return items if isinstance(items, list) else []
    except Exception as exc:
        logger.warning("list_underlay_ssids failed: %s", exc)
        return []
