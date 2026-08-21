import asyncio

import pytest

from hpe_networking_mcp.mcp_servers import nac


def _aaa_cases():
    return [
        (
            {
                "serial_number": "AP1",
                "username": "user",
                "password": "pass",
                "device_type": "AP",
                "server_name": "radius-primary",
            },
            nac.troubleshooting_endpoint_candidates("aps", "AP1", "aaa"),
            {
                "serverName": "radius-primary",
                "username": "user",
                "password": "pass",
            },
        ),
        (
            {
                "serial_number": "CX1",
                "username": "user",
                "password": "pass",
                "device_type": "CX",
                "radius_server_ip": "192.0.2.10",
                "auth_method": "pap",
            },
            nac.troubleshooting_endpoint_candidates("cx", "CX1", "aaa"),
            {
                "authMethodType": "pap",
                "radiusServerIp": "192.0.2.10",
                "username": "user",
                "password": "pass",
            },
        ),
    ]


@pytest.mark.parametrize(("kwargs", "expected_endpoint", "expected_payload"), _aaa_cases())
def test_aaa_uses_async_troubleshooting_helper(
    monkeypatch, kwargs, expected_endpoint, expected_payload
):
    calls = []
    client = object()

    async def fake_atroubleshoot_async(
        received_client, endpoint, payload, errors, *, diagnostic=False
    ):
        assert diagnostic is True
        calls.append((received_client, endpoint, payload, errors))
        return {"status": "COMPLETED", "errors": errors}

    monkeypatch.setattr(nac, "get_client", lambda: client)
    monkeypatch.setattr(nac, "atroubleshoot_async", fake_atroubleshoot_async)

    result = asyncio.run(nac.test_aaa(**kwargs))

    assert result == {"status": "COMPLETED", "errors": []}
    assert calls == [(client, expected_endpoint, expected_payload, [])]


def test_aaa_ap_requires_server_name_before_async_call(monkeypatch):
    called = False

    async def fake_atroubleshoot_async(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(nac, "atroubleshoot_async", fake_atroubleshoot_async)

    result = asyncio.run(
        nac.test_aaa(
            serial_number="AP1",
            username="user",
            password="pass",
            device_type="AP",
        )
    )

    assert result == {"status": None, "errors": ["server_name is required for AP AAA tests"]}
    assert called is False


def test_aaa_cx_requires_radius_server_ip_before_async_call(monkeypatch):
    called = False

    async def fake_atroubleshoot_async(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(nac, "atroubleshoot_async", fake_atroubleshoot_async)

    result = asyncio.run(
        nac.test_aaa(
            serial_number="CX1",
            username="user",
            password="pass",
            device_type="CX",
        )
    )

    assert result == {"status": None, "errors": ["radius_server_ip is required for CX AAA tests"]}
    assert called is False
