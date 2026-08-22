from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from hpe_networking_mcp.pipeline.aos8_target_adapters import CandidateAction, Operation, TargetType

module = importlib.import_module("scripts.evaluate_aos8_060_lab")


def candidate(identifier: str = "hpe-mcp-lab-wlan") -> dict[str, Any]:
    return {
        "object_type": "wlan",
        "identifier": identifier,
        "payload": {},
        "dependencies": [],
    }


def target() -> Any:
    return module.LabTarget(
        target_type=TargetType.NEW_CENTRAL,
        scope_name="Disposable Lab",
        scope_id="scope-1",
        persona="CAMPUS_AP",
    )


class FakeAdapter:
    def __init__(self, *, preview_status: str = "ready", conflict: str = "absent"):
        self.preview_status = preview_status
        self.conflict = conflict
        self.execute_calls: list[dict[str, Any]] = []

    def preview(self, candidates: list[dict[str, Any]], **_: Any) -> dict[str, Any]:
        return {
            "operations": [
                {
                    "candidate": module._candidate_key(item),
                    "status": self.preview_status,
                    "conflict": self.conflict,
                    "dry_run_only": False,
                }
                for item in candidates
            ]
        }

    def candidate_action(self, item: dict[str, Any]) -> CandidateAction:
        return CandidateAction(
            key=module._candidate_key(item),
            candidate=item,
            operations=[
                Operation(
                    invocation="tool",
                    name="create",
                    arguments={"dry_run": True},
                )
            ],
            delete_operations=[
                Operation(
                    invocation="tool",
                    name="delete",
                    arguments={"dry_run": True},
                )
            ],
        )

    def execute(
        self,
        candidates: list[dict[str, Any]],
        *,
        dry_run: bool,
        confirmation: bool,
    ) -> dict[str, Any]:
        self.execute_calls.append(
            {"dry_run": dry_run, "confirmation": confirmation}
        )
        return {
            "results": [
                {
                    "candidate": module._candidate_key(item),
                    "status": "applied",
                    "results": [{"operation": "create"}],
                }
                for item in candidates
            ]
        }


def test_lab_owned_validation_rejects_unprefixed_identifier():
    with pytest.raises(ValueError, match="not lab-owned"):
        module._validate_lab_owned_candidates(
            [candidate("production-wlan")],
            lab_prefix=module.DEFAULT_LAB_PREFIX,
            lab_vlan_ids=set(),
        )


def test_vlan_requires_explicit_lab_owned_allowlist():
    vlan = {"object_type": "vlan", "identifier": "4090"}
    with pytest.raises(ValueError, match="not declared lab-owned"):
        module._validate_lab_owned_candidates(
            [vlan],
            lab_prefix=module.DEFAULT_LAB_PREFIX,
            lab_vlan_ids=set(),
        )
    module._validate_lab_owned_candidates(
        [vlan],
        lab_prefix=module.DEFAULT_LAB_PREFIX,
        lab_vlan_ids={4090},
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_secret_input_file_requires_private_permissions(tmp_path: Path):
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"wlan:x": {"wpa_passphrase": "secret"}}))
    path.chmod(0o644)
    with pytest.raises(ValueError, match="group/world accessible"):
        module._load_secret_inputs(path)
    path.chmod(0o600)
    assert module._load_secret_inputs(path)["wlan:x"]["wpa_passphrase"] == "secret"


def test_prepare_plan_requires_absent_ready_and_cleanup(monkeypatch: pytest.MonkeyPatch):
    adapter = FakeAdapter()
    monkeypatch.setattr(module, "_build_adapter", lambda *args, **kwargs: adapter)

    artifact = module.prepare_write_plan(target(), [candidate()])

    assert artifact["cleanup_required"] is True
    assert artifact["secrets_included"] is False
    assert len(artifact["preview_sha256"]) == 64


def test_prepare_plan_rejects_existing_target(monkeypatch: pytest.MonkeyPatch):
    adapter = FakeAdapter(conflict="existing")
    monkeypatch.setattr(module, "_build_adapter", lambda *args, **kwargs: adapter)

    with pytest.raises(ValueError, match="conflict=absent"):
        module.prepare_write_plan(target(), [candidate()])


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"confirm_digest": "wrong"}, "confirm-digest"),
        ({"confirm_target": "wrong"}, "confirm-target"),
        ({"allow_lab_writes": False}, "allow-lab-writes"),
        ({"cleanup_after_write": False}, "cleanup-after-write"),
    ],
)
def test_execute_plan_requires_every_gate(
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, Any],
    message: str,
):
    adapter = FakeAdapter()
    monkeypatch.setattr(module, "_build_adapter", lambda *args, **kwargs: adapter)
    artifact = module.prepare_write_plan(target(), [candidate()])
    monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
    options = {
        "confirm_digest": artifact["preview_sha256"],
        "confirm_target": "Disposable Lab",
        "allow_lab_writes": True,
        "cleanup_after_write": True,
    }
    options.update(kwargs)

    with pytest.raises(PermissionError, match=message):
        module.execute_write_plan(artifact, **options)


def test_execute_plan_requires_explicit_env_gate(monkeypatch: pytest.MonkeyPatch):
    adapter = FakeAdapter()
    monkeypatch.setattr(module, "_build_adapter", lambda *args, **kwargs: adapter)
    artifact = module.prepare_write_plan(target(), [candidate()])
    monkeypatch.delenv("HPE_MCP_CENTRAL_WRITES", raising=False)

    with pytest.raises(PermissionError, match="HPE_MCP_CENTRAL_WRITES=1"):
        module.execute_write_plan(
            artifact,
            confirm_digest=artifact["preview_sha256"],
            confirm_target="Disposable Lab",
            allow_lab_writes=True,
            cleanup_after_write=True,
        )


def test_execute_plan_applies_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
):
    adapter = FakeAdapter()
    monkeypatch.setattr(module, "_build_adapter", lambda *args, **kwargs: adapter)
    artifact = module.prepare_write_plan(target(), [candidate()])
    monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
    writes: list[tuple[str, bool]] = []

    def fake_write(operation: Operation, *, confirmation: bool) -> dict[str, Any]:
        writes.append((operation.name, confirmation))
        return {"status_code": 200}

    monkeypatch.setattr(
        module.aos8_tools, "_aos8_migration_write_invoker", fake_write
    )

    result = module.execute_write_plan(
        artifact,
        confirm_digest=artifact["preview_sha256"],
        confirm_target="Disposable Lab",
        allow_lab_writes=True,
        cleanup_after_write=True,
    )

    assert result["status"] == "completed_and_cleaned"
    assert result["cleanup_complete"] is True
    assert writes == [("delete", True)]
    assert adapter.execute_calls == [{"dry_run": False, "confirmation": True}]


def test_changed_preview_invalidates_reviewed_digest(
    monkeypatch: pytest.MonkeyPatch,
):
    prepare_adapter = FakeAdapter()
    monkeypatch.setattr(
        module, "_build_adapter", lambda *args, **kwargs: prepare_adapter
    )
    artifact = module.prepare_write_plan(target(), [candidate()])
    changed_adapter = FakeAdapter(preview_status="blocked", conflict="existing")
    monkeypatch.setattr(
        module, "_build_adapter", lambda *args, **kwargs: changed_adapter
    )
    monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")

    with pytest.raises(ValueError, match="not executable"):
        module.execute_write_plan(
            artifact,
            confirm_digest=artifact["preview_sha256"],
            confirm_target="Disposable Lab",
            allow_lab_writes=True,
            cleanup_after_write=True,
        )


def test_aos8_unconfigured_evidence_is_blocked(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        module.aos8_tools, "aos8_status", lambda: {"configured": False}
    )
    result = asyncio_run(
        module.collect_aos8_readonly_evidence(
            config_path="/md",
            limit=10,
            max_items_per_type=20,
        )
    )
    assert result["coverage"] == "blocked"


def asyncio_run(awaitable: Any) -> Any:
    import asyncio

    return asyncio.run(awaitable)
