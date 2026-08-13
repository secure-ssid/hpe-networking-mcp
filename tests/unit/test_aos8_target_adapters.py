"""Pure/fake-invoker tests for AOS8 Classic/New Central target adapters."""

from __future__ import annotations

import pytest

from hpe_networking_mcp.pipeline.aos8_target_adapters import (
    MAX_SECRET_LENGTH,
    VERIFICATION_READ_PAGE_SIZE,
    ClassicCentralAdapter,
    ConflictPolicy,
    ContextValidationError,
    DependencySelectionError,
    NewCentralAdapter,
    TargetContext,
    TargetType,
    WriteGateError,
)


def candidate(
    object_type: str,
    identifier: str,
    *,
    payload: dict | None = None,
    dependencies: list[str] | None = None,
    apply_order: int = 10,
    unsupported_fields: dict | None = None,
    requires_secret_input: bool = False,
) -> dict:
    return {
        "object_type": object_type,
        "identifier": identifier,
        "payload": payload or {},
        "dependencies": dependencies or [],
        "apply_order": apply_order,
        "unsupported_fields": unsupported_fields or {},
        "requires_secret_input": requires_secret_input,
        "secret_fields": [],
        "warnings": [],
    }


def security(mode: str, **overrides) -> dict:
    base = {
        "mode": mode,
        "opmode": overrides.pop("opmode", mode),
        "ambiguous": False,
        "aaa_profile": None,
        "dot1x_auth_profile": None,
        "mac_auth_profile": None,
        "passphrase_present": False,
        "psk_hexkey_present": False,
        "wpa3_transition": False,
        "evidence": [],
    }
    base.update(overrides)
    return base


class FakeBackend:
    def __init__(self, reads=None, failures=None, write_results=None):
        self.reads = reads or {}
        self.failures = failures or {}
        # Optional override of the *returned* write result (as opposed to
        # `failures`, which makes the write invoker raise). Used to simulate
        # a write invoker that returns without raising but whose result
        # itself signals rejection (status_code/ok/error), or a "successful"
        # write whose subsequent read-back does not actually confirm it.
        self.write_results = write_results or {}
        self.read_calls = []
        self.write_calls = []

    def read(self, operation):
        self.read_calls.append(operation)
        value = self.reads.get(operation.name)
        if isinstance(value, Exception):
            raise value
        return value

    def write(self, operation, *, confirmation):
        self.write_calls.append((operation, confirmation))
        failure = self.failures.get(operation.name)
        if failure:
            raise failure
        if operation.name in self.write_results:
            return self.write_results[operation.name]
        return {"ok": True, "name": operation.name}


def resolve_scope(context):
    if context.scope_name == "bad":
        raise ValueError("unknown scope")
    return context.scope_id or "100", context.scope_name or "Branch"


def validate_persona(context):
    if context.persona not in {
        "CAMPUS_AP",
        "MICROBRANCH_AP",
        "MOBILITY_GW",
        "ACCESS_SWITCH",
    }:
        raise ValueError("invalid persona")
    return context.persona


def new_adapter(
    backend,
    *,
    policy=ConflictPolicy.FAIL,
    secrets=None,
    writes=True,
    cluster=False,
    persona="CAMPUS_AP",
):
    return NewCentralAdapter(
        TargetContext(
            target_type=TargetType.NEW_CENTRAL,
            scope_id="100",
            scope_name="Branch",
            persona=persona,
            cluster_name="cluster-1" if cluster else None,
            cluster_scope_id="200" if cluster else None,
            conflict_policy=policy,
            secret_inputs=secrets or {},
        ),
        scope_resolver=resolve_scope,
        persona_validator=validate_persona,
        read_invoker=backend.read,
        write_invoker=backend.write,
        writes_enabled=lambda target: writes,
    )


def classic_adapter(
    backend,
    *,
    policy=ConflictPolicy.FAIL,
    scope_name="Branch Group",
    secrets=None,
    external_object_references=None,
    ap_group_target_map=None,
    ap_group_device_serials=None,
    writes=True,
):
    return ClassicCentralAdapter(
        TargetContext(
            target_type=TargetType.CLASSIC_CENTRAL,
            scope_id="classic-id",
            scope_name=scope_name,
            persona="CAMPUS_AP",
            conflict_policy=policy,
            secret_inputs=secrets or {},
            external_object_references=external_object_references or {},
            ap_group_target_map=ap_group_target_map or {},
            ap_group_device_serials=ap_group_device_serials or {},
        ),
        scope_resolver=resolve_scope,
        persona_validator=validate_persona,
        read_invoker=backend.read,
        write_invoker=backend.write,
        writes_enabled=lambda target: writes,
    )


def test_context_scope_and_persona_are_validated_by_injected_collaborators():
    backend = FakeBackend()
    with pytest.raises(ContextValidationError, match="unknown scope"):
        NewCentralAdapter(
            TargetContext(
                target_type=TargetType.NEW_CENTRAL,
                scope_name="bad",
                persona="CAMPUS_AP",
            ),
            scope_resolver=resolve_scope,
            persona_validator=validate_persona,
            read_invoker=backend.read,
            write_invoker=backend.write,
            writes_enabled=lambda target: True,
        )
    with pytest.raises(ContextValidationError, match="invalid persona"):
        NewCentralAdapter(
            TargetContext(
                target_type=TargetType.NEW_CENTRAL,
                scope_name="Branch",
                persona="CONTROLLER",
            ),
            scope_resolver=resolve_scope,
            persona_validator=validate_persona,
            read_invoker=backend.read,
            write_invoker=backend.write,
            writes_enabled=lambda target: True,
        )


def test_new_vlan_preview_has_verified_tool_scope_and_deterministic_order():
    backend = FakeBackend()
    adapter = new_adapter(backend)
    vlan = candidate("vlan", "20", payload={"description": "Corp"}, apply_order=10)
    role = candidate(
        "role",
        "employee",
        payload={"name": "employee", "vlan": 20, "policies": ["allowall"]},
        dependencies=["vlan:20"],
        apply_order=30,
    )

    preview = adapter.preview([role, vlan])

    assert [item["candidate"] for item in preview["operations"]] == [
        "vlan:20",
        "role:employee",
    ]
    vlan_operation = preview["operations"][0]["operations"][0]
    assert vlan_operation["tool_or_endpoint"] == "create_vlan"
    assert vlan_operation["arguments"]["scope_id"] == "100"
    role_assignment = preview["operations"][1]["operations"][1]
    assert role_assignment["tool_or_endpoint"] == ("/network-config/v1alpha1/config-assignments")
    assert role_assignment["payload"]["config-assignment"][0]["device-function"] == ("CAMPUS_AP")
    assert preview["checkpoint_and_rollback"] == {
        "post_change_checkpoint_policy_only": True,
        "automatic_rollback_supported": True,
        "manual_checkpoint_restore_supported": False,
        "guidance": (
            "Optionally configure build_config_checkpoint_policy before migration. "
            "New Central can generate a post-change checkpoint and devices can "
            "automatically revert a failed push; there is no manual checkpoint "
            "listing or restore operation."
        ),
        "provenance": (
            "hpe_networking_mcp.mcp_servers.config.build_config_checkpoint_policy and "
            "get_config_rollback_status; "
            "developer.arubanetworks.com/new-central-config/reference/config-checkpoint"
        ),
    }


def test_selection_adds_dependency_closure_and_can_reject_omissions():
    backend = FakeBackend()
    adapter = new_adapter(backend)
    vlan = candidate("vlan", "20")
    role = candidate(
        "role",
        "employee",
        payload={"policies": ["allowall"]},
        dependencies=["vlan:20"],
        apply_order=30,
    )
    preview = adapter.preview([role, vlan], selected={"role:employee"})
    assert [item["candidate"] for item in preview["operations"]] == [
        "vlan:20",
        "role:employee",
    ]

    with pytest.raises(DependencySelectionError, match="was not selected"):
        adapter.preview(
            [role, vlan],
            selected={"role:employee"},
            include_dependency_closure=False,
        )

    unresolved = adapter.preview(
        [role, vlan],
        selected={"role:employee"},
        include_dependency_closure=False,
        allow_unresolved_blockers=True,
    )
    assert unresolved["operations"][0]["status"] == "blocked"
    assert "was not selected" in unresolved["operations"][0]["blockers"][0]


def test_missing_candidate_dependency_rejected_unless_previewing_blocker():
    backend = FakeBackend()
    adapter = new_adapter(backend)
    role = candidate(
        "role",
        "employee",
        payload={"policies": ["allowall"]},
        dependencies=["vlan:20"],
    )
    with pytest.raises(DependencySelectionError, match="absent"):
        adapter.preview([role])
    preview = adapter.preview([role], allow_unresolved_blockers=True)
    assert preview["operations"][0]["status"] == "blocked"


def test_conflict_fail_skip_and_update_behaviors():
    existing = {"items": [{"name": "employee"}]}
    role = candidate("role", "employee", payload={"policies": ["allowall"]})

    failed = new_adapter(FakeBackend(reads={"list_roles": existing})).preview([role])
    assert failed["operations"][0]["status"] == "blocked"

    skipped = new_adapter(
        FakeBackend(reads={"list_roles": existing}),
        policy=ConflictPolicy.SKIP_EXISTING,
    ).preview([role])
    assert skipped["operations"][0]["status"] == "skipped"

    updated = new_adapter(
        FakeBackend(reads={"list_roles": existing}),
        policy=ConflictPolicy.UPDATE,
    ).preview([role])
    assert updated["operations"][0]["conflict"] == "update"
    assert updated["operations"][0]["operations"][0]["tool_or_endpoint"] == "update_role"


def test_update_policy_blocks_when_verified_update_is_unavailable():
    # WLAN mappings have no verified New Central update tool/endpoint today
    # (build_underlay_ssid/build_overlay_ssid are create-only); RADIUS/LDAP/
    # TACACS auth-servers, AAA/dot1x/macauth profiles, server-groups, and
    # roles all now carry a verified PATCH/update path (see
    # test_radius_*_update_and_delete_operations_are_verified and friends),
    # so WLAN is the remaining example of this "no update operation" gate.
    wlan = candidate(
        "wlan",
        "Guest",
        payload={
            "name": "Guest",
            "essid": "Guest",
            "vlan": 20,
            "aaa_profile": None,
            "security": {"mode": "open", "opmode": "open", "ambiguous": False},
        },
        unsupported_fields={
            "ssid_profile.opmode": "open",
            "virtual_ap.forward_mode": "bridge",
        },
    )
    backend = FakeBackend(reads={"get_ssid": {"name": "Guest"}})
    adapter = new_adapter(backend, policy=ConflictPolicy.UPDATE)
    preview = adapter.preview([wlan])
    assert preview["operations"][0]["conflict"] == "existing"
    assert preview["operations"][0]["status"] == "blocked"
    assert "no update operation" in preview["operations"][0]["blockers"][0]


def test_radius_requires_caller_secret_and_masks_preview():
    auth = candidate(
        "auth_server",
        "radius:rad1",
        payload={"name": "rad1", "server_type": "radius", "host": "10.0.0.10"},
        unsupported_fields={"rad_authport": 1812, "rad_key": "<redacted:present>"},
        requires_secret_input=True,
    )
    missing = new_adapter(FakeBackend()).preview([auth])
    assert missing["operations"][0]["status"] == "unsupported"
    assert "non-redacted target secret" in missing["operations"][0]["unsupported_warnings"][0]

    redacted = new_adapter(
        FakeBackend(),
        secrets={"auth_server:radius:rad1": {"shared_secret": "<redacted:present>"}},
    ).preview([auth])
    assert redacted["operations"][0]["status"] == "unsupported"

    valid = new_adapter(
        FakeBackend(),
        secrets={"auth_server:radius:rad1": {"shared_secret": "actual-secret"}},
    ).preview([auth])
    arguments = valid["operations"][0]["operations"][0]["arguments"]
    assert arguments["shared_secret"] == "***"
    assert "actual-secret" not in str(valid)


def test_simple_aaa_profile_maps_only_verified_fields():
    # AAA profiles are a Gateway/Switch device concept only (never AP), per
    # docs/aos8-migration-contract-matrix.md §4; use a Gateway persona.
    aaa = candidate(
        "aaa_profile",
        "guest-aaa",
        payload={
            "name": "guest-aaa",
            "default_user_role": "guest",
            "accounting_server_group": "acct-group",
        },
    )
    preview = new_adapter(FakeBackend(), persona="MOBILITY_GW").preview([aaa])
    operation = preview["operations"][0]["operations"][0]
    assert operation["tool_or_endpoint"] == "create_aaa_profile"
    assert operation["arguments"]["auth_role"] == "guest"
    assert operation["arguments"]["acct_server_group"] == "acct-group"


def test_aaa_profile_maps_device_auth_profile_bindings():
    aaa = candidate(
        "aaa_profile",
        "corp-aaa",
        payload={
            "name": "corp-aaa",
            "default_user_role": "employee",
            "dot1x_auth_profile": "corp-dot1x",
            "mac_auth_profile": "corp-mac",
        },
        dependencies=[
            "dot1x_auth_profile:corp-dot1x",
            "mac_auth_profile:corp-mac",
        ],
    )

    action = new_adapter(
        FakeBackend(), persona="MOBILITY_GW"
    ).candidate_action(aaa)

    create = action.operations[0]
    assert create.name == "create_aaa_profile"
    assert create.arguments["dot1x_auth_profile"] == "corp-dot1x"
    assert create.arguments["mac_auth_profile"] == "corp-mac"
    assert action.update_operations[0].payload["authentication"] == {
        "dot1x-auth": "corp-dot1x",
        "mac-auth": "corp-mac",
    }


def test_aaa_profile_rejects_ap_persona_and_exposes_update_delete_operations():
    aaa = candidate(
        "aaa_profile",
        "guest-aaa",
        payload={"name": "guest-aaa", "default_user_role": "guest"},
    )
    rejected = new_adapter(FakeBackend(), persona="CAMPUS_AP").preview([aaa])
    assert rejected["operations"][0]["status"] == "unsupported"
    assert "device-function" in rejected["operations"][0]["unsupported_warnings"][0]

    preview = new_adapter(FakeBackend(), persona="ACCESS_SWITCH").preview([aaa])
    entry = preview["operations"][0]
    # Finding #1 (fail-closed): the aaa-profile object itself is verified
    # (create/update/delete/read all present below), but the SHARED
    # config-assignment profile-type "aaa-profile" is not independently
    # evidenced locally, so this candidate stays "blocked" -- it is never
    # executed and never claims a completed migration.
    assert entry["status"] == "blocked"
    assert entry["update_operations"][0]["method"] == "PATCH"
    assert entry["delete_operations"][0]["tool_or_endpoint"] == "delete_aaa_profile"
    assert len(entry["operations"]) == 1
    assert len(entry["update_operations"]) == 1
    assert len(entry["delete_operations"]) == 1
    assert entry["rollback_supported"] is False
    assert any("aaa-profile" in blocker for blocker in entry["blockers"])
    assert any("config-assignment" in blocker for blocker in entry["blockers"])


def test_unsupported_objects_and_lossy_mappings_remain_unapplied():
    backend = FakeBackend()
    adapter = new_adapter(backend)
    ldap = candidate(
        "auth_server",
        "ldap:ldap1",
        payload={"name": "ldap1", "server_type": "ldap", "host": "10.0.0.11"},
    )
    route = candidate("route", "ipv4:0.0.0.0")
    custom_role = candidate(
        "role",
        "restricted",
        payload={"policies": ["corp-acl"]},
    )
    preview = adapter.preview([ldap, route, custom_role])
    assert {item["status"] for item in preview["operations"]} == {"unsupported"}
    assert backend.read_calls == []


def test_open_underlay_and_tunneled_wlan_mapping_differences():
    wlan = candidate(
        "wlan",
        "Guest",
        payload={
            "name": "Guest",
            "essid": "Guest",
            "vlan": 20,
            "aaa_profile": None,
            "security": {"mode": "open", "opmode": "open", "ambiguous": False},
        },
        unsupported_fields={
            "ssid_profile.opmode": "open",
            "virtual_ap.forward_mode": "bridge",
        },
    )
    underlay = new_adapter(FakeBackend()).preview([wlan])
    assert underlay["operations"][0]["operations"][0]["tool_or_endpoint"] == ("build_underlay_ssid")
    assert underlay["operations"][0]["delete_operations"][0]["tool_or_endpoint"] == (
        "delete_underlay_ssid"
    )

    tunneled = {
        **wlan,
        "unsupported_fields": {
            "ssid_profile.opmode": "open",
            "virtual_ap.forward_mode": "tunnel",
        },
    }
    missing_cluster = new_adapter(FakeBackend()).preview([tunneled])
    assert missing_cluster["operations"][0]["status"] == "unsupported"
    with_cluster = new_adapter(FakeBackend(), cluster=True).preview([tunneled])
    assert with_cluster["operations"][0]["operations"][0]["tool_or_endpoint"] == (
        "build_overlay_ssid"
    )
    assert with_cluster["operations"][0]["delete_operations"][0]["tool_or_endpoint"] == (
        "delete_overlay_ssid"
    )


def test_classic_maps_only_verified_open_bridged_full_wlan():
    backend = FakeBackend()
    adapter = classic_adapter(backend)
    wlan = candidate(
        "wlan",
        "Guest",
        payload={
            "name": "Guest",
            "essid": "Guest",
            "vlan": 20,
            "aaa_profile": None,
            "security": security("open"),
        },
        unsupported_fields={
            "ssid_profile.opmode": "open",
            "virtual_ap.forward_mode": "bridge",
        },
    )
    preview = adapter.preview([wlan])
    action = preview["operations"][0]
    operation = action["operations"][0]
    assert operation["method"] == "POST"
    assert operation["tool_or_endpoint"] == ("/configuration/full_wlan/Branch%20Group/Guest")
    assert operation["payload"]["wlan"]["opmode"] == "opensystem"
    assert operation["payload"]["wlan"]["type"] == "guest"
    assert preview["checkpoint_and_rollback"]["automatic_rollback_supported"] is False

    # Preflight/update/read-back/rollback metadata is present for every
    # executable Classic mapping (contract requirement).
    assert action["read_back"]["tool_or_endpoint"] == (
        "/configuration/full_wlan/Branch%20Group/Guest"
    )
    assert action["read_back"]["method"] == "GET"
    assert len(action["rollback"]) == 1
    assert action["rollback"][0]["method"] == "DELETE"
    assert action["rollback"][0]["tool_or_endpoint"] == (
        "/configuration/full_wlan/Branch%20Group/Guest"
    )
    assert action["dry_run_only"] is False

    unsupported = adapter.preview([candidate("vlan", "20")])
    assert unsupported["operations"][0]["status"] == "unsupported"


def test_classic_open_wlan_update_uses_full_body_put_on_conflict():
    existing_wlan = {"name": "Guest", "essid": "Guest"}
    backend = FakeBackend(reads={"central_api_read": existing_wlan})
    adapter = classic_adapter(backend, policy=ConflictPolicy.UPDATE)
    wlan = candidate(
        "wlan",
        "Guest",
        payload={
            "name": "Guest",
            "essid": "Guest",
            "vlan": 20,
            "aaa_profile": None,
            "security": security("open"),
        },
        unsupported_fields={
            "ssid_profile.opmode": "open",
            "virtual_ap.forward_mode": "bridge",
        },
    )
    preview = adapter.preview([wlan])
    action = preview["operations"][0]
    assert action["conflict"] == "update"
    operation = action["operations"][0]
    assert operation["method"] == "PUT"
    assert operation["tool_or_endpoint"] == ("/configuration/full_wlan/Branch%20Group/Guest")
    # PUT is a complete-body replace of the same shape as create, never a
    # partial patch.
    assert operation["payload"]["wlan"]["opmode"] == "opensystem"
    assert operation["payload"]["access_rule"]["name"] == "Guest"


def test_classic_wlan_embeds_vlan_dependency_while_vlan_candidate_stays_unapplied():
    backend = FakeBackend(
        reads={
            "central_api_read_back": {
                "wlan": {
                    "name": "Guest",
                    "essid": "Guest",
                    "opmode": "opensystem",
                    "vlan": "20",
                }
            }
        }
    )
    adapter = classic_adapter(backend)
    vlan = candidate("vlan", "20")
    wlan = candidate(
        "wlan",
        "Guest",
        payload={
            "name": "Guest",
            "essid": "Guest",
            "vlan": 20,
            "aaa_profile": None,
            "security": security("open"),
        },
        dependencies=["vlan:20"],
        apply_order=50,
        unsupported_fields={
            "ssid_profile.opmode": "open",
            "virtual_ap.forward_mode": "bridge",
        },
    )
    result = adapter.execute([wlan, vlan], dry_run=False, confirmation=True)
    assert result["results"][0]["status"] == "unsupported"
    assert result["results"][1]["status"] == "applied"
    assert result["operations"][1]["inline_dependencies"] == ["vlan:20"]


def test_dry_run_invokes_backend_with_dry_run_and_no_confirmation():
    backend = FakeBackend()
    adapter = new_adapter(backend)
    result = adapter.dry_run([candidate("vlan", "20")])
    operation, confirmation = backend.write_calls[0]
    assert operation.arguments["dry_run"] is True
    assert confirmation is False
    assert result["results"][0]["status"] == "dry-run"


def test_execution_requires_all_write_gates():
    vlan = candidate("vlan", "20")
    with pytest.raises(WriteGateError, match="dry_run=False"):
        new_adapter(FakeBackend()).execute([vlan], dry_run=True, confirmation=True)
    with pytest.raises(WriteGateError, match="confirmation"):
        new_adapter(FakeBackend()).execute([vlan], dry_run=False, confirmation=False)
    with pytest.raises(WriteGateError, match="disabled"):
        new_adapter(FakeBackend(), writes=False).execute([vlan], dry_run=False, confirmation=True)


def test_execution_passes_confirmation_and_preserves_partial_failures():
    vlan = candidate("vlan", "20")
    role = candidate(
        "role",
        "employee",
        payload={"policies": ["allowall"]},
        dependencies=["vlan:20"],
        apply_order=30,
    )
    backend = FakeBackend(failures={"create_vlan": RuntimeError("API 503")})
    result = new_adapter(backend).execute([vlan, role], dry_run=False, confirmation=True)
    assert result["results"][0]["status"] == "failed"
    assert result["results"][0]["errors"] == ["create_vlan: API 503"]
    assert result["results"][1]["status"] == "blocked"
    assert all(confirmation is True for _, confirmation in backend.write_calls)
    assert all(call.arguments["dry_run"] is False for call, _ in backend.write_calls)


def test_multi_operation_candidate_preserves_success_before_later_failure():
    role = candidate("role", "employee", payload={"policies": ["allowall"]})
    backend = FakeBackend(failures={"central_api_request": RuntimeError("assignment rejected")})
    result = new_adapter(backend).execute([role], dry_run=False, confirmation=True)
    role_result = result["results"][0]
    assert role_result["status"] == "failed"
    assert len(role_result["results"]) == 1
    assert role_result["results"][0]["operation"]["tool_or_endpoint"] == "create_role"
    assert role_result["errors"] == ["central_api_request: assignment rejected"]


def test_preflight_errors_are_reported_without_writes():
    backend = FakeBackend(reads={"list_roles": RuntimeError("read timeout")})
    adapter = new_adapter(backend)
    role = candidate("role", "employee", payload={"policies": ["allowall"]})
    result = adapter.execute([role], dry_run=False, confirmation=True)
    assert result["results"][0]["status"] == "blocked"
    assert result["results"][0]["errors"] == ["preflight read failed: read timeout"]
    assert backend.write_calls == []


# ---------------------------------------------------------------------------
# LDAP / TACACS auth-servers (item 1: extend RADIUS carefully, add LDAP/
# TACACS only with exact spec fields and transient secret requirements).
# ---------------------------------------------------------------------------


def test_radius_auth_server_has_verified_update_and_delete_operations():
    auth = candidate(
        "auth_server",
        "radius:rad1",
        payload={"name": "rad1", "server_type": "radius", "host": "10.0.0.10"},
        unsupported_fields={"rad_authport": 1812, "rad_acctport": 1813},
    )
    preview = new_adapter(
        FakeBackend(),
        secrets={"auth_server:radius:rad1": {"shared_secret": "s3cret"}},
    ).preview([auth])
    entry = preview["operations"][0]
    # Finding #1 (fail-closed): the auth-server object itself is verified,
    # but the SHARED config-assignment profile-type "auth-servers" is not
    # independently evidenced locally, so this candidate stays "blocked".
    assert entry["status"] == "blocked"
    assert entry["operations"][0]["tool_or_endpoint"] == "create_auth_server"
    assert len(entry["operations"]) == 1
    assert entry["update_operations"][0]["method"] == "PATCH"
    assert entry["update_operations"][0]["tool_or_endpoint"] == (
        "/network-config/v1alpha1/auth-servers/rad1"
    )
    assert len(entry["update_operations"]) == 1
    assert "s3cret" not in str(entry["update_operations"])
    assert entry["delete_operations"][0]["tool_or_endpoint"] == "delete_auth_server"
    assert len(entry["delete_operations"]) == 1
    assert entry["rollback_supported"] is False
    assert any("auth-servers" in blocker for blocker in entry["blockers"])


def test_ldap_auth_server_maps_exact_fields_and_requires_admin_password_secret():
    ldap = candidate(
        "auth_server",
        "ldap:ldap1",
        payload={"name": "ldap1", "server_type": "ldap", "host": "10.0.0.11"},
        unsupported_fields={
            "ldap_admindn": "cn=admin,dc=example,dc=com",
            "ldap_keyattribute": "uid",
        },
    )
    missing_secret = new_adapter(FakeBackend()).preview([ldap])
    assert missing_secret["operations"][0]["status"] == "unsupported"
    assert "admin_password" in missing_secret["operations"][0]["unsupported_warnings"][0]

    preview = new_adapter(
        FakeBackend(),
        secrets={"auth_server:ldap:ldap1": {"admin_password": "bindpw"}},
    ).preview([ldap])
    entry = preview["operations"][0]
    # Finding #1 (fail-closed): the object mapping is verified, but the
    # SHARED config-assignment profile-type "auth-servers" is not
    # independently evidenced locally, so this stays "blocked".
    assert entry["status"] == "blocked"
    operation = entry["operations"][0]
    assert operation["method"] == "POST"
    assert operation["tool_or_endpoint"] == "/network-config/v1alpha1/auth-servers/ldap1"
    assert operation["payload"]["type"] == "LDAP"
    assert operation["payload"]["admin-dn"] == "cn=admin,dc=example,dc=com"
    assert operation["payload"]["key-attribute"] == "uid"
    assert operation["payload"]["admin-password"] == "***"
    assert "bindpw" not in str(preview)
    assert entry["update_operations"][0]["method"] == "PATCH"
    assert entry["delete_operations"][0]["tool_or_endpoint"] == "delete_auth_server"
    assert len(entry["delete_operations"]) == 1


def test_ldap_auth_server_rejected_on_switch_persona():
    ldap = candidate(
        "auth_server",
        "ldap:ldap1",
        payload={"name": "ldap1", "server_type": "ldap", "host": "10.0.0.11"},
    )
    preview = new_adapter(
        FakeBackend(),
        persona="ACCESS_SWITCH",
        secrets={"auth_server:ldap:ldap1": {"admin_password": "bindpw"}},
    ).preview([ldap])
    assert preview["operations"][0]["status"] == "unsupported"
    assert "device-function" in preview["operations"][0]["unsupported_warnings"][0]


def test_tacacs_auth_server_maps_exact_fields_on_every_persona_family():
    tacacs = candidate(
        "auth_server",
        "tacacs:tac1",
        payload={"name": "tac1", "server_type": "tacacs", "host": "10.0.0.12"},
        unsupported_fields={"tacacs_tcpport": 49, "tacacs_timeout": 5},
    )
    for persona in ("CAMPUS_AP", "MOBILITY_GW", "ACCESS_SWITCH"):
        preview = new_adapter(
            FakeBackend(),
            persona=persona,
            secrets={"auth_server:tacacs:tac1": {"shared_secret": "tacsecret"}},
        ).preview([tacacs])
        entry = preview["operations"][0]
        # Finding #1 (fail-closed): the object mapping is verified, but the
        # SHARED config-assignment profile-type "auth-servers" is not
        # independently evidenced locally, so this stays "blocked" on every
        # persona.
        assert entry["status"] == "blocked", persona
        payload = entry["operations"][0]["payload"]
        assert payload["type"] == "TACACS"
        assert payload["tcp-port"] == 49
        assert payload["timeout"] == 5
        assert payload["shared-secret-config"] == "***"
        assert "tacsecret" not in str(preview)


def test_radsec_and_other_auth_server_types_stay_unsupported():
    for server_type in ("radsec", "windows", "xmlapi", "local", "rfc3576"):
        auth = candidate(
            "auth_server",
            f"{server_type}:s1",
            payload={"name": "s1", "server_type": server_type, "host": "10.0.0.13"},
        )
        preview = new_adapter(FakeBackend()).preview([auth])
        assert preview["operations"][0]["status"] == "unsupported"


# ---------------------------------------------------------------------------
# Server groups (item 2): ordered `servers` entries with positions,
# type/persona validation, dependency on typed auth-server candidates.
# ---------------------------------------------------------------------------


def test_server_group_builds_ordered_servers_array_from_dependencies():
    group = candidate(
        "server_group",
        "corp-sg",
        payload={
            "name": "corp-sg",
            "auth_servers": ["rad2", "rad1"],
            "auth_server_entries": [
                {"name": "rad2", "position": 2},
                {"name": "rad1", "position": 1},
            ],
            "fail_through": True,
            "load_balance": False,
        },
        dependencies=["auth_server:radius:rad1", "auth_server:radius:rad2"],
    )
    rad1 = candidate(
        "auth_server",
        "radius:rad1",
        payload={"name": "rad1", "server_type": "radius", "host": "10.0.0.10"},
    )
    rad2 = candidate(
        "auth_server",
        "radius:rad2",
        payload={"name": "rad2", "server_type": "radius", "host": "10.0.0.11"},
    )
    preview = new_adapter(
        FakeBackend(),
        secrets={
            "auth_server:radius:rad1": {"shared_secret": "s1"},
            "auth_server:radius:rad2": {"shared_secret": "s2"},
        },
    ).preview([group, rad1, rad2], selected={"server_group:corp-sg"})
    entry = next(
        item for item in preview["operations"] if item["candidate"] == "server_group:corp-sg"
    )
    # Finding #1 (fail-closed): the server-groups object contract (ordered
    # `servers` array with positions) is verified below, but the SHARED
    # config-assignment profile-type "server-groups" is not independently
    # evidenced locally (unlike "roles", no manifest worked example names
    # it) -- this candidate stays "blocked", never a complete migration.
    assert entry["status"] == "blocked"
    assert len(entry["operations"]) == 1
    create_op = entry["operations"][0]
    assert create_op["tool_or_endpoint"] == "create_server_group"
    assert create_op["arguments"]["server_names"] == ["rad1", "rad2"]
    servers = create_op["payload"]["servers"]
    assert servers == [
        {"server-name": "rad1", "position": 1},
        {"server-name": "rad2", "position": 2},
    ]
    assert create_op["payload"]["type"] == "RADIUS"
    assert create_op["payload"]["fail-through"] is True
    assert entry["delete_operations"][0]["tool_or_endpoint"] == "delete_server_group"
    assert len(entry["delete_operations"]) == 1
    assert entry["rollback_supported"] is False
    assert any("server-groups" in blocker for blocker in entry["blockers"])


def test_server_group_rejects_mixed_auth_server_types():
    group = candidate(
        "server_group",
        "mixed-sg",
        payload={
            "name": "mixed-sg",
            "auth_servers": ["rad1", "ldap1"],
            "auth_server_entries": [
                {"name": "rad1", "position": 1},
                {"name": "ldap1", "position": 2},
            ],
        },
        dependencies=["auth_server:radius:rad1", "auth_server:ldap:ldap1"],
    )
    rad1 = candidate(
        "auth_server",
        "radius:rad1",
        payload={"name": "rad1", "server_type": "radius", "host": "10.0.0.10"},
    )
    ldap1 = candidate(
        "auth_server",
        "ldap:ldap1",
        payload={"name": "ldap1", "server_type": "ldap", "host": "10.0.0.11"},
    )
    preview = new_adapter(FakeBackend()).preview(
        [group, rad1, ldap1], selected={"server_group:mixed-sg"}
    )
    entry = next(
        item for item in preview["operations"] if item["candidate"] == "server_group:mixed-sg"
    )
    assert entry["status"] == "unsupported"
    assert "mixes auth-server types" in entry["unsupported_warnings"][0]


def test_server_group_rejects_type_collision_flag_and_unresolved_entries():
    collision_group = candidate(
        "server_group",
        "ambiguous-sg",
        payload={"name": "ambiguous-sg", "auth_server_entries": [{"name": "rad1", "position": 1}]},
        unsupported_fields={"auth_server_type_collisions": {"rad1": ["ldap", "radius"]}},
        dependencies=[],
    )
    preview = new_adapter(FakeBackend()).preview([collision_group])
    assert preview["operations"][0]["status"] == "unsupported"
    assert "ambiguous across server types" in preview["operations"][0]["unsupported_warnings"][0]

    unresolved_entry_group = candidate(
        "server_group",
        "corp-sg2",
        payload={
            "name": "corp-sg2",
            "auth_server_entries": [{"name": "does-not-exist", "position": 1}],
        },
        dependencies=["auth_server:radius:rad1"],
    )
    rad1 = candidate(
        "auth_server",
        "radius:rad1",
        payload={"name": "rad1", "server_type": "radius", "host": "10.0.0.10"},
    )
    preview2 = new_adapter(FakeBackend()).preview(
        [unresolved_entry_group, rad1], selected={"server_group:corp-sg2"}
    )
    entry2 = next(
        item for item in preview2["operations"] if item["candidate"] == "server_group:corp-sg2"
    )
    assert entry2["status"] == "unsupported"
    assert "does not resolve to a verified auth-server dependency" in (
        entry2["unsupported_warnings"][0]
    )


def test_server_group_ldap_type_rejected_on_switch_persona():
    group = candidate(
        "server_group",
        "ldap-sg",
        payload={
            "name": "ldap-sg",
            "auth_server_entries": [{"name": "ldap1", "position": 1}],
        },
        dependencies=["auth_server:ldap:ldap1"],
    )
    ldap1 = candidate(
        "auth_server",
        "ldap:ldap1",
        payload={"name": "ldap1", "server_type": "ldap", "host": "10.0.0.11"},
    )
    preview = new_adapter(FakeBackend(), persona="ACCESS_SWITCH").preview(
        [group, ldap1], selected={"server_group:ldap-sg"}
    )
    entry = next(
        item for item in preview["operations"] if item["candidate"] == "server_group:ldap-sg"
    )
    assert entry["status"] == "unsupported"
    assert "device-function" in entry["unsupported_warnings"][0]


def test_server_group_uses_curated_lifecycle_tools():
    group = candidate(
        "server_group",
        "corp-sg",
        payload={
            "name": "corp-sg",
            "auth_server_entries": [
                {"name": "rad2", "position": 2},
                {"name": "rad1", "position": 1},
            ],
            "fail_through": True,
        },
        dependencies=["auth_server:radius:rad1", "auth_server:radius:rad2"],
    )

    action = new_adapter(
        FakeBackend(), persona="MOBILITY_GW"
    ).candidate_action(group)

    create = action.operations[0]
    assert create.invocation == "tool"
    assert create.name == "create_server_group"
    assert create.arguments["server_names"] == ["rad1", "rad2"]
    assert create.arguments["fail_through"] is True
    assert create.payload["servers"] == [
        {"server-name": "rad1", "position": 1},
        {"server-name": "rad2", "position": 2},
    ]
    assert action.read_operation.name == "get_server_group"
    assert action.delete_operations[0].name == "delete_server_group"


# ---------------------------------------------------------------------------
# Gateway/switch-only dot1x/macauth device profiles (item 3).
# ---------------------------------------------------------------------------


def test_bare_dot1x_and_macauth_profiles_map_on_gateway_switch_only():
    dot1x = candidate("dot1x_auth_profile", "corp-dot1x", payload={"name": "corp-dot1x"})
    macauth = candidate("mac_auth_profile", "corp-mac", payload={"name": "corp-mac"})

    for object_candidate, resource in ((dot1x, "dot1xauth"), (macauth, "macauth")):
        rejected = new_adapter(FakeBackend(), persona="CAMPUS_AP").preview([object_candidate])
        assert rejected["operations"][0]["status"] == "unsupported"
        assert "device-function" in rejected["operations"][0]["unsupported_warnings"][0]

        for persona in ("MOBILITY_GW", "ACCESS_SWITCH"):
            preview = new_adapter(FakeBackend(), persona=persona).preview([object_candidate])
            entry = preview["operations"][0]
            # Finding #1 (fail-closed): the dot1xauth/macauth object
            # contract is verified below, but the SHARED config-assignment
            # profile-type for this resource is not independently
            # evidenced locally, so this candidate stays "blocked".
            assert entry["status"] == "blocked", (resource, persona)
            create_op = entry["operations"][0]
            assert create_op["method"] == "POST"
            assert resource in create_op["tool_or_endpoint"]
            assert create_op["payload"] == {"name": object_candidate["identifier"]}
            assert len(entry["operations"]) == 1
            assert entry["update_operations"][0]["method"] == "PATCH"
            assert len(entry["update_operations"]) == 1
            assert entry["delete_operations"][0]["method"] == "DELETE"
            assert len(entry["delete_operations"]) == 1
            assert entry["rollback_supported"] is False
            assert any(resource in blocker for blocker in entry["blockers"])


def test_blocked_shared_profiles_expose_exact_assignment_reads():
    cases = [
        (
            candidate(
                "auth_server",
                "radius:rad1",
                payload={
                    "name": "rad1",
                    "server_type": "radius",
                    "host": "10.0.0.10",
                },
            ),
            "auth-servers",
            "rad1",
            {"auth_server:radius:rad1": {"shared_secret": "secret"}},
        ),
        (
            candidate(
                "aaa_profile",
                "aaa1",
                payload={"name": "aaa1", "default_user_role": "employee"},
            ),
            "aaa-profile",
            "aaa1",
            {},
        ),
        (
            candidate(
                "dot1x_auth_profile",
                "dot1x1",
                payload={"name": "dot1x1"},
            ),
            "dot1xauth",
            "dot1x1",
            {},
        ),
        (
            candidate(
                "mac_auth_profile",
                "mac1",
                payload={"name": "mac1"},
            ),
            "macauth",
            "mac1",
            {},
        ),
        (
            candidate(
                "server_group",
                "sg1",
                payload={
                    "name": "sg1",
                    "auth_server_entries": [{"name": "rad1", "position": 1}],
                },
                dependencies=["auth_server:radius:rad1"],
            ),
            "server-groups",
            "sg1",
            {},
        ),
    ]

    for object_candidate, profile_type, profile_instance, secrets in cases:
        action = new_adapter(
            FakeBackend(),
            persona="MOBILITY_GW",
            secrets=secrets or None,
        ).candidate_action(object_candidate)

        assert action.assignment_read_operation is not None
        assert action.assignment_read_operation.name == "list_config_assignments"
        assert action.assignment_read_operation.arguments == {
            "scope_id": "100",
            "device_function": "MOBILITY_GW",
            "profile_type": profile_type,
            "limit": VERIFICATION_READ_PAGE_SIZE,
            "offset": 0,
        }
        assert action.assignment_expected == {
            "scope-id": "100",
            "device-function": "MOBILITY_GW",
            "profile-type": profile_type,
            "profile-instance": profile_instance,
        }
        assert action.blockers


def test_gateway_policy_maps_verified_any_to_any_subset_dry_run_only():
    policy = candidate(
        "policy",
        "hpe-mcp-lab-policy",
        payload={
            "name": "hpe-mcp-lab-policy",
            "rule_count": 2,
            "rules": [
                {
                    "address_family": "ipv4",
                    "source": "any",
                    "destination": "any",
                    "service": "any",
                    "action": "permit",
                    "log": False,
                },
                {
                    "address_family": "ipv4",
                    "source": "any",
                    "destination": "any",
                    "service": None,
                    "action": "deny",
                    "log": None,
                },
            ],
        },
    )

    preview = new_adapter(FakeBackend(), persona="MOBILITY_GW").preview([policy])
    entry = preview["operations"][0]

    assert entry["status"] == "ready"
    assert entry["dry_run_only"] is True
    assert entry["operations"][0]["tool_or_endpoint"] == "create_gw_policy"
    rules = entry["operations"][0]["arguments"]["rules"]
    assert [rule["position"] for rule in rules] == [1, 2]
    assert [rule["action"]["type"] for rule in rules] == [
        "ACTION_ALLOW",
        "ACTION_DENY",
    ]
    assert all(rule["condition"]["source"] == {"type": "ADDRESS_ANY"} for rule in rules)
    assert entry["delete_operations"][0]["tool_or_endpoint"] == "delete_gw_policy"


@pytest.mark.parametrize("persona", ["CAMPUS_AP", "ACCESS_SWITCH"])
def test_gateway_policy_rejects_non_gateway_personas(persona):
    policy = candidate(
        "policy",
        "hpe-mcp-lab-policy",
        payload={
            "rule_count": 1,
            "rules": [
                {
                    "address_family": "ipv4",
                    "source": "any",
                    "destination": "any",
                    "service": "any",
                    "action": "permit",
                    "log": False,
                }
            ],
        },
    )

    preview = new_adapter(FakeBackend(), persona=persona).preview([policy])

    assert preview["operations"][0]["status"] == "unsupported"
    assert "device-function" in preview["operations"][0]["unsupported_warnings"][0]


@pytest.mark.parametrize(
    "rule_update",
    [
        {"service": "https"},
        {"source": "employee"},
        {"log": True},
        {"address_family": "ipv6"},
    ],
)
def test_gateway_policy_rejects_unverified_rule_semantics(rule_update):
    rule = {
        "address_family": "ipv4",
        "source": "any",
        "destination": "any",
        "service": "any",
        "action": "permit",
        "log": False,
    }
    rule.update(rule_update)
    policy = candidate(
        "policy",
        "hpe-mcp-lab-policy",
        payload={"rule_count": 1, "rules": [rule]},
    )

    preview = new_adapter(FakeBackend(), persona="MOBILITY_GW").preview([policy])

    assert preview["operations"][0]["status"] == "unsupported"


def test_rich_dot1x_and_macauth_profiles_are_rejected_not_guessed():
    dot1x = candidate(
        "dot1x_auth_profile",
        "corp-dot1x",
        payload={"name": "corp-dot1x"},
        unsupported_fields={"use_session_key": True, "reauthentication": True},
    )
    preview = new_adapter(FakeBackend(), persona="MOBILITY_GW").preview([dot1x])
    assert preview["operations"][0]["status"] == "unsupported"
    assert "no verified 1:1 New Central field mapping" in (
        preview["operations"][0]["unsupported_warnings"][0]
    )


def test_aaa_profile_and_device_profiles_never_apply_to_ap_wlan_personas():
    # Cross-check: every Gateway/Switch-only device concept must reject the
    # AP persona family consistently.
    for object_type, payload in (
        ("aaa_profile", {"name": "p1", "default_user_role": "emp"}),
        ("dot1x_auth_profile", {"name": "d1"}),
        ("mac_auth_profile", {"name": "m1"}),
    ):
        cand = candidate(object_type, payload["name"], payload=payload)
        preview = new_adapter(FakeBackend(), persona="CAMPUS_AP").preview([cand])
        assert preview["operations"][0]["status"] == "unsupported"


# ---------------------------------------------------------------------------
# Secured AP WLANs using normalized `payload.security` (item 5).
# ---------------------------------------------------------------------------


def _wlan_candidate(
    mode, *, forward_mode="bridge", opmode="wpa2-aes", ambiguous=False, **extra_security
):
    security = {"mode": mode, "opmode": opmode, "ambiguous": ambiguous, **extra_security}
    return candidate(
        "wlan",
        "Corp",
        payload={
            "name": "Corp",
            "essid": "Corp",
            "vlan": 20,
            "aaa_profile": None,
            "security": security,
        },
        unsupported_fields={
            "ssid_profile.opmode": opmode,
            "virtual_ap.forward_mode": forward_mode,
        },
    )


def test_wpa2_personal_wlan_requires_transient_passphrase_and_uses_correct_enum():
    wlan = _wlan_candidate("wpa2_personal")
    missing = new_adapter(FakeBackend()).preview([wlan])
    assert missing["operations"][0]["status"] == "unsupported"
    assert "wpa_passphrase" in missing["operations"][0]["unsupported_warnings"][0]

    preview = new_adapter(
        FakeBackend(),
        secrets={"wlan:Corp": {"wpa_passphrase": "SuperSecretPass1"}},
    ).preview([wlan])
    entry = preview["operations"][0]
    assert entry["status"] == "ready"
    operation = entry["operations"][0]
    assert operation["tool_or_endpoint"] == "build_underlay_ssid"
    assert operation["arguments"]["opmode"] == "WPA2_PERSONAL"
    assert operation["arguments"]["passphrase"] == "***"
    assert "SuperSecretPass1" not in str(preview)


def test_wpa3_sae_and_enhanced_open_and_open_wlan_ready_mappings():
    sae = _wlan_candidate("wpa3_sae", opmode="wpa3-sae")
    preview = new_adapter(
        FakeBackend(),
        secrets={"wlan:Corp": {"wpa_passphrase": "AnotherPass99"}},
    ).preview([sae])
    op = preview["operations"][0]["operations"][0]
    assert op["arguments"]["opmode"] == "WPA3_SAE"
    assert op["arguments"]["passphrase"] == "***"

    enhanced_open = _wlan_candidate("enhanced_open", opmode="enhanced-open")
    preview2 = new_adapter(FakeBackend()).preview([enhanced_open])
    op2 = preview2["operations"][0]["operations"][0]
    assert op2["arguments"]["opmode"] == "ENHANCED_OPEN"
    assert op2["arguments"]["passphrase"] is None
    assert preview2["operations"][0]["status"] == "ready"

    open_wlan = _wlan_candidate("open", opmode="open")
    preview3 = new_adapter(FakeBackend()).preview([open_wlan])
    op3 = preview3["operations"][0]["operations"][0]
    assert op3["arguments"]["opmode"] == "OPEN"
    assert preview3["operations"][0]["status"] == "ready"


def test_wpa3_transition_personal_is_blocked_not_unsupported():
    wlan = _wlan_candidate("wpa3_transition_personal", opmode="wpa3-sae-transition")
    preview = new_adapter(FakeBackend()).preview([wlan])
    entry = preview["operations"][0]
    assert entry["status"] == "blocked"
    assert entry["operations"] == []
    assert entry["unsupported_warnings"] == []
    assert "wpa3-transition-mode-enable" in entry["blockers"][0]


@pytest.mark.parametrize("mode", ["mac_auth_only", "mac_auth_psk", "enterprise_dot1x"])
def test_mac_auth_and_enterprise_dot1x_wlans_remain_unsupported(mode):
    wlan = _wlan_candidate(mode, opmode="mac-auth" if "mac" in mode else "wpa2-aes-dot1x")
    preview = new_adapter(FakeBackend()).preview([wlan])
    assert preview["operations"][0]["status"] == "unsupported"
    assert "MAC-auth and enterprise 802.1X" in preview["operations"][0]["unsupported_warnings"][0]


def test_unknown_or_ambiguous_wlan_security_mode_stays_unsupported():
    wlan = _wlan_candidate("unknown", opmode="wpa-tkip", ambiguous=True)
    preview = new_adapter(FakeBackend()).preview([wlan])
    assert preview["operations"][0]["status"] == "unsupported"
    assert "unverified" in preview["operations"][0]["unsupported_warnings"][0]


def test_role_only_aaa_profile_does_not_block_wpa2_personal_classification():
    """A role-only aaa_profile (no dot1x/MAC-auth profile) attached to a
    PSK WLAN must not block the mapping -- `_wlan_security_intent` has
    already verified it carries no authentication intent of its own
    (docs/aos8-migration-contract-matrix.md item 8 / role-only fixture)."""
    wlan = candidate(
        "wlan",
        "Corp",
        payload={
            "name": "Corp",
            "essid": "Corp",
            "vlan": 20,
            "aaa_profile": "role-only-aaa",
            "security": {"mode": "wpa2_personal", "opmode": "wpa2-aes", "ambiguous": False},
        },
        unsupported_fields={
            "ssid_profile.opmode": "wpa2-aes",
            "virtual_ap.forward_mode": "bridge",
        },
    )
    preview = new_adapter(
        FakeBackend(),
        secrets={"wlan:Corp": {"wpa_passphrase": "RolePass123"}},
    ).preview([wlan])
    assert preview["operations"][0]["status"] == "ready"


def test_wlan_missing_security_intent_summary_is_unsupported():
    wlan = candidate(
        "wlan",
        "Corp",
        payload={"name": "Corp", "essid": "Corp", "vlan": 20, "aaa_profile": None},
        unsupported_fields={
            "ssid_profile.opmode": "open",
            "virtual_ap.forward_mode": "bridge",
        },
    )
    preview = new_adapter(FakeBackend()).preview([wlan])
    assert preview["operations"][0]["status"] == "unsupported"
    assert "no `payload.security`" in preview["operations"][0]["unsupported_warnings"][0]


# ---------------------------------------------------------------------------
# Verification metadata / deterministic ordering (item 9) across new object
# types.
# ---------------------------------------------------------------------------


def test_deterministic_ordering_across_new_object_types():
    auth = candidate(
        "auth_server",
        "radius:rad1",
        payload={"name": "rad1", "server_type": "radius", "host": "10.0.0.10"},
        apply_order=10,
    )
    group = candidate(
        "server_group",
        "corp-sg",
        payload={
            "name": "corp-sg",
            "auth_server_entries": [{"name": "rad1", "position": 1}],
        },
        dependencies=["auth_server:radius:rad1"],
        apply_order=20,
    )
    dot1x = candidate(
        "dot1x_auth_profile",
        "corp-dot1x",
        payload={"name": "corp-dot1x"},
        apply_order=20,
    )
    aaa = candidate(
        "aaa_profile",
        "corp-aaa",
        payload={"name": "corp-aaa", "default_user_role": "employee"},
        apply_order=40,
    )
    preview = new_adapter(
        FakeBackend(),
        persona="MOBILITY_GW",
        secrets={"auth_server:radius:rad1": {"shared_secret": "s3cret"}},
    ).preview([aaa, dot1x, group, auth])
    # Round-based topological order: `auth` has no dependencies so it is
    # ready in round 1 alongside `dot1x`/`aaa` (neither of which declares a
    # dependency on `group` in this fixture); within a round candidates are
    # ordered by (apply_order, key). `group` depends on `auth` and is only
    # ready once `auth` has been removed from `remaining`, in round 2 --
    # this is what makes the ordering deterministic and dependency-safe,
    # not a strict linear apply_order-family ranking.
    assert [item["candidate"] for item in preview["operations"]] == [
        "auth_server:radius:rad1",
        "dot1x_auth_profile:corp-dot1x",
        "aaa_profile:corp-aaa",
        "server_group:corp-sg",
    ]
    group_index = [item["candidate"] for item in preview["operations"]].index(
        "server_group:corp-sg"
    )
    auth_index = [item["candidate"] for item in preview["operations"]].index(
        "auth_server:radius:rad1"
    )
    assert auth_index < group_index
    for entry in preview["operations"]:
        assert entry["read_operation"] is not None
        assert entry["update_operations"] is not None
        assert entry["delete_operations"] is not None


# ---------------------------------------------------------------------------
# Review-fix regression tests.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,opmode",
    [
        ("open", "open"),
        ("wpa2_personal", "wpa2-aes"),
        ("wpa3_sae", "wpa3-sae"),
        ("enhanced_open", "enhanced-open"),
    ],
)
def test_pure_security_modes_explicitly_set_wpa3_transition_false(mode, opmode):
    """Finding #3: OPEN/WPA2_PERSONAL/WPA3_SAE/ENHANCED_OPEN must never
    inherit a transition=True default; the mapper must set an explicit
    `False` for every currently-supported pure mode."""
    secrets = (
        {"wlan:Corp": {"wpa_passphrase": "SuperSecretPass1"}}
        if mode in {"wpa2_personal", "wpa3_sae"}
        else None
    )
    wlan = _wlan_candidate(mode, opmode=opmode)
    preview = new_adapter(FakeBackend(), secrets=secrets).preview([wlan])
    entry = preview["operations"][0]
    assert entry["status"] == "ready"
    operation = entry["operations"][0]
    assert operation["arguments"]["wpa3_transition"] is False


def test_wpa3_transition_candidate_remains_blocked_never_ready_with_transition_true():
    """Transition candidates stay blocked; they must never surface a
    `wpa3_transition: True` optimistic write."""
    wlan = _wlan_candidate("wpa3_transition_personal", opmode="wpa3-sae-transition")
    preview = new_adapter(FakeBackend()).preview([wlan])
    entry = preview["operations"][0]
    assert entry["status"] == "blocked"
    assert entry["operations"] == []


def test_wlan_candidate_rejected_on_gateway_and_switch_personas():
    """Finding #4: only AP-family personas may map WLAN candidates; Gateway
    and Switch target contexts must return unsupported, never ready."""
    wlan = _wlan_candidate("wpa2_personal")
    for persona in ("MOBILITY_GW", "ACCESS_SWITCH"):
        preview = new_adapter(
            FakeBackend(),
            persona=persona,
            secrets={"wlan:Corp": {"wpa_passphrase": "SuperSecretPass1"}},
        ).preview([wlan])
        entry = preview["operations"][0]
        assert entry["status"] == "unsupported", persona
        assert "device-function" in entry["unsupported_warnings"][0]


def test_wlan_candidate_still_ready_on_ap_family_personas():
    wlan = _wlan_candidate("wpa2_personal")
    for persona in ("CAMPUS_AP", "MICROBRANCH_AP"):
        preview = new_adapter(
            FakeBackend(),
            persona=persona,
            secrets={"wlan:Corp": {"wpa_passphrase": "SuperSecretPass1"}},
        ).preview([wlan])
        entry = preview["operations"][0]
        assert entry["status"] == "ready", persona


def test_server_group_radsec_dependency_is_unsupported_not_a_crash():
    """Finding #5: an unsupported auth-server type dependency (e.g. radsec)
    must return a precise unsupported action, never raise KeyError."""
    group = candidate(
        "server_group",
        "radsec-sg",
        payload={
            "name": "radsec-sg",
            "auth_server_entries": [{"name": "radsec1", "position": 1}],
        },
        dependencies=["auth_server:radsec:radsec1"],
    )
    radsec = candidate(
        "auth_server",
        "radsec:radsec1",
        payload={"name": "radsec1", "server_type": "radsec", "host": "10.0.0.20"},
    )
    # Must not raise -- this is the precise regression for the KeyError risk.
    preview = new_adapter(FakeBackend()).preview(
        [group, radsec], selected={"server_group:radsec-sg"}
    )
    entry = next(
        item for item in preview["operations"] if item["candidate"] == "server_group:radsec-sg"
    )
    assert entry["status"] == "unsupported"
    assert "radsec" in entry["unsupported_warnings"][0]
    assert "no verified New Central server-groups mapping" in entry["unsupported_warnings"][0]


def test_unverified_shared_profile_assignment_types_stay_blocked_not_ready():
    """Finding #1 (fail-closed, final pass): auth-servers, aaa-profile,
    dot1xauth, macauth, and server-groups all share the same SHARED
    config-assignment problem -- their `profile-type` literal is only an
    endpoint-path-segment convention, never independently evidenced
    locally. Every one of them must return a "blocked" CandidateAction
    (library object contract verified, but not executable as a complete
    migration) -- never "ready", never carrying an assignment operation,
    and never claiming `rollback_supported`."""
    cases = [
        (
            candidate(
                "auth_server",
                "radius:rad1",
                payload={"name": "rad1", "server_type": "radius", "host": "10.0.0.10"},
            ),
            "CAMPUS_AP",
            "auth-servers",
            {"auth_server:radius:rad1": {"shared_secret": "s3cret"}},
        ),
        (
            candidate(
                "aaa_profile",
                "corp-aaa",
                payload={"name": "corp-aaa", "default_user_role": "employee"},
            ),
            "MOBILITY_GW",
            "aaa-profile",
            None,
        ),
        (
            candidate("dot1x_auth_profile", "corp-dot1x", payload={"name": "corp-dot1x"}),
            "ACCESS_SWITCH",
            "dot1xauth",
            None,
        ),
        (
            candidate("mac_auth_profile", "corp-mac", payload={"name": "corp-mac"}),
            "ACCESS_SWITCH",
            "macauth",
            None,
        ),
    ]
    for object_candidate, persona, profile_type, secrets in cases:
        preview = new_adapter(FakeBackend(), persona=persona, secrets=secrets).preview(
            [object_candidate]
        )
        entry = preview["operations"][0]
        assert entry["status"] == "blocked", profile_type
        assert entry["rollback_supported"] is False, profile_type
        assert len(entry["operations"]) == 1, profile_type
        assert len(entry["update_operations"]) == 1, profile_type
        assert len(entry["delete_operations"]) == 1, profile_type
        assert not any(
            "config-assignment" in str(op.get("payload") or {})
            for op in entry["operations"] + entry["update_operations"] + entry["delete_operations"]
        ), profile_type
        assert any(profile_type in blocker for blocker in entry["blockers"]), profile_type
        assert any(
            "config-assignment" in blocker or "assignment" in blocker
            for blocker in entry["blockers"]
        ), profile_type
        # Never executed: dry_run()/execute() must not attempt a write for a
        # blocked candidate.
        backend = FakeBackend()
        adapter = new_adapter(backend, persona=persona, secrets=secrets)
        result = adapter.execute([object_candidate], dry_run=False, confirmation=True)
        assert result["results"][0]["status"] == "blocked", profile_type
        assert backend.write_calls == [], profile_type


def test_roles_assignment_stays_ready_evidenced_by_manifest_worked_example():
    """"roles" is the one SHARED profile-type whose literal is
    independently evidenced (not just endpoint-path convention): the
    generated New Central manifest's own `profile-type` parameter
    description gives it as the worked example (src/hpe_networking_mcp/mcp_servers/openapi_gen/
    manifests/central.json). It must remain "ready" with an executable
    config-assignment operation, unlike the unverified profile types above."""
    role = candidate("role", "employee", payload={"policies": ["allowall"]})
    preview = new_adapter(FakeBackend()).preview([role])
    entry = preview["operations"][0]
    assert entry["status"] == "ready"
    assignment_op = entry["operations"][1]
    assert assignment_op["tool_or_endpoint"] == "/network-config/v1alpha1/config-assignments"
    assert assignment_op["payload"]["config-assignment"][0]["profile-type"] == "roles"
    assert entry["update_operations"][1]["payload"]["config-assignment"][0][
        "profile-type"
    ] == "roles"
    assert entry["delete_operations"][0]["tool_or_endpoint"] == "delete_config_assignment"
    assert entry["delete_operations"][0]["arguments"]["profile_type"] == "roles"
    assert len(entry["delete_operations"]) == 2


# --------------------------------------------------------------------------
# Classic Central: context validation (group/GUID/serial, never a New
# Central scope_id)
# --------------------------------------------------------------------------


def test_classic_context_accepts_numeric_group_name_guid_or_serial():
    # Regression: a purely numeric Classic group name (e.g. a site number
    # used as the group name) is a legitimate, explicitly-declared operator
    # value and must be accepted -- spelling/format heuristics are not a
    # substitute for a dedicated Classic-only target resolver (which never
    # performs a New Central `/scopes` lookup; see
    # hpe_networking_mcp.mcp_servers.aos8._aos8_migration_classic_target_resolver).
    for scope_name in (
        "Branch Group",
        "550e8400-e29b-41d4-a716-446655440000",
        "CN12345678",
        "12345",
    ):
        adapter = classic_adapter(FakeBackend(), scope_name=scope_name)
        assert adapter.context.scope_name == scope_name


def test_classic_context_rejects_whitespace_only_scope_name():
    # The base adapter's own non-empty check only catches a falsy (empty)
    # string; a resolver returning a whitespace-only value would slip past
    # it, so ClassicCentralAdapter's own `.strip()`-based check is what
    # actually catches this case.
    def whitespace_resolver(context):
        return "classic-id", "   "

    with pytest.raises(ContextValidationError, match="explicit Classic group"):
        ClassicCentralAdapter(
            TargetContext(
                target_type=TargetType.CLASSIC_CENTRAL,
                scope_id="classic-id",
                scope_name="ignored-by-fake-resolver",
                persona="CAMPUS_AP",
            ),
            scope_resolver=whitespace_resolver,
            persona_validator=validate_persona,
            read_invoker=FakeBackend().read,
            write_invoker=FakeBackend().write,
            writes_enabled=lambda target: True,
        )


# --------------------------------------------------------------------------
# Classic Central: full_wlan account/group unavailability is never confused
# with "this specific item is absent"
# --------------------------------------------------------------------------


def test_classic_detects_full_wlan_unavailable_and_reports_unsupported_not_absent():
    backend = FakeBackend(
        reads={
            "central_api_read": {
                "error": "full_wlan API is not supported for this account"
            }
        }
    )
    adapter = classic_adapter(backend)
    wlan = candidate(
        "wlan",
        "Guest",
        payload={
            "name": "Guest",
            "essid": "Guest",
            "vlan": 20,
            "aaa_profile": None,
            "security": security("open"),
        },
        unsupported_fields={
            "ssid_profile.opmode": "open",
            "virtual_ap.forward_mode": "bridge",
        },
    )
    preview = adapter.preview([wlan])
    action = preview["operations"][0]
    assert action["status"] == "unsupported"
    assert action["conflict"] == "not-checked"
    assert "unavailable" in action["unsupported_warnings"][0].lower()


# --------------------------------------------------------------------------
# Classic Central: WPA3-Personal (official-sample-backed)
# --------------------------------------------------------------------------


def _wpa3_personal_wlan(name="Secure", vlan=30, **security_overrides):
    security_overrides.setdefault("passphrase_present", True)
    sec = security("wpa3_sae", opmode="wpa3-sae-aes", **security_overrides)
    return candidate(
        "wlan",
        name,
        payload={
            "name": name,
            "essid": name,
            "vlan": vlan,
            "aaa_profile": None,
            "security": sec,
        },
        unsupported_fields={
            "ssid_profile.opmode": "wpa3-sae-aes",
            "virtual_ap.forward_mode": "bridge",
            "ssid_profile.wpa_passphrase": "<redacted:present>",
        },
    )


def test_classic_wpa3_personal_requires_caller_secret_and_masks_preview():
    wlan = _wpa3_personal_wlan()

    missing = classic_adapter(FakeBackend()).preview([wlan])
    assert missing["operations"][0]["status"] == "unsupported"
    assert "wpa_passphrase" in missing["operations"][0]["unsupported_warnings"][0]

    valid_preview = classic_adapter(
        FakeBackend(), secrets={"wlan:Secure": {"wpa_passphrase": "actual-secret"}}
    ).preview([wlan])
    action = valid_preview["operations"][0]
    assert action["status"] == "ready"
    assert action["dry_run_only"] is False
    operation = action["operations"][0]
    assert operation["method"] == "POST"
    assert operation["payload"]["wlan"]["opmode"] == "wpa3-sae-aes"
    assert operation["payload"]["wlan"]["opmode_transition_disable"] is True
    assert operation["payload"]["wlan"]["wpa_passphrase"] == "***"
    assert "data" not in operation["arguments"]
    assert "actual-secret" not in str(valid_preview)
    assert action["read_back"]["method"] == "GET"
    assert action["rollback"][0]["method"] == "DELETE"


def test_classic_wpa3_personal_requires_transition_disabled():
    wlan = _wpa3_personal_wlan(name="Mixed", wpa3_transition=True)
    result = classic_adapter(
        FakeBackend(), secrets={"wlan:Mixed": {"wpa_passphrase": "actual-secret"}}
    ).preview([wlan])
    action = result["operations"][0]
    assert action["status"] == "unsupported"
    assert "transition" in action["unsupported_warnings"][0].lower()


def test_classic_wpa3_personal_requires_passphrase_presence_signal():
    wlan = _wpa3_personal_wlan(name="NoPass", passphrase_present=False)
    result = classic_adapter(
        FakeBackend(), secrets={"wlan:NoPass": {"wpa_passphrase": "actual-secret"}}
    ).preview([wlan])
    action = result["operations"][0]
    assert action["status"] == "unsupported"
    assert "wpa_passphrase" in action["unsupported_warnings"][0]


def test_classic_wpa3_personal_read_back_rejects_transition_enabled_false_success():
    # Regression: WPA3-Personal is only verified with transition mode
    # disabled. A write that "succeeds" but whose read-back reports
    # `opmode_transition_disable: False` (transition still enabled, or the
    # target silently left it at its own default) must never be marked
    # "applied" -- the read-back expectation on this field is what catches
    # that false-success case.
    wlan = _wpa3_personal_wlan(name="Secure", vlan=30)
    backend = FakeBackend(
        reads={
            "central_api_read_back": {
                "wlan": {
                    "name": "Secure",
                    "essid": "Secure",
                    "opmode": "wpa3-sae-aes",
                    "opmode_transition_disable": False,
                    "vlan": "30",
                }
            }
        }
    )
    adapter = classic_adapter(
        backend, secrets={"wlan:Secure": {"wpa_passphrase": "actual-secret"}}
    )
    result = adapter.execute([wlan], dry_run=False, confirmation=True)
    outcome = result["results"][0]
    assert outcome["status"] == "failed"
    assert any(
        "opmode_transition_disable=True" in error and "not confirmed" in error
        for error in outcome["errors"]
    )


def test_classic_wpa3_personal_read_back_confirms_transition_disabled_true_marks_applied():
    wlan = _wpa3_personal_wlan(name="Secure", vlan=30)
    backend = FakeBackend(
        reads={
            "central_api_read_back": {
                "wlan": {
                    "name": "Secure",
                    "essid": "Secure",
                    "opmode": "wpa3-sae-aes",
                    "opmode_transition_disable": True,
                    "vlan": "30",
                }
            }
        }
    )
    adapter = classic_adapter(
        backend, secrets={"wlan:Secure": {"wpa_passphrase": "actual-secret"}}
    )
    result = adapter.execute([wlan], dry_run=False, confirmation=True)
    outcome = result["results"][0]
    assert outcome["status"] == "applied"


# --------------------------------------------------------------------------
# Classic Central: WPA3-Enterprise (conditional/dry-run-only, requires an
# explicit already-existing auth-server reference)
# --------------------------------------------------------------------------


def _wpa3_enterprise_wlan(name="Enterprise-Test", vlan=40):
    sec = security(
        "enterprise_dot1x",
        opmode="wpa3-aes-ccm-128",
        dot1x_auth_profile="corp-dot1x",
    )
    return candidate(
        "wlan",
        name,
        payload={
            "name": name,
            "essid": name,
            "vlan": vlan,
            "aaa_profile": "corp-aaa",
            "security": sec,
        },
        unsupported_fields={
            "ssid_profile.opmode": "wpa3-aes-ccm-128",
            "virtual_ap.forward_mode": "bridge",
        },
    )


def test_classic_wpa3_enterprise_requires_explicit_auth_server_reference():
    wlan = _wpa3_enterprise_wlan()
    missing = classic_adapter(FakeBackend()).preview([wlan])
    action = missing["operations"][0]
    assert action["status"] == "unsupported"
    assert "auth_server1" in action["unsupported_warnings"][0]
    assert "never auto-provisioned" in action["unsupported_warnings"][0]


def test_classic_wpa3_enterprise_is_ready_for_preview_but_dry_run_only():
    wlan = _wpa3_enterprise_wlan()
    backend = FakeBackend()
    adapter = classic_adapter(
        backend,
        external_object_references={"wlan:Enterprise-Test": {"auth_server1": "InternalServer"}},
    )
    preview = adapter.preview([wlan])
    action = preview["operations"][0]
    assert action["status"] == "ready"
    assert action["dry_run_only"] is True
    assert "conditional/dry-run-only" in action["dry_run_only_reason"]
    operation = action["operations"][0]
    assert operation["payload"]["wlan"]["opmode"] == "wpa3-aes-ccm-128"
    assert operation["payload"]["wlan"]["auth_server1"] == "InternalServer"
    assert operation["payload"]["wlan"]["access_type"] == "network_based"

    dry_result = adapter.dry_run([wlan])
    assert dry_result["results"][0]["status"] == "dry-run"

    # A real (non-dry-run) execute is refused even with confirmation and
    # writes enabled -- no additional (real) write call ever reaches the
    # backend beyond the dry-run one above.
    write_calls_before_real_execute = len(backend.write_calls)
    real_result = adapter.execute([wlan], dry_run=False, confirmation=True)
    assert real_result["results"][0]["status"] == "blocked"
    assert "dry-run-only" in real_result["results"][0]["errors"][0]
    assert len(backend.write_calls) == write_calls_before_real_execute
    assert all(call[0].arguments.get("dry_run") is True for call in backend.write_calls)


def test_classic_wpa2_enterprise_remains_unsupported_distinct_from_wpa3():
    sec = security("enterprise_dot1x", opmode="wpa2-aes", dot1x_auth_profile="corp-dot1x")
    wlan = candidate(
        "wlan",
        "Corp",
        payload={
            "name": "Corp",
            "essid": "Corp",
            "vlan": 60,
            "aaa_profile": "corp-aaa",
            "security": sec,
        },
        unsupported_fields={
            "ssid_profile.opmode": "wpa2-aes",
            "virtual_ap.forward_mode": "bridge",
        },
    )
    adapter = classic_adapter(
        FakeBackend(),
        external_object_references={"wlan:Corp": {"auth_server1": "InternalServer"}},
    )
    preview = adapter.preview([wlan])
    action = preview["operations"][0]
    assert action["status"] == "unsupported"
    assert "wpa2-enterprise" in action["unsupported_warnings"][0].lower()


# --------------------------------------------------------------------------
# Classic Central: every other secured-WLAN mode stays blocked with
# actionable, mode-specific guidance
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,expected_snippet",
    [
        ("wpa2_personal", "v2 wlan contract"),
        ("wpa3_transition_personal", "transition"),
        ("enhanced_open", "enhanced open"),
        ("mac_auth_only", "mac-auth"),
        ("mac_auth_psk", "mac-auth"),
        ("unknown", "could not be classified"),
    ],
)
def test_classic_blocked_wlan_security_modes_report_actionable_guidance(mode, expected_snippet):
    wlan = candidate(
        "wlan",
        "Blocked",
        payload={
            "name": "Blocked",
            "essid": "Blocked",
            "vlan": 50,
            "aaa_profile": None,
            "security": security(mode),
        },
        unsupported_fields={
            "ssid_profile.opmode": mode,
            "virtual_ap.forward_mode": "bridge",
        },
    )
    preview = classic_adapter(FakeBackend()).preview([wlan])
    action = preview["operations"][0]
    assert action["status"] == "unsupported"
    assert expected_snippet.lower() in action["unsupported_warnings"][0].lower()


def test_classic_open_and_wpa3_personal_reject_attached_aaa_profile():
    open_wlan = candidate(
        "wlan",
        "Guest",
        payload={
            "name": "Guest",
            "essid": "Guest",
            "vlan": 20,
            "aaa_profile": "some-aaa",
            "security": security("open"),
        },
        unsupported_fields={
            "ssid_profile.opmode": "open",
            "virtual_ap.forward_mode": "bridge",
        },
    )
    result = classic_adapter(FakeBackend()).preview([open_wlan])
    action = result["operations"][0]
    assert action["status"] == "unsupported"
    assert "does not translate aos8 aaa profiles" in action["unsupported_warnings"][0].lower()


def test_classic_wlan_rejects_unmapped_wep_key_material():
    wlan = candidate(
        "wlan",
        "Legacy",
        payload={
            "name": "Legacy",
            "essid": "Legacy",
            "vlan": 20,
            "aaa_profile": None,
            "security": security("unknown"),
        },
        unsupported_fields={
            "ssid_profile.opmode": "static-wep",
            "virtual_ap.forward_mode": "bridge",
            "ssid_profile.wepkey1": "<redacted:present>",
        },
    )
    result = classic_adapter(FakeBackend()).preview([wlan])
    action = result["operations"][0]
    assert action["status"] == "unsupported"
    assert "unmapped source fields" in action["unsupported_warnings"][0].lower()


def test_classic_wlan_rejects_non_bridged_forward_mode():
    wlan = candidate(
        "wlan",
        "Tunnel",
        payload={
            "name": "Tunnel",
            "essid": "Tunnel",
            "vlan": 20,
            "aaa_profile": None,
            "security": security("open"),
        },
        unsupported_fields={
            "ssid_profile.opmode": "open",
            "virtual_ap.forward_mode": "tunnel",
        },
    )
    result = classic_adapter(FakeBackend()).preview([wlan])
    action = result["operations"][0]
    assert action["status"] == "unsupported"
    assert "bridged" in action["unsupported_warnings"][0].lower()


# --------------------------------------------------------------------------
# Classic Central: AP groups/device moves stay manual/unsupported unless an
# explicit operator mapping *and* explicit device serials are supplied — and
# even then, no verified device-move endpoint is ever fabricated.
# --------------------------------------------------------------------------


def test_classic_ap_group_requires_explicit_group_then_serials_then_stays_manual():
    apg = candidate("ap_group", "aos8-lobby-ap-group", payload={"name": "aos8-lobby-ap-group"})

    no_mapping = classic_adapter(FakeBackend()).preview([apg])
    action = no_mapping["operations"][0]
    assert action["status"] == "unsupported"
    assert "ap_group_target_map" in action["unsupported_warnings"][0]

    mapped_no_serials = classic_adapter(
        FakeBackend(),
        ap_group_target_map={"aos8-lobby-ap-group": "Lobby-Classic-Group"},
    ).preview([apg])
    action = mapped_no_serials["operations"][0]
    assert action["status"] == "unsupported"
    assert "device serial" in action["unsupported_warnings"][0].lower()

    mapped_with_serials = classic_adapter(
        FakeBackend(),
        ap_group_target_map={"aos8-lobby-ap-group": "Lobby-Classic-Group"},
        ap_group_device_serials={"aos8-lobby-ap-group": ("CNXXXX0001", "CNXXXX0002")},
    ).preview([apg])
    action = mapped_with_serials["operations"][0]
    assert action["status"] == "unsupported"
    message = action["unsupported_warnings"][0].lower()
    assert "no verified classic central device-move" in message
    assert "never fabricated" in message
    # Never leak the operator-supplied Classic-group name, the serial
    # values, or a derived serial *count* into this message -- only that
    # a runtime mapping was supplied.
    assert "lobby-classic-group" not in message
    assert "cnxxxx0001" not in message
    assert "cnxxxx0002" not in message
    assert "2 device serial" not in message
    assert "runtime device serial mapping" in message


# --------------------------------------------------------------------------
# Classic Central: precise manual/AP-CLI/template guidance per unverified
# family (AAA/auth servers/server groups, roles/policies/ACLs, routes, VRRP,
# controllers) — never presented as a safe idempotent per-object write.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "object_type",
    [
        "aaa_profile",
        "dot1x_auth_profile",
        "mac_auth_profile",
        "server_group",
        "auth_server",
        "role",
        "policy",
        "route",
        "vrrp",
        "controller",
    ],
)
def test_classic_manual_family_guidance_never_looks_like_a_safe_write(object_type):
    result = classic_adapter(FakeBackend()).preview([candidate(object_type, "thing")])
    action = result["operations"][0]
    assert action["status"] == "unsupported"
    message = action["unsupported_warnings"][0]
    assert "AP-CLI" in message
    assert "never" in message.lower()
    assert action["operations"] == []


def test_classic_manual_family_guidance_is_specific_per_family():
    aaa_message = classic_adapter(FakeBackend()).preview(
        [candidate("aaa_profile", "corp-aaa")]
    )["operations"][0]["unsupported_warnings"][0]
    role_message = classic_adapter(FakeBackend()).preview(
        [candidate("role", "employee")]
    )["operations"][0]["unsupported_warnings"][0]
    assert aaa_message != role_message
    assert "AAA profiles" in aaa_message
    assert "roles" in role_message


# --------------------------------------------------------------------------
# Finding #2 regression: a write invoker returning without raising is never
# sufficient proof a write applied. Non-2xx/rejected write results must be
# caught, and every candidate with a `read_back_operation` must have that
# read-back confirm the identifier and any declared `read_back_expectations`
# before being reported "applied".
# --------------------------------------------------------------------------


def _open_wlan_candidate(name="Guest", vlan=20):
    return candidate(
        "wlan",
        name,
        payload={
            "name": name,
            "essid": name,
            "vlan": vlan,
            "aaa_profile": None,
            "security": security("open"),
        },
        unsupported_fields={
            "ssid_profile.opmode": "open",
            "virtual_ap.forward_mode": "bridge",
        },
    )


def test_write_result_status_code_rejection_prevents_false_success():
    # The write invoker returns *without raising*, but the payload carries an
    # explicit non-2xx status_code -- this must never be reported "applied".
    backend = FakeBackend(
        write_results={"central_api_request": {"status_code": 500, "ok": False}}
    )
    adapter = classic_adapter(backend)
    result = adapter.execute([_open_wlan_candidate()], dry_run=False, confirmation=True)
    outcome = result["results"][0]
    assert outcome["status"] == "failed"
    assert any("status_code=500" in error for error in outcome["errors"])
    # Read-back must never even be attempted once the write itself was
    # rejected.
    assert not any(
        call.name == "central_api_read_back" for call in backend.read_calls
    )


def test_write_result_ok_false_rejection_prevents_false_success():
    backend = FakeBackend(write_results={"central_api_request": {"ok": False}})
    adapter = classic_adapter(backend)
    result = adapter.execute([_open_wlan_candidate()], dry_run=False, confirmation=True)
    outcome = result["results"][0]
    assert outcome["status"] == "failed"
    assert any("ok=False" in error for error in outcome["errors"])


def test_write_result_error_field_rejection_prevents_false_success():
    backend = FakeBackend(
        write_results={"central_api_request": {"error": "group is out of sync"}}
    )
    adapter = classic_adapter(backend)
    result = adapter.execute([_open_wlan_candidate()], dry_run=False, confirmation=True)
    outcome = result["results"][0]
    assert outcome["status"] == "failed"
    assert any("group is out of sync" in error for error in outcome["errors"])


def test_missing_read_back_confirmation_prevents_false_success():
    # The write invoker "succeeds" (no rejection signal), but the mandatory
    # post-write read-back does not confirm the identifier -- this is the
    # documented Classic group/device write footgun (contract matrix §3
    # item 8): a successful write response is not sufficient proof the
    # group actually applied the change.
    backend = FakeBackend(reads={"central_api_read_back": {"error": "not found"}})
    adapter = classic_adapter(backend)
    result = adapter.execute([_open_wlan_candidate()], dry_run=False, confirmation=True)
    outcome = result["results"][0]
    assert outcome["status"] == "failed"
    assert any("read_back verification failed" in error for error in outcome["errors"])
    assert any("was not confirmed" in error for error in outcome["errors"])


def test_read_back_field_mismatch_prevents_false_success():
    # Identifier matches, but a declared read_back_expectations field (e.g.
    # opmode) does not -- must still fail, not be reported "applied".
    backend = FakeBackend(
        reads={
            "central_api_read_back": {
                "wlan": {
                    "name": "Guest",
                    "essid": "Guest",
                    "opmode": "wpa2-psk-aes",  # wrong: expected opensystem
                    "vlan": "20",
                }
            }
        }
    )
    adapter = classic_adapter(backend)
    result = adapter.execute([_open_wlan_candidate()], dry_run=False, confirmation=True)
    outcome = result["results"][0]
    assert outcome["status"] == "failed"
    assert any(
        "opmode='opensystem'" in error and "not confirmed" in error
        for error in outcome["errors"]
    )


def test_read_back_confirmation_required_field_and_identifier_match_marks_applied():
    backend = FakeBackend(
        reads={
            "central_api_read_back": {
                "wlan": {
                    "name": "Guest",
                    "essid": "Guest",
                    "opmode": "opensystem",
                    "vlan": "20",
                }
            }
        }
    )
    adapter = classic_adapter(backend)
    result = adapter.execute([_open_wlan_candidate()], dry_run=False, confirmation=True)
    outcome = result["results"][0]
    assert outcome["status"] == "applied"
    assert any(call.name == "central_api_read_back" for call in backend.read_calls)


def test_dry_run_never_requires_read_back_confirmation():
    # Dry-run writes must not attempt (or require) a mandatory read-back --
    # there is nothing on the wire to confirm.
    backend = FakeBackend()
    adapter = classic_adapter(backend)
    result = adapter.dry_run([_open_wlan_candidate()])
    outcome = result["results"][0]
    assert outcome["status"] == "dry-run"
    assert not any(
        call.name == "central_api_read_back" for call in backend.read_calls
    )


# --------------------------------------------------------------------------
# Finding #5 regression: operator-context fields are surfaced in the
# preview target dict so a later orchestrator layer can persist and reload
# them, and WPA3-Enterprise's conditional dry-run is reachable when the
# operator supplies an explicit auth-server reference this way.
# --------------------------------------------------------------------------


def test_preview_target_exposes_operator_context_fields():
    backend = FakeBackend()
    adapter = classic_adapter(
        backend,
        external_object_references={"wlan:Corp": {"auth_server1": "InternalServer"}},
        ap_group_target_map={"ap-group-hq": "HQ-Group"},
        ap_group_device_serials={"ap-group-hq": ["CN1234", "CN5678"]},
    )
    preview = adapter.preview([_open_wlan_candidate()])
    target = preview["target"]
    assert target["external_object_references"] == {
        "wlan:Corp": {"auth_server1": "InternalServer"}
    }
    assert target["ap_group_target_map"] == {"ap-group-hq": "HQ-Group"}
    assert target["ap_group_device_serials"] == {"ap-group-hq": ["CN1234", "CN5678"]}


# --------------------------------------------------------------------------
# Runtime secret length bound (`MAX_SECRET_LENGTH`): a caller-supplied
# target secret (PSK/passphrase, RADIUS/TACACS+ shared secret, LDAP bind
# password) must be rejected outright once it exceeds the bound, before it
# is ever mapped into a write payload -- see `_secret_value`/
# `_secret_bundle_error` in `hpe_networking_mcp.pipeline.aos8_target_adapters`.
# --------------------------------------------------------------------------


def test_secret_bundle_rejects_oversized_wpa3_personal_passphrase():
    wlan = _wpa3_personal_wlan(name="TooLong")
    oversized = "x" * (MAX_SECRET_LENGTH + 1)
    result = classic_adapter(
        FakeBackend(), secrets={"wlan:TooLong": {"wpa_passphrase": oversized}}
    ).preview([wlan])
    action = result["operations"][0]
    assert action["status"] == "unsupported"
    assert str(MAX_SECRET_LENGTH) in action["unsupported_warnings"][0]
    assert oversized not in str(result)


def test_secret_bundle_accepts_max_bound_wpa3_personal_passphrase():
    wlan = _wpa3_personal_wlan(name="MaxBound")
    at_bound = "y" * MAX_SECRET_LENGTH
    result = classic_adapter(
        FakeBackend(), secrets={"wlan:MaxBound": {"wpa_passphrase": at_bound}}
    ).preview([wlan])
    action = result["operations"][0]
    assert action["status"] == "ready"
    assert action["operations"][0]["payload"]["wlan"]["wpa_passphrase"] == "***"
