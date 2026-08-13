"""Unit tests for the v0.7 Central-depth curated workflows added to
src/hpe_networking_mcp/mcp_servers/config.py:

- VSF template lifecycle (build_vsf_template / delete_vsf_template):
  dry_run/confirm/write-gate/validated-result/read-back.
- Bulk site / site-collection delete: bounds, dry_run/confirm/write-gate.
- Multi-target firmware-compliance campaign: bounds, partial failure,
  dry_run/confirm/write-gate.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from hpe_networking_mcp.mcp_servers import config
from hpe_networking_mcp.mcp_servers.shared import WriteResultError


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None):
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload

    @property
    def text(self) -> str:
        return "" if self._payload is None else str(self._payload)


class FakeClient:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, Any, Any]] = []

    def _request(self, method: str, endpoint: str, *, json: Any = None, params: Any = None):
        self.calls.append((method, endpoint, json, params))
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def _central_writes_enabled(monkeypatch):
    """Default: Central write gate enabled (matches historical default)."""
    monkeypatch.delenv("HPE_MCP_CENTRAL_WRITES", raising=False)


# ---------------------------------------------------------------------------
# build_vsf_template
# ---------------------------------------------------------------------------


def test_build_vsf_template_dry_run_sends_nothing(monkeypatch):
    monkeypatch.setattr(
        config, "get_client", lambda: (_ for _ in ()).throw(AssertionError("must not call"))
    )

    result = config.build_vsf_template(
        "stack-1", 2, scope_id="scope-1", device_function="ACCESS_SWITCH", dry_run=True
    )

    assert result["dry_run"] is True
    assert result["payload"]["number-of-members"] == 2
    assert result["params"] == {
        "object-type": "LOCAL", "scope-id": "scope-1", "device-function": "ACCESS_SWITCH",
    }


def test_build_vsf_template_rejects_out_of_range_members():
    with pytest.raises(ValueError, match="number_of_members"):
        config.build_vsf_template("stack-1", 0, scope_id="scope-1", dry_run=True)
    with pytest.raises(ValueError, match="number_of_members"):
        config.build_vsf_template("stack-1", 11, scope_id="scope-1", dry_run=True)


def test_build_vsf_template_requires_scope_id():
    with pytest.raises(ValueError, match="scope_id is required"):
        config.build_vsf_template("stack-1", 2, scope_id="", dry_run=True)


def test_build_vsf_template_requires_confirm_when_not_dry_run(monkeypatch):
    monkeypatch.setattr(
        config, "get_client", lambda: (_ for _ in ()).throw(AssertionError("must not call"))
    )
    result = config.build_vsf_template(
        "stack-1", 2, scope_id="scope-1", dry_run=False, confirm=False
    )
    assert result["error"] == "confirm=True is required when dry_run=False."
    assert result["dry_run"] is True


def test_build_vsf_template_blocked_when_central_writes_disabled(monkeypatch):
    monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "0")
    monkeypatch.setattr(
        config, "get_client", lambda: (_ for _ in ()).throw(AssertionError("must not call"))
    )
    result = config.build_vsf_template(
        "stack-1", 2, scope_id="scope-1", dry_run=False, confirm=True
    )
    assert result["status"] == "blocked"
    assert result["platform"] == "central"


def test_build_vsf_template_executes_and_reads_back(monkeypatch):
    client = FakeClient([
        FakeResponse(201, {"name": "stack-1"}),
        FakeResponse(200, {"name": "stack-1", "number-of-members": 2}),
    ])
    monkeypatch.setattr(config, "get_client", lambda: client)

    result = config.build_vsf_template(
        "stack-1", 2, scope_id="scope-1", device_function="ACCESS_SWITCH",
        dry_run=False, confirm=True,
    )

    assert result["action"] == "created"
    assert result["response"] == {"name": "stack-1"}
    assert result["read_back"] == {"name": "stack-1", "number-of-members": 2}
    assert [c[0] for c in client.calls] == ["POST", "GET"]


def test_build_vsf_template_raises_on_failed_write(monkeypatch):
    client = FakeClient([FakeResponse(500, {"errors": ["boom"]})])
    monkeypatch.setattr(config, "get_client", lambda: client)

    with pytest.raises(WriteResultError):
        config.build_vsf_template(
            "stack-1", 2, scope_id="scope-1", dry_run=False, confirm=True
        )


# ---------------------------------------------------------------------------
# delete_vsf_template
# ---------------------------------------------------------------------------


def test_delete_vsf_template_dry_run(monkeypatch):
    monkeypatch.setattr(
        config, "get_client", lambda: (_ for _ in ()).throw(AssertionError("must not call"))
    )
    result = config.delete_vsf_template("stack-1", scope_id="scope-1", dry_run=True)
    assert result["dry_run"] is True
    assert result["params"]["object-type"] == "LOCAL"


def test_delete_vsf_template_requires_scope_id():
    with pytest.raises(ValueError, match="scope_id is required"):
        config.delete_vsf_template("stack-1", scope_id="", dry_run=True)


def test_delete_vsf_template_confirms_removal_via_read_back(monkeypatch):
    client = FakeClient([
        FakeResponse(200, {"status": "deleted"}),
        FakeResponse(404, None),
    ])
    monkeypatch.setattr(config, "get_client", lambda: client)

    result = config.delete_vsf_template(
        "stack-1", scope_id="scope-1", dry_run=False, confirm=True
    )

    assert result["response"] == {"status": "deleted"}
    assert result["read_back"] == {"deleted_confirmed": True}


def test_delete_vsf_template_blocked_when_writes_disabled(monkeypatch):
    monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "0")
    result = config.delete_vsf_template(
        "stack-1", scope_id="scope-1", dry_run=False, confirm=True
    )
    assert result["status"] == "blocked"


# ---------------------------------------------------------------------------
# delete_sites_bulk / delete_site_collections_bulk
# ---------------------------------------------------------------------------


def test_delete_sites_bulk_dry_run_builds_items_payload():
    result = config.delete_sites_bulk(["scope-1", "scope-2"], dry_run=True)
    assert result["dry_run"] is True
    assert result["payload"] == {"items": [{"id": "scope-1"}, {"id": "scope-2"}]}
    assert result["endpoint"] == "/network-config/v1/sites/bulk"


def test_delete_sites_bulk_rejects_empty_list():
    with pytest.raises(ValueError, match="non-empty"):
        config.delete_sites_bulk([], dry_run=True)


def test_delete_sites_bulk_rejects_over_bound():
    ids = [f"scope-{i}" for i in range(config._MAX_BULK_DELETE_ITEMS + 1)]
    with pytest.raises(ValueError, match="cannot exceed"):
        config.delete_sites_bulk(ids, dry_run=True)


def test_delete_sites_bulk_requires_confirm(monkeypatch):
    monkeypatch.setattr(
        config, "get_client", lambda: (_ for _ in ()).throw(AssertionError("must not call"))
    )
    result = config.delete_sites_bulk(["scope-1"], dry_run=False, confirm=False)
    assert "confirm=True is required" in result["error"]


def test_delete_sites_bulk_blocked_when_writes_disabled(monkeypatch):
    monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "0")
    result = config.delete_sites_bulk(["scope-1"], dry_run=False, confirm=True)
    assert result["status"] == "blocked"


def test_delete_sites_bulk_executes(monkeypatch):
    client = FakeClient([FakeResponse(202, {"status": "success", "items": ["scope-1"]})])
    monkeypatch.setattr(config, "get_client", lambda: client)
    result = config.delete_sites_bulk(["scope-1"], dry_run=False, confirm=True)
    assert result == {"status": "success", "items": ["scope-1"]}
    assert client.calls[0][:2] == ("DELETE", "/network-config/v1/sites/bulk")


def test_delete_site_collections_bulk_dry_run():
    result = config.delete_site_collections_bulk(["col-1"], dry_run=True)
    assert result["endpoint"] == "/network-config/v1/site-collections/bulk"
    assert result["payload"] == {"items": [{"id": "col-1"}]}


# ---------------------------------------------------------------------------
# run_firmware_compliance_campaign
# ---------------------------------------------------------------------------


def test_campaign_dry_run_previews_every_target(monkeypatch):
    # set_firmware_compliance (reused per-target for the dry_run preview)
    # constructs a client handle even in dry_run before returning its
    # preview — it never issues a request, so a bare MagicMock is enough.
    monkeypatch.setattr(config, "get_client", lambda: MagicMock())
    targets = [
        {"scope_id": "s1", "device_function": "ACCESS_SWITCH"},
        {"scope_id": "s2", "device_function": "CAMPUS_AP"},
    ]
    result = config.run_firmware_compliance_campaign(targets, "10.16.1030", dry_run=True)
    assert result["dry_run"] is True
    assert len(result["results"]) == 2
    assert all(r["dry_run"] is True for r in result["results"])


def test_campaign_rejects_empty_targets():
    with pytest.raises(ValueError, match="non-empty"):
        config.run_firmware_compliance_campaign([], "10.16.1030", dry_run=True)


def test_campaign_rejects_too_many_targets():
    targets = [
        {"scope_id": f"s{i}", "device_function": "ACCESS_SWITCH"}
        for i in range(config._MAX_CAMPAIGN_TARGETS + 1)
    ]
    with pytest.raises(ValueError, match="cannot exceed"):
        config.run_firmware_compliance_campaign(targets, "10.16.1030", dry_run=True)


def test_campaign_rejects_malformed_target():
    with pytest.raises(ValueError, match="targets\\[0\\]"):
        config.run_firmware_compliance_campaign(
            [{"scope_id": "s1"}], "10.16.1030", dry_run=True
        )


def test_campaign_blocked_when_writes_disabled(monkeypatch):
    monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "0")
    targets = [{"scope_id": "s1", "device_function": "ACCESS_SWITCH"}]
    result = config.run_firmware_compliance_campaign(
        targets, "10.16.1030", dry_run=False, confirm=True
    )
    assert result["status"] == "blocked"


def test_campaign_requires_confirm(monkeypatch):
    monkeypatch.setattr(
        config, "get_client", lambda: (_ for _ in ()).throw(AssertionError("must not call"))
    )
    targets = [{"scope_id": "s1", "device_function": "ACCESS_SWITCH"}]
    result = config.run_firmware_compliance_campaign(
        targets, "10.16.1030", dry_run=False, confirm=False
    )
    assert "confirm=True is required" in result["error"]


def test_campaign_reports_partial_failure_without_aborting(monkeypatch):
    """One target fails (POST 500), the other succeeds — both are reported,
    and the failing target does not stop the second target from running."""
    responses_by_scope = {
        "s1": [FakeResponse(500, {"errors": ["boom"]})],
        "s2": [FakeResponse(201, {"name": "compliance-campus_ap"}),
               FakeResponse(200, {"policy": "ok"})],
    }
    calls: list[tuple[str, str]] = []

    class MultiScopeClient:
        def _request(self, method, endpoint, *, json=None, params=None):
            scope = (params or {}).get("scope-id")
            calls.append((method, scope))
            return responses_by_scope[scope].pop(0)

    monkeypatch.setattr(config, "get_client", lambda: MultiScopeClient())

    targets = [
        {"scope_id": "s1", "device_function": "ACCESS_SWITCH"},
        {"scope_id": "s2", "device_function": "CAMPUS_AP"},
    ]
    result = config.run_firmware_compliance_campaign(
        targets, "10.16.1030", dry_run=False, confirm=True
    )

    assert result["targets_attempted"] == 2
    assert result["targets_succeeded"] == 1
    assert result["targets_failed"] == 1
    by_scope = {r["scope_id"]: r for r in result["results"]}
    assert "error" in by_scope["s1"]
    assert "read_back" in by_scope["s2"]
    # s2 must still have been attempted even though s1 failed first.
    assert ("POST", "s2") in calls
