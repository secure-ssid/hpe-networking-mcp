"""Unit tests for the per-platform pooled AsyncClient registry."""

from __future__ import annotations

import asyncio

import httpx

from hpe_networking_mcp.pipeline.clients.pooled_clients import (
    aclose_pooled_clients,
    pooled_client,
)


def test_consecutive_calls_on_one_loop_share_one_client():
    async def run():
        first = pooled_client("mist")
        second = pooled_client("mist")
        return first, second

    first, second = asyncio.run(run())
    assert first is second


def test_pooled_client_has_structured_timeout_and_limits():
    client = asyncio.run(_make("clearpass"))
    assert isinstance(client, httpx.AsyncClient)
    # Flat 30s timeout with no connect bound is what this replaces: a stalled
    # connect must not occupy a slot for the full read timeout.
    assert client.timeout.connect == 10.0
    assert client.timeout.read == 30.0


async def _make(name):
    return pooled_client(name)


def test_new_event_loop_gets_a_fresh_client():
    first = asyncio.run(_make("uxi"))
    second = asyncio.run(_make("uxi"))
    assert first is not second


def test_closed_client_is_recreated():
    async def run():
        first = pooled_client("aos8")
        await first.aclose()
        return first, pooled_client("aos8")

    first, second = asyncio.run(run())
    assert first is not second
    assert not second.is_closed


def test_aclose_pooled_clients_closes_current_loop_clients():
    async def run():
        client = pooled_client("edgeconnect")
        await aclose_pooled_clients()
        return client

    client = asyncio.run(run())
    assert client.is_closed


def test_backend_calls_reuse_one_pooled_client(monkeypatch):
    """Two consecutive mist read calls on one loop construct exactly one client."""
    from hpe_networking_mcp.mcp_servers import mist

    constructions: list[object] = []

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self):
            return {"ok": True}

    class _FakeAsyncClient:
        def __init__(self, timeout=None, **_ignored):
            constructions.append(self)

        async def get(self, url, headers=None, params=None):
            return _Resp()

    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    monkeypatch.setattr(mist.httpx, "AsyncClient", _FakeAsyncClient)

    async def run():
        first = await mist._mist_get_request("/api/v1/self")
        second = await mist._mist_get_request("/api/v1/self")
        return first, second

    first, second = asyncio.run(run())

    assert first["status_code"] == 200
    assert second["status_code"] == 200
    assert len(constructions) == 1
