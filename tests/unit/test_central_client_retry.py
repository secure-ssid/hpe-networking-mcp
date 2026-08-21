"""Unit tests for CentralClient._request retry behavior.

Covers the cautious-review test bar:
- 429 + Retry-After honored (integer and HTTP-date forms)
- 5xx retried with backoff for idempotent methods
- 4xx (non-429) NOT retried
- Max-attempts cap enforced
"""

from __future__ import annotations

import asyncio
import datetime
import email.utils
import time
from unittest.mock import MagicMock

import httpx
import pytest

from hpe_networking_mcp.pipeline.clients import central_client
from hpe_networking_mcp.pipeline.clients.central_client import (
    CentralClient,
    _parse_retry_after,
)

# ---------------------------------------------------------------------------
# Retry-After parser
# ---------------------------------------------------------------------------


class TestParseRetryAfter:
    def test_integer_seconds(self):
        assert _parse_retry_after("30") == 30.0

    def test_float_seconds(self):
        assert _parse_retry_after("2.5") == 2.5

    def test_negative_clamped_to_zero(self):
        assert _parse_retry_after("-5") == 0.0

    def test_http_date_in_future(self):
        # Build a date ~10s in the future; allow 2s slop for test timing.
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=10)
        header = email.utils.format_datetime(future)
        parsed = _parse_retry_after(header)
        assert parsed is not None
        assert 7 < parsed < 12, f"expected ~10s, got {parsed}"

    def test_http_date_in_past_clamps_to_zero(self):
        assert _parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0

    def test_unparseable_returns_none(self):
        assert _parse_retry_after("tomorrow") is None
        assert _parse_retry_after("") is None
        assert _parse_retry_after("nan") is None
        assert _parse_retry_after("inf") is None


# ---------------------------------------------------------------------------
# _request retry behavior
# ---------------------------------------------------------------------------


def _make_response(status_code, headers=None, text="{}"):
    r = MagicMock()
    r.status_code = status_code
    r.headers = headers or {}
    r.text = text
    r.json.return_value = {}
    r.is_success = 200 <= status_code < 300
    return r


def _make_httpx_response(status_code, headers=None, text="{}"):
    return httpx.Response(
        status_code,
        headers=headers or {},
        content=text,
        request=httpx.Request("POST", "https://test.example.com/x"),
    )


def _make_token_manager(token: str = "fake-token", generation: int = 0):
    """A MagicMock TokenManager wired for both the sync and async 401 paths."""
    tm = MagicMock()
    tm.get_access_token.return_value = token
    tm.generation = generation
    tm.get_access_token_with_generation.return_value = (token, generation)
    return tm


def _make_client(responses, token_manager=None, *, write_platform="central"):
    """Build a CentralClient whose session yields ``responses`` in sequence."""
    tm = token_manager if token_manager is not None else _make_token_manager()
    client = CentralClient(
        base_url="https://test.example.com",
        token_manager=tm,
        write_platform=write_platform,
    )
    # Replace session with one that returns our scripted responses.
    client.session = MagicMock()
    client.session.headers = {}
    resp_iter = iter(responses)
    client.session.request.side_effect = lambda *a, **k: next(resp_iter)
    return client


class TestRetryBehavior:
    def test_429_honors_integer_retry_after(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

        client = _make_client([
            _make_response(429, {"Retry-After": "2"}),
            _make_response(200),
        ])
        resp = client._request("GET", "/x")
        assert resp.status_code == 200
        assert sleeps == [2.0]

    def test_429_without_header_uses_default_backoff(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

        client = _make_client([
            _make_response(429),
            _make_response(200),
        ])
        resp = client._request("GET", "/x")
        assert resp.status_code == 200
        # Legacy default is 60s.
        assert sleeps == [60.0]

    def test_5xx_retried_for_get(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

        client = _make_client([
            _make_response(503),
            _make_response(502),
            _make_response(200),
        ])
        resp = client._request("GET", "/x")
        assert resp.status_code == 200
        # Two retries, each waits >= 0.8s (with jitter) and <= 30s.
        assert len(sleeps) == 2
        for s in sleeps:
            assert 0.3 < s < 30.0

    def test_5xx_not_retried_for_post_by_default(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda s: None)
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
        client = _make_client([
            _make_response(503),
        ])
        resp = client._request("POST", "/x")
        assert resp.status_code == 503  # returned immediately, no retry

    def test_5xx_retried_for_post_when_opted_in(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda s: None)
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
        client = _make_client([
            _make_response(503),
            _make_response(200),
        ])
        resp = client._request("POST", "/x", retry_5xx=True)
        assert resp.status_code == 200

    def test_4xx_non_429_not_retried(self, monkeypatch):
        """A 400/403/404 comes back immediately — don't retry client errors."""
        monkeypatch.setattr(time, "sleep", lambda s: None)
        for code in (400, 403, 404):
            client = _make_client([_make_response(code)])
            resp = client._request("GET", "/x")
            assert resp.status_code == code

    def test_401_forces_token_refresh_and_retries(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda s: None)
        tm = _make_token_manager(generation=3)
        client = CentralClient(base_url="https://test.example.com", token_manager=tm)
        client.session = MagicMock()
        client.session.headers = {}
        client.session.request.side_effect = [
            _make_response(401),
            _make_response(200),
        ]

        resp = client._request("GET", "/x")

        assert resp.status_code == 200
        # The generation observed when the (now-rejected) token was set is
        # threaded through so concurrent 401s can collapse into one refresh.
        tm.get_access_token.assert_any_call(force_refresh=True, observed_generation=3)
        assert client.session.request.call_count == 2

    def test_max_retries_cap_enforced(self, monkeypatch):
        """After max_retries, return the last response even if still transient."""
        monkeypatch.setattr(time, "sleep", lambda s: None)
        # max_retries=2 → up to 3 total attempts.
        client = _make_client([
            _make_response(503),
            _make_response(503),
            _make_response(503),
        ])
        resp = client._request("GET", "/x", max_retries=2)
        assert resp.status_code == 503

    def test_retry_after_clamped_to_max(self, monkeypatch):
        """Server says 'wait an hour' — we clamp to MAX_RETRY_DELAY (300s)."""
        sleeps: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

        client = _make_client([
            _make_response(429, {"Retry-After": "3600"}),
            _make_response(200),
        ])
        client._request("GET", "/x")
        assert sleeps == [300.0]

    def test_post_accepts_real_httpx_success_response(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
        client = _make_client([
            _make_httpx_response(200, text='{"accepted": true}'),
        ])

        assert client.post("/x") == {"accepted": True}

    def test_post_async_accepts_real_httpx_success_response(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
        client = _make_client([
            _make_httpx_response(202, headers={"Location": "/task/1"}),
        ])

        assert client.post_async("/x") == "/task/1"


class TestCentralWriteGate:
    def test_sync_write_is_blocked_before_network_call(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "0")
        client = _make_client([_make_response(200)])

        with pytest.raises(PermissionError, match="Central write requests are disabled"):
            client._request("POST", "/x")

        client.session.request.assert_not_called()

    def test_diagnostic_post_is_allowed_when_writes_are_disabled(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "safe-read-only")
        client = _make_client([_make_response(202)])

        response = client._request(
            "POST",
            "/network-troubleshooting/v1/cx/CX1/ping",
            diagnostic=True,
        )

        assert response.status_code == 202
        client.session.request.assert_called_once()

    def test_diagnostic_post_cannot_bypass_glp_transport_gate(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "safe-read-only")
        client = _make_client(
            [_make_response(202)],
            write_platform="glp",
        )

        with pytest.raises(PermissionError, match="GLP write requests are disabled"):
            client._request(
                "POST",
                "/network-troubleshooting/v1/cx/CX1/ping",
                diagnostic=True,
            )

        client.session.request.assert_not_called()

    def test_diagnostic_flag_cannot_bypass_for_destructive_action(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "safe-read-only")
        client = _make_client([_make_response(202)])

        with pytest.raises(PermissionError, match="Central write requests are disabled"):
            client._request(
                "POST",
                "/network-troubleshooting/v1/cx/CX1/portBounce",
                diagnostic=True,
            )

        client.session.request.assert_not_called()

    @pytest.mark.parametrize(
        "endpoint",
        [
            "/network-troubleshooting/v1/cx/../ping",
            "/network-troubleshooting/v1/cx/%2e%2e/ping",
            "/network-troubleshooting/v1/cx/CX1/ping?next=/network-config/v1",
            "https://example.invalid/network-troubleshooting/v1/cx/CX1/ping",
        ],
    )
    def test_diagnostic_flag_rejects_noncanonical_paths(
        self,
        monkeypatch,
        endpoint,
    ):
        monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "safe-read-only")
        client = _make_client([_make_response(202)])

        with pytest.raises(PermissionError, match="Central write requests are disabled"):
            client._request("POST", endpoint, diagnostic=True)

        client.session.request.assert_not_called()

    def test_async_write_is_blocked_before_client_creation(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "0")
        client = _make_client([])
        async_client = MagicMock(side_effect=AssertionError("network client should not open"))
        monkeypatch.setattr(central_client.httpx, "AsyncClient", async_client)

        with pytest.raises(PermissionError, match="Central write requests are disabled"):
            asyncio.run(client._arequest("PATCH", "/x"))

        async_client.assert_not_called()


class TestAsyncRetryBehavior:
    def test_arequest_uses_async_httpx_and_async_sleep(self, monkeypatch):
        sleeps: list[float] = []
        calls: list[dict] = []
        responses = iter(
            [
                _make_httpx_response(429, headers={"Retry-After": "2"}),
                _make_httpx_response(200),
            ]
        )

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                self.kwargs = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def request(self, method, url, **kwargs):
                calls.append({"method": method, "url": url, "kwargs": kwargs})
                return next(responses)

        monkeypatch.setattr(central_client.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(central_client.httpx, "AsyncClient", FakeAsyncClient)

        client = _make_client([])
        resp = asyncio.run(client._arequest("GET", "/x"))

        assert resp.status_code == 200
        assert sleeps == [2.0]
        assert [call["method"] for call in calls] == ["GET", "GET"]
        assert calls[0]["url"] == "https://test.example.com/x"
        assert calls[0]["kwargs"]["headers"]["Authorization"] == "Bearer fake-token"

    def test_aget_parses_json_response(self, monkeypatch):
        calls: list[dict] = []
        responses = iter([_make_httpx_response(200, text='{"ok": true}')])

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def request(self, method, url, **kwargs):
                calls.append({"method": method, "url": url, "kwargs": kwargs})
                return next(responses)

        monkeypatch.setattr(central_client.httpx, "AsyncClient", FakeAsyncClient)

        client = _make_client([])
        result = asyncio.run(client.aget("/poll/1", params={"limit": 1}))

        assert result == {"ok": True}
        assert calls == [
            {
                "method": "GET",
                "url": "https://test.example.com/poll/1",
                "kwargs": {
                    "headers": {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer fake-token",
                    },
                    "params": {"limit": 1},
                },
            }
        ]
