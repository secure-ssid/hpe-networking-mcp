"""Unit tests for scripts.run_v07_validation_matrix.

Covers:
- Classification of every category with no live-test env vars set
  (the safe default): expected offline_fixture / blocked mix, no
  exception raised, exit code 0.
- Env-var gating precedence: read-without-credentials -> unavailable
  (even for a platform with a working offline fixture); both gates plus
  credentials -> disposable_write; read gate plus credentials -> live_read;
  permanent write-gap platform with both gates plus credentials ->
  coverage_gap.
- classify_category never performs a live network call regardless of the
  env vars set (patched to raise on any attempted HTTP call).
- The CLI writes a schema-valid validation_matrix_result artifact and
  returns exit code 1 only when a category is unavailable.
"""

from __future__ import annotations

import json

import pytest

from hpe_networking_mcp.pipeline import artifact_contracts as contracts
from hpe_networking_mcp.pipeline import live_test_config
from scripts import run_v07_validation_matrix as matrix_runner


@pytest.fixture(autouse=True)
def _clear_live_test_env(monkeypatch):
    """Ensure no live-test opt-in or credential env var leaks between tests."""
    for platform in live_test_config.LIVE_TEST_PLATFORMS:
        monkeypatch.delenv(live_test_config.live_test_read_env_var(platform), raising=False)
        monkeypatch.delenv(live_test_config.live_test_write_env_var(platform), raising=False)


@pytest.fixture(autouse=True)
def _forbid_network_calls(monkeypatch):
    """Fail loudly if anything in the matrix runner attempts a real network call."""

    def _blocked(*args, **kwargs):
        raise AssertionError("scripts.run_v07_validation_matrix must never make a network call")

    monkeypatch.setattr("urllib.request.urlopen", _blocked, raising=False)
    monkeypatch.setattr("socket.create_connection", _blocked, raising=False)


class TestDefaultClassification:
    def test_all_categories_present_exactly_once(self):
        payload = matrix_runner.build_validation_matrix_payload()
        categories = [entry["category"] for entry in payload["entries"]]
        assert sorted(categories) == sorted(matrix_runner.CATEGORIES)
        assert len(categories) == len(set(categories))

    def test_default_state_has_no_unavailable_categories(self):
        """With no env vars set at all, nothing should ever report
        unavailable -- that classification is reserved for a genuine
        misconfiguration (opt-in without credentials, or a failing
        offline self-check)."""
        payload = matrix_runner.build_validation_matrix_payload()
        classifications = {
            entry["category"]: entry["classification"] for entry in payload["entries"]
        }
        assert "unavailable" not in classifications.values(), classifications

    def test_payload_validates_as_contract(self):
        payload = matrix_runner.build_validation_matrix_payload()
        matrix = contracts.build_artifact(contracts.VALIDATION_MATRIX_RESULT, payload)
        assert matrix.kind == contracts.VALIDATION_MATRIX_RESULT


class TestEnvVarGatingPrecedence:
    def test_read_enabled_without_credentials_is_unavailable(self, monkeypatch):
        monkeypatch.setenv(live_test_config.live_test_read_env_var("central"), "1")
        entry = matrix_runner.classify_category("central")
        assert entry["classification"] == "unavailable"
        assert entry["read_enabled"] is True
        assert entry["credentials_configured"] is False

    def test_read_and_credentials_is_live_read(self, monkeypatch):
        monkeypatch.setenv(live_test_config.live_test_read_env_var("apstra"), "1")
        monkeypatch.setattr(
            live_test_config, "credentials_configured", lambda platform: True
        )
        entry = matrix_runner.classify_category("apstra")
        assert entry["classification"] == "live_read"
        assert entry["read_enabled"] is True
        assert entry["write_enabled"] is False

    def test_read_write_and_credentials_is_disposable_write(self, monkeypatch):
        monkeypatch.setenv(live_test_config.live_test_read_env_var("apstra"), "1")
        monkeypatch.setenv(live_test_config.live_test_write_env_var("apstra"), "1")
        monkeypatch.setattr(
            live_test_config, "credentials_configured", lambda platform: True
        )
        entry = matrix_runner.classify_category("apstra")
        assert entry["classification"] == "disposable_write"
        assert entry["read_enabled"] is True
        assert entry["write_enabled"] is True

    def test_permanent_write_gap_platform_is_coverage_gap(self, monkeypatch):
        monkeypatch.setenv(live_test_config.live_test_read_env_var("uxi"), "1")
        monkeypatch.setenv(live_test_config.live_test_write_env_var("uxi"), "1")
        monkeypatch.setattr(
            live_test_config, "credentials_configured", lambda platform: True
        )
        entry = matrix_runner.classify_category("uxi")
        assert entry["classification"] == "coverage_gap"

    def test_offline_fixture_used_when_no_opt_in_set(self):
        entry = matrix_runner.classify_category("central")
        assert entry["classification"] == "offline_fixture"
        assert entry["read_enabled"] is False
        assert entry["write_enabled"] is False

    def test_offline_check_exception_is_unavailable_not_a_crash(self, monkeypatch):
        def _raise():
            raise RuntimeError("simulated offline self-check failure")

        monkeypatch.setitem(matrix_runner._OFFLINE_CHECKS, "central", _raise)
        entry = matrix_runner.classify_category("central")
        assert entry["classification"] == "unavailable"
        assert "simulated offline self-check failure" in entry["detail"]

    def test_unknown_category_raises_value_error(self):
        with pytest.raises(ValueError, match="unknown validation matrix category"):
            matrix_runner.classify_category("not-a-real-category")


class TestCliMain:
    def test_main_writes_valid_artifact_and_exits_zero_by_default(self, tmp_path):
        output = tmp_path / "validation-matrix.json"
        exit_code = matrix_runner.main(["--output", str(output)])
        assert exit_code == 0
        payload = json.loads(output.read_text(encoding="utf-8"))
        contracts.build_artifact(contracts.VALIDATION_MATRIX_RESULT, payload)

    def test_main_exits_nonzero_when_a_category_is_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.setenv(live_test_config.live_test_read_env_var("central"), "1")
        output = tmp_path / "validation-matrix.json"
        exit_code = matrix_runner.main(["--output", str(output)])
        assert exit_code == 1
