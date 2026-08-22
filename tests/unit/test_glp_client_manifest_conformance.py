"""Regression tests for the audited GLPClient defects.

Covers:

- ``poll_task`` crashed on a non-string/missing ``status``, ignored the
  ``state`` spelling some GLP services use, spun silently until timeout on an
  unrecognized status, and reported terminal failures without any detail.
- Audit-log helpers hit ``/audit-log/v1/logs`` and
  ``/audit-log/v1/logs/{id}/detail``, neither of which exists in the committed
  manifest (``src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/glp.json``), and passed
  ``category`` as a query parameter when getAuditLogs documents it as a
  *filter field*.
- Guarded GLP writes had no server-side dry-run even where the manifest
  declares one (patchDevicesV2beta1 ``dry-run``).
- GLP write gating was entangled with Central's gate.

No network calls, no writes.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hpe_networking_mcp.pipeline.clients.glp_client import (
    _V2BETA1_WRITES_FLAG,
    AUDIT_LOG_BASE_PATH,
    GLPClient,
    _audit_log_filter,
    _extract_task_status,
    glp_write_gate_message,
)

MANIFEST = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/glp.json"
    ).read_text(encoding="utf-8")
)
MANIFEST_KEYS = {op["key"] for op in MANIFEST["operations"]}


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv(_V2BETA1_WRITES_FLAG, raising=False)
    monkeypatch.delenv("HPE_MCP_CENTRAL_WRITES", raising=False)
    yield
    monkeypatch.delenv(_V2BETA1_WRITES_FLAG, raising=False)


@pytest.fixture
def writes_on(monkeypatch):
    monkeypatch.setenv(_V2BETA1_WRITES_FLAG, "1")


def _client():
    glp = GLPClient.__new__(GLPClient)
    glp.workspace_id = "ws"
    glp._device_id_cache = {}
    inner = MagicMock()
    glp._client = inner
    return glp, inner


def _patch_response(status_code=202, headers=None, body=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.text = text
    resp.json.return_value = body if body is not None else {}
    return resp


# ---------------------------------------------------------------------------
# poll_task
# ---------------------------------------------------------------------------


class TestExtractTaskStatus:
    def test_reads_status(self):
        assert _extract_task_status({"status": "SUCCEEDED"}) == "succeeded"

    def test_falls_back_to_state(self):
        assert _extract_task_status({"state": "RUNNING"}) == "running"

    def test_status_wins_over_state(self):
        assert _extract_task_status({"status": "FAILED", "state": "RUNNING"}) == "failed"

    def test_blank_status_falls_through_to_state(self):
        assert _extract_task_status({"status": "  ", "state": "RUNNING"}) == "running"

    def test_missing_fields_are_none(self):
        assert _extract_task_status({"transactionId": "t1"}) is None

    def test_non_dict_is_none(self):
        assert _extract_task_status(["SUCCEEDED"]) is None
        assert _extract_task_status(None) is None

    def test_non_string_scalar_is_coerced(self):
        assert _extract_task_status({"status": 200}) == "200"

    def test_nested_object_status_is_none(self):
        assert _extract_task_status({"status": {"code": "OK"}}) is None


class TestPollTask:
    def test_succeeded_terminal_state_returns_payload(self):
        glp, inner = _client()
        inner.get.return_value = {"status": "SUCCEEDED", "id": "t1"}

        assert glp.poll_task("t1", timeout=5, interval=0)["id"] == "t1"

    def test_state_key_is_honoured(self):
        """Regression: a payload using ``state`` polled until timeout."""
        glp, inner = _client()
        inner.get.return_value = {"state": "SUCCEEDED"}

        assert glp.poll_task("t1", timeout=5, interval=0) == {"state": "SUCCEEDED"}

    def test_terminal_failure_includes_detail(self):
        glp, inner = _client()
        inner.get.return_value = {
            "status": "FAILED",
            "error": {"message": "serial already claimed"},
        }

        with pytest.raises(RuntimeError) as exc:
            glp.poll_task("t1", timeout=5, interval=0)

        assert "'failed'" in str(exc.value)
        assert "serial already claimed" in str(exc.value)

    def test_timeout_terminal_state_is_a_failure(self):
        glp, inner = _client()
        inner.get.return_value = {"status": "TIMEOUT"}

        with pytest.raises(RuntimeError, match="terminal state 'timeout'"):
            glp.poll_task("t1", timeout=5, interval=0)

    def test_malformed_payload_raises_immediately(self):
        """Regression: ``result.get("status", "").lower()`` blew up on a
        non-string status, and a status-less payload looked 'in progress'."""
        glp, inner = _client()
        inner.get.return_value = {"transactionId": "t1", "progress": 40}

        with pytest.raises(RuntimeError, match="malformed response"):
            glp.poll_task("t1", timeout=5, interval=0)
        assert inner.get.call_count == 1

    def test_non_dict_payload_raises(self):
        glp, inner = _client()
        inner.get.return_value = ["nope"]

        with pytest.raises(RuntimeError, match="malformed response"):
            glp.poll_task("t1", timeout=5, interval=0)

    def test_nested_status_object_raises_instead_of_crashing(self):
        glp, inner = _client()
        inner.get.return_value = {"status": {"code": "RUNNING"}}

        with pytest.raises(RuntimeError, match="malformed response"):
            glp.poll_task("t1", timeout=5, interval=0)

    def test_in_progress_then_success(self, monkeypatch):
        glp, inner = _client()
        monkeypatch.setattr(
            "hpe_networking_mcp.pipeline.clients.glp_client.time.sleep", lambda s: None
        )
        inner.get.side_effect = [
            {"status": "INITIALIZED"},
            {"status": "RUNNING"},
            {"status": "SUCCEEDED", "done": True},
        ]

        assert glp.poll_task("t1", timeout=300, interval=1)["done"] is True
        assert inner.get.call_count == 3

    def test_unknown_status_warns_and_surfaces_in_timeout(self, monkeypatch, caplog):
        """An unrecognized in-flight status stays forward compatible (still
        polled) but must be named in the timeout error, not swallowed."""
        import logging

        glp, inner = _client()
        monkeypatch.setattr(
            "hpe_networking_mcp.pipeline.clients.glp_client.time.sleep", lambda s: None
        )
        inner.get.return_value = {"status": "QUIESCING"}

        with caplog.at_level(
            logging.WARNING, logger="hpe_networking_mcp.pipeline.clients.glp_client"
        ):
            with pytest.raises(RuntimeError) as exc:
                glp.poll_task("t1", timeout=1, interval=1)

        assert "last status='quiescing'" in str(exc.value)
        unknown = [r for r in caplog.records if "unrecognized status" in r.message]
        assert len(unknown) == 1, "unknown status should warn exactly once"

    def test_always_polls_at_least_once(self):
        glp, inner = _client()
        inner.get.return_value = {"status": "SUCCEEDED"}

        assert glp.poll_task("t1", timeout=0, interval=0) == {"status": "SUCCEEDED"}


# ---------------------------------------------------------------------------
# Audit-log manifest conformance
# ---------------------------------------------------------------------------


class TestAuditLogManifestConformance:
    def test_manifest_has_only_v2beta1_audit_paths(self):
        audit = {key for key in MANIFEST_KEYS if "/audit-log" in key}
        assert audit == {
            "GET /audit-log/v2beta1/logs",
            "GET /audit-log/v2beta1/logs/{id}",
            "GET /audit-log/v2beta1/logs/{id}/details",
        }
        assert AUDIT_LOG_BASE_PATH == "/audit-log/v2beta1/logs"

    def test_manifest_list_op_has_no_category_query_param(self):
        op = next(o for o in MANIFEST["operations"] if o["key"] == "GET /audit-log/v2beta1/logs")
        names = {p["name"] for p in op["parameters"]}
        assert "category" not in names
        assert {"filter", "select", "limit", "offset", "sort"} <= names

    def test_list_audit_logs_uses_manifest_path(self):
        glp, inner = _client()
        inner.get.return_value = {"items": [{"id": "a1"}]}

        assert glp.list_audit_logs() == [{"id": "a1"}]
        inner.get.assert_called_once_with(
            AUDIT_LOG_BASE_PATH, params={"limit": 100, "offset": 0}
        )

    def test_category_becomes_a_filter_expression(self):
        glp, inner = _client()
        inner.get.return_value = {"items": []}

        glp.list_audit_logs(category="User Management")

        params = inner.get.call_args.kwargs["params"]
        assert params["filter"] == "category eq 'User Management'"
        assert "category" not in params

    def test_category_and_filter_are_anded(self):
        glp, inner = _client()
        inner.get.return_value = {"items": []}

        glp.list_audit_logs(category="Device Management", filter="username eq 'a@b.c'")

        assert inner.get.call_args.kwargs["params"]["filter"] == (
            "category eq 'Device Management' and (username eq 'a@b.c')"
        )

    def test_select_and_sort_are_forwarded(self):
        glp, inner = _client()
        inner.get.return_value = {"items": []}

        glp.list_audit_logs(select="createdAt,category", sort="createdAt desc")

        params = inner.get.call_args.kwargs["params"]
        assert params["select"] == "createdAt,category"
        assert params["sort"] == "createdAt desc"

    def test_no_filter_key_when_nothing_supplied(self):
        assert _audit_log_filter(None, None) is None

    def test_filter_only_is_passed_through_unwrapped(self):
        assert _audit_log_filter(None, "region eq 'us-west'") == "region eq 'us-west'"

    def test_category_quote_is_escaped(self):
        """OData literals escape a quote by doubling it — a category value
        containing one must not terminate the literal."""
        assert _audit_log_filter("O'Brien", None) == "category eq 'O''Brien'"

    def test_detail_path_is_plural_details(self):
        glp, inner = _client()
        inner.get.return_value = {"id": "a1"}

        glp.get_audit_log_detail("a1")

        inner.get.assert_called_once_with("/audit-log/v2beta1/logs/a1/details")

    def test_single_entry_path(self):
        glp, inner = _client()
        inner.get.return_value = {"id": "a1"}

        glp.get_audit_log("a1")

        inner.get.assert_called_once_with("/audit-log/v2beta1/logs/a1")

    def test_v2beta1_alias_matches_the_unversioned_helper(self):
        glp, inner = _client()
        inner.get.return_value = {"items": []}

        glp.list_audit_logs_v2beta1(category="X")
        v2_params = inner.get.call_args.kwargs["params"]
        inner.get.reset_mock()
        glp.list_audit_logs(category="X")

        assert inner.get.call_args.kwargs["params"] == v2_params


# ---------------------------------------------------------------------------
# Server-side dry-run on the manifest-declared write
# ---------------------------------------------------------------------------


class TestDevicePatchDryRun:
    def test_manifest_declares_dry_run_on_the_device_patch(self):
        op = next(o for o in MANIFEST["operations"] if o["key"] == "PATCH /devices/v2beta1/devices")
        dry = next(p for p in op["parameters"] if p["name"] == "dry-run")
        assert dry["in"] == "query"
        assert dry["type"] == "boolean"
        assert dry["default"] is False

    def test_dry_run_adds_the_hyphenated_query_param(self, clean_env, writes_on):
        glp, inner = _client()
        inner.get.return_value = {"items": [{"id": "uuid-1"}]}
        inner._request.return_value = _patch_response(200, body={"validated": True})

        result = glp.archive_device("SERIAL1", dry_run=True)

        assert inner._request.call_args.kwargs["params"] == {"id": "uuid-1", "dry-run": "true"}
        assert result["status"] == "dry_run"
        assert result["dry_run"] is True
        assert result["request_body"] == {"archived": True}
        assert result["response"] == {"validated": True}

    def test_dry_run_does_not_poll_a_nonexistent_async_op(self, clean_env, writes_on):
        glp, inner = _client()
        inner.get.return_value = {"items": [{"id": "uuid-1"}]}
        inner._request.return_value = _patch_response(
            202, headers={"Location": "/devices/v1/async-operations/task-1"}
        )
        glp.poll_task = MagicMock(side_effect=AssertionError("dry-run must not poll"))

        assert glp.archive_device("SERIAL1", dry_run=True)["status"] == "dry_run"

    def test_default_is_still_a_real_write(self, clean_env, writes_on):
        glp, inner = _client()
        inner.get.return_value = {"items": [{"id": "uuid-1"}]}
        inner._request.return_value = _patch_response(
            202, headers={"Location": "/devices/v1/async-operations/task-1"}
        )
        glp.poll_task = MagicMock(return_value={"status": "SUCCEEDED"})

        result = glp.archive_device("SERIAL1")

        assert inner._request.call_args.kwargs["params"] == {"id": "uuid-1"}
        assert result == {"status": "SUCCEEDED"}

    @pytest.mark.parametrize(
        "call,expected_body",
        [
            (lambda g: g.unarchive_device("S", dry_run=True), {"archived": False}),
            (lambda g: g.unassign_subscription("S", dry_run=True), {"subscription": []}),
            (
                lambda g: g.assign_subscription(
                    "S", "123e4567-e89b-12d3-a456-426614174000", dry_run=True
                ),
                {"subscription": [{"id": "123e4567-e89b-12d3-a456-426614174000"}]},
            ),
        ],
    )
    def test_all_device_patch_wrappers_support_dry_run(
        self, clean_env, writes_on, call, expected_body
    ):
        glp, inner = _client()
        inner.get.return_value = {"items": [{"id": "uuid-1"}]}
        inner._request.return_value = _patch_response(200, body={})

        result = call(glp)

        assert result["status"] == "dry_run"
        assert result["request_body"] == expected_body
        assert inner._request.call_args.kwargs["params"]["dry-run"] == "true"

    def test_dry_run_still_requires_the_write_flag(self, clean_env):
        """The validation request reaches the tenant, so it stays gated."""
        glp, inner = _client()

        with pytest.raises(NotImplementedError) as exc:
            glp.archive_device("SERIAL1", dry_run=True)

        assert _V2BETA1_WRITES_FLAG in str(exc.value)
        assert "dry_run=True" in str(exc.value)
        inner._request.assert_not_called()


# ---------------------------------------------------------------------------
# Gate independence
# ---------------------------------------------------------------------------


class TestGlpGateIndependence:
    def test_central_flag_does_not_enable_glp_writes(self, clean_env, monkeypatch):
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
        glp, _ = _client()

        with pytest.raises(NotImplementedError):
            glp.archive_device("SERIAL1")

    def test_central_flag_off_does_not_block_glp_writes(self, clean_env, monkeypatch):
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "0")
        monkeypatch.setenv(_V2BETA1_WRITES_FLAG, "1")
        glp, inner = _client()
        inner.get.return_value = {"items": [{"id": "uuid-1"}]}
        inner._request.return_value = _patch_response(200, body={"ok": True})

        assert glp.archive_device("SERIAL1", dry_run=True)["status"] == "dry_run"

    def test_gate_message_names_only_the_glp_flag(self):
        message = glp_write_gate_message("glp_archive_device", "detail here")
        assert "HPE_MCP_GLP_V2BETA1_WRITES=1" in message
        assert "independent of HPE_MCP_CENTRAL_WRITES" in message
        assert "detail here" in message
