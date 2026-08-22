"""Per-platform pooled ``httpx.AsyncClient`` registry.

Constructing a fresh ``AsyncClient`` per tool call pays a full TCP+TLS
handshake on every invocation and applies one flat 30s timeout with no
connect-phase bound, so a stalled connect occupies the whole window. This
module hands out one shared client per platform — structured timeout,
bounded connection pool — reused across calls for the life of the process.

Clients are keyed by platform name *and* the running event loop: a suite (or
embedder) that drives calls through repeated ``asyncio.run()`` gets a fresh
client per loop instead of a client bound to a closed loop. A superseded
client bound to a dead loop cannot be closed from the new loop and is
dropped for GC; on a real server (one long-lived loop) recreation never
happens.
"""

from __future__ import annotations

import asyncio

import httpx

_POOL: dict[str, tuple[asyncio.AbstractEventLoop, httpx.AsyncClient]] = {}


def pooled_client(
    name: str,
    *,
    timeout: float = 30.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Return the shared client for ``name``, creating it on first use per loop.

    ``timeout`` and ``transport`` only apply at creation; the first caller's
    values win for the life of the pooled instance. ``transport`` exists so
    tests can inject ``httpx.MockTransport`` without monkey-patching.
    """
    loop = asyncio.get_running_loop()
    entry = _POOL.get(name)
    if entry is not None:
        client_loop, client = entry
        # getattr: test doubles that stand in for AsyncClient don't always
        # carry `is_closed`; a client that can't report closed is open.
        if client_loop is loop and not getattr(client, "is_closed", False):
            return client
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=10.0),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        transport=transport,
    )
    _POOL[name] = (loop, client)
    return client


async def aclose_pooled_clients() -> None:
    """Close every pooled client bound to the *current* loop and drop the rest.

    Entries bound to other (closed) loops cannot be awaited and are simply
    discarded. Call from server shutdown or test teardown.
    """
    loop = asyncio.get_running_loop()
    for name, (client_loop, client) in list(_POOL.items()):
        del _POOL[name]
        if client_loop is loop and not getattr(client, "is_closed", False):
            await client.aclose()
