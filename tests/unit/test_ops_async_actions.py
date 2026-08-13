import asyncio
from types import SimpleNamespace

from hpe_networking_mcp.mcp_servers import ops


class _AcceptedContext:
    async def elicit(self, **kwargs):
        return SimpleNamespace(action="accept", data=SimpleNamespace(confirm=True))


class _Response:
    status_code = 202
    headers = {}

    def __init__(self, payload=None):
        self._payload = payload or {"accepted": True}

    def json(self):
        return self._payload


def test_reboot_device_uses_async_request_when_type_is_provided(monkeypatch):
    calls = []
    client = object()

    async def fake_atroubleshoot(received_client, endpoints, payload, errors):
        calls.append((received_client, endpoints, payload, errors))
        return {
            "status": "COMPLETED",
            "endpoint_used": endpoints[0],
            "errors": errors,
        }

    monkeypatch.setattr(ops, "get_client", lambda: client)
    monkeypatch.setattr(ops, "atroubleshoot_async", fake_atroubleshoot)

    result = asyncio.run(ops.reboot_device(_AcceptedContext(), "CX1", device_type="CX"))

    assert result == {
        "serial_number": "CX1",
        "device_type": "CX",
        "response": {
            "status": "COMPLETED",
            "endpoint_used": "/network-troubleshooting/v1/cx/CX1/reboot",
            "errors": [],
        },
        "errors": [],
    }
    assert calls == [
        (
            client,
            ops.troubleshooting_endpoint_candidates("cx", "CX1", "reboot"),
            {},
            [],
        )
    ]


def test_reboot_device_uses_async_request_after_auto_detection(monkeypatch):
    calls = []
    client = object()

    async def fake_atroubleshoot(received_client, endpoints, payload, errors):
        calls.append((received_client, endpoints, payload, errors))
        return {"status": "COMPLETED", "endpoint_used": endpoints[0], "errors": errors}

    mcp_client = SimpleNamespace(get_device_by_serial=lambda serial: {"deviceType": "ACCESS_POINT"})
    monkeypatch.setattr(ops, "get_client", lambda: client)
    monkeypatch.setattr(ops, "get_mcp_client", lambda: mcp_client)
    monkeypatch.setattr(ops, "atroubleshoot_async", fake_atroubleshoot)

    result = asyncio.run(ops.reboot_device(_AcceptedContext(), "AP1"))

    assert result["device_type"] == "AP"
    assert calls == [
        (
            client,
            ops.troubleshooting_endpoint_candidates("aps", "AP1", "reboot"),
            {},
            [],
        )
    ]


def test_disconnect_client_uses_async_request_when_ap_serial_is_provided(monkeypatch):
    calls = []
    client = object()

    async def fake_atroubleshoot(received_client, endpoints, payload, errors):
        calls.append((received_client, endpoints, payload, errors))
        return {"status": "COMPLETED", "endpoint_used": endpoints[0], "errors": errors}

    monkeypatch.setattr(ops, "get_client", lambda: client)
    monkeypatch.setattr(ops, "atroubleshoot_async", fake_atroubleshoot)

    result = asyncio.run(ops.disconnect_client(_AcceptedContext(), "aa:bb:cc:dd:ee:ff", ap_serial="AP1"))

    assert result == {
        "mac_address": "aa:bb:cc:dd:ee:ff",
        "ap_serial": "AP1",
        "endpoint_used": "/network-troubleshooting/v1/aps/AP1/disconnectUserByMacAddress",
        "response": {
            "status": "COMPLETED",
            "endpoint_used": "/network-troubleshooting/v1/aps/AP1/disconnectUserByMacAddress",
            "errors": [],
        },
        "errors": [],
    }
    assert calls == [
        (
            client,
            ops.troubleshooting_endpoint_candidates(
                "aps", "AP1", "disconnectUserByMacAddress"
            ),
            {"userMacAddress": "aa:bb:cc:dd:ee:ff"},
            [],
        )
    ]


def test_disconnect_client_uses_async_request_after_auto_lookup(monkeypatch):
    calls = []
    client = object()

    async def fake_atroubleshoot(received_client, endpoints, payload, errors):
        calls.append((received_client, endpoints, payload, errors))
        return {"status": "COMPLETED", "endpoint_used": endpoints[0], "errors": errors}

    mcp_client = SimpleNamespace(
        find_client=lambda mac: {"connectedDeviceSerial": "AP2"},
    )
    monkeypatch.setattr(ops, "get_client", lambda: client)
    monkeypatch.setattr(ops, "get_mcp_client", lambda: mcp_client)
    monkeypatch.setattr(ops, "atroubleshoot_async", fake_atroubleshoot)

    result = asyncio.run(ops.disconnect_client(_AcceptedContext(), "aa:bb:cc:dd:ee:ff"))

    assert result["ap_serial"] == "AP2"
    assert calls == [
        (
            client,
            ops.troubleshooting_endpoint_candidates(
                "aps", "AP2", "disconnectUserByMacAddress"
            ),
            {"userMacAddress": "aa:bb:cc:dd:ee:ff"},
            [],
        )
    ]


def test_reboot_device_rejects_unknown_type_before_client_construction(monkeypatch):
    def fail_get_client():
        raise AssertionError("get_client should not be called for rejected input")

    monkeypatch.setattr(ops, "get_client", fail_get_client)

    result = asyncio.run(ops.reboot_device(_AcceptedContext(), "X1", device_type="BOGUS"))

    assert result["device_type"] == "BOGUS"
    assert result["response"] is None
    assert "Unknown device_type 'BOGUS'" in result["errors"][0]
