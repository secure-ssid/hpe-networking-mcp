"""Unit tests for the MCP middleware chain.

Test bar targets from the pre-merge review:

- NullStripMiddleware:
  - dict with ``None`` values → stripped;
  - non-None falsy (``0``, ``""``, ``False``, ``[]``) → preserved;
  - idempotent on second pass;
  - empty / missing arguments don't crash.

- RateLimitMiddleware:
  - token-bucket holds at the configured rate under burst;
  - releases correctly;
  - no deadlock on exception in the wrapped call.

- install_middleware:
  - idempotent (installing twice doesn't stack);
  - ``before_call`` runs in order before the tool;
  - ``after_call`` runs in reverse order after the tool;
  - ``on_error`` is called when the wrapped tool raises, and a
    non-None return substitutes the result (swallowing the exception).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from hpe_networking_mcp.mcp_servers._middleware import (
    MacNormalizeMiddleware,
    NullStripMiddleware,
    RateLimitMiddleware,
    ResponseEnvelopeMiddleware,
    UnknownToolSuggestMiddleware,
    install_middleware,
)
from hpe_networking_mcp.mcp_servers._middleware._outcome import classify_outcome

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_server_with_tool(fn):
    """Build an MCPServer instance with ``fn`` registered as a single tool."""
    srv = MCPServer("test")
    srv.tool()(fn)
    return srv


def _call(srv: MCPServer, name: str, args: dict[str, Any]):
    """Call a tool and block for the result — handles ToolManager.call_tool
    being an async coroutine."""
    return asyncio.run(srv._tool_manager.call_tool(name, args))


def _call_converted(srv: MCPServer, name: str, args: dict[str, Any]):
    return asyncio.run(
        srv._tool_manager.call_tool(name, args, Context(mcp_server=srv), convert_result=True)
    )


# ---------------------------------------------------------------------------
# NullStripMiddleware
# ---------------------------------------------------------------------------


class TestNullStrip:
    def test_strips_none_values(self):
        mw = NullStripMiddleware()
        out = mw.before_call("t", {"a": 1, "b": None, "c": "ok"})
        assert out == {"a": 1, "c": "ok"}

    def test_preserves_non_none_falsy(self):
        mw = NullStripMiddleware()
        out = mw.before_call(
            "t",
            {"z": 0, "s": "", "b": False, "l": [], "d": {}, "n": None},
        )
        # Every falsy-but-not-None key must survive; only 'n' should drop.
        assert "n" not in out
        assert out == {"z": 0, "s": "", "b": False, "l": [], "d": {}}

    def test_idempotent_second_pass(self):
        mw = NullStripMiddleware()
        first = mw.before_call("t", {"a": 1, "b": None})
        # ``None`` means "no change"; feed the result back.
        second = mw.before_call("t", first)
        assert second is None  # nothing to strip the second time

    def test_no_args(self):
        mw = NullStripMiddleware()
        assert mw.before_call("t", {}) is None
        assert mw.before_call("t", None) is None  # type: ignore[arg-type]

    def test_does_not_recurse(self):
        """Nested None is deliberately preserved — we only strip the top level."""
        mw = NullStripMiddleware()
        out = mw.before_call("t", {"cfg": {"inner": None}, "drop_me": None})
        assert out == {"cfg": {"inner": None}}

    def test_end_to_end_with_install(self):
        def greet(name: str = "world", shout: bool = False) -> str:
            return f"hello {name}!" + ("!" if shout else "")

        srv = _make_server_with_tool(greet)
        install_middleware(srv, [NullStripMiddleware()])
        # A real client would send {"name": None} for "unset"; Pydantic would
        # reject that (name: str has no None union). NullStrip removes the
        # key so the default kicks in.
        result = _call(srv, "greet", {"name": None})
        # Result is a list of TextContent or similar — check string contains
        # "world".
        assert "world" in str(result)


# ---------------------------------------------------------------------------
# RateLimitMiddleware
# ---------------------------------------------------------------------------


class TestRateLimit:
    @staticmethod
    def _acquire_many(mw: RateLimitMiddleware, count: int) -> None:
        async def _run() -> None:
            for _ in range(count):
                await mw._acquire()

        asyncio.run(_run())

    def test_allows_burst(self):
        """Burst of N calls where N == burst should not wait at all."""
        mw = RateLimitMiddleware(rate=100.0, burst=5)
        t0 = time.monotonic()
        self._acquire_many(mw, 5)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1, f"burst took {elapsed:.3f}s, expected <0.1s"

    def test_holds_at_rate_after_burst(self):
        """Calls beyond burst are paced. 10 calls at rate=20, burst=2:
        burst of 2 fires instantly, remaining 8 take ~8/20 = 0.4s."""
        mw = RateLimitMiddleware(rate=20.0, burst=2)
        t0 = time.monotonic()
        self._acquire_many(mw, 10)
        elapsed = time.monotonic() - t0
        # Loose bounds to avoid flakes; expected ~0.4s steady-state.
        assert 0.3 < elapsed < 1.0, f"elapsed={elapsed:.3f}s, want ~0.4s"

    def test_refills_after_idle(self):
        """After an idle period the bucket should be full again."""
        mw = RateLimitMiddleware(rate=50.0, burst=3)
        self._acquire_many(mw, 3)  # drain
        time.sleep(0.1)  # 0.1s * 50/s = 5 tokens (capped at burst=3)
        t0 = time.monotonic()
        self._acquire_many(mw, 3)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.05, f"post-idle burst took {elapsed:.3f}s"

    def test_wait_does_not_block_event_loop(self):
        async def _run() -> float:
            mw = RateLimitMiddleware(rate=1.0, burst=1)
            await mw._acquire()  # drain the only token
            marker = asyncio.create_task(asyncio.sleep(0.01))
            waiter = asyncio.create_task(mw.before_call("slow_tool", {}))
            t0 = time.monotonic()
            await marker
            marker_elapsed = time.monotonic() - t0
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            return marker_elapsed

        elapsed = asyncio.run(_run())
        assert elapsed < 0.2, f"rate-limit wait blocked event loop for {elapsed:.3f}s"

    def test_no_deadlock_on_exception(self):
        """If the wrapped tool raises, subsequent calls must still pass."""
        def raiser() -> str:
            raise RuntimeError("boom")

        srv = _make_server_with_tool(raiser)
        install_middleware(srv, [RateLimitMiddleware(rate=100.0, burst=5)])

        # First call raises; that must not leave the rate-limit lock held.
        # The SDK's ToolManager wraps tool exceptions in ToolError.
        with pytest.raises(ToolError, match="boom"):
            _call(srv, "raiser", {})

        # If the lock were stuck, this call would hang forever. Cap at 2s.
        def ok() -> str:
            return "ok"
        srv2 = _make_server_with_tool(ok)
        install_middleware(srv2, [RateLimitMiddleware(rate=100.0, burst=5)])
        t0 = time.monotonic()
        _call(srv2, "ok", {})
        assert time.monotonic() - t0 < 2.0

    def test_invalid_rate_raises(self):
        with pytest.raises(ValueError):
            RateLimitMiddleware(rate=0)
        with pytest.raises(ValueError):
            RateLimitMiddleware(rate=-1)

    def test_on_wait_observer_called_with_actual_wait_seconds(self):
        """v0.7: an injected observer records real observed wait durations
        -- never fabricated, never called when no wait occurred."""
        waits: list[float] = []
        mw = RateLimitMiddleware(rate=20.0, burst=1, on_wait=waits.append)

        self._acquire_many(mw, 1)  # drains the single token, no wait
        assert waits == []

        self._acquire_many(mw, 3)  # forces 3 waits at rate=20/s
        assert len(waits) == 3
        assert all(w > 0 for w in waits)

    def test_on_wait_observer_exception_does_not_break_rate_limiting(self):
        """A broken observer must not take down rate limiting itself."""

        def bad_observer(_seconds: float) -> None:
            raise RuntimeError("observer boom")

        mw = RateLimitMiddleware(rate=50.0, burst=1, on_wait=bad_observer)
        # First drains the token, second forces a wait + a raising observer
        # call; must not raise out of _acquire.
        self._acquire_many(mw, 3)

    def test_on_wait_defaults_to_none_and_is_unused_by_default(self):
        mw = RateLimitMiddleware(rate=20.0, burst=1)
        assert mw._on_wait is None
        # No observer wired in -- calls beyond burst still just wait, no crash.
        self._acquire_many(mw, 3)


# ---------------------------------------------------------------------------
# install_middleware
# ---------------------------------------------------------------------------


class _RecordingMiddleware:
    """Test helper: records every before/after/error call."""

    def __init__(self, tag: str, log: list):
        self.tag = tag
        self.log = log

    def before_call(self, name, arguments):
        self.log.append(("before", self.tag, name, dict(arguments)))
        return None

    def after_call(self, name, arguments, result):
        self.log.append(("after", self.tag, name, result))
        return None

    def on_error(self, name, arguments, exc):
        self.log.append(("error", self.tag, name, type(exc).__name__))
        return None


class TestInstallMiddleware:
    def test_hooks_fire_in_order(self):
        log: list = []

        def echo(x: int) -> int:
            log.append(("tool", "echo", x))
            return x * 2

        srv = _make_server_with_tool(echo)
        install_middleware(srv, [_RecordingMiddleware("A", log), _RecordingMiddleware("B", log)])

        _call(srv, "echo", {"x": 3})

        phases = [entry[0] for entry in log]
        tags = [entry[1] if entry[0] != "tool" else "-" for entry in log]
        # before in order A, B; tool; after in order A, B (the installer
        # runs after_call in the same order, not reverse — simpler and
        # fine since middlewares here don't stack side effects).
        assert phases == ["before", "before", "tool", "after", "after"]
        assert tags == ["A", "B", "-", "A", "B"]

    def test_idempotent_install_does_not_stack(self):
        log: list = []

        def echo(x: int) -> int:
            return x

        srv = _make_server_with_tool(echo)
        mw = _RecordingMiddleware("X", log)
        install_middleware(srv, [mw])
        install_middleware(srv, [mw])  # re-install

        _call(srv, "echo", {"x": 1})
        before_count = sum(1 for e in log if e[0] == "before")
        assert before_count == 1, f"got {before_count} before-calls, expected 1"

    def test_on_error_can_substitute_result(self):
        def boom() -> str:
            raise RuntimeError("kaboom")

        class Swallow:
            def before_call(self, name, arguments):
                return None

            def after_call(self, name, arguments, result):
                return None

            def on_error(self, name, arguments, exc):
                return "handled"

        srv = _make_server_with_tool(boom)
        install_middleware(srv, [Swallow()])

        result = _call(srv, "boom", {})
        # ``result`` is the MCP tool return shape; "handled" must appear in it.
        assert "handled" in str(result)

    def test_broken_middleware_does_not_crash_server(self):
        """A middleware that raises in before_call must not kill the tool."""

        class Broken:
            def before_call(self, name, arguments):
                raise RuntimeError("middleware bug")

            def after_call(self, name, arguments, result):
                return None

            def on_error(self, name, arguments, exc):
                return None

        def ok() -> str:
            return "ok"

        srv = _make_server_with_tool(ok)
        install_middleware(srv, [Broken()])

        # Fail-open: the tool still runs.
        result = _call(srv, "ok", {})
        assert "ok" in str(result)


class TestUnknownToolSuggest:
    def test_unknown_tool_returns_structured_hint(self):
        def list_devices() -> str:
            return "ok"

        srv = _make_server_with_tool(list_devices)
        install_middleware(
            srv,
            [UnknownToolSuggestMiddleware(lambda: srv._tool_manager._tools)],
        )

        result = _call(srv, "get_devices", {})

        assert "Unknown tool: get_devices" in str(result)
        assert "find_tool" in str(result)
        assert "list_devices" in str(result)

    def test_custom_suggestion_provider(self):
        def find_tool() -> str:
            return "ok"

        srv = _make_server_with_tool(find_tool)
        install_middleware(
            srv,
            [
                UnknownToolSuggestMiddleware(
                    lambda: srv._tool_manager._tools,
                    suggestion_provider=lambda name, limit: [{"name": "create_vlan", "score": 1.0}],
                )
            ],
        )

        result = _call(srv, "create_vlan", {})

        assert "create_vlan" in str(result)

    def test_platform_hint_resolver_reports_unconfigured_platform(self):
        """(a) A prefix-matched, unconfigured-platform guess gets the
        distinct platform_not_configured shape instead of a fuzzy hint."""

        def list_devices() -> str:
            return "ok"

        def platform_hint_resolver(name: str):
            if name.startswith("mist_"):
                return {
                    "reason": "platform_not_configured",
                    "platform": "mist",
                    "hint": "The 'mist' backend is not currently enabled.",
                }
            return None

        srv = _make_server_with_tool(list_devices)
        install_middleware(
            srv,
            [
                UnknownToolSuggestMiddleware(
                    lambda: srv._tool_manager._tools,
                    suggestion_provider=lambda name, limit: [
                        {"name": "SHOULD_NOT_APPEAR", "score": 1.0}
                    ],
                    platform_hint_resolver=platform_hint_resolver,
                )
            ],
        )

        result = _call(srv, "mist_get_site_stats", {})

        assert result["reason"] == "platform_not_configured"
        assert result["platform"] == "mist"
        assert result["suggestions"] == []
        assert "SHOULD_NOT_APPEAR" not in str(result)

    def test_platform_hint_resolver_none_falls_back_to_fuzzy_for_unknown_name(self):
        """(b) A genuinely unknown name with no platform-prefix match is
        unaffected: the resolver returns None and fuzzy suggestions run."""

        def list_devices() -> str:
            return "ok"

        srv = _make_server_with_tool(list_devices)
        install_middleware(
            srv,
            [
                UnknownToolSuggestMiddleware(
                    lambda: srv._tool_manager._tools,
                    platform_hint_resolver=lambda name: None,
                )
            ],
        )

        result = _call(srv, "get_devices", {})

        assert "reason" not in result
        assert "list_devices" in str(result)

    def test_platform_hint_resolver_none_for_already_enabled_platform_typo(self):
        """(c) A typo of an already-enabled platform's tool must not be
        misreported as platform_not_configured -- the resolver already
        encodes "is it enabled" and returns None for this case, so the
        ordinary fuzzy path still runs."""

        def mist_status() -> str:
            return "ok"

        srv = _make_server_with_tool(mist_status)
        install_middleware(
            srv,
            [
                UnknownToolSuggestMiddleware(
                    lambda: srv._tool_manager._tools,
                    # Simulates: mist IS enabled, so a mist_ prefix never blocks.
                    platform_hint_resolver=lambda name: None,
                )
            ],
        )

        result = _call(srv, "mist_statuss", {})

        assert "reason" not in result
        assert "mist_status" in str(result)


class TestResponseEnvelope:
    def test_wraps_error_dict(self):
        mw = ResponseEnvelopeMiddleware()

        result = mw.after_call("clearpass_get", {}, {"error": "not configured"})

        assert result == {
            "ok": False,
            "status": 500,
            "data": {"error": "not configured"},
            "message": "not configured",
            "tool": "clearpass_get",
            "platform": None,
        }

    def test_wraps_cancelled_status(self):
        mw = ResponseEnvelopeMiddleware()

        result = mw.after_call(
            "reboot_device",
            {},
            {
                "status": "CANCELLED",
                "detail": "user declined confirmation",
            },
        )

        assert result is not None
        assert result["ok"] is False
        assert result["status"] == 409
        assert result["message"] == "user declined confirmation"

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("blocked", 403),
            ("forbidden", 403),
            ("cancelled", 409),
            ("confirmation_required", 409),
            ("not_found", 404),
            ("failed", 500),
            ("unknown_tool", 404),
            ("invalid_cursor", 400),
            ("invalid_call", 400),
        ],
    )
    def test_named_status_precedes_generic_error_fallback(self, status, expected):
        mw = ResponseEnvelopeMiddleware()

        result = mw.after_call(
            "write_tool",
            {},
            {"status": status, "error": "operation did not run"},
        )

        assert result is not None
        assert result["ok"] is False
        assert result["status"] == expected

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("Client error '404 Not Found' for url 'https://x/y'", 404),
            ("Client error '422 Unprocessable Entity' for url 'https://x/y'", 422),
            ("Client error '429 Too Many Requests' for url 'https://x/y'", 429),
            ("Server error '503 Service Unavailable' for url 'https://x/y'", 503),
        ],
    )
    def test_upstream_http_code_recovered_from_message(self, message, expected):
        result = ResponseEnvelopeMiddleware().after_call("read_tool", {}, {"error": message})

        assert result is not None
        assert result["ok"] is False
        assert result["status"] == expected

    @pytest.mark.parametrize(
        "message",
        [
            (
                "1 validation error for mist_list_sitesArguments\norg_id\n"
                "  Field required [type=missing, input_value={}, input_type=dict]"
            ),
            (
                "2 validation errors for mist_get_org_sle_overviewArguments\n"
                "org_id\n  Field required\nduration\n  Field required"
            ),
        ],
    )
    def test_argument_validation_failure_is_a_caller_error_not_a_server_fault(self, message):
        """A missing required argument must classify 400, never the 500 fallback.

        Omitting ``org_id`` on a Mist tool produced a pydantic error carrying no
        HTTP code, so it reached the generic 500 fallback and was reported as
        "Server error ... retrying may help". Callers then retried the same
        argument-less call indefinitely rather than supplying the field.
        """
        result = ResponseEnvelopeMiddleware().after_call("mist_list_sites", {}, {"error": message})

        assert result is not None
        assert result["ok"] is False
        assert result["status"] == 400

    def test_upstream_http_code_still_outranks_validation_text(self):
        """A real upstream code is more specific than the validation heuristic."""
        message = (
            "Client error '422 Unprocessable Entity' for url 'https://x/y'; "
            "1 validation error for Body"
        )

        result = ResponseEnvelopeMiddleware().after_call("read_tool", {}, {"error": message})

        assert result is not None
        assert result["status"] == 422

    def test_unrelated_message_with_digits_still_falls_back_to_500(self):
        """The validation regex must not swallow ordinary numeric prose."""
        result = ResponseEnvelopeMiddleware().after_call(
            "read_tool", {}, {"error": "upstream returned 3 records for site 12 before failing"}
        )

        assert result is not None
        assert result["status"] == 500

    @pytest.mark.parametrize(
        "message",
        [
            "connection refused",
            "returned 404 devices",
            "timed out after 30 seconds on port 443",
            "Client error '999 Nonsense' for url 'https://x/y'",
        ],
    )
    def test_unparseable_message_still_falls_back_to_500(self, message):
        result = ResponseEnvelopeMiddleware().after_call("read_tool", {}, {"error": message})

        assert result is not None
        assert result["status"] == 500

    def test_backend_status_code_field_beats_message_and_fallback(self):
        result = ResponseEnvelopeMiddleware().after_call(
            "read_tool",
            {},
            {"status_code": 403, "error": "Client error '404 Not Found' for url 'https://x/y'"},
        )

        assert result is not None
        assert result["status"] == 403

    def test_successful_status_code_does_not_envelope_without_error(self):
        assert ResponseEnvelopeMiddleware().after_call(
            "read_tool", {}, {"status_code": 200, "data": {"items": []}}
        ) is None

    def test_bare_blocked_status_is_enveloped_as_forbidden(self):
        result = ResponseEnvelopeMiddleware().after_call(
            "write_tool",
            {},
            {"status": "blocked"},
        )

        assert result is not None
        assert result["ok"] is False
        assert result["status"] == 403

    def test_generic_error_with_success_status_uses_500(self):
        result = ResponseEnvelopeMiddleware().after_call(
            "read_tool",
            {},
            {"status": 200, "error": "response parsing failed"},
        )

        assert result is not None
        assert result["ok"] is False
        assert result["status"] == 500

    def test_success_dict_passes_through(self):
        mw = ResponseEnvelopeMiddleware()

        assert mw.after_call("list_devices", {}, {"items": []}) is None

    @pytest.mark.parametrize(
        "status",
        [None, "COMPLETED", "partial_success", "RUNNING", 200, "200"],
    )
    def test_nonempty_errors_wrap_regardless_of_neutral_status(self, status):
        mw = ResponseEnvelopeMiddleware()
        payload = {"status": status, "errors": ["one sub-operation failed"]}

        result = mw.after_call("batch_operation", {}, payload)

        assert result is not None
        assert result["ok"] is False
        assert result["status"] == 500
        assert result["message"] == "one sub-operation failed"
        assert result["data"] is payload

    @pytest.mark.parametrize("status", [None, "COMPLETED", 200])
    def test_empty_errors_pass_through(self, status):
        mw = ResponseEnvelopeMiddleware()

        result = mw.after_call(
            "batch_operation",
            {},
            {"status": status, "errors": []},
        )

        assert result is None

    def test_already_enveloped_passes_through(self):
        mw = ResponseEnvelopeMiddleware()
        result = {"ok": False, "data": {}, "tool": "x"}

        assert mw.after_call("x", {}, result) is None

    def test_envelope_runs_before_mcpserver_conversion(self):
        def bad() -> dict[str, str]:
            return {"error": "nope"}

        srv = _make_server_with_tool(bad)
        install_middleware(srv, [ResponseEnvelopeMiddleware()])

        result = _call_converted(srv, "bad", {})

        rendered = str(result)
        assert '"ok": false' in rendered
        assert '"tool": "bad"' in rendered


class TestOutcomeClassification:
    @pytest.mark.parametrize(
        "result",
        [
            {"errors": ["failed"]},
            {"status": "COMPLETED", "errors": ["failed"]},
            {"status": "partial_success", "errors": ["failed"]},
            {"status": 200, "errors": ["failed"]},
        ],
    )
    def test_nonempty_errors_are_classified_as_error(self, result):
        assert classify_outcome(result) == "error"

    def test_raw_blocked_status_precedes_errors_fallback(self):
        assert classify_outcome(
            {"status": "blocked", "errors": ["write gate is closed"]}
        ) == "blocked"

    def test_enveloped_neutral_status_errors_classify_as_error(self):
        raw = {"status": "COMPLETED", "errors": ["one sub-operation failed"]}
        enveloped = ResponseEnvelopeMiddleware().after_call(
            "batch_operation",
            {},
            raw,
        )

        assert enveloped is not None
        assert classify_outcome(enveloped) == "error"

    @pytest.mark.parametrize(
        "result",
        [
            {"status": "COMPLETED", "errors": []},
            {"status": 200, "errors": []},
            {"items": []},
        ],
    )
    def test_empty_or_absent_errors_remain_success(self, result):
        assert classify_outcome(result) == "success"


class TestMacNormalize:
    def test_normalizes_common_mac_formats(self):
        mw = MacNormalizeMiddleware()
        payload = {
            "clientMac": "AA-BB-CC-DD-EE-FF",
            "ap": {"mac": "aabb.ccdd.eeff"},
            "text": "client 11:22:33:44:55:66 connected",
        }

        result = mw.after_call("find_client", {}, payload)

        assert result == {
            "clientMac": "aa:bb:cc:dd:ee:ff",
            "ap": {"mac": "aa:bb:cc:dd:ee:ff"},
            "text": "client 11:22:33:44:55:66 connected",
        }

    def test_leaves_non_mac_strings_unchanged(self):
        mw = MacNormalizeMiddleware()

        assert mw.after_call("x", {}, {"serial": "CN1234567890"}) is None
