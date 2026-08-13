"""Regression tests for the verified Aruba Central / GLP fixes.

Each test fails against the pre-fix code and passes after the corresponding
source change. Covers:
  1. list_inventory server-side OData filtering (monitoring.py)
  2. assign_device_to_site non-numeric site_id guard (config.py)
  3. poe/port/cable unknown-vs-AP distinction (ops.py)
  4. GLP get_device transient-failure propagation (glp_client.py)
  5. VLAN scope-map resume idempotency for BOTH scopes (s6_configure.py)
  6. SSH brute-force min_failures clamp + unattributed bucket (monitoring.py)
  7. subscription-key validation/escaping before OData (glp_client.py)
  8. GLP list pagination metadata through client + MCP wrappers (glp_client/glp)
  9. audit-log id quoting in direct GLPClient helpers (glp_client.py)
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

import hpe_networking_mcp.mcp_servers.config as cfg
import hpe_networking_mcp.mcp_servers.glp as glp
import hpe_networking_mcp.mcp_servers.monitoring as mon
import hpe_networking_mcp.mcp_servers.ops as ops
import hpe_networking_mcp.pipeline.stages.s6_configure as s6
from hpe_networking_mcp.pipeline.clients.glp_client import GLPClient, _pagination_fields


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _glp_client(fake_transport) -> GLPClient:
    client = GLPClient.__new__(GLPClient)
    client._client = fake_transport
    client.workspace_id = "workspace"
    client._device_id_cache = {}
    return client


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text


class _ConflictError(Exception):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.response = _Resp(text)


# --------------------------------------------------------------------------
# (1) list_inventory server-side OData filtering
# --------------------------------------------------------------------------
def test_list_inventory_builds_server_side_odata_filter(monkeypatch):
    mc = MagicMock()
    mc.get_devices_page.return_value = (
        [{"serialNumber": "AP1", "deviceType": "ACCESS_POINT", "isProvisioned": "YES"}],
        "cursor-2",
    )
    monkeypatch.setattr(mon, "get_mcp_client", lambda: mc)

    result = mon.list_inventory(status="yes", device_type="AP", limit=999)

    # AP normalized to ACCESS_POINT, status normalized to YES, ANDed, limit clamped.
    mc.get_devices_page.assert_called_once_with(
        {"filter": "deviceType eq 'ACCESS_POINT' and isProvisioned eq 'YES'"},
        limit=200,
        next_cursor=None,
    )
    # No client-side single-page post-filter drops the returned row.
    assert result["items"] == [
        {"serialNumber": "AP1", "deviceType": "ACCESS_POINT", "isProvisioned": "YES"}
    ]
    assert result["next_cursor"] == "cursor-2"
    assert result["errors"] == []


def test_list_inventory_device_type_only_and_none(monkeypatch):
    mc = MagicMock()
    mc.get_devices_page.return_value = ([], None)
    monkeypatch.setattr(mon, "get_mcp_client", lambda: mc)

    mon.list_inventory(device_type="GATEWAY")
    assert mc.get_devices_page.call_args.args[0] == {"filter": "deviceType eq 'GATEWAY'"}

    mc.get_devices_page.reset_mock()
    mc.get_devices_page.return_value = ([], None)
    mon.list_inventory(status="NO")
    assert mc.get_devices_page.call_args.args[0] == {"filter": "isProvisioned eq 'NO'"}

    mc.get_devices_page.reset_mock()
    mc.get_devices_page.return_value = ([], None)
    mon.list_inventory()
    assert mc.get_devices_page.call_args.args[0] is None


def test_list_inventory_forwards_next_cursor(monkeypatch):
    mc = MagicMock()
    mc.get_devices_page.return_value = ([], None)
    monkeypatch.setattr(mon, "get_mcp_client", lambda: mc)

    mon.list_inventory(next_cursor="opaque-cursor")
    assert mc.get_devices_page.call_args.kwargs["next_cursor"] == "opaque-cursor"


def test_list_inventory_escapes_single_quote(monkeypatch):
    mc = MagicMock()
    mc.get_devices_page.return_value = ([], None)
    monkeypatch.setattr(mon, "get_mcp_client", lambda: mc)

    mon.list_inventory(device_type="O'Brien")  # nonsense value, but must be escaped
    assert mc.get_devices_page.call_args.args[0] == {"filter": "deviceType eq 'O''BRIEN'"}


# --------------------------------------------------------------------------
# (2) assign_device_to_site non-numeric site_id guard
# --------------------------------------------------------------------------
def test_assign_device_to_site_non_numeric_does_not_crash(monkeypatch):
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 404
    client._request.return_value = resp
    monkeypatch.setattr(cfg, "get_client", lambda: client)

    out = cfg.assign_device_to_site("SER1", "campus-a")  # non-numeric id

    # The string-id primary candidate is attempted first (no eager crash)...
    assert client._request.call_args_list[0].args == (
        "POST",
        "/network-monitoring/v1/sites/campus-a/devices",
    )
    # ...and the two legacy int-payload candidates are skipped, not fatal.
    assert any("is not numeric" in e for e in out["errors"])
    # Only the primary candidate reached the transport.
    assert client._request.call_count == 1


def test_assign_device_to_site_numeric_builds_legacy_payloads(monkeypatch):
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 404
    client._request.return_value = resp
    monkeypatch.setattr(cfg, "get_client", lambda: client)

    cfg.assign_device_to_site("SER1", "12345", device_type="SWITCH")

    legacy = client._request.call_args_list[1]
    assert legacy.args[0] == "POST"
    assert legacy.args[1] == "/central/v2/sites/associate"
    assert legacy.kwargs["json"] == {
        "site_id": 12345,
        "device_id": ["SER1"],
        "device_type": "SWITCH",
    }


# --------------------------------------------------------------------------
# (3) ops poe/port/cable unknown-vs-AP distinction
# --------------------------------------------------------------------------
@pytest.mark.parametrize("tool", ["poe_bounce", "port_bounce", "cable_test"])
def test_ops_unknown_device_type_is_not_reported_as_ap(monkeypatch, tool):
    monkeypatch.setattr(ops, "device_type_for_troubleshoot", lambda s, d: None)
    fn = getattr(ops, tool)
    if tool == "cable_test":
        result = asyncio.run(fn("S1", ["1/1/1"]))
    else:
        result = asyncio.run(fn(MagicMock(), "S1", ["1"]))
    joined = " ".join(result["errors"])
    assert "Could not determine device type" in joined
    assert "Access Points" not in joined


@pytest.mark.parametrize("tool", ["poe_bounce", "port_bounce", "cable_test"])
def test_ops_ap_still_reported_unsupported(monkeypatch, tool):
    monkeypatch.setattr(ops, "device_type_for_troubleshoot", lambda s, d: "aps")
    fn = getattr(ops, tool)
    if tool == "cable_test":
        result = asyncio.run(fn("S1", ["1/1/1"]))
    else:
        result = asyncio.run(fn(MagicMock(), "S1", ["1"]))
    assert "Access Points" in " ".join(result["errors"])


# --------------------------------------------------------------------------
# (4) GLP get_device transient-failure propagation
# --------------------------------------------------------------------------
def test_get_device_propagates_transient_failure():
    class Raiser:
        def get(self, path, params=None):
            raise RuntimeError("401 Unauthorized")

    client = _glp_client(Raiser())
    with pytest.raises(RuntimeError, match="device lookup failed"):
        client.get_device("SG1")


def test_get_device_returns_none_on_empty_filter_result():
    class Empty:
        def get(self, path, params=None):
            return {"items": []}

    assert _glp_client(Empty()).get_device("SG1") is None


def test_get_device_returns_hit_and_escapes_serial():
    seen = {}

    class Hit:
        def get(self, path, params=None):
            seen["params"] = params
            return {"items": [{"id": "dev-1", "serialNumber": "SG1"}]}

    client = _glp_client(Hit())
    assert client.get_device("SG'1")["id"] == "dev-1"
    assert seen["params"]["filter"] == "serialNumber eq 'SG''1'"


# --------------------------------------------------------------------------
# (5) VLAN scope-map resume idempotency for BOTH scopes
# --------------------------------------------------------------------------
def test_push_vlan_interface_tolerates_duplicate_on_both_scopes(monkeypatch):
    seen = []

    def fake_scope_map(cc, scope_id, persona, resource):
        seen.append((scope_id, resource))
        raise _ConflictError("Scope map already exists")

    monkeypatch.setattr(s6, "_post_scope_map", fake_scope_map)

    vi = {"vlan": 100, "ip_address": None, "helper_address": None, "dhcp": False}
    central = MagicMock()
    central.post.side_effect = _ConflictError("duplicate")  # steps 1-3 already applied

    # Must NOT raise even though the *global* scope-map already exists.
    s6._push_vlan_interface(central, vi, "dev-1", "glob-1", "ACCESS_SWITCH")

    assert ("glob-1", "layer2-vlan/100") in seen
    assert ("dev-1", "vlan-interfaces/100") in seen


def test_push_vlan_interface_reraises_non_idempotent_scope_map_error(monkeypatch):
    class HardError(Exception):
        def __init__(self):
            super().__init__("500 boom")
            self.response = _Resp("internal server error")

    def fake_scope_map(cc, scope_id, persona, resource):
        raise HardError()

    monkeypatch.setattr(s6, "_post_scope_map", fake_scope_map)
    vi = {"vlan": 100, "ip_address": None, "helper_address": None, "dhcp": False}
    central = MagicMock()
    central.post.side_effect = _ConflictError("duplicate")

    with pytest.raises(HardError):
        s6._push_vlan_interface(central, vi, "dev-1", "glob-1", "ACCESS_SWITCH")


def test_is_idempotent_conflict_matches_markers():
    assert s6._is_idempotent_conflict(_ConflictError("Resource already exists"))
    assert s6._is_idempotent_conflict(_ConflictError("duplicate entry"))
    assert not s6._is_idempotent_conflict(_ConflictError("bad request"))
    assert not s6._is_idempotent_conflict(RuntimeError("no response attr"))


# --------------------------------------------------------------------------
# (6) SSH brute-force clamp + unattributed bucket
# --------------------------------------------------------------------------
def test_detect_ssh_brute_force_clamps_and_buckets_unattributed(monkeypatch):
    mc = MagicMock()
    mc.get_events.return_value = [
        {"eventId": "5210", "eventName": "ssh", "description": "from 10.0.0.1", "timeAt": 1},
        {"eventId": "5210", "eventName": "ssh", "description": "from 10.0.0.1", "timeAt": 2},
        {"eventId": "5214", "eventName": "ssh-deny", "description": "no ip in here", "timeAt": 3},
        {"eventId": "5214", "eventName": "ssh-deny", "description": "also none", "timeAt": 4},
    ]
    monkeypatch.setattr(mon, "get_mcp_client", lambda: mc)

    result = mon.detect_ssh_brute_force("SW1", min_failures=0)

    assert result["min_failures_threshold"] == 1  # clamped from 0
    assert result["unattributed_failures"] == 2
    sources = [f["source_ip"] for f in result["flagged_sources"]]
    assert sources == ["10.0.0.1"]
    assert "unknown" not in sources


# --------------------------------------------------------------------------
# (7) subscription-key validation/escaping before OData
# --------------------------------------------------------------------------
def test_resolve_subscription_id_valid_key_is_escaped_in_filter():
    seen = {}

    class Sub:
        def get(self, path, params=None):
            seen["params"] = params
            return {"items": [{"id": "sub-uuid"}]}

    client = _glp_client(Sub())
    assert client._resolve_subscription_id("ABC123-KEY_9") == "sub-uuid"
    assert seen["params"]["filter"] == "key eq 'ABC123-KEY_9'"


def test_resolve_subscription_id_rejects_injection():
    class Sub:
        def get(self, path, params=None):
            raise AssertionError("must not reach transport for an unsafe key")

    client = _glp_client(Sub())
    with pytest.raises(ValueError, match="Invalid subscription key"):
        client._resolve_subscription_id("bad' or key eq '1")


# --------------------------------------------------------------------------
# (8) GLP list pagination metadata through client + MCP wrappers
# --------------------------------------------------------------------------
def test_pagination_fields_extracts_and_defaults():
    assert _pagination_fields(
        {"items": [1, 2], "count": 2, "offset": 5, "total": 40, "next": "cur"}, [1, 2]
    ) == {"count": 2, "offset": 5, "total": 40, "next": "cur"}
    # count defaults to page length when absent; booleans are not ints here.
    assert _pagination_fields({"items": [1]}, [1]) == {"count": 1}


def test_list_devices_page_surfaces_metadata_and_list_compat():
    class LC:
        def get(self, path, params=None):
            return {"items": [{"id": 1}], "count": 1, "offset": 0, "total": 9}

    client = _glp_client(LC())
    page = client.list_devices_page(limit=10, offset=0)
    assert page == {"items": [{"id": 1}], "count": 1, "offset": 0, "total": 9}
    # Back-compat: list_devices still returns a bare list.
    assert client.list_devices(limit=10, offset=0) == [{"id": 1}]


def test_list_glp_devices_wrapper_includes_pagination(monkeypatch):
    class DummyGLP:
        def list_devices_page(self, limit=100, offset=0, filter=None):
            return {
                "items": [{"id": "d1"}],
                "count": 1,
                "offset": 3,
                "total": 25,
                "next": "cursor-x",
            }

    monkeypatch.setattr(glp, "get_glp_client", lambda: DummyGLP())
    out = glp.list_glp_devices(limit=10, offset=3)
    assert out["items"] == [{"id": "d1"}]
    assert out["errors"] == []
    assert out["count"] == 1
    assert out["offset"] == 3
    assert out["total"] == 25
    assert out["next"] == "cursor-x"


def test_list_glp_audit_logs_wrapper_includes_pagination(monkeypatch):
    class DummyGLP:
        def list_audit_logs_page(self, **kwargs):
            return {"items": [], "count": 0, "offset": 0, "total": 0}

    monkeypatch.setattr(glp, "get_glp_client", lambda: DummyGLP())
    out = glp.list_glp_audit_logs(limit=5)
    assert out["items"] == []
    assert out["total"] == 0
    assert out["errors"] == []


# --------------------------------------------------------------------------
# (9) audit-log id quoting in direct GLPClient helpers
# --------------------------------------------------------------------------
def test_audit_log_helpers_quote_id():
    class AuditClient:
        def __init__(self):
            self.paths = []

        def get(self, path, params=None):
            self.paths.append(path)
            return {}

    fake = AuditClient()
    client = _glp_client(fake)
    client.get_audit_log_v2beta1("a b/c#d")
    client.get_audit_log_v2beta1_detail("a b/c#d")
    assert fake.paths == [
        "/audit-log/v2beta1/logs/a%20b%2Fc%23d",
        "/audit-log/v2beta1/logs/a%20b%2Fc%23d/details",
    ]
