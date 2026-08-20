from __future__ import annotations

from hpe_networking_mcp.mcp_servers import glp
from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import load_manifest


class _DummyCentral:
    def __init__(self, payload=None):
        self.calls: list[tuple[str, dict | None]] = []
        self.payload = payload if payload is not None else {"items": []}

    def get(self, path, params=None):
        self.calls.append((path, params))
        return self.payload


class _DummyGLP:
    def __init__(self, payload=None):
        self._client = _DummyCentral(payload)


def _patch_client(monkeypatch, payload=None):
    client = _DummyGLP(payload)
    monkeypatch.setattr(glp, "get_glp_client", lambda: client)
    return client._client


def test_curated_priority_workflows_match_committed_manifest():
    manifest = load_manifest("glp")
    operations = {(op["method"], op["path"]): op for op in manifest["operations"]}
    expected_reads = {
        "/authorization/v1beta1/role-assignments",
        "/authorization/v1beta1/role-assignments/{id}",
        "/authorization/v1beta1/scope-groups",
        "/authorization/v1beta1/scope-groups/{id}",
        "/authorization/v1beta1/scope-groups/{id}/scopes",
        "/events/v1beta1/webhooks",
        "/events/v1beta1/webhooks/{id}",
        "/events/v1beta1/subscriptions",
        "/events/v1beta1/webhooks/{id}/recent-deliveries",
        "/locations/v1/locations",
        "/locations/v1/locations/{id}",
        "/locations/v1/locations/address/revgeocode",
        "/locations/v1/locations/tags",
        "/locations/v1/locations/tags/{id}",
        "/tags/v1/tags",
        "/tags/v1/tag-resources",
        "/identity/v2beta1/scim/v2/Users",
        "/identity/v2beta1/scim/v2/Users/{userId}",
        "/identity/v2beta1/scim/v2/Groups",
        "/identity/v2beta1/scim/v2/Groups/{groupId}",
        "/identity/v2beta1/scim/v2/extensions/Groups/{groupId}/users",
        "/identity/v2beta1/scim/v2/extensions/Users/{userId}/groups",
    }

    for path in expected_reads:
        operation = operations[("GET", path)]
        assert operation["server_urls"] == ["https://global.api.greenlake.hpe.com"]


def test_rbac_and_event_workflows_forward_exact_paths_and_pagination(monkeypatch):
    central = _patch_client(monkeypatch)

    glp.list_glp_role_assignments(limit=999, offset=-2, filter="principal in ('user:1')")
    glp.get_glp_scope_group("scope group 1")
    glp.list_glp_event_subscriptions(filter="webhookId", limit=10, offset=3)
    glp.list_glp_webhook_deliveries("hook 1", limit=999, offset=4)

    assert central.calls == [
        (
            "/authorization/v1beta1/role-assignments",
            {"filter": "principal in ('user:1')", "limit": 200, "offset": 0},
        ),
        ("/authorization/v1beta1/scope-groups/scope%20group%201", {}),
        (
            "/events/v1beta1/subscriptions",
            {"filter": "webhookId", "limit": 10, "offset": 3},
        ),
        (
            "/events/v1beta1/webhooks/hook%201/recent-deliveries",
            {"limit": 200, "offset": 4},
        ),
    ]


def test_location_and_tag_workflows_forward_exact_parameters(monkeypatch):
    central = _patch_client(monkeypatch)

    glp.list_glp_locations(limit=50, offset=2, filter="name eq 'HQ'")
    glp.reverse_geocode_glp_location(41.2, -96.1, "en")
    glp.list_glp_tags(
        limit=25,
        offset=5,
        filter="key eq 'site'",
        sort="createdAt desc",
        select=["id", "key"],
    )
    glp.list_glp_tag_resources(filter_tags="site:HQ", select=["id"])

    assert central.calls == [
        (
            "/locations/v1/locations",
            {"filter": "name eq 'HQ'", "limit": 50, "offset": 2},
        ),
        (
            "/locations/v1/locations/address/revgeocode",
            {"latitude": 41.2, "longitude": -96.1, "language": "en"},
        ),
        (
            "/tags/v1/tags",
            {
                "filter": "key eq 'site'",
                "sort": "createdAt desc",
                "select": ["id", "key"],
                "limit": 25,
                "offset": 5,
            },
        ),
        (
            "/tags/v1/tag-resources",
            {"select": ["id"], "filter-tags": "site:HQ", "limit": 100, "offset": 0},
        ),
    ]


def test_scim_reads_use_one_based_pagination_and_bound_redacted_output(monkeypatch):
    payload = {
        "totalResults": 3,
        "Resources": [
            {"id": "u1", "clientSecret": "one"},
            {"id": "u2", "access_token": "two"},
            {"id": "u3"},
        ],
    }
    central = _patch_client(monkeypatch, payload)

    result = glp.list_glp_scim_users(
        filter='userName co "admin"',
        count=2,
        start_index=0,
        sort_by="displayName",
        sort_order="ascending",
    )

    assert central.calls == [
        (
            "/identity/v2beta1/scim/v2/Users",
            {
                "filter": 'userName co "admin"',
                "count": 2,
                "startIndex": 1,
                "sortBy": "displayName",
                "sortOrder": "ascending",
            },
        )
    ]
    assert len(result["data"]["Resources"]) == 2
    assert result["data"]["Resources"][0]["clientSecret"] == "******"
    assert result["data"]["Resources"][1]["access_token"] == "******"
    assert result["data"]["_pagination"]["limit"] == 2


def test_curated_glp_tool_count_is_108():
    # 76 pre-v0.7 curated tools + 32 GLP-depth additions (compute-ops-mgmt,
    # storage-fleet, block-storage, virtualization, backup-recovery,
    # data-services curated reads/writes, plus plan_glp_reconciliation) — see
    # src/hpe_networking_mcp/mcp_servers/glp.py module docstring and list_glp_api_families.
    curated = set(glp.mcp._tool_manager._tools) - set(glp.GENERATED_GLP_TOOLS)
    assert len(curated) == 108
