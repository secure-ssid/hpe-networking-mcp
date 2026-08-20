from __future__ import annotations

from hpe_networking_mcp.mcp_servers import glp


def _configure_glp(monkeypatch, tmp_path, *, region: str | None = "eu-west") -> None:
    import hpe_networking_mcp.mcp_servers.shared as shared

    monkeypatch.setattr(shared, "_glp_client", None)
    monkeypatch.setenv("CREDS_PATH", str(tmp_path / "missing-credentials.yaml"))
    monkeypatch.setenv("GLP_BASE_URL", "https://global.api.greenlake.hpe.com")
    monkeypatch.setenv(
        "GLP_TOKEN_URL",
        "https://sso.common.cloud.hpe.com/as/token.oauth2",
    )
    monkeypatch.setenv("TARGET_CLIENT_ID", "client-id")
    monkeypatch.setenv("TARGET_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("TARGET_GLP_WORKSPACE", "workspace-123")
    monkeypatch.setenv("TOKEN_CACHE_DIR", str(tmp_path / "token-cache"))
    if region is None:
        monkeypatch.delenv("GLP_GENERATED_REGION", raising=False)
    else:
        monkeypatch.setenv("GLP_GENERATED_REGION", region)


def test_glp_preflight_is_local_and_reports_regional_readiness(monkeypatch, tmp_path):
    _configure_glp(monkeypatch, tmp_path)

    result = glp.glp_preflight()

    assert result["network_calls"] == 0
    assert result["status"] == "configured_unverified"
    assert result["credentials"]["workspace"] == {
        "id": "workspace-123",
        "scope_configured": True,
    }
    assert result["token"]["token_present"] is False
    assert result["token"]["network_probe"] == "not_run"
    assert result["region"]["status"] == "configured"
    assert result["region"]["families"]["compute-ops-mgmt"]["status"] == "region_required"
    assert result["region"]["families"]["storage-fleet"]["status"] == "ready"
    assert result["rbac"]["status"] == "not_probed"
    assert result["rate_limit"]["status"] == "not_observed"
    assert result["write_gate"]["platform"] == "glp"


def test_glp_preflight_reports_missing_configuration_without_network(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CREDS_PATH", str(tmp_path / "missing-credentials.yaml"))
    monkeypatch.delenv("TARGET_CLIENT_ID", raising=False)
    monkeypatch.delenv("TARGET_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("TARGET_GLP_WORKSPACE", raising=False)
    monkeypatch.delenv("GLP_TOKEN_URL", raising=False)
    monkeypatch.setenv("TOKEN_CACHE_DIR", str(tmp_path / "token-cache"))
    monkeypatch.delenv("GLP_GENERATED_REGION", raising=False)

    result = glp.glp_preflight()

    assert result["network_calls"] == 0
    assert result["status"] == "not_configured"
    assert "client_id" in result["errors"][0] or "workspace_id" in result["errors"][0]
    assert result["region"]["status"] == "not_configured"
