"""Unit tests for the local skills/runbook engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from hpe_networking_mcp.mcp_servers.skills import (
    Skill,
    SkillRegistry,
    get_registry,
    list_skills_payload,
    load_skill_payload,
    reset_registry_for_tests,
)
from hpe_networking_mcp.mcp_servers.skills._engine import _parse_skill


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_registry_for_tests(None)
    yield
    reset_registry_for_tests(None)


def test_bundled_skills_load_and_exclude_template():
    registry = get_registry()
    names = {skill.name for skill in registry.all()}
    assert "TEMPLATE" not in names
    assert "infrastructure-health-check" in names
    assert "ssid-review" in names
    assert "network-design-diagram" in names
    assert "morning-report" in names
    assert "central-scope-walker" in names
    assert "central-scope-audit" in names
    assert "clearpass-policy-audit" in names
    assert "wlan-sync-validation" in names
    assert "cross-platform-rf-check" in names
    assert "mist-scope-audit" in names
    assert "uxi-diagnostics" in names
    assert "aos8-migration-readiness" in names
    assert len(names) >= 15


def test_operator_skills_declare_router_entrypoints_and_read_only_bias():
    """High-value operator runbooks must steer through the router and gate writes."""
    registry = get_registry()
    required = {
        "morning-report": ("morning", "invoke_read_tool"),
        "central-scope-audit": ("list_scopes", "Read-only"),
        "wlan-sync-validation": ("translate_central_wlan_to_mist", "Read-only"),
        "cross-platform-rf-check": ("get_channel_utilization", "Read-only"),
        "clearpass-policy-audit": ("clearpass_list_services", "Read-only"),
        "mist-scope-audit": ("mist_list_wlans", "Read-only"),
        "uxi-diagnostics": ("uxi_list_sensors", "synthetic"),
        "aos8-migration-readiness": ("aos8_migration_dependency_plan", "preview"),
    }
    for name, needles in required.items():
        skill = registry.lookup(name)
        assert isinstance(skill, Skill)
        body = skill.body
        tools = set(skill.tools)
        assert "find_tool" in tools or "find_tool" in body
        for needle in needles:
            assert needle in body or needle in tools


def test_list_skills_compact_by_default_and_detail_flag():
    compact = list_skills_payload(tag="health")
    assert compact["count"] >= 1
    entry = compact["skills"][0]
    assert "description" not in entry
    assert "tools" not in entry
    assert entry["load_with"].startswith("load_skill(name=")
    assert "load_skill" in compact["next_step"]

    detailed = list_skills_payload(tag="health", detail=True)
    detailed_entry = detailed["skills"][0]
    assert detailed_entry["description"]
    assert isinstance(detailed_entry["tools"], list)


def test_list_skills_platform_filter():
    out = list_skills_payload(platform="glp")
    assert out["count"] >= 1
    assert all("glp" in skill["platforms"] for skill in out["skills"])


def test_load_skill_exact_and_substring():
    exact = load_skill_payload("ssid-review")
    assert exact["name"] == "ssid-review"
    assert "Objective" in exact["body"]
    assert "list_ssids" in exact["tools"]

    sub = load_skill_payload("ssid")
    assert sub["name"] == "ssid-review"


def test_load_skill_unknown_and_ambiguous(tmp_path: Path):
    (tmp_path / "alpha-one.md").write_text(
        "---\nname: alpha-one\ntitle: Alpha One\ndescription: first\n"
        "platforms: [central]\ntags: [a]\ntools: [find_tool]\n---\n\n# A\n",
        encoding="utf-8",
    )
    (tmp_path / "alpha-two.md").write_text(
        "---\nname: alpha-two\ntitle: Alpha Two\ndescription: second\n"
        "platforms: [central]\ntags: [a]\ntools: [find_tool]\n---\n\n# B\n",
        encoding="utf-8",
    )
    registry = SkillRegistry.from_directory(tmp_path)
    reset_registry_for_tests(registry)

    missing = load_skill_payload("nope")
    assert "error" in missing

    ambiguous = load_skill_payload("alpha")
    assert "error" in ambiguous
    assert "alpha-one" in ambiguous["error"]
    assert "alpha-two" in ambiguous["error"]


def test_parse_skill_rejects_name_filename_mismatch(tmp_path: Path):
    path = tmp_path / "good-name.md"
    path.write_text(
        "---\nname: bad-name\ntitle: X\ndescription: Y\n---\n\nbody\n",
        encoding="utf-8",
    )
    assert _parse_skill(path) is None


def test_registry_match_ranks_relevant_skill():
    registry = SkillRegistry(
        [
            Skill(
                name="wireless-check",
                title="Wireless client check",
                description="wifi",
                platforms=("central",),
                tags=("client", "wireless"),
            ),
            Skill(
                name="glp-only",
                title="GreenLake inventory",
                description="devices",
                platforms=("glp",),
                tags=("inventory",),
            ),
        ]
    )
    hits = registry.match("wireless client connectivity", limit=2)
    assert hits
    assert hits[0].name == "wireless-check"


def test_rag_registers_skill_tools():
    from hpe_networking_mcp.mcp_servers import rag

    tools = rag.mcp._tool_manager._tools
    assert "list_skills" in tools
    assert "load_skill" in tools
    anns = tools["list_skills"].annotations
    read_only = getattr(anns, "read_only_hint", None)
    if read_only is None:
        read_only = getattr(anns, "readOnlyHint", None)
    assert read_only is True
    out = rag.list_skills(platform="central")
    assert out["count"] >= 1
    loaded = rag.load_skill("change-pre-check")
    assert loaded["name"] == "change-pre-check"
