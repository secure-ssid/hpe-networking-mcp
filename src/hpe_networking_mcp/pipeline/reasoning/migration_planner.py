"""Multi-vendor network migration planner and translation reasoner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MigrationFeatureMapping:
    feature_category: str
    source_vendor: str
    source_syntax: str
    target_syntax: str
    explanation: str
    caveats: list[str] = field(default_factory=list)


AOS_S_TO_CX_MAPPINGS: list[MigrationFeatureMapping] = [
    MigrationFeatureMapping(
        feature_category="VLAN Tagging & Trunking",
        source_vendor="AOS-S (ProCurve)",
        source_syntax="vlan 10 tagged 1/1-1/4\nvlan 20 untagged 1/5",
        target_syntax="interface 1/1/1-1/1/4\n  vlan trunk allowed 10\ninterface 1/1/5\n  vlan access 20",
        explanation="AOS-S configures port membership inside the VLAN context ('vlan 10 tagged 1'). AOS-CX configures VLAN encapsulation directly under the interface context ('vlan trunk allowed 10' or 'vlan access 20').",
        caveats=["AOS-CX requires 'vlan trunk allowed' to include native VLAN if untagged traffic traverses the trunk."],
    ),
    MigrationFeatureMapping(
        feature_category="Link Aggregation / Port-Channels",
        source_vendor="AOS-S (ProCurve)",
        source_syntax="trunk 1/47-1/48 trk1 lacp",
        target_syntax="interface lag 1\n  no shutdown\n  no routing\n  vlan trunk allowed all\n  lacp mode active\ninterface 1/1/47-1/1/48\n  lag 1",
        explanation="AOS-S assigns ports to trunk IDs ('trk1'). AOS-CX creates a logical LAG interface ('interface lag 1') and adds member interfaces under each physical port context.",
    ),
    MigrationFeatureMapping(
        feature_category="Spanning Tree (RPVST+)",
        source_vendor="AOS-S (ProCurve)",
        source_syntax="spanning-tree\nspanning-tree protocol-version-force rpvst\nspanning-tree 1/1-1/48 admin-edge-port\nspanning-tree 1/1-1/48 bpdu-protection",
        target_syntax="spanning-tree mode rpvst\nspanning-tree\nspanning-tree priority 0\ninterface 1/1/1-1/1/48\n  spanning-tree port-type admin-edge\n  spanning-tree bpdu-guard",
        explanation="AOS-CX uses 'admin-edge' and 'bpdu-guard' per interface, with global 'spanning-tree mode rpvst'.",
    ),
    MigrationFeatureMapping(
        feature_category="Stacking / High Availability",
        source_vendor="AOS-S (ProCurve)",
        source_syntax="stacking\n  member 1\n  member 2",
        target_syntax="vsf member 1\n  type jl658a\n  link 1 1/1/49,1/1/50\nvsf member 2\n  type jl658a\n  link 1 2/1/49,2/1/50\nvsf secondary-member 2\nvsf split-detect mgmt",
        explanation="AOS-CX uses VSF (Virtual Switching Framework) with explicit 10G/25G/50G link port bindings and MAD (Multi-Active Detection) via management port.",
    ),
    MigrationFeatureMapping(
        feature_category="802.1X & MAC Authentication",
        source_vendor="AOS-S (ProCurve)",
        source_syntax="aaa port-access authenticator 1/1-1/48\naaa port-access mac-based 1/1-1/48",
        target_syntax="interface 1/1/1-1/1/48\n  aaa authentication port-access dot1x authenticator\n    enable\n  aaa authentication port-access mac-auth\n    enable\n  aaa authentication port-access client-limit 32",
        explanation="AOS-CX standardizes client port-access with multi-auth / concurrent 802.1X and MAC-Auth under the interface block.",
    ),
]

CISCO_IOS_TO_CX_MAPPINGS: list[MigrationFeatureMapping] = [
    MigrationFeatureMapping(
        feature_category="Switchport Encapsulation",
        source_vendor="Cisco IOS-XE",
        source_syntax="interface GigabitEthernet1/0/1\n switchport mode access\n switchport access vlan 100\n switchport voice vlan 200",
        target_syntax="interface 1/1/1\n  no shutdown\n  vlan access 100\n  vlan voice 200",
        explanation="Cisco uses 'switchport mode access / access vlan'. AOS-CX uses 'vlan access <id>' and 'vlan voice <id>'.",
    ),
    MigrationFeatureMapping(
        feature_category="Trunk Port Configuration",
        source_vendor="Cisco IOS-XE",
        source_syntax="interface TenGigabitEthernet1/1/1\n switchport mode trunk\n switchport trunk native vlan 1\n switchport trunk allowed vlan 10,20,30",
        target_syntax="interface 1/1/1\n  no shutdown\n  vlan trunk native 1\n  vlan trunk allowed 10,20,30",
        explanation="Cisco uses 'switchport mode trunk / switchport trunk allowed vlan'. AOS-CX uses 'vlan trunk allowed <list>'.",
    ),
    MigrationFeatureMapping(
        feature_category="Dynamic Routing (OSPF)",
        source_vendor="Cisco IOS-XE",
        source_syntax="router ospf 1\n router-id 10.255.255.1\n network 10.1.0.0 0.0.0.255 area 0",
        target_syntax="router ospf 1\n  router-id 10.255.255.1\n  area 0.0.0.0\ninterface vlan 10\n  ip ospf 1 area 0.0.0.0",
        explanation="Cisco matches subnets via 'network <subnet> <wildcard>'. AOS-CX enables OSPF directly on the interface / SVI ('ip ospf 1 area 0.0.0.0').",
    ),
]


def plan_migration(source_vendor: str, config_snippet: str | None = None) -> dict[str, Any]:
    """Generate a step-by-step migration blueprint for transitioning to AOS-CX and Aruba Central."""
    src = source_vendor.lower()
    if "cisco" in src or "ios" in src:
        mappings = CISCO_IOS_TO_CX_MAPPINGS
        vendor_name = "Cisco IOS / IOS-XE"
    else:
        mappings = AOS_S_TO_CX_MAPPINGS
        vendor_name = "ArubaOS-S / ProCurve"

    phases = [
        {
            "phase": "1. Discovery & Inventory Audit",
            "description": "Catalog switch models, serials, MAC addresses, transceivers, PoE power budgets, and firmware versions.",
            "recommended_tools": ["list_devices", "get_device_health", "list_switch_interfaces"],
        },
        {
            "phase": "2. Architecture & VLAN Mapping",
            "description": "Design VSF stacking topology, Core-to-Access LAGs, Multi-VLAN segmentation, and Dynamic Segmentation / EVPN-VXLAN.",
            "recommended_tools": ["get_switch_vlans", "lookup_hardware_specs"],
        },
        {
            "phase": "3. Central Cloud Group & Persona Provisioning",
            "description": "Define Central configuration group, assign personas (CAMPUS_AP, ACCESS_SWITCH, AGG_SWITCH), and create port profiles.",
            "recommended_tools": ["list_groups", "create_vlan", "create_sw_port_profile"],
        },
        {
            "phase": "4. Cutover, Stacking & Validation",
            "description": "Execute VSF auto-stacking, verify LLDP neighbors, test 802.1X/MAC-Auth, and validate gateway reachability.",
            "recommended_tools": ["test_ping", "cx_show", "list_clients"],
        },
    ]

    return {
        "source_vendor": vendor_name,
        "target_platform": "Aruba CX (AOS-CX) & Aruba Central",
        "phases": phases,
        "syntax_mappings": [
            {
                "category": m.feature_category,
                "source_syntax": m.source_syntax,
                "target_syntax": m.target_syntax,
                "explanation": m.explanation,
                "caveats": m.caveats,
            }
            for m in mappings
        ],
    }


def format_migration_plan_markdown(plan: dict[str, Any]) -> str:
    """Format migration plan as clean Markdown documentation."""
    lines = [
        f"## 🚀 Migration Blueprint: {plan['source_vendor']} → {plan['target_platform']}",
        "\n### 📌 Phased Migration Roadmap",
    ]

    for p in plan["phases"]:
        lines.append(f"#### {p['phase']}")
        lines.append(p["description"])
        tools = ", ".join(f"`{t}`" for t in p["recommended_tools"])
        lines.append(f"**Associated MCP Tools:** {tools}\n")

    lines.append("### 🔄 Configuration Syntax & Architecture Translation")
    for sm in plan["syntax_mappings"]:
        lines.append(f"#### {sm['category']}")
        lines.append(f"*{sm['explanation']}*\n")
        lines.append(f"**{plan['source_vendor']} Syntax:**")
        lines.append(f"```cli\n{sm['source_syntax']}\n```")
        lines.append("**AOS-CX Equivalent:**")
        lines.append(f"```cli\n{sm['target_syntax']}\n```")
        if sm.get("caveats"):
            for c in sm["caveats"]:
                lines.append(f"⚠️ *Note:* {c}")
        lines.append("")

    return "\n".join(lines)
