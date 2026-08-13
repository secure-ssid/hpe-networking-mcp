"""Unit tests for network-troubleshooting API version selection.

Covers:
- Default order is v1 first, v1alpha1 fallback.
- HPE_MCP_TROUBLESHOOTING_API_VERSION=v1alpha1/legacy pins the legacy
  order for tenants that still require it.
- troubleshooting_endpoint_candidates() builds the right ordered list.
- atroubleshoot_async() falls back to the next candidate on 404 and stops
  trying once one candidate succeeds or a non-404 failure occurs.
"""

from __future__ import annotations

import asyncio

import pytest

from hpe_networking_mcp.mcp_servers import shared


class TestVersionOrder:
    def test_default_order_is_v1_first(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_TROUBLESHOOTING_API_VERSION", raising=False)
        assert shared.troubleshooting_version_order() == ("v1", "v1alpha1")

    @pytest.mark.parametrize("value", ["v1alpha1", "legacy", "LEGACY"])
    def test_legacy_override_pins_v1alpha1_first(self, monkeypatch, value):
        monkeypatch.setenv("HPE_MCP_TROUBLESHOOTING_API_VERSION", value)
        assert shared.troubleshooting_version_order() == ("v1alpha1", "v1")

    def test_unrecognized_override_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TROUBLESHOOTING_API_VERSION", "v99")
        assert shared.troubleshooting_version_order() == ("v1", "v1alpha1")

    def test_module_constants_resolve_to_v1_by_default(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_TROUBLESHOOTING_API_VERSION", raising=False)
        assert shared.troubleshooting_base("cx") == "/network-troubleshooting/v1/cx"


class TestCandidates:
    def test_default_candidates_are_v1_then_v1alpha1(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_TROUBLESHOOTING_API_VERSION", raising=False)

        candidates = shared.troubleshooting_endpoint_candidates("cx", "CX1", "ping")

        assert candidates == [
            "/network-troubleshooting/v1/cx/CX1/ping",
            "/network-troubleshooting/v1alpha1/cx/CX1/ping",
        ]

    def test_legacy_pin_reverses_order(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TROUBLESHOOTING_API_VERSION", "legacy")

        candidates = shared.troubleshooting_endpoint_candidates("aos-s", "SW1", "traceroute")

        assert candidates == [
            "/network-troubleshooting/v1alpha1/aos-s/SW1/traceroute",
            "/network-troubleshooting/v1/aos-s/SW1/traceroute",
        ]


class _Response:
    def __init__(self, status_code, location=""):
        self.status_code = status_code
        self.headers = {"Location": location} if location else {}

    def json(self):
        return {}


class TestAtroubleshootAsyncFallback:
    def test_falls_back_to_second_candidate_on_404(self, monkeypatch):
        async def fake_sleep(_seconds):
            return None

        monkeypatch.setattr(shared.asyncio, "sleep", fake_sleep)

        posted = []

        class FakeClient:
            async def _arequest(self, method, endpoint, **kwargs):
                posted.append(endpoint)
                if endpoint.endswith("/v1/cx/CX1/ping"):
                    return _Response(404)
                return _Response(202, location=f"{endpoint}/async-operations/task-1")

            async def aget(self, endpoint):
                return {"status": "COMPLETED"}

        candidates = shared.troubleshooting_endpoint_candidates("cx", "CX1", "ping")
        result = asyncio.run(shared.atroubleshoot_async(FakeClient(), candidates, {}, []))

        assert result == {
            "status": "COMPLETED",
            "errors": [],
            "endpoint_used": "/network-troubleshooting/v1alpha1/cx/CX1/ping",
        }
        assert posted == [
            "/network-troubleshooting/v1/cx/CX1/ping",
            "/network-troubleshooting/v1alpha1/cx/CX1/ping",
        ]

    def test_stops_at_first_success_without_trying_fallback(self, monkeypatch):
        async def fake_sleep(_seconds):
            return None

        monkeypatch.setattr(shared.asyncio, "sleep", fake_sleep)
        posted = []

        class FakeClient:
            async def _arequest(self, method, endpoint, **kwargs):
                posted.append(endpoint)
                return _Response(202, location=f"{endpoint}/async-operations/task-1")

            async def aget(self, endpoint):
                return {"status": "COMPLETED"}

        candidates = shared.troubleshooting_endpoint_candidates("cx", "CX1", "ping")
        asyncio.run(shared.atroubleshoot_async(FakeClient(), candidates, {}, []))

        assert posted == ["/network-troubleshooting/v1/cx/CX1/ping"]

    def test_non_404_failure_on_first_candidate_does_not_fall_back(self, monkeypatch):
        posted = []

        class FakeClient:
            async def _arequest(self, method, endpoint, **kwargs):
                posted.append(endpoint)
                return _Response(400)

        candidates = shared.troubleshooting_endpoint_candidates("cx", "CX1", "ping")
        result = asyncio.run(shared.atroubleshoot_async(FakeClient(), candidates, {}, []))

        assert posted == ["/network-troubleshooting/v1/cx/CX1/ping"]
        assert result["status"] is None
        assert result["errors"] == ["HTTP 400: "]

    def test_404_on_last_candidate_is_returned_as_failure(self, monkeypatch):
        class FakeClient:
            async def _arequest(self, method, endpoint, **kwargs):
                return _Response(404)

        result = asyncio.run(shared.atroubleshoot_async(FakeClient(), ["/only/candidate"], {}, []))

        assert result["status"] is None
        assert "404" in result["errors"][0]

    def test_single_string_endpoint_still_works(self, monkeypatch):
        async def fake_sleep(_seconds):
            return None

        monkeypatch.setattr(shared.asyncio, "sleep", fake_sleep)

        class FakeClient:
            async def _arequest(self, method, endpoint, **kwargs):
                return _Response(202, location=f"{endpoint}/async-operations/task-1")

            async def aget(self, endpoint):
                return {"status": "COMPLETED"}

        result = asyncio.run(shared.atroubleshoot_async(FakeClient(), "/x", {}, []))

        assert result == {"status": "COMPLETED", "errors": [], "endpoint_used": "/x"}

    def test_empty_candidate_list_returns_structured_error(self):
        result = asyncio.run(shared.atroubleshoot_async(object(), [], {}, []))
        assert result == {
            "status": None,
            "errors": ["no troubleshooting endpoint candidates provided"],
        }
