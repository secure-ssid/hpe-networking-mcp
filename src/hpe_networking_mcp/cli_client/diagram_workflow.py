"""Guided network diagram generator and preference wizard."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from hpe_networking_mcp.cli_client.output import tool_result_to_text
from hpe_networking_mcp.cli_client.safety import SafetyPolicy
from hpe_networking_mcp.cli_client.sessions import SessionManager


@dataclass
class DiagramPreferences:
    """User preferences for building a network diagram."""

    title: str = "Network Topology"
    format: str = "drawio"  # drawio | graphviz | nextui
    icon_style: str = "generic"  # generic | vendor
    vendor: str = "aruba"  # aruba | cisco | mist | generic
    topology_source: str = "manual"  # manual | live
    site_id: str | None = None
    site_name: str | None = None
    filename_stem: str = "network_topology"
    nodes: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    groups: list[dict[str, Any]] = field(default_factory=list)


def parse_diagram_intent(prompt: str) -> DiagramPreferences:
    """Extract diagram parameters and preferences from natural language."""
    pref = DiagramPreferences()
    low = prompt.lower()

    # Format
    if "graphviz" in low or " dot" in low or "png" in low or "svg" in low:
        pref.format = "graphviz"
    elif "next" in low or "nextui" in low or "web" in low or "dashboard" in low:
        pref.format = "nextui"
    else:
        pref.format = "drawio"

    # Icon style
    if "vendor icon" in low or "vendor-icon" in low or "real icon" in low or "vendor" in low:
        pref.icon_style = "vendor"
    elif "generic" in low:
        pref.icon_style = "generic"

    # Vendor hints
    if "mist" in low or "juniper" in low:
        pref.vendor = "mist"
    elif "cisco" in low:
        pref.vendor = "cisco"
    elif "clearpass" in low:
        pref.vendor = "clearpass"
    elif "aruba" in low or "cx" in low or "central" in low or "hpe" in low:
        pref.vendor = "aruba"

    # Live vs manual
    if "live" in low or "site" in low or "central topology" in low:
        pref.topology_source = "live"
        site_id_m = re.search(
            r"\bsite(?:[-_\s]?id)\s*[:=]?\s*([a-zA-Z0-9_.:-]+)",
            prompt,
            re.IGNORECASE,
        ) or re.search(r"\bsite\s*[:=]\s*([a-zA-Z0-9_.:-]+)", prompt, re.IGNORECASE)
        if site_id_m:
            pref.site_id = site_id_m.group(1)
        else:
            quoted_site_m = re.search(
                r"""\bsite\s+(?:named\s+)?(?:"([^"]+)"|'([^']+)')""",
                prompt,
                re.IGNORECASE,
            )
            plain_site_m = re.search(
                r"\bsite\s+(?:named\s+)?([a-zA-Z0-9][a-zA-Z0-9_. -]*?)"
                r"(?=\s+(?:with|using|in|as|for|and|to)\b|[,;]|$)",
                prompt,
                re.IGNORECASE,
            )
            if quoted_site_m:
                pref.site_name = next(value.strip() for value in quoted_site_m.groups() if value)
            elif plain_site_m:
                site_name = re.sub(
                    r"\s+(?:topology|diagram|network)$",
                    "",
                    plain_site_m.group(1).strip(),
                    flags=re.IGNORECASE,
                )
                pref.site_name = site_name or None

    # Dynamic model generation based on rich user requirements
    pref.title, pref.filename_stem, pref.nodes, pref.links, pref.groups = (
        _synthesize_topology_model(prompt, pref.vendor)
    )

    return pref


def _synthesize_topology_model(
    prompt: str, default_vendor: str
) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Dynamically synthesize nodes, links, and hierarchy from natural language prompt."""
    low = prompt.lower()

    # Determine specific feature presence
    has_mist = "mist" in low or "juniper" in low
    has_cx = "cx" in low or "aruba" in low or "8360" in low or "6300" in low or "6200" in low
    has_ex = "ex" in low or "ex4400" in low or "ex4100" in low or "ex2300" in low
    has_auth_roles = any(
        w in low
        for w in (
            "auth",
            "role",
            "profile",
            "client",
            "clearpass",
            "802.1x",
            "dot1x",
            "macauth",
            "mpsk",
            "nac",
            "radius",
            "dynamic segmentation",
        )
    )
    is_three_tier = any(w in low for w in ("3 tier", "3-tier", "three tier", "three-tier"))
    is_vsx = "vsx" in low
    is_branch = "branch" in low

    # If detailed prompt with auth, multi-vendor, or Mist client profiling:
    if has_auth_roles or (has_mist and (has_cx or has_ex)) or (has_cx and has_ex):
        title = "Enterprise Network with Role-Based Client Authentication"
        if is_three_tier:
            title = "Three-Tier Campus with Role-Based Client Authentication"
        if has_mist and (has_cx or has_ex):
            title += " (Mist + Hybrid Switching)"
        elif has_mist:
            title += " (Mist Wireless)"
        elif has_cx:
            title += " (Aruba CX Switching)"

        stem = "campus_auth_topology"
        if is_three_tier:
            stem = "three_tier_auth_campus"

        nodes: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []
        groups: list[dict[str, Any]] = []

        # Cloud / Policy Engine Layer
        if has_mist:
            nodes.append(
                {
                    "id": "cloud_mist",
                    "label": "Mist Cloud (AI / Access Assurance)",
                    "role": "cloud",
                    "vendor": "mist",
                }
            )
        if "clearpass" in low or not has_mist or has_cx:
            nodes.append(
                {
                    "id": "nac_server",
                    "label": "ClearPass / RADIUS (Role & Policy Engine)",
                    "role": "clearpass" if "clearpass" in low else "server",
                    "vendor": "aruba" if ("clearpass" in low or has_cx) else "generic",
                }
            )

        # Core Layer
        core_vendor = "mist" if (has_mist and not has_cx) else "aruba"
        core1_label = "Core-SW-01 (CX-8360)" if core_vendor == "aruba" else "Core-SW-01 (QFX5120)"
        core2_label = "Core-SW-02 (CX-8360)" if core_vendor == "aruba" else "Core-SW-02 (QFX5120)"
        nodes.append(
            {"id": "core1", "label": core1_label, "role": "core_switch", "vendor": core_vendor}
        )
        nodes.append(
            {"id": "core2", "label": core2_label, "role": "core_switch", "vendor": core_vendor}
        )
        links.append(
            {"source": "core1", "target": "core2", "link_type": "trunk", "label": "L3 / ISL 100GbE"}
        )

        # Aggregation Layer (if 3-tier or VSX)
        if is_three_tier or is_vsx:
            agg_vendor = "aruba" if has_cx else ("mist" if has_mist else default_vendor)
            agg1_label = "Agg-VSX-01 (CX-8325)" if agg_vendor == "aruba" else "Agg-SW-01 (EX4650)"
            agg2_label = "Agg-VSX-02 (CX-8325)" if agg_vendor == "aruba" else "Agg-SW-02 (EX4650)"
            nodes.append(
                {"id": "agg1", "label": agg1_label, "role": "agg_switch", "vendor": agg_vendor}
            )
            nodes.append(
                {"id": "agg2", "label": agg2_label, "role": "agg_switch", "vendor": agg_vendor}
            )

            links.append(
                {
                    "source": "agg1",
                    "target": "agg2",
                    "link_type": "trunk",
                    "label": "VSX ISL / MC-LAG",
                }
            )
            links.append(
                {"source": "core1", "target": "agg1", "link_type": "ethernet", "bandwidth": "40GbE"}
            )
            links.append(
                {"source": "core1", "target": "agg2", "link_type": "ethernet", "bandwidth": "40GbE"}
            )
            links.append(
                {"source": "core2", "target": "agg1", "link_type": "ethernet", "bandwidth": "40GbE"}
            )
            links.append(
                {"source": "core2", "target": "agg2", "link_type": "ethernet", "bandwidth": "40GbE"}
            )
            parent_agg1, parent_agg2 = "agg1", "agg2"
        else:
            parent_agg1, parent_agg2 = "core1", "core2"

        # Access Layer
        # Add CX switch if requested or default
        if has_cx or not has_ex:
            nodes.append(
                {
                    "id": "acc_cx",
                    "label": "Access-CX-6300 (802.1X / Dynamic Seg)",
                    "role": "access_switch",
                    "vendor": "aruba",
                }
            )
            links.append(
                {
                    "source": parent_agg1,
                    "target": "acc_cx",
                    "link_type": "ethernet",
                    "bandwidth": "10GbE",
                }
            )
            links.append(
                {
                    "source": parent_agg2,
                    "target": "acc_cx",
                    "link_type": "ethernet",
                    "bandwidth": "10GbE",
                }
            )

        # Add EX switch if requested
        if has_ex or (has_mist and not has_cx):
            nodes.append(
                {
                    "id": "acc_ex",
                    "label": "Access-EX-4400 (Virtual Chassis / PoE+)",
                    "role": "access_switch",
                    "vendor": "mist",
                }
            )
            links.append(
                {
                    "source": parent_agg1,
                    "target": "acc_ex",
                    "link_type": "ethernet",
                    "bandwidth": "10GbE",
                }
            )
            links.append(
                {
                    "source": parent_agg2,
                    "target": "acc_ex",
                    "link_type": "ethernet",
                    "bandwidth": "10GbE",
                }
            )

        # Wireless Access Point Layer
        ap_switch = "acc_cx" if (has_cx or not has_ex) else "acc_ex"
        if has_mist:
            nodes.append(
                {
                    "id": "ap_mist",
                    "label": "Mist AP45 (Wi-Fi 6E / Multi-SSID)",
                    "role": "mist_ap",
                    "vendor": "mist",
                }
            )
            links.append(
                {
                    "source": ap_switch,
                    "target": "ap_mist",
                    "link_type": "ethernet",
                    "label": "PoE+ / Trunk",
                }
            )
            if "cloud_mist" in [n["id"] for n in nodes]:
                links.append(
                    {
                        "source": "cloud_mist",
                        "target": "ap_mist",
                        "link_type": "logical",
                        "label": "Mist AI Telemetry",
                    }
                )
        else:
            nodes.append(
                {
                    "id": "ap_campus",
                    "label": "Aruba AP-635 (Wi-Fi 6E)",
                    "role": "campus_ap",
                    "vendor": "aruba",
                }
            )
            links.append(
                {
                    "source": ap_switch,
                    "target": "ap_campus",
                    "link_type": "ethernet",
                    "label": "PoE+ / Trunk",
                }
            )

        # Clients with Profiled Roles Layer
        wireless_ap = "ap_mist" if has_mist else "ap_campus"
        nodes.append(
            {
                "id": "client_corp",
                "label": "Corp Laptop (802.1X | Role: Employee-VLAN10)",
                "role": "client",
                "vendor": "generic",
            }
        )
        nodes.append(
            {
                "id": "client_guest",
                "label": "Guest Mobile (MPSK | Role: Guest-Isolated)",
                "role": "client",
                "vendor": "generic",
            }
        )
        nodes.append(
            {
                "id": "client_iot",
                "label": "IoT Camera / Sensor (MAC-Auth | Role: IoT-Restricted)",
                "role": "client",
                "vendor": "generic",
            }
        )

        links.append(
            {
                "source": wireless_ap,
                "target": "client_corp",
                "link_type": "wireless",
                "label": "WPA3 Enterprise (802.1X)",
            }
        )
        links.append(
            {
                "source": wireless_ap,
                "target": "client_guest",
                "link_type": "wireless",
                "label": "Guest Portal / MPSK",
            }
        )
        # Connect IoT directly to wired access switch or AP
        wired_acc = "acc_cx" if (has_cx or not has_ex) else "acc_ex"
        links.append(
            {
                "source": wired_acc,
                "target": "client_iot",
                "link_type": "ethernet",
                "label": "Wired MAC-Auth",
            }
        )

        # RADIUS / Policy Enforcement Link
        if "nac_server" in [n["id"] for n in nodes]:
            links.append(
                {
                    "source": wired_acc,
                    "target": "nac_server",
                    "link_type": "logical",
                    "label": "RADIUS / CoA",
                }
            )
        if "cloud_mist" in [n["id"] for n in nodes]:
            links.append(
                {
                    "source": wireless_ap,
                    "target": "cloud_mist",
                    "link_type": "logical",
                    "label": "Access Assurance Auth",
                }
            )

        return title, stem, nodes, links, groups

    # Standard / Simple Scenarios
    if is_three_tier:
        nodes, links = _build_three_tier_model(default_vendor)
        return "Three-Tier Campus Network", "three_tier_campus", nodes, links, []
    if is_vsx:
        nodes, links = _build_vsx_model(default_vendor)
        return "VSX Redundant Aggregation", "vsx_aggregation", nodes, links, []
    if is_branch:
        nodes, links = _build_branch_model(default_vendor)
        return "Branch Site Topology", "branch_topology", nodes, links, []

    nodes, links = _build_default_model(default_vendor)
    return "Network Topology", "network_topology", nodes, links, []


def _build_three_tier_model(vendor: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    v = vendor if vendor in {"aruba", "cisco", "mist"} else "aruba"
    nodes = [
        {"id": "core1", "label": "Core-SW-01", "role": "core_switch", "vendor": v},
        {"id": "core2", "label": "Core-SW-02", "role": "core_switch", "vendor": v},
        {"id": "agg1", "label": "Agg-VSX-01", "role": "agg_switch", "vendor": v},
        {"id": "agg2", "label": "Agg-VSX-02", "role": "agg_switch", "vendor": v},
        {"id": "acc1", "label": "Access-SW-01", "role": "access_switch", "vendor": v},
        {"id": "acc2", "label": "Access-SW-02", "role": "access_switch", "vendor": v},
        {"id": "ap1", "label": "Campus-AP-01", "role": "campus_ap", "vendor": v},
        {"id": "ap2", "label": "Campus-AP-02", "role": "campus_ap", "vendor": v},
    ]
    links = [
        {"source": "core1", "target": "core2", "link_type": "trunk", "label": "ISL / L3"},
        {"source": "core1", "target": "agg1", "link_type": "ethernet", "bandwidth": "40GbE"},
        {"source": "core1", "target": "agg2", "link_type": "ethernet", "bandwidth": "40GbE"},
        {"source": "core2", "target": "agg1", "link_type": "ethernet", "bandwidth": "40GbE"},
        {"source": "core2", "target": "agg2", "link_type": "ethernet", "bandwidth": "40GbE"},
        {"source": "agg1", "target": "agg2", "link_type": "trunk", "label": "VSX ISL"},
        {"source": "agg1", "target": "acc1", "link_type": "ethernet", "bandwidth": "10GbE"},
        {"source": "agg2", "target": "acc1", "link_type": "ethernet", "bandwidth": "10GbE"},
        {"source": "agg1", "target": "acc2", "link_type": "ethernet", "bandwidth": "10GbE"},
        {"source": "agg2", "target": "acc2", "link_type": "ethernet", "bandwidth": "10GbE"},
        {"source": "acc1", "target": "ap1", "link_type": "ethernet", "label": "PoE+"},
        {"source": "acc2", "target": "ap2", "link_type": "ethernet", "label": "PoE+"},
    ]
    return nodes, links


def _build_vsx_model(vendor: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    v = vendor if vendor in {"aruba", "cisco", "mist"} else "aruba"
    nodes = [
        {"id": "vsx1", "label": "VSX-Primary-8360", "role": "agg_switch", "vendor": v},
        {"id": "vsx2", "label": "VSX-Secondary-8360", "role": "agg_switch", "vendor": v},
        {"id": "acc1", "label": "Stack-Access-6300", "role": "access_switch", "vendor": v},
        {"id": "gw1", "label": "Mobility-Gateway", "role": "gateway", "vendor": v},
    ]
    links = [
        {"source": "vsx1", "target": "vsx2", "link_type": "trunk", "label": "ISL Keepalive"},
        {"source": "vsx1", "target": "acc1", "link_type": "ethernet", "label": "MC-LAG Port 1"},
        {"source": "vsx2", "target": "acc1", "link_type": "ethernet", "label": "MC-LAG Port 2"},
        {"source": "vsx1", "target": "gw1", "link_type": "ethernet", "label": "Uplink"},
        {"source": "vsx2", "target": "gw1", "link_type": "ethernet", "label": "Uplink"},
    ]
    return nodes, links


def _build_branch_model(vendor: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    v = vendor if vendor in {"aruba", "cisco", "mist"} else "aruba"
    nodes = [
        {"id": "gw", "label": "Branch-Gateway-9004", "role": "gateway", "vendor": v},
        {"id": "sw", "label": "Access-Switch-6200F", "role": "access_switch", "vendor": v},
        {"id": "ap1", "label": "Branch-AP-635", "role": "campus_ap", "vendor": v},
        {"id": "ap2", "label": "Branch-AP-635", "role": "campus_ap", "vendor": v},
    ]
    links = [
        {"source": "gw", "target": "sw", "link_type": "trunk", "label": "Uplink Trunk"},
        {"source": "sw", "target": "ap1", "link_type": "ethernet", "label": "PoE+ Eth0"},
        {"source": "sw", "target": "ap2", "link_type": "ethernet", "label": "PoE+ Eth0"},
    ]
    return nodes, links


def _build_default_model(vendor: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    v = vendor if vendor in {"aruba", "cisco", "mist"} else "aruba"
    nodes = [
        {"id": "core", "label": "Core-Switch", "role": "core_switch", "vendor": v},
        {"id": "access", "label": "Access-Switch", "role": "access_switch", "vendor": v},
        {"id": "ap", "label": "Access-Point", "role": "campus_ap", "vendor": v},
    ]
    links = [
        {"source": "core", "target": "access", "link_type": "ethernet", "bandwidth": "10GbE"},
        {"source": "access", "target": "ap", "link_type": "ethernet", "label": "PoE"},
    ]
    return nodes, links


def _has_tool(mgr: SessionManager, name: str) -> bool:
    try:
        mgr.resolve_tool_name(name)
        return True
    except KeyError:
        return False


async def _call_backend_read(
    mgr: SessionManager,
    name: str,
    arguments: dict[str, Any],
) -> Any:
    """Prefer the router's read-only dispatcher, then use a direct read tool."""
    if _has_tool(mgr, "invoke_read_tool"):
        return await mgr.call_tool(
            "invoke_read_tool",
            {"name": name, "arguments": arguments},
        )
    resolved = mgr.resolve_tool_name(name)
    return await mgr.call_tool(resolved, arguments)


def _tool_payload(result: Any) -> Any:
    """Extract structured JSON from direct or MCP CallToolResult responses."""
    if isinstance(result, (dict, list)):
        payload = result
    else:
        payload = getattr(result, "structuredContent", None) or getattr(
            result, "structured_content", None
        )
        if payload is None:
            for block in getattr(result, "content", None) or []:
                text = (
                    block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
                )
                if not isinstance(text, str):
                    continue
                try:
                    payload = json.loads(text)
                    break
                except json.JSONDecodeError:
                    continue
    while isinstance(payload, dict):
        nested = payload.get("result")
        if not isinstance(nested, (dict, list)):
            break
        payload = nested
    return payload


def _payload_error(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()
    if payload.get("ok") is False:
        return str(payload.get("message") or "tool returned an unsuccessful result")
    return None


def _scope_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list) and isinstance(payload.get("data"), dict):
        items = payload["data"].get("items")
    return [item for item in (items or [])[:100] if isinstance(item, dict)]


async def _resolve_live_site(
    mgr: SessionManager,
    pref: DiagramPreferences,
) -> tuple[str | None, str | None]:
    if pref.site_id:
        return pref.site_id, None

    try:
        result = await _call_backend_read(
            mgr,
            "list_scopes",
            {"limit": 100, "offset": 0, "full_list": False},
        )
    except Exception as exc:
        return None, f"Central site discovery failed: {exc}"

    payload = _tool_payload(result)
    if error := _payload_error(payload):
        return None, f"Central site discovery failed: {error}"

    sites = [
        item
        for item in _scope_items(payload)
        if str(item.get("scope_type") or item.get("type") or "SITE").upper() == "SITE"
    ]
    if pref.site_name:
        wanted = pref.site_name.casefold()
        matches = [
            item
            for item in sites
            if wanted
            in {
                str(item.get("scope_id") or item.get("id") or "").casefold(),
                str(item.get("scope_name") or item.get("name") or "").casefold(),
            }
        ]
        if len(matches) == 1:
            site_id = matches[0].get("scope_id") or matches[0].get("id")
            return str(site_id), None
        if len(matches) > 1:
            return None, f"Site {pref.site_name!r} is ambiguous in Central"
        return None, (
            f"Site {pref.site_name!r} was not found in the first {len(sites)} Central sites"
        )

    if len(sites) == 1:
        site_id = sites[0].get("scope_id") or sites[0].get("id")
        if site_id:
            return str(site_id), None
    return None, "Live topology requires a site ID or an unambiguous Central site name"


def _safe_identifier(value: Any, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(value or fallback))[:64]
    return cleaned or fallback


def _labeled_filename_stem(stem: str, label: str) -> str:
    suffix = f"_{label}"
    return stem if stem.casefold().endswith(suffix) else f"{stem}{suffix}"


def _endpoint_identifier(value: Any) -> str:
    if isinstance(value, dict):
        value = (
            value.get("id")
            or value.get("device_id")
            or value.get("deviceId")
            or value.get("serial")
            or value.get("name")
        )
    return str(value or "")


def _live_role(item: dict[str, Any]) -> str:
    dtype = str(
        item.get("role")
        or item.get("type")
        or item.get("device_type")
        or item.get("deviceType")
        or item.get("persona")
        or ""
    ).lower()
    if "firewall" in dtype:
        return "firewall"
    if "gateway" in dtype:
        return "gateway"
    if "controller" in dtype:
        return "controller"
    if "core" in dtype:
        return "core_switch"
    if any(value in dtype for value in ("aggregation", "aggregate", "distribution", "agg")):
        return "agg_switch"
    if any(value in dtype for value in ("access point", "access_point", "campus_ap", "iap")):
        return "campus_ap"
    if dtype == "ap" or dtype.endswith("_ap"):
        return "campus_ap"
    if "switch" in dtype:
        return "access_switch"
    if "router" in dtype:
        return "router"
    if "client" in dtype:
        return "client"
    if "server" in dtype:
        return "server"
    return "generic"


def _live_model(
    payload: Any,
    *,
    site_id: str,
    default_vendor: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise ValueError("topology tool returned no structured payload")
    if error := _payload_error(payload):
        raise ValueError(error)

    container = payload
    nodes_raw = container.get("nodes") or container.get("devices") or container.get("vertices")
    links_raw = container.get("links") or container.get("edges") or container.get("connections")
    groups_raw = container.get("groups")
    if not isinstance(nodes_raw, list) and isinstance(container.get("data"), dict):
        container = container["data"]
        nodes_raw = container.get("nodes") or container.get("devices") or container.get("vertices")
        links_raw = container.get("links") or container.get("edges") or container.get("connections")
        groups_raw = container.get("groups")
    if not isinstance(nodes_raw, list) or not nodes_raw:
        raise ValueError("topology payload has no nodes or devices")

    nodes: list[dict[str, Any]] = []
    raw_to_node: dict[str, str] = {}
    used_ids: set[str] = set()
    derived_group_members: dict[str, list[str]] = {}
    for index, item in enumerate(nodes_raw[:200]):
        if not isinstance(item, dict):
            continue
        raw_id = (
            item.get("id")
            or item.get("device_id")
            or item.get("deviceId")
            or item.get("serial")
            or item.get("serial_number")
            or item.get("name")
            or f"node_{index}"
        )
        node_id = _safe_identifier(raw_id, f"node_{index}")
        if node_id in used_ids:
            node_id = _safe_identifier(f"{node_id}_{index}", f"node_{index}")
        used_ids.add(node_id)
        raw_to_node[str(raw_id)] = node_id

        dtype = str(item.get("type") or item.get("device_type") or "").lower()
        vendor = str(item.get("vendor") or item.get("manufacturer") or default_vendor).lower()
        if "mist" in dtype:
            vendor = "mist"
        elif vendor not in {"aruba", "hpe", "mist", "juniper", "cisco", "generic"}:
            vendor = "aruba"

        node: dict[str, Any] = {
            "id": node_id,
            "label": str(
                item.get("label")
                or item.get("name")
                or item.get("hostname")
                or item.get("display_name")
                or raw_id
            ),
            "role": _live_role(item),
            "vendor": vendor,
            "site": site_id,
        }
        serial = item.get("serial") or item.get("serial_number")
        mgmt_ip = item.get("mgmt_ip") or item.get("ip") or item.get("ipv4")
        if serial:
            node["serial"] = str(serial)
        if mgmt_ip:
            node["mgmt_ip"] = str(mgmt_ip)

        raw_group = item.get("group") or item.get("group_name") or item.get("groupName")
        if isinstance(raw_group, dict):
            raw_group = raw_group.get("id") or raw_group.get("name")
        if raw_group:
            group_id = _safe_identifier(raw_group, f"group_{index}")
            node["group"] = group_id
            derived_group_members.setdefault(group_id, []).append(node_id)
        nodes.append(node)

    if not nodes:
        raise ValueError("topology payload has no usable nodes")

    links: list[dict[str, Any]] = []
    for item in links_raw[:500] if isinstance(links_raw, list) else []:
        if not isinstance(item, dict):
            continue
        raw_source = _endpoint_identifier(
            item.get("source")
            or item.get("from")
            or item.get("src")
            or item.get("local_device")
            or item.get("source_id")
        )
        raw_target = _endpoint_identifier(
            item.get("target")
            or item.get("to")
            or item.get("dst")
            or item.get("remote_device")
            or item.get("target_id")
        )
        source = raw_to_node.get(raw_source, _safe_identifier(raw_source, ""))
        target = raw_to_node.get(raw_target, _safe_identifier(raw_target, ""))
        if source not in used_ids or target not in used_ids or source == target:
            continue
        link_type = str(item.get("link_type") or item.get("type") or "ethernet").lower()
        if "wireless" in link_type:
            link_type = "wireless"
        elif link_type not in {"ethernet", "trunk", "wan", "logical"}:
            link_type = "ethernet"
        link: dict[str, Any] = {
            "source": source,
            "target": target,
            "link_type": link_type,
        }
        label = item.get("label") or item.get("name")
        bandwidth = item.get("bandwidth") or item.get("speed")
        if label:
            link["label"] = str(label)
        if bandwidth:
            link["bandwidth"] = str(bandwidth)
        links.append(link)

    groups_by_id: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(groups_raw[:100] if isinstance(groups_raw, list) else []):
        if not isinstance(item, dict):
            continue
        raw_group_id = item.get("id") or item.get("group_id") or item.get("name")
        group_id = _safe_identifier(raw_group_id, f"group_{index}")
        members: list[str] = []
        for member in item.get("members") or item.get("nodes") or item.get("devices") or []:
            raw_member = _endpoint_identifier(member)
            member_id = raw_to_node.get(raw_member, _safe_identifier(raw_member, ""))
            if member_id in used_ids and member_id not in members:
                members.append(member_id)
        groups_by_id[group_id] = {
            "id": group_id,
            "label": str(item.get("label") or item.get("name") or raw_group_id or group_id),
            "members": members,
        }
    for group_id, members in derived_group_members.items():
        group = groups_by_id.setdefault(
            group_id,
            {"id": group_id, "label": group_id.replace("_", " "), "members": []},
        )
        group["members"] = list(dict.fromkeys([*group["members"], *members]))

    return nodes, links, list(groups_by_id.values())


async def execute_diagram_export(
    mgr: SessionManager,
    safety: SafetyPolicy,
    pref: DiagramPreferences,
) -> dict[str, Any]:
    """Execute diagram validation and export using the connected MCP backend."""
    # 1. Determine tool to call
    tool_map = {
        "drawio": "drawio_network_design_diagram",
        "graphviz": "export_graphviz_topology",
        "nextui": "export_next_ui_topology",
    }
    tool_name = tool_map.get(pref.format, "drawio_network_design_diagram")

    # Check if tool is available
    has_tool = _has_tool(mgr, tool_name)

    # Router invoke_read_tool fallback check
    has_router_read = _has_tool(mgr, "invoke_read_tool")

    if not has_tool and not has_router_read:
        return {
            "ok": False,
            "error": (
                f"Design tool '{tool_name}' is not loaded. "
                "Ensure HPE_MCP_PRODUCTS includes 'design' (e.g. export HPE_MCP_PRODUCTS=design) "
                "and re-index the tool catalog with "
                "'uv run python scripts/ingest_tools.py --complete-catalog'."
            ),
        }

    model_title = pref.title
    model_notes: list[str] = []
    topology_source = pref.topology_source
    fallback_warning: str | None = None
    resolved_site_id: str | None = pref.site_id
    export_filename_stem = pref.filename_stem

    if pref.topology_source == "live":
        resolved_site_id, resolution_error = await _resolve_live_site(mgr, pref)
        if resolution_error or not resolved_site_id:
            return {
                "ok": False,
                "error": resolution_error or "Unable to resolve a Central site for live topology",
                "topology_source": "live",
            }
        pref.site_id = resolved_site_id
        try:
            topology_result = await _call_backend_read(
                mgr,
                "get_topology",
                {"site_id": resolved_site_id},
            )
            pref.nodes, pref.links, pref.groups = _live_model(
                _tool_payload(topology_result),
                site_id=resolved_site_id,
                default_vendor=pref.vendor,
            )
            model_title = f"LIVE — {pref.title} (site {resolved_site_id})"
            model_notes.append(
                f"Grounded in live Central topology fetched for site {resolved_site_id}."
            )
            export_filename_stem = _labeled_filename_stem(pref.filename_stem, "live")
        except Exception as exc:
            reason = str(exc).strip().replace("\n", " ")[:240] or type(exc).__name__
            topology_source = "illustrative_fallback"
            model_title = f"ILLUSTRATIVE fallback — {pref.title}"
            fallback_warning = (
                f"Live topology for site {resolved_site_id!r} was unavailable; "
                f"exported an illustrative model instead: {reason}"
            )
            model_notes.append(fallback_warning)
            export_filename_stem = _labeled_filename_stem(
                pref.filename_stem,
                "illustrative",
            )

    model_payload = {
        "title": model_title,
        "nodes": pref.nodes,
        "links": pref.links,
        "groups": pref.groups,
    }
    if model_notes:
        model_payload["notes"] = model_notes

    # 2. Validate model
    try:
        if has_tool:
            await mgr.call_tool("validate_diagram_model", {"model": model_payload})
        elif has_router_read:
            await mgr.call_tool(
                "invoke_read_tool",
                {"name": "validate_diagram_model", "arguments": {"model": model_payload}},
            )
    except Exception:
        pass

    # 3. Export diagram
    args: dict[str, Any] = {
        "model": model_payload,
        "save": True,
        "filename_stem": export_filename_stem,
    }
    if pref.format == "graphviz":
        args["render_format"] = "svg"

    try:
        if has_tool:
            res = await mgr.call_tool(tool_name, args)
        else:
            res = await mgr.call_tool(
                "invoke_read_tool",
                {"name": tool_name, "arguments": args},
            )
        text = tool_result_to_text(res)
        if fallback_warning:
            text = f"ILLUSTRATIVE fallback: {fallback_warning}\n\n{text}"
        elif topology_source == "live":
            text = (
                f"LIVE topology: grounded in Central site {resolved_site_id}.\n\n"
                f"{text}"
            )
        return {
            "ok": True,
            "tool": tool_name,
            "format": pref.format,
            "title": model_title,
            "node_count": len(pref.nodes),
            "link_count": len(pref.links),
            "group_count": len(pref.groups),
            "topology_source": topology_source,
            "site_id": resolved_site_id,
            "filename_stem": export_filename_stem,
            "warning": fallback_warning,
            "text": text,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Diagram export failed: {exc}",
        }
