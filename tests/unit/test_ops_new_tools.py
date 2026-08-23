"""Unit tests for the new ops.py tools: device notes, gateway iperf/ping-sweep/halt,
AP-scoped diagnostics, show-command catalog, locate ops, swarm reboot, and
stack-conductor serial resolution.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import hpe_networking_mcp.mcp_servers.ops as ops


class _AcceptCtx:
    async def elicit(self, **kwargs):
        return SimpleNamespace(action="accept", data=SimpleNamespace(confirm=True))


class _DeclineCtx:
    async def elicit(self, **kwargs):
        return SimpleNamespace(action="decline", data=SimpleNamespace(confirm=False))


def _response(status_code=202, payload=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = dict(payload or {})
    resp.text = "{}"
    return resp


# ---------------------------------------------------------------------------
# Device notes
# ---------------------------------------------------------------------------


def test_update_device_notes_dry_run():
    result = ops.update_device_notes("SERIAL1", "in maintenance", dry_run=True)

    assert result["dry_run"] is True
    assert result["payload"] == {"notes": "in maintenance"}


def test_update_device_notes_rejects_too_long():
    try:
        ops.update_device_notes("SERIAL1", "x" * 300)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "256" in str(exc)


def test_update_device_notes_sends_patch(monkeypatch):
    client = MagicMock()
    client._request.return_value = _response(status_code=200, payload={"notes": "hi"})
    monkeypatch.setattr(ops, "get_client", lambda: client)

    result = ops.update_device_notes("SERIAL1", "hi")

    assert result["notes"] == "hi"
    client._request.assert_called_once_with(
        "PATCH", "/network-monitoring/v1/devices/SERIAL1", json={"notes": "hi"}
    )


def test_delete_device_notes_sends_empty_string(monkeypatch):
    client = MagicMock()
    client._request.return_value = _response(status_code=200, payload={"notes": ""})
    monkeypatch.setattr(ops, "get_client", lambda: client)

    ops.delete_device_notes("SERIAL1")

    client._request.assert_called_once_with(
        "PATCH", "/network-monitoring/v1/devices/SERIAL1", json={"notes": ""}
    )


# ---------------------------------------------------------------------------
# Gateway iperf / ping-sweep / halt
# ---------------------------------------------------------------------------


def test_gateway_iperf_uses_async_helper(monkeypatch):
    calls = []

    async def fake_async(client, endpoint, payload, errors, *, diagnostic=False):
        assert diagnostic is True
        calls.append((endpoint, payload))
        return {"status": "COMPLETED", "errors": errors}

    monkeypatch.setattr(ops, "get_client", lambda: object())
    monkeypatch.setattr(ops, "atroubleshoot_async", fake_async)

    result = asyncio.run(
        ops.gateway_iperf("GW1", "10.0.0.1", port=5201, duration=10, protocol="tcp")
    )

    assert result == {"status": "COMPLETED", "errors": []}
    assert calls == [
        (
            ops.troubleshooting_endpoint_candidates("gateways", "GW1", "iperf"),
            {"iperfServerAddress": "10.0.0.1", "port": 5201, "duration": 10, "protocol": "tcp"},
        )
    ]


def test_gateway_ping_sweep_validates_packet_size_range():
    async def _run():
        return await ops.gateway_ping_sweep(
            "GW1", "1.1.1.1", start_packet_size=100, end_packet_size=50, sweep_interval=10
        )

    try:
        asyncio.run(_run())
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "end_packet_size" in str(exc)


def test_gateway_ping_sweep_uses_async_helper(monkeypatch):
    calls = []

    async def fake_async(client, endpoint, payload, errors, *, diagnostic=False):
        assert diagnostic is True
        calls.append((endpoint, payload))
        return {"status": "COMPLETED", "errors": errors}

    monkeypatch.setattr(ops, "get_client", lambda: object())
    monkeypatch.setattr(ops, "atroubleshoot_async", fake_async)

    result = asyncio.run(
        ops.gateway_ping_sweep(
            "GW1", "1.1.1.1", start_packet_size=50, end_packet_size=100, sweep_interval=10, count=3
        )
    )

    assert result["status"] == "COMPLETED"
    assert calls[0][0] == ops.troubleshooting_endpoint_candidates(
        "gateways", "GW1", "pingSweep"
    )
    assert calls[0][1]["startPacketSize"] == 50
    assert calls[0][1]["endPacketSize"] == 100


def test_gateway_halt_dry_run_skips_confirmation():
    result = asyncio.run(ops.gateway_halt(_DeclineCtx(), "GW1", dry_run=True))

    assert result["dry_run"] is True


def test_gateway_halt_requires_confirmation(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(ops, "get_client", lambda: client)

    result = asyncio.run(ops.gateway_halt(_DeclineCtx(), "GW1"))

    assert result["status"] == "CANCELLED"
    client._arequest.assert_not_called()


def test_gateway_halt_sends_post_when_confirmed(monkeypatch):
    client = MagicMock()

    async def fake_arequest(method, endpoint, **kwargs):
        return _response(status_code=200, payload={"status": "halted"})

    client._arequest = fake_arequest
    monkeypatch.setattr(ops, "get_client", lambda: client)

    result = asyncio.run(ops.gateway_halt(_AcceptCtx(), "GW1"))

    assert result["status"] == "halted"
    assert result["endpoint_used"] == "/network-troubleshooting/v1/gateways/GW1/halt"


def test_reboot_ap_swarm_requires_confirmation(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(ops, "get_client", lambda: client)

    result = asyncio.run(ops.reboot_ap_swarm(_DeclineCtx(), "AP1"))

    assert result["status"] == "CANCELLED"


# ---------------------------------------------------------------------------
# AP-scoped diagnostics
# ---------------------------------------------------------------------------


def test_ap_ping_uses_async_helper(monkeypatch):
    calls = []

    async def fake_async(client, endpoint, payload, errors, *, diagnostic=False):
        assert diagnostic is True
        calls.append((endpoint, payload))
        return {"status": "COMPLETED", "errors": errors}

    monkeypatch.setattr(ops, "get_client", lambda: object())
    monkeypatch.setattr(ops, "atroubleshoot_async", fake_async)

    asyncio.run(ops.ap_ping("AP1", "8.8.8.8", count=3))

    assert calls == [
        (
            ops.troubleshooting_endpoint_candidates("aps", "AP1", "ping"),
            {"destination": "8.8.8.8", "count": 3},
        )
    ]


def test_ap_tcp_uses_async_helper(monkeypatch):
    calls = []

    async def fake_async(client, endpoint, payload, errors, *, diagnostic=False):
        assert diagnostic is True
        calls.append((endpoint, payload))
        return {"status": "COMPLETED", "errors": errors}

    monkeypatch.setattr(ops, "get_client", lambda: object())
    monkeypatch.setattr(ops, "atroubleshoot_async", fake_async)

    asyncio.run(ops.ap_tcp("AP1", "10.0.0.5", 443, timeout=5))

    assert calls == [
        (
            ops.troubleshooting_endpoint_candidates("aps", "AP1", "tcp"),
            {"host": "10.0.0.5", "port": 443, "timeout": 5},
        )
    ]


def test_ap_show_validates_commands(monkeypatch):
    called = False

    async def fake_async(*a, **k):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(ops, "get_client", lambda: object())
    monkeypatch.setattr(ops, "atroubleshoot_async", fake_async)

    result = asyncio.run(ops.ap_show("AP1", ["reload"]))

    assert called is False
    assert "must start with 'show '" in result["errors"][0]


def test_ap_show_runs_when_valid(monkeypatch):
    async def fake_async(client, endpoint, payload, errors, *, diagnostic=False):
        assert diagnostic is True
        return {"status": "COMPLETED", "errors": errors}

    monkeypatch.setattr(ops, "get_client", lambda: object())
    monkeypatch.setattr(ops, "atroubleshoot_async", fake_async)

    result = asyncio.run(ops.ap_show("AP1", ["show clients"]))

    assert result["status"] == "COMPLETED"


# ---------------------------------------------------------------------------
# Show-command catalog
# ---------------------------------------------------------------------------


def test_list_show_commands_resolves_platform_and_fetches(monkeypatch):
    client = MagicMock()
    client._request.return_value = _response(status_code=200, payload={"commands": ["show run"]})
    monkeypatch.setattr(ops, "get_client", lambda: client)
    monkeypatch.setattr(ops, "device_type_for_troubleshoot", lambda serial, dtype: "cx")

    result = ops.list_show_commands("SW1")

    assert result["device_type"] == "cx"
    assert result["commands"] == {"commands": ["show run"]}
    client._request.assert_called_once_with(
        "GET", "/network-troubleshooting/v1/cx/SW1/show-commands"
    )


def test_list_show_commands_reports_unresolvable_platform(monkeypatch):
    monkeypatch.setattr(ops, "device_type_for_troubleshoot", lambda serial, dtype: None)

    result = ops.list_show_commands("UNKNOWN1")

    assert result["commands"] is None
    assert "Could not determine a supported platform" in result["errors"][0]


# ---------------------------------------------------------------------------
# Locate operations
# ---------------------------------------------------------------------------


def test_locate_ap_posts_to_locate_endpoint(monkeypatch):
    client = MagicMock()

    async def fake_arequest(method, endpoint, **kwargs):
        assert method == "POST"
        return _response(status_code=202, payload={"status": "locating"})

    client._arequest = fake_arequest
    monkeypatch.setattr(ops, "get_client", lambda: client)

    result = asyncio.run(ops.locate_ap("AP1"))

    assert result["endpoint_used"] == "/network-troubleshooting/v1/aps/AP1/locate"
    assert result["status"] == "locating"


def test_locate_cx_switch_posts_to_locate_endpoint(monkeypatch):
    client = MagicMock()

    async def fake_arequest(method, endpoint, **kwargs):
        return _response(status_code=202, payload={})

    client._arequest = fake_arequest
    monkeypatch.setattr(ops, "get_client", lambda: client)

    result = asyncio.run(ops.locate_cx_switch("SW1"))

    assert result["endpoint_used"] == "/network-troubleshooting/v1/cx/SW1/locate"


# ---------------------------------------------------------------------------
# Stack-conductor serial resolution
# ---------------------------------------------------------------------------


def test_resolve_stack_serial_returns_unchanged_for_standalone(monkeypatch):
    mcp_client = MagicMock()
    mcp_client.get_device_by_serial.return_value = {"switchRole": "STANDALONE"}
    monkeypatch.setattr(ops, "get_mcp_client", lambda: mcp_client)

    result = ops.resolve_stack_serial("SW1")

    assert result == {"serial_number": "SW1", "resolved_serial": "SW1", "was_stack_member": False}


def test_resolve_stack_serial_resolves_member_to_conductor(monkeypatch):
    mcp_client = MagicMock()
    mcp_client.get_device_by_serial.return_value = {
        "switchRole": "MEMBER",
        "stackConductorSerial": "SW-CONDUCTOR",
    }
    monkeypatch.setattr(ops, "get_mcp_client", lambda: mcp_client)

    result = ops.resolve_stack_serial("SW-MEMBER-1")

    assert result["resolved_serial"] == "SW-CONDUCTOR"
    assert result["was_stack_member"] is True


def test_resolve_stack_serial_falls_back_when_device_not_found(monkeypatch):
    mcp_client = MagicMock()
    mcp_client.get_device_by_serial.return_value = None
    monkeypatch.setattr(ops, "get_mcp_client", lambda: mcp_client)

    result = ops.resolve_stack_serial("UNKNOWN")

    assert result["resolved_serial"] == "UNKNOWN"
