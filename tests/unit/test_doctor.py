from __future__ import annotations

from hpe_networking_mcp.cli import doctor


def test_doctor_recognizes_uxi_optional_product(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCTS", "uxi")
    monkeypatch.delenv("HPE_MCP_TOOLSETS", raising=False)
    monkeypatch.delenv("UXI_CLIENT_ID", raising=False)
    monkeypatch.delenv("UXI_CLIENT_SECRET", raising=False)

    checks = {check.name: check for check in doctor._runtime_checks()}

    assert checks["Optional product names"].status == "OK"
    assert checks["uxi required env"].detail == (
        "missing or placeholder: UXI_CLIENT_ID, UXI_CLIENT_SECRET"
    )


def test_doctor_warns_on_uxi_placeholder_credentials(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCTS", "uxi")
    monkeypatch.delenv("HPE_MCP_TOOLSETS", raising=False)
    monkeypatch.setenv("UXI_CLIENT_ID", "YOUR_UXI_CLIENT_ID")
    monkeypatch.setenv("UXI_CLIENT_SECRET", "YOUR_UXI_CLIENT_SECRET")

    checks = {check.name: check for check in doctor._runtime_checks()}

    assert checks["uxi required env"].status == "WARN"
    assert checks["uxi required env"].detail == (
        "missing or placeholder: UXI_CLIENT_ID, UXI_CLIENT_SECRET"
    )


def test_doctor_warns_on_invalid_product_access(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-wrtie")
    monkeypatch.delenv("HPE_MCP_PRODUCTS", raising=False)
    monkeypatch.delenv("HPE_MCP_TOOLSETS", raising=False)

    checks = {check.name: check for check in doctor._runtime_checks()}

    assert checks["Optional product access"].status == "WARN"
    assert "optional writes fail closed" in checks["Optional product access"].detail


def test_doctor_reports_unset_product_access_as_read_only(monkeypatch):
    monkeypatch.delenv("HPE_MCP_PRODUCT_ACCESS", raising=False)
    monkeypatch.delenv("HPE_MCP_PRODUCTS", raising=False)
    monkeypatch.delenv("HPE_MCP_TOOLSETS", raising=False)

    checks = {check.name: check for check in doctor._runtime_checks()}

    assert checks["Optional product access"].status == "OK"
    assert checks["Optional product access"].detail == (
        "unset; optional product writes default to read-only"
    )


def test_doctor_accepts_direct_full_catalog_mode(monkeypatch):
    monkeypatch.setenv("HPE_MCP_ROUTER_MODE", "direct")
    monkeypatch.setenv("HPE_MCP_TOOLSETS", "all")
    monkeypatch.setenv("HPE_MCP_PRODUCTS", "mist")

    checks = {check.name: check for check in doctor._runtime_checks()}

    assert checks["Router mode"].status == "OK"
    assert checks["Router toolsets"].status == "OK"
    assert checks["Optional products"].status == "OK"


def test_doctor_source_manifest_matches_ingest_sources():
    checks = {check.name: check for check in doctor._source_manifest_checks()}

    assert checks["RAG source manifest"].status == "OK"
    assert "sources match ingestion SOURCE_META" in checks["RAG source manifest"].detail


def _http_checks(monkeypatch):
    return {check.name: check for check in doctor._http_security_checks()}


class TestHttpSecurityChecks:
    def test_loopback_host_is_ok_without_allowlist(self, monkeypatch):
        monkeypatch.setenv("MCP_HOST", "127.0.0.1")
        monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
        monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)

        checks = _http_checks(monkeypatch)

        assert checks["HTTP bind host"].status == "OK"
        assert "Per-platform write gates" in checks

    def test_public_host_without_allowlist_fails(self, monkeypatch):
        monkeypatch.setenv("MCP_HOST", "0.0.0.0")
        monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
        monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)

        checks = _http_checks(monkeypatch)

        assert checks["HTTP allow-list"].status == "FAIL"
        assert "MCP_ALLOWED_HOSTS" in checks["HTTP allow-list"].detail

    def test_public_host_with_allowlist_is_ok(self, monkeypatch):
        monkeypatch.setenv("MCP_HOST", "0.0.0.0")
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.example.com:*")
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://mcp.example.com")
        monkeypatch.delenv("MCP_HTTP_BEARER_TOKEN", raising=False)

        checks = _http_checks(monkeypatch)

        assert checks["HTTP allow-list"].status == "OK"
        assert checks["HTTP bearer token"].status == "WARN"

    def test_public_host_bare_wildcard_allowlist_fails_closed(self, monkeypatch):
        """A bare '*' is outside the SDK's '<host>:*' grammar, so it matches
        nothing and the server refuses to start -- doctor must FAIL, not WARN."""
        monkeypatch.setenv("MCP_HOST", "0.0.0.0")
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "*")
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "*")

        checks = _http_checks(monkeypatch)

        assert checks["HTTP allow-list wildcard"].status == "FAIL"
        assert "'<host>:*'" in checks["HTTP allow-list wildcard"].detail

    def test_public_host_supported_port_wildcard_is_not_flagged(self, monkeypatch):
        """The one supported wildcard shape ('<host>:*') raises no wildcard check."""
        monkeypatch.setenv("MCP_HOST", "0.0.0.0")
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.example.com:*")
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://mcp.example.com")

        checks = _http_checks(monkeypatch)

        assert "HTTP allow-list wildcard" not in checks

    def test_public_host_with_bearer_token_is_ok(self, monkeypatch):
        monkeypatch.setenv("MCP_HOST", "0.0.0.0")
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.example.com:*")
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://mcp.example.com")
        monkeypatch.setenv("MCP_HTTP_BEARER_TOKEN", "s3cr3t")

        checks = _http_checks(monkeypatch)

        assert checks["HTTP bearer token"].status == "OK"

    def test_platform_write_gate_overrides_are_listed(self, monkeypatch):
        monkeypatch.setenv("MCP_HOST", "127.0.0.1")
        assert "HPE_MCP_AXIS_WRITES" in doctor._PLATFORM_WRITE_ENV_VARS
        for name in doctor._PLATFORM_WRITE_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("HPE_MCP_MIST_WRITES", "1")

        checks = _http_checks(monkeypatch)

        assert checks["Per-platform write gates"].status == "OK"
        assert "HPE_MCP_MIST_WRITES" in checks["Per-platform write gates"].detail

    def test_no_platform_write_gate_overrides_reports_defaults(self, monkeypatch):
        monkeypatch.setenv("MCP_HOST", "127.0.0.1")
        for name in doctor._PLATFORM_WRITE_ENV_VARS:
            monkeypatch.delenv(name, raising=False)

        checks = _http_checks(monkeypatch)

        assert "none overridden" in checks["Per-platform write gates"].detail

    def test_main_includes_http_security_checks(self, monkeypatch, capsys):
        monkeypatch.setenv("MCP_HOST", "127.0.0.1")
        monkeypatch.setattr(doctor.sys, "argv", ["doctor.py"])

        doctor.main()

        captured = capsys.readouterr()
        assert "HTTP bind host" in captured.out
