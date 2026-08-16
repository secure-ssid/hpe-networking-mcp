"""Central <-> Mist WLAN/site concept translation seams.

Provenance / license note: this module is an original implementation
written for this repository. It is not derived from, and does not copy,
any code from the ``nowireless4u/hpe-networking-mcp`` benchmark or any
other project. It closes a capability gap surfaced while auditing this
repo against that benchmark's reference feature set -- its dynamic-mode
surface includes "four translation tools" for cross-platform concept
mapping (see ``docs/capability-gap-matrix.md``), and this repo had none.

These are pure, offline, no-network-call functions. They translate an
*already-fetched or about-to-be-written* dict between the two platforms'
shapes; they never call an API themselves. They are deliberately NOT
registered as MCP tools by this change -- doing so would mean adding
cross-platform entries to ``hpe_networking_mcp.mcp_servers.tool_router``,
the shared discovery/dispatch surface used by every backend, which is out
of scope for a capability-parity audit change (see the audit's final
report for the narrow follow-up). Instead this module exposes plain,
well-tested functions any tool -- router-registered or not -- can import
and call directly.

Field names on both sides are grounded in this repo's own, already-reviewed
schemas rather than guessed:

- Central WLAN/SSID fields: the keyword-argument shape of
  ``hpe_networking_mcp.pipeline.create_ssid.build_underlay_ssid`` /
  ``build_overlay_ssid`` -- this repo's own New Central WLAN-profile
  payload builder (``ssid_name``, ``vlan_ids``, ``opmode``, ``rf_band``,
  ``hide_ssid``, ``max_clients``, ``client_isolation``,
  ``inactivity_timeout``, ``dtim_period``, ``wpa_passphrase``).
- Central site fields: ``hpe_networking_mcp.mcp_servers.config.create_site``
  (``name``, ``address``, ``city``, ``state``, ``country``, ``zipcode``,
  ``latitude``, ``longitude``).
- Mist WLAN fields: the committed, official-OpenAPI-derived manifest at
  ``mcp_servers/openapi_gen/manifests/mist.json``, ``createSiteWlan`` /
  ``createOrgWlan`` request-body property names (``ssid``, ``vlan_id``,
  ``vlan_ids``, ``enabled``, ``hide_ssid``, ``max_num_clients``,
  ``isolation``, ``max_idletime``, ``dtim``, ``auth``, ``band``/``bands``).
- Mist site fields: the same manifest's ``createOrgSite`` request-body
  property names (``name``, ``address``, ``country_code``, ``latlng``).

One subset is NOT independently verified: the manifest only records the
``auth`` property *name*, not its nested schema, so the Central
``opmode`` <-> Mist ``auth.type`` mapping in ``_OPMODE_TO_MIST_AUTH_TYPE``
is this repo's best-effort guess at Mist's enum values (``open``/``psk``/
``eap``), not a confirmed contract. Every function that touches it returns
a ``warnings`` entry when it is used, and callers should confirm against a
live Mist tenant before relying on it for a real write -- the same caveat
this repo already applies to the Axis manifest (see
``docs/capability-gap-matrix.md``).
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# WLAN / SSID auth-type mapping (best-effort -- see module docstring caveat)
# ---------------------------------------------------------------------------

_OPMODE_TO_MIST_AUTH_TYPE: dict[str, str] = {
    "OPEN": "open",
    "ENHANCED_OPEN": "open",
    "WPA_PERSONAL": "psk",
    "WPA2_PERSONAL": "psk",
    "BOTH_WPA_WPA2_PSK": "psk",
    "WPA3_SAE": "psk",
    "WPA_ENTERPRISE": "eap",
    "WPA2_ENTERPRISE": "eap",
    "BOTH_WPA_WPA2_DOT1X": "eap",
    "WPA3_ENTERPRISE_CCM_128": "eap",
    "WPA3_ENTERPRISE_GCM_256": "eap",
    "WPA3_ENTERPRISE_CNSA": "eap",
    "WPA2_MPSK_AES": "psk",
    "WPA2_MPSK_LOCAL": "psk",
}
_MIST_AUTH_TYPE_TO_OPMODE: dict[str, str] = {
    "open": "OPEN",
    "psk": "WPA2_PERSONAL",
    "eap": "WPA2_ENTERPRISE",
}

_CENTRAL_RF_BAND_TO_MIST_BANDS: dict[str, list[str]] = {
    "24GHZ": ["24"],
    "5GHZ": ["5"],
    "6GHZ": ["6"],
    "24GHZ_5GHZ": ["24", "5"],
    "24GHZ_6GHZ": ["24", "6"],
    "5GHZ_6GHZ": ["5", "6"],
    "BAND_24": ["24"],
    "BAND_5": ["5"],
    "BAND_6": ["6"],
}
_MIST_BANDS_TO_CENTRAL_RF_BAND: dict[tuple[str, ...], str] = {
    ("24",): "24GHZ",
    ("5",): "5GHZ",
    ("6",): "6GHZ",
    ("24", "5"): "24GHZ_5GHZ",
    ("24", "6"): "24GHZ_6GHZ",
    ("5", "6"): "5GHZ_6GHZ",
    ("24", "5", "6"): "BAND_ALL",
}


def _normalized_bands(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        raw = value
    elif value in (None, ""):
        return []
    else:
        raw = [value]

    normalized: list[str] = []
    seen: set[str] = set()
    for band in raw:
        token = str(band).strip()
        if token not in {"24", "5", "6"} or token in seen:
            continue
        seen.add(token)
        normalized.append(token)
    return normalized


def translate_central_wlan_to_mist_wlan(profile: dict[str, Any]) -> dict[str, Any]:
    """Translate a Central WLAN/SSID profile dict into a Mist ``wlans`` payload shape.

    Args:
        profile: A dict using Central-style keys -- the same shape as the
            keyword arguments to ``build_underlay_ssid``/``build_overlay_ssid``
            (``ssid_name`` or ``ssid``, ``vlan_ids``, ``opmode``, ``rf_band``,
            ``hide_ssid``, ``max_clients``, ``client_isolation``,
            ``inactivity_timeout``, ``dtim_period``, ``enabled``,
            ``wpa_passphrase``). Unrecognized keys are ignored -- this is a
            concept seam, not a generic pass-through/rename.

    Returns:
        Dict with keys: ``wlan`` (the translated Mist-shape payload) and
        ``warnings`` (list[str] describing any field this function could
        not confidently map).
    """
    warnings: list[str] = []
    wlan: dict[str, Any] = {}

    ssid = profile.get("ssid_name", profile.get("ssid"))
    if ssid is not None:
        wlan["ssid"] = ssid

    vlan_ids = profile.get("vlan_ids")
    if vlan_ids:
        wlan["vlan_ids"] = list(vlan_ids)
        if len(vlan_ids) == 1:
            try:
                wlan["vlan_id"] = int(vlan_ids[0])
            except (TypeError, ValueError):
                warnings.append(
                    f"vlan id {vlan_ids[0]!r} is not numeric; carried over in "
                    "vlan_ids only, vlan_id omitted."
                )

    if "enabled" in profile:
        wlan["enabled"] = bool(profile["enabled"])

    if "hide_ssid" in profile:
        wlan["hide_ssid"] = bool(profile["hide_ssid"])

    if "max_clients" in profile:
        wlan["max_num_clients"] = profile["max_clients"]

    if "client_isolation" in profile:
        wlan["isolation"] = bool(profile["client_isolation"])

    if "inactivity_timeout" in profile:
        wlan["max_idletime"] = profile["inactivity_timeout"]

    if "dtim_period" in profile:
        wlan["dtim"] = profile["dtim_period"]

    rf_band = profile.get("rf_band")
    if rf_band is not None and rf_band != "BAND_ALL":
        mist_bands = _CENTRAL_RF_BAND_TO_MIST_BANDS.get(str(rf_band))
        if mist_bands is not None:
            if len(mist_bands) == 1:
                wlan["band"] = mist_bands[0]
            wlan["bands"] = mist_bands
        else:
            warnings.append(f"unmapped Central rf_band {rf_band!r}; Mist band/bands left unset.")
    # BAND_ALL: leave band/bands unset -- Mist's default covers all bands.

    opmode = profile.get("opmode")
    if opmode is not None:
        auth_type = _OPMODE_TO_MIST_AUTH_TYPE.get(opmode)
        if auth_type is not None:
            auth: dict[str, Any] = {"type": auth_type}
            if auth_type == "psk" and profile.get("wpa_passphrase"):
                auth["psk"] = profile["wpa_passphrase"]
            wlan["auth"] = auth
            warnings.append(
                "auth.type is a best-effort mapping from Central opmode -- "
                "verify against a live Mist tenant before writing (see module docstring)."
            )
        else:
            warnings.append(
                f"unmapped Central opmode {opmode!r}; Mist auth.type left unset -- "
                "set it explicitly before writing this WLAN."
            )

    return {"wlan": wlan, "warnings": warnings}


def translate_mist_wlan_to_central_wlan(wlan: dict[str, Any]) -> dict[str, Any]:
    """Translate a Mist WLAN dict into a Central WLAN/SSID profile shape.

    Inverse of :func:`translate_central_wlan_to_mist_wlan`. See that
    function and the module docstring for field provenance and the
    ``auth``/``opmode`` best-effort caveat.

    Args:
        wlan: A dict using Mist-style keys (``ssid``, ``vlan_ids``/
            ``vlan_id``, ``enabled``, ``hide_ssid``, ``max_num_clients``,
            ``isolation``, ``max_idletime``, ``dtim``, ``band``, ``auth``).

    Returns:
        Dict with keys: ``profile`` (Central-shape kwargs dict, matching
        ``build_underlay_ssid``/``build_overlay_ssid`` parameter names) and
        ``warnings`` (list[str]).
    """
    warnings: list[str] = []
    profile: dict[str, Any] = {}

    if "ssid" in wlan and wlan["ssid"] is not None:
        profile["ssid_name"] = wlan["ssid"]

    vlan_ids = wlan.get("vlan_ids")
    if vlan_ids:
        profile["vlan_ids"] = [str(v) for v in vlan_ids]
    elif wlan.get("vlan_id") is not None:
        profile["vlan_ids"] = [str(wlan["vlan_id"])]

    if "enabled" in wlan:
        profile["enabled"] = bool(wlan["enabled"])

    if "hide_ssid" in wlan:
        profile["hide_ssid"] = bool(wlan["hide_ssid"])

    if "max_num_clients" in wlan:
        profile["max_clients"] = wlan["max_num_clients"]

    if "isolation" in wlan:
        profile["client_isolation"] = bool(wlan["isolation"])

    if "max_idletime" in wlan:
        profile["inactivity_timeout"] = wlan["max_idletime"]

    if "dtim" in wlan:
        profile["dtim_period"] = wlan["dtim"]

    bands = _normalized_bands(wlan.get("bands"))
    if not bands:
        bands = _normalized_bands(wlan.get("band"))
    if bands:
        central_band = _MIST_BANDS_TO_CENTRAL_RF_BAND.get(tuple(sorted(bands)))
        if central_band is not None:
            profile["rf_band"] = central_band
        else:
            warnings.append(
                f"unmapped Mist bands {bands!r}; Central rf_band left unset "
                "(New Central defaults new SSIDs to BAND_ALL)."
            )

    auth = wlan.get("auth")
    if isinstance(auth, dict) and auth.get("type") is not None:
        opmode = _MIST_AUTH_TYPE_TO_OPMODE.get(auth["type"])
        if opmode is not None:
            profile["opmode"] = opmode
            if auth["type"] == "psk" and auth.get("psk"):
                profile["wpa_passphrase"] = auth["psk"]
            warnings.append(
                "opmode is a best-effort mapping from Mist auth.type -- verify "
                "against New Central before writing (see module docstring)."
            )
        else:
            warnings.append(
                f"unmapped Mist auth.type {auth['type']!r}; Central opmode left "
                "unset -- set it explicitly before writing this SSID."
            )

    return {"profile": profile, "warnings": warnings}


# ---------------------------------------------------------------------------
# Site translation
# ---------------------------------------------------------------------------


def translate_central_site_to_mist_site(site: dict[str, Any]) -> dict[str, Any]:
    """Translate a Central site dict into a Mist ``createOrgSite``/``updateOrgSite`` shape.

    Args:
        site: A dict using Central-style keys -- the same shape as
            ``config.create_site`` (``name``, ``address``, ``city``,
            ``state``, ``country``, ``zipcode``, ``latitude``, ``longitude``).

    Returns:
        Dict with keys: ``site`` (translated Mist-shape payload) and
        ``warnings`` (list[str]).
    """
    warnings: list[str] = []
    mist_site: dict[str, Any] = {}

    name = site.get("name")
    if name is not None:
        mist_site["name"] = name

    address_parts = [
        str(part)
        for part in (site.get("address"), site.get("city"), site.get("state"), site.get("zipcode"))
        if part
    ]
    if address_parts:
        mist_site["address"] = ", ".join(address_parts)

    country = site.get("country")
    if country:
        mist_site["country_code"] = country
        if len(str(country)) != 2:
            warnings.append(
                f"country {country!r} is not a 2-letter code; Mist country_code "
                "expects ISO 3166-1 alpha-2 -- verify before writing."
            )

    latitude = site.get("latitude")
    longitude = site.get("longitude")
    if latitude is not None and longitude is not None:
        mist_site["latlng"] = {"lat": latitude, "lng": longitude}

    return {"site": mist_site, "warnings": warnings}


def translate_mist_site_to_central_site(site: dict[str, Any]) -> dict[str, Any]:
    """Translate a Mist site dict into a Central ``create_site`` payload shape.

    Inverse of :func:`translate_central_site_to_mist_site`.

    Args:
        site: A dict using Mist-style keys (``name``, ``address``,
            ``country_code``, ``latlng``).

    Returns:
        Dict with keys: ``site`` (translated Central-shape payload) and
        ``warnings`` (list[str]). Mist's single ``address`` string cannot be
        reliably split back into Central's separate ``city``/``state``/
        ``zipcode`` fields, so those are always left unset here (with a
        warning) -- callers needing them must supply them separately.
    """
    warnings: list[str] = []
    central_site: dict[str, Any] = {}

    name = site.get("name")
    if name is not None:
        central_site["name"] = name

    address = site.get("address")
    if address:
        central_site["address"] = address
        warnings.append(
            "Mist address is a single string; Central city/state/zipcode are "
            "not split out and are left unset."
        )

    country_code = site.get("country_code")
    if country_code:
        central_site["country"] = country_code

    latlng = site.get("latlng")
    if isinstance(latlng, dict):
        if "lat" in latlng:
            central_site["latitude"] = latlng["lat"]
        if "lng" in latlng:
            central_site["longitude"] = latlng["lng"]

    return {"site": central_site, "warnings": warnings}
