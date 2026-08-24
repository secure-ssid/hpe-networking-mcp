from __future__ import annotations

import os

from hpe_networking_mcp.cli import doctor


def test_doctor_loads_dotenv_values_with_runtime_comment_semantics(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / ".env"
    path.write_text(
        "HPE_MCP_ACCESS_PROFILE=full-read-write # safety profile\n"
        "MIST_API_TOKEN=abc#def\n"
    )
    monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "__restore_after_test__")
    monkeypatch.setenv("MIST_API_TOKEN", "__restore_after_test__")
    monkeypatch.delenv("HPE_MCP_ACCESS_PROFILE", raising=False)
    monkeypatch.delenv("MIST_API_TOKEN", raising=False)

    doctor._load_local_env(path)

    assert os.environ["HPE_MCP_ACCESS_PROFILE"] == "full-read-write"
    assert os.environ["MIST_API_TOKEN"] == "abc#def"
    checks = {check.name: check for check in doctor._runtime_checks()}
    assert checks["Access profile"].status == "OK"


def test_doctor_warns_on_legacy_environment_prefix(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "CENTRALMCP_ROUTER_MODE=minimal\n"
        "CENTRALMCP_CENTRAL_WRITES=1\n"
    )

    check = doctor._legacy_env_check(dotenv)

    assert check.status == "WARN"
    assert "CENTRALMCP_ROUTER_MODE" in check.detail
    assert "CENTRALMCP_CENTRAL_WRITES" in check.detail
    assert "minimal" not in check.detail


def test_doctor_accepts_canonical_environment_prefix(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("HPE_MCP_ROUTER_MODE=minimal\n")

    check = doctor._legacy_env_check(dotenv)

    assert check.status == "OK"


def test_doctor_uses_exported_values_for_dotenv_interpolation(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / ".env"
    path.write_text(
        "MIST_ROOT=https://stale.example\n"
        "MIST_HOST=${MIST_ROOT}\n"
    )
    monkeypatch.setenv("MIST_ROOT", "https://api.mist.com")
    monkeypatch.setenv("MIST_HOST", "__restore_after_test__")
    monkeypatch.delenv("MIST_HOST")

    doctor._load_local_env(path)

    assert os.environ["MIST_HOST"] == "https://api.mist.com"


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


def test_doctor_fails_on_invalid_product_access(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-wrtie")
    monkeypatch.delenv("HPE_MCP_PRODUCTS", raising=False)
    monkeypatch.delenv("HPE_MCP_TOOLSETS", raising=False)

    checks = {check.name: check for check in doctor._runtime_checks()}

    assert checks["Optional product access"].status == "FAIL"
    assert "refuse to start" in checks["Optional product access"].detail
    assert checks["Access profile"].status == "FAIL"


def test_doctor_reports_unset_product_access_as_read_only(monkeypatch):
    monkeypatch.delenv("HPE_MCP_PRODUCT_ACCESS", raising=False)
    monkeypatch.delenv("HPE_MCP_PRODUCTS", raising=False)
    monkeypatch.delenv("HPE_MCP_TOOLSETS", raising=False)

    checks = {check.name: check for check in doctor._runtime_checks()}

    assert checks["Optional product access"].status == "OK"
    assert checks["Optional product access"].detail == (
        "unset; custom defaults optional-product writes to read-only"
    )


def test_doctor_accepts_direct_full_catalog_mode(monkeypatch):
    monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "full-read-write")
    monkeypatch.setenv("HPE_MCP_ROUTER_MODE", "direct")
    monkeypatch.setenv("HPE_MCP_TOOLSETS", "all")
    monkeypatch.setenv("HPE_MCP_PRODUCTS", "mist")

    checks = {check.name: check for check in doctor._runtime_checks()}

    assert checks["Router mode"].status == "OK"
    assert checks["Router toolsets"].status == "OK"
    assert checks["Optional products"].status == "OK"
    assert checks["Access profile"].status == "OK"


def test_doctor_rejects_contradictory_full_profile(monkeypatch):
    monkeypatch.setenv("HPE_MCP_ACCESS_PROFILE", "full-read-write")
    monkeypatch.setenv("HPE_MCP_READONLY", "1")

    checks = {check.name: check for check in doctor._runtime_checks()}

    assert checks["Access profile"].status == "FAIL"
    assert "HPE_MCP_READONLY" in checks["Access profile"].detail


def test_doctor_rejects_contradictory_stdio_profile():
    checks = {
        check.name: check
        for check in doctor._router_env_checks(
            {
                "mcpServers": {
                    "hpe-networking-mcp": {
                        "env": {
                            "HPE_MCP_ROUTER_MODE": "minimal",
                            "HPE_MCP_TOOLSETS": "central,glp,rag",
                            "HPE_MCP_ACCESS_PROFILE": "full-read-write",
                            "HPE_MCP_READONLY": "1",
                        }
                    }
                }
            }
        )
    }

    check = checks["Local stdio access profile"]
    assert check.status == "FAIL"
    assert "HPE_MCP_READONLY" in check.detail


def test_doctor_merges_inherited_access_settings_for_stdio(monkeypatch):
    monkeypatch.setenv("HPE_MCP_PRODUCT_ACCESS", "read-write")
    checks = {
        check.name: check
        for check in doctor._router_env_checks(
            {
                "mcpServers": {
                    "hpe-networking-mcp": {
                        "env": {
                            "HPE_MCP_ROUTER_MODE": "minimal",
                            "HPE_MCP_TOOLSETS": "central,glp,rag",
                            "HPE_MCP_ACCESS_PROFILE": "safe-read-only",
                        }
                    }
                }
            }
        )
    }

    check = checks["Local stdio access profile"]
    assert check.status == "FAIL"
    assert "HPE_MCP_PRODUCT_ACCESS" in check.detail


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


class TestIndexChecks:
    """Index remedies must describe the install in front of the operator.

    A fresh clone has no ``data/`` and no scraped corpus but does have the
    committed ``vendor/openapi`` corpus, so "run ingest_docs.py" is the wrong
    remedy for every index there: the spec index has an offline build and the
    prose index has nothing to ingest until sources are fetched. Each state
    gets its own command.
    """

    @staticmethod
    def _checks_by_name(root, monkeypatch):
        monkeypatch.setattr(doctor, "ROOT", root)
        return {check.name: check for check in doctor._index_checks()}

    def test_fresh_clone_points_each_index_at_its_own_producer(
        self, tmp_path, monkeypatch
    ):
        checks = self._checks_by_name(tmp_path, monkeypatch)

        specs = checks["Structured API index"]
        assert specs.status == "WARN"
        assert "git checkout -- vendor/openapi" in specs.detail

        sources = checks["Prose corpus sources"]
        assert sources.status == "WARN"
        assert "refresh_rag_sources.py --refresh-sources" in sources.detail

        docs = checks["Prose docs RAG index"]
        assert docs.status == "WARN"
        assert "no corpus to build from" in docs.detail
        assert "ingest_docs.py" not in docs.detail

        tools = checks["Router tool index"]
        assert tools.status == "WARN"
        assert "ingest_tools.py" in tools.detail

    def test_committed_vendor_corpus_makes_spec_index_an_offline_build(
        self, tmp_path, monkeypatch
    ):
        corpus = tmp_path / "vendor" / "openapi"
        corpus.mkdir(parents=True)
        (corpus / "MANIFEST.json").write_text("{}")

        checks = self._checks_by_name(tmp_path, monkeypatch)

        specs = checks["Structured API index"]
        assert specs.status == "WARN"
        assert "build_spec_index.py" in specs.detail
        assert "git checkout" not in specs.detail

    def test_populated_sources_make_prose_index_a_plain_build(
        self, tmp_path, monkeypatch
    ):
        source = tmp_path / "ingestion" / "sources" / "tech_docs"
        source.mkdir(parents=True)
        (source / "guide.md").write_text("# Guide")

        checks = self._checks_by_name(tmp_path, monkeypatch)

        sources = checks["Prose corpus sources"]
        assert sources.status == "OK"
        assert sources.detail.startswith("1 populated")

        docs = checks["Prose docs RAG index"]
        assert docs.status == "WARN"
        assert "ingest_docs.py" in docs.detail

    def test_empty_source_folder_is_not_a_populated_corpus(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / "ingestion" / "sources" / "tech_docs").mkdir(parents=True)

        monkeypatch.setattr(doctor, "ROOT", tmp_path)
        folders, populated = doctor._prose_sources_state()

        assert (folders, populated) == (1, 0)

    def test_all_indexes_present_reports_ok(self, tmp_path, monkeypatch):
        data = tmp_path / "data"
        data.mkdir()
        (data / "tools.lance").mkdir()
        (data / "docs.lance").mkdir()
        (data / "specs.sqlite").write_text("sqlite")
        source = tmp_path / "ingestion" / "sources" / "tech_docs"
        source.mkdir(parents=True)
        (source / "guide.md").write_text("# Guide")

        checks = self._checks_by_name(tmp_path, monkeypatch)

        assert all(check.status == "OK" for check in checks.values())
