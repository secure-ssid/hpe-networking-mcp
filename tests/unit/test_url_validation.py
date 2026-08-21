from __future__ import annotations

import pytest

from hpe_networking_mcp.mcp_servers.shared import validate_product_base_url
from hpe_networking_mcp.pipeline.url_validation import (
    local_urls_allowed,
    validate_infra_url,
)


def test_public_https_url_is_normalized():
    assert (
        validate_infra_url(
            "https://api.example.com///",
            label="Product base URL",
        )
        == "https://api.example.com"
    )


@pytest.mark.parametrize(
    "value",
    [
        "http://api.example.com",
        "https://localhost",
        "https://127.0.0.1",
        "https://10.0.0.5",
        "https://[::1]",
    ],
)
def test_local_or_insecure_url_is_rejected_by_default(monkeypatch, value):
    monkeypatch.delenv("HPE_MCP_ALLOW_LOCAL_PRODUCT_URLS", raising=False)

    with pytest.raises(ValueError, match="HPE_MCP_ALLOW_LOCAL_PRODUCT_URLS"):
        validate_infra_url(value, label="Product base URL")


def test_local_lab_opt_in_allows_http_and_private_hosts(monkeypatch):
    monkeypatch.setenv("HPE_MCP_ALLOW_LOCAL_PRODUCT_URLS", "1")

    assert (
        validate_infra_url(
            "http://127.0.0.1:8443/",
            label="Product base URL",
        )
        == "http://127.0.0.1:8443"
    )
    assert local_urls_allowed() is True


def test_falsy_local_lab_opt_in_remains_disabled(monkeypatch):
    monkeypatch.setenv("HPE_MCP_ALLOW_LOCAL_PRODUCT_URLS", "false")

    assert local_urls_allowed() is False
    with pytest.raises(ValueError, match="not public"):
        validate_infra_url("https://192.168.1.10", label="Product base URL")


def test_embedded_credentials_are_rejected_even_for_public_hosts(monkeypatch):
    monkeypatch.delenv("HPE_MCP_ALLOW_LOCAL_PRODUCT_URLS", raising=False)

    with pytest.raises(ValueError, match="must not include credentials"):
        validate_infra_url(
            "https://user:secret@api.example.com",
            label="Product base URL",
        )


def test_optional_product_wrapper_uses_shared_validator(monkeypatch):
    monkeypatch.delenv("HPE_MCP_ALLOW_LOCAL_PRODUCT_URLS", raising=False)

    with pytest.raises(ValueError, match="ClearPass base URL"):
        validate_product_base_url(
            "https://192.168.50.10",
            product="ClearPass",
        )
