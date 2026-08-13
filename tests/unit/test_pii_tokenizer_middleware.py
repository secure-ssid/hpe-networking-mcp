"""Unit tests for PIITokenizeMiddleware.

Reference-capability audit note: this repo's existing tokenization
middleware (``SecretTokenizeMiddleware``) and ``shared.redact_sensitive``
only recognize credential-shaped keys (password/api_key/token/...). Neither
touches genuinely personal fields -- ``hpe_networking_mcp.mcp_servers.nac``'s
visitor tools and ``hpe_networking_mcp.mcp_servers.clearpass``'s guest
tools pass ``email``/``phone``/``company_name``/``visitor_name`` straight
through. ``PIITokenizeMiddleware`` closes that gap using the same
reversible-tokenization shape already established and tested for secrets
(see ``test_secret_tokenizer_middleware.py``), in an independent token
namespace/env var so the two capabilities compose without collision.

Test structure deliberately mirrors ``test_secret_tokenizer_middleware.py``
so the two suites are easy to diff against each other.
"""

from __future__ import annotations

import gc
import time
from types import SimpleNamespace
from typing import Any

import pytest

from hpe_networking_mcp.mcp_servers._middleware import (
    NullStripMiddleware,
    PIITokenizeMiddleware,
    install_middleware,
)
from hpe_networking_mcp.mcp_servers._middleware.pii_tokenizer import (
    _TOKEN_RE,
    pii_tokenize_enabled,
)


def _ctx(session_obj: Any) -> SimpleNamespace:
    return SimpleNamespace(session=session_obj)


class TestDisabledByDefault:
    def test_pii_tokenize_enabled_false_without_env(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_TOKENIZE_PII", raising=False)
        assert pii_tokenize_enabled() is False

    def test_after_call_is_noop_when_disabled(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_TOKENIZE_PII", raising=False)
        mw = PIITokenizeMiddleware()

        result = mw.after_call("list_visitors", {}, {"email": "guest@example.com"})

        assert result is None

    def test_before_call_is_noop_when_disabled(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_TOKENIZE_PII", raising=False)
        mw = PIITokenizeMiddleware()

        result = mw.before_call(
            "update_visitor", {"email": "hpe_mcp_pii_" + "a" * 32}
        )

        assert result is None


class TestEnabledRoundTrip:
    @pytest.mark.parametrize(
        "key",
        ["email", "phone", "company_name", "companyName", "visitor_name", "sponsor_email"],
    )
    def test_pii_value_is_tokenized_in_result(self, monkeypatch, key):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_PII", "1")
        mw = PIITokenizeMiddleware()

        result = mw.after_call("list_visitors", {}, {key: "sensitive-value"})

        assert result is not None
        token = result[key]
        assert token != "sensitive-value"
        assert _TOKEN_RE.fullmatch(token)

    def test_token_round_trips_back_to_plaintext_on_later_call(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_PII", "1")
        mw = PIITokenizeMiddleware()

        tokenized = mw.after_call("list_visitors", {}, {"email": "guest@example.com"})
        token = tokenized["email"]

        resolved = mw.before_call("update_visitor", {"email": token})

        assert resolved == {"email": "guest@example.com"}

    def test_non_pii_values_are_left_alone(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_PII", "1")
        mw = PIITokenizeMiddleware()

        result = mw.after_call(
            "list_visitors", {}, {"visitor_id": "V123", "count": 3, "status": "active"}
        )

        assert result is None

    def test_credential_keys_are_not_tokenized_by_pii_middleware(self, monkeypatch):
        # Cross-check: credential-shaped keys stay the concern of
        # SecretTokenizeMiddleware, not this one -- the two vaults/prefixes
        # must not overlap.
        monkeypatch.setenv("HPE_MCP_TOKENIZE_PII", "1")
        mw = PIITokenizeMiddleware()

        result = mw.after_call("get_webhook", {}, {"webhookSecret": "sekrit-value"})

        assert result is None

    def test_already_tokenized_value_is_not_retokenized(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_PII", "1")
        mw = PIITokenizeMiddleware()

        result = mw.after_call("list_visitors", {}, {"email": "******"})

        assert result is None

    def test_nested_pii_values_are_tokenized(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_PII", "1")
        mw = PIITokenizeMiddleware()

        payload = {"visitor": {"contact": {"email": "deep@example.com"}}}
        result = mw.after_call("get_visitor", {}, payload)

        assert result is not None
        token_value = result["visitor"]["contact"]["email"]
        assert _TOKEN_RE.fullmatch(token_value)

    def test_list_of_pii_dicts_are_tokenized(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_PII", "1")
        mw = PIITokenizeMiddleware()

        payload = {"visitors": [{"email": "one@example.com"}, {"email": "two@example.com"}]}
        result = mw.after_call("list_visitors", {}, payload)

        emails_out = [entry["email"] for entry in result["visitors"]]
        assert all(_TOKEN_RE.fullmatch(e) for e in emails_out)
        assert emails_out[0] != emails_out[1]

    def test_unknown_token_string_passed_through_unchanged(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_PII", "1")
        mw = PIITokenizeMiddleware()

        result = mw.before_call(
            "update_visitor", {"email": "hpe_mcp_pii_" + "0" * 32}
        )

        assert result is None

    def test_secret_token_string_does_not_resolve_in_pii_vault(self, monkeypatch):
        # A hpe_mcp_secret_* token (minted by the *other* middleware) must not
        # be mistaken for, or resolved against, this middleware's vault.
        monkeypatch.setenv("HPE_MCP_TOKENIZE_PII", "1")
        mw = PIITokenizeMiddleware()

        result = mw.before_call(
            "update_visitor", {"email": "hpe_mcp_secret_" + "a" * 32}
        )

        assert result is None


class TestBounding:
    def test_max_entries_evicts_oldest(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_PII", "1")
        mw = PIITokenizeMiddleware(max_entries=2)

        first = mw.after_call("list_visitors", {}, {"email": "first@example.com"})["email"]
        mw.after_call("list_visitors", {}, {"email": "second@example.com"})
        mw.after_call("list_visitors", {}, {"email": "third@example.com"})

        resolved = mw.before_call("x", {"email": first})
        assert resolved is None

    def test_ttl_expiry_stops_resolving(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_PII", "1")
        mw = PIITokenizeMiddleware(ttl_seconds=0.05)

        token = mw.after_call("list_visitors", {}, {"email": "short-lived@example.com"})[
            "email"
        ]
        time.sleep(0.1)

        resolved = mw.before_call("x", {"email": token})
        assert resolved is None

    def test_invalid_max_entries_raises(self):
        with pytest.raises(ValueError):
            PIITokenizeMiddleware(max_entries=0)

    def test_invalid_ttl_raises(self):
        with pytest.raises(ValueError):
            PIITokenizeMiddleware(ttl_seconds=0)

    def test_invalid_max_session_vaults_raises(self):
        with pytest.raises(ValueError):
            PIITokenizeMiddleware(max_session_vaults=0)


class TestSessionScoping:
    def test_token_from_one_session_does_not_resolve_in_another(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_PII", "1")
        mw = PIITokenizeMiddleware()
        session_a = object()
        session_b = object()

        tokenized = mw.after_call(
            "list_visitors", {}, {"email": "a@example.com"}, context=_ctx(session_a)
        )
        token = tokenized["email"]

        resolved_wrong_session = mw.before_call("x", {"email": token}, context=_ctx(session_b))
        resolved_right_session = mw.before_call("x", {"email": token}, context=_ctx(session_a))

        assert resolved_wrong_session is None
        assert resolved_right_session == {"email": "a@example.com"}

    def test_no_context_uses_shared_fallback_scope(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_PII", "1")
        mw = PIITokenizeMiddleware()

        tokenized = mw.after_call("list_visitors", {}, {"email": "no-context@example.com"})
        token = tokenized["email"]

        resolved = mw.before_call("x", {"email": token})
        assert resolved == {"email": "no-context@example.com"}

    def test_weak_session_vault_is_removed_after_session_collection(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOKENIZE_PII", "1")
        mw = PIITokenizeMiddleware()

        class Session:
            pass

        session = Session()
        mw.after_call("list_visitors", {}, {"email": "temp@example.com"}, context=_ctx(session))
        assert len(mw._vaults) == 1

        del session
        gc.collect()

        assert len(mw._vaults) == 0


class TestInstallMiddlewareIntegration:
    def test_context_aware_middleware_receives_context(self, monkeypatch):
        import asyncio

        from mcp.server.mcpserver import MCPServer

        monkeypatch.setenv("HPE_MCP_TOKENIZE_PII", "1")
        srv = MCPServer("ctx-passing-pii-test")

        @srv.tool()
        def get_visitor() -> dict[str, str]:
            return {"email": "visitor@example.com"}

        install_middleware(srv, [NullStripMiddleware(), PIITokenizeMiddleware()])

        result = asyncio.run(srv._tool_manager.call_tool("get_visitor", {}))
        assert result["email"] != "visitor@example.com"
        assert _TOKEN_RE.fullmatch(result["email"])
