"""Focused tests for live diagram topology orchestration."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from hpe_networking_mcp.cli_client.diagram_workflow import (
    execute_diagram_export,
    parse_diagram_intent,
)
from hpe_networking_mcp.cli_client.safety import SafetyPolicy


class _RouterManager:
    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def resolve_tool_name(self, name: str) -> str:
        if name == "invoke_read_tool":
            return name
        raise KeyError(name)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        args = arguments or {}
        self.calls.append((name, args))
        assert name == "invoke_read_tool"
        backend_name = args["name"]
        response = self.responses[backend_name]
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(
            structuredContent=response,
            content=[],
            isError=False,
        )


def _backend_calls(manager: _RouterManager) -> list[tuple[str, dict[str, Any]]]:
    return [(arguments["name"], arguments["arguments"]) for _, arguments in manager.calls]


def test_live_diagram_resolves_site_and_exports_real_topology():
    pref = parse_diagram_intent('draw a live topology for site "Austin Lab"')
    manager = _RouterManager(
        {
            "list_scopes": {
                "items": [
                    {
                        "scope_id": "site-1",
                        "scope_name": "Austin Lab",
                        "scope_type": "SITE",
                    }
                ]
            },
            "get_topology": {
                "data": {
                    "devices": [
                        {
                            "id": "sw 1",
                            "name": "Austin-Core",
                            "type": "CORE_SWITCH",
                            "group": "Campus Floor",
                        },
                        {
                            "serial": "AP-1",
                            "hostname": "Austin-AP",
                            "device_type": "AP",
                            "group": "Campus Floor",
                        },
                    ],
                    "edges": [
                        {
                            "source": "sw 1",
                            "target": "AP-1",
                            "type": "ethernet",
                        }
                    ],
                    "groups": [
                        {
                            "id": "Campus Floor",
                            "label": "Campus Floor",
                            "members": ["sw 1", {"serial": "AP-1"}],
                        }
                    ],
                }
            },
            "validate_diagram_model": {"ok": True},
            "drawio_network_design_diagram": {
                "ok": True,
                "saved_path": "outputs/diagrams/network_topology.drawio",
            },
        }
    )

    result = asyncio.run(execute_diagram_export(manager, SafetyPolicy(), pref))

    assert result["ok"] is True
    assert result["topology_source"] == "live"
    assert result["site_id"] == "site-1"
    assert result["title"] == "LIVE — Network Topology (site site-1)"
    assert result["filename_stem"] == "network_topology_live"
    assert result["text"].startswith("LIVE topology:")
    assert result["node_count"] == 2
    assert result["link_count"] == 1
    assert result["group_count"] == 1
    assert {node["label"] for node in pref.nodes} == {"Austin-Core", "Austin-AP"}
    assert pref.links == [{"source": "sw_1", "target": "AP-1", "link_type": "ethernet"}]
    assert pref.groups == [
        {
            "id": "Campus_Floor",
            "label": "Campus Floor",
            "members": ["sw_1", "AP-1"],
        }
    ]

    calls = _backend_calls(manager)
    assert calls[0] == (
        "list_scopes",
        {"limit": 100, "offset": 0, "full_list": False},
    )
    assert calls[1] == ("get_topology", {"site_id": "site-1"})
    exported_model = calls[-1][1]["model"]
    assert exported_model["nodes"] == pref.nodes
    assert exported_model["links"] == pref.links
    assert exported_model["groups"] == pref.groups
    assert exported_model["title"] == "LIVE — Network Topology (site site-1)"
    assert exported_model["notes"] == [
        "Grounded in live Central topology fetched for site site-1."
    ]
    assert calls[-1][1]["filename_stem"] == "network_topology_live"


def test_live_diagram_returns_error_when_site_cannot_be_resolved():
    pref = parse_diagram_intent('draw a live topology for site "Missing Site"')
    manager = _RouterManager(
        {
            "list_scopes": {
                "items": [
                    {
                        "scope_id": "site-1",
                        "scope_name": "Austin Lab",
                        "scope_type": "SITE",
                    }
                ]
            }
        }
    )

    result = asyncio.run(execute_diagram_export(manager, SafetyPolicy(), pref))

    assert result["ok"] is False
    assert result["topology_source"] == "live"
    assert "Missing Site" in result["error"]
    assert _backend_calls(manager) == [
        ("list_scopes", {"limit": 100, "offset": 0, "full_list": False})
    ]


def test_failed_live_fetch_exports_explicit_illustrative_fallback():
    pref = parse_diagram_intent("draw a live topology for site-id=site-1")
    illustrative_nodes = list(pref.nodes)
    manager = _RouterManager(
        {
            "get_topology": {"error": "Central topology service unavailable"},
            "validate_diagram_model": {"ok": True},
            "drawio_network_design_diagram": {
                "ok": True,
                "saved_path": "outputs/diagrams/network_topology.drawio",
            },
        }
    )

    result = asyncio.run(execute_diagram_export(manager, SafetyPolicy(), pref))

    assert result["ok"] is True
    assert result["topology_source"] == "illustrative_fallback"
    assert result["title"].startswith("ILLUSTRATIVE fallback")
    assert result["filename_stem"] == "network_topology_illustrative"
    assert "Central topology service unavailable" in result["warning"]
    assert result["text"].startswith("ILLUSTRATIVE fallback:")
    assert pref.nodes == illustrative_nodes

    calls = _backend_calls(manager)
    assert calls[0] == ("get_topology", {"site_id": "site-1"})
    exported_model = calls[-1][1]["model"]
    assert exported_model["title"].startswith("ILLUSTRATIVE fallback")
    assert exported_model["notes"] == [result["warning"]]
    assert calls[-1][1]["filename_stem"] == "network_topology_illustrative"
