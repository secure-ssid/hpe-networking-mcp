from __future__ import annotations

import importlib
from typing import Any

import pytest

module = importlib.import_module("scripts.evaluate_aos8_050_readonly")


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.token_manager = type("TokenManager", (), {"_refresh_token": lambda self: None})()

    def _request(self, method: str, endpoint: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((method.upper(), endpoint))
        return {"method": method.upper(), "endpoint": endpoint}


def test_offline_report_never_touches_get_client(monkeypatch: pytest.MonkeyPatch):
    def fail_get_client() -> Any:
        raise AssertionError("offline path must not call get_client")

    monkeypatch.setattr(module.shared_tools, "get_client", fail_get_client)
    report = module._build_report(live_new_central_readonly=False)
    assert report["metadata"]["mode"] == "offline_fixture_backed"
    assert report["coverage_classification"]["new_central"] == "fixture_backed"
    assert report["observed_http_verbs"] == []


def test_get_only_guard_blocks_non_get_before_transmission():
    client = FakeClient()
    observed: list[str] = []
    module._install_get_only_request_guard(client, observed)

    with pytest.raises(RuntimeError, match="Blocked non-read-only HTTP method"):
        client._request("POST", "/blocked")
    assert client.calls == []

    response = client._request("GET", "/allowed")
    assert response["method"] == "GET"
    assert client.calls == [("GET", "/allowed")]
    assert observed == ["POST", "GET"]


def test_sanitize_identifier_is_deterministic_and_non_revealing():
    first = module._sanitize_identifier("branch-site-001")
    second = module._sanitize_identifier("branch-site-001")
    third = module._sanitize_identifier("branch-site-002")

    assert first == second
    assert first != third
    assert "branch-site-001" not in first
    assert first.startswith("sha256:")


def test_offline_run_does_not_persist_state_files():
    before = module._snapshot_state_dir()
    module._run_offline()
    after = module._snapshot_state_dir()
    assert before == after == []


def test_offline_report_contains_required_sections():
    report = module._build_report(live_new_central_readonly=False)

    required_sections = {
        "metadata",
        "coverage_classification",
        "results",
        "observed_http_verbs",
        "secret_leak_assertion",
        "no_write_confirmation",
        "state_aos8_migrations",
    }
    assert required_sections.issubset(report)
    assert {"commit_sha", "version", "mode"}.issubset(report["metadata"])
    assert {"new_central", "classic_central", "aos8_source"}.issubset(
        report["coverage_classification"]
    )
    assert any(row["family"] == "wlan" for row in report["results"])
    assert report["secret_leak_assertion"]["passed"] is True
    assert report["no_write_confirmation"]["write_attempted"] is False
    assert report["state_aos8_migrations"]["unchanged"] is True


def test_live_new_central_readonly_end_to_end_is_guarded(monkeypatch: pytest.MonkeyPatch):
    client = FakeClient()
    monkeypatch.setattr(module, "_live_get_only_client_ready", lambda: (True, None))
    monkeypatch.setattr(module.shared_tools, "get_client", lambda: client)

    def fake_get_global_scope_id() -> dict[str, Any]:
        module.shared_tools.get_client()._request("GET", "/network-config/v1/scope-maps")
        return {"global_scope_id": "global-1", "errors": []}

    def fake_list_scopes(*, full_list: bool = False, **_: Any) -> dict[str, Any]:
        module.shared_tools.get_client()._request("GET", "/network-config/v1/scopes")
        return {
            "items": [
                {"scope_id": "global-1", "scope_name": "Global", "scope_type": "GLOBAL"}
            ]
        }

    def fake_preview_migration_run(
        *,
        target_type: str,
        candidates: list[dict[str, Any]],
        scope_name: str,
        persona: str,
        conflict_policy: str,
        **_: Any,
    ) -> dict[str, Any]:
        assert target_type == "new_central"
        assert scope_name == "Global"
        assert conflict_policy == "fail"
        module.shared_tools.get_client()._request("GET", f"/preview/{persona}")
        return {
            "operations": [
                {
                    "candidate": module._candidate_key(candidate),
                    "status": "ready",
                    "conflict": "absent",
                    "dry_run_only": False,
                }
                for candidate in candidates
            ]
        }

    def _fail_create_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("create_run should not be called")

    def _fail_apply_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("apply_run should not be called")

    monkeypatch.setattr(module.monitoring_tools, "get_global_scope_id", fake_get_global_scope_id)
    monkeypatch.setattr(module.monitoring_tools, "list_scopes", fake_list_scopes)
    monkeypatch.setattr(module.aos8_tools, "aos8_preview_migration_run", fake_preview_migration_run)
    monkeypatch.setattr(module.aos8_tools, "aos8_create_migration_run", _fail_create_run)
    monkeypatch.setattr(module.aos8_tools, "aos8_apply_migration_run", _fail_apply_run)

    report = module._build_report(live_new_central_readonly=True)

    assert report["coverage_classification"]["new_central"] == "live_get_only"
    assert report["observed_http_verbs"] == ["GET"]
    assert report["blockers"] == []
    assert report["no_write_confirmation"]["called_aos8_create_migration_run"] is False
    assert report["no_write_confirmation"]["called_aos8_apply_migration_run"] is False
    assert client.calls
    assert all(method == "GET" for method, _ in client.calls)
