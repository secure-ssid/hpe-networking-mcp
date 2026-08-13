"""Unit tests for hpe_networking_mcp.pipeline.live_test_config.

Covers:
- Default disabled/read-only behavior for every platform.
- Explicit per-platform read and disposable-write opt-ins.
- Credential presence never inferring read or write authorization.
- The status/validation API never revealing a credential value.
"""

from __future__ import annotations

import pytest

from hpe_networking_mcp.pipeline import live_test_config as lt


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Ensure no live-test opt-in or credential env var leaks between tests."""
    for platform in lt.LIVE_TEST_PLATFORMS:
        monkeypatch.delenv(lt.live_test_read_env_var(platform), raising=False)
        monkeypatch.delenv(lt.live_test_write_env_var(platform), raising=False)
        for name in lt.credential_env_vars(platform):
            monkeypatch.delenv(name, raising=False)


class TestUnknownPlatform:
    def test_unknown_platform_raises(self):
        with pytest.raises(ValueError, match="unknown live-test platform"):
            lt.live_test_read_enabled("not-a-real-platform")


class TestDefaults:
    @pytest.mark.parametrize("platform", lt.LIVE_TEST_PLATFORMS)
    def test_read_defaults_disabled(self, platform):
        assert lt.live_test_read_enabled(platform) is False

    @pytest.mark.parametrize("platform", lt.LIVE_TEST_PLATFORMS)
    def test_write_defaults_disabled(self, platform):
        assert lt.live_test_write_enabled(platform) is False

    @pytest.mark.parametrize("platform", lt.LIVE_TEST_PLATFORMS)
    def test_defaults_disabled_even_with_credentials_configured(self, platform, monkeypatch):
        for name in lt.credential_env_vars(platform):
            monkeypatch.setenv(name, "some-configured-value")
        assert lt.credentials_configured(platform) is True
        assert lt.live_test_read_enabled(platform) is False
        assert lt.live_test_write_enabled(platform) is False


class TestExplicitOptIns:
    def test_read_flag_enables_read_only(self, monkeypatch):
        monkeypatch.setenv(lt.live_test_read_env_var("mist"), "1")
        assert lt.live_test_read_enabled("mist") is True
        assert lt.live_test_write_enabled("mist") is False

    def test_write_flag_alone_is_insufficient(self, monkeypatch):
        monkeypatch.setenv(lt.live_test_write_env_var("mist"), "1")
        assert lt.live_test_read_enabled("mist") is False
        assert lt.live_test_write_enabled("mist") is False

    def test_read_and_write_flags_together_enable_write(self, monkeypatch):
        monkeypatch.setenv(lt.live_test_read_env_var("aos8"), "1")
        monkeypatch.setenv(lt.live_test_write_env_var("aos8"), "1")
        assert lt.live_test_read_enabled("aos8") is True
        assert lt.live_test_write_enabled("aos8") is True

    def test_flags_are_per_platform(self, monkeypatch):
        monkeypatch.setenv(lt.live_test_read_env_var("mist"), "1")
        assert lt.live_test_read_enabled("mist") is True
        assert lt.live_test_read_enabled("apstra") is False

    @pytest.mark.parametrize("falsy_value", ["0", "false", "no", "off", "", "bogus"])
    def test_non_truthy_values_stay_disabled(self, monkeypatch, falsy_value):
        monkeypatch.setenv(lt.live_test_read_env_var("clearpass"), falsy_value)
        assert lt.live_test_read_enabled("clearpass") is False


class TestCredentialPresenceNeverInferred:
    def test_credentials_configured_true_does_not_enable_read(self, monkeypatch):
        for name in lt.credential_env_vars("edgeconnect"):
            monkeypatch.setenv(name, "real-value")
        assert lt.credentials_configured("edgeconnect") is True
        assert lt.live_test_read_enabled("edgeconnect") is False

    def test_placeholder_credential_values_are_not_configured(self, monkeypatch):
        env_vars = lt.credential_env_vars("uxi")
        for name in env_vars:
            monkeypatch.setenv(name, "YOUR_CLIENT_ID_HERE")
        assert lt.credentials_configured("uxi") is False

    def test_lowercase_placeholder_values_are_not_configured(self, monkeypatch):
        for name in lt.credential_env_vars("mist"):
            monkeypatch.setenv(name, "your-token-here")
        assert lt.credentials_configured("mist") is False

    def test_missing_credential_is_not_configured(self, monkeypatch):
        env_vars = lt.credential_env_vars("axis")
        monkeypatch.setenv(env_vars[0], "set-value")
        # Remaining required env vars are left unset.
        assert lt.credentials_configured("axis") is False


class TestStatusApi:
    @pytest.mark.parametrize("platform", lt.LIVE_TEST_PLATFORMS)
    def test_status_never_includes_a_credential_value(self, platform, monkeypatch):
        secret_value = "super-secret-credential-value-12345"
        for name in lt.credential_env_vars(platform):
            monkeypatch.setenv(name, secret_value)
        status = lt.live_test_status(platform)
        assert secret_value not in repr(status)
        assert secret_value not in str(status)

    def test_status_reports_env_var_names_and_booleans_only(self):
        status = lt.live_test_status("central")
        assert status["platform"] == "central"
        assert status["read_env_var"] == "HPE_MCP_LIVE_TEST_CENTRAL_READ"
        assert status["write_env_var"] == "HPE_MCP_LIVE_TEST_CENTRAL_WRITE"
        assert status["read_enabled"] is False
        assert status["write_enabled"] is False
        assert isinstance(status["credential_env_vars"], list)
        assert status["credentials_configured"] is False
        assert "never grants read or write authorization" in status["note"]

    def test_status_reflects_explicit_opt_ins(self, monkeypatch):
        monkeypatch.setenv(lt.live_test_read_env_var("glp"), "1")
        monkeypatch.setenv(lt.live_test_write_env_var("glp"), "1")
        status = lt.live_test_status("glp")
        assert status["read_enabled"] is True
        assert status["write_enabled"] is True


class TestPlatformCoverage:
    def test_all_write_gate_platforms_have_credential_env_vars(self):
        for platform in lt.LIVE_TEST_PLATFORMS:
            env_vars = lt.credential_env_vars(platform)
            assert env_vars, f"{platform} must declare at least one credential env var"
