"""Unit tests for AI reasoning engines, adapters, and agent execution loop."""

from __future__ import annotations

import pytest

from hpe_networking_mcp.cli_client.ai import (
    ChatMessage,
    HeuristicReasoningEngine,
    MessageRole,
    OllamaAdapter,
    OpenAiAdapter,
    get_ai_backend,
)
from hpe_networking_mcp.cli_client.ai.agent_loop import AgentReasoningLoop
from hpe_networking_mcp.pipeline.reasoning import (
    ProblemCategory,
    classify_troubleshooting_intent,
    create_troubleshooting_plan,
    decompose_query,
    extract_target_entities,
    format_architecture_recommendation_markdown,
    format_migration_plan_markdown,
    format_troubleshooting_report,
    plan_migration,
    synthesize_architecture,
)


def test_classify_troubleshooting_intents():
    assert classify_troubleshooting_intent("Client cannot authenticate with 802.1X") == ProblemCategory.WIRELESS_CLIENT_AUTH
    assert classify_troubleshooting_intent("PoE budget exceeded on port 1/1/4") == ProblemCategory.POE_BUDGET
    assert classify_troubleshooting_intent("Client getting 169.254 APIPA address") == ProblemCategory.DHCP_IP_AM
    assert classify_troubleshooting_intent("Spanning tree loop detected, broadcast storm") == ProblemCategory.STP_TOPOLOGY_LOOP
    assert classify_troubleshooting_intent("Sticky client won't roam between APs") == ProblemCategory.ROAMING_RF_HEALTH
    assert classify_troubleshooting_intent("CRC errors and port flap on 1/1/1") == ProblemCategory.WIRED_PORT_HEALTH


def test_extract_target_entities():
    entities = extract_target_entities("Check client AA:BB:CC:DD:EE:FF on switch SG1234567890 port 1/1/24 with IP 10.1.20.55")
    assert entities["mac"] == "AA:BB:CC:DD:EE:FF"
    assert entities["serial"] == "SG1234567890"
    assert entities["ip"] == "10.1.20.55"
    assert entities["port"] == "1/1/24"


def test_create_troubleshooting_plan_and_format():
    plan = create_troubleshooting_plan("Investigate 802.1X auth failure for client 00:11:22:33:44:55")
    assert plan.category == ProblemCategory.WIRELESS_CLIENT_AUTH
    assert plan.target_client == "00:11:22:33:44:55"
    assert len(plan.steps) >= 2
    assert len(plan.hypotheses) >= 1

    report = format_troubleshooting_report(plan)
    assert "Wireless Client Auth" in report
    assert "00:11:22:33:44:55" in report
    assert "Recommended Diagnostic Steps" in report


def test_migration_planner_aos_s_and_cisco():
    plan_aoss = plan_migration("aos-s")
    assert plan_aoss["source_vendor"] == "ArubaOS-S / ProCurve"
    assert len(plan_aoss["phases"]) == 4
    assert len(plan_aoss["syntax_mappings"]) >= 3

    md_aoss = format_migration_plan_markdown(plan_aoss)
    assert "ArubaOS-S / ProCurve" in md_aoss
    assert "vlan trunk allowed" in md_aoss

    plan_cisco = plan_migration("cisco")
    assert plan_cisco["source_vendor"] == "Cisco IOS / IOS-XE"
    md_cisco = format_migration_plan_markdown(plan_cisco)
    assert "Cisco IOS / IOS-XE" in md_cisco


def test_network_architect_campus_and_datacenter():
    campus = synthesize_architecture(environment="campus", scale_ap_count=30, scale_switch_port_count=100)
    assert campus.topology_type == "collapsed_core_campus"
    assert len(campus.recommended_hardware) >= 3

    campus_md = format_architecture_recommendation_markdown(campus)
    assert "Aruba CX 8360" in campus_md
    assert "ClearPass" in campus_md

    dc = synthesize_architecture(environment="datacenter", require_evpn=True)
    assert dc.topology_type == "evpn_vxlan_fabric"
    dc_md = format_architecture_recommendation_markdown(dc)
    assert "EVPN-VXLAN" in dc_md
    assert "Pensando DPU" in dc_md


def test_query_decomposer():
    hw_plan = decompose_query("cx6300 specs and throughput")
    assert hw_plan.intent_summary == "Hardware datasheet specifications lookup"
    assert hw_plan.actions[0].tool_name == "ask_docs"

    diag_plan = decompose_query("draw a campus network diagram")
    assert "diagram" in diag_plan.intent_summary.lower()

    trouble_plan = decompose_query("why is client AA:BB:CC:DD:EE:FF unable to connect")
    assert trouble_plan.actions[0].tool_name == "get_client_detail"


@pytest.mark.anyio
async def test_heuristic_reasoning_engine():
    engine = HeuristicReasoningEngine()
    assert engine.name == "heuristic-expert"

    # Hardware specs
    resp_hw = await engine.complete([ChatMessage(role=MessageRole.USER, content="cx6300 specs")])
    assert "Aruba CX 6300" in resp_hw.content
    assert "880 Gbps" in resp_hw.content

    # Troubleshooting
    resp_tb = await engine.complete([ChatMessage(role=MessageRole.USER, content="client auth fail error")])
    assert "Network Diagnostic" in resp_tb.content
    assert len(resp_tb.tool_calls) >= 1

    # Migration
    resp_mg = await engine.complete([ChatMessage(role=MessageRole.USER, content="migrate cisco to cx")])
    assert "Migration Blueprint" in resp_mg.content

    # Design
    resp_ds = await engine.complete([ChatMessage(role=MessageRole.USER, content="architect spine-leaf evpn fabric")])
    assert "EVPN-VXLAN" in resp_ds.content


def test_get_ai_backend_factory():
    assert isinstance(get_ai_backend("heuristic"), HeuristicReasoningEngine)
    assert isinstance(get_ai_backend("openai"), OpenAiAdapter)
    assert isinstance(get_ai_backend("ollama"), OllamaAdapter)


@pytest.mark.anyio
async def test_agent_reasoning_loop_execution():
    class DummyTool:
        def __init__(self, name: str, description: str = ""):
            self.name = name
            self.description = description
            self.inputSchema = {"type": "object"}

    class DummySessionManager:
        def __init__(self):
            self.tools = {"list_clients": DummyTool("list_clients")}

        async def list_all_tools(self):
            return list(self.tools.values())

        async def call_tool(self, name: str, args: dict):
            return {"clients": [{"mac": "AA:BB:CC:DD:EE:FF", "status": "associated"}]}

    engine = HeuristicReasoningEngine()
    mgr = DummySessionManager()
    loop = AgentReasoningLoop(ai_backend=engine, session_manager=mgr, max_turns=3)

    steps = []
    async for step in loop.run("troubleshoot client auth issue"):
        steps.append(step)

    step_types = [s.step_type for s in steps]
    assert "thought" in step_types
    assert "tool_call" in step_types or "answer" in step_types
