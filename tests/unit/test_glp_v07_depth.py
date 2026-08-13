"""Unit tests for the v0.7 GLP-depth curated additions (v07-glp-depth):

- Bounded, region-aware curated reads for Compute Ops Management,
  Storage Fleet, Block Storage, Virtualization, Backup & Recovery, and Data
  Services (all served from region-specific hosts per the committed
  manifest -- never global.api.greenlake.hpe.com).
- Guarded writes in those families (VM power on/off + a bounded bulk
  composite with partial-failure reporting; run-protection-job-now), gated
  behind the existing HPE_MCP_GLP_V2BETA1_WRITES flag with dry_run/
  confirm.
- plan_glp_reconciliation: read-only, cross-resource reconciliation/
  planning composite over devices/subscriptions/users/role-assignments/
  scope-groups/audit-logs/reporting-statuses.
- The hpe_networking_mcp.pipeline.live_test_config / hpe_networking_mcp.pipeline.artifact_contracts integration
  used by scripts/evaluate_glp_070_depth.py.

All host/region data is cross-checked against the committed manifest
(src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/glp.json) so upstream drift fails loudly
instead of silently mis-routing a real request.
"""

from __future__ import annotations

import asyncio

import pytest

import hpe_networking_mcp.mcp_servers.glp as glp
import hpe_networking_mcp.mcp_servers.openapi_gen.http_exec as http_exec
from hpe_networking_mcp.mcp_servers.openapi_gen.manifest import load_manifest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeTokenManager:
    def __init__(self, token="GLPTOKEN"):
        self._token = token
        self.calls = 0
        self.forced = 0

    def get_access_token(self, force_refresh=False, **kw):
        self.calls += 1
        if force_refresh:
            self.forced += 1
        return self._token


class _FakeCentralClient:
    def __init__(self, base_url="https://global.api.greenlake.hpe.com", token="GLPTOKEN"):
        self.base_url = base_url
        self.token_manager = _FakeTokenManager(token)


class _FakeGLPClient:
    def __init__(self):
        self._client = _FakeCentralClient()
        self.workspace_id = "ws-123"


def _patch_glp(monkeypatch, token="GLPTOKEN"):
    fake = _FakeGLPClient()
    fake._client.token_manager = _FakeTokenManager(token)
    monkeypatch.setattr(glp, "get_glp_client", lambda: fake)
    return fake


def _fn(name):
    return glp.mcp._tool_manager._tools[name].fn


def _fake_httpx(monkeypatch, captured, *, payload=None, status_code=200, resp_cls=None):
    class Resp:
        def __init__(self):
            self.status_code = status_code
            self.headers = {"content-type": "application/json"}
            self.text = "{}"

        def json(self):
            return payload if payload is not None else {"items": []}

    class FakeClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def request(self, method, url, headers=None, params=None, **kw):
            captured.setdefault("calls", []).append(
                {"method": method, "url": url, "headers": headers or {}, "params": params or {}}
            )
            captured.update(method=method, url=url, headers=headers or {}, params=params or {})
            return (resp_cls or Resp)()

    monkeypatch.setattr(http_exec.httpx, "AsyncClient", FakeClient)


@pytest.fixture(autouse=True)
def _clear_family_executor_cache():
    """Each test gets fresh cached executors (region/token can differ per test)."""
    glp._GLP_FAMILY_READ_EXECUTORS.clear()
    glp._GLP_FAMILY_WRITE_EXECUTORS.clear()
    yield
    glp._GLP_FAMILY_READ_EXECUTORS.clear()
    glp._GLP_FAMILY_WRITE_EXECUTORS.clear()


@pytest.fixture(autouse=True)
def _clean_writes_env(monkeypatch):
    monkeypatch.delenv("HPE_MCP_GLP_V2BETA1_WRITES", raising=False)
    monkeypatch.delenv("GLP_GENERATED_REGION", raising=False)


# ---------------------------------------------------------------------------
# Manifest cross-check -- host/region drift detection
# ---------------------------------------------------------------------------

_FAMILY_SOURCE_FILES = {
    "compute-ops-mgmt": "compute-ops-mgmt.json",
    "storage-fleet": "storage-fleet.json",
    "block-storage": "block-storage.json",
    "virtualization": "virtualization.json",
    "backup-recovery": "backup-recovery.json",
    "data-services": "data-services.json",
}


def test_family_hosts_match_committed_manifest():
    manifest = load_manifest("glp")
    by_file: dict[str, set[tuple[str, ...]]] = {}
    for op in manifest["operations"]:
        by_file.setdefault(op["source_file"], set()).add(tuple(op.get("server_urls") or ()))
    for family, source_file in _FAMILY_SOURCE_FILES.items():
        host_sets = by_file[source_file]
        assert len(host_sets) == 1, f"{source_file} has more than one distinct host set"
        (manifest_hosts,) = host_sets
        assert tuple(glp._GLP_FAMILY_HOSTS[family]) == manifest_hosts, (
            f"_GLP_FAMILY_HOSTS[{family!r}] is stale vs the committed manifest"
        )


def test_curated_family_operations_exist_in_manifest():
    manifest = load_manifest("glp")
    keys = {(op["method"], op["path"]) for op in manifest["operations"]}
    expected = [
        ("GET", "/compute-ops-mgmt/v1/servers"),
        ("GET", "/compute-ops-mgmt/v1/servers/{id}"),
        ("GET", "/compute-ops-mgmt/v1/servers/{id}/alerts"),
        ("GET", "/compute-ops-mgmt/v1/groups"),
        ("GET", "/compute-ops-mgmt/v1/jobs"),
        ("GET", "/storage-fleet/v1alpha1/storage-systems"),
        ("GET", "/storage-fleet/v1alpha1/storage-systems/{id}"),
        ("GET", "/storage-fleet/v1alpha1/storage-types"),
        ("GET", "/block-storage/v1alpha1/volumes"),
        ("GET", "/block-storage/v1alpha1/volumes/{id}"),
        ("GET", "/block-storage/v1alpha1/host-initiators"),
        ("GET", "/virtualization/v1beta1/virtual-machines"),
        ("GET", "/virtualization/v1beta1/virtual-machines/{vm-id}"),
        ("GET", "/virtualization/v1beta1/hypervisor-managers"),
        ("GET", "/virtualization/v1beta1/hypervisor-clusters"),
        ("GET", "/virtualization/v1beta1/datastores"),
        ("POST", "/virtualization/v1beta1/virtual-machines/{vm-id}/power-on"),
        ("POST", "/virtualization/v1beta1/virtual-machines/{vm-id}/power-off"),
        ("GET", "/backup-recovery/v1beta1/protection-jobs"),
        ("GET", "/backup-recovery/v1beta1/protection-jobs/{id}"),
        ("GET", "/backup-recovery/v1beta1/protection-stores"),
        ("GET", "/backup-recovery/v1beta1/storeonces"),
        ("GET", "/backup-recovery/v1beta1/virtual-machine-protection-groups"),
        ("POST", "/backup-recovery/v1beta1/protection-jobs/{id}/run"),
        ("GET", "/data-services/v1beta1/issues"),
        ("GET", "/data-services/v1beta1/issues/{id}"),
        ("GET", "/data-services/v1beta1/async-operations"),
        ("GET", "/data-services/v1beta1/storage-locations"),
    ]
    missing = [pair for pair in expected if pair not in keys]
    assert not missing, f"curated ops missing from committed manifest: {missing}"


def test_new_tools_registered_on_curated_server():
    tools = glp.mcp._tool_manager._tools
    new_tools = [
        "list_glp_compute_servers", "get_glp_compute_server", "list_glp_compute_server_alerts",
        "list_glp_compute_groups", "list_glp_compute_jobs",
        "list_glp_storage_systems", "get_glp_storage_system", "list_glp_storage_system_types",
        "list_glp_block_storage_volumes", "get_glp_block_storage_volume",
        "list_glp_block_storage_hosts",
        "list_glp_virtual_machines", "get_glp_virtual_machine", "list_glp_hypervisor_managers",
        "list_glp_hypervisor_clusters", "list_glp_datastores",
        "set_glp_virtual_machine_power", "set_glp_virtual_machines_power_bulk",
        "list_glp_backup_protection_jobs", "get_glp_backup_protection_job",
        "list_glp_backup_protection_stores", "list_glp_backup_storeonces",
        "list_glp_backup_vm_protection_groups", "run_glp_backup_protection_job",
        "list_glp_data_services_issues", "get_glp_data_services_issue",
        "list_glp_data_services_async_operations", "list_glp_data_services_storage_locations",
        "plan_glp_reconciliation",
    ]
    for name in new_tools:
        assert name in tools, name
    assert len(new_tools) == 29


def test_write_status_and_api_families_mention_new_tools():
    status = _fn("glp_write_status")()
    assert "set_glp_virtual_machine_power" in status["guarded_tools"]
    assert "set_glp_virtual_machines_power_bulk" in status["guarded_tools"]
    assert "run_glp_backup_protection_job" in status["guarded_tools"]

    families = _fn("list_glp_api_families")()
    assert "list_glp_storage_systems" in families["region_aware_family_tools"]
    assert "plan_glp_reconciliation" in families["reconciliation_tools"]
    assert set(families["region_aware_families"]) == set(_FAMILY_SOURCE_FILES)


# ---------------------------------------------------------------------------
# Region resolution
# ---------------------------------------------------------------------------


def test_family_server_requires_region():
    with pytest.raises(ValueError, match="GLP_GENERATED_REGION"):
        glp._glp_family_server("compute-ops-mgmt")


def test_family_server_resolves_data_host(monkeypatch):
    monkeypatch.setenv("GLP_GENERATED_REGION", "eu-west")
    assert glp._glp_family_server("storage-fleet") == "https://eu1.data.cloud.hpe.com"
    assert glp._glp_family_server("block-storage") == "https://eu1.data.cloud.hpe.com"


def test_family_server_resolves_api_host(monkeypatch):
    monkeypatch.setenv("GLP_GENERATED_REGION", "us-west")
    assert glp._glp_family_server("virtualization") == "https://us-west.api.greenlake.hpe.com"
    assert glp._glp_family_server("backup-recovery") == "https://us-west.api.greenlake.hpe.com"


def test_family_server_rejects_region_invalid_for_family(monkeypatch):
    # compute-ops-mgmt has no eu-west host (only us-west/eu-central/ap-northeast).
    monkeypatch.setenv("GLP_GENERATED_REGION", "eu-west")
    with pytest.raises(ValueError, match="GLP_GENERATED_REGION"):
        glp._glp_family_server("compute-ops-mgmt")


# ---------------------------------------------------------------------------
# Read dispatch -- pagination, auth, bounding
# ---------------------------------------------------------------------------


def test_list_read_forwards_pagination_and_bounds(monkeypatch):
    _patch_glp(monkeypatch)
    monkeypatch.setenv("GLP_GENERATED_REGION", "us-west")
    cap: dict = {}
    _fake_httpx(monkeypatch, cap, payload={"items": [1, 2, 3]})

    out = asyncio.run(_fn("list_glp_compute_servers")(limit=2, offset=1, filter="name eq 'x'"))

    assert cap["url"] == "https://us-west.api.greenlake.hpe.com/compute-ops-mgmt/v1/servers"
    assert cap["params"]["limit"] == 2
    assert cap["params"]["offset"] == 1
    assert cap["params"]["filter"] == "name eq 'x'"
    assert out["errors"] == []
    assert "_pagination" in out["data"]


def test_get_by_id_escapes_path(monkeypatch):
    _patch_glp(monkeypatch)
    monkeypatch.setenv("GLP_GENERATED_REGION", "eu-west")
    cap: dict = {}
    _fake_httpx(monkeypatch, cap, payload={"id": "sys 1"})
    out = asyncio.run(_fn("get_glp_storage_system")(system_id="sys 1"))
    # safe_api_path validates then returns the decoded path (consistent with
    # the existing curated _glp_read convention) -- httpx itself percent-
    # encodes the space when it actually builds the request line.
    assert cap["url"].endswith("/storage-fleet/v1alpha1/storage-systems/sys 1")
    assert out["data"] == {"id": "sys 1"}


def test_region_unset_surfaces_as_tool_error_not_exception(monkeypatch):
    _patch_glp(monkeypatch)
    out = asyncio.run(_fn("list_glp_virtual_machines")())
    assert out["data"] is None
    assert any("GLP_GENERATED_REGION" in e for e in out["errors"])


def test_reject_path_outside_family_prefix():
    # _glp_family_get is defense-in-depth even though only in-module callers
    # ever pass a path -- a path outside the family prefix must fail closed.
    out = asyncio.run(
        glp._glp_family_get("virtualization", "/compute-ops-mgmt/v1/servers")
    )
    assert out["data"] is None
    assert out["errors"]


def test_no_pagination_family_still_bounded(monkeypatch):
    _patch_glp(monkeypatch)
    monkeypatch.setenv("GLP_GENERATED_REGION", "us-west")
    cap: dict = {}
    _fake_httpx(monkeypatch, cap, payload={"items": list(range(500))})
    out = asyncio.run(_fn("list_glp_data_services_storage_locations")())
    assert len(out["data"]["items"]) <= 200


def test_403_surfaces_in_errors_not_raised(monkeypatch):
    _patch_glp(monkeypatch)
    monkeypatch.setenv("GLP_GENERATED_REGION", "us-west")

    class ForbiddenResp:
        status_code = 403
        headers = {"content-type": "application/json"}
        text = '{"message": "forbidden"}'

        def json(self):
            return {"message": "forbidden"}

    cap: dict = {}
    _fake_httpx(monkeypatch, cap, resp_cls=ForbiddenResp)
    out = asyncio.run(_fn("list_glp_backup_protection_jobs")())
    # make_read_executor does not raise_for_status for reads; _glp_family_get
    # surfaces the non-2xx status explicitly instead of masking it as success.
    assert out["status_code"] == 403
    assert out["errors"] and "403" in out["errors"][0]


def test_401_triggers_token_refresh_then_retries(monkeypatch):
    fake = _patch_glp(monkeypatch)
    monkeypatch.setenv("GLP_GENERATED_REGION", "us-west")

    calls = {"n": 0}

    class FlakyResp:
        def __init__(self):
            calls["n"] += 1
            self.status_code = 401 if calls["n"] == 1 else 200
            self.headers = {"content-type": "application/json"}
            self.text = "{}"

        def json(self):
            return {"items": []}

    cap: dict = {}
    _fake_httpx(monkeypatch, cap, resp_cls=FlakyResp)
    out = asyncio.run(_fn("list_glp_compute_groups")())
    assert calls["n"] == 2
    assert fake._client.token_manager.forced == 1
    assert out["errors"] == []


def test_429_retries_read(monkeypatch):
    _patch_glp(monkeypatch)
    monkeypatch.setenv("GLP_GENERATED_REGION", "us-west")

    calls = {"n": 0}

    class RateLimited:
        def __init__(self):
            calls["n"] += 1
            self.status_code = 429 if calls["n"] == 1 else 200
            self.headers = {"content-type": "application/json"}
            self.text = "{}"

        def json(self):
            return {"items": []}

    cap: dict = {}
    _fake_httpx(monkeypatch, cap, resp_cls=RateLimited)
    out = asyncio.run(_fn("list_glp_backup_storeonces")())
    assert calls["n"] == 2
    assert out["errors"] == []


# ---------------------------------------------------------------------------
# Guarded writes -- gate defaults, dry_run/confirm, partial failure
# ---------------------------------------------------------------------------


def test_vm_power_blocked_when_writes_disabled(monkeypatch):
    _patch_glp(monkeypatch)
    monkeypatch.setenv("GLP_GENERATED_REGION", "us-west")
    out = asyncio.run(
        _fn("set_glp_virtual_machine_power")(
            vm_id="vm1", action="power-off", dry_run=False, confirm=True
        )
    )
    assert (
        out["status"] == "FORBIDDEN"
        or out.get("status") == "blocked"
        or "blocked" in str(out)
    )


def test_vm_power_dry_run_default_previews_without_network(monkeypatch):
    monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
    _patch_glp(monkeypatch)
    monkeypatch.setenv("GLP_GENERATED_REGION", "us-west")

    def _boom(*a, **kw):
        raise AssertionError("dry_run must not touch the network")

    monkeypatch.setattr(http_exec.httpx, "AsyncClient", _boom)
    out = asyncio.run(_fn("set_glp_virtual_machine_power")(vm_id="vm1", action="power-on"))
    assert out["dry_run"] is True
    assert out["url"].endswith("/virtualization/v1beta1/virtual-machines/vm1/power-on")


def test_vm_power_requires_confirm_when_not_dry_run(monkeypatch):
    monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
    _patch_glp(monkeypatch)
    monkeypatch.setenv("GLP_GENERATED_REGION", "us-west")
    out = asyncio.run(
        _fn("set_glp_virtual_machine_power")(
            vm_id="vm1", action="power-on", dry_run=False, confirm=False
        )
    )
    assert "error" in out
    assert out["dry_run"] is True


def test_vm_power_executes_with_confirm(monkeypatch):
    monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
    _patch_glp(monkeypatch)
    monkeypatch.setenv("GLP_GENERATED_REGION", "us-west")
    cap: dict = {}
    _fake_httpx(monkeypatch, cap, payload={"ok": True})
    out = asyncio.run(
        _fn("set_glp_virtual_machine_power")(
            vm_id="vm1", action="power-off", dry_run=False, confirm=True
        )
    )
    assert out["status_code"] == 200
    assert cap["method"] == "POST"


def test_vm_power_bulk_partial_failure_reporting(monkeypatch):
    monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
    _patch_glp(monkeypatch)
    monkeypatch.setenv("GLP_GENERATED_REGION", "us-west")

    calls = {"n": 0}

    class MixedResp:
        def __init__(self):
            calls["n"] += 1
            self.status_code = 200 if calls["n"] == 1 else 500
            self.headers = {"content-type": "application/json"}
            self.text = "{}"

        def json(self):
            return {"ok": calls["n"] == 1}

    cap: dict = {}
    _fake_httpx(monkeypatch, cap, resp_cls=MixedResp)
    out = asyncio.run(
        _fn("set_glp_virtual_machines_power_bulk")(
            vm_ids=["vm1", "vm2"], action="power-off", dry_run=False, confirm=True
        )
    )
    assert out["requested"] == 2
    assert out["succeeded"] == 1
    assert out["failed"] == 1
    assert len(out["errors"]) == 1
    assert {r["vm_id"] for r in out["results"]} == {"vm1", "vm2"}


def test_vm_power_bulk_rejects_empty_and_oversized():
    out_empty = asyncio.run(
        _fn("set_glp_virtual_machines_power_bulk")(vm_ids=[], action="power-on")
    )
    assert out_empty["errors"]

    out_big = asyncio.run(
        _fn("set_glp_virtual_machines_power_bulk")(
            vm_ids=[f"vm{i}" for i in range(21)], action="power-on"
        )
    )
    assert out_big["errors"]
    assert out_big["results"] == []


def test_run_backup_protection_job_dry_run_shows_body(monkeypatch):
    monkeypatch.setenv("HPE_MCP_GLP_V2BETA1_WRITES", "1")
    _patch_glp(monkeypatch)
    monkeypatch.setenv("GLP_GENERATED_REGION", "us-west")
    out = asyncio.run(_fn("run_glp_backup_protection_job")(job_id="job1", full_backup=True))
    assert out["dry_run"] is True
    assert out["json"] == {"fullBackup": True, "includeResources": [], "scheduleIds": []}
    assert out["url"].endswith("/backup-recovery/v1beta1/protection-jobs/job1/run")


# ---------------------------------------------------------------------------
# Reconciliation / planning (read-only, partial-failure resilient)
# ---------------------------------------------------------------------------


def test_plan_reconciliation_read_only_and_bounded(monkeypatch):
    monkeypatch.setattr(
        glp,
        "list_glp_devices",
        lambda limit, offset: {
            "items": [{"serialNumber": "SN1"}, {"serialNumber": "SN2", "subscriptionId": "sub1"}],
            "errors": [],
        },
    )
    monkeypatch.setattr(
        glp,
        "list_glp_subscriptions",
        lambda limit, offset: {"items": [{"id": "sub1"}], "errors": []},
    )
    monkeypatch.setattr(
        glp, "list_glp_users", lambda limit, offset: {"items": [{"id": "u1"}], "errors": []}
    )
    monkeypatch.setattr(
        glp,
        "list_glp_role_assignments",
        lambda limit, offset: {"data": {"items": []}, "errors": []},
    )
    monkeypatch.setattr(
        glp,
        "list_glp_scope_groups",
        lambda limit, offset: {"data": {"items": [{"id": "sg1"}]}, "errors": []},
    )
    monkeypatch.setattr(
        glp, "list_glp_audit_logs", lambda limit, offset: {"items": [], "errors": []}
    )
    monkeypatch.setattr(
        glp,
        "list_glp_reporting_statuses",
        lambda limit, offset: {"data": {"items": [{"id": "r1", "status": "FAILED"}]}, "errors": []},
    )

    out = _fn("plan_glp_reconciliation")(sample_size=50)

    assert out["counts"]["devices"] == 2
    assert out["counts"]["users"] == 1
    finding_types = {f["type"] for f in out["findings"]}
    assert "device_without_subscription" in finding_types
    assert "users_present_without_any_sampled_role_assignment" in finding_types
    assert "reporting_status_failure" in finding_types
    assert out["errors"] == []
    assert all(section["ok"] for section in out["sections"].values())


def test_plan_reconciliation_partial_failure_does_not_abort(monkeypatch):
    def _boom(limit, offset):
        raise RuntimeError("upstream 500")

    monkeypatch.setattr(glp, "list_glp_devices", _boom)
    monkeypatch.setattr(
        glp, "list_glp_subscriptions", lambda limit, offset: {"items": [], "errors": []}
    )
    monkeypatch.setattr(glp, "list_glp_users", lambda limit, offset: {"items": [], "errors": []})
    monkeypatch.setattr(
        glp,
        "list_glp_role_assignments",
        lambda limit, offset: {"data": {"items": []}, "errors": []},
    )
    monkeypatch.setattr(
        glp, "list_glp_scope_groups", lambda limit, offset: {"data": {"items": []}, "errors": []}
    )
    monkeypatch.setattr(
        glp, "list_glp_audit_logs", lambda limit, offset: {"items": [], "errors": []}
    )
    monkeypatch.setattr(
        glp,
        "list_glp_reporting_statuses",
        lambda limit, offset: {"data": {"items": []}, "errors": []},
    )

    out = _fn("plan_glp_reconciliation")()

    assert out["sections"]["devices"]["ok"] is False
    assert any("devices" in e for e in out["errors"])
    # every other section still ran despite the devices failure
    assert out["sections"]["subscriptions"]["ok"] is True


def test_plan_reconciliation_never_writes():
    # No write-shaped helper (_write_disabled/executor) is reachable from
    # plan_glp_reconciliation -- it only calls list_glp_* read tools.
    import inspect

    source = inspect.getsource(glp.plan_glp_reconciliation)
    assert "_write_disabled" not in source
    assert "_glp_family_write_executor" not in source
    assert "dry_run" not in source


# ---------------------------------------------------------------------------
# Artifact leakage -- no raw tokens/headers ever surface in tool output
# ---------------------------------------------------------------------------


def test_read_tool_output_never_contains_raw_token(monkeypatch):
    _patch_glp(monkeypatch, token="SUPERSECRETTOKEN")
    monkeypatch.setenv("GLP_GENERATED_REGION", "us-west")
    cap: dict = {}
    _fake_httpx(monkeypatch, cap, payload={"items": [{"id": 1}]})
    out = asyncio.run(_fn("list_glp_storage_systems")())
    assert "SUPERSECRETTOKEN" not in repr(out)
