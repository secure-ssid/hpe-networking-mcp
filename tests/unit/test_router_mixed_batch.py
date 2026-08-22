"""Acceptance tests for invoke_tools_batch -- the ordered mixed read/write batch.

Pins the Wave-2 C1 contract:

- Every step dispatches through ``_dispatch_tool`` -- the identical path
  ``invoke_tool`` uses -- so platform write gates, the aggregate read-only
  gate, the per-call rate gate and response bounding apply to batch steps
  exactly as they do to single calls (zero duplicated enforcement).
- A write step whose platform gate is off is blocked with the *same* payload
  a single ``invoke_tool`` call would return, and never reaches the backend.
- Default ``on_error="stop"`` halts on the first non-ok step -- including a
  gate-*blocked* write (status "blocked", not "error") -- so a refused step
  can never silently fall through to a later dependent write.
- ``on_error="continue"`` collects every step's outcome.
- The rate gate is charged per *dispatched* step; locally refused steps
  (unknown tool, blocked write, invalid call) are never charged.
- Per-step elicitation is preserved: the router's request ``ctx`` reaches
  every step unwrapped, and a declined confirmation surfaces as that step's
  failure and halts the batch under the default.
- The whole response stays strictly within the configured byte budget at the
  25-step worst case.

No network access.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel

import hpe_networking_mcp.mcp_servers.tool_router as router
from hpe_networking_mcp.mcp_servers.shared import DESTRUCTIVE, READ_ONLY


class _ConfirmAction(BaseModel):
    confirm: bool


class _FakeElicitCtx:
    """Stand-in request ctx recording elicitations and answering accept/decline."""

    def __init__(self, action: str = "accept", confirm: bool = True) -> None:
        self.elicitations: list[str] = []
        self._action = action
        self._confirm = confirm

    async def elicit(self, message: str, schema: Any) -> Any:
        self.elicitations.append(message)
        return SimpleNamespace(
            action=self._action, data=SimpleNamespace(confirm=self._confirm)
        )


def _backend(dispatched: list[str], seen_ctx: list[Any]) -> MCPServer:
    srv = MCPServer("central-monitoring")

    @srv.tool(annotations=READ_ONLY)
    def echo(value: str = "x") -> dict[str, Any]:
        dispatched.append("echo")
        return {"value": value}

    @srv.tool(annotations=READ_ONLY)
    def big_list(count: int = 5) -> dict[str, Any]:
        dispatched.append("big_list")
        return {"items": [{"i": i, "pad": "y" * 200} for i in range(count)]}

    @srv.tool(annotations=DESTRUCTIVE)
    def wipe() -> dict[str, Any]:
        dispatched.append("wipe")
        return {"wiped": True}

    @srv.tool(annotations=READ_ONLY)
    def explode() -> dict[str, Any]:
        dispatched.append("explode")
        raise RuntimeError("backend boom")

    @srv.tool(annotations=READ_ONLY)
    async def ctx_probe(ctx: Context) -> dict[str, Any]:
        seen_ctx.append(ctx)
        dispatched.append("ctx_probe")
        return {"probed": True}

    @srv.tool(annotations=DESTRUCTIVE)
    async def confirm_wipe(ctx: Context, target: str = "x") -> dict[str, Any]:
        seen_ctx.append(ctx)
        result = await ctx.elicit(
            message=f"Confirm wipe of {target}?", schema=_ConfirmAction
        )
        if result.action != "accept" or not result.data.confirm:
            # Same refusal shape the real ops/monitoring tools return.
            return {"status": "CANCELLED", "detail": "user declined confirmation"}
        dispatched.append(f"confirm_wipe:{target}")
        return {"wiped": target}

    return srv


@pytest.fixture
def wired(monkeypatch):
    dispatched: list[str] = []
    seen_ctx: list[Any] = []
    backend = _backend(dispatched, seen_ctx)
    monkeypatch.setattr(router, "_BACKENDS", {"central-monitoring": "fake.mod"})
    monkeypatch.setattr(router, "_tool_index", {})
    monkeypatch.setattr(router, "_tool_servers", {})
    monkeypatch.setattr(router, "_tool_backend_names", {})
    monkeypatch.setattr(router, "_backend_load_errors", {})
    monkeypatch.setattr(
        router.importlib, "import_module", lambda path: SimpleNamespace(mcp=backend)
    )
    monkeypatch.setattr(router, "_dispatch_rate_gate", None)
    return SimpleNamespace(backend=backend, dispatched=dispatched, seen_ctx=seen_ctx)


def _mixed(calls, on_error: str = "stop", ctx: Any = None):
    return asyncio.run(router.invoke_tools_batch(ctx, calls, on_error))


# ---------------------------------------------------------------------------
# Registration / input validation
# ---------------------------------------------------------------------------


class TestMixedBatchRegistration:
    def test_tool_is_registered_in_default_mode(self):
        assert hasattr(router, "invoke_tools_batch")
        assert "invoke_tools_batch" in router.mcp._tool_manager._tools

    def test_published_schema_describes_calls_and_on_error(self):
        tool = router.mcp._tool_manager._tools["invoke_tools_batch"]
        schema = tool.parameters
        text = json.dumps(schema)

        assert "calls" in schema["properties"]
        assert "on_error" in schema["properties"]
        for field in ("name", "arguments", "id"):
            assert f'"{field}"' in text

    def test_model_bounds_name_and_id_lengths(self):
        model = router.MixedBatchCall

        with pytest.raises(Exception):
            model(name="x" * (router.MAX_BATCH_TOOL_NAME_CHARS + 1))
        with pytest.raises(Exception):
            model(name="echo", id="i" * (router.MAX_BATCH_CALL_ID_CHARS + 1))

    def test_model_rejects_unknown_fields(self):
        with pytest.raises(Exception):
            router.MixedBatchCall(name="echo", surprise=1)

    def test_model_has_no_cursor_field(self):
        assert "cursor" not in router.MixedBatchCall.model_fields

    def test_model_instances_are_accepted_at_runtime(self, wired):
        out = _mixed([router.MixedBatchCall(name="echo", arguments={"value": "hi"}, id="a")])

        assert out["ok"] is True
        assert out["results"][0]["result"]["value"] == "hi"

    def test_invalid_on_error_is_rejected(self, wired):
        out = _mixed([{"name": "echo"}], on_error="ignore")

        assert out["ok"] is False
        assert "on_error" in out["error"]
        assert wired.dispatched == []

    def test_empty_batch_is_rejected(self, wired):
        assert _mixed([])["ok"] is False

    def test_calls_must_be_a_list(self, wired):
        assert _mixed("echo")["ok"] is False

    def test_batch_entry_cap_is_enforced(self, wired):
        out = _mixed([{"name": "echo"} for _ in range(router.MAX_BATCH_CALLS + 1)])

        assert out["ok"] is False
        assert "exceeding" in out["error"]
        assert wired.dispatched == []


class TestMixedBatchCallValidation:
    """Criterion (f): per-entry rejection is identical to the read batch."""

    def test_duplicate_explicit_ids_reject_the_whole_batch(self, wired):
        out = _mixed([{"name": "echo", "id": "dup"}, {"name": "echo", "id": "dup"}])

        assert out["ok"] is False
        assert "duplicate call id" in out["error"]
        assert "results" not in out
        assert wired.dispatched == []

    def test_oversized_arguments_reject_only_that_entry(self, wired):
        huge = {"value": "z" * (router.MAX_BATCH_ARGUMENTS_BYTES + 10)}
        out = _mixed(
            [{"name": "echo", "id": "big", "arguments": huge},
             {"name": "echo", "id": "ok"}],
            on_error="continue",
        )

        assert out["results"][0]["status"] == "invalid_call"
        assert out["results"][1]["status"] == "ok"
        # The argument value itself is never echoed back.
        assert "zzz" not in json.dumps(out)

    def test_cursor_entries_are_rejected(self, wired):
        """Cursor resumption is read-batch-only; a mixed batch never resumes."""
        out = _mixed([{"name": "echo", "cursor": "opaque-cursor"}])

        assert out["results"][0]["status"] == "invalid_call"
        assert "cursor" in out["results"][0]["error"]
        assert wired.dispatched == []


# ---------------------------------------------------------------------------
# Write-gate enforcement (criterion a)
# ---------------------------------------------------------------------------


class TestMixedBatchWriteGates:
    def test_write_step_is_blocked_when_platform_gate_is_off(self, wired):
        out = _mixed([{"name": "wipe"}])

        item = out["results"][0]
        assert item["status"] == "blocked"
        assert out["ok"] is False
        # The refusal is local: the backend is never reached.
        assert wired.dispatched == []

    def test_blocked_payload_is_identical_to_a_single_call(self, wired):
        out = _mixed([{"name": "wipe"}])
        single = asyncio.run(router._dispatch_tool(None, "wipe", {}))

        item = out["results"][0]
        assert single["status"] == "blocked"
        assert item["detail"] == single

    def test_write_step_dispatches_when_the_gate_is_on(self, wired, monkeypatch):
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")

        out = _mixed([{"name": "wipe"}])

        assert out["results"][0]["status"] == "ok"
        assert out["results"][0]["result"]["wiped"] is True
        assert wired.dispatched == ["wipe"]

    def test_unknown_tool_is_isolated_per_step(self, wired):
        out = _mixed([{"name": "nope"}, {"name": "echo"}], on_error="continue")

        assert out["results"][0]["status"] == "unknown_tool"
        assert out["results"][1]["status"] == "ok"


# ---------------------------------------------------------------------------
# Halt semantics (criterion b, incl. the pinned blocked-write case; criterion c)
# ---------------------------------------------------------------------------


class TestMixedBatchHaltSemantics:
    def test_default_halts_after_a_backend_error(self, wired):
        out = _mixed([{"name": "echo"}, {"name": "explode"}, {"name": "echo"}])

        assert [item["status"] for item in out["results"]] == ["ok", "error"]
        assert out["halted"] is True
        assert out["remaining"] == 1
        assert out["ok"] is False
        # The step after the failure never reached the backend.
        assert wired.dispatched == ["echo", "explode"]

    def test_default_halts_after_a_gate_blocked_write(self, wired):
        """Pinned case: status "blocked" (not "error") at position k must
        prevent steps k+1.. from dispatching -- a platform-gate skip falling
        through to a later dependent write is the ordering violation the
        fail-fast default exists to prevent."""
        out = _mixed([{"name": "echo"}, {"name": "wipe"}, {"name": "echo"}])

        assert [item["status"] for item in out["results"]] == ["ok", "blocked"]
        assert out["halted"] is True
        assert out["remaining"] == 1
        assert wired.dispatched == ["echo"]

    def test_halt_after_an_invalid_call(self, wired):
        huge = {"value": "z" * (router.MAX_BATCH_ARGUMENTS_BYTES + 10)}
        out = _mixed([{"name": "echo", "arguments": huge}, {"name": "echo"}])

        assert [item["status"] for item in out["results"]] == ["invalid_call"]
        assert out["halted"] is True
        assert out["remaining"] == 1
        assert wired.dispatched == []

    def test_counts_cover_dispatched_steps_only(self, wired):
        out = _mixed(
            [{"name": "echo", "id": "a"}, {"name": "explode", "id": "b"},
             {"name": "echo", "id": "c"}]
        )

        assert out["counts"] == {"total": 2, "succeeded": 1, "failed": 1}
        assert out["failed_ids"] == ["b"]
        assert out["failed_indexes"] == [1]

    def test_continue_collects_every_step(self, wired):
        out = _mixed(
            [{"name": "explode"}, {"name": "wipe"}, {"name": "echo"}],
            on_error="continue",
        )

        assert [item["status"] for item in out["results"]] == ["error", "blocked", "ok"]
        assert out["halted"] is False
        assert out["remaining"] == 0
        assert out["ok"] is False
        assert out["counts"] == {"total": 3, "succeeded": 1, "failed": 2}

    def test_all_ok_batch_is_not_halted(self, wired):
        out = _mixed([{"name": "echo"}, {"name": "echo"}])

        assert out["ok"] is True
        assert out["halted"] is False
        assert out["remaining"] == 0
        assert out["truncated"] is False


# ---------------------------------------------------------------------------
# Rate limiting (criterion d)
# ---------------------------------------------------------------------------


class TestMixedBatchRateGate:
    def test_gate_is_charged_once_per_dispatched_step(self, wired, monkeypatch):
        calls = {"n": 0}

        async def _gate():
            calls["n"] += 1

        monkeypatch.setattr(router, "_dispatch_rate_gate", _gate)

        _mixed([{"name": "echo"} for _ in range(4)])

        assert calls["n"] == 4

    def test_locally_refused_steps_are_never_charged(self, wired, monkeypatch):
        calls = {"n": 0}

        async def _gate():
            calls["n"] += 1

        monkeypatch.setattr(router, "_dispatch_rate_gate", _gate)

        _mixed([{"name": "nope"}, {"name": "wipe"}, {"name": "echo"}], on_error="continue")

        # Unknown tool and gate-blocked write are refused before the gate.
        assert calls["n"] == 1

    def test_undispatched_steps_after_a_halt_are_never_charged(self, wired, monkeypatch):
        calls = {"n": 0}

        async def _gate():
            calls["n"] += 1

        monkeypatch.setattr(router, "_dispatch_rate_gate", _gate)

        _mixed([{"name": "explode"}, {"name": "echo"}, {"name": "echo"}])

        assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Byte budget (criterion e)
# ---------------------------------------------------------------------------


class TestMixedBatchBudgets:
    def test_per_item_budget_is_threaded_into_dispatch(self, wired, monkeypatch):
        seen: list[int | None] = []
        original = router._dispatch_tool

        async def _spy(ctx, name, arguments=None, **kwargs):
            seen.append(kwargs.get("max_bytes"))
            return await original(ctx, name, arguments, **kwargs)

        monkeypatch.setattr(router, "_dispatch_tool", _spy)
        monkeypatch.setenv(router._BATCH_RESPONSE_BUDGET_BYTES_ENV, "100000")

        _mixed([{"name": "echo"} for _ in range(4)])

        assert seen == [25000, 25000, 25000, 25000]

    def test_worst_case_25_step_response_is_strictly_bounded(self, wired, monkeypatch):
        monkeypatch.setenv(router._BATCH_RESPONSE_BUDGET_BYTES_ENV, "2048")

        out = _mixed(
            [{"name": "big_list", "arguments": {"count": 40}} for _ in range(25)]
        )

        size = len(json.dumps(out, ensure_ascii=False, default=str).encode("utf-8"))
        assert size <= 4096, size
        assert out["truncated"] is True

    def test_within_budget_responses_are_not_marked_truncated(self, wired):
        out = _mixed([{"name": "echo"}])

        assert out["truncated"] is False
        assert "result_truncated" not in out["results"][0]


# ---------------------------------------------------------------------------
# Per-step elicitation (criterion g)
# ---------------------------------------------------------------------------


class TestMixedBatchElicitation:
    def test_router_ctx_reaches_every_step_unwrapped(self, wired, monkeypatch):
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
        sentinel = _FakeElicitCtx()

        out = _mixed(
            [
                {"name": "ctx_probe"},
                {"name": "confirm_wipe", "arguments": {"target": "ap-1"}},
                {"name": "ctx_probe"},
            ],
            ctx=sentinel,
        )

        assert out["ok"] is True
        # The exact router ctx object -- never wrapped, dropped, or replaced --
        # reached every step, read and write alike.
        assert wired.seen_ctx == [sentinel, sentinel, sentinel]
        assert len(sentinel.elicitations) == 1

    def test_declined_confirmation_fails_the_step_and_halts(self, wired, monkeypatch):
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
        declining = _FakeElicitCtx(action="decline", confirm=False)

        out = _mixed(
            [
                {"name": "echo"},
                {"name": "confirm_wipe", "arguments": {"target": "ap-1"}},
                {"name": "echo"},
            ],
            ctx=declining,
        )

        assert [item["status"] for item in out["results"]] == ["ok", "cancelled"]
        assert "declined" in out["results"][1]["error"]
        assert out["halted"] is True
        assert out["remaining"] == 1
        assert out["ok"] is False
        # The write was never performed and the dependent step never ran.
        assert wired.dispatched == ["echo"]

    def test_declined_confirmation_is_collected_under_continue(self, wired, monkeypatch):
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
        declining = _FakeElicitCtx(action="decline", confirm=False)

        out = _mixed(
            [{"name": "confirm_wipe"}, {"name": "echo"}],
            on_error="continue",
            ctx=declining,
        )

        assert [item["status"] for item in out["results"]] == ["cancelled", "ok"]
        assert out["halted"] is False

    def test_accepted_confirmation_proceeds(self, wired, monkeypatch):
        monkeypatch.setenv("HPE_MCP_CENTRAL_WRITES", "1")
        accepting = _FakeElicitCtx()

        out = _mixed(
            [{"name": "confirm_wipe", "arguments": {"target": "ap-2"}}], ctx=accepting
        )

        assert out["ok"] is True
        assert out["results"][0]["result"]["wiped"] == "ap-2"
        assert wired.dispatched == ["confirm_wipe:ap-2"]


# ---------------------------------------------------------------------------
# Observability: metrics/audit labels stay in sync for the new dispatching tool
# ---------------------------------------------------------------------------


class TestMixedBatchObservability:
    def test_mixed_batch_is_a_dispatching_router_tool(self):
        assert "invoke_tools_batch" in router._DISPATCHING_ROUTER_TOOLS

    def test_audit_dispatching_names_stay_in_sync(self):
        from hpe_networking_mcp.mcp_servers._middleware.audit_log import (
            _DISPATCHING_TOOL_NAMES,
        )

        assert _DISPATCHING_TOOL_NAMES == router._DISPATCHING_ROUTER_TOOLS

    def test_single_target_batch_resolves_the_real_tool(self, wired):
        router._load_all_backends()
        args = {"calls": [{"name": "echo"}, {"name": "echo"}]}

        tool, backend, capability = router._router_call_labels("invoke_tools_batch", args)

        assert (tool, backend, capability) == ("echo", "central-monitoring", "read")

    def test_multi_target_batch_uses_a_bounded_constant_label(self, wired):
        router._load_all_backends()
        args = {"calls": [{"name": "echo"}, {"name": "big_list"}]}

        tool, backend, capability = router._router_call_labels("invoke_tools_batch", args)

        assert tool == router.BATCH_MULTI_LABEL
        assert backend == "central-monitoring"
        assert capability == "read"

    def test_capability_label_reflects_the_most_severe_step(self, wired):
        router._load_all_backends()

        _tool, _backend, capability = router._router_call_labels(
            "invoke_tools_batch", {"calls": [{"name": "echo"}, {"name": "wipe"}]}
        )

        assert capability == "destructive"

    def test_audit_target_resolves_for_a_mixed_batch(self, wired):
        router._load_all_backends()

        target = router._router_call_target(
            "invoke_tools_batch", {"calls": [{"name": "echo"}]}
        )

        assert target == "echo"

    def test_label_resolution_never_reads_argument_values(self, wired):
        router._load_all_backends()
        args = {"calls": [{"name": "echo", "arguments": {"value": "SECRET-TOKEN"}}]}

        labels = router._router_call_labels("invoke_tools_batch", args)

        assert "SECRET-TOKEN" not in json.dumps(labels)
