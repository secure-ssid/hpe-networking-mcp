from __future__ import annotations

import json

from hpe_networking_mcp.mcp_servers._middleware.audit_log import AuditLogMiddleware, audit_path


def test_audit_disabled_without_environment(monkeypatch):
    monkeypatch.delenv("HPE_MCP_AUDIT_LOG", raising=False)

    assert audit_path() is None


def test_audit_records_router_target_without_raw_arguments(tmp_path):
    path = tmp_path / "audit.jsonl"
    middleware = AuditLogMiddleware(
        path,
        target_resolver=lambda name, arguments: "create_ssid",
    )
    arguments = {
        "name": "create_ssid",
        "arguments": {
            "ssid": "employee",
            "password": "SuperSecret!",
            "token": "Bearer secret-token",
        },
    }

    middleware.before_call("invoke_tool", arguments)
    middleware.after_call("invoke_tool", arguments, {"status": "blocked"})

    text = path.read_text()
    record = json.loads(text)
    assert record["tool"] == "invoke_tool"
    assert record["target_tool"] == "create_ssid"
    assert record["outcome"] == "blocked"
    assert record["argument_keys"] == ["arguments", "name"]
    assert len(record["argument_digest"]) == 64
    assert record["duration_ms"] is not None
    assert "SuperSecret" not in text
    assert "secret-token" not in text
    assert "employee" not in text


def test_audit_never_records_unresolved_raw_target_name(tmp_path):
    path = tmp_path / "audit.jsonl"
    raw_target = "customer-secret-target-name"
    middleware = AuditLogMiddleware(path)
    arguments = {"name": raw_target, "arguments": {}}

    middleware.before_call("invoke_tool", arguments)
    middleware.after_call("invoke_tool", arguments, {"status": "blocked"})

    text = path.read_text()
    record = json.loads(text)
    assert record["target_tool"] == "unknown"
    assert raw_target not in text


def test_audit_records_exception_type_without_message(tmp_path):
    path = tmp_path / "audit.jsonl"
    middleware = AuditLogMiddleware(path)
    arguments = {"name": "delete_vlan", "arguments": {"password": "do-not-log"}}

    middleware.before_call("invoke_tool", arguments)
    middleware.on_error(
        "invoke_tool",
        arguments,
        RuntimeError("failure contains do-not-log"),
    )

    text = path.read_text()
    record = json.loads(text)
    assert record["outcome"] == "exception"
    assert record["error_type"] == "RuntimeError"
    assert "do-not-log" not in text


# ---------------------------------------------------------------------------
# v0.7: run/session correlation + write/destructive classification
# ---------------------------------------------------------------------------


def test_audit_records_include_stable_process_run_id(tmp_path):
    path = tmp_path / "audit.jsonl"
    middleware = AuditLogMiddleware(path)
    arguments = {"name": "list_devices", "arguments": {}}

    middleware.before_call("invoke_read_tool", arguments)
    middleware.after_call("invoke_read_tool", arguments, {"items": []})
    middleware.before_call("invoke_read_tool", arguments)
    middleware.after_call("invoke_read_tool", arguments, {"items": []})

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[0]["run_id"] == records[1]["run_id"]
    assert records[0]["run_id"].startswith("run_")


def test_audit_records_stable_session_id_per_session_object(tmp_path):
    from types import SimpleNamespace

    class _FakeSession:
        """A plain class instance (weakly referenceable, unlike a bare
        ``object()``) standing in for a real MCP ``ServerSession``."""

    path = tmp_path / "audit.jsonl"
    middleware = AuditLogMiddleware(path)
    arguments = {"name": "list_devices", "arguments": {}}
    ctx_a = SimpleNamespace(session=_FakeSession())
    ctx_b = SimpleNamespace(session=_FakeSession())

    middleware.before_call("invoke_read_tool", arguments, context=ctx_a)
    middleware.after_call("invoke_read_tool", arguments, {"items": []}, context=ctx_a)
    middleware.before_call("invoke_read_tool", arguments, context=ctx_a)
    middleware.after_call("invoke_read_tool", arguments, {"items": []}, context=ctx_a)
    middleware.before_call("invoke_read_tool", arguments, context=ctx_b)
    middleware.after_call("invoke_read_tool", arguments, {"items": []}, context=ctx_b)

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[0]["session_id"] == records[1]["session_id"]
    assert records[0]["session_id"] != records[2]["session_id"]
    assert records[0]["session_id"].startswith("sess_")


def test_audit_records_session_none_without_context(tmp_path):
    path = tmp_path / "audit.jsonl"
    middleware = AuditLogMiddleware(path)
    arguments = {"name": "list_devices", "arguments": {}}

    middleware.before_call("invoke_read_tool", arguments)
    middleware.after_call("invoke_read_tool", arguments, {"items": []})

    record = json.loads(path.read_text())
    assert record["session_id"] == "sess_none"


def test_audit_classification_uses_injected_classifier(tmp_path):
    path = tmp_path / "audit.jsonl"
    middleware = AuditLogMiddleware(
        path,
        classifier=lambda name, arguments: "destructive",
    )
    arguments = {"name": "reboot_device", "arguments": {"serial": "SG12345678"}}

    middleware.before_call("invoke_tool", arguments)
    middleware.after_call("invoke_tool", arguments, {"status": "ok"})

    record = json.loads(path.read_text())
    assert record["classification"] == "destructive"


def test_audit_classification_defaults_to_unknown_without_classifier(tmp_path):
    path = tmp_path / "audit.jsonl"
    middleware = AuditLogMiddleware(path)
    arguments = {"name": "list_devices", "arguments": {}}

    middleware.before_call("invoke_read_tool", arguments)
    middleware.after_call("invoke_read_tool", arguments, {"items": []})

    record = json.loads(path.read_text())
    assert record["classification"] == "unknown"


def test_audit_classification_falls_back_when_classifier_raises(tmp_path):
    def bad_classifier(name, arguments):
        raise RuntimeError("boom")

    path = tmp_path / "audit.jsonl"
    middleware = AuditLogMiddleware(path, classifier=bad_classifier)
    arguments = {"name": "list_devices", "arguments": {}}

    middleware.before_call("invoke_read_tool", arguments)
    middleware.after_call("invoke_read_tool", arguments, {"items": []})

    record = json.loads(path.read_text())
    assert record["classification"] == "unknown"


def test_audit_classification_rejects_unknown_values(tmp_path):
    path = tmp_path / "audit.jsonl"
    middleware = AuditLogMiddleware(
        path,
        classifier=lambda name, arguments: "not-a-real-classification",
    )
    arguments = {"name": "list_devices", "arguments": {}}

    middleware.before_call("invoke_read_tool", arguments)
    middleware.after_call("invoke_read_tool", arguments, {"items": []})

    record = json.loads(path.read_text())
    assert record["classification"] == "unknown"


def test_audit_records_cancelled_outcome_for_cancelled_error(tmp_path):
    import asyncio

    path = tmp_path / "audit.jsonl"
    middleware = AuditLogMiddleware(path)
    arguments = {"name": "reboot_device", "arguments": {}}

    middleware.before_call("invoke_tool", arguments)
    middleware.on_error("invoke_tool", arguments, asyncio.CancelledError())

    record = json.loads(path.read_text())
    assert record["outcome"] == "cancelled"
    assert record["error_type"] == "CancelledError"


def test_audit_records_timeout_outcome_for_timeout_error(tmp_path):
    path = tmp_path / "audit.jsonl"
    middleware = AuditLogMiddleware(path)
    arguments = {"name": "reboot_device", "arguments": {}}

    middleware.before_call("invoke_tool", arguments)
    middleware.on_error("invoke_tool", arguments, TimeoutError("deadline exceeded"))

    record = json.loads(path.read_text())
    assert record["outcome"] == "timeout"


def test_audit_session_ids_bounded_for_non_weakly_referenceable_sessions(tmp_path):
    """Session-like objects that cannot be weak-referenced (some test
    doubles / lightweight namedtuples) must still get a bounded, stable
    per-object id via the fallback LRU cache, not raise or grow unbounded.
    """
    path = tmp_path / "audit.jsonl"
    middleware = AuditLogMiddleware(path)

    class _NotWeakReferenceable:
        __slots__ = ()

    from types import SimpleNamespace

    ctx = SimpleNamespace(session=_NotWeakReferenceable())
    arguments = {"name": "list_devices", "arguments": {}}

    middleware.before_call("invoke_read_tool", arguments, context=ctx)
    middleware.after_call("invoke_read_tool", arguments, {"items": []}, context=ctx)
    middleware.before_call("invoke_read_tool", arguments, context=ctx)
    middleware.after_call("invoke_read_tool", arguments, {"items": []}, context=ctx)

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[0]["session_id"] == records[1]["session_id"]
    assert records[0]["session_id"].startswith("sess_")
