from __future__ import annotations

import asyncio

import pytest

import hpe_networking_mcp.mcp_servers.ops as ops


def test_cx_ping_uses_async_troubleshooting_helper(monkeypatch):
    calls = []
    client = object()

    async def fake_atroubleshoot_async(received_client, endpoint, payload, errors):
        calls.append((received_client, endpoint, payload, errors))
        return {"status": "COMPLETED", "errors": errors}

    monkeypatch.setattr(ops, "get_client", lambda: client)
    monkeypatch.setattr(ops, "atroubleshoot_async", fake_atroubleshoot_async)

    result = asyncio.run(
        ops.cx_ping(
            "SERIAL1",
            "8.8.8.8",
            count=3,
            packet_size=128,
            vrf_name="mgmt",
            use_management_interface=True,
        )
    )

    assert result == {"status": "COMPLETED", "errors": []}
    assert calls == [
        (
            client,
            ops.troubleshooting_endpoint_candidates("cx", "SERIAL1", "ping"),
            {
                "destination": "8.8.8.8",
                "count": 3,
                "packetSize": 128,
                "vrfName": "mgmt",
                "useManagementInterface": True,
            },
            [],
        )
    ]


def test_cx_traceroute_uses_async_troubleshooting_helper(monkeypatch):
    calls = []
    client = object()

    async def fake_atroubleshoot_async(received_client, endpoint, payload, errors):
        calls.append((received_client, endpoint, payload, errors))
        return {"status": "COMPLETED", "errors": errors}

    monkeypatch.setattr(ops, "get_client", lambda: client)
    monkeypatch.setattr(ops, "atroubleshoot_async", fake_atroubleshoot_async)

    result = asyncio.run(
        ops.cx_traceroute(
            "SERIAL1",
            "1.1.1.1",
            vrf_name="mgmt",
            use_management_interface=False,
        )
    )

    assert result == {"status": "COMPLETED", "errors": []}
    assert calls == [
        (
            client,
            ops.troubleshooting_endpoint_candidates("cx", "SERIAL1", "traceroute"),
            {
                "destination": "1.1.1.1",
                "vrfName": "mgmt",
                "useManagementInterface": False,
            },
            [],
        )
    ]


def test_cx_show_validates_commands_before_async_call(monkeypatch):
    called = False

    async def fake_atroubleshoot_async(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(ops, "atroubleshoot_async", fake_atroubleshoot_async)

    result = asyncio.run(ops.cx_show("SERIAL1", ["configure terminal"]))

    assert result["status"] is None
    assert "must start with 'show '" in result["errors"][0]
    assert called is False


def test_cx_show_uses_async_troubleshooting_helper(monkeypatch):
    calls = []
    client = object()

    async def fake_atroubleshoot_async(received_client, endpoint, payload, errors):
        calls.append((received_client, endpoint, payload, errors))
        return {"status": "COMPLETED", "errors": errors}

    monkeypatch.setattr(ops, "get_client", lambda: client)
    monkeypatch.setattr(ops, "atroubleshoot_async", fake_atroubleshoot_async)

    result = asyncio.run(ops.cx_show("SERIAL1", ["show version"]))

    assert result == {"status": "COMPLETED", "errors": []}
    assert calls == [
        (
            client,
            ops.troubleshooting_endpoint_candidates("cx", "SERIAL1", "showCommands"),
            {"commands": ["show version"]},
            [],
        )
    ]


@pytest.mark.parametrize(
    ("func", "args", "expected_commands"),
    [
        (ops.get_lldp_neighbors, ("SERIAL1",), ["show lldp neighbors"]),
        (ops.get_cx_arp_table, ("SERIAL1",), ["show arp"]),
        (ops.get_cx_mac_table, ("SERIAL1",), ["show mac-address-table"]),
        (
            ops.get_cx_mac_table,
            ("SERIAL1", "1/1/16"),
            ["show mac-address-table interface 1/1/16"],
        ),
        (
            ops.get_switch_port_errors,
            ("SERIAL1",),
            ["show interface statistics"],
        ),
        (
            ops.get_switch_port_errors,
            ("SERIAL1", "1/1/5"),
            ["show interface 1/1/5 statistics"],
        ),
        (
            ops.get_switch_spanning_tree,
            ("SERIAL1",),
            ["show spanning-tree detail"],
        ),
        (
            ops.get_switch_spanning_tree,
            ("SERIAL1", "1/1/5"),
            ["show spanning-tree detail interface 1/1/5"],
        ),
        (
            ops.get_switch_interface_counters,
            ("SERIAL1",),
            ["show interface counters"],
        ),
        (
            ops.get_switch_interface_counters,
            ("SERIAL1", "1/1/5"),
            ["show interface 1/1/5 counters"],
        ),
    ],
)
def test_cx_switch_intelligence_tools_use_async_show_helper(
    monkeypatch,
    func,
    args,
    expected_commands,
):
    calls = []

    async def fake_cx_show_commands(serial_number, commands):
        calls.append((serial_number, commands))
        return {"status": "COMPLETED", "errors": []}

    monkeypatch.setattr(ops, "_cx_show_commands", fake_cx_show_commands)

    result = asyncio.run(func(*args))

    assert result == {"status": "COMPLETED", "errors": []}
    assert calls == [("SERIAL1", expected_commands)]


def test_find_mac_on_switch_preserves_input_mac_metadata(monkeypatch):
    calls = []

    async def fake_cx_show_commands(serial_number, commands):
        calls.append((serial_number, commands))
        return {"status": "COMPLETED", "errors": []}

    monkeypatch.setattr(ops, "_cx_show_commands", fake_cx_show_commands)

    result = asyncio.run(ops.find_mac_on_switch("SERIAL1", "AA-BB-CC-DD-EE-FF"))

    assert result == {
        "status": "COMPLETED",
        "errors": [],
        "mac_address": "AA-BB-CC-DD-EE-FF",
    }
    assert calls == [
        (
            "SERIAL1",
            ["show mac-address-table address aa:bb:cc:dd:ee:ff"],
        )
    ]


def _non_cx_cases():
    return [
        (
            ops.aos_s_ping,
            ("AOSS1", "8.8.8.8"),
            ops.troubleshooting_endpoint_candidates("aos-s", "AOSS1", "ping"),
            {"destination": "8.8.8.8"},
        ),
        (
            ops.aos_s_traceroute,
            ("AOSS1", "1.1.1.1"),
            ops.troubleshooting_endpoint_candidates("aos-s", "AOSS1", "traceroute"),
            {"destination": "1.1.1.1"},
        ),
        (
            ops.aos_s_show,
            ("AOSS1", ["show version"]),
            ops.troubleshooting_endpoint_candidates("aos-s", "AOSS1", "showCommands"),
            {"commands": ["show version"]},
        ),
        (
            ops.gateway_show,
            ("GW1", ["show datapath session"]),
            ops.troubleshooting_endpoint_candidates(
                "gateways", "GW1", "showCommands"
            ),
            {"commands": ["show datapath session"]},
        ),
        (
            ops.aos_s_arp,
            ("AOSS1",),
            ops.troubleshooting_endpoint_candidates(
                "aos-s", "AOSS1", "getArpTable"
            ),
            {},
        ),
        (
            ops.run_speed_test,
            ("AP1",),
            ops.troubleshooting_endpoint_candidates("aps", "AP1", "speedtest"),
            {},
        ),
    ]


@pytest.mark.parametrize(("func", "args", "expected_endpoint", "expected_payload"), _non_cx_cases())
def test_non_cx_diagnostic_tools_use_async_troubleshooting_helper(
    monkeypatch,
    func,
    args,
    expected_endpoint,
    expected_payload,
):
    calls = []
    client = object()

    async def fake_atroubleshoot_async(received_client, endpoint, payload, errors):
        calls.append((received_client, endpoint, payload, errors))
        return {"status": "COMPLETED", "errors": errors}

    monkeypatch.setattr(ops, "get_client", lambda: client)
    monkeypatch.setattr(ops, "atroubleshoot_async", fake_atroubleshoot_async)

    result = asyncio.run(func(*args))

    assert result == {"status": "COMPLETED", "errors": []}
    assert calls == [(client, expected_endpoint, expected_payload, [])]


def test_aos_s_show_validates_commands_before_async_call(monkeypatch):
    called = False

    async def fake_atroubleshoot_async(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(ops, "atroubleshoot_async", fake_atroubleshoot_async)

    result = asyncio.run(ops.aos_s_show("AOSS1", ["configure terminal"]))

    assert result["status"] is None
    assert "must start with 'show '" in result["errors"][0]
    assert called is False


def test_gateway_show_validates_commands_before_async_call(monkeypatch):
    called = False

    async def fake_atroubleshoot_async(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(ops, "atroubleshoot_async", fake_atroubleshoot_async)

    result = asyncio.run(ops.gateway_show("GW1", ["configure terminal"]))

    assert result["status"] is None
    assert "must start with 'show '" in result["errors"][0]
    assert called is False


def test_cable_test_uses_async_troubleshooting_helper(monkeypatch):
    calls = []
    client = object()

    async def fake_atroubleshoot_async(received_client, endpoint, payload, errors):
        calls.append((received_client, endpoint, payload, errors))
        return {"status": "COMPLETED", "errors": errors}

    monkeypatch.setattr(ops, "get_client", lambda: client)
    monkeypatch.setattr(ops, "device_type_for_troubleshoot", lambda serial, dtype: "cx")
    monkeypatch.setattr(ops, "atroubleshoot_async", fake_atroubleshoot_async)

    result = asyncio.run(ops.cable_test("CX1", ["1/1/1"]))

    assert result == {"status": "COMPLETED", "errors": []}
    assert calls == [
        (
            client,
            ops.troubleshooting_endpoint_candidates("cx", "CX1", "cableTest"),
            {"ports": ["1/1/1"]},
            [],
        )
    ]


def _fail_get_client():
    raise AssertionError("get_client should not be called for rejected input")


def test_poe_bounce_rejects_aps_before_client_construction(monkeypatch):
    monkeypatch.setattr(ops, "get_client", _fail_get_client)
    monkeypatch.setattr(ops, "device_type_for_troubleshoot", lambda serial, dtype: "aps")

    result = asyncio.run(ops.poe_bounce(object(), "AP1", ["1"], device_type="AP"))

    assert result == {"status": None, "errors": ["PoE bounce is not supported on Access Points."]}


def test_port_bounce_rejects_aps_before_client_construction(monkeypatch):
    monkeypatch.setattr(ops, "get_client", _fail_get_client)
    monkeypatch.setattr(ops, "device_type_for_troubleshoot", lambda serial, dtype: "aps")

    result = asyncio.run(ops.port_bounce(object(), "AP1", ["1"], device_type="AP"))

    assert result == {"status": None, "errors": ["Port bounce is not supported on Access Points."]}


def test_cable_test_rejects_gateways_before_client_construction(monkeypatch):
    monkeypatch.setattr(ops, "get_client", _fail_get_client)
    monkeypatch.setattr(ops, "device_type_for_troubleshoot", lambda serial, dtype: "gateways")

    result = asyncio.run(ops.cable_test("GW1", ["GE 0/0/0"]))

    assert result == {"status": None, "errors": ["Cable test is not supported on gateways."]}
