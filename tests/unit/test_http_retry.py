"""Unit tests for the shared GET-only async retry helper."""

from __future__ import annotations

import asyncio
import email.utils
import time

import httpx
import pytest

from hpe_networking_mcp.pipeline.clients import http_retry
from hpe_networking_mcp.pipeline.clients.http_retry import get_with_retry


class _FakeClient:
    """Minimal stand-in for httpx.AsyncClient — records calls, plays responses."""

    def __init__(self, script):
        self._script = list(script)
        self.get_calls: list[dict] = []
        self.request_calls: list[dict] = []

    async def get(self, url, **kwargs):
        self.get_calls.append({"url": url, **kwargs})
        outcome = self._script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def request(self, method, url, **kwargs):
        self.request_calls.append({"method": method, "url": url, **kwargs})
        outcome = self._script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _resp(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, json={"ok": status < 400})


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def no_sleep(monkeypatch):
    waits: list[float] = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(http_retry.asyncio, "sleep", fake_sleep)
    return waits


def test_retries_once_on_transient_503_get(no_sleep):
    client = _FakeClient([_resp(503), _resp(200)])

    resp = _run(get_with_retry(client, "https://api.example/items"))

    assert resp.status_code == 200
    assert len(client.get_calls) == 2
    assert len(no_sleep) == 1


def test_never_retries_past_max_and_returns_last_response(no_sleep):
    client = _FakeClient([_resp(503), _resp(503), _resp(503)])

    resp = _run(get_with_retry(client, "https://api.example/items", max_retries=2))

    assert resp.status_code == 503
    assert len(client.get_calls) == 3


def test_does_not_retry_client_errors(no_sleep):
    client = _FakeClient([_resp(400), _resp(200)])

    resp = _run(get_with_retry(client, "https://api.example/items"))

    assert resp.status_code == 400
    assert len(client.get_calls) == 1
    assert no_sleep == []


def test_honors_retry_after_http_date(no_sleep):
    from datetime import datetime, timezone

    header = email.utils.format_datetime(
        datetime.fromtimestamp(time.time() + 30, tz=timezone.utc)
    )
    client = _FakeClient([_resp(429, {"Retry-After": header}), _resp(200)])

    resp = _run(get_with_retry(client, "https://api.example/items"))

    assert resp.status_code == 200
    assert len(no_sleep) == 1
    # HTTP-date resolved to ~30s (allowing test execution slack), not the backoff default.
    assert 20.0 < no_sleep[0] <= 30.0


def test_retries_transport_error_once(no_sleep):
    client = _FakeClient(
        [httpx.ConnectError("connection reset"), _resp(200)]
    )

    resp = _run(get_with_retry(client, "https://api.example/items"))

    assert resp.status_code == 200
    assert len(client.get_calls) == 2


def test_helper_issues_gets_only(no_sleep):
    """The helper has no method parameter: only .get may ever be invoked."""
    client = _FakeClient([_resp(503), _resp(200)])

    _run(get_with_retry(client, "https://api.example/items"))

    assert client.request_calls == []
    assert len(client.get_calls) == 2


class TestRequestReadRetried:
    """The generic-executor dispatch shim: retry coverage for bodiless GETs,
    single-attempt passthrough for everything else (the write guardrail)."""

    def test_get_retries_transient_503(self, no_sleep):
        client = _FakeClient([_resp(503), _resp(200)])

        resp = _run(
            http_retry.request_read_retried(
                client, "GET", "https://api.example/items", headers={"a": "b"}
            )
        )

        assert resp.status_code == 200
        assert len(client.get_calls) == 2
        assert client.request_calls == []

    def test_post_is_never_retried(self, no_sleep):
        client = _FakeClient([_resp(503), _resp(503)])

        resp = _run(
            http_retry.request_read_retried(
                client, "POST", "https://api.example/items", json={"x": 1}
            )
        )

        assert resp.status_code == 503
        assert len(client.request_calls) == 1
        assert client.get_calls == []
        assert no_sleep == []

    def test_get_with_a_body_is_not_retried(self, no_sleep):
        """A GET carrying a body is not the plain read path; keep it single-attempt."""
        client = _FakeClient([_resp(503)])

        resp = _run(
            http_retry.request_read_retried(
                client, "GET", "https://api.example/search", json={"q": 1}
            )
        )

        assert resp.status_code == 503
        assert len(client.request_calls) == 1
        assert client.get_calls == []
