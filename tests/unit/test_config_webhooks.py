from hpe_networking_mcp.mcp_servers import config


def test_create_webhook_requires_oidc_fields_before_client_construction(monkeypatch):
    def fail_get_client():
        raise AssertionError("get_client should not be called for rejected input")

    monkeypatch.setattr(config, "get_client", fail_get_client)

    result = config.create_webhook(
        name="hook",
        endpoint_url="https://webhook.example.com/receiver",
        auth_mechanism="OIDC",
    )

    assert result == {
        "errors": ["OIDC requires oidc_client_id, oidc_client_secret, and oidc_well_known_url"]
    }
