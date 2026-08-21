"""Unit tests for the per-platform write-gate helpers in hpe_networking_mcp.mcp_servers.shared.

Covers:
- The custom compatibility profile preserves Central's historical default.
- GLP defaults disabled (preserves the historical fail-closed behavior).
- Safe-read-only blocks every platform; full-read-write opens every platform.
- The optional-product backends fall back to HPE_MCP_PRODUCT_ACCESS
  by default (unchanged legacy behavior) but can be overridden per-platform.
- Invalid and contradictory startup values are rejected clearly.
- Unknown platform names raise instead of silently allowing/denying.
"""

from __future__ import annotations

import importlib

import pytest

from hpe_networking_mcp.mcp_servers.shared import (
    ACCESS_PROFILES,
    PLATFORM_WRITE_GATE_NAMES,
    InvalidRuntimeConfigError,
    access_profile,
    build_write_execution_contract,
    enforce_platform_write,
    global_readonly_enabled,
    global_write_blocked,
    platform_write_blocked,
    platform_write_enable_instruction,
    platform_write_gate_state,
    platform_writes_allowed,
    validate_access_profile_environment,
)


class TestDefaults:
    def test_central_defaults_disabled(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_ACCESS_PROFILE", raising=False)
        monkeypatch.delenv("HPE_MCP_CENTRAL_WRITES", raising=False)
        assert access_profile() == "custom"
        # 0.9.1: Central is deny-by-default
        assert platform_writes_allowed("central") is False

    def test_glp_defaults_disabled(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_GLP_V2BETA1_WRITES", raising=False)
        assert platform_writes_allowed("glp") is False

    @pytest.mark.parametrize(
        "platform",
        ["aos8", "edgeconnect", "apstra", "mist", "clearpass", "uxi", "axis"],
    )
    def test_optional_products_default_to_shared_toggle(self, platform, monkeypatch):
        for gate in PLATFORM_WRITE_GATE_NAMES:
            monkeypatch.delenv(f"HPE_MCP_{gate.upper()}_WRITES", raising=False)
        monkeypatch.delenv("HPE_MCP_GLP_V2BETA1_WRITES", raising=False)

        monkeypatch.delenv("HPE_MCP_PRODUCT_ACCESS", raising=False)
        assert platform_writes_allowed(platform) is False

        monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
        assert platform_writes_allowed(platform) is True

    def test_safe_read_only_blocks_every_platform(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "safe-read-only")
        monkeypatch.delenv("HPE_MCP_READONLY", raising=False)
        monkeypatch.delenv("HPE_MCP_PRODUCT_ACCESS", raising=False)
        for platform in PLATFORM_WRITE_GATE_NAMES:
            gate = platform_write_gate_state(platform)
            assert gate["enabled"] is False
            assert gate["source"] == "HPE_MCP_ACCESS_PROFILE"
        assert global_readonly_enabled() is True

    def test_safe_profile_blocked_guidance_coordinates_legacy_gates(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "safe-read-only")

        global_error = global_write_blocked("write_tool")["error"]
        platform_hint = platform_write_enable_instruction(
            "mist",
            "HPE_MCP_MIST_WRITES",
        )

        assert "scripts/setup_wizard.py --access-profile full-read-write" in global_error
        assert "HPE_MCP_READONLY=0" in global_error
        assert "HPE_MCP_MIST_WRITES=1" in platform_hint
        assert "HPE_MCP_READONLY=0" in platform_hint

    def test_full_read_write_opens_every_platform(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "full-read-write")
        monkeypatch.delenv("HPE_MCP_READONLY", raising=False)
        monkeypatch.delenv("HPE_MCP_PRODUCT_ACCESS", raising=False)
        for platform in PLATFORM_WRITE_GATE_NAMES:
            monkeypatch.delenv(
                platform_write_gate_state(platform)["env_var"], raising=False
            )
        assert validate_access_profile_environment() == "full-read-write"
        assert all(platform_writes_allowed(name) for name in PLATFORM_WRITE_GATE_NAMES)
        assert global_readonly_enabled() is False


class TestOverrides:
    def test_platform_override_wins_over_shared_toggle(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
        monkeypatch.setenv("HPE_MCP_MIST_WRITES", "0")

        assert platform_writes_allowed("mist") is False
        assert platform_write_gate_state("mist") == {
            "env_var": "HPE_MCP_MIST_WRITES",
            "state": "disabled",
            "enabled": False,
            "source": "platform_override",
        }

    def test_platform_override_can_enable_a_single_optional_product(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_PRODUCT_ACCESS", raising=False)
        monkeypatch.setenv("HPE_MCP_MIST_WRITES", "1")
        monkeypatch.delenv("HPE_MCP_CLEARPASS_WRITES", raising=False)

        assert platform_writes_allowed("mist") is True
        assert platform_writes_allowed("clearpass") is False

    def test_central_can_be_opted_out(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "0")
        assert platform_writes_allowed("central") is False

    def test_glp_can_be_opted_in(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
        assert platform_writes_allowed("glp") is True

    @pytest.mark.parametrize("value", ["banana", "maybe", "2"])
    def test_ambiguous_override_value_fails_closed(self, monkeypatch, value):
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", value)
        assert platform_writes_allowed("central") is False
        assert platform_write_gate_state("central")["state"] == "invalid"

    def test_invalid_shared_fallback_fails_closed(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_AXIS_WRITES", raising=False)
        monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "surprise")

        state = platform_write_gate_state("axis")

        assert state["enabled"] is False
        assert state["state"] == "invalid"
        assert state["source"] == "HPE_MCP_PRODUCT_ACCESS"


class TestValidation:
    def test_access_profile_values_are_stable(self):
        assert ACCESS_PROFILES == {
            "safe-read-only",
            "custom",
            "full-read-write",
        }

    def test_unknown_access_profile_raises(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "full-ish")
        with pytest.raises(InvalidRuntimeConfigError, match="HPE_MCP_ACCESS_PROFILE"):
            access_profile()

    def test_safe_profile_rejects_write_enable_overrides(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "safe-read-only")
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
        with pytest.raises(InvalidRuntimeConfigError, match="conflicts"):
            validate_access_profile_environment()

    def test_full_profile_rejects_read_only_overrides(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "full-read-write")
        monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "0")
        with pytest.raises(InvalidRuntimeConfigError, match="conflicts"):
            validate_access_profile_environment()

    def test_custom_profile_accepts_mixed_platform_overrides(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "custom")
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "0")
        monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
        assert validate_access_profile_environment() == "custom"

    def test_invalid_boolean_gate_is_rejected_at_startup(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "custom")
        monkeypatch.setenv("HPE_MCP_MIST_WRITES", "sometimes")
        with pytest.raises(InvalidRuntimeConfigError, match="HPE_MCP_MIST_WRITES"):
            validate_access_profile_environment()

    def test_unknown_platform_raises(self):
        with pytest.raises(ValueError, match="unknown platform"):
            platform_writes_allowed("does-not-exist")

    def test_all_required_platforms_are_registered(self):
        required = {
            "central",
            "glp",
            "aos8",
            "edgeconnect",
            "apstra",
            "mist",
            "clearpass",
            "uxi",
            "axis",
        }
        assert required <= set(PLATFORM_WRITE_GATE_NAMES)


class TestEnforcementHelper:
    def test_enforce_returns_none_when_allowed(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_MIST_WRITES", "1")
        assert enforce_platform_write("mist", "mist_set_site") is None

    def test_enforce_returns_blocked_dict_when_denied(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_MIST_WRITES", "0")

        blocked = enforce_platform_write("mist", "mist_set_site")

        assert blocked is not None
        assert blocked["status"] == "blocked"
        assert blocked["tool"] == "mist_set_site"
        assert blocked["platform"] == "mist"
        assert "HPE_MCP_MIST_WRITES=1" in blocked["error"]
        assert blocked["execution_contract"]["gate"]["state"] == "disabled"
        assert blocked["execution_contract"]["next_action"].startswith(
            "Set HPE_MCP_MIST_WRITES=1"
        )

    def test_platform_write_blocked_mentions_correct_env_var_per_platform(self):
        blocked = platform_write_blocked("glp", "glp_write")
        assert "HPE_MCP_GLP_V2BETA1_WRITES" in blocked["error"]

    def test_contract_shape_is_compact_and_stable(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_AXIS_WRITES", "1")

        contract = build_write_execution_contract(
            "axis",
            "destructive",
            supports_dry_run=True,
            dry_run_state="preview",
            supports_confirm=True,
            requires_confirmation=True,
            idempotent=False,
            next_action="Review the preview.",
        )

        assert set(contract) == {
            "platform",
            "capability",
            "gate",
            "dry_run",
            "confirm",
            "idempotent",
            "next_action",
        }
        assert contract["gate"] == {
            "env_var": "HPE_MCP_AXIS_WRITES",
            "state": "enabled",
            "source": "platform_override",
        }


class TestOptionalBackendIntegration:
    @pytest.mark.parametrize(
        ("module_name", "platform", "env_var"),
        [
            ("hpe_networking_mcp.mcp_servers.aos8", "aos8", "HPE_MCP_AOS8_WRITES"),
            (
                "hpe_networking_mcp.mcp_servers.edgeconnect",
                "edgeconnect",
                "HPE_MCP_EDGECONNECT_WRITES",
            ),
            ("hpe_networking_mcp.mcp_servers.apstra", "apstra", "HPE_MCP_APSTRA_WRITES"),
            ("hpe_networking_mcp.mcp_servers.mist", "mist", "HPE_MCP_MIST_WRITES"),
            ("hpe_networking_mcp.mcp_servers.clearpass", "clearpass", "HPE_MCP_CLEARPASS_WRITES"),
            ("hpe_networking_mcp.mcp_servers.uxi", "uxi", "HPE_MCP_UXI_WRITES"),
            ("hpe_networking_mcp.mcp_servers.axis", "axis", "HPE_MCP_AXIS_WRITES"),
        ],
    )
    def test_backend_honors_platform_override(
        self, module_name, platform, env_var, monkeypatch
    ):
        monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
        monkeypatch.setenv(env_var, "0")
        module = importlib.import_module(module_name)

        assert module.optional_product_writes_allowed() is False
        blocked = module.optional_product_write_blocked(f"{platform}_write")
        assert blocked["platform"] == platform
        assert env_var in blocked["error"]
