"""Structured hardware specifications catalog and exact lookup for Aruba and Juniper hardware."""

from __future__ import annotations

import re
from typing import Any

# Authoritative datasheet hardware specifications for switches and APs
HARDWARE_CATALOG: dict[str, dict[str, Any]] = {
    "cx6300": {
        "model": "Aruba CX 6300 Switch Series (6300F / 6300M)",
        "vendor": "Aruba / HPE",
        "family": "AOS-CX Access & Aggregation Switches",
        "switching_capacity": "Up to 880 Gbps (standard) / 1,760 Gbps (modular chassis)",
        "throughput": "Up to 660 Mpps / 1,310 Mpps",
        "stacking": "Virtual Switching Framework (VSF) up to 10 switches; up to 400 Gbps stacking bandwidth",
        "uplinks": "Built-in 4x 1/10/25/50GbE SFP56 or modular 100GbE QSFP28",
        "access_ports": "24 or 48 ports: 10/100/1000BASE-T, SmartRate (1/2.5/5GbE), or 1G/10G SFP+",
        "poe": "IEEE 802.3bt Class 8 (up to 90W/port PoE) with modular dynamic power supplies",
        "architecture": "Aruba Gen7 ASIC, AOS-CX modular OS, embedded Network Analytics Engine (NAE)",
        "layer3_features": "BGP, EVPN-VXLAN, OSPF, VRF-lite / multiple VRFs, Dynamic Segmentation",
    },
    "cx6200": {
        "model": "Aruba CX 6200 Switch Series (6200F / 6200M)",
        "vendor": "Aruba / HPE",
        "family": "AOS-CX Campus Access Switches",
        "switching_capacity": "Up to 336 Gbps",
        "throughput": "Up to 249 Mpps",
        "stacking": "Virtual Switching Framework (VSF) up to 8 switches",
        "uplinks": "4x 1/10GbE SFP+ fixed uplinks",
        "access_ports": "24 or 48 ports: 10/100/1000BASE-T or SmartRate multi-gigabit",
        "poe": "IEEE 802.3at Class 4 (up to 30W/port) and Class 6 (up to 60W/port) PoE",
        "architecture": "Aruba Gen7 ASIC, AOS-CX modular OS",
        "layer3_features": "Static routing, OSPFv2/v3, static VXLAN, ACLs",
    },
    "cx6100": {
        "model": "Aruba CX 6100 Switch Series",
        "vendor": "Aruba / HPE",
        "family": "AOS-CX Entry Campus Access Switches",
        "switching_capacity": "Up to 176 Gbps",
        "throughput": "Up to 98.6 Mpps",
        "stacking": "Standalone (no VSF stacking)",
        "uplinks": "4x 1/10GbE SFP+ (or 2x SFP on 12G model)",
        "access_ports": "12, 24, or 48 ports: 10/100/1000BASE-T",
        "poe": "IEEE 802.3at Class 4 PoE (up to 370W total budget)",
        "architecture": "AOS-CX modular OS, enterprise Class 2 Layer 3 static routing",
        "layer3_features": "Layer 2 switching with static IPv4/IPv6 routing",
    },
    "cx6000": {
        "model": "Aruba CX 6000 Switch Series",
        "vendor": "Aruba / HPE",
        "family": "AOS-CX Entry Layer 2 Access Switches",
        "switching_capacity": "Up to 104 Gbps",
        "throughput": "Up to 77.3 Mpps",
        "stacking": "Standalone",
        "uplinks": "4x 1GbE SFP ports",
        "access_ports": "12, 24, or 48 ports 10/100/1000BASE-T",
        "poe": "IEEE 802.3at Class 4 PoE (up to 370W budget)",
        "architecture": "AOS-CX OS, compact and fanless models available",
        "layer3_features": "Layer 2 with static routing",
    },
    "cx6400": {
        "model": "Aruba CX 6400 Switch Series (6405 / 6410 Chassis)",
        "vendor": "Aruba / HPE",
        "family": "AOS-CX Modular Core, Aggregation & Access Chassis",
        "switching_capacity": "Up to 28 Tbps bidirectional switching capacity",
        "throughput": "Up to 11.4 Bpps throughput",
        "stacking": "VSX (Virtual Switching Extension) High Availability dual-chassis pair",
        "uplinks": "Modular line cards supporting 1GbE, 10GbE, 25GbE, 40GbE, 50GbE, and 100GbE",
        "access_ports": "Up to 480 ports of 10/100/1000BASE-T, SmartRate multi-gigabit, or high-density SFP56",
        "poe": "High-density IEEE 802.3bt Class 8 (up to 90W/port PoE) with N+N power redundancy",
        "architecture": "Aruba Gen7 ASIC, AOS-CX carrier-class architecture with VSX live-sync",
        "layer3_features": "Full Layer 3 routing: BGP, EVPN-VXLAN, OSPF, IS-IS, VRFs, Dynamic Segmentation",
    },
    "cx8360": {
        "model": "Aruba CX 8360 Switch Series (8360-32Y4C / 8360-16Y2C / 8360-48XT4C)",
        "vendor": "Aruba / HPE",
        "family": "AOS-CX Campus Core & Data Center Aggregation",
        "switching_capacity": "Up to 2.4 Tbps (8360-32Y4C) / 4.8 Tbps system capacity",
        "throughput": "Up to 1,145 Mpps throughput",
        "stacking": "VSX (Virtual Switching Extension) live-sync active-active multichassis LAG",
        "uplinks": "4x 100GbE (QSFP28) / 40GbE (QSFP+) uplinks",
        "access_ports": "32x 10G/25GbE SFP28, 48x 10GBASE-T, or 12x 40/100G QSFP28 ports",
        "poe": "Non-PoE (Core/Agg/Data Center switch)",
        "architecture": "AOS-CX modular OS with wire-speed Layer 2 and Layer 3 performance, NAE",
        "layer3_features": "BGP, EVPN-VXLAN distributed overlay, OSPF, IS-IS, VRF-lite, Dynamic Segmentation",
    },
    "cx8325": {
        "model": "Aruba CX 8325 Switch Series (8325-48Y8C / 8325-32C)",
        "vendor": "Aruba / HPE",
        "family": "AOS-CX Campus Core & High-Density Aggregation",
        "switching_capacity": "Up to 6.4 Tbps switching capacity",
        "throughput": "Up to 2,000 Mpps throughput",
        "stacking": "VSX (Virtual Switching Extension) High Availability multichassis pair",
        "uplinks": "8x 40G/100GbE QSFP28 ports (or 32x 40/100GbE on 32C)",
        "access_ports": "48x 1/10/25GbE SFP28 ports",
        "poe": "Non-PoE (Core/Data Center)",
        "architecture": "High-density 1U switch with redundant hot-swappable power supplies and fans",
        "layer3_features": "BGP, EVPN-VXLAN, OSPF, multi-VRF, RoCEv2, DCB/PFC",
    },
    "cx10000": {
        "model": "Aruba CX 10000 Switch Series with AMD Pensando DPU",
        "vendor": "Aruba / HPE",
        "family": "Distributed Services Switch (SmartSwitch)",
        "switching_capacity": "3.2 Tbps switching capacity with 800 Gbps stateful firewall / telemetry throughput",
        "throughput": "Up to 2,000 Mpps",
        "stacking": "VSX active-active HA multichassis",
        "uplinks": "6x 40G/100GbE QSFP28 uplinks",
        "access_ports": "48x 10G/25GbE SFP28 ports",
        "poe": "Non-PoE (Distributed Services DC/Campus)",
        "architecture": "AOS-CX integrated with AMD Pensando Elba DPU for stateful microsegmentation and NAT",
        "layer3_features": "Stateful Firewall, Zero Trust microsegmentation, EVPN-VXLAN, BGP, telemetry export",
    },
    "ex4400": {
        "model": "Juniper Networks EX4400 Switch Series (EX4400-48P / EX4400-24P / EX4400-48F)",
        "vendor": "Juniper Networks / Mist AI",
        "family": "Cloud-Ready AI-Powered Access Switch",
        "switching_capacity": "Up to 704 Gbps switching capacity",
        "throughput": "Up to 523 Mpps throughput",
        "stacking": "Virtual Chassis (VC) up to 10 switches; 2x 100GbE dedicated virtual chassis ports (400 Gbps VC)",
        "uplinks": "Modular uplink extension options: 4x 10G/25GbE SFP28 or 1x 100GbE QSFP28",
        "access_ports": "24 or 48 ports: 10/100/1000BASE-T, Multi-Gigabit (100M/1G/2.5G/5G/10GbE), or SFP",
        "poe": "IEEE 802.3bt Class 8 (up to 90W/port PoE) with PoE++ and Fast/Perpetual PoE",
        "architecture": "Junos OS driven by Mist AI, native telemetry streaming, hardware-based Flow-based telemetry",
        "layer3_features": "EVPN-VXLAN to the access edge, Group-Based Policies (GBP), OSPF, BGP, VRF, MACsec AES-256",
    },
    "ex4100": {
        "model": "Juniper Networks EX4100 Switch Series (EX4100 / EX4100-F)",
        "vendor": "Juniper Networks / Mist AI",
        "family": "Cloud-Native Campus Access Switch",
        "switching_capacity": "Up to 336 Gbps",
        "throughput": "Up to 250 Mpps",
        "stacking": "Virtual Chassis (VC) up to 10 switches using 4x 10GbE/25GbE SFP28 ports",
        "uplinks": "4x 10GbE or 4x 25GbE SFP28 fixed uplinks",
        "access_ports": "24 or 48 ports 10/100/1000BASE-T (or Multi-Gigabit on EX4100-Multi-Gig models)",
        "poe": "IEEE 802.3bt Class 6/8 (up to 30W/60W/90W PoE)",
        "architecture": "Junos OS with native Mist AI cloud telemetry and zero-touch provisioning",
        "layer3_features": "EVPN-VXLAN, OSPF, BGP, static routing, Group-Based Policy (GBP), MACsec",
    },
    "ex2300": {
        "model": "Juniper Networks EX2300 Switch Series",
        "vendor": "Juniper Networks / Mist AI",
        "family": "Compact Entry Campus Access Switch",
        "switching_capacity": "Up to 128 Gbps",
        "throughput": "Up to 95 Mpps",
        "stacking": "Virtual Chassis (VC) up to 4 switches",
        "uplinks": "4x 1GbE/10GbE SFP+ fixed uplinks",
        "access_ports": "12, 24, or 48 ports 10/100/1000BASE-T",
        "poe": "IEEE 802.3at PoE+ (up to 30W/port)",
        "architecture": "Junos OS, compact and fanless models available for quiet branch deployments",
        "layer3_features": "Layer 2 switching, static routing, RIP, OSPFv2",
    },
    "ex4650": {
        "model": "Juniper Networks EX4650 Switch Series",
        "vendor": "Juniper Networks / Mist AI",
        "family": "High-Density Campus Distribution / Aggregation & Core",
        "switching_capacity": "Up to 2 Tbps switching capacity",
        "throughput": "Up to 1.49 Bpps",
        "stacking": "Virtual Chassis (VC) up to 2 switches or EVPN-VXLAN multi-homing (ESI-LAG)",
        "uplinks": "8x 40GbE / 100GbE QSFP28 uplinks",
        "access_ports": "48x 10GbE/25GbE SFP28 ports",
        "poe": "Non-PoE (Aggregation/Core)",
        "architecture": "Junos OS with Mist AI cloud telemetry, redundant hot-swappable power supplies and fans",
        "layer3_features": "EVPN-VXLAN, BGP, OSPF, IS-IS, VRF, MACsec AES-256",
    },
    "ap505": {
        "model": "Aruba AP-505 Campus Access Point",
        "vendor": "Aruba / HPE",
        "family": "Wi-Fi 6 (802.11ax) Compact Dual-Radio Campus AP",
        "radios": "Dual-radio 2x2:2 MU-MIMO on both 2.4GHz and 5GHz",
        "ethernet_ports": "1x GbE (10/100/1000BASE-T) Ethernet uplink port",
        "poe": "IEEE 802.3af PoE (Class 3, reduced-feature mode) or 802.3at PoE+ (Class 4, full function)",
        "features": "Compact entry-level Wi-Fi 6 AP, Bluetooth 5 / Zigbee IoT radio, Aruba Central / Mobility Conductor managed",
    },
    "ap515": {
        "model": "Aruba AP-515 Campus Access Point",
        "vendor": "Aruba / HPE",
        "family": "Wi-Fi 6 (802.11ax) Dual-Radio Campus AP",
        "radios": "Dual-radio 4x4:4 MU-MIMO on both 2.4GHz and 5GHz",
        "ethernet_ports": "1x SmartRate 1G/2.5GbE port + 1x GbE (10/100/1000BASE-T) port, plus USB for IoT dongles",
        "poe": "IEEE 802.3at PoE+ (Class 4) or 802.3bt PoE (Class 5) for full function",
        "features": "Mid-range high-capacity Wi-Fi 6 AP, Bluetooth 5 / Zigbee IoT radio, USB port for third-party IoT modules",
    },
    "ap535": {
        "model": "Aruba AP-535 Campus Access Point",
        "vendor": "Aruba / HPE",
        "family": "Wi-Fi 6 (802.11ax) Dual-Radio High-Performance Campus AP",
        "radios": "Dual-radio 4x4:4 MU-MIMO, flexible radio can run 2.4GHz+5GHz or dual 5GHz for high-density deployments",
        "ethernet_ports": "1x SmartRate 1G/2.5GbE Ethernet uplink port",
        "poe": "IEEE 802.3at PoE+ (Class 4) or 802.3bt PoE (Class 5/6) for full function",
        "features": "High-density flexible-radio design, Bluetooth 5 / Zigbee IoT radio, USB port for IoT dongles",
    },
    "ap545": {
        "model": "Aruba AP-545 Campus Access Point",
        "vendor": "Aruba / HPE",
        "family": "Wi-Fi 6 (802.11ax) Tri-Radio Campus AP with Security Radio",
        "radios": "Tri-radio: 2.4GHz (4x4) + 5GHz (4x4) client radios plus a dedicated third radio usable for scanning/security or as a client-serving radio",
        "ethernet_ports": "1x SmartRate 1G/2.5GbE port + 1x GbE (10/100/1000BASE-T) port",
        "poe": "IEEE 802.3at PoE+ (Class 4) or 802.3bt PoE (Class 5/6) for full function",
        "features": "High-performance tri-radio AP, integrated WIDS/WIPS-capable third radio, Bluetooth 5 / Zigbee IoT radio",
    },
    "ap555": {
        "model": "Aruba AP-555 Campus Access Point",
        "vendor": "Aruba / HPE",
        "family": "Wi-Fi 6 (802.11ax) Tri-Radio Flagship Campus AP",
        "radios": "Tri-radio: 2.4GHz (4x4) + 5GHz (4x4) client radios plus a dedicated full-time security/spectrum-analysis radio (4x4)",
        "ethernet_ports": "2x SmartRate 1G/2.5GbE Ethernet ports (uplink / link aggregation)",
        "poe": "IEEE 802.3bt PoE (Class 6, full function) or 802.3at PoE+ (Class 4, reduced-feature mode)",
        "features": "Aruba's highest-throughput Wi-Fi 6 indoor AP, dedicated always-on security radio, Bluetooth 5 / Zigbee IoT radio",
    },
    "ap635": {
        "model": "Aruba AP-635 Campus Access Point",
        "vendor": "Aruba / HPE",
        "family": "Wi-Fi 6E (802.11ax) Tri-Radio Campus AP",
        "radios": "Tri-radio 2.4GHz (2x2), 5GHz (2x2), and 6GHz (2x2) with up to 3.9 Gbps combined peak rate",
        "ethernet_ports": "2x SmartRate 1G/2.5GbE Ethernet ports (uplink / failover)",
        "poe": "IEEE 802.3at PoE (Class 4) or 802.3bt PoE (Class 5)",
        "features": "Ultra-Tri-Band filtering, GPS receiver for location, Bluetooth 5 & Zigbee IoT radios",
    },
    "ap45": {
        "model": "Juniper Mist AP45 Access Point",
        "vendor": "Juniper Networks / Mist AI",
        "family": "Wi-Fi 6E (802.11ax) Tri-Band Enterprise AP with AI Engine",
        "radios": "Tri-band 2.4GHz (4x4), 5GHz (4x4), and 6GHz (4x4) with dedicated 4th scanning radio",
        "ethernet_ports": "1x 100M/1G/2.5G/5GbE Multi-Gigabit Ethernet uplink + 1x 1GbE auxiliary port",
        "poe": "IEEE 802.3bt Class 5 PoE or 802.3at PoE+",
        "features": "Virtual BLE (vBLE) 16-element antenna array, Mist AI Marvis automated RF optimization, dynamic packet capture",
    },
}

_MODEL_ALIASES: dict[str, str] = {
    "cx6300": "cx6300",
    "6300": "cx6300",
    "cx6300f": "cx6300",
    "cx6300m": "cx6300",
    "6300f": "cx6300",
    "6300m": "cx6300",
    "cx6200": "cx6200",
    "6200": "cx6200",
    "6200f": "cx6200",
    "6200m": "cx6200",
    "cx6100": "cx6100",
    "6100": "cx6100",
    "cx6000": "cx6000",
    "6000": "cx6000",
    "cx6400": "cx6400",
    "6400": "cx6400",
    "6405": "cx6400",
    "6410": "cx6400",
    "cx8360": "cx8360",
    "8360": "cx8360",
    "cx8325": "cx8325",
    "8325": "cx8325",
    "cx8320": "cx8325",
    "8320": "cx8325",
    "cx10000": "cx10000",
    "10000": "cx10000",
    "ex4400": "ex4400",
    "4400": "ex4400",
    "ex4100": "ex4100",
    "4100": "ex4100",
    "ex2300": "ex2300",
    "2300": "ex2300",
    "ex4650": "ex4650",
    "4650": "ex4650",
    "ap505": "ap505",
    "505": "ap505",
    "ap-505": "ap505",
    "ap515": "ap515",
    "515": "ap515",
    "ap-515": "ap515",
    "ap535": "ap535",
    "535": "ap535",
    "ap-535": "ap535",
    "ap545": "ap545",
    "545": "ap545",
    "ap-545": "ap545",
    "ap555": "ap555",
    "555": "ap555",
    "ap-555": "ap555",
    "ap635": "ap635",
    "635": "ap635",
    "ap-635": "ap635",
    "ap45": "ap45",
    "ap-45": "ap45",
}

_HARDWARE_QUERY_HINTS = {
    "spec",
    "specs",
    "specification",
    "specifications",
    "datasheet",
    "capacity",
    "throughput",
    "stacking",
    "vsf",
    "uplink",
    "uplinks",
    "poe",
    "smartrate",
    "ports",
    "hardware",
}


def detect_hardware_query(question: str) -> str | None:
    """Detect if a question is asking for hardware specifications of a specific model."""
    tokens = {
        tok.strip(".,:;?!()[]{}\"'").lower()
        for tok in question.replace("/", " ").replace("-", " ").split()
    }
    # Check if there is any hardware intent hint
    has_spec_intent = bool(tokens & _HARDWARE_QUERY_HINTS)
    raw_clean = re.sub(r"[^a-zA-Z0-9]+", " ", question.lower()).split()

    for word in raw_clean:
        if word in _MODEL_ALIASES and (has_spec_intent or len(raw_clean) <= 3):
            return _MODEL_ALIASES[word]
    # Check 2-word combinations e.g. "cx 6300", "ex 4400", "ap 635"
    for i in range(len(raw_clean) - 1):
        combo = f"{raw_clean[i]}{raw_clean[i+1]}"
        if combo in _MODEL_ALIASES and (has_spec_intent or len(raw_clean) <= 4):
            return _MODEL_ALIASES[combo]

    return None


def get_hardware_specs(model_key: str) -> dict[str, Any] | None:
    """Return hardware specification record for model."""
    return HARDWARE_CATALOG.get(model_key)


def format_hardware_specs_markdown(model_key: str) -> str:
    """Render a comprehensive Markdown datasheet summary for the switch / AP."""
    spec = HARDWARE_CATALOG.get(model_key)
    if not spec:
        return f"Hardware specifications for '{model_key}' are not available in catalog."

    lines = [
        f"### {spec['model']}",
        f"**Vendor / Family:** {spec['vendor']} · {spec['family']}",
        "",
    ]
    if "switching_capacity" in spec:
        lines.extend([
            f"• **Switching Capacity:** {spec['switching_capacity']}",
            f"• **Throughput:** {spec['throughput']}",
            f"• **Stacking / HA:** {spec['stacking']}",
            f"• **Access Ports:** {spec['access_ports']}",
            f"• **Uplinks:** {spec['uplinks']}",
            f"• **PoE:** {spec['poe']}",
            f"• **Architecture:** {spec['architecture']}",
            f"• **Layer 3 & Security:** {spec['layer3_features']}",
        ])
    elif "radios" in spec:
        lines.extend([
            f"• **Radios:** {spec['radios']}",
            f"• **Ethernet Uplinks:** {spec['ethernet_ports']}",
            f"• **PoE Power:** {spec['poe']}",
            f"• **Key Features:** {spec['features']}",
        ])

    return "\n".join(lines)
