"""Multi-step query planner and task decomposition engine."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlannedAction:
    step_num: int
    operation_type: str  # "discovery", "inspection", "verification", "synthesis"
    tool_name: str
    arguments: dict[str, Any]
    purpose: str
    depends_on: list[int] = field(default_factory=list)


@dataclass
class QueryExecutionPlan:
    original_query: str
    intent_summary: str
    estimated_complexity: str  # "simple", "moderate", "multi-step"
    actions: list[PlannedAction] = field(default_factory=list)


def decompose_query(query: str) -> QueryExecutionPlan:
    """Decompose natural language request into an optimal sequence of tool calls."""
    q = query.lower()

    # Hardware specs query
    if any(k in q for k in ["spec", "datasheet", "throughput", "capacity", "switching capacity", "poe budget of 6300", "stacking limit"]):
        return QueryExecutionPlan(
            original_query=query,
            intent_summary="Hardware datasheet specifications lookup",
            estimated_complexity="simple",
            actions=[
                PlannedAction(
                    step_num=1,
                    operation_type="discovery",
                    tool_name="ask_docs",
                    arguments={"question": query},
                    purpose="Retrieve exact hardware specifications table from authoritative datasheet catalog",
                )
            ],
        )

    # API endpoint / schema query
    if any(k in q for k in ["endpoint", "method", "post to", "get to", "openapi", "rest api", "schema for"]):
        return QueryExecutionPlan(
            original_query=query,
            intent_summary="Exact REST API endpoint and schema lookup",
            estimated_complexity="simple",
            actions=[
                PlannedAction(
                    step_num=1,
                    operation_type="discovery",
                    tool_name="lookup_api",
                    arguments={"query": query},
                    purpose="Look up exact OpenAPI path, HTTP method, parameters, and payload schema",
                )
            ],
        )

    # Diagram generation
    if any(k in q for k in ["diagram", "draw", "drawio", "topology", "visualize"]):
        return QueryExecutionPlan(
            original_query=query,
            intent_summary="Network topology architecture diagram generation",
            estimated_complexity="moderate",
            actions=[
                PlannedAction(
                    step_num=1,
                    operation_type="discovery",
                    tool_name="list_diagram_roles_and_vendors",
                    arguments={},
                    purpose="Discover valid vendor icon packs and network node roles",
                ),
                PlannedAction(
                    step_num=2,
                    operation_type="synthesis",
                    tool_name="drawio_network_design_diagram",
                    arguments={"topology_model": "...", "filename": "campus_network.drawio"},
                    purpose="Synthesize and export editable Draw.io XML architecture diagram",
                    depends_on=[1],
                ),
            ],
        )

    # Client troubleshooting
    if any(k in q for k in ["client", "user", "mac", "connected", "auth fail", "cant connect"]):
        mac_match = re.search(r"([0-9a-fA-F]{2}[:-]){5}([0-9a-fA-F]{2})", query)
        mac = mac_match.group(0) if mac_match else None
        return QueryExecutionPlan(
            original_query=query,
            intent_summary="Wireless/wired client troubleshooting & session inspection",
            estimated_complexity="multi-step",
            actions=[
                PlannedAction(
                    step_num=1,
                    operation_type="discovery",
                    tool_name="get_client_detail" if mac else "list_clients",
                    arguments={"mac": mac} if mac else {"limit": 20},
                    purpose="Retrieve client association, RSSI/SNR, IP address, SSID, and VLAN",
                ),
                PlannedAction(
                    step_num=2,
                    operation_type="inspection",
                    tool_name="list_aaa_profiles",
                    arguments={},
                    purpose="Verify authentication server reachability and role enforcement",
                    depends_on=[1],
                ),
            ],
        )

    # Switch port & PoE troubleshooting
    if any(k in q for k in ["switch", "port", "poe", "vlan", "interface"]):
        return QueryExecutionPlan(
            original_query=query,
            intent_summary="Switch health, interface telemetry & PoE budget inspection",
            estimated_complexity="multi-step",
            actions=[
                PlannedAction(
                    step_num=1,
                    operation_type="discovery",
                    tool_name="list_devices",
                    arguments={"limit": 25},
                    purpose="Discover active switch serial numbers and site associations",
                ),
                PlannedAction(
                    step_num=2,
                    operation_type="inspection",
                    tool_name="get_device_health",
                    arguments={"serial": "...", "device_type": "switch"},
                    purpose="Check CPU, memory, uptime, and system alarms",
                    depends_on=[1],
                ),
                PlannedAction(
                    step_num=3,
                    operation_type="inspection",
                    tool_name="list_switch_interfaces",
                    arguments={"serial": "..."},
                    purpose="Inspect port operational status, speed, duplex, and error counters",
                    depends_on=[1],
                ),
            ],
        )

    # Default RAG / General Docs
    return QueryExecutionPlan(
        original_query=query,
        intent_summary="General networking knowledge retrieval & documentation inquiry",
        estimated_complexity="simple",
        actions=[
            PlannedAction(
                step_num=1,
                operation_type="discovery",
                tool_name="ask_docs",
                arguments={"question": query},
                purpose="Search authoritative technical documentation, JVDs, and configuration guides",
            )
        ],
    )
