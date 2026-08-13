"""Regression tests for doctor.py's runtime-config hardening:

- unrecognized HPE_MCP_TOOLSETS/_PRODUCTS/_RAG_BACKEND values now
  escalate to FAIL (the real router refuses to start on them) instead of
  the previous WARN/"ignored by the router" wording;
- wildcard allow-list detection is aligned with the SDK's actual
  '<host>:*' grammar (a subdomain glob or an unlisted '*' in a
  comma-separated list used to slip through unflagged);
- overly permissive credentials/.env/local MCP config file permissions are
  now flagged;
- a local, no-API startup/config check exercises the same
  credentials-file structure/URL validation the real server's /readyz
  performs.

All of these only read local files/env vars -- no network or API calls.
"""

from __future__ import annotations

import os

import pytest

from hpe_networking_mcp.cli import doctor


def _checks_by_name(checks):
    return {check.name: check for check in checks}


class TestUnknownToolsetsAndProductsEscalateToFail:
    def test_unknown_toolset_is_fail(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOOLSETS", "central,bogus-toolset")
        monkeypatch.delenv("HPE_MCP_PRODUCTS", raising=False)

        checks = _checks_by_name(doctor._runtime_checks())

        assert checks["Router toolsets"].status == "FAIL"
        assert "bogus-toolset" in checks["Router toolsets"].detail

    def test_unknown_product_is_fail(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_PRODUCTS", "clearpass,bogus-product")
        monkeypatch.delenv("HPE_MCP_TOOLSETS", raising=False)

        checks = _checks_by_name(doctor._runtime_checks())

        assert checks["Optional product names"].status == "FAIL"
        assert "bogus-product" in checks["Optional product names"].detail

    def test_valid_non_default_toolsets_combination_is_ok(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_TOOLSETS", "monitoring,rag")
        monkeypatch.delenv("HPE_MCP_PRODUCTS", raising=False)

        checks = _checks_by_name(doctor._runtime_checks())

        assert checks["Router toolsets"].status == "OK"

    def test_unknown_rag_backend_is_fail(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_RAG_BACKEND", "sqlite")
        monkeypatch.delenv("HPE_MCP_TOOLSETS", raising=False)
        monkeypatch.delenv("HPE_MCP_PRODUCTS", raising=False)

        checks = _checks_by_name(doctor._runtime_checks())

        assert checks["RAG backend"].status == "FAIL"
        assert "sqlite" in checks["RAG backend"].detail

    def test_recognized_rag_backend_is_ok(self, monkeypatch):
        monkeypatch.setenv("HPE_MCP_RAG_BACKEND", "redis")
        monkeypatch.delenv("HPE_MCP_TOOLSETS", raising=False)
        monkeypatch.delenv("HPE_MCP_PRODUCTS", raising=False)

        checks = _checks_by_name(doctor._runtime_checks())

        assert checks["RAG backend"].status == "OK"

    def test_unset_rag_backend_is_ok(self, monkeypatch):
        monkeypatch.delenv("HPE_MCP_RAG_BACKEND", raising=False)
        monkeypatch.delenv("HPE_MCP_TOOLSETS", raising=False)
        monkeypatch.delenv("HPE_MCP_PRODUCTS", raising=False)

        checks = _checks_by_name(doctor._runtime_checks())

        assert checks["RAG backend"].status == "OK"


class TestWildcardGrammarAlignment:
    def test_bare_star_still_warns(self, monkeypatch):
        monkeypatch.setenv("MCP_HOST", "0.0.0.0")
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "*")
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "*")

        checks = _checks_by_name(doctor._http_security_checks())

        assert checks["HTTP allow-list wildcard"].status == "FAIL"

    def test_subdomain_glob_is_flagged(self, monkeypatch):
        """Previously slipped through unflagged: '*.example.com' is not the
        SDK's supported '<host>:*' grammar and silently matches nothing."""
        monkeypatch.setenv("MCP_HOST", "0.0.0.0")
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "*.example.com")
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://mcp.example.com")

        checks = _checks_by_name(doctor._http_security_checks())

        assert checks["HTTP allow-list wildcard"].status == "FAIL"

    def test_wildcard_buried_in_a_multi_value_list_is_flagged(self, monkeypatch):
        """Previously missed: the old check only compared the whole raw
        string against '*', so a comma-separated list with '*' as one of
        several entries was never detected."""
        monkeypatch.setenv("MCP_HOST", "0.0.0.0")
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.example.com,*")
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://mcp.example.com")

        checks = _checks_by_name(doctor._http_security_checks())

        assert checks["HTTP allow-list wildcard"].status == "FAIL"

    def test_supported_port_wildcard_is_not_flagged(self, monkeypatch):
        monkeypatch.setenv("MCP_HOST", "0.0.0.0")
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.example.com:*")
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://mcp.example.com")

        checks = _checks_by_name(doctor._http_security_checks())

        assert "HTTP allow-list wildcard" not in checks


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
class TestFilePermissionChecks:
    def test_world_writable_file_fails(self, tmp_path):
        path = tmp_path / "credentials.yaml"
        path.write_text("central_account:\n  base_url: https://x.example.com\n")
        path.chmod(0o646)

        check = doctor._permission_check(path, "Credentials")

        assert check.status == "FAIL"
        assert "world-writable" in check.detail

    def test_group_readable_file_warns(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("FOO=bar\n")
        path.chmod(0o640)

        check = doctor._permission_check(path, ".env")

        assert check.status == "WARN"

    def test_owner_only_file_is_ok(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("FOO=bar\n")
        path.chmod(0o600)

        check = doctor._permission_check(path, ".env")

        assert check.status == "OK"

    def test_missing_file_returns_none(self, tmp_path):
        assert doctor._permission_check(tmp_path / "missing", "Credentials") is None

    def test_config_checks_include_permission_warnings(self, monkeypatch, tmp_path):
        creds = tmp_path / "credentials.yaml"
        creds.write_text("central_account:\n  base_url: https://x.example.com\n")
        creds.chmod(0o644)
        monkeypatch.setenv("CREDS_PATH", str(creds))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(doctor, "ROOT", tmp_path)

        checks = _checks_by_name(doctor._config_checks())

        assert checks["Credentials permissions"].status == "WARN"


class TestLocalStartupConfigCheck:
    def test_missing_credentials_file_warns(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREDS_PATH", str(tmp_path / "missing.yaml"))

        checks = _checks_by_name(doctor._local_startup_config_check())

        assert checks["Local startup config"].status == "WARN"

    def test_well_formed_credentials_pass(self, monkeypatch, tmp_path):
        creds = tmp_path / "credentials.yaml"
        creds.write_text(
            """
central_account:
  base_url: https://central.example.com
  client_id: id
  client_secret: super-secret-marker
  glp_workspace_id: ws
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("CREDS_PATH", str(creds))

        checks = _checks_by_name(doctor._local_startup_config_check())

        assert checks["Local startup config"].status == "OK"
        assert "super-secret-marker" not in checks["Local startup config"].detail

    def test_invalid_base_url_fails_without_leaking_secret(self, monkeypatch, tmp_path):
        creds = tmp_path / "credentials.yaml"
        creds.write_text(
            """
central_account:
  base_url: http://internal.example.com
  client_id: id
  client_secret: super-secret-marker
  glp_workspace_id: ws
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("CREDS_PATH", str(creds))

        checks = _checks_by_name(doctor._local_startup_config_check())

        assert checks["Local startup config"].status == "FAIL"
        assert "super-secret-marker" not in checks["Local startup config"].detail
        assert "base URL" in checks["Local startup config"].detail

    def test_malformed_yaml_fails_without_leaking_secret(self, monkeypatch, tmp_path):
        creds = tmp_path / "credentials.yaml"
        creds.write_text(
            """
central_account:
  base_url: https://central.example.com
  client_secret: super-secret-marker
  : broken yaml [
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("CREDS_PATH", str(creds))

        checks = _checks_by_name(doctor._local_startup_config_check())

        assert checks["Local startup config"].status == "FAIL"
        assert "super-secret-marker" not in checks["Local startup config"].detail


def test_main_includes_local_startup_config_check(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("CREDS_PATH", str(tmp_path / "missing.yaml"))
    monkeypatch.setattr(doctor.sys, "argv", ["doctor.py"])

    doctor.main()

    captured = capsys.readouterr()
    assert "Local startup config" in captured.out
