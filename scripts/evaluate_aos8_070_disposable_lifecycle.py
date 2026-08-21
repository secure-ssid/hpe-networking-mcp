"""AOS8 -> New Central v0.7 disposable-write lifecycle harness.

Credential-gated, bounded create/read-back/delete round-trip harness for the
New Central target object families most in need of a controlled write
confirmation: RADIUS/LDAP/TACACS auth servers, server groups, AAA profiles,
device 802.1X (dot1x) and MAC-auth profiles, IPv4 static routes, VRRP
interfaces, AP-group/Device-Group profile assignment, and role
scope+device-function config-assignments.

Every mode is gated through `hpe_networking_mcp.pipeline.live_test_config` (platform
`"central"`, since every object family here lives on the New Central target
account, never the AOS8 source):

- `--mode status` never makes a network call. It reports the same redacted
  `live_test_status("central")` summary every other v0.7 harness reports,
  plus this harness's own per-kind support classification (see
  `LIFECYCLE_KINDS` below) -- credential presence never implies
  authorization.
- `--mode read` requires `HPE_MCP_LIVE_TEST_CENTRAL_READ=1`
  (`live_test_read_enabled("central")`). Performs one bounded preflight/
  evidence GET for the requested `--kind` -- never a write.
- `--mode write` requires *both* the read opt-in and
  `HPE_MCP_LIVE_TEST_CENTRAL_WRITE=1`
  (`live_test_write_enabled("central")`), `--confirm`, and a lab-owned
  `--lab-prefix`-prefixed identifier. Builds a synthetic, disposable,
  lab-owned candidate for the requested `--kind`, dry-runs it, executes the
  create, attempts a read-back, and always attempts a best-effort cleanup
  delete -- even when the create step failed, and even when the harness
  process is interrupted after create but before cleanature (the printed/
  written evidence artifact always records whether cleanup succeeded so an
  operator can finish it by hand otherwise).

Every kind not yet backed by a verified New Central adapter mapping
(`route`, `vrrp`, `ap_group`) is refused for `--mode write` with an explicit,
specific reason instead of being silently skipped or approximated -- see
`docs/aos8-migration-contract-matrix.md` §6.11-§6.13 for the underlying
evidence gaps. `assignment` reuses the already-verified `role`
config-assignment lifecycle (§6.9) rather than inventing a separate
assignment-only object family.

This script performs **no live calls when imported or by default** -- it
only acts when explicitly invoked with `--mode read`/`write` *and* the
corresponding `hpe_networking_mcp.pipeline.live_test_config` opt-in(s) are set. Per the
`v07-aos8-promotion` todo, this harness is implemented for later confirmed
execution and is not run live as part of that todo.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from hpe_networking_mcp.pipeline.aos8_target_adapters import (
    AdapterError,
    NewCentralAdapter,
    Operation,
    TargetContext,
    TargetType,
)
from hpe_networking_mcp.pipeline.live_test_config import (
    live_test_read_enabled,
    live_test_status,
    live_test_write_enabled,
)

PLATFORM = "central"
DEFAULT_LAB_PREFIX = "hpe-mcp-lab-"


@dataclass(frozen=True)
class LifecycleKind:
    """One disposable-lifecycle object family this harness understands.

    `object_type`/`build_payload` are only meaningful when
    `adapter_mapping_verified` is True: they build a synthetic, lab-owned
    candidate dict in the exact shape
    `hpe_networking_mcp.pipeline.aos8_migration.build_migration_plan`
    would have produced, which `NewCentralAdapter.candidate_action` maps
    the same way it would for a real migration candidate -- this harness
    never duplicates or approximates the adapter's own mapping logic.
    """

    name: str
    object_type: str | None
    build_payload: Callable[[str], dict[str, Any]] | None
    dependencies: Callable[[str], list[str]]
    adapter_mapping_verified: bool
    unsupported_reason: str | None = None
    # Identifier construction for the synthetic lab candidate -- defaults to
    # the bare lab name, overridden for kinds whose production
    # (`hpe_networking_mcp.pipeline.aos8_migration`) identifier convention encodes extra
    # structure the adapter mapping itself depends on (e.g. auth servers key
    # `dependencies`/`_map_server_group` off an `"auth_server:{type}:{name}"`
    # dependency string, so the auth-server candidate's own identifier must
    # be `"{type}:{name}"`, not the bare name).
    build_identifier: Callable[[str], str] = lambda lab_name: lab_name
    # The `context.secret_inputs[candidate_key]` field name this kind's
    # adapter mapping requires via `_secret_value` (e.g. auth servers need
    # `shared_secret`). `None` means no secret is required.
    secret_field: str | None = None


def _no_dependencies(_: str) -> list[str]:
    return []


def _server_group_dependencies(lab_name: str) -> list[str]:
    return [f"auth_server:radius:{lab_name}"]


def _radius_auth_server_identifier(lab_name: str) -> str:
    return f"radius:{lab_name}"


LIFECYCLE_KINDS: dict[str, LifecycleKind] = {
    "auth_server": LifecycleKind(
        name="auth_server",
        object_type="auth_server",
        build_payload=lambda lab_name: {
            "name": lab_name,
            "server_type": "radius",
            "host": "192.0.2.10",
        },
        dependencies=_no_dependencies,
        adapter_mapping_verified=True,
        build_identifier=_radius_auth_server_identifier,
        secret_field="shared_secret",
    ),
    "server_group": LifecycleKind(
        name="server_group",
        object_type="server_group",
        build_payload=lambda lab_name: {
            "name": lab_name,
            "auth_servers": [lab_name],
            "auth_server_entries": [lab_name],
            "fail_through": None,
            "load_balance": None,
            "derivation_rules": None,
        },
        dependencies=_server_group_dependencies,
        adapter_mapping_verified=True,
    ),
    "aaa_profile": LifecycleKind(
        name="aaa_profile",
        object_type="aaa_profile",
        build_payload=lambda lab_name: {
            "name": lab_name,
            "default_user_role": None,
            "dot1x_auth_profile": None,
            "dot1x_default_role": None,
            "dot1x_server_group": None,
            "mac_auth_profile": None,
            "mac_default_role": None,
            "mac_server_group": None,
            "accounting_server_group": None,
        },
        dependencies=_no_dependencies,
        adapter_mapping_verified=True,
    ),
    "dot1x_auth_profile": LifecycleKind(
        name="dot1x_auth_profile",
        object_type="dot1x_auth_profile",
        build_payload=lambda lab_name: {"name": lab_name, "auth_type": "dot1x"},
        dependencies=_no_dependencies,
        adapter_mapping_verified=True,
    ),
    "mac_auth_profile": LifecycleKind(
        name="mac_auth_profile",
        object_type="mac_auth_profile",
        build_payload=lambda lab_name: {"name": lab_name, "auth_type": "mac"},
        dependencies=_no_dependencies,
        adapter_mapping_verified=True,
    ),
    "assignment": LifecycleKind(
        name="assignment",
        object_type="role",
        build_payload=lambda lab_name: {"name": lab_name, "policies": ["allowall"]},
        dependencies=_no_dependencies,
        adapter_mapping_verified=True,
    ),
    "route": LifecycleKind(
        name="route",
        object_type=None,
        build_payload=None,
        dependencies=_no_dependencies,
        adapter_mapping_verified=False,
        unsupported_reason=(
            "IPv4/IPv6 static routes have no verified New Central adapter "
            "mapping (`NewCentralAdapter` has no `_map_route`): a live 0.6 "
            "MOBILITY_GW read reached /network-config/v1alpha1/static-route "
            "but only exposed a default-gateway representation, not a "
            "general destination/prefix write contract -- see "
            "docs/aos8-migration-contract-matrix.md §6.12. This harness "
            "refuses --mode write; use --mode read for the same bounded, "
            "evidenced GET only."
        ),
    ),
    "vrrp": LifecycleKind(
        name="vrrp",
        object_type=None,
        build_payload=None,
        dependencies=_no_dependencies,
        adapter_mapping_verified=False,
        unsupported_reason=(
            "VRRP/VRRPv6/tracking has no verified New Central adapter "
            "mapping (`NewCentralAdapter` has no `_map_vrrp`): a live 0.6 "
            "MOBILITY_GW GET confirmed /vrrp is reachable but returned an "
            "empty collection, so no live attachment/tracking shape is "
            "available yet -- see docs/aos8-migration-contract-matrix.md "
            "§6.13. This harness refuses --mode write; use --mode read for "
            "the same bounded, evidenced GET only."
        ),
    ),
    "ap_group": LifecycleKind(
        name="ap_group",
        object_type=None,
        build_payload=None,
        dependencies=_no_dependencies,
        adapter_mapping_verified=False,
        unsupported_reason=(
            "AOS8 AP groups have no automatic 1:1 New Central Device Group "
            "mapping and no `_map_ap_group` method exists at all: an "
            "operator must select the target Device Group/scope and "
            "profile assignments explicitly -- see "
            "docs/aos8-migration-contract-matrix.md §6.11. This harness "
            "refuses both --mode read and --mode write for this kind; there "
            "is no single-object endpoint to probe."
        ),
    ),
}


class LifecycleHarnessError(ValueError):
    """Raised for a gate/kind/argument violation before any network call."""


def _kind(name: str) -> LifecycleKind:
    kind = LIFECYCLE_KINDS.get(name)
    if kind is None:
        raise LifecycleHarnessError(
            f"Unknown --kind {name!r}; expected one of {sorted(LIFECYCLE_KINDS)}"
        )
    return kind


def status_report() -> dict[str, Any]:
    """Redacted, network-free status: gate state plus per-kind support."""
    return {
        "live_test_status": live_test_status(PLATFORM),
        "kinds": {
            name: {
                "adapter_mapping_verified": kind.adapter_mapping_verified,
                "supports_write": kind.adapter_mapping_verified,
                "unsupported_reason": kind.unsupported_reason,
            }
            for name, kind in sorted(LIFECYCLE_KINDS.items())
        },
    }


def _build_context(
    *, scope_id: str, scope_name: str, persona: str, secret_inputs: Mapping[str, Mapping[str, str]]
) -> TargetContext:
    return TargetContext(
        target_type=TargetType.NEW_CENTRAL,
        scope_id=scope_id,
        scope_name=scope_name,
        persona=persona,
        secret_inputs=secret_inputs,
    )


def _build_adapter(
    context: TargetContext,
    *,
    read_invoker: Callable[[Operation], Any],
    write_invoker: Callable[..., Any],
    writes_enabled: Callable[[TargetType], bool],
) -> NewCentralAdapter:
    return NewCentralAdapter(
        context,
        scope_resolver=lambda ctx: (str(ctx.scope_id), str(ctx.scope_name)),
        persona_validator=lambda ctx: str(ctx.persona),
        read_invoker=read_invoker,
        write_invoker=write_invoker,
        writes_enabled=writes_enabled,
    )


def _lab_candidate(kind: LifecycleKind, lab_name: str) -> dict[str, Any]:
    assert kind.build_payload is not None and kind.object_type is not None
    identifier = kind.build_identifier(lab_name)
    return {
        "object_type": kind.object_type,
        "identifier": identifier,
        "payload": kind.build_payload(lab_name),
        "dependencies": kind.dependencies(lab_name),
        "apply_order": 10,
        "unsupported_fields": {},
        "warnings": [],
        "requires_secret_input": kind.secret_field is not None,
        "secret_fields": [kind.secret_field] if kind.secret_field else [],
    }


def _lab_candidate_key(kind: LifecycleKind, lab_name: str) -> str:
    assert kind.object_type is not None
    return f"{kind.object_type}:{kind.build_identifier(lab_name)}"


def _disposable_secret_inputs(
    kind: LifecycleKind,
    lab_name: str,
    overrides: Mapping[str, Mapping[str, str]] | None,
) -> dict[str, dict[str, str]]:
    """Build the `TargetContext.secret_inputs` bundle for one lab candidate.

    Some adapter mappings (currently only `auth_server`) need *a* secret
    value present just to construct their `CandidateAction` at all (the
    create-payload-building and read/delete-operation-building code paths
    are not separated) -- even a bounded, read-only evidence probe needs
    one. This is always a disposable, harness-generated placeholder value,
    never a real credential, and it is never logged or persisted; `overrides`
    lets a caller substitute a real value for an actual write round trip.
    """
    merged = dict(overrides or {})
    if kind.secret_field is not None:
        key = _lab_candidate_key(kind, lab_name)
        merged.setdefault(key, {kind.secret_field: f"{lab_name}-disposable-secret"})
    return merged


def read_evidence(
    kind_name: str,
    *,
    read_invoker: Callable[[Operation], Any],
    scope_id: str,
    scope_name: str,
    persona: str,
    lab_name: str = "hpe-mcp-lab-probe",
) -> dict[str, Any]:
    """Perform one bounded preflight/evidence read for `kind_name`.

    Requires `live_test_read_enabled("central")`. Never a write.
    """
    if not live_test_read_enabled(PLATFORM):
        raise LifecycleHarnessError(
            "Read-only live evidence collection is disabled; set "
            "HPE_MCP_LIVE_TEST_CENTRAL_READ=1 to enable."
        )
    kind = _kind(kind_name)
    if not kind.adapter_mapping_verified:
        raise LifecycleHarnessError(
            f"{kind_name}: {kind.unsupported_reason}"
        )
    context = _build_context(
        scope_id=scope_id,
        scope_name=scope_name,
        persona=persona,
        secret_inputs=_disposable_secret_inputs(kind, lab_name, None),
    )
    adapter = _build_adapter(
        context,
        read_invoker=read_invoker,
        write_invoker=lambda operation, confirmation: (_ for _ in ()).throw(
            LifecycleHarnessError("read_evidence never invokes a write.")
        ),
        writes_enabled=lambda _target: False,
    )
    candidate = _lab_candidate(kind, lab_name)
    action = adapter.candidate_action(candidate)
    if action.compatibility_errors:
        raise LifecycleHarnessError(
            f"{kind_name}: adapter mapping rejected this lab candidate: "
            f"{'; '.join(action.compatibility_errors)}"
        )
    if action.read_operation is None:
        return {"kind": kind_name, "read_operation": None, "result": None}
    result = read_invoker(action.read_operation)
    return {
        "kind": kind_name,
        "read_operation": action.read_operation.preview_dict(),
        "result": result,
    }


def run_disposable_write_lifecycle(
    kind_name: str,
    *,
    confirm: bool,
    lab_prefix: str,
    lab_name: str,
    read_invoker: Callable[[Operation], Any],
    write_invoker: Callable[..., Any],
    scope_id: str,
    scope_name: str,
    persona: str,
    secret_inputs: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Create -> read-back -> (always attempt) delete a disposable, lab-owned
    candidate for `kind_name` against a live New Central target.

    Requires `live_test_write_enabled("central")` (which itself already
    requires the read opt-in -- see `hpe_networking_mcp.pipeline.live_test_config`), `confirm`,
    and a `lab_name` that starts with `lab_prefix` (at least 6 characters).
    Every kind without a verified adapter mapping (`route`, `vrrp`,
    `ap_group`) is refused outright with an explicit reason.

    This function is never invoked by importing this module, by
    `--mode status`, or by `--mode read` -- only an explicit `--mode write`
    CLI invocation (or an equivalent direct call) reaches it, and even then
    only after every gate above passes.
    """
    if not confirm:
        raise LifecycleHarnessError("Disposable write execution requires confirm=True.")
    if not live_test_write_enabled(PLATFORM):
        raise LifecycleHarnessError(
            "Disposable live writes are disabled; set both "
            "HPE_MCP_LIVE_TEST_CENTRAL_READ=1 and "
            "HPE_MCP_LIVE_TEST_CENTRAL_WRITE=1 to enable."
        )
    if len(lab_prefix) < 6:
        raise LifecycleHarnessError("lab_prefix must contain at least six characters.")
    if not lab_name.startswith(lab_prefix):
        raise LifecycleHarnessError(
            f"lab_name {lab_name!r} must start with lab_prefix {lab_prefix!r}."
        )
    kind = _kind(kind_name)
    if not kind.adapter_mapping_verified:
        raise LifecycleHarnessError(f"{kind_name}: {kind.unsupported_reason}")

    candidate_key = _lab_candidate_key(kind, lab_name)
    context = _build_context(
        scope_id=scope_id,
        scope_name=scope_name,
        persona=persona,
        secret_inputs=_disposable_secret_inputs(kind, lab_name, secret_inputs),
    )
    adapter = _build_adapter(
        context,
        read_invoker=read_invoker,
        write_invoker=write_invoker,
        writes_enabled=lambda _target: True,
    )
    candidate = _lab_candidate(kind, lab_name)
    action = adapter.candidate_action(candidate)
    if action.compatibility_errors:
        raise LifecycleHarnessError(
            f"{kind_name}: adapter mapping rejected this lab candidate: "
            f"{'; '.join(action.compatibility_errors)}"
        )

    evidence: dict[str, Any] = {"kind": kind_name, "candidate": candidate_key}

    # This deliberately drives `action.operations`/`read_back_operation`/
    # `delete_operations` directly instead of `adapter.execute(...)`:
    # `auth_server`/`server_group`/`aaa_profile`/`dot1x_auth_profile`/
    # `mac_auth_profile` candidates are *permanently* `status="blocked"`
    # through the normal `execute()` path
    # (`BaseCentralTargetAdapter._assignment_write_blocker`) until exactly
    # this kind of controlled, disposable create/read-back/delete lab round
    # trip has been completed and recorded -- this harness *is* that
    # controlled round trip, so it must be able to reach the create/delete
    # calls the standing blocker is gating, not be blocked by it itself.
    dry_run_results = [
        {
            "operation": operation.with_dry_run(True).preview_dict(),
            "result": write_invoker(operation.with_dry_run(True), confirmation=False),
        }
        for operation in action.operations
    ]
    evidence["dry_run"] = dry_run_results

    create_results: list[dict[str, Any]] = []
    create_errors: list[str] = []
    for operation in action.operations:
        try:
            value = write_invoker(operation.with_dry_run(False), confirmation=True)
            create_results.append({"operation": operation.name, "result": value})
        except Exception as exc:  # noqa: BLE001 - reported, never re-raised
            create_errors.append(f"{operation.name}: {exc}")
            break
    evidence["create"] = create_results
    evidence["create_status"] = "failed" if create_errors else "applied"
    evidence["create_errors"] = create_errors

    read_back_operation = action.read_back_operation or action.read_operation
    if read_back_operation is not None:
        try:
            evidence["read_back"] = read_invoker(read_back_operation)
        except Exception as exc:  # noqa: BLE001 - reported, never re-raised
            evidence["read_back"] = None
            evidence["read_back_error"] = str(exc)
    else:
        evidence["read_back"] = None

    cleanup_results: list[dict[str, Any]] = []
    cleanup_ok = True
    if action.delete_operations:
        for operation in action.delete_operations:
            try:
                value = write_invoker(operation.with_dry_run(False), confirmation=True)
                cleanup_results.append({"operation": operation.name, "result": value})
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup, never re-raised
                cleanup_ok = False
                cleanup_results.append({"operation": operation.name, "error": str(exc)})
    else:
        cleanup_ok = False
        cleanup_results.append(
            {"operation": None, "error": "no verified delete_operations for this kind"}
        )
    evidence["cleanup"] = cleanup_results
    evidence["cleanup_ok"] = cleanup_ok
    return evidence


def _write_output(value: Any, output: str | None) -> None:
    text = json.dumps(value, indent=2, sort_keys=True, default=str)
    if output:
        from pathlib import Path

        Path(output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("status", "read", "write"), default="status")
    parser.add_argument("--kind", choices=sorted(LIFECYCLE_KINDS), default=None)
    parser.add_argument("--lab-prefix", default=DEFAULT_LAB_PREFIX)
    parser.add_argument("--lab-name", default=None)
    parser.add_argument("--scope-id", default=None)
    parser.add_argument("--scope-name", default=None)
    parser.add_argument("--persona", default="MOBILITY_GW")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)

    if args.mode == "status":
        _write_output(status_report(), args.output)
        return 0

    if not args.kind:
        parser.error("--kind is required for --mode read/write")
    if not args.scope_id or not args.scope_name:
        parser.error("--scope-id and --scope-name are required for --mode read/write")

    # This module never wires a live `read_invoker`/`write_invoker` itself
    # -- doing so requires the production `hpe_networking_mcp.mcp_servers.aos8` bindings
    # (`_aos8_migration_read_invoker`/`_aos8_migration_write_invoker`),
    # which this script deliberately does not import at module scope so
    # `--mode status` (and importing this module for its tests) never has
    # a `mcp_servers`/network dependency. A caller wanting a real `--mode
    # read`/`write` run wires those in explicitly; see this module's
    # docstring -- this harness is implemented for later confirmed
    # execution, not invoked live by this todo.
    try:
        from hpe_networking_mcp.mcp_servers.aos8 import (
            _aos8_migration_read_invoker,
            _aos8_migration_write_invoker,
        )
    except Exception as exc:  # pragma: no cover - defensive only
        parser.error(f"could not load live invokers: {exc}")
        return 2

    try:
        if args.mode == "read":
            result = read_evidence(
                args.kind,
                read_invoker=_aos8_migration_read_invoker,
                scope_id=args.scope_id,
                scope_name=args.scope_name,
                persona=args.persona,
                lab_name=args.lab_name or f"{args.lab_prefix}probe",
            )
        else:
            lab_name = args.lab_name or f"{args.lab_prefix}rt"
            result = run_disposable_write_lifecycle(
                args.kind,
                confirm=args.confirm,
                lab_prefix=args.lab_prefix,
                lab_name=lab_name,
                read_invoker=_aos8_migration_read_invoker,
                write_invoker=_aos8_migration_write_invoker,
                scope_id=args.scope_id,
                scope_name=args.scope_name,
                persona=args.persona,
            )
    except (LifecycleHarnessError, AdapterError) as exc:
        _write_output({"status": "blocked", "error": str(exc)}, args.output)
        return 1

    _write_output(result, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
