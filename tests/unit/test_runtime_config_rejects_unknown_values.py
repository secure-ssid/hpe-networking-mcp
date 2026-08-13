"""Regression tests: HPE_MCP_TOOLSETS / HPE_MCP_PRODUCTS /
HPE_MCP_RAG_BACKEND must reject an unrecognized, non-empty value with a
clear ``InvalidRuntimeConfigError`` (env var name + requested-vs-valid
context) instead of the previous behavior --
unknown toolset/product names were silently dropped from the resolved
backend set, and an unrecognized HPE_MCP_RAG_BACKEND silently fell back
to "lancedb". Documented valid combinations and empty/unset (default)
behavior must be unaffected.

No network calls.
"""

from __future__ import annotations

import importlib

import pytest

import hpe_networking_mcp.mcp_servers.rag as rag_module
import hpe_networking_mcp.mcp_servers.tool_router as router
from hpe_networking_mcp.mcp_servers.shared import (
    InvalidRuntimeConfigError,
    access_profile,
    reject_unknown_env_choices,
    resolve_rag_backend,
    validate_access_profile_environment,
)


class TestRejectUnknownEnvChoices:
    def test_empty_requested_is_a_no_op(self):
        # Must not raise -- unset/empty env vars keep default behavior.
        reject_unknown_env_choices("HPE_MCP_TOOLSETS", [], {"central", "glp"})

    def test_all_known_values_pass(self):
        reject_unknown_env_choices(
            "HPE_MCP_TOOLSETS", ["central", "glp"], {"central", "glp", "rag"}
        )

    def test_unknown_value_raises_with_requested_and_valid_context(self):
        with pytest.raises(InvalidRuntimeConfigError) as exc:
            reject_unknown_env_choices(
                "HPE_MCP_TOOLSETS", ["central", "bogus"], {"central", "glp"}
            )
        message = str(exc.value)
        assert "HPE_MCP_TOOLSETS" in message
        assert "bogus" in message
        # Requested-vs-resolved context: both what was asked for and what is
        # actually valid must be visible in the error.
        assert "requested=" in message
        assert "central" in message
        assert "glp" in message


class TestResolveRagBackend:
    def test_unset_defaults_to_lancedb(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_RAG_BACKEND", raising=False)
        assert resolve_rag_backend() == "lancedb"

    def test_redis_is_recognized(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_RAG_BACKEND", "redis")
        assert resolve_rag_backend() == "redis"

    def test_case_and_whitespace_insensitive(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_RAG_BACKEND", "  ReDiS  ")
        assert resolve_rag_backend() == "redis"

    def test_unknown_value_raises_instead_of_silently_falling_back(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_RAG_BACKEND", "sqlite")

        with pytest.raises(InvalidRuntimeConfigError) as exc:
            resolve_rag_backend()

        message = str(exc.value)
        assert "HPE_MCP_RAG_BACKEND" in message
        assert "sqlite" in message
        assert "lancedb" in message
        assert "redis" in message


class TestAccessProfileValidation:
    def test_unset_preserves_custom_compatibility_profile(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_ACCESS_PROFILE", raising=False)
        assert access_profile() == "custom"

    def test_invalid_profile_is_rejected(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "read-mostly")
        with pytest.raises(InvalidRuntimeConfigError, match="safe-read-only"):
            access_profile()

    def test_full_profile_conflict_is_rejected_before_startup(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "full-read-write")
        monkeypatch.setenv("HPE_MCP_READONLY", "1")
        with pytest.raises(InvalidRuntimeConfigError, match="conflicts"):
            validate_access_profile_environment()


class TestToolRouterBuildBackendsRejectsUnknown:
    def test_unknown_toolset_raises(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_PRODUCTS", raising=False)
        monkeypatch.setenv("HPE_MCP_TOOLSETS", "central,bogus-toolset")

        with pytest.raises(InvalidRuntimeConfigError) as exc:
            router._build_backends()

        message = str(exc.value)
        assert "HPE_MCP_TOOLSETS" in message
        assert "bogus-toolset" in message

    def test_unknown_product_raises(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_TOOLSETS", raising=False)
        monkeypatch.setenv("HPE_MCP_PRODUCTS", "clearpass,bogus-product")

        with pytest.raises(InvalidRuntimeConfigError) as exc:
            router._build_backends()

        message = str(exc.value)
        assert "HPE_MCP_PRODUCTS" in message
        assert "bogus-product" in message

    def test_documented_valid_toolsets_combination_still_works(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_PRODUCTS", raising=False)
        monkeypatch.setenv("HPE_MCP_TOOLSETS", "central,glp,rag")

        backends = router._build_backends()

        assert "glp-core" in backends
        assert "rag-core" in backends

    def test_all_keyword_still_works_for_toolsets_only(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_PRODUCTS", raising=False)
        monkeypatch.setenv("HPE_MCP_TOOLSETS", "all")

        backends = router._build_backends()

        assert "clearpass-core" in backends

    def test_all_is_not_a_valid_product_value(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_TOOLSETS", raising=False)
        monkeypatch.setenv("HPE_MCP_PRODUCTS", "all")

        with pytest.raises(InvalidRuntimeConfigError):
            router._build_backends()

    def test_empty_toolsets_and_products_keep_default_behavior(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_PRODUCTS", raising=False)
        monkeypatch.delenv("HPE_MCP_TOOLSETS", raising=False)

        backends = router._build_backends()

        assert "central-config" in backends
        assert "clearpass-core" not in backends


def test_module_import_raises_invalid_runtime_config_for_bad_rag_backend(monkeypatch):
    """Importing the router with an unrecognized HPE_MCP_RAG_BACKEND must
    fail at import/startup time, not silently coerce to lancedb."""
    monkeypatch.setenv("HPE_MCP_RAG_BACKEND", "not-a-real-backend")

    with pytest.raises(InvalidRuntimeConfigError):
        importlib.reload(router)

    # Restore the module to its normal (valid-env) state for any tests that
    # import it later in the same process.
    monkeypatch.delenv("HPE_MCP_RAG_BACKEND", raising=False)
    importlib.reload(router)
    importlib.reload(rag_module)
