"""MCP server — cross-vendor interop/translation helpers (5 tools).

Product-neutral, credential-free backend: every tool here is a pure,
offline transform over a payload the caller already has. Nothing in this
module reads configuration, opens a socket, or touches Central/Mist/GLP --
so it is always safe to load, and it is registered unconditionally by
``hpe_networking_mcp.mcp_servers.tool_router`` (see ``_ALWAYS_ON_BACKENDS``)
rather than hidden behind ``HPE_MCP_PRODUCTS``. It also has its own
``HPE_MCP_TOOLSETS=interop`` toolset for callers that want *only* these
tools.

Two capabilities are exposed:

* Central <-> Mist WLAN/site concept translation, wrapping the four tested
  helpers in ``hpe_networking_mcp.mcp_servers.central_mist_translation``.
  Those helpers' ``warnings`` lists are preserved verbatim -- notably the
  best-effort Central ``opmode`` <-> Mist ``auth.type`` mapping, which is
  *not* a confirmed vendor contract and must be verified against a live
  tenant before any write.
* Bounded trend/time-series normalization, wrapping
  ``hpe_networking_mcp.mcp_servers.trend_normalizer.normalize_trend_series``
  so Central ``*-trends`` and Mist ``insights``/SLE payloads fold into one
  ``{timestamp, value}`` sample shape with an explicit ``normalized`` flag.

Every tool is annotated ``READ_ONLY_LOCAL`` (read-only, idempotent, and
closed-world: no external system is contacted), returns a bounded dict, and
reports problems through ``errors``/``warnings`` lists instead of raising.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from hpe_networking_mcp.mcp_servers.central_mist_translation import (
    translate_central_site_to_mist_site,
    translate_central_wlan_to_mist_wlan,
    translate_mist_site_to_central_site,
    translate_mist_wlan_to_central_wlan,
)
from hpe_networking_mcp.mcp_servers.shared import READ_ONLY_LOCAL
from hpe_networking_mcp.mcp_servers.trend_normalizer import (
    DEFAULT_MAX_SAMPLES,
    normalize_trend_series,
)

mcp = MCPServer("interop-core")

#: Hard ceiling on ``normalize_trends`` ``max_samples``, so one call can never
#: return an unbounded series into the model's context regardless of argument.
MAX_TREND_SAMPLES = 2_000


def _translate(
    fn: Any,
    payload: dict[str, Any] | None,
    payload_arg: str,
    result_key: str,
) -> dict[str, Any]:
    """Run one translation helper and normalize its result envelope.

    Keeps every tool below to a single, identical ``{ok, <result_key>,
    warnings, errors}`` shape and turns a bad argument type into a reported
    error rather than a raised exception.
    """
    if not isinstance(payload, dict):
        return {
            "ok": False,
            result_key: {},
            "warnings": [],
            "errors": [f"{payload_arg} must be an object/dict, got {type(payload).__name__}"],
        }
    try:
        result = fn(payload)
    except Exception as exc:  # defensive: helpers are pure, but never raise at the tool seam
        return {
            "ok": False,
            result_key: {},
            "warnings": [],
            "errors": [f"translation failed: {exc}"],
        }
    return {
        "ok": True,
        result_key: result[result_key],
        "warnings": list(result.get("warnings", [])),
        "errors": [],
    }


@mcp.tool(annotations=READ_ONLY_LOCAL)
def translate_central_wlan_to_mist(profile: dict[str, Any]) -> dict[str, Any]:
    """Translate a Central WLAN/SSID profile into a Mist WLAN payload shape.

    Pure/offline: nothing is fetched or written. Feed the result to a Mist
    create/update WLAN tool only after reviewing ``warnings``.

    Args:
        profile: Central-shape WLAN keys -- the same names accepted by
            ``build_underlay_ssid``/``build_overlay_ssid`` (``ssid_name`` or
            ``ssid``, ``vlan_ids``, ``opmode``, ``rf_band``, ``hide_ssid``,
            ``max_clients``, ``client_isolation``, ``inactivity_timeout``,
            ``dtim_period``, ``enabled``, ``wpa_passphrase``). Unrecognized
            keys are ignored -- this is a concept seam, not a rename pass.

    Returns:
        ``{ok, wlan, warnings, errors}``. ``warnings`` is preserved verbatim
        from the translation helper and flags every field that could not be
        confidently mapped -- including the best-effort ``opmode`` ->
        ``auth.type`` mapping, which must be verified against a live Mist
        tenant before writing.
    """
    return _translate(translate_central_wlan_to_mist_wlan, profile, "profile", "wlan")


@mcp.tool(annotations=READ_ONLY_LOCAL)
def translate_mist_wlan_to_central(wlan: dict[str, Any]) -> dict[str, Any]:
    """Translate a Mist WLAN dict into a Central WLAN/SSID profile shape.

    Pure/offline inverse of ``translate_central_wlan_to_mist``.

    Args:
        wlan: Mist-shape WLAN keys (``ssid``, ``vlan_ids``/``vlan_id``,
            ``enabled``, ``hide_ssid``, ``max_num_clients``, ``isolation``,
            ``max_idletime``, ``dtim``, ``band``, ``auth``).

    Returns:
        ``{ok, profile, warnings, errors}``. ``profile`` uses Central
        ``build_underlay_ssid``/``build_overlay_ssid`` parameter names;
        ``warnings`` carries the same best-effort ``auth.type`` -> ``opmode``
        caveat and any unmapped band.
    """
    return _translate(translate_mist_wlan_to_central_wlan, wlan, "wlan", "profile")


@mcp.tool(annotations=READ_ONLY_LOCAL)
def translate_central_site_to_mist(site: dict[str, Any]) -> dict[str, Any]:
    """Translate a Central site dict into a Mist ``createOrgSite`` payload shape.

    Pure/offline: nothing is fetched or written.

    Args:
        site: Central-shape site keys (``name``, ``address``, ``city``,
            ``state``, ``country``, ``zipcode``, ``latitude``, ``longitude``).
            Central's separate address parts are joined into Mist's single
            ``address`` string.

    Returns:
        ``{ok, site, warnings, errors}``. ``warnings`` flags a ``country``
        value that is not the ISO 3166-1 alpha-2 code Mist expects.
    """
    return _translate(translate_central_site_to_mist_site, site, "site", "site")


@mcp.tool(annotations=READ_ONLY_LOCAL)
def translate_mist_site_to_central(site: dict[str, Any]) -> dict[str, Any]:
    """Translate a Mist site dict into a Central ``create_site`` payload shape.

    Pure/offline inverse of ``translate_central_site_to_mist``.

    Args:
        site: Mist-shape site keys (``name``, ``address``, ``country_code``,
            ``latlng``).

    Returns:
        ``{ok, site, warnings, errors}``. Mist's single ``address`` string
        cannot be split back into Central's ``city``/``state``/``zipcode``, so
        those stay unset and ``warnings`` says so -- supply them separately
        before creating the site.
    """
    return _translate(translate_mist_site_to_central_site, site, "site", "site")


@mcp.tool(annotations=READ_ONLY_LOCAL)
def normalize_trends(
    payload: Any,
    metric: str | None = None,
    max_samples: int = DEFAULT_MAX_SAMPLES,
) -> dict[str, Any]:
    """Fold a vendor trend/insights payload into one bounded sample list.

    Pure/offline: pass in an already-retrieved payload (e.g. from Central
    ``get_device_trends``/``get_switch_interface_trends`` or Mist
    ``mist_get_client_insights``/``mist_get_org_insights``); this tool never
    calls an API itself.

    Args:
        payload: The vendor's raw trend/insights JSON -- a list of samples, a
            dict wrapping one under a common key (``data``/``series``/
            ``points``/...), or a dict of parallel timestamp/value arrays.
            An unrecognized shape is reported, never raised on.
        metric: Optional cosmetic label stamped onto the result (e.g.
            ``"cpu"``); never used to drive parsing.
        max_samples: Bounds the returned samples (oldest dropped first).
            Clamped to 1..``MAX_TREND_SAMPLES`` so the response stays bounded;
            a clamp is reported in ``warnings``.

    Returns:
        ``{ok, metric, normalized, samples, sample_count, truncated,
        warnings, errors}``. Check ``normalized`` before trusting
        ``samples``: when it is ``False`` the shape was not recognized and
        ``raw_preserved`` indicates the original payload was left untouched
        (it is deliberately not echoed back, to keep this response bounded).
    """
    warnings: list[str] = []
    try:
        bounded_max = int(max_samples)
    except (TypeError, ValueError):
        return {
            "ok": False,
            "metric": metric,
            "normalized": False,
            "samples": [],
            "sample_count": 0,
            "truncated": False,
            "warnings": [],
            "errors": [f"max_samples must be an integer, got {max_samples!r}"],
        }

    if bounded_max < 1:
        warnings.append(f"max_samples {bounded_max} raised to 1")
        bounded_max = 1
    elif bounded_max > MAX_TREND_SAMPLES:
        warnings.append(f"max_samples {bounded_max} clamped to {MAX_TREND_SAMPLES}")
        bounded_max = MAX_TREND_SAMPLES

    result = normalize_trend_series(payload, metric=metric, max_samples=bounded_max)
    normalized = bool(result["normalized"])
    if not normalized:
        warnings.append(
            "payload shape was not recognized as a time series; samples is empty "
            "and the original payload is unchanged upstream"
        )
    return {
        "ok": True,
        "metric": result["metric"],
        "normalized": normalized,
        "samples": result["samples"],
        "sample_count": result["sample_count"],
        "truncated": bool(result["truncated"]),
        "raw_preserved": not normalized,
        "warnings": warnings,
        "errors": [],
    }


if __name__ == "__main__":
    from hpe_networking_mcp.mcp_servers._cache_hygiene import stable_list_tools
    from hpe_networking_mcp.mcp_servers._middleware import (
        NullStripMiddleware,
        ResponseEnvelopeMiddleware,
        install_middleware,
    )
    stable_list_tools(mcp)
    install_middleware(
        mcp,
        [
            # No rate limiter: these tools make no API call, so there is no
            # account-wide request budget to protect.
            NullStripMiddleware(),
            ResponseEnvelopeMiddleware(),
        ],
    )
    from hpe_networking_mcp.mcp_servers.shared import run_server
    run_server(mcp)
