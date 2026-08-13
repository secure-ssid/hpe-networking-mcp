"""Regression tests for four audited MCP-core hardening fixes.

* redact_sensitive must preserve ``_pagination.list_key`` (helper-generated
  pagination metadata the router's cursor/truncation logic reads) while still
  redacting real secrets nested under ``_pagination``.
* ResponseEnvelopeMiddleware must envelope a terminal ``status="FAILED"`` /
  ``"failure"`` result as an error (ok=False), not let it escape as an
  un-enveloped implicit success.
* install_middleware must route an ``on_error`` substitute through the
  ``after_call`` chain, so a substituted failure gets the same envelope
  treatment as any other result.
* device_type_for_troubleshoot must treat the generic ``SWITCH``/``SWITCHES``
  inventory value as "disambiguate via inventory", never emit the literal
  ``switch`` URL segment.
* The three local-index RAG tools carry READ_ONLY_LOCAL (open_world_hint=False).
"""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

import hpe_networking_mcp.mcp_servers.shared as sh
from hpe_networking_mcp.mcp_servers._middleware.install import install_middleware
from hpe_networking_mcp.mcp_servers._middleware.response_envelope import ResponseEnvelopeMiddleware


# ---------------------------------------------------------------------------
# Fix 2: redact_sensitive preserves _pagination.list_key
# ---------------------------------------------------------------------------
class TestRedactPreservesPagination:
    def test_list_key_survives_redaction(self):
        out = sh.redact_sensitive(
            {"items": [1, 2], "_pagination": {"list_key": "items", "limit": 50}}
        )
        assert out["_pagination"]["list_key"] == "items"
        assert out["_pagination"]["limit"] == 50

    def test_real_secret_under_pagination_still_redacted(self):
        out = sh.redact_sensitive(
            {"_pagination": {"list_key": "items", "api_token": "abc123"}}
        )
        assert out["_pagination"]["list_key"] == "items"
        assert out["_pagination"]["api_token"] == sh._REDACTED

    def test_non_dict_pagination_untouched_path(self):
        # A field literally named _pagination but not a dict falls through to
        # normal handling (not sensitive => passed through).
        out = sh.redact_sensitive({"_pagination": "n/a"})
        assert out["_pagination"] == "n/a"


# ---------------------------------------------------------------------------
# Fix 3: ResponseEnvelopeMiddleware envelopes FAILED/failure as errors
# ---------------------------------------------------------------------------
class TestFailedStatusEnvelope:
    def test_failed_status_is_enveloped(self):
        mw = ResponseEnvelopeMiddleware()
        for status in ("FAILED", "failed", "Failure"):
            out = mw.after_call("op", {}, {"status": status, "detail": "x"})
            assert out is not None and out["ok"] is False and out["status"] == 500

    def test_completed_status_passes_through(self):
        mw = ResponseEnvelopeMiddleware()
        assert mw.after_call("op", {}, {"status": "COMPLETED"}) is None


# ---------------------------------------------------------------------------
# Fix 4: on_error substitute is routed through after_call
# ---------------------------------------------------------------------------
class _OnErrorSubstitute:
    """Swallows the raised error and substitutes a hint dict."""

    def before_call(self, name, arguments):  # noqa: ANN001
        return None

    def after_call(self, name, arguments, result):  # noqa: ANN001
        return None

    def on_error(self, name, arguments, exc):  # noqa: ANN001
        return {"error": "boom", "status": "error"}


class _RecordingAfter:
    """Records everything that reaches after_call and tags it."""

    def __init__(self) -> None:
        self.seen: list[Any] = []

    def before_call(self, name, arguments):  # noqa: ANN001
        return None

    def after_call(self, name, arguments, result):  # noqa: ANN001
        self.seen.append(result)
        if isinstance(result, dict):
            return {**result, "enveloped": True}
        return None


class TestOnErrorRoutedThroughAfter:
    def test_substitute_passes_through_after_call(self):
        srv = MCPServer("err-backend")

        @srv.tool()
        def boom() -> dict[str, Any]:
            raise RuntimeError("kaboom")

        recorder = _RecordingAfter()
        install_middleware(srv, [_OnErrorSubstitute(), recorder])

        result = asyncio.run(srv._tool_manager.call_tool("boom", {}, Context(mcp_server=srv)))
        # The on_error substitute was seen by after_call and transformed.
        assert recorder.seen and recorder.seen[0]["error"] == "boom"
        assert result.get("enveloped") is True


# ---------------------------------------------------------------------------
# Fix 5: SWITCH/SWITCHES device-type uses inventory disambiguation
# ---------------------------------------------------------------------------
class _FakeMC:
    def __init__(self, device: dict[str, Any] | None) -> None:
        self._device = device
        self.calls = 0

    def get_device_by_serial(self, serial: str) -> dict[str, Any] | None:
        self.calls += 1
        return self._device


class TestDeviceTypeSwitch:
    def test_switch_falls_through_to_inventory(self, monkeypatch):
        fake = _FakeMC(None)
        monkeypatch.setattr(sh, "get_mcp_client", lambda: fake)
        for value in ("SWITCH", "switch", "SWITCHES", "switches"):
            fake.calls = 0
            result = sh.device_type_for_troubleshoot("S1", value)
            # Never the literal invalid segment; always consulted inventory.
            assert result != "switch"
            assert fake.calls == 1

    def test_known_type_still_maps_without_inventory(self, monkeypatch):
        fake = _FakeMC({"type": "CX"})
        monkeypatch.setattr(sh, "get_mcp_client", lambda: fake)
        assert sh.device_type_for_troubleshoot("S1", "AP") == "aps"
        assert fake.calls == 0

    def test_unknown_specific_type_passes_through_lowercased(self, monkeypatch):
        fake = _FakeMC({"type": "CX"})
        monkeypatch.setattr(sh, "get_mcp_client", lambda: fake)
        assert sh.device_type_for_troubleshoot("S1", "GATEWAYS") == "gateways"
        assert fake.calls == 0


# ---------------------------------------------------------------------------
# Fix 6: local RAG tools carry READ_ONLY_LOCAL (open_world_hint=False)
# ---------------------------------------------------------------------------
class TestRagLocalAnnotations:
    def test_read_only_local_profile(self):
        assert sh.READ_ONLY_LOCAL.read_only_hint is True
        assert sh.READ_ONLY_LOCAL.destructive_hint is False
        assert sh.READ_ONLY_LOCAL.open_world_hint is False

    def test_local_rag_tools_are_read_only_local(self):
        import hpe_networking_mcp.mcp_servers.rag as rag

        tools = rag.mcp._tool_manager._tools
        for name in ("search_docs", "lookup_api", "ask_docs"):
            ann = tools[name].annotations
            assert ann is sh.READ_ONLY_LOCAL, name
            assert ann.open_world_hint is False
