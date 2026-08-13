"""Unit tests for the guarded curated GLP identity/RBAC lifecycle and
auto-subscription-setting writes added in the v0.6 GLP coverage increment:

- RBAC role-assignment create/update/delete
  (createRoleAssignmentV1beta1 / updateRoleAssignmentV1beta1 /
  deleteRoleAssignmentV1beta1)
- RBAC scope-group create/update/delete + scopes batch-add/bulk-delete
  (createScopeGroupV1beta1 / updateScopeGroupV1beta1 /
  deleteScopeGroupV1beta1 / addScopeGroupScopesV1beta1 /
  deleteScopeGroupScopesV1beta1)
- Identity user lifecycle: invite / update-preferences / disassociate
  (invite_user_to_account_identity_v1_users_post /
  update_user_preferences_identity_v1_users__id__put /
  disassociate_platform_user_identity_v1_users__id__delete)
- Auto-subscription settings list/get/update
  (getAutoSubscriptionsV1 / getAutoSubscriptionByIdV1 /
  updateAutoSubscriptionsV1)

All shapes are taken from the committed manifest at
src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/glp.json. No live calls. Writes stay
gated behind HPE_MCP_GLP_V2BETA1_WRITES, mirroring the existing
_write_disabled / _writes_enabled convention used by the other curated
GLP writes in this file (glp_assign_subscription, glp_add_device,
update_glp_workspace_contact, glp_add_subscriptions, ...).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hpe_networking_mcp.mcp_servers import glp
from hpe_networking_mcp.pipeline.clients.glp_client import _V2BETA1_WRITES_FLAG, GLPClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv(_V2BETA1_WRITES_FLAG, raising=False)
    yield
    monkeypatch.delenv(_V2BETA1_WRITES_FLAG, raising=False)


@pytest.fixture
def writes_on(monkeypatch):
    monkeypatch.setenv(_V2BETA1_WRITES_FLAG, "1")


def _make_glp_client():
    glp_client = GLPClient.__new__(GLPClient)
    glp_client.workspace_id = "test-workspace"
    glp_client._device_id_cache = {}
    inner = MagicMock()
    glp_client._client = inner
    return glp_client, inner


def _success_response(payload):
    resp = MagicMock()
    resp.is_success = True
    resp.json.return_value = payload
    return resp


def _error_response(status_code, text):
    resp = MagicMock()
    resp.is_success = False
    resp.status_code = status_code
    resp.text = text
    return resp


class _StubGLP:
    def __init__(self, **overrides):
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        raise AttributeError(name)


# ---------------------------------------------------------------------------
# GLPClient — RBAC role assignments
# ---------------------------------------------------------------------------


class TestRoleAssignmentClient:
    def test_create_disabled_by_default(self, clean_env):
        glp_client, _ = _make_glp_client()
        with pytest.raises(NotImplementedError) as exc:
            glp_client.create_role_assignment({"principal": "user:1"})
        assert _V2BETA1_WRITES_FLAG in str(exc.value)

    def test_create_sends_post_with_body(self, clean_env, writes_on):
        glp_client, inner = _make_glp_client()
        inner._request.return_value = _success_response({"id": "ra1"})

        body = {"principal": "user:1", "role": "role:admin", "scope": "scope:1"}
        result = glp_client.create_role_assignment(body)

        assert result == {"id": "ra1"}
        inner._request.assert_called_once_with(
            "POST", "/authorization/v1beta1/role-assignments", json=body
        )

    def test_update_sends_put_to_id_path(self, clean_env, writes_on):
        glp_client, inner = _make_glp_client()
        inner._request.return_value = _success_response({"id": "ra1"})

        body = {"id": "ra1", "principal": "user:1", "role": "role:admin", "scope": "scope:2"}
        glp_client.update_role_assignment("ra1", body)

        inner._request.assert_called_once_with(
            "PUT", "/authorization/v1beta1/role-assignments/ra1", json=body
        )

    def test_delete_sends_delete_to_id_path(self, clean_env, writes_on):
        glp_client, inner = _make_glp_client()
        resp = _success_response({})
        resp.text = ""
        inner._request.return_value = resp

        glp_client.delete_role_assignment("ra1")

        inner._request.assert_called_once_with(
            "DELETE", "/authorization/v1beta1/role-assignments/ra1"
        )

    def test_error_response_raises_with_status(self, clean_env, writes_on):
        glp_client, inner = _make_glp_client()
        inner._request.return_value = _error_response(409, "conflict")

        with pytest.raises(RuntimeError, match="HTTP 409"):
            glp_client.create_role_assignment({"principal": "user:1"})

    def test_empty_delete_body_falls_back(self, clean_env, writes_on):
        """204 No Content responses have no JSON body; verify the fallback."""
        glp_client, inner = _make_glp_client()
        resp = MagicMock()
        resp.is_success = True
        resp.text = ""
        resp.json.side_effect = ValueError("no content")
        inner._request.return_value = resp

        result = glp_client.delete_role_assignment("ra1")

        assert result == {"status": "completed", "rawResponse": ""}


# ---------------------------------------------------------------------------
# GLPClient — RBAC scope groups
# ---------------------------------------------------------------------------


class TestScopeGroupClient:
    def test_create_disabled_by_default(self, clean_env):
        glp_client, _ = _make_glp_client()
        with pytest.raises(NotImplementedError):
            glp_client.create_scope_group({"name": "sg1"})

    def test_create_sends_post_with_body(self, clean_env, writes_on):
        glp_client, inner = _make_glp_client()
        inner._request.return_value = _success_response({"id": "sg1"})

        body = {"name": "Site A scopes", "description": "d"}
        glp_client.create_scope_group(body)

        inner._request.assert_called_once_with(
            "POST", "/authorization/v1beta1/scope-groups", json=body
        )

    def test_update_sends_put_to_id_path(self, clean_env, writes_on):
        glp_client, inner = _make_glp_client()
        inner._request.return_value = _success_response({"id": "sg1"})

        body = {"id": "sg1", "name": "Renamed"}
        glp_client.update_scope_group("sg1", body)

        inner._request.assert_called_once_with(
            "PUT", "/authorization/v1beta1/scope-groups/sg1", json=body
        )

    def test_delete_sends_delete_to_id_path(self, clean_env, writes_on):
        glp_client, inner = _make_glp_client()
        resp = _success_response({})
        resp.text = ""
        inner._request.return_value = resp

        glp_client.delete_scope_group("sg1")

        inner._request.assert_called_once_with(
            "DELETE", "/authorization/v1beta1/scope-groups/sg1"
        )

    def test_add_scopes_wraps_items_and_posts_to_batch_path(self, clean_env, writes_on):
        glp_client, inner = _make_glp_client()
        inner._request.return_value = _success_response({"added": 2})

        items = [{"scope": "scope:1"}, {"scope": "scope:2"}]
        glp_client.add_scope_group_scopes("sg1", items)

        inner._request.assert_called_once_with(
            "POST",
            "/authorization/v1beta1/scope-groups/sg1/scopes/batch",
            json={"items": items},
        )

    def test_delete_scopes_sends_delete_with_body_to_bulk_path(self, clean_env, writes_on):
        """DELETE .../scopes/bulk carries a request body per the manifest, so
        it must go through _request directly (CentralClient.delete() takes
        no body)."""
        glp_client, inner = _make_glp_client()
        inner._request.return_value = _success_response({"removed": 1})

        items = [{"scope": "scope:1"}]
        glp_client.delete_scope_group_scopes("sg1", items)

        inner._request.assert_called_once_with(
            "DELETE",
            "/authorization/v1beta1/scope-groups/sg1/scopes/bulk",
            json={"items": items},
        )

    def test_error_response_raises_with_status(self, clean_env, writes_on):
        glp_client, inner = _make_glp_client()
        inner._request.return_value = _error_response(404, "not found")

        with pytest.raises(RuntimeError, match="HTTP 404"):
            glp_client.update_scope_group("sg-missing", {"id": "sg-missing"})


# ---------------------------------------------------------------------------
# GLPClient — identity user lifecycle
# ---------------------------------------------------------------------------


class TestUserLifecycleClient:
    def test_invite_disabled_by_default(self, clean_env):
        glp_client, _ = _make_glp_client()
        with pytest.raises(NotImplementedError) as exc:
            glp_client.invite_user("new.user@example.com")
        assert "new.user@example.com" in str(exc.value)

    def test_invite_sends_post_with_email_only(self, clean_env, writes_on):
        glp_client, inner = _make_glp_client()
        inner._request.return_value = _success_response({"id": "u1"})

        glp_client.invite_user("new.user@example.com")

        inner._request.assert_called_once_with(
            "POST", "/identity/v1/users", json={"email": "new.user@example.com"}
        )

    def test_invite_includes_send_welcome_email_when_set(self, clean_env, writes_on):
        glp_client, inner = _make_glp_client()
        inner._request.return_value = _success_response({"id": "u1"})

        glp_client.invite_user("new.user@example.com", send_welcome_email=False)

        inner._request.assert_called_once_with(
            "POST",
            "/identity/v1/users",
            json={"email": "new.user@example.com", "sendWelcomeEmail": False},
        )

    def test_update_preferences_sends_put_with_both_fields(self, clean_env, writes_on):
        glp_client, inner = _make_glp_client()
        inner._request.return_value = _success_response({"id": "u1"})

        glp_client.update_user_preferences("u1", idle_timeout=30, language="en")

        inner._request.assert_called_once_with(
            "PUT",
            "/identity/v1/users/u1",
            json={"idleTimeout": 30, "language": "en"},
        )

    def test_disassociate_sends_delete_to_id_path(self, clean_env, writes_on):
        glp_client, inner = _make_glp_client()
        resp = _success_response({})
        resp.text = ""
        inner._request.return_value = resp

        glp_client.disassociate_user("u1")

        inner._request.assert_called_once_with("DELETE", "/identity/v1/users/u1")

    def test_error_response_raises_with_status(self, clean_env, writes_on):
        glp_client, inner = _make_glp_client()
        inner._request.return_value = _error_response(422, "unprocessable")

        with pytest.raises(RuntimeError, match="HTTP 422"):
            glp_client.invite_user("bad@example.com")


# ---------------------------------------------------------------------------
# GLPClient — auto-subscription settings (read + gated write)
# ---------------------------------------------------------------------------


class TestAutoSubscriptionSettingsClient:
    def test_list_hits_correct_endpoint(self, clean_env):
        glp_client, inner = _make_glp_client()
        inner.get.return_value = {"items": [{"id": "as1"}]}

        items = glp_client.list_auto_subscription_settings()

        assert items == [{"id": "as1"}]
        inner.get.assert_called_once_with("/subscriptions/v1/auto-subscription-settings")

    def test_list_raises_on_error(self, clean_env):
        glp_client, inner = _make_glp_client()
        inner.get.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError, match="GLP list_auto_subscription_settings failed"):
            glp_client.list_auto_subscription_settings()

    def test_get_by_id_hits_correct_endpoint(self, clean_env):
        glp_client, inner = _make_glp_client()
        inner.get.return_value = {"id": "as1", "deviceType": "AP"}

        result = glp_client.get_auto_subscription_setting("as1")

        assert result == {"id": "as1", "deviceType": "AP"}
        inner.get.assert_called_once_with(
            "/subscriptions/v1/auto-subscription-settings/as1"
        )

    def test_get_by_id_returns_none_on_error(self, clean_env):
        glp_client, inner = _make_glp_client()
        inner.get.side_effect = RuntimeError("boom")

        assert glp_client.get_auto_subscription_setting("as1") is None

    def test_update_disabled_by_default(self, clean_env):
        glp_client, _ = _make_glp_client()
        with pytest.raises(NotImplementedError) as exc:
            glp_client.update_auto_subscription_settings("as1", {"tier": "FOUNDATION"})
        assert _V2BETA1_WRITES_FLAG in str(exc.value)

    def test_update_sends_patch_with_merge_patch_content_type(self, clean_env, writes_on):
        glp_client, inner = _make_glp_client()
        inner._request.return_value = _success_response({"id": "as1"})

        settings = {"deviceType": "AP", "tier": "FOUNDATION"}
        glp_client.update_auto_subscription_settings("as1", settings)

        inner._request.assert_called_once_with(
            "PATCH",
            "/subscriptions/v1/auto-subscription-settings/as1",
            json=settings,
            headers={"Content-Type": "application/merge-patch+json"},
        )

    def test_update_error_response_raises_with_status(self, clean_env, writes_on):
        glp_client, inner = _make_glp_client()
        inner._request.return_value = _error_response(400, "bad request")

        with pytest.raises(RuntimeError, match="HTTP 400"):
            glp_client.update_auto_subscription_settings("as1", {"tier": "FOUNDATION"})


# ---------------------------------------------------------------------------
# glp.py MCP tool wrappers — write gate (fail closed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name,kwargs",
    [
        ("create_glp_role_assignment", {"role_assignment": {"principal": "user:1"}}),
        (
            "update_glp_role_assignment",
            {"role_assignment_id": "ra1", "role_assignment": {"id": "ra1"}},
        ),
        ("delete_glp_role_assignment", {"role_assignment_id": "ra1"}),
        ("create_glp_scope_group", {"scope_group": {"name": "sg1"}}),
        (
            "update_glp_scope_group",
            {"scope_group_id": "sg1", "scope_group": {"id": "sg1"}},
        ),
        ("delete_glp_scope_group", {"scope_group_id": "sg1"}),
        (
            "add_glp_scope_group_scopes",
            {"scope_group_id": "sg1", "items": [{"scope": "scope:1"}]},
        ),
        (
            "delete_glp_scope_group_scopes",
            {"scope_group_id": "sg1", "items": [{"scope": "scope:1"}]},
        ),
        ("invite_glp_user", {"email": "new.user@example.com"}),
        (
            "update_glp_user_preferences",
            {"user_id": "u1", "idle_timeout": 30, "language": "en"},
        ),
        ("disassociate_glp_user", {"user_id": "u1"}),
        (
            "update_glp_auto_subscription_settings",
            {"setting_id": "as1", "settings": {"tier": "FOUNDATION"}},
        ),
    ],
)
def test_new_glp_writes_blocked_when_writes_disabled(monkeypatch, tool_name, kwargs):
    monkeypatch.delenv("HPE_MCP_GLP_V2BETA1_WRITES", raising=False)

    def fail_client():
        raise AssertionError(f"{tool_name} should not reach get_glp_client when gated off")

    monkeypatch.setattr(glp, "get_glp_client", fail_client)

    tool_fn = getattr(glp, tool_name)
    result = tool_fn(**kwargs)

    assert result["status"] == "FORBIDDEN"
    assert glp._V2BETA1_WRITES_FLAG in result["error"]


# ---------------------------------------------------------------------------
# glp.py MCP tool wrappers — enabled success/error handling
# ---------------------------------------------------------------------------


def test_create_glp_role_assignment_calls_client_when_enabled(monkeypatch):
    monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
    stub = _StubGLP(create_role_assignment=lambda role_assignment: {"id": "ra1"})
    monkeypatch.setattr(glp, "get_glp_client", lambda: stub)

    result = glp.create_glp_role_assignment({"principal": "user:1"})

    assert result == {"result": {"id": "ra1"}, "errors": []}


def test_create_glp_role_assignment_reports_client_error(monkeypatch):
    monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")

    def boom(role_assignment):
        raise RuntimeError("HTTP 409: conflict")

    stub = _StubGLP(create_role_assignment=boom)
    monkeypatch.setattr(glp, "get_glp_client", lambda: stub)

    result = glp.create_glp_role_assignment({"principal": "user:1"})

    assert result["result"] is None
    assert "HTTP 409" in result["errors"][0]


def test_delete_glp_role_assignment_calls_client_when_enabled(monkeypatch):
    monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
    stub = _StubGLP(delete_role_assignment=lambda role_assignment_id: {"status": "completed"})
    monkeypatch.setattr(glp, "get_glp_client", lambda: stub)

    result = glp.delete_glp_role_assignment("ra1")

    assert result == {"result": {"status": "completed"}, "errors": []}


def test_add_glp_scope_group_scopes_forwards_items(monkeypatch):
    monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
    seen = {}

    def fake_add(scope_group_id, items):
        seen["scope_group_id"] = scope_group_id
        seen["items"] = items
        return {"added": len(items)}

    stub = _StubGLP(add_scope_group_scopes=fake_add)
    monkeypatch.setattr(glp, "get_glp_client", lambda: stub)

    items = [{"scope": "scope:1"}, {"scope": "scope:2"}]
    result = glp.add_glp_scope_group_scopes("sg1", items)

    assert seen == {"scope_group_id": "sg1", "items": items}
    assert result == {"result": {"added": 2}, "errors": []}


def test_invite_glp_user_calls_client_when_enabled(monkeypatch):
    monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
    stub = _StubGLP(
        invite_user=lambda email, send_welcome_email=None: {
            "email": email,
            "sendWelcomeEmail": send_welcome_email,
        }
    )
    monkeypatch.setattr(glp, "get_glp_client", lambda: stub)

    result = glp.invite_glp_user("new.user@example.com", send_welcome_email=True)

    assert result == {
        "result": {"email": "new.user@example.com", "sendWelcomeEmail": True},
        "errors": [],
    }


def test_update_glp_user_preferences_calls_client_when_enabled(monkeypatch):
    monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
    seen = {}

    def fake_update(user_id, idle_timeout, language):
        seen["args"] = (user_id, idle_timeout, language)
        return {"id": user_id}

    stub = _StubGLP(update_user_preferences=fake_update)
    monkeypatch.setattr(glp, "get_glp_client", lambda: stub)

    result = glp.update_glp_user_preferences("u1", idle_timeout=15, language="fr")

    assert seen["args"] == ("u1", 15, "fr")
    assert result == {"result": {"id": "u1"}, "errors": []}


def test_disassociate_glp_user_reports_client_error(monkeypatch):
    monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")

    def boom(user_id):
        raise RuntimeError("HTTP 422: unprocessable")

    stub = _StubGLP(disassociate_user=boom)
    monkeypatch.setattr(glp, "get_glp_client", lambda: stub)

    result = glp.disassociate_glp_user("u1")

    assert result["result"] is None
    assert "HTTP 422" in result["errors"][0]


def test_update_glp_auto_subscription_settings_calls_client_when_enabled(monkeypatch):
    monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
    seen = {}

    def fake_update(setting_id, settings):
        seen["args"] = (setting_id, settings)
        return {"id": setting_id, **settings}

    stub = _StubGLP(update_auto_subscription_settings=fake_update)
    monkeypatch.setattr(glp, "get_glp_client", lambda: stub)

    settings = {"deviceType": "AP", "tier": "FOUNDATION"}
    result = glp.update_glp_auto_subscription_settings("as1", settings)

    assert seen["args"] == ("as1", settings)
    assert result == {"result": {"id": "as1", **settings}, "errors": []}


# ---------------------------------------------------------------------------
# glp.py MCP tool wrappers — auto-subscription-settings reads
# ---------------------------------------------------------------------------


def test_list_glp_auto_subscription_settings_wraps_client(monkeypatch):
    stub = _StubGLP(list_auto_subscription_settings=lambda: [{"id": "as1"}])
    monkeypatch.setattr(glp, "get_glp_client", lambda: stub)

    result = glp.list_glp_auto_subscription_settings()

    assert result == {"items": [{"id": "as1"}], "errors": []}


def test_list_glp_auto_subscription_settings_reports_errors(monkeypatch):
    def boom():
        raise RuntimeError("not available on this tenant")

    stub = _StubGLP(list_auto_subscription_settings=boom)
    monkeypatch.setattr(glp, "get_glp_client", lambda: stub)

    result = glp.list_glp_auto_subscription_settings()

    assert result["items"] == []
    assert "not available on this tenant" in result["errors"][0]


def test_get_glp_auto_subscription_setting_wraps_client(monkeypatch):
    stub = _StubGLP(get_auto_subscription_setting=lambda setting_id: {"id": setting_id})
    monkeypatch.setattr(glp, "get_glp_client", lambda: stub)

    result = glp.get_glp_auto_subscription_setting("as1")

    assert result == {"setting": {"id": "as1"}, "errors": []}


# ---------------------------------------------------------------------------
# glp_write_status / list_glp_api_families surface the new tools
# ---------------------------------------------------------------------------


def test_glp_write_status_lists_new_guarded_tools(monkeypatch):
    monkeypatch.delenv("HPE_MCP_GLP_V2BETA1_WRITES", raising=False)

    status = glp.glp_write_status()

    for tool_name in (
        "create_glp_role_assignment",
        "update_glp_role_assignment",
        "delete_glp_role_assignment",
        "create_glp_scope_group",
        "update_glp_scope_group",
        "delete_glp_scope_group",
        "add_glp_scope_group_scopes",
        "delete_glp_scope_group_scopes",
        "invite_glp_user",
        "update_glp_user_preferences",
        "disassociate_glp_user",
        "update_glp_auto_subscription_settings",
    ):
        assert tool_name in status["guarded_tools"]


def test_list_glp_api_families_lists_new_curated_tools():
    result = glp.list_glp_api_families()
    curated = result["curated_manifest_backed_tools"]

    for tool_name in (
        "create_glp_role_assignment",
        "update_glp_role_assignment",
        "delete_glp_role_assignment",
        "create_glp_scope_group",
        "update_glp_scope_group",
        "delete_glp_scope_group",
        "add_glp_scope_group_scopes",
        "delete_glp_scope_group_scopes",
        "invite_glp_user",
        "update_glp_user_preferences",
        "disassociate_glp_user",
        "list_glp_auto_subscription_settings",
        "get_glp_auto_subscription_setting",
        "update_glp_auto_subscription_settings",
    ):
        assert tool_name in curated


# ---------------------------------------------------------------------------
# Committed-manifest cross-check — every new tool maps to a real operation
# ---------------------------------------------------------------------------


def test_new_tool_endpoints_match_committed_manifest():
    from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import load_manifest

    manifest = load_manifest("glp")
    operations = {(op["method"], op["path"]) for op in manifest["operations"]}

    expected = {
        ("POST", "/authorization/v1beta1/role-assignments"),
        ("PUT", "/authorization/v1beta1/role-assignments/{id}"),
        ("DELETE", "/authorization/v1beta1/role-assignments/{id}"),
        ("POST", "/authorization/v1beta1/scope-groups"),
        ("PUT", "/authorization/v1beta1/scope-groups/{id}"),
        ("DELETE", "/authorization/v1beta1/scope-groups/{id}"),
        ("POST", "/authorization/v1beta1/scope-groups/{id}/scopes/batch"),
        ("DELETE", "/authorization/v1beta1/scope-groups/{id}/scopes/bulk"),
        ("POST", "/identity/v1/users"),
        ("PUT", "/identity/v1/users/{id}"),
        ("DELETE", "/identity/v1/users/{id}"),
        ("GET", "/subscriptions/v1/auto-subscription-settings"),
        ("GET", "/subscriptions/v1/auto-subscription-settings/{id}"),
        ("PATCH", "/subscriptions/v1/auto-subscription-settings/{id}"),
    }

    missing = expected - operations
    assert not missing, f"tool endpoints missing from committed manifest: {missing}"
