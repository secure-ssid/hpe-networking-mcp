"""Unit tests for CentralClient response metadata (rate-limit / deprecation)
and generation-aware 401 handling.

Covers the audited hardening items:
- RateLimit-* / X-RateLimit-* headers are parsed into ``rate_limit_status()``.
- Deprecation / Sunset / Link headers are parsed into ``deprecation_status()``
  and logged as a warning.
- The sync and async 401 retry paths pass the generation observed when the
  now-rejected token was issued, so TokenManager can collapse concurrent
  401s into a single real refresh (integration with the real TokenManager,
  not just a MagicMock check).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from unittest.mock import MagicMock

import httpx

from hpe_networking_mcp.pipeline.clients.central_client import CentralClient, reset_deprecation_warning_cache
from hpe_networking_mcp.pipeline.clients.token_manager import TokenManager


def _make_httpx_response(status_code, headers=None, text="{}"):
    return httpx.Response(
        status_code,
        headers=headers or {},
        content=text,
        request=httpx.Request("GET", "https://test.example.com/x"),
    )


def _make_client_with_real_token_manager(tmp_path, monkeypatch, tokens=("token-1",)):
    monkeypatch.setenv("TOKEN_CACHE_DIR", str(tmp_path))
    token_iter = iter(tokens)

    class _TokenResponse:
        def __init__(self, token):
            self._token = token

        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": self._token, "expires_in": 7200}

    def fake_post(url, data=None, headers=None, timeout=None):
        return _TokenResponse(next(token_iter))

    monkeypatch.setattr("hpe_networking_mcp.pipeline.clients.token_manager.httpx.post", fake_post)

    tm = TokenManager(
        client_id="client-id",
        client_secret="secret",
        token_url="https://sso.example.com/token",
        cache_key="metadata-test",
    )
    client = CentralClient(base_url="https://test.example.com", token_manager=tm)
    client.session = MagicMock()
    client.session.headers = {}
    return client, tm


class TestRateLimitMetadata:
    def test_rate_limit_headers_are_parsed(self, tmp_path, monkeypatch):
        client, _tm = _make_client_with_real_token_manager(tmp_path, monkeypatch)
        client.session.request.side_effect = [
            _make_httpx_response(
                200,
                headers={
                    "RateLimit-Limit": "1000",
                    "RateLimit-Remaining": "42",
                    "RateLimit-Reset": "30",
                },
            )
        ]

        assert client.rate_limit_status() is None
        client.get("/x")

        status = client.rate_limit_status()
        assert status == {
            "limit": 1000,
            "remaining": 42,
            "reset_seconds": 30.0,
            "raw_reset": "30",
        }

    def test_x_ratelimit_fallback_headers_are_parsed(self, tmp_path, monkeypatch):
        client, _tm = _make_client_with_real_token_manager(tmp_path, monkeypatch)
        client.session.request.side_effect = [
            _make_httpx_response(
                200,
                headers={"X-RateLimit-Limit": "10", "X-RateLimit-Remaining": "1"},
            )
        ]

        client.get("/x")

        status = client.rate_limit_status()
        assert status["limit"] == 10
        assert status["remaining"] == 1

    def test_no_rate_limit_headers_leaves_status_none(self, tmp_path, monkeypatch):
        client, _tm = _make_client_with_real_token_manager(tmp_path, monkeypatch)
        client.session.request.side_effect = [_make_httpx_response(200)]

        client.get("/x")

        assert client.rate_limit_status() is None


class TestDeprecationMetadata:
    def test_deprecation_and_sunset_headers_are_parsed_and_logged(
        self, tmp_path, monkeypatch, caplog
    ):
        # The warning is deduplicated per process/endpoint, so clear the cache
        # to stay independent of test ordering.
        reset_deprecation_warning_cache()
        client, _tm = _make_client_with_real_token_manager(tmp_path, monkeypatch)
        client.session.request.side_effect = [
            _make_httpx_response(
                200,
                headers={
                    "Deprecation": "true",
                    "Sunset": "Wed, 11 Nov 2026 23:59:59 GMT",
                    "Link": "<https://developer.arubanetworks.com/deprecated>; rel=\"deprecation\"",
                },
            )
        ]

        with caplog.at_level(logging.WARNING):
            client.get("/x")

        status = client.deprecation_status()
        assert status["deprecation"] == "true"
        assert status["sunset"] == "Wed, 11 Nov 2026 23:59:59 GMT"
        assert "deprecation" in status["link"]
        assert any("Deprecated endpoint" in record.message for record in caplog.records)

    def test_no_deprecation_headers_leaves_status_none(self, tmp_path, monkeypatch):
        client, _tm = _make_client_with_real_token_manager(tmp_path, monkeypatch)
        client.session.request.side_effect = [_make_httpx_response(200)]

        client.get("/x")

        assert client.deprecation_status() is None


class TestGenerationAware401Collapse:
    def test_sync_concurrent_401s_on_shared_client_collapse_to_one_refresh(
        self, tmp_path, monkeypatch
    ):
        """Two threads sharing ONE CentralClient (mirroring the process-wide
        singleton from hpe_networking_mcp.mcp_servers.shared.get_client()) both read the same
        token, both get 401'd at (as close as possible to) the same time --
        only ONE of them should trigger a real token refresh; the other
        must see the already-refreshed token via generation comparison."""
        client, tm = _make_client_with_real_token_manager(
            tmp_path, monkeypatch, tokens=("token-1", "token-2", "token-3")
        )
        monkeypatch.setattr("hpe_networking_mcp.pipeline.clients.central_client.time.sleep", lambda s: None)
        assert tm.generation == 1

        barrier = threading.Barrier(2, timeout=5)
        counter_lock = threading.Lock()
        call_count = {"n": 0}

        def request_side_effect(method, url, **kwargs):
            with counter_lock:
                call_count["n"] += 1
                n = call_count["n"]
            if n <= 2:
                # Hold both threads here until both have sent their first
                # request with the SAME (still-generation-1) token, so
                # neither has refreshed before the other's 401 arrives.
                barrier.wait()
                return _make_httpx_response(401)
            return _make_httpx_response(200)

        client.session.request.side_effect = request_side_effect

        results: list[int] = []
        results_lock = threading.Lock()

        def worker():
            resp = client._request("GET", "/x")
            with results_lock:
                results.append(resp.status_code)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert results == [200, 200]
        # Constructor fetch (generation 1) + exactly one collapsed refresh
        # for both concurrent 401s (generation 2) -- never a 3rd (token-3
        # is never consumed).
        assert tm.generation == 2

    def test_async_401_passes_generation_through(self, tmp_path, monkeypatch):
        client, tm = _make_client_with_real_token_manager(
            tmp_path, monkeypatch, tokens=("token-1", "token-2")
        )
        monkeypatch.setattr("hpe_networking_mcp.pipeline.clients.central_client.asyncio.sleep", _fake_async_sleep)

        responses = iter(
            [
                _make_httpx_response(401),
                _make_httpx_response(200),
            ]
        )

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def request(self, method, url, **kwargs):
                return next(responses)

        monkeypatch.setattr("hpe_networking_mcp.pipeline.clients.central_client.httpx.AsyncClient", FakeAsyncClient)

        resp = asyncio.run(client._arequest("GET", "/x"))

        assert resp.status_code == 200
        # The 401 path should not have forced a redundant refresh beyond
        # what a legitimately-rejected token requires (generation moves
        # from 1 -> 2 exactly once).
        assert tm.generation == 2


async def _fake_async_sleep(_seconds):
    return None
