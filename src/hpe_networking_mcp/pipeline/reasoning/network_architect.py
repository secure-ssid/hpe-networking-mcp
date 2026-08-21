"""Campus, branch, and data center network architecture synthesis engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ArchitectureRecommendation:
    topology_type: str  # "evpn_vxlan_fabric", "collapsed_core", "three_tier_campus", "sdwan_branch"
    title: str
    description: str
    recommended_hardware: list[dict[str, Any]] = field(default_factory=list)
    key_design_principles: list[str] = field(default_factory=list)
    advantages: list[str] = field(default_factory=list)
    config_highlights: list[str] = field(default_factory=list)


def synthesize_architecture(
    environment: str = "campus",
    scale_ap_count: int = 50,
    scale_switch_port_count: int = 200,
    require_evpn: bool = False,
    require_zero_trust: bool = True,
) -> ArchitectureRecommendation:
    """Synthesize an optimal network design architecture and hardware bill of materials."""
    env = environment.lower()

    if "dc" in env or "datacenter" in env or "data center" in env or require_evpn:
        return ArchitectureRecommendation(
            topology_type="evpn_vxlan_fabric",
            title="Modern EVPN-VXLAN Spine-Leaf Campus / Data Center Fabric",
            description="High-performance, non-blocking L3 Spine-Leaf underlay with BGP EVPN "
                        "control plane and VXLAN encapsulation for microsegmentation and seamless "
                        "VM/workload mobility.",
            recommended_hardware=[
                {
                    "role": "Spine",
                    "model": "Aruba CX 8360-32Y4C",
                    "quantity": 2,
                    "ports": "32x 25G SFP28 + 4x 100G QSFP28",
                    "notes": "Spine aggregation with line-rate BGP EVPN route-reflector",
                },
                {
                    "role": "Leaf (Distributed Services)",
                    "model": "Aruba CX 10000-48Y6C (with Pensando DPU)",
                    "quantity": 2,
                    "ports": "48x 25G SFP28 + 6x 100G QSFP28 + 800G Stateful Firewall DPU",
                    "notes": "Stateful east-west firewalling, microsegmentation, and IPsec "
                             "line-rate encryption",
                },
                {
                    "role": "Access / Server Leaf",
                    "model": "Aruba CX 6300M 48-port SmartRate / PoE",
                    "quantity": max(2, (scale_switch_port_count + 47) // 48),
                    "ports": "48x SmartRate 1G/2.5G/5G PoE Class 8 90W + 4x 50G SFP56",
                    "notes": "High-density multi-gigabit access for Wi-Fi 6E/7 APs and servers",
                },
            ],
            key_design_principles=[
                "Layer 3 routed underlay using eBGP with ECMP (Equal-Cost Multi-Path)",
                "EVPN-VXLAN overlay with symmetric IRB (Integrated Routing and Bridging) and "
                "Anycast Gateway",
                "Distributed Stateful Security at the access edge via Pensando DPUs",
                "Jumbo frame MTU 9198 on all underlay links to support VXLAN overhead (50 bytes)",
            ],
            advantages=[
                "Eliminates Spanning Tree loops and blocked links across the fabric",
                "Sub-second convergence during link or spine failure via BGP BFD",
                "Network-wide microsegmentation with Group-Based Policies (GBP)",
            ],
            config_highlights=[
                "router bgp 65001\n  neighbor 10.0.0.1 remote-as 65000\n  address-family l2vpn "
                "evpn\n    neighbor 10.0.0.1 activate",
                "evpn\n  vni 10010\n    rd 10.255.255.1:10010\n    route-target export "
                "65000:10010\n    route-target import 65000:10010",
            ],
        )

    # Campus Collapsed Core / 3-Tier
    access_switch_count = max(2, (scale_switch_port_count + 47) // 48)
    ap_count = scale_ap_count

    return ArchitectureRecommendation(
        topology_type="collapsed_core_campus",
        title="High-Availability Multi-Gigabit Campus Network (Aruba ESP)",
        description="Enterprise campus architecture with redundant VSF/VSX Core switching, "
                    "multi-gigabit Class 8 PoE access layer, Wi-Fi 6E APs, and ClearPass Zero "
                    "Trust Network Access.",
        recommended_hardware=[
            {
                "role": "Core / Aggregation",
                "model": "Aruba CX 8360-16Y2C (VSX Pair)",
                "quantity": 2,
                "ports": "16x 25G SFP28 + 2x 100G QSFP28",
                "notes": "Active-Active dual-chassis VSX pairing with sub-50ms failover and "
                         "Multi-Active Detection",
            },
            {
                "role": "Access Layer",
                "model": "Aruba CX 6300M 48-port SmartRate Class 8 PoE",
                "quantity": access_switch_count,
                "ports": "48x 1G/2.5G/5G SmartRate PoE Class 8 (90W) + 4x 50G SFP56 Uplinks",
                "notes": "VSF Stacking up to 10 switches per stack, redundant hot-swappable power "
                         "supplies",
            },
            {
                "role": "Wireless APs",
                "model": "Aruba AP-635 (Wi-Fi 6E Tri-Band)",
                "quantity": ap_count,
                "ports": "2x 2.5G SmartRate Ethernet (Hitless Failover)",
                "notes": "Tri-band 2.4GHz / 5GHz / 6GHz with dedicated IoT BLE/Zigbee radio",
            },
            {
                "role": "Policy & NAC Engine",
                "model": "Aruba ClearPass Policy Manager (CPPM)",
                "quantity": 2,
                "ports": "High-Availability Cluster",
                "notes": "Dynamic 802.1X, MPSK, MAC-Auth, Profiling, and Downloadable User Roles "
                         "(DUR)",
            },
        ],
        key_design_principles=[
            "VSX dual-core with ISL (Inter-Switch Link) and Keepalive over dedicated management "
            "link",
            "Multi-Chassis LAG (MC-LAG) from access VSF stacks to Core VSX pair (zero blocked STP "
            "links)",
            "Dynamic Segmentation: Access switches dynamically tunnel untrusted traffic to Central "
            "Gateways",
            "PoE Class 8 90W budget planned to support full 802.3bt tri-band APs without radio "
            "power throttling",
        ],
        advantages=[
            "Complete hardware redundancy at Core, Stacking, Power Supply, and Uplink layers",
            "Hitless AP failover and client roaming with 6GHz clean spectrum",
            "Zero Trust policy enforcement based on identity, device posture, and role",
        ],
        config_highlights=[
            "vsx\n  inter-switch-link lag 100\n  keepalive peer 192.168.1.2 source 192.168.1.1 vrf "
            "mgmt\n  role active",
            "interface lag 1\n  vsx-sync\n  vlan trunk allowed all\n  lacp mode active",
        ],
    )


def format_architecture_recommendation_markdown(rec: ArchitectureRecommendation) -> str:
    """Format architecture recommendation as structured Markdown documentation."""
    lines = [
        f"## 🏛️ Network Architecture Blueprint: {rec.title}",
        f"**Topology Type:** `{rec.topology_type}`",
        f"\n{rec.description}\n",
        "### 📦 Recommended Bill of Materials (BOM) & Hardware Sizing",
    ]

    for hw in rec.recommended_hardware:
        lines.append(f"• **{hw['role']}**: **{hw['model']}** (Qty: {hw['quantity']})")
        lines.append(f"  - *Interfaces:* {hw['ports']}")
        lines.append(f"  - *Role Notes:* {hw['notes']}")

    lines.append("\n### 📐 Key Architecture & Design Principles")
    for princ in rec.key_design_principles:
        lines.append(f"1. **{princ}**")

    lines.append("\n### ⭐ Solution Advantages")
    for adv in rec.advantages:
        lines.append(f"• {adv}")

    if rec.config_highlights:
        lines.append("\n### ⚙️ Core Configuration Snippets")
        for snip in rec.config_highlights:
            lines.append(f"```cli\n{snip}\n```")

    return "\n".join(lines)
