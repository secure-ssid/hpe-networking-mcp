from __future__ import annotations

import httpx

from hpe_networking_mcp.pipeline.clients.glp_client import _compact_exception_message


def test_compact_exception_message_supports_httpx_reason_phrase():
    request = httpx.Request("GET", "https://global.api.greenlake.hpe.com/devices")
    response = httpx.Response(
        429,
        json={"message": "rate limited"},
        request=request,
    )
    exc = httpx.HTTPStatusError("too many requests", request=request, response=response)

    message = _compact_exception_message(exc)

    assert message == "HTTP 429 Too Many Requests: {'message': 'rate limited'}"
