"""Regression tests for GLP token-URL construction in ``hpe_networking_mcp.pipeline.config``.

The derived token URL interpolates the GLP workspace ID straight into a URL
path, and the configured override was accepted verbatim. Neither was
validated, so a workspace ID containing a slash or ``..``, or a token URL with
a non-TLS (or relative) scheme, silently produced a credential exchange
pointed somewhere other than intended.

The safe behaviours that existed before are preserved exactly:
- an explicitly configured token URL still wins over the derived one;
- the derived form is still ``{base}/authorization/v2/oauth2/{ws}/token``;
- an absent workspace ID (with no configured URL) still yields ``""`` rather
  than a guess.

No network calls.
"""

from __future__ import annotations

import pytest

from hpe_networking_mcp.pipeline.config import build_account_contexts

BASE_CREDS = """
central_account:
  base_url: https://central.example.com
  client_id: central-id
  client_secret: central-secret
  glp_workspace_id: {source_ws}
glp_account:
  base_url: https://target-central.example.com
  client_id: glp-id
  client_secret: glp-secret
  glp_workspace_id: {target_ws}
glp:
  token_url: "{token_url}"
  base_url: {base_url}
"""


@pytest.fixture(autouse=True)
def _no_env_overrides(monkeypatch):
    for var in ("GLP_TOKEN_URL", "GLP_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    for var in (
        "SOURCE_BASE_URL",
        "SOURCE_CLIENT_ID",
        "SOURCE_CLIENT_SECRET",
        "SOURCE_GLP_WORKSPACE",
        "TARGET_BASE_URL",
        "TARGET_CLIENT_ID",
        "TARGET_CLIENT_SECRET",
        "TARGET_GLP_WORKSPACE",
    ):
        monkeypatch.delenv(var, raising=False)


def _creds(
    tmp_path,
    source_ws="source-workspace",
    target_ws="target-workspace",
    token_url="",
    base_url="https://global.api.greenlake.hpe.com",
):
    path = tmp_path / "credentials.yaml"
    path.write_text(
        BASE_CREDS.format(
            source_ws=source_ws, target_ws=target_ws, token_url=token_url, base_url=base_url
        ),
        encoding="utf-8",
    )
    return str(path)


class TestPreservedSafeBehaviour:
    def test_derived_url_shape_is_unchanged(self, tmp_path):
        source, target = build_account_contexts(_creds(tmp_path))

        assert source.glp_token_url == (
            "https://global.api.greenlake.hpe.com/authorization/v2/oauth2/source-workspace/token"
        )
        assert target.glp_token_url.endswith("/authorization/v2/oauth2/target-workspace/token")

    def test_configured_url_still_wins(self, tmp_path):
        source, target = build_account_contexts(
            _creds(tmp_path, token_url="https://sso.example.com/token")
        )

        assert source.glp_token_url == "https://sso.example.com/token"
        assert target.glp_token_url == "https://sso.example.com/token"

    def test_env_override_still_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GLP_TOKEN_URL", "https://env-sso.example.com/token")

        source, _ = build_account_contexts(_creds(tmp_path))

        assert source.glp_token_url == "https://env-sso.example.com/token"

    def test_missing_workspace_id_yields_empty_string(self, tmp_path):
        source, _ = build_account_contexts(_creds(tmp_path, source_ws='""'))

        assert source.glp_token_url == ""

    def test_uuid_workspace_ids_are_accepted(self, tmp_path):
        ws = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
        source, _ = build_account_contexts(_creds(tmp_path, source_ws=ws))

        assert source.glp_token_url.endswith(f"/oauth2/{ws}/token")

    def test_trailing_slash_on_base_url_is_stripped(self, tmp_path):
        source, _ = build_account_contexts(
            _creds(tmp_path, base_url="https://glp.example.com/")
        )

        assert source.glp_token_url == (
            "https://glp.example.com/authorization/v2/oauth2/source-workspace/token"
        )


class TestValidation:
    @pytest.mark.parametrize(
        "workspace_id",
        ["../../evil", "ws/../other", "ws id", "ws?x=1", "ws#frag", "ws/token"],
    )
    def test_unsafe_workspace_id_is_rejected(self, tmp_path, workspace_id):
        with pytest.raises(ValueError) as exc:
            build_account_contexts(_creds(tmp_path, source_ws=f'"{workspace_id}"'))

        assert "workspace ID" in str(exc.value)
        assert "source glp_workspace_id" in str(exc.value)

    @pytest.mark.parametrize(
        "token_url",
        [
            "http://sso.example.com/token",
            "ftp://sso.example.com/token",
            "/authorization/v2/oauth2/ws/token",
            "sso.example.com/token",
        ],
    )
    def test_non_https_or_relative_token_url_is_rejected(self, tmp_path, token_url):
        with pytest.raises(ValueError) as exc:
            build_account_contexts(_creds(tmp_path, token_url=token_url))

        assert "GLP token URL" in str(exc.value)

    def test_scheme_only_token_url_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="must include a host"):
            build_account_contexts(_creds(tmp_path, token_url="https://"))

    def test_non_https_base_url_is_rejected(self, tmp_path):
        with pytest.raises(ValueError) as exc:
            build_account_contexts(_creds(tmp_path, base_url="http://glp.example.com"))

        assert "GLP base URL" in str(exc.value)

    def test_missing_base_url_with_a_workspace_id_is_a_clear_error(self, tmp_path):
        with pytest.raises(ValueError, match="Cannot derive a GLP token URL"):
            build_account_contexts(_creds(tmp_path, base_url='""'))

    def test_missing_base_url_without_workspace_ids_is_still_fine(self, tmp_path):
        source, target = build_account_contexts(
            _creds(tmp_path, source_ws='""', target_ws='""', base_url='""')
        )

        assert source.glp_token_url == ""
        assert target.glp_token_url == ""

    def test_configured_url_bypasses_workspace_validation(self, tmp_path):
        """An explicit URL is never built from the workspace ID, so a weird
        (but unused) workspace value must not break an otherwise valid config."""
        source, _ = build_account_contexts(
            _creds(tmp_path, source_ws='"ws/../x"', token_url="https://sso.example.com/token")
        )

        assert source.glp_token_url == "https://sso.example.com/token"

    def test_error_names_the_offending_account(self, tmp_path):
        with pytest.raises(ValueError, match="target glp_workspace_id"):
            build_account_contexts(_creds(tmp_path, target_ws='"bad/ws"'))
