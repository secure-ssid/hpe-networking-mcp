"""Regression tests for the audited CentralClient response contracts.

Covers four reproduced defects:

- ``X-RateLimit-Reset`` carrying an absolute Unix epoch was parsed as
  delta-seconds, producing a ~55-year ``reset_seconds`` (and a matching
  retry wait) instead of the real window.
- The deprecation warning promised "once per process" in its docstring but
  fired on every single call to a deprecated endpoint.
- A 2xx response with a non-JSON body was logged and returned as ``{}``,
  which callers could not distinguish from a genuine empty success.
- Non-GET verbs were always gated on the *Central* write flag, so GLP writes
  riding the same transport were blocked (or unblocked) by Central's gate and
  the error told operators to flip the wrong env var.

No network calls.
"""

from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock

import httpx
import pytest

from hpe_networking_mcp.pipeline.clients import central_client as cc
from hpe_networking_mcp.pipeline.clients.central_client import (
    CentralClient,
    ResponseParseError,
    _extract_rate_limit,
    _parse_rate_limit_reset,
    reset_deprecation_warning_cache,
)
from hpe_networking_mcp.pipeline.clients.token_manager import TokenManager


def _response(status_code=200, headers=None, text="{}", method="GET"):
    return httpx.Response(
        status_code,
        headers=headers or {},
        content=text,
        request=httpx.Request(method, "https://test.example.com/x"),
    )


def _client(tmp_path, monkeypatch, write_platform="central"):
    monkeypatch.setenv("TOKEN_CACHE_DIR", str(tmp_path))

    class _TokenResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": "tok", "expires_in": 7200}

    monkeypatch.setattr(
        "hpe_networking_mcp.pipeline.clients.token_manager.httpx.post",
        lambda url, data=None, headers=None, timeout=None: _TokenResponse(),
    )
    tm = TokenManager(
        client_id="id",
        client_secret="secret",
        token_url="https://sso.example.com/token",
        cache_key=f"contracts-{write_platform}",
    )
    client = CentralClient(
        base_url="https://test.example.com",
        token_manager=tm,
        write_platform=write_platform,
    )
    client.session = MagicMock()
    client.session.headers = {}
    return client


# ---------------------------------------------------------------------------
# X-RateLimit-Reset epoch handling
# ---------------------------------------------------------------------------


class TestRateLimitResetParsing:
    def test_small_value_is_delta_seconds(self):
        assert _parse_rate_limit_reset("30") == 30.0

    def test_zero_is_preserved(self):
        assert _parse_rate_limit_reset("0") == 0.0

    def test_epoch_seconds_becomes_delta(self):
        future = time.time() + 45
        parsed = _parse_rate_limit_reset(str(int(future)))
        assert parsed is not None
        assert 30 <= parsed <= 60, f"epoch parsed as {parsed}s — expected ~45s"

    def test_epoch_milliseconds_becomes_delta(self):
        future_ms = int((time.time() + 90) * 1000)
        parsed = _parse_rate_limit_reset(str(future_ms))
        assert parsed is not None
        assert 75 <= parsed <= 105, f"epoch-ms parsed as {parsed}s — expected ~90s"

    def test_past_epoch_clamps_to_zero(self):
        assert _parse_rate_limit_reset(str(int(time.time()) - 500)) == 0.0

    def test_http_date_still_supported(self):
        assert _parse_rate_limit_reset("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0

    def test_blank_and_garbage_are_none(self):
        assert _parse_rate_limit_reset(None) is None
        assert _parse_rate_limit_reset("   ") is None
        assert _parse_rate_limit_reset("not-a-time") is None

    def test_x_ratelimit_epoch_header_end_to_end(self, tmp_path, monkeypatch):
        """The regression itself: X-RateLimit-Reset as an epoch must not
        surface as a multi-decade reset_seconds."""
        client = _client(tmp_path, monkeypatch)
        epoch = int(time.time()) + 60
        client.session.request.side_effect = [
            _response(200, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(epoch)})
        ]

        client.get("/x")

        status = client.rate_limit_status()
        assert status["raw_reset"] == str(epoch)
        assert 45 <= status["reset_seconds"] <= 75

    def test_ietf_delta_header_unchanged(self):
        status = _extract_rate_limit({"RateLimit-Limit": "100", "RateLimit-Reset": "12"})
        assert status.reset_seconds == 12.0
        assert status.limit == 100


# ---------------------------------------------------------------------------
# Deprecation warning deduplication
# ---------------------------------------------------------------------------


class TestDeprecationWarningDeduplication:
    def test_repeated_calls_warn_once(self, tmp_path, monkeypatch, caplog):
        reset_deprecation_warning_cache()
        client = _client(tmp_path, monkeypatch)
        headers = {"Deprecation": "true", "Sunset": "Wed, 11 Nov 2026 23:59:59 GMT"}
        client.session.request.side_effect = [_response(200, headers=headers) for _ in range(5)]

        with caplog.at_level(logging.WARNING, logger="hpe_networking_mcp.pipeline.clients.central_client"):
            for _ in range(5):
                client.get("/deprecated-endpoint")

        warnings = [r for r in caplog.records if "Deprecated endpoint" in r.message]
        assert len(warnings) == 1, f"expected 1 warning, got {len(warnings)}"
        # Metadata is still refreshed on every response.
        assert client.deprecation_status()["deprecation"] == "true"

    def test_distinct_endpoints_each_warn(self, tmp_path, monkeypatch, caplog):
        reset_deprecation_warning_cache()
        client = _client(tmp_path, monkeypatch)
        headers = {"Deprecation": "true"}
        client.session.request.side_effect = [_response(200, headers=headers) for _ in range(4)]

        with caplog.at_level(logging.WARNING, logger="hpe_networking_mcp.pipeline.clients.central_client"):
            client.get("/a")
            client.get("/a")
            client.get("/b")
            client.get("/b")

        warnings = [r for r in caplog.records if "Deprecated endpoint" in r.message]
        assert len(warnings) == 2

    def test_changed_sunset_date_warns_again(self, tmp_path, monkeypatch, caplog):
        reset_deprecation_warning_cache()
        client = _client(tmp_path, monkeypatch)
        client.session.request.side_effect = [
            _response(200, headers={"Deprecation": "true", "Sunset": "Wed, 11 Nov 2026 23:59:59 GMT"}),
            _response(200, headers={"Deprecation": "true", "Sunset": "Fri, 01 Jan 2027 00:00:00 GMT"}),
        ]

        with caplog.at_level(logging.WARNING, logger="hpe_networking_mcp.pipeline.clients.central_client"):
            client.get("/a")
            client.get("/a")

        warnings = [r for r in caplog.records if "Deprecated endpoint" in r.message]
        assert len(warnings) == 2


# ---------------------------------------------------------------------------
# Malformed 2xx JSON
# ---------------------------------------------------------------------------


class TestMalformedJsonDiagnostics:
    def test_html_error_page_with_200_raises(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        client.session.request.side_effect = [
            _response(200, headers={"Content-Type": "text/html"}, text="<html>gateway timeout</html>")
        ]

        with pytest.raises(ResponseParseError) as exc:
            client.get("/x")

        assert "Malformed JSON" in str(exc.value)
        assert "gateway timeout" in str(exc.value)
        assert exc.value.response.status_code == 200

    def test_truncated_json_raises(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        client.session.request.side_effect = [_response(200, text='{"items": [{"id": 1}')]

        with pytest.raises(ResponseParseError):
            client.get("/x")

    def test_empty_body_still_returns_empty_dict(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        client.session.request.side_effect = [_response(204, text="")]

        assert client.get("/x") == {}

    def test_whitespace_only_body_still_returns_empty_dict(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        client.session.request.side_effect = [_response(200, text="   \n ")]

        assert client.get("/x") == {}

    def test_json_list_body_is_wrapped(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        client.session.request.side_effect = [_response(200, text='[{"id": 1}]')]

        assert client.get("/x") == {"items": [{"id": 1}]}

    def test_parse_error_is_a_valueerror_for_legacy_handlers(self, tmp_path, monkeypatch):
        """Existing ``except ValueError`` handlers keep working."""
        client = _client(tmp_path, monkeypatch)
        client.session.request.side_effect = [_response(200, text="nope")]

        with pytest.raises(ValueError):
            client.get("/x")


# ---------------------------------------------------------------------------
# Write gate independence
# ---------------------------------------------------------------------------


class TestWriteGateIndependence:
    def test_default_platform_is_central(self, tmp_path, monkeypatch):
        assert _client(tmp_path, monkeypatch).write_platform == "central"

    def test_glp_transport_not_blocked_by_central_gate(self, tmp_path, monkeypatch):
        """Regression: a read-only Central deployment used to block GLP writes."""
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "0")
        monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
        client = _client(tmp_path, monkeypatch, write_platform="glp")
        client.session.request.side_effect = [_response(200, method="POST")]

        assert client.post("/devices/v2beta1/devices", data={}) == {}

    def test_glp_transport_blocked_names_the_glp_flag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
        monkeypatch.delenv("HPE_MCP_GLP_V2BETA1_WRITES", raising=False)
        client = _client(tmp_path, monkeypatch, write_platform="glp")

        with pytest.raises(PermissionError) as exc:
            client.post("/devices/v2beta1/devices", data={})

        message = str(exc.value)
        assert "GLP write requests are disabled" in message
        assert "HPE_MCP_GLP_V2BETA1_WRITES" in message
        assert "HPE_MCP_CENTRAL_WRITES" not in message
        client.session.request.assert_not_called()

    def test_central_transport_not_enabled_by_glp_gate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "0")
        monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
        client = _client(tmp_path, monkeypatch)

        with pytest.raises(PermissionError, match="Central write requests are disabled"):
            client.post("/network-config/v1/roles/x", data={})

    def test_reads_are_never_gated(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "0")
        monkeypatch.delenv("HPE_MCP_GLP_V2BETA1_WRITES", raising=False)
        for platform in ("central", "glp"):
            client = _client(tmp_path, monkeypatch, write_platform=platform)
            client.session.request.side_effect = [_response(200, text='{"ok": true}')]
            assert client.get("/x") == {"ok": True}

    def test_glp_client_wires_the_glp_platform(self, monkeypatch, tmp_path):
        from hpe_networking_mcp.pipeline.clients.glp_client import GLPClient

        monkeypatch.setenv("TOKEN_CACHE_DIR", str(tmp_path))
        token_manager = MagicMock()
        token_manager.get_access_token_with_generation.return_value = ("tok", 1)
        glp = GLPClient(
            token_manager=token_manager,
            workspace_id="ws",
            base_url="https://glp.example.com",
        )
        assert glp._client.write_platform == "glp"


def test_module_exposes_reset_helper_for_tests():
    """The dedup cache is process-wide, so it must be resettable."""
    assert callable(cc.reset_deprecation_warning_cache)
