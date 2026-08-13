"""Regression tests: MCP_ALLOWED_HOSTS/MCP_ALLOWED_ORIGINS wildcard
detection must align with the grammar the installed MCP SDK's
``TransportSecurityMiddleware`` actually implements.

The SDK's ``_validate_host``/``_validate_origin`` only special-case an
entry ending in a literal ``:*`` suffix (``str.startswith(base + ":")``);
anything else containing ``*`` (a bare ``"*"``, a subdomain glob like
``*.example.com``, etc.) is compared as an exact literal string that no
real Host/Origin header can equal -- it silently matches *nothing*. Before
this fix, only a bare ``"*"`` list entry was treated as "needs the wildcard
opt-in"; a glob like ``*.example.com`` sailed through unflagged on a public
bind and would have silently turned every real request into a 421/403 once
deployed.

No network calls.
"""

from __future__ import annotations

import pytest

from hpe_networking_mcp.mcp_servers.shared import (
    UnsafeHttpBindingError,
    _configure_http_transport,
    _has_unsupported_wildcard,
    _is_supported_sdk_wildcard,
)


class TestIsSupportedSdkWildcard:
    @pytest.mark.parametrize(
        "value",
        [
            "example.com:*",
            "https://example.com:*",
            "localhost:*",
            "  mcp.example.com:*  ",
        ],
    )
    def test_recognized_port_wildcard_shapes(self, value):
        assert _is_supported_sdk_wildcard(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "*",
            "*.example.com",
            "example.*.com",
            "*:*",
            ":*",
            "example.com",
            "https://example.com",
        ],
    )
    def test_unrecognized_shapes(self, value):
        assert _is_supported_sdk_wildcard(value) is False


class TestHasUnsupportedWildcard:
    def test_no_wildcard_entries_is_fine(self):
        assert _has_unsupported_wildcard(["example.com", "https://example.com"]) is False

    def test_supported_port_wildcard_is_fine(self):
        assert _has_unsupported_wildcard(["example.com:*"]) is False

    def test_bare_star_is_unsupported(self):
        assert _has_unsupported_wildcard(["*"]) is True

    def test_subdomain_glob_is_unsupported(self):
        assert _has_unsupported_wildcard(["*.example.com"]) is True

    def test_mixed_list_flags_the_bad_entry(self):
        assert _has_unsupported_wildcard(["example.com:*", "*.evil.example.com"]) is True


class TestConfigureHttpTransportWildcardAlignment:
    def test_public_bind_with_subdomain_glob_raises(self, monkeypatch):
        """A subdomain glob is not the SDK's supported grammar -- it would
        silently match nothing and must be rejected the same way a bare '*'
        already is."""
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "*.example.com")
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://mcp.example.com")
        monkeypatch.delenv("HPE_MCP_ALLOW_WILDCARD_HTTP_ALLOWLIST", raising=False)

        with pytest.raises(UnsafeHttpBindingError, match="wildcard"):
            _configure_http_transport("0.0.0.0", 8010)

    def test_public_bind_with_subdomain_glob_rejected_even_with_legacy_opt_in(
        self, monkeypatch
    ):
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "*.example.com")
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://mcp.example.com")
        monkeypatch.setenv("HPE_MCP_ALLOW_WILDCARD_HTTP_ALLOWLIST", "1")

        with pytest.raises(UnsafeHttpBindingError, match="wildcard"):
            _configure_http_transport("0.0.0.0", 8010)

    def test_public_bind_with_exact_port_wildcard_still_needs_no_opt_in(self, monkeypatch):
        """Preserves existing behavior: 'host:*' is the SDK's one real
        wildcard grammar and must not require the extra acknowledgement."""
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.example.com:*")
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://mcp.example.com")
        monkeypatch.delenv("HPE_MCP_ALLOW_WILDCARD_HTTP_ALLOWLIST", raising=False)

        security = _configure_http_transport("0.0.0.0", 8010)

        assert security.allowed_hosts == ["mcp.example.com:*"]

    def test_public_bind_with_bare_star_still_raises(self, monkeypatch):
        """Preserves existing behavior for the literal '*' case."""
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "*")
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "*")
        monkeypatch.delenv("HPE_MCP_ALLOW_WILDCARD_HTTP_ALLOWLIST", raising=False)

        with pytest.raises(UnsafeHttpBindingError, match="wildcard"):
            _configure_http_transport("0.0.0.0", 8010)
