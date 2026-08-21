"""Unit tests for SecretTokenizeMiddleware and the context-passing extension
to hpe_networking_mcp.mcp_servers._middleware.install that it relies on.

Covers:
- Disabled by default (HPE_MCP_TOKENIZE_SECRETS unset) — every hook is
  a strict no-op, byte-for-byte, so adopting this middleware is safe.
- Enabled: sensitive-keyed result values become opaque hpe_mcp_secret_* tokens
  and are resolved back to plaintext when echoed into a later call's
  arguments — round trip without the plaintext ever being the return value
  the model sees.
- Bounded by max_entries (oldest evicted) and ttl_seconds (expired entries
  stop resolving).
- Session scoping: two different Context.session identities get
  independent vaults — a token minted in session A does not resolve in
  session B.
- Existing middlewares' 2/3-arg hook signatures are unaffected by the
  install.py context-passing change (backward compatibility guard).
"""

from __future__ import annotations

import gc
import time
from types import SimpleNamespace
from typing import Any

import pytest

from hpe_networking_mcp.mcp_servers._middleware import (
    NullStripMiddleware,
    SecretTokenizeMiddleware,
    install_middleware,
)
from hpe_networking_mcp.mcp_servers._middleware.secret_tokenizer import _TOKEN_RE, tokenize_enabled


def _ctx(session_obj: Any) -> SimpleNamespace:
    return SimpleNamespace(session=session_obj)


class TestDisabledByDefault:
    def test_tokenize_enabled_false_without_env(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_TOKENIZE_SECRETS", raising=False)
        assert tokenize_enabled() is False

    def test_after_call_is_noop_when_disabled(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_TOKENIZE_SECRETS", raising=False)
        mw = SecretTokenizeMiddleware()

        result = mw.after_call("get_webhook", {}, {"webhookSecret": "sekrit"})

        assert result is None

    def test_before_call_is_noop_when_disabled(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_TOKENIZE_SECRETS", raising=False)
        mw = SecretTokenizeMiddleware()

        result = mw.before_call("set_webhook", {"webhookSecret": "hpe_mcp_secret_" + "a" * 32})

        assert result is None


class TestEnabledRoundTrip:
    def test_sensitive_value_is_tokenized_in_result(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_SECRETS", "1")
        mw = SecretTokenizeMiddleware()

        result = mw.after_call("get_webhook", {}, {"webhookSecret": "sekrit-value"})

        assert result is not None
        token = result["webhookSecret"]
        assert token != "sekrit-value"
        assert _TOKEN_RE.fullmatch(token)

    def test_token_round_trips_back_to_plaintext_on_later_call(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_SECRETS", "1")
        mw = SecretTokenizeMiddleware()

        tokenized = mw.after_call("get_webhook", {}, {"webhookSecret": "sekrit-value"})
        token = tokenized["webhookSecret"]

        resolved = mw.before_call("set_webhook", {"webhookSecret": token})

        assert resolved == {"webhookSecret": "sekrit-value"}

    def test_non_sensitive_values_are_left_alone(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_SECRETS", "1")
        mw = SecretTokenizeMiddleware()

        result = mw.after_call("list_devices", {}, {"serial": "CN1234567890", "count": 3})

        assert result is None

    def test_already_redacted_value_is_not_tokenized(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_SECRETS", "1")
        mw = SecretTokenizeMiddleware()

        result = mw.after_call("get_webhook", {}, {"webhookSecret": "******"})

        assert result is None

    def test_nested_sensitive_values_are_tokenized(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_SECRETS", "1")
        mw = SecretTokenizeMiddleware()

        payload = {"webhook": {"config": {"token": "deep-secret"}}}
        result = mw.after_call("get_webhook", {}, payload)

        assert result is not None
        token_value = result["webhook"]["config"]["token"]
        assert _TOKEN_RE.fullmatch(token_value)

    def test_list_of_sensitive_dicts_are_tokenized(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_SECRETS", "1")
        mw = SecretTokenizeMiddleware()

        payload = {"webhooks": [{"secret": "one"}, {"secret": "two"}]}
        result = mw.after_call("list_webhooks", {}, payload)

        secrets_out = [entry["secret"] for entry in result["webhooks"]]
        assert all(_TOKEN_RE.fullmatch(s) for s in secrets_out)
        assert secrets_out[0] != secrets_out[1]

    def test_unknown_token_string_passed_through_unchanged(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_SECRETS", "1")
        mw = SecretTokenizeMiddleware()

        # A model that never received this token (or one from a different
        # session -- see TestSessionScoping) gets it back unresolved rather
        # than the call silently proceeding with a bogus placeholder.
        result = mw.before_call("set_webhook", {"webhookSecret": "hpe_mcp_secret_" + "0" * 32})

        assert result is None


class TestBounding:
    def test_max_entries_evicts_oldest(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_SECRETS", "1")
        mw = SecretTokenizeMiddleware(max_entries=2)

        first = mw.after_call("get_webhook", {}, {"secret": "first"})["secret"]
        mw.after_call("get_webhook", {}, {"secret": "second"})
        mw.after_call("get_webhook", {}, {"secret": "third"})

        # "first" should have been evicted once a 3rd entry was inserted
        # past the max_entries=2 cap.
        resolved = mw.before_call("x", {"secret": first})
        assert resolved is None

    def test_ttl_expiry_stops_resolving(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_SECRETS", "1")
        mw = SecretTokenizeMiddleware(ttl_seconds=0.05)

        token = mw.after_call("get_webhook", {}, {"secret": "short-lived"})["secret"]
        time.sleep(0.1)

        resolved = mw.before_call("x", {"secret": token})
        assert resolved is None

    def test_invalid_max_entries_raises(self):
        with pytest.raises(ValueError):
            SecretTokenizeMiddleware(max_entries=0)

    def test_invalid_ttl_raises(self):
        with pytest.raises(ValueError):
            SecretTokenizeMiddleware(ttl_seconds=0)

    def test_invalid_max_session_vaults_raises(self):
        with pytest.raises(ValueError):
            SecretTokenizeMiddleware(max_session_vaults=0)


class TestSessionScoping:
    def test_token_from_one_session_does_not_resolve_in_another(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_SECRETS", "1")
        mw = SecretTokenizeMiddleware()
        session_a = object()
        session_b = object()

        tokenized = mw.after_call(
            "get_webhook", {}, {"secret": "a-secret"}, context=_ctx(session_a)
        )
        token = tokenized["secret"]

        resolved_wrong_session = mw.before_call("x", {"secret": token}, context=_ctx(session_b))
        resolved_right_session = mw.before_call("x", {"secret": token}, context=_ctx(session_a))

        assert resolved_wrong_session is None
        assert resolved_right_session == {"secret": "a-secret"}

    def test_no_context_uses_shared_fallback_scope(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_SECRETS", "1")
        mw = SecretTokenizeMiddleware()

        tokenized = mw.after_call("get_webhook", {}, {"secret": "no-context-secret"})
        token = tokenized["secret"]

        resolved = mw.before_call("x", {"secret": token})
        assert resolved == {"secret": "no-context-secret"}

    def test_weak_session_vault_is_removed_after_session_collection(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_SECRETS", "1")
        mw = SecretTokenizeMiddleware()

        class Session:
            pass

        session = Session()
        mw.after_call("get_webhook", {}, {"secret": "temporary"}, context=_ctx(session))
        assert len(mw._vaults) == 1

        del session
        gc.collect()

        assert len(mw._vaults) == 0


class TestInstallMiddlewareContextPassing:
    """Guards the install.py extension: existing 2/3-arg middlewares must
    keep working unmodified, and a context-aware middleware must receive
    the real Context."""

    def test_existing_middleware_signature_unaffected(self):
        import asyncio

        from mcp.server.mcpserver import MCPServer

        srv = MCPServer("ctx-passing-test")

        @srv.tool()
        def echo(x: int) -> int:
            return x

        install_middleware(srv, [NullStripMiddleware()])
        result = asyncio.run(srv._tool_manager.call_tool("echo", {"x": 1, "y": None}))
        assert result == 1

    def test_context_aware_middleware_receives_context(self, monkeypatch):
        import asyncio

        from mcp.server.mcpserver import MCPServer

        monkeypatch.setenv("HPE_MCP_TOKENIZE_SECRETS", "1")
        srv = MCPServer("ctx-passing-secret-test")

        @srv.tool()
        def get_secret() -> dict[str, str]:
            return {"apiKey": "top-secret"}

        install_middleware(srv, [SecretTokenizeMiddleware()])

        result = asyncio.run(srv._tool_manager.call_tool("get_secret", {}))
        assert result["apiKey"] != "top-secret"
        assert _TOKEN_RE.fullmatch(result["apiKey"])
