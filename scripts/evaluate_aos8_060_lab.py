"""AOS8 0.6 live evidence and controlled lab-write harness.

Read-only modes collect bounded, sanitized evidence from AOS8, Classic
Central, or New Central. Controlled writes use a mandatory two-phase flow:

1. Generate a reviewable plan with ``--prepare-write-plan``.
2. Execute that unchanged plan with its SHA-256 digest, exact target
   confirmation, explicit Central write gate, and mandatory cleanup.

The harness never persists migration runs, target secrets, or operator maps.
It is intentionally stricter than the MCP write tools because its purpose is
contract validation in disposable lab scopes, not general migration.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hpe_networking_mcp.mcp_servers import aos8 as aos8_tools
from hpe_networking_mcp.mcp_servers import shared as shared_tools
from hpe_networking_mcp.pipeline.aos8_target_adapters import (
    CandidateAction,
    ClassicCentralAdapter,
    ConflictPolicy,
    NewCentralAdapter,
    Operation,
    TargetContext,
    TargetType,
)

try:
    from scripts import evaluate_aos8_050_readonly as baseline
except ModuleNotFoundError:
    import evaluate_aos8_050_readonly as baseline

REPO_ROOT = Path(__file__).resolve().parents[1]
PLAN_SCHEMA_VERSION = 1
DEFAULT_LAB_PREFIX = "hpe-mcp-lab-"
TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class LabTarget:
    target_type: TargetType
    scope_name: str
    persona: str
    scope_id: str | None = None
    cluster_name: str | None = None
    cluster_scope_id: str | None = None

    def confirmation_value(self) -> str:
        return self.scope_name

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_type": self.target_type.value,
            "scope_name": self.scope_name,
            "scope_id": self.scope_id,
            "persona": self.persona,
            "cluster_name": self.cluster_name,
            "cluster_scope_id": self.cluster_scope_id,
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _candidate_key(candidate: Mapping[str, Any]) -> str:
    return f"{candidate.get('object_type')}:{candidate.get('identifier')}"


def _load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text())


def _load_candidates(path: str | Path) -> list[dict[str, Any]]:
    value = _load_json(path)
    candidates = value.get("candidates") if isinstance(value, Mapping) else value
    if not isinstance(candidates, list) or not candidates:
        raise ValueError(
            "Candidate file must contain a non-empty JSON list or {candidates: [...]}."
        )
    normalized: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping):
            raise ValueError(f"Candidate at index {index} must be a JSON object.")
        if not candidate.get("object_type") or candidate.get("identifier") in (None, ""):
            raise ValueError(
                f"Candidate at index {index} requires object_type and identifier."
            )
        normalized.append(dict(candidate))
    return normalized


def _load_secret_inputs(path: str | Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    secret_path = Path(path)
    mode = stat.S_IMODE(secret_path.stat().st_mode)
    if mode & 0o077:
        raise ValueError(
            f"Secret input file must not be group/world accessible (mode={mode:04o})."
        )
    value = _load_json(secret_path)
    if not isinstance(value, Mapping):
        raise ValueError("Secret input file must be a candidate-key -> secret-object mapping.")
    out: dict[str, dict[str, str]] = {}
    for key, secrets in value.items():
        if not isinstance(secrets, Mapping):
            raise ValueError(f"Secret bundle for {key!r} must be a JSON object.")
        out[str(key)] = {str(name): str(secret) for name, secret in secrets.items()}
    return out


def _validate_lab_owned_candidates(
    candidates: Iterable[Mapping[str, Any]],
    *,
    lab_prefix: str,
    lab_vlan_ids: set[int],
) -> None:
    if not lab_prefix or len(lab_prefix) < 6:
        raise ValueError("lab_prefix must contain at least six characters.")
    for candidate in candidates:
        object_type = str(candidate.get("object_type"))
        identifier = str(candidate.get("identifier"))
        if object_type == "vlan":
            try:
                vlan_id = int(identifier)
            except ValueError as exc:
                raise ValueError(f"VLAN identifier must be numeric: {identifier!r}.") from exc
            if vlan_id not in lab_vlan_ids:
                raise ValueError(
                    f"VLAN {vlan_id} is not declared lab-owned with --lab-vlan-id."
                )
            continue
        if not identifier.startswith(lab_prefix):
            raise ValueError(
                f"{_candidate_key(candidate)} is not lab-owned; identifier must start "
                f"with {lab_prefix!r}."
            )


def _build_adapter(
    target: LabTarget,
    *,
    secret_inputs: Mapping[str, Mapping[str, str]] | None = None,
) -> ClassicCentralAdapter | NewCentralAdapter:
    context = TargetContext(
        target_type=target.target_type,
        scope_id=target.scope_id,
        scope_name=target.scope_name,
        persona=target.persona,
        cluster_name=target.cluster_name,
        cluster_scope_id=target.cluster_scope_id,
        conflict_policy=ConflictPolicy.FAIL,
        secret_inputs=secret_inputs or {},
    )
    is_classic = target.target_type is TargetType.CLASSIC_CENTRAL
    adapter_class = ClassicCentralAdapter if is_classic else NewCentralAdapter
    resolver = (
        aos8_tools._aos8_migration_classic_target_resolver
        if is_classic
        else aos8_tools._aos8_migration_scope_resolver
    )
    return adapter_class(
        context,
        scope_resolver=resolver,
        persona_validator=aos8_tools._aos8_migration_persona_validator,
        read_invoker=aos8_tools._aos8_migration_read_invoker,
        write_invoker=aos8_tools._aos8_migration_write_invoker,
        writes_enabled=lambda _target: shared_tools.platform_writes_allowed("central"),
    )


def _preview_digest_payload(
    target: LabTarget,
    candidates: Sequence[Mapping[str, Any]],
    preview: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "target": target.as_dict(),
        "candidates": [dict(candidate) for candidate in candidates],
        "preview": dict(preview),
    }


def _actions_by_key(
    adapter: ClassicCentralAdapter | NewCentralAdapter,
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, CandidateAction]:
    return {
        _candidate_key(candidate): adapter.candidate_action(candidate)
        for candidate in candidates
    }


def _cleanup_operations(action: CandidateAction) -> list[Operation]:
    if action.delete_operations is not None:
        return list(action.delete_operations)
    return list(action.rollback_operations)


def _validate_write_preview(
    adapter: ClassicCentralAdapter | NewCentralAdapter,
    candidates: Sequence[Mapping[str, Any]],
    preview: Mapping[str, Any],
) -> None:
    actions = _actions_by_key(adapter, candidates)
    preview_rows = {
        str(row.get("candidate")): row
        for row in preview.get("operations", [])
        if isinstance(row, Mapping)
    }
    errors: list[str] = []
    for key, action in actions.items():
        row = preview_rows.get(key)
        if row is None:
            errors.append(f"{key}: missing preview row")
            continue
        if row.get("status") != "ready" or row.get("conflict") != "absent":
            errors.append(
                f"{key}: requires status=ready and conflict=absent, got "
                f"status={row.get('status')!r}, conflict={row.get('conflict')!r}"
            )
        if action.dry_run_only:
            errors.append(f"{key}: action is dry-run-only")
        if not _cleanup_operations(action):
            errors.append(f"{key}: no verified delete/cleanup operation is available")
    if errors:
        raise ValueError("Controlled write plan is not executable:\n- " + "\n- ".join(errors))


def prepare_write_plan(
    target: LabTarget,
    candidates: Sequence[Mapping[str, Any]],
    *,
    lab_prefix: str = DEFAULT_LAB_PREFIX,
    lab_vlan_ids: set[int] | None = None,
) -> dict[str, Any]:
    _validate_lab_owned_candidates(
        candidates,
        lab_prefix=lab_prefix,
        lab_vlan_ids=set(lab_vlan_ids or ()),
    )
    adapter = _build_adapter(target)
    preview = adapter.preview(candidates)
    _validate_write_preview(adapter, candidates, preview)
    digest_payload = _preview_digest_payload(target, candidates, preview)
    return {
        **digest_payload,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "preview_sha256": _digest(digest_payload),
        "lab_prefix": lab_prefix,
        "lab_vlan_ids": sorted(lab_vlan_ids or ()),
        "secrets_included": False,
        "operator_maps_persisted": False,
        "cleanup_required": True,
        "warning": "Local lab artifact; review exact target and payloads and do not commit it.",
    }


def _explicit_central_write_gate_enabled() -> bool:
    return os.getenv("HPE_MCP_CENTRAL_WRITES", "").strip().lower() in TRUTHY


def _cleanup_candidates(
    adapter: ClassicCentralAdapter | NewCentralAdapter,
    candidates: Sequence[Mapping[str, Any]],
    attempted_keys: set[str],
) -> list[dict[str, Any]]:
    actions = _actions_by_key(adapter, candidates)
    results: list[dict[str, Any]] = []
    for candidate in reversed(candidates):
        key = _candidate_key(candidate)
        if key not in attempted_keys:
            continue
        action = actions[key]
        operation_results: list[dict[str, Any]] = []
        errors: list[str] = []
        for operation in _cleanup_operations(action):
            invoked = operation.with_dry_run(False)
            try:
                value = aos8_tools._aos8_migration_write_invoker(
                    invoked, confirmation=True
                )
            except Exception as exc:
                errors.append(f"{operation.name}: {exc}")
                break
            operation_results.append(
                {"operation": invoked.preview_dict(), "result": value}
            )
        try:
            post_cleanup = adapter.preview(
                [candidate],
                selected={key},
                include_dependency_closure=False,
                allow_unresolved_blockers=True,
            )
            row = next(
                (
                    item
                    for item in post_cleanup.get("operations", [])
                    if item.get("candidate") == key
                ),
                {},
            )
            deleted = row.get("conflict") == "absent"
            if not deleted:
                errors.append(
                    "post-cleanup read did not confirm target absence "
                    f"(status={row.get('status')!r}, conflict={row.get('conflict')!r})"
                )
        except Exception as exc:
            deleted = False
            errors.append(f"post-cleanup verification failed: {exc}")
        results.append(
            {
                "candidate": key,
                "status": "cleaned" if not errors and deleted else "cleanup_failed",
                "results": operation_results,
                "errors": errors,
            }
        )
    return results


def execute_write_plan(
    artifact: Mapping[str, Any],
    *,
    confirm_digest: str,
    confirm_target: str,
    allow_lab_writes: bool,
    cleanup_after_write: bool,
    secret_inputs: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    if int(artifact.get("schema_version", 0)) != PLAN_SCHEMA_VERSION:
        raise ValueError("Unsupported write-plan schema version.")
    expected_digest = str(artifact.get("preview_sha256") or "")
    if not expected_digest or confirm_digest != expected_digest:
        raise PermissionError("Exact --confirm-digest value from the reviewed plan is required.")
    target_data = artifact.get("target")
    if not isinstance(target_data, Mapping):
        raise ValueError("Write plan has no target object.")
    target = LabTarget(
        target_type=TargetType(str(target_data["target_type"])),
        scope_name=str(target_data["scope_name"]),
        scope_id=(
            str(target_data["scope_id"]) if target_data.get("scope_id") is not None else None
        ),
        persona=str(target_data["persona"]),
        cluster_name=(
            str(target_data["cluster_name"])
            if target_data.get("cluster_name") is not None
            else None
        ),
        cluster_scope_id=(
            str(target_data["cluster_scope_id"])
            if target_data.get("cluster_scope_id") is not None
            else None
        ),
    )
    if confirm_target != target.confirmation_value():
        raise PermissionError("Exact --confirm-target value from the reviewed plan is required.")
    if not allow_lab_writes:
        raise PermissionError("--allow-lab-writes is required.")
    if not cleanup_after_write:
        raise PermissionError("--cleanup-after-write is mandatory for controlled lab writes.")
    if not _explicit_central_write_gate_enabled():
        raise PermissionError(
            "Set HPE_MCP_CENTRAL_WRITES=1 explicitly for controlled lab writes."
        )

    raw_candidates = artifact.get("candidates")
    if not isinstance(raw_candidates, list):
        raise ValueError("Write plan has no candidate list.")
    candidates = [dict(item) for item in raw_candidates if isinstance(item, Mapping)]
    if len(candidates) != len(raw_candidates):
        raise ValueError("Write plan candidate list contains a non-object value.")
    _validate_lab_owned_candidates(
        candidates,
        lab_prefix=str(artifact.get("lab_prefix") or DEFAULT_LAB_PREFIX),
        lab_vlan_ids={int(value) for value in artifact.get("lab_vlan_ids", [])},
    )

    adapter = _build_adapter(target, secret_inputs=secret_inputs)
    fresh_preview = adapter.preview(candidates)
    _validate_write_preview(adapter, candidates, fresh_preview)
    fresh_payload = _preview_digest_payload(target, candidates, fresh_preview)
    fresh_digest = _digest(fresh_payload)
    if fresh_digest != expected_digest:
        raise PermissionError(
            "The live preview changed after review; generate and review a new plan."
        )

    apply_result: dict[str, Any] | None = None
    attempted_keys: set[str] = set()
    cleanup_result: list[dict[str, Any]] = []
    apply_error: str | None = None
    try:
        apply_result = adapter.execute(
            candidates,
            dry_run=False,
            confirmation=True,
        )
        for row in apply_result.get("results", []):
            if row.get("status") == "applied" or row.get("results"):
                attempted_keys.add(str(row.get("candidate")))
        failed = [
            row
            for row in apply_result.get("results", [])
            if row.get("status") not in {"applied", "skipped"}
        ]
        if failed:
            apply_error = "One or more controlled writes failed."
    except Exception as exc:
        apply_error = str(exc)
        attempted_keys.update(_candidate_key(candidate) for candidate in candidates)
    finally:
        cleanup_result = _cleanup_candidates(adapter, candidates, attempted_keys)

    cleanup_failed = any(row["status"] != "cleaned" for row in cleanup_result)
    return {
        "target": target.as_dict(),
        "preview_sha256": expected_digest,
        "apply": apply_result,
        "apply_error": apply_error,
        "cleanup": cleanup_result,
        "cleanup_complete": not cleanup_failed,
        "secrets_persisted": False,
        "operator_maps_persisted": False,
        "status": (
            "completed_and_cleaned"
            if apply_error is None and not cleanup_failed
            else "failed_or_cleanup_incomplete"
        ),
    }


def _family_counts(export: Mapping[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key, value in export.items():
        if isinstance(value, list):
            counts[str(key)] = len(value)
        elif isinstance(value, Mapping):
            nested = [item for item in value.values() if isinstance(item, list)]
            if nested:
                counts[str(key)] = sum(len(item) for item in nested)
    return dict(sorted(counts.items()))


async def collect_aos8_readonly_evidence(
    *,
    config_path: str,
    limit: int,
    max_items_per_type: int,
) -> dict[str, Any]:
    status = aos8_tools.aos8_status()
    if not status.get("configured"):
        return {"coverage": "blocked", "blockers": ["AOS8 is not configured."]}

    observed_methods: list[str] = []
    original_dispatch = aos8_tools._aos8_dispatch

    async def guarded_dispatch(
        client: Any,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any],
        body: Any,
    ) -> Any:
        verb = str(method).upper()
        observed_methods.append(verb)
        if verb != "GET":
            raise RuntimeError(f"Blocked non-GET AOS8 export request: {verb}.")
        return await original_dispatch(client, method, url, headers, params, body)

    aos8_tools._aos8_dispatch = guarded_dispatch
    try:
        login = await aos8_tools.aos8_login()
        if login.get("error"):
            return {"coverage": "blocked", "blockers": [str(login["error"])]}
        export = await aos8_tools.aos8_export_all(
            config_path=config_path,
            limit=limit,
            max_items_per_type=max_items_per_type,
        )
    finally:
        aos8_tools._aos8_dispatch = original_dispatch
        await aos8_tools.aos8_logout()

    redacted = shared_tools.redact_sensitive(export)
    return {
        "coverage": "live_read_only",
        "config_path_hash": baseline._sanitize_identifier(config_path),
        "family_counts": _family_counts(redacted if isinstance(redacted, Mapping) else {}),
        "sanitized_export_sha256": _digest(redacted),
        "warning_count": len(export.get("warnings", [])),
        "observed_export_methods": sorted(set(observed_methods)),
        "secrets_persisted": False,
    }


def collect_central_readonly_evidence(
    target: LabTarget,
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    try:
        # Client construction may perform the OAuth token POST needed to
        # authenticate. The data-plane request guard is installed immediately
        # afterward and permits only GET/HEAD/OPTIONS against Central APIs.
        client = shared_tools.get_client()
    except Exception as exc:
        return {"coverage": "blocked", "blockers": [f"Central auth failed: {exc}"]}
    observed: list[str] = []
    baseline._install_get_only_request_guard(client, observed)
    baseline._install_no_token_refresh_guard(client)
    adapter = _build_adapter(target)
    preview = adapter.preview(candidates)
    rows = [
        {
            "candidate": row.get("candidate"),
            "status": row.get("status"),
            "conflict": row.get("conflict"),
            "dry_run_only": row.get("dry_run_only"),
        }
        for row in preview.get("operations", [])
        if isinstance(row, Mapping)
    ]
    return {
        "coverage": "live_get_only",
        "target": {
            **target.as_dict(),
            "scope_name": baseline._sanitize_identifier(target.scope_name),
            "scope_id": (
                baseline._sanitize_identifier(target.scope_id)
                if target.scope_id
                else None
            ),
        },
        "results": rows,
        "observed_http_verbs": sorted(set(observed)),
        "oauth_bootstrap_post_allowed": True,
        "preview_sha256": _digest(preview),
        "secrets_persisted": False,
    }


def _write_output(value: Any, output: str | None) -> None:
    rendered = json.dumps(shared_tools.redact_sensitive(value), indent=2, sort_keys=True)
    if output:
        Path(output).write_text(rendered + "\n")
    else:
        print(rendered)


def _target_from_args(args: argparse.Namespace) -> LabTarget:
    if not args.target_type or not args.scope_name or not args.persona:
        raise ValueError("--target-type, --scope-name, and --persona are required.")
    return LabTarget(
        target_type=TargetType(args.target_type),
        scope_name=args.scope_name,
        scope_id=args.scope_id,
        persona=args.persona,
        cluster_name=args.cluster_name,
        cluster_scope_id=args.cluster_scope_id,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--live-aos8-readonly", action="store_true")
    mode.add_argument("--live-central-readonly", action="store_true")
    mode.add_argument("--prepare-write-plan", action="store_true")
    mode.add_argument("--execute-write-plan")
    parser.add_argument("--output")
    parser.add_argument("--candidates")
    parser.add_argument(
        "--target-type",
        choices=(TargetType.CLASSIC_CENTRAL.value, TargetType.NEW_CENTRAL.value),
    )
    parser.add_argument("--scope-name")
    parser.add_argument("--scope-id")
    parser.add_argument("--persona")
    parser.add_argument("--cluster-name")
    parser.add_argument("--cluster-scope-id")
    parser.add_argument("--config-path", default="/md")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-items-per-type", type=int, default=1000)
    parser.add_argument("--lab-prefix", default=DEFAULT_LAB_PREFIX)
    parser.add_argument("--lab-vlan-id", type=int, action="append", default=[])
    parser.add_argument("--secret-inputs")
    parser.add_argument("--confirm-digest")
    parser.add_argument("--confirm-target")
    parser.add_argument("--allow-lab-writes", action="store_true")
    parser.add_argument("--cleanup-after-write", action="store_true")
    args = parser.parse_args(argv)

    if args.offline:
        result = baseline._build_report(live_new_central_readonly=False)
    elif args.live_aos8_readonly:
        result = asyncio.run(
            collect_aos8_readonly_evidence(
                config_path=args.config_path,
                limit=args.limit,
                max_items_per_type=args.max_items_per_type,
            )
        )
    elif args.live_central_readonly:
        if not args.candidates:
            raise ValueError("--candidates is required for live Central evidence.")
        result = collect_central_readonly_evidence(
            _target_from_args(args),
            _load_candidates(args.candidates),
        )
    elif args.prepare_write_plan:
        if not args.candidates:
            raise ValueError("--candidates is required to prepare a write plan.")
        result = prepare_write_plan(
            _target_from_args(args),
            _load_candidates(args.candidates),
            lab_prefix=args.lab_prefix,
            lab_vlan_ids=set(args.lab_vlan_id),
        )
    else:
        artifact = _load_json(args.execute_write_plan)
        if not isinstance(artifact, Mapping):
            raise ValueError("Write-plan artifact must be a JSON object.")
        result = execute_write_plan(
            artifact,
            confirm_digest=args.confirm_digest or "",
            confirm_target=args.confirm_target or "",
            allow_lab_writes=args.allow_lab_writes,
            cleanup_after_write=args.cleanup_after_write,
            secret_inputs=_load_secret_inputs(args.secret_inputs),
        )

    _write_output(result, args.output)
    return 0 if result.get("status") != "failed_or_cleanup_incomplete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
