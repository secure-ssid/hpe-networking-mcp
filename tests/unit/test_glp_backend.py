from __future__ import annotations

from hpe_networking_mcp.mcp_servers import glp


def test_glp_write_status_disabled_by_default(monkeypatch):
    monkeypatch.delenv("HPE_MCP_GLP_V2BETA1_WRITES", raising=False)

    status = glp.glp_write_status()

    assert status["enabled"] is False
    assert status["flag"] == "HPE_MCP_GLP_V2BETA1_WRITES"
    assert "glp_archive_device" in status["guarded_tools"]
    assert "fail closed" in status["message"]


def test_glp_write_status_enabled(monkeypatch):
    monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")

    status = glp.glp_write_status()

    assert status["enabled"] is True
    assert "can execute" in status["message"]


def test_glp_get_rejects_absolute_url():
    result = glp.glp_get("https://evil.example/devices/v1/devices")

    assert "Invalid path" in result["error"]


def test_glp_get_rejects_dot_segments():
    result = glp.glp_get("/service-catalog/v1/../devices")

    assert "dot segments" in result["error"]


def test_glp_get_rejects_encoded_query_delimiter_bypass(monkeypatch):
    def fail_client():
        raise AssertionError("GLP client should not be called for encoded query delimiters")

    monkeypatch.setattr(glp, "get_glp_client", fail_client)

    result = glp.glp_get("/service-catalog/v1beta1/service-provisions%3Funredacted=true")

    assert "encoded query or fragment delimiters" in result["error"]


def test_glp_get_rejects_unsupported_prefix():
    result = glp.glp_get("/admin/v1/secrets")

    assert "path must begin" in result["error"]


def test_glp_get_calls_guarded_path(monkeypatch):
    class DummyCentral:
        def get(self, path, params=None):
            return {"path": path, "params": params}

    class DummyGLP:
        _client = DummyCentral()

    monkeypatch.setattr(glp, "get_glp_client", lambda: DummyGLP())

    result = glp.glp_get("/service-catalog/v1/services", {"limit": 5})

    assert result == {
        "data": {"path": "/service-catalog/v1/services", "params": {"limit": 5}},
        "endpoint_used": "/service-catalog/v1/services",
    }


def test_glp_get_accepts_official_audit_log_prefix(monkeypatch):
    class DummyCentral:
        def get(self, path, params=None):
            return {"path": path, "params": params}

    class DummyGLP:
        _client = DummyCentral()

    monkeypatch.setattr(glp, "get_glp_client", lambda: DummyGLP())

    result = glp.glp_get("/audit-log/v1/logs", {"limit": 5})

    assert result == {
        "data": {"path": "/audit-log/v1/logs", "params": {"limit": 5}},
        "endpoint_used": "/audit-log/v1/logs",
    }


def test_glp_get_bounds_list_payloads(monkeypatch):
    class DummyCentral:
        def get(self, path, params=None):
            return [{"id": 1}, {"id": 2}, {"id": 3}]

    class DummyGLP:
        _client = DummyCentral()

    monkeypatch.setattr(glp, "get_glp_client", lambda: DummyGLP())

    result = glp.glp_get("/service-catalog/v1/services", limit=2, offset=1)

    assert result == {
        "data": {
            "items": [{"id": 2}, {"id": 3}],
            "_pagination": {
                "offset": 1,
                "limit": 2,
                "total": 3,
                "truncated": False,
            },
        },
        "endpoint_used": "/service-catalog/v1/services",
    }


def test_glp_list_tools_clamp_limit_and_forward_offset(monkeypatch):
    calls = []

    class DummyGLP:
        # Wrappers now call the ``*_page`` methods so pagination metadata
        # (count/offset/total/next) can flow through to the tool response.
        def list_devices_page(self, limit=100, offset=0, filter=None):
            calls.append(("devices", limit, offset, filter))
            return {"items": []}

        def list_subscriptions_page(self, limit=100, offset=0):
            calls.append(("subscriptions", limit, offset))
            return {"items": []}

        def list_users_page(self, limit=100, offset=0):
            calls.append(("users", limit, offset))
            return {"items": []}

        def list_audit_logs_page(
            self, limit=100, offset=0, category=None, filter=None, select=None, sort=None
        ):
            calls.append(("audit", limit, offset, category, filter, select, sort))
            return {"items": []}

    monkeypatch.setattr(glp, "get_glp_client", lambda: DummyGLP())

    assert glp.list_glp_devices(limit=999, offset=-1, filter="deviceType eq 'AP'")["errors"] == []
    assert glp.list_glp_subscriptions(limit=999, offset=2)["errors"] == []
    assert glp.list_glp_users(limit=999, offset=3)["errors"] == []
    assert glp.list_glp_audit_logs(
        limit=999, offset=4, category="USER_MANAGEMENT", sort="createdAt desc"
    )["errors"] == []
    assert calls == [
        ("devices", 200, 0, "deviceType eq 'AP'"),
        ("subscriptions", 200, 2),
        ("users", 200, 3),
        ("audit", 200, 4, "USER_MANAGEMENT", None, None, "createdAt desc"),
    ]


def test_glp_official_id_wrappers_encode_and_call_paths(monkeypatch):
    calls = []

    class DummyCentral:
        def get(self, path, params=None):
            calls.append((path, params))
            return {"path": path, "params": params}

    class DummyGLP:
        _client = DummyCentral()

    monkeypatch.setattr(glp, "get_glp_client", lambda: DummyGLP())

    assert glp.get_glp_device_by_id("device 1")["data"]["path"] == "/devices/v1/devices/device%201"
    # getAuditLogDetails in the committed manifest is v2beta1 with a plural
    # /details segment; the old /audit-log/v1/.../detail path does not exist.
    assert glp.get_glp_audit_log_detail("audit-1")["data"]["path"] == (
        "/audit-log/v2beta1/logs/audit-1/details"
    )
    assert glp.get_glp_user("user 1")["data"]["path"] == "/identity/v1/users/user%201"
    assert glp.get_glp_workspace("workspace-1")["data"]["path"] == (
        "/workspaces/v1/workspaces/workspace-1"
    )
    assert glp.get_glp_reporting_status("report-1")["data"]["path"] == (
        "/reporting/v1/statuses/report-1"
    )
    assert calls == [
        ("/devices/v1/devices/device%201", {}),
        ("/audit-log/v2beta1/logs/audit-1/details", {}),
        ("/identity/v1/users/user%201", {}),
        ("/workspaces/v1/workspaces/workspace-1", {}),
        ("/reporting/v1/statuses/report-1", {}),
    ]


def test_glp_reporting_statuses_requires_filter_and_makes_no_call(monkeypatch):
    def _boom():
        raise AssertionError("must not reach the GLP client")

    monkeypatch.setattr(glp, "get_glp_client", _boom)

    for missing in (None, "", "   "):
        out = glp.list_glp_reporting_statuses(filter=missing, limit=10, offset=0)
        assert out["data"] is None
        assert out["endpoint_used"] == "/reporting/v1/statuses"
        assert "filter is a required query parameter" in out["errors"][0]


def test_glp_official_list_wrappers_clamp_and_forward_params(monkeypatch):
    calls = []

    class DummyCentral:
        def get(self, path, params=None):
            calls.append((path, params))
            return {"items": []}

    class DummyGLP:
        _client = DummyCentral()

    monkeypatch.setattr(glp, "get_glp_client", lambda: DummyGLP())

    assert glp.list_glp_reporting_statuses(
        filter="type eq 'REPORT'",
        sort="name asc",
        limit=999,
        offset=-2,
    )["errors"] == []
    assert glp.list_glp_service_offers(
        next_cursor="cursor-1",
        limit=999,
        filter="status eq 'ONBOARDED'",
    )["errors"] == []
    assert glp.list_glp_service_manager_provisions(limit=999, offset=3)["errors"] == []

    assert calls == [
        (
            "/reporting/v1/statuses",
            {"filter": "type eq 'REPORT'", "sort": "name asc", "limit": 200, "offset": 0},
        ),
        (
            "/service-catalog/v1beta1/service-offers",
            {"next": "cursor-1", "filter": "status eq 'ONBOARDED'", "limit": 200},
        ),
        (
            "/service-catalog/v1/service-manager-provisions",
            {"limit": 200, "offset": 3},
        ),
    ]


def test_glp_service_provisions_can_send_workspace_header(monkeypatch):
    calls = []

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"items": []}

    class DummyCentral:
        def _request(self, method, path, params=None, headers=None):
            calls.append((method, path, params, headers))
            return DummyResponse()

    class DummyGLP:
        _client = DummyCentral()

    monkeypatch.setattr(glp, "get_glp_client", lambda: DummyGLP())

    result = glp.list_glp_service_provisions(
        workspace_id="workspace-1",
        next_cursor="cursor-1",
        limit=999,
        filter="slug eq 'AC'",
        all_workspaces=False,
    )

    assert result["errors"] == []
    assert result["data"] == {"items": []}
    assert calls == [
        (
            "GET",
            "/service-catalog/v1beta1/service-provisions",
            {
                "next": "cursor-1",
                "filter": "slug eq 'AC'",
                "all": False,
                "limit": 200,
            },
            {"Hpe-workspace-id": "workspace-1"},
        )
    ]


def test_glp_service_provision_reads_strip_unredacted_and_redact(monkeypatch):
    calls = []

    class DummyCentral:
        def get(self, path, params=None):
            calls.append((path, params))
            return {
                "id": "provision-1",
                "clientSecret": "secret-value",
                "nested": {"access_token": "token-value"},
            }

    class DummyGLP:
        _client = DummyCentral()

    monkeypatch.setattr(glp, "get_glp_client", lambda: DummyGLP())

    result = glp.glp_get(
        "/service-catalog/v1beta1/service-provisions/provision-1",
        {"unredacted": True, "filter": "id eq 'provision-1'"},
    )

    assert calls == [
        (
            "/service-catalog/v1beta1/service-provisions/provision-1",
            {"filter": "id eq 'provision-1'"},
        )
    ]
    assert result["data"]["clientSecret"] == "******"
    assert result["data"]["nested"]["access_token"] == "******"
    assert "unredacted responses are disabled" in result["warnings"][0]


def test_glp_add_device_fails_closed_when_writes_disabled(monkeypatch):
    monkeypatch.delenv("HPE_MCP_GLP_V2BETA1_WRITES", raising=False)

    def fail_client():
        raise AssertionError("get_glp_client should not be called when writes are disabled")

    monkeypatch.setattr(glp, "get_glp_client", fail_client)

    result = glp.glp_add_device("SERIAL1", "aa:bb:cc:dd:ee:ff")

    assert result["status"] == "FORBIDDEN"
    assert "HPE_MCP_GLP_V2BETA1_WRITES=1" in result["error"]
    assert result["would_have_sent"]["serial_number"] == "SERIAL1"


def test_glp_assign_subscription_fails_closed_when_writes_disabled(monkeypatch):
    monkeypatch.delenv("HPE_MCP_GLP_V2BETA1_WRITES", raising=False)

    result = glp.glp_assign_subscription("SERIAL1", "SUBKEY")

    assert result["status"] == "FORBIDDEN"
    assert result["would_have_sent"] == {
        "serial_number": "SERIAL1",
        "subscription_key": "SUBKEY",
        "dry_run": False,
    }
