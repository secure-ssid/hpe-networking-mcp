from __future__ import annotations

import hpe_networking_mcp.mcp_servers.shared as shared
import hpe_networking_mcp.cli.run_pipeline as run_pipeline
from hpe_networking_mcp.pipeline.config import build_account_contexts
from hpe_networking_mcp.pipeline.models import AccountContext


def test_build_account_contexts_carries_glp_overrides(tmp_path, monkeypatch):
    creds = tmp_path / "credentials.yaml"
    creds.write_text(
        """
central_account:
  base_url: https://central.example.com
  client_id: central-id
  client_secret: central-secret
  glp_workspace_id: source-workspace
glp_account:
  base_url: https://target-central.example.com
  client_id: glp-id
  client_secret: glp-secret
  glp_workspace_id: target-workspace
glp:
  token_url: https://yaml-sso.example.com/token
  base_url: https://yaml-glp.example.com
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("GLP_TOKEN_URL", "https://env-sso.example.com/token")
    monkeypatch.setenv("GLP_BASE_URL", "https://env-glp.example.com")

    source, target = build_account_contexts(str(creds))

    assert source.glp_token_url == "https://env-sso.example.com/token"
    assert source.glp_base_url == "https://env-glp.example.com"
    assert target.glp_token_url == "https://env-sso.example.com/token"
    assert target.glp_base_url == "https://env-glp.example.com"


def test_build_account_contexts_derives_workspace_token_urls(tmp_path, monkeypatch):
    creds = tmp_path / "credentials.yaml"
    creds.write_text(
        """
central_account:
  base_url: https://central.example.com
  client_id: central-id
  client_secret: central-secret
  glp_workspace_id: source-workspace
glp_account:
  base_url: https://target-central.example.com
  client_id: glp-id
  client_secret: glp-secret
  glp_workspace_id: target-workspace
glp:
  token_url: ""
  base_url: https://global.api.greenlake.hpe.com
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("GLP_TOKEN_URL", raising=False)
    monkeypatch.delenv("GLP_BASE_URL", raising=False)

    source, target = build_account_contexts(str(creds))

    assert source.glp_token_url.endswith(
        "/authorization/v2/oauth2/source-workspace/token"
    )
    assert target.glp_token_url.endswith(
        "/authorization/v2/oauth2/target-workspace/token"
    )


def test_pipeline_client_builder_uses_separate_glp_overrides(monkeypatch):
    token_managers = []
    glp_clients = []

    class FakeTokenManager:
        def __init__(self, **kwargs):
            token_managers.append(kwargs)

        def get_access_token(self):
            return "token"

    class FakeCentralClient:
        def __init__(self, base_url, token_manager):
            self.base_url = base_url
            self.token_manager = token_manager

    class FakeGLPClient:
        def __init__(self, token_manager, workspace_id, base_url):
            self.token_manager = token_manager
            self.workspace_id = workspace_id
            self.base_url = base_url
            glp_clients.append(
                {
                    "token_manager": token_manager,
                    "workspace_id": workspace_id,
                    "base_url": base_url,
                }
            )

    monkeypatch.setattr(run_pipeline, "TokenManager", FakeTokenManager)
    monkeypatch.setattr(run_pipeline, "CentralClient", FakeCentralClient)
    monkeypatch.setattr(run_pipeline, "GLPClient", FakeGLPClient)

    ctx = AccountContext(
        label="target",
        base_url="https://central.example.com",
        client_id="client-id",
        client_secret="secret",
        glp_workspace_id="workspace-id",
        glp_token_url="https://custom-sso.example.com/token",
        glp_base_url="https://custom-glp.example.com",
    )

    run_pipeline._build_clients(ctx, "target")

    assert token_managers[0]["cache_key"] == "target"
    assert token_managers[0]["cache_context"] == "https://central.example.com|workspace-id"
    assert "token_url" not in token_managers[0]
    assert token_managers[1]["cache_key"] == "target-glp"
    assert token_managers[1]["token_url"] == "https://custom-sso.example.com/token"
    assert token_managers[1]["cache_context"] == "https://custom-glp.example.com|workspace-id"
    assert token_managers[1]["expiry_buffer"] == 60
    assert glp_clients == [
        {
            "token_manager": ctx.glp_client.token_manager,
            "workspace_id": "workspace-id",
            "base_url": "https://custom-glp.example.com",
        }
    ]


def test_mcp_shared_glp_client_uses_loaded_glp_overrides(monkeypatch):
    token_managers = []
    glp_clients = []

    class FakeTokenManager:
        def __init__(self, **kwargs):
            token_managers.append(kwargs)

        def get_access_token(self):
            return "token"

    class FakeGLPClient:
        def __init__(self, token_manager, workspace_id, base_url):
            glp_clients.append(
                {
                    "token_manager": token_manager,
                    "workspace_id": workspace_id,
                    "base_url": base_url,
                }
            )

    target_ctx = AccountContext(
        label="target",
        base_url="https://central.example.com",
        client_id="client-id",
        client_secret="secret",
        glp_workspace_id="workspace-id",
        glp_token_url="https://custom-sso.example.com/token",
        glp_base_url="https://custom-glp.example.com",
    )

    monkeypatch.setattr(shared, "_glp_client", None)
    monkeypatch.setattr(shared, "build_account_contexts", lambda creds_path: (object(), target_ctx))
    monkeypatch.setattr(shared, "TokenManager", FakeTokenManager)
    monkeypatch.setattr(shared, "GLPClient", FakeGLPClient)

    shared.get_glp_client()

    assert token_managers == [
        {
            "client_id": "client-id",
            "client_secret": "secret",
            "token_url": "https://custom-sso.example.com/token",
            "cache_context": "https://custom-glp.example.com|workspace-id",
            "cache_key": "glp",
            "expiry_buffer": 60,
        }
    ]
    assert glp_clients[0]["workspace_id"] == "workspace-id"
    assert glp_clients[0]["base_url"] == "https://custom-glp.example.com"
