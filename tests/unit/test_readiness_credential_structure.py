"""Regression tests for readiness (``/readyz``) credential structure/
loadability checks.

Before this hardening pass, ``_readiness_detail`` only checked whether the
credentials file *exists* -- a file with a malformed structure, an invalid
URL, or an unsafe workspace ID reported ``ready`` even though every real
tool call would immediately fail with a config error. This exercises the
new ``build_account_contexts``-backed structure/loadability check with no
network calls, and confirms secret values are never echoed back in the
readiness detail even when the underlying error is a raw YAML parse
failure that could otherwise include a snippet of the file (e.g. a
``client_secret:`` line).
"""

from __future__ import annotations

from hpe_networking_mcp.mcp_servers.shared import _readiness_detail

SECRET_MARKER = "super-secret-value-should-never-leak"


class TestReadinessDetail:
    def test_missing_file_is_not_loadable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREDS_PATH", str(tmp_path / "missing.yaml"))

        detail = _readiness_detail()

        assert detail["creds_path_exists"] is False
        assert detail["credentials_loadable"] is False
        assert SECRET_MARKER not in str(detail)

    def test_well_formed_credentials_are_loadable(self, monkeypatch, tmp_path):
        creds = tmp_path / "credentials.yaml"
        creds.write_text(
            f"""
central_account:
  base_url: https://central.example.com
  client_id: id
  client_secret: {SECRET_MARKER}
  glp_workspace_id: ws
glp_account:
  base_url: https://central2.example.com
  client_id: id2
  client_secret: {SECRET_MARKER}-2
  glp_workspace_id: ws2
glp:
  base_url: https://global.api.greenlake.hpe.com
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("CREDS_PATH", str(creds))

        detail = _readiness_detail()

        assert detail["creds_path_exists"] is True
        assert detail["credentials_loadable"] is True
        assert "credentials_error" not in detail
        assert SECRET_MARKER not in str(detail)

    def test_invalid_base_url_is_not_loadable_without_exposing_detail(
        self, monkeypatch, tmp_path
    ):
        creds = tmp_path / "credentials.yaml"
        creds.write_text(
            f"""
central_account:
  base_url: http://internal-central.example.com
  client_id: id
  client_secret: {SECRET_MARKER}
  glp_workspace_id: ws
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("CREDS_PATH", str(creds))

        detail = _readiness_detail()

        assert detail["credentials_loadable"] is False
        assert detail["credentials_error"].startswith("credential configuration is invalid")
        assert SECRET_MARKER not in str(detail)

    def test_secret_bearing_malformed_url_is_never_echoed(self, monkeypatch, tmp_path):
        creds = tmp_path / "credentials.yaml"
        creds.write_text(
            f"""
central_account:
  base_url: ftp://user:{SECRET_MARKER}@central.example.com/api
  client_id: id
  client_secret: another-secret
  glp_workspace_id: ws
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("CREDS_PATH", str(creds))

        detail = _readiness_detail()

        assert detail["credentials_loadable"] is False
        assert SECRET_MARKER not in str(detail)

    def test_malformed_yaml_reports_generic_error_without_leaking_content(
        self, monkeypatch, tmp_path
    ):
        creds = tmp_path / "credentials.yaml"
        creds.write_text(
            f"""
central_account:
  base_url: https://central.example.com
  client_secret: {SECRET_MARKER}
  : broken yaml [
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("CREDS_PATH", str(creds))

        detail = _readiness_detail()

        assert detail["credentials_loadable"] is False
        assert "credentials_error" in detail
        assert SECRET_MARKER not in detail["credentials_error"]
        assert SECRET_MARKER not in str(detail)
        # Still bounded/informative -- names the exception type, not raw content.
        assert "failed to load" in detail["credentials_error"]

    def test_readiness_detail_never_includes_client_secret_key(self, monkeypatch, tmp_path):
        creds = tmp_path / "credentials.yaml"
        creds.write_text(
            f"""
central_account:
  base_url: https://central.example.com
  client_id: id
  client_secret: {SECRET_MARKER}
  glp_workspace_id: ws
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("CREDS_PATH", str(creds))

        detail = _readiness_detail()

        assert "client_secret" not in detail
        assert "client_id" not in detail
