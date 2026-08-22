"""v0.7 regressions: secrets/identifiers must never reach observability
surfaces -- metrics snapshots, audit records, or the artifact-contracts
layer used by v0.7 evaluation harnesses -- even for nested payloads and
failure paths (exceptions, cancellation, timeouts, simulated retry
exhaustion).

These tests exercise the *combined* middleware stack the router actually
installs (NullStrip -> RateLimit -> ResponseEnvelope -> Metrics ->
AuditLog) rather than each middleware in isolation, because a leak can
appear at the seam between two middlewares (e.g. ResponseEnvelope
re-wrapping a result that Metrics/AuditLog then inspect) as easily as
inside one.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import Context, MCPServer

from hpe_networking_mcp.mcp_servers._middleware import (
    AuditLogMiddleware,
    MetricsMiddleware,
    MetricsRegistry,
    NullStripMiddleware,
    RateLimitMiddleware,
    ResponseEnvelopeMiddleware,
    install_middleware,
)
from hpe_networking_mcp.pipeline import artifact_contracts as contracts

_SECRETS = (
    "hunter2-super-secret-password",
    "cmcp-api-token-abcdef0123456789",
    "SG12345678",  # device serial
    "aa:bb:cc:dd:ee:ff",  # MAC
    "tenant-acme-corp-real-name",
    "confidential exception detail: disk full at /srv/data",
)


def _assert_no_secrets(haystack: str) -> None:
    for secret in _SECRETS:
        assert secret not in haystack, f"leaked {secret!r} into observability output"


def _stack(tmp_path: Path, registry: MetricsRegistry) -> tuple[MCPServer, Path]:
    audit_file = tmp_path / "audit.jsonl"
    srv = MCPServer("secret-leak-test")
    install_middleware(
        srv,
        [
            NullStripMiddleware(),
            RateLimitMiddleware(rate=1000.0, burst=100),
            ResponseEnvelopeMiddleware(),
            MetricsMiddleware(registry),
            AuditLogMiddleware(audit_file),
        ],
    )
    return srv, audit_file


def _call(srv: MCPServer, name: str, args: dict[str, Any]):
    return asyncio.run(srv._tool_manager.call_tool(name, args, Context(mcp_server=srv)))


# ---------------------------------------------------------------------------
# Nested payloads: success + blocked results
# ---------------------------------------------------------------------------


class TestNestedPayloadNoLeak:
    def test_nested_secret_result_never_leaks(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HPE_MCP_METRICS", "1")
        monkeypatch.setenv("HPE_MCP_AUDIT_LOG", "1")
        registry = MetricsRegistry()

        def create_ssid(password: str, api_token: str) -> dict[str, Any]:
            return {
                "status": "ok",
                "summary": {
                    "device": {
                        "serial": "SG12345678",
                        "mac": "aa:bb:cc:dd:ee:ff",
                    },
                    "tenant_id": "tenant-acme-corp-real-name",
                },
            }

        srv, audit_file = _stack(tmp_path, registry)
        srv.tool()(create_ssid)
        install_middleware(
            srv,
            [
                NullStripMiddleware(),
                RateLimitMiddleware(rate=1000.0, burst=100),
                ResponseEnvelopeMiddleware(),
                MetricsMiddleware(registry),
                AuditLogMiddleware(audit_file),
            ],
        )

        _call(
            srv,
            "create_ssid",
            {
                "password": "hunter2-super-secret-password",
                "api_token": "cmcp-api-token-abcdef0123456789",
            },
        )

        _assert_no_secrets(json.dumps(registry.snapshot()))
        _assert_no_secrets(audit_file.read_text())

    def test_nested_blocked_result_never_leaks(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HPE_MCP_METRICS", "1")
        monkeypatch.setenv("HPE_MCP_AUDIT_LOG", "1")
        registry = MetricsRegistry()

        def blocked_write(password: str) -> dict[str, Any]:
            return {
                "status": "blocked",
                "reason": "write disabled",
                "context": {"attempted_password": password},
            }

        srv, audit_file = _stack(tmp_path, registry)
        srv.tool()(blocked_write)
        install_middleware(
            srv,
            [
                NullStripMiddleware(),
                RateLimitMiddleware(rate=1000.0, burst=100),
                ResponseEnvelopeMiddleware(),
                MetricsMiddleware(registry),
                AuditLogMiddleware(audit_file),
            ],
        )

        _call(srv, "blocked_write", {"password": "hunter2-super-secret-password"})

        _assert_no_secrets(json.dumps(registry.snapshot()))
        _assert_no_secrets(audit_file.read_text())


# ---------------------------------------------------------------------------
# Failure paths: exceptions, cancellation, timeouts, retry-shaped errors
# ---------------------------------------------------------------------------


class TestFailurePathNoLeak:
    def test_exception_message_never_leaks(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HPE_MCP_METRICS", "1")
        monkeypatch.setenv("HPE_MCP_AUDIT_LOG", "1")
        registry = MetricsRegistry()

        def raiser(password: str) -> str:
            raise RuntimeError(
                "confidential exception detail: disk full at /srv/data "
                f"(while handling password={password})"
            )

        srv, audit_file = _stack(tmp_path, registry)
        srv.tool()(raiser)
        install_middleware(
            srv,
            [
                NullStripMiddleware(),
                RateLimitMiddleware(rate=1000.0, burst=100),
                ResponseEnvelopeMiddleware(),
                MetricsMiddleware(registry),
                AuditLogMiddleware(audit_file),
            ],
        )

        result = _call(srv, "raiser", {"password": "hunter2-super-secret-password"})
        # ResponseEnvelopeMiddleware.on_error substitutes the raised exception
        # with the router-surface envelope shape (the router catches backend
        # exceptions into the same {ok: false, ...} form). The wrapped
        # ToolError message -- which embeds the secret -- reaches only the
        # result payload; audit/metrics still see only type(exc).__name__.
        assert result["ok"] is False
        assert result["status"] == 500

        _assert_no_secrets(json.dumps(registry.snapshot()))
        _assert_no_secrets(audit_file.read_text())
        # Two records: the on_error "exception" record, then the substitute
        # envelope's after_call outcome record (the same pattern unknown-tool
        # substitutes already produce). The first carries the failure
        # classification; neither may carry the secret.
        records = [json.loads(line) for line in audit_file.read_text().splitlines()]
        assert records[0]["error_type"] == "ToolError"
        assert records[0]["outcome"] == "exception"

    def test_retry_exhausted_style_error_never_leaks_url_or_token(self, tmp_path, monkeypatch):
        """Simulates the shape of an error central_client would raise after
        retries are exhausted (an httpx-style message embedding the request
        URL, which can carry a query-string token) -- only error_type may
        ever reach audit/metrics, never str(exc)."""
        monkeypatch.setenv("HPE_MCP_METRICS", "1")
        monkeypatch.setenv("HPE_MCP_AUDIT_LOG", "1")
        registry = MetricsRegistry()

        class RetryExhaustedError(RuntimeError):
            pass

        def flaky_tool() -> str:
            raise RetryExhaustedError(
                "GET https://internal.example/api?access_token=cmcp-api-token-abcdef0123456789 "
                "failed after 5 retries (503)"
            )

        srv, audit_file = _stack(tmp_path, registry)
        srv.tool()(flaky_tool)
        install_middleware(
            srv,
            [
                NullStripMiddleware(),
                RateLimitMiddleware(rate=1000.0, burst=100),
                ResponseEnvelopeMiddleware(),
                MetricsMiddleware(registry),
                AuditLogMiddleware(audit_file),
            ],
        )

        result = _call(srv, "flaky_tool", {})
        # The envelope substitutes the exception (see above); the URL-bearing
        # message reaches only the result payload, never audit/metrics.
        assert result["ok"] is False

        _assert_no_secrets(json.dumps(registry.snapshot()))
        _assert_no_secrets(audit_file.read_text())
        records = [json.loads(line) for line in audit_file.read_text().splitlines()]
        assert records[0]["error_type"] == "ToolError"

    def test_cancellation_never_leaks_and_is_classified_cancelled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HPE_MCP_METRICS", "1")
        monkeypatch.setenv("HPE_MCP_AUDIT_LOG", "1")
        registry = MetricsRegistry()
        audit_file = tmp_path / "audit.jsonl"
        audit_mw = AuditLogMiddleware(audit_file)
        metrics_mw = MetricsMiddleware(registry)

        async def slow_secret_tool(password: str) -> str:
            await asyncio.sleep(5)
            return password

        async def _run() -> None:
            args = {"password": "hunter2-super-secret-password"}
            metrics_mw.before_call("slow_secret_tool", args)
            audit_mw.before_call("slow_secret_tool", args)
            task = asyncio.ensure_future(slow_secret_tool(**args))
            await asyncio.sleep(0.01)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError as exc:
                metrics_mw.on_error("slow_secret_tool", args, exc)
                await audit_mw.on_error("slow_secret_tool", args, exc)

        asyncio.run(_run())

        snapshot = registry.snapshot()
        _assert_no_secrets(json.dumps(snapshot))
        _assert_no_secrets(audit_file.read_text())
        assert snapshot["series"][0]["outcomes"] == {"cancelled": 1}
        record = json.loads(audit_file.read_text())
        assert record["outcome"] == "cancelled"

    def test_timeout_never_leaks_and_is_classified_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HPE_MCP_METRICS", "1")
        monkeypatch.setenv("HPE_MCP_AUDIT_LOG", "1")
        registry = MetricsRegistry()
        audit_file = tmp_path / "audit.jsonl"
        audit_mw = AuditLogMiddleware(audit_file)
        metrics_mw = MetricsMiddleware(registry)

        async def slow_secret_tool(password: str) -> str:
            await asyncio.sleep(5)
            return password

        async def _run() -> None:
            args = {"password": "hunter2-super-secret-password"}
            metrics_mw.before_call("slow_secret_tool", args)
            audit_mw.before_call("slow_secret_tool", args)
            try:
                await asyncio.wait_for(slow_secret_tool(**args), timeout=0.01)
            except asyncio.TimeoutError as exc:
                metrics_mw.on_error("slow_secret_tool", args, exc)
                await audit_mw.on_error("slow_secret_tool", args, exc)

        asyncio.run(_run())

        snapshot = registry.snapshot()
        _assert_no_secrets(json.dumps(snapshot))
        _assert_no_secrets(audit_file.read_text())
        assert snapshot["series"][0]["outcomes"] == {"timeout": 1}
        record = json.loads(audit_file.read_text())
        assert record["outcome"] == "timeout"


# ---------------------------------------------------------------------------
# Concurrency: many secret-bearing calls must never cross-contaminate
# ---------------------------------------------------------------------------


class TestConcurrentNoLeak:
    def test_concurrent_secret_bearing_calls_never_leak_or_miscount(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HPE_MCP_METRICS", "1")
        monkeypatch.setenv("HPE_MCP_AUDIT_LOG", "1")
        registry = MetricsRegistry()
        audit_file = tmp_path / "audit.jsonl"

        async def secret_tool(password: str) -> dict[str, Any]:
            await asyncio.sleep(0)
            return {"status": "ok", "echo_len": len(password)}

        srv = MCPServer("concurrent-secret-test")
        srv.tool()(secret_tool)
        install_middleware(
            srv,
            [
                NullStripMiddleware(),
                RateLimitMiddleware(rate=1000.0, burst=100),
                ResponseEnvelopeMiddleware(),
                MetricsMiddleware(registry),
                AuditLogMiddleware(audit_file),
            ],
        )

        async def _run() -> None:
            await asyncio.gather(
                *[
                    srv._tool_manager.call_tool(
                        "secret_tool", {"password": f"hunter2-secret-{i}"}, Context(mcp_server=srv)
                    )
                    for i in range(25)
                ]
            )

        asyncio.run(_run())

        snapshot = registry.snapshot()
        assert snapshot["series"][0]["requests"] == 25
        assert snapshot["series"][0]["outcomes"] == {"success": 25}
        records = [json.loads(line) for line in audit_file.read_text().splitlines()]
        assert len(records) == 25

        serialized_snapshot = json.dumps(snapshot)
        audit_text = audit_file.read_text()
        for i in range(25):
            assert f"hunter2-secret-{i}" not in serialized_snapshot
            assert f"hunter2-secret-{i}" not in audit_text


# ---------------------------------------------------------------------------
# hpe_networking_mcp.pipeline.artifact_contracts reuse: same guarantee for the v0.7 evidence
# artifact layer these tools may write alongside audit/metrics.
# ---------------------------------------------------------------------------


class TestArtifactContractsNoLeak:
    def test_artifact_redaction_covers_nested_secret_and_identifier(self):
        redacted = contracts.redact_artifact_payload(
            {
                "summary": {
                    "nested": {
                        "api_token": "cmcp-api-token-abcdef0123456789",
                        "tenant_id": "tenant-acme-corp-real-name",
                    }
                }
            }
        )
        serialized = json.dumps(redacted)
        assert "cmcp-api-token-abcdef0123456789" not in serialized
        assert "tenant-acme-corp-real-name" not in serialized

    def test_artifact_validation_error_never_echoes_secret_value(self):
        try:
            contracts.build_artifact(
                contracts.LIVE_LIFECYCLE_EVIDENCE,
                {
                    "platform": "aos8",
                    "mode": "hunter2-super-secret-password",
                    "generated_at": "2026-07-25T00:00:00+00:00",
                },
            )
        except contracts.ArtifactValidationError as exc:
            assert "hunter2-super-secret-password" not in str(exc)
        else:
            pytest.fail("expected ArtifactValidationError")
