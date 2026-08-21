"""Automated network troubleshooting reasoner and root cause analyzer."""

from __future__ import annotations

import re
import uuid

from hpe_networking_mcp.pipeline.reasoning.models import (
    PlanStep,
    ProblemCategory,
    ReasoningPlan,
    RemediationAction,
    RootCauseHypothesis,
)


def classify_troubleshooting_intent(query: str) -> ProblemCategory:
    """Classify user symptom or question into a specific network problem category."""
    q = query.lower()

    if any(k in q for k in ["auth", "802.1x", "radius", "clearpass", "eap", "mpsk",
                            "passphrase", "login fail", "mac auth"]):
        return ProblemCategory.WIRELESS_CLIENT_AUTH

    if any(k in q for k in ["poe", "power", "watt", "inline power", "pd class",
                            "class 4", "class 6", "class 8"]):
        return ProblemCategory.POE_BUDGET

    if any(k in q for k in ["dhcp", "ip address", "169.254", "apipa", "lease",
                            "ip exhaustion", "no ip"]):
        return ProblemCategory.DHCP_IP_AM

    if any(k in q for k in ["stp", "spanning-tree", "loop", "broadcast storm",
                            "topology change", "root bridge"]):
        return ProblemCategory.STP_TOPOLOGY_LOOP

    if any(k in q for k in ["roam", "sticky", "snr", "rssi", "disconnect", "drops",
                            "coverage", "rf", "channel utilization"]):
        return ProblemCategory.ROAMING_RF_HEALTH

    if any(k in q for k in ["port flap", "crc", "fcs", "duplex", "link down",
                            "errors on port", "cable"]):
        return ProblemCategory.WIRED_PORT_HEALTH

    if any(k in q for k in ["gateway", "vpnc", "cluster", "vrrp", "split-brain",
                            "tunnel down", "ipsec"]):
        return ProblemCategory.GATEWAY_VPNC_CLUSTER

    if any(k in q for k in ["firmware", "upgrade", "compliance", "image", "os version"]):
        return ProblemCategory.FIRMWARE_COMPLIANCE

    return ProblemCategory.GENERAL_NETWORK_HEALTH


def extract_target_entities(query: str) -> dict[str, str | None]:
    """Extract MAC addresses, serial numbers, IP addresses, port identifiers, and
    site names from query."""
    mac_match = re.search(
        r"([0-9a-fA-F]{2}[:-]){5}([0-9a-fA-F]{2})"
        r"|([0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4})",
        query,
    )
    mac = mac_match.group(0) if mac_match else None

    serial_match = re.search(r"\b([A-Z0-9]{10,12})\b", query)
    serial = serial_match.group(1) if serial_match else None

    ip_match = re.search(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b", query)
    ip = ip_match.group(0) if ip_match else None

    port_match = re.search(
        r"\b([0-9]+/[0-9]+/[0-9]+|[0-9]+/[0-9]+|ge-[0-9]/[0-9]/[0-9]|1/[0-9]+)\b",
        query,
        re.IGNORECASE,
    )
    port = port_match.group(1) if port_match else None

    return {
        "mac": mac,
        "serial": serial,
        "ip": ip,
        "port": port,
    }


def create_troubleshooting_plan(query: str, site_id: str | None = None) -> ReasoningPlan:
    """Generate a structured diagnostic plan and step sequence for investigating a network issue."""
    category = classify_troubleshooting_intent(query)
    entities = extract_target_entities(query)
    plan_id = f"plan-{uuid.uuid4().hex[:8]}"

    plan = ReasoningPlan(
        plan_id=plan_id,
        goal=query,
        category=category,
        target_device=entities.get("serial"),
        target_client=entities.get("mac"),
        target_site=site_id,
    )

    if category == ProblemCategory.WIRELESS_CLIENT_AUTH:
        plan.steps = [
            PlanStep(
                step_id="step-1",
                title="Inspect client connectivity and association status",
                tool_name="get_client_detail" if entities.get("mac") else "list_clients",
                tool_args={"mac": entities.get("mac")} if entities.get("mac") else {"limit": 20},
                rationale=(
                    "Check if the client is associated to an AP, signal strength, "
                    "SSID, and assigned VLAN."
                ),
            ),
            PlanStep(
                step_id="step-2",
                title="Verify AAA server and authentication profile bindings",
                tool_name="list_aaa_profiles",
                tool_args={},
                rationale=(
                    "Confirm RADIUS / ClearPass server reachability, dead server "
                    "detection, and shared secret consistency."
                ),
            ),
            PlanStep(
                step_id="step-3",
                title="Run AAA authentication test from AP/gateway to RADIUS server",
                tool_name="test_aaa_authentication",
                tool_args={"username": "test_user"},
                rationale=(
                    "Isolate whether auth failure is caused by credential rejection, "
                    "EAP certificate validation, or RADIUS timeout."
                ),
            ),
        ]
        plan.hypotheses = [
            RootCauseHypothesis(
                hypothesis_id="hyp-auth-1",
                category=ProblemCategory.WIRELESS_CLIENT_AUTH,
                confidence_score=0.85,
                summary=(
                    "Client 802.1X supplicant certificate or credential mismatch "
                    "against RADIUS/ClearPass policy"
                ),
                evidence=[
                    "Client unable to complete EAP transaction",
                    "RADIUS server rejects or discards Access-Request packets",
                ],
                remediation_actions=[
                    RemediationAction(
                        action_id="act-auth-1",
                        title="Check ClearPass / RADIUS Access Tracker logs",
                        description=(
                            "Review ClearPass Policy Manager Access Tracker for exact "
                            "alert code (e.g., EAP-TLS unknown CA, MSCHAPv2 bad "
                            "password, user account locked)."
                        ),
                        suggested_cli_commands=[
                            "show aaa authentication-server radius statistics",
                            "show dot1x clients detail",
                        ],
                    ),
                    RemediationAction(
                        action_id="act-auth-2",
                        title="Disconnect stuck client session to force re-authentication",
                        description=(
                            "Send CoA disconnect or use disconnect_client tool to "
                            "clear stale session state."
                        ),
                        tool_name="disconnect_client",
                        tool_args={"client_mac": entities.get("mac") or "AA:BB:CC:DD:EE:FF"},
                        is_destructive=True,
                    ),
                ],
            )
        ]

    elif category == ProblemCategory.POE_BUDGET:
        port_id = entities.get("port") or "1/1/1"
        serial = entities.get("serial")
        plan.steps = [
            PlanStep(
                step_id="step-1",
                title="Check switch interface PoE power status and consumption",
                tool_name="get_switch_poe_details",
                tool_args={"serial": serial, "interface": port_id} if serial else {},
                rationale=(
                    "Inspect allocated wattage, PD power class, priority, and "
                    "remaining switch power supply budget."
                ),
            ),
            PlanStep(
                step_id="step-2",
                title="Inspect hardware trends and power supply health",
                tool_name="get_switch_hardware_trends",
                tool_args={"serial": serial} if serial else {},
                rationale=(
                    "Verify power supply unit (PSU) redundancy and aggregate PoE draw over time."
                ),
            ),
        ]
        plan.hypotheses = [
            RootCauseHypothesis(
                hypothesis_id="hyp-poe-1",
                category=ProblemCategory.POE_BUDGET,
                confidence_score=0.90,
                summary=(
                    "PoE power budget exhausted on switch or port negotiation capped "
                    "at 802.3af (15.4W) instead of 802.3bt (60W/90W)"
                ),
                evidence=[
                    "PD requires Class 6 (60W) or Class 8 (90W) but switch port "
                    "negotiated 802.3at (30W)",
                    "LLDP-MED power negotiation disabled or delayed",
                ],
                remediation_actions=[
                    RemediationAction(
                        action_id="act-poe-1",
                        title=(
                            "Configure PoE allocated power priority and LLDP dot3 power negotiation"
                        ),
                        description=(
                            "Ensure 'lldp dot3' is enabled to negotiate full 802.3bt "
                            "Class 6/8 power budget via LLDP."
                        ),
                        suggested_cli_commands=[
                            f"interface {port_id}",
                            "  poe-power allocated-by lldp",
                            "  poe-power priority high",
                        ],
                    ),
                    RemediationAction(
                        action_id="act-poe-2",
                        title="Power cycle PoE port (PoE bounce)",
                        description=(
                            "Bounce power to re-trigger hardware signature detection "
                            "and LLDP power negotiation."
                        ),
                        tool_name="poe_bounce",
                        tool_args={"serial": serial or "SWITCH_SERIAL", "port": port_id},
                        is_destructive=True,
                    ),
                ],
            )
        ]

    elif category == ProblemCategory.DHCP_IP_AM:
        plan.steps = [
            PlanStep(
                step_id="step-1",
                title="Check client IP allocation and DHCP lease status",
                tool_name="list_clients",
                tool_args={"limit": 25},
                rationale=(
                    "Identify whether multiple clients on the same VLAN are receiving "
                    "169.254.x.x (APIPA) addresses."
                ),
            ),
            PlanStep(
                step_id="step-2",
                title="Verify DHCP relay helper addresses on switch/gateway SVI",
                tool_name="get_switch_vlans",
                tool_args={"serial": entities.get("serial")} if entities.get("serial") else {},
                rationale=(
                    "Confirm 'ip helper-address' / 'dhcp-server' is properly "
                    "configured on the VLAN interface."
                ),
            ),
        ]
        plan.hypotheses = [
            RootCauseHypothesis(
                hypothesis_id="hyp-dhcp-1",
                category=ProblemCategory.DHCP_IP_AM,
                confidence_score=0.88,
                summary=(
                    "DHCP pool exhaustion or missing IP Helper / DHCP Relay address "
                    "on the client VLAN interface"
                ),
                evidence=[
                    "Client self-assigned 169.254.x.x link-local address",
                    "DHCP Discover broadcasts not reaching upstream DHCP server",
                ],
                remediation_actions=[
                    RemediationAction(
                        action_id="act-dhcp-1",
                        title="Configure DHCP Relay Helper Address on VLAN SVI",
                        description="Add upstream DHCP server IP to the VLAN SVI interface.",
                        suggested_cli_commands=[
                            "interface vlan <VLAN_ID>",
                            "  ip helper-address <DHCP_SERVER_IP>",
                            "  ip dhcp-relay",
                        ],
                    )
                ],
            )
        ]

    elif category == ProblemCategory.STP_TOPOLOGY_LOOP:
        plan.steps = [
            PlanStep(
                step_id="step-1",
                title="Inspect switch interface error counters and STP state",
                tool_name="list_switch_interfaces",
                tool_args={"serial": entities.get("serial")} if entities.get("serial") else {},
                rationale=(
                    "Check for blocked ports, topology change notifications (TCNs), "
                    "and broadcast storm counters."
                ),
            ),
            PlanStep(
                step_id="step-2",
                title="Verify root bridge priority and RPVST+ / MSTP configuration",
                tool_name="get_switch_hardware_trends",
                tool_args={"serial": entities.get("serial")} if entities.get("serial") else {},
                rationale=(
                    "Check CPU spikes caused by packet storm and confirm "
                    "deterministic root bridge priority (0 or 4096)."
                ),
            ),
        ]
        plan.hypotheses = [
            RootCauseHypothesis(
                hypothesis_id="hyp-stp-1",
                category=ProblemCategory.STP_TOPOLOGY_LOOP,
                confidence_score=0.85,
                summary=(
                    "Spanning Tree loop or non-deterministic root bridge election "
                    "causing broadcast storm and MAC flapping"
                ),
                evidence=[
                    "High CPU utilization on switch control plane",
                    "Continuous STP Topology Change Notifications (TCNs)",
                ],
                remediation_actions=[
                    RemediationAction(
                        action_id="act-stp-1",
                        title=(
                            "Set deterministic STP Root Bridge priority and enable "
                            "BPDU guard on edge ports"
                        ),
                        description=(
                            "Enforce Core switch as Root (priority 0) and Aggregation "
                            "as Secondary (priority 4096), with bpdu-guard on all "
                            "client access ports."
                        ),
                        suggested_cli_commands=[
                            "spanning-tree priority 0",
                            "spanning-tree mode rpvst",
                            "interface 1/1/1-1/1/48",
                            "  spanning-tree bpdu-guard",
                            "  spanning-tree port-type admin-edge",
                        ],
                    )
                ],
            )
        ]

    else:
        plan.steps = [
            PlanStep(
                step_id="step-1",
                title="Run tenant and site health diagnostic check",
                tool_name="get_tenant_health",
                tool_args={},
                rationale=(
                    "Query overall connectivity, AP/switch status, client counts, "
                    "and major alert summaries."
                ),
            ),
            PlanStep(
                step_id="step-2",
                title="Check active device alarms and configuration sync status",
                tool_name="list_devices",
                tool_args={"limit": 50},
                rationale=(
                    "Identify offline devices, configuration out-of-sync warnings, "
                    "or hardware faults."
                ),
            ),
        ]
        plan.hypotheses = [
            RootCauseHypothesis(
                hypothesis_id="hyp-gen-1",
                category=ProblemCategory.GENERAL_NETWORK_HEALTH,
                confidence_score=0.70,
                summary="General connectivity or device synchronization degradation",
                evidence=["Alerts detected in monitoring telemetry"],
                remediation_actions=[
                    RemediationAction(
                        action_id="act-gen-1",
                        title="Review device telemetry and audit log",
                        description=(
                            "Inspect configuration changes and system events in Central audit log."
                        ),
                        tool_name="list_audit_logs",
                        tool_args={"limit": 20},
                    )
                ],
            )
        ]

    return plan


def format_troubleshooting_report(plan: ReasoningPlan) -> str:
    """Format reasoning plan and root cause diagnosis as a clean Markdown report."""
    lines = [
        f"## 🩺 Network Diagnostic & Root Cause Reasoning: "
        f"{plan.category.value.replace('_', ' ').title()}",
        f"**Goal / Symptom:** {plan.goal}",
    ]
    if plan.target_device:
        lines.append(f"**Target Device:** `{plan.target_device}`")
    if plan.target_client:
        lines.append(f"**Target Client:** `{plan.target_client}`")
    if plan.target_site:
        lines.append(f"**Target Site:** `{plan.target_site}`")

    lines.append("\n### 📋 Recommended Diagnostic Steps")
    for s in plan.steps:
        tool_str = f" → Tool: `{s.tool_name}`" if s.tool_name else ""
        lines.append(f"- **{s.step_id}**: {s.title}{tool_str}")
        lines.append(f"  *Rationale:* {s.rationale}")

    if plan.hypotheses:
        lines.append("\n### 🔍 Probable Root Causes & Hypotheses")
        for h in plan.hypotheses:
            conf_pct = int(h.confidence_score * 100)
            lines.append(f"#### Hypothesis ({conf_pct}% Confidence): {h.summary}")
            if h.evidence:
                lines.append("**Observed Indicators / Evidence:**")
                for ev in h.evidence:
                    lines.append(f"• {ev}")
            if h.remediation_actions:
                lines.append("\n**Suggested Remediation Actions:**")
                for act in h.remediation_actions:
                    destr = " ⚠️ [DESTRUCTIVE / REQUIRES CONFIRMATION]" if act.is_destructive else ""
                    lines.append(f"1. **{act.title}**{destr}: {act.description}")
                    if act.suggested_cli_commands:
                        lines.append("   ```cli")
                        for cmd in act.suggested_cli_commands:
                            lines.append(f"   {cmd}")
                        lines.append("   ```")

    return "\n".join(lines)
