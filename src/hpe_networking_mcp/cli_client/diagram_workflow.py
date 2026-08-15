"""Guided network diagram generator and preference wizard."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from hpe_networking_mcp.cli_client.output import prettify_tool_text, tool_result_to_text
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
    if "vendor icon" in low or "vendor-icon" in low or "real icon" in low:
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
        site_m = re.search(r"site(?:-id)?[:=\s]+([a-zA-Z0-9_-]+)", prompt)
        if site_m:
            pref.site_id = site_m.group(1)

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
        site_m = re.search(r"site(?:-id)?[:=\s]+([a-zA-Z0-9_-]+)", prompt)
        if site_m:
            pref.site_id = site_m.group(1)

    # Dynamic model generation based on rich user requirements
    pref.title, pref.filename_stem, pref.nodes, pref.links, pref.groups = _synthesize_topology_model(
        prompt, pref.vendor
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
        nodes.append({"id": "core1", "label": core1_label, "role": "core_switch", "vendor": core_vendor})
        nodes.append({"id": "core2", "label": core2_label, "role": "core_switch", "vendor": core_vendor})
        links.append({"source": "core1", "target": "core2", "link_type": "trunk", "label": "L3 / ISL 100GbE"})

        # Aggregation Layer (if 3-tier or VSX)
        if is_three_tier or is_vsx:
            agg_vendor = "aruba" if has_cx else ("mist" if has_mist else default_vendor)
            agg1_label = "Agg-VSX-01 (CX-8325)" if agg_vendor == "aruba" else "Agg-SW-01 (EX4650)"
            agg2_label = "Agg-VSX-02 (CX-8325)" if agg_vendor == "aruba" else "Agg-SW-02 (EX4650)"
            nodes.append({"id": "agg1", "label": agg1_label, "role": "agg_switch", "vendor": agg_vendor})
            nodes.append({"id": "agg2", "label": agg2_label, "role": "agg_switch", "vendor": agg_vendor})

            links.append({"source": "agg1", "target": "agg2", "link_type": "trunk", "label": "VSX ISL / MC-LAG"})
            links.append({"source": "core1", "target": "agg1", "link_type": "ethernet", "bandwidth": "40GbE"})
            links.append({"source": "core1", "target": "agg2", "link_type": "ethernet", "bandwidth": "40GbE"})
            links.append({"source": "core2", "target": "agg1", "link_type": "ethernet", "bandwidth": "40GbE"})
            links.append({"source": "core2", "target": "agg2", "link_type": "ethernet", "bandwidth": "40GbE"})
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
            links.append({"source": parent_agg1, "target": "acc_cx", "link_type": "ethernet", "bandwidth": "10GbE"})
            links.append({"source": parent_agg2, "target": "acc_cx", "link_type": "ethernet", "bandwidth": "10GbE"})

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
            links.append({"source": parent_agg1, "target": "acc_ex", "link_type": "ethernet", "bandwidth": "10GbE"})
            links.append({"source": parent_agg2, "target": "acc_ex", "link_type": "ethernet", "bandwidth": "10GbE"})

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
            links.append({"source": ap_switch, "target": "ap_mist", "link_type": "ethernet", "label": "PoE+ / Trunk"})
            if "cloud_mist" in [n["id"] for n in nodes]:
                links.append({"source": "cloud_mist", "target": "ap_mist", "link_type": "logical", "label": "Mist AI Telemetry"})
        else:
            nodes.append(
                {
                    "id": "ap_campus",
                    "label": "Aruba AP-635 (Wi-Fi 6E)",
                    "role": "campus_ap",
                    "vendor": "aruba",
                }
            )
            links.append({"source": ap_switch, "target": "ap_campus", "link_type": "ethernet", "label": "PoE+ / Trunk"})

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

        links.append({"source": wireless_ap, "target": "client_corp", "link_type": "wireless", "label": "WPA3 Enterprise (802.1X)"})
        links.append({"source": wireless_ap, "target": "client_guest", "link_type": "wireless", "label": "Guest Portal / MPSK"})
        # Connect IoT directly to wired access switch or AP
        wired_acc = "acc_cx" if (has_cx or not has_ex) else "acc_ex"
        links.append({"source": wired_acc, "target": "client_iot", "link_type": "ethernet", "label": "Wired MAC-Auth"})

        # RADIUS / Policy Enforcement Link
        if "nac_server" in [n["id"] for n in nodes]:
            links.append({"source": wired_acc, "target": "nac_server", "link_type": "logical", "label": "RADIUS / CoA"})
        if "cloud_mist" in [n["id"] for n in nodes]:
            links.append({"source": wireless_ap, "target": "cloud_mist", "link_type": "logical", "label": "Access Assurance Auth"})

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


async def execute_diagram_export(
    mgr: SessionManager,
    safety: SafetyPolicy,
    pref: DiagramPreferences,
) -> dict[str, Any]:
    """Execute diagram validation and export using the connected MCP backend."""
    model_payload = {
        "title": pref.title,
        "nodes": pref.nodes,
        "links": pref.links,
        "groups": pref.groups,
    }

    # 1. Determine tool to call
    tool_map = {
        "drawio": "drawio_network_design_diagram",
        "graphviz": "export_graphviz_topology",
        "nextui": "export_next_ui_topology",
    }
    tool_name = tool_map.get(pref.format, "drawio_network_design_diagram")

    # Check if tool is available
    has_tool = False
    try:
        mgr.resolve_tool_name(tool_name)
        has_tool = True
    except KeyError:
        pass

    # Router invoke_read_tool fallback check
    has_router_read = False
    try:
        mgr.resolve_tool_name("invoke_read_tool")
        has_router_read = True
    except KeyError:
        pass

    if not has_tool and not has_router_read:
        return {
            "ok": False,
            "error": (
                f"Design tool '{tool_name}' is not loaded. "
                "Ensure HPE_MCP_PRODUCTS includes 'design' (e.g. export HPE_MCP_PRODUCTS=design) "
                "and re-index the tool catalog with 'uv run python scripts/ingest_tools.py --complete-catalog'."
            ),
        }

    # 2. Validate model
    try:
        if has_tool:
            val_result = await mgr.call_tool("validate_diagram_model", {"model": model_payload})
        elif has_router_read:
            val_result = await mgr.call_tool(
                "invoke_read_tool",
                {"name": "validate_diagram_model", "arguments": {"model": model_payload}},
            )
        else:
            val_result = None
    except Exception:
        val_result = None

    # 3. Export diagram
    args: dict[str, Any] = {
        "model": model_payload,
        "save": True,
        "filename_stem": pref.filename_stem,
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
        return {
            "ok": True,
            "tool": tool_name,
            "format": pref.format,
            "title": pref.title,
            "node_count": len(pref.nodes),
            "link_count": len(pref.links),
            "text": text,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Diagram export failed: {exc}",
        }
