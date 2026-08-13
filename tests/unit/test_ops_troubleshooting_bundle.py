"""Unit tests for the v0.7 bounded troubleshooting orchestration tool
hpe_networking_mcp.mcp_servers.ops.run_troubleshooting_bundle — composes the existing CX/AOS-S
diagnostic tools with per-step partial-failure handling.
"""

from __future__ import annotations

import asyncio

import pytest

import hpe_networking_mcp.mcp_servers.ops as ops


def test_rejects_unknown_device_type():
    with pytest.raises(ValueError, match="device_type must be one of"):
        asyncio.run(ops.run_troubleshooting_bundle("SERIAL1", "aps"))


def test_rejects_too_many_show_commands():
    with pytest.raises(ValueError, match="cannot exceed 5"):
        asyncio.run(
            ops.run_troubleshooting_bundle(
                "SERIAL1", "cx", commands=[f"show x{i}" for i in range(6)]
            )
        )


def test_rejects_non_show_command():
    with pytest.raises(ValueError, match="must start with 'show '"):
        asyncio.run(
            ops.run_troubleshooting_bundle("SERIAL1", "cx", commands=["reload"])
        )


def test_cx_bundle_runs_lldp_and_arp_by_default(monkeypatch):
    calls = []

    async def fake_lldp(serial):
        calls.append("lldp")
        return {"neighbors": []}

    async def fake_arp(serial):
        calls.append("arp")
        return {"arp": []}

    monkeypatch.setattr(ops, "get_lldp_neighbors", fake_lldp)
    monkeypatch.setattr(ops, "get_cx_arp_table", fake_arp)

    result = asyncio.run(ops.run_troubleshooting_bundle("SERIAL1", "cx"))

    assert calls == ["lldp", "arp"]
    assert result["step_count"] == 2
    assert result["failed_steps"] == []
    names = [s["name"] for s in result["steps"]]
    assert names == ["lldp_neighbors", "arp_table"]


def test_cx_bundle_adds_ping_and_show_when_requested(monkeypatch):
    async def fake_lldp(serial):
        return {}

    async def fake_arp(serial):
        return {}

    async def fake_ping(serial, destination):
        assert destination == "8.8.8.8"
        return {"status": "COMPLETED"}

    async def fake_show(serial, commands):
        assert commands == ["show version"]
        return {"output": "..."}

    monkeypatch.setattr(ops, "get_lldp_neighbors", fake_lldp)
    monkeypatch.setattr(ops, "get_cx_arp_table", fake_arp)
    monkeypatch.setattr(ops, "cx_ping", fake_ping)
    monkeypatch.setattr(ops, "cx_show", fake_show)

    result = asyncio.run(
        ops.run_troubleshooting_bundle(
            "SERIAL1", "cx", destination="8.8.8.8", commands=["show version"]
        )
    )

    names = [s["name"] for s in result["steps"]]
    assert names == ["lldp_neighbors", "arp_table", "ping", "show"]
    assert result["failed_steps"] == []


def test_aos_s_bundle_has_no_lldp_step(monkeypatch):
    async def fake_arp(serial):
        return {"arp": []}

    monkeypatch.setattr(ops, "aos_s_arp", fake_arp)

    result = asyncio.run(ops.run_troubleshooting_bundle("SERIAL1", "aos-s"))

    names = [s["name"] for s in result["steps"]]
    assert names == ["arp_table"]


def test_one_step_failure_does_not_abort_remaining_steps(monkeypatch):
    async def failing_lldp(serial):
        raise RuntimeError("upstream 500")

    async def fake_arp(serial):
        return {"arp": ["entry"]}

    monkeypatch.setattr(ops, "get_lldp_neighbors", failing_lldp)
    monkeypatch.setattr(ops, "get_cx_arp_table", fake_arp)

    result = asyncio.run(ops.run_troubleshooting_bundle("SERIAL1", "cx"))

    assert result["failed_steps"] == ["lldp_neighbors"]
    by_name = {s["name"]: s for s in result["steps"]}
    assert by_name["lldp_neighbors"]["status"] == "error"
    assert "upstream 500" in by_name["lldp_neighbors"]["error"]
    # arp_table must still have run and succeeded despite lldp failing first.
    assert by_name["arp_table"]["status"] == "ok"
    assert by_name["arp_table"]["result"] == {"arp": ["entry"]}


def test_backend_error_result_marks_step_failed(monkeypatch):
    async def failing_lldp(serial):
        return {"status": None, "errors": ["device unreachable"]}

    async def fake_arp(serial):
        return {"status": "COMPLETED", "arp": []}

    monkeypatch.setattr(ops, "get_lldp_neighbors", failing_lldp)
    monkeypatch.setattr(ops, "get_cx_arp_table", fake_arp)

    result = asyncio.run(ops.run_troubleshooting_bundle("SERIAL1", "cx"))

    assert result["failed_steps"] == ["lldp_neighbors"]
    by_name = {step["name"]: step for step in result["steps"]}
    assert by_name["lldp_neighbors"]["status"] == "error"
    assert "device unreachable" in by_name["lldp_neighbors"]["error"]
    assert by_name["arp_table"]["status"] == "ok"
