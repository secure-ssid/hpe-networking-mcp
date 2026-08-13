"""Unit tests for invoke_read_tool's opaque continuation cursors (v0.7 follow-up).

Covers ``hpe_networking_mcp.mcp_servers.tool_router``'s cursor subsystem:

- Multi-page round trips (no duplicated/skipped items), including nested
  primary-list dict responses and the shared 200-item clamp.
- Correct next-offset math after byte-driven shrinking.
- Every tamper/expiry/mismatch/restart/length failure mode returns an
  error-shaped dict *without* ever calling the backend.
- Only capability "read" tools (via invoke_read_tool) can emit or consume
  a cursor -- diagnostic/write/destructive tools and invoke_tool never do.
- Decoded cursor payloads never leak raw arguments/identifiers/secrets.
- A single oversized item is reported as explicitly non-resumable instead
  of producing an endless-loop cursor.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import pytest
from mcp.server.mcpserver import Context, MCPServer

import hpe_networking_mcp.mcp_servers.tool_router as router
from hpe_networking_mcp.mcp_servers.shared import DESTRUCTIVE, DIAGNOSTIC, READ_ONLY, WRITE

# ---------------------------------------------------------------------------
# Backend fixture: one tool per capability, plus a big-list read tool.
# ---------------------------------------------------------------------------


def _build_backend() -> MCPServer:
    srv = MCPServer("cursor-test-backend")

    @srv.tool(annotations=READ_ONLY)
    def list_items(count: int = 250, filter: str | None = None) -> list[int]:
        return list(range(count))

    @srv.tool(annotations=READ_ONLY)
    def list_devices(count: int = 250) -> dict[str, Any]:
        return {"devices": [{"serial": f"sn-{i}"} for i in range(count)], "meta": "ok"}

    @srv.tool(annotations=READ_ONLY)
    def huge_single_item() -> dict[str, Any]:
        return {"items": ["z" * 5000]}

    @srv.tool(annotations=READ_ONLY)
    def small_result() -> dict[str, Any]:
        return {"ok": True}

    @srv.tool(annotations=DIAGNOSTIC)
    def diagnostic_big_list() -> list[int]:
        return list(range(500))

    @srv.tool(annotations=WRITE)
    def write_big_list() -> list[int]:
        return list(range(500))

    @srv.tool(annotations=DESTRUCTIVE)
    def destructive_big_list() -> list[int]:
        return list(range(500))

    return srv


@pytest.fixture
def wired_cursor_router(monkeypatch):
    backend = _build_backend()
    tools = dict(backend._tool_manager._tools)
    servers = {name: backend for name in tools}
    monkeypatch.setattr(router, "_tool_index", tools, raising=True)
    monkeypatch.setattr(router, "_tool_servers", servers, raising=True)
    monkeypatch.setattr(
        router, "_tool_backend_names", {name: "cursor-test-backend" for name in tools}, raising=True
    )
    monkeypatch.setattr(router, "_load_all_backends", lambda: None, raising=True)
    return backend


def _invoke_read(name: str, arguments: dict[str, Any] | None = None, cursor: str | None = None):
    ctx = Context(mcp_server=router.mcp)
    return asyncio.run(router.invoke_read_tool(ctx, name, arguments, cursor))


def _invoke(name: str, arguments: dict[str, Any] | None = None):
    ctx = Context(mcp_server=router.mcp)
    return asyncio.run(router.invoke_tool(ctx, name, arguments))


# ---------------------------------------------------------------------------
# Multi-page round trips
# ---------------------------------------------------------------------------


class TestMultiPageRoundTrip:
    def test_two_plus_pages_no_overlap_or_gaps(self, wired_cursor_router, monkeypatch):
        monkeypatch.setenv(router._RESPONSE_BUDGET_ITEMS_ENV, "40")
        collected: list[int] = []
        out = _invoke_read("list_items", {"count": 100})
        assert out["_response_bounds"]["truncated"] is True
        assert "next_cursor" in out
        collected.extend(out["items"])

        pages = 1
        while "next_cursor" in out:
            out = _invoke_read("list_items", {"count": 100}, cursor=out["next_cursor"])
            assert "error" not in out
            collected.extend(out["items"])
            pages += 1
            assert pages < 20  # guard against an infinite loop bug

        assert pages >= 3  # 100 items / 40 per page => 3 pages
        assert collected == list(range(100))

    def test_nested_primary_list_dict_pagination(self, wired_cursor_router, monkeypatch):
        monkeypatch.setenv(router._RESPONSE_BUDGET_ITEMS_ENV, "30")
        collected: list[dict[str, Any]] = []
        out = _invoke_read("list_devices", {"count": 90})
        collected.extend(out["devices"])
        while "next_cursor" in out:
            out = _invoke_read("list_devices", {"count": 90}, cursor=out["next_cursor"])
            assert "error" not in out
            collected.extend(out["devices"])
        assert collected == [{"serial": f"sn-{i}"} for i in range(90)]
        # non-list keys must survive every page untouched
        assert out["meta"] == "ok"

    def test_default_shared_200_item_clamp_respected_across_pages(self, wired_cursor_router):
        # No explicit max_items override: default budget is clamped to
        # MAX_LIST_LIMIT (200) even though the backend returns 250 items.
        out = _invoke_read("list_items", {"count": 250})
        assert len(out["items"]) == router.MAX_LIST_LIMIT
        assert "next_cursor" in out
        out2 = _invoke_read("list_items", {"count": 250}, cursor=out["next_cursor"])
        assert "error" not in out2
        assert out2["items"] == list(range(router.MAX_LIST_LIMIT, 250))
        assert "next_cursor" not in out2  # final page, nothing left to resume
        assert "_response_bounds" not in out2  # final page, nothing left to resume

    def test_byte_shrunk_page_next_offset_is_the_actual_applied_page_size(
        self, wired_cursor_router, monkeypatch
    ):
        # Force a byte-driven shrink well below the requested item budget,
        # then confirm the next page picks up exactly where the *actual*
        # (shrunk) page left off -- no duplicates, no gaps.
        monkeypatch.setattr(router, "_response_budget_items", lambda: 50, raising=True)
        monkeypatch.setattr(router, "_response_budget_bytes", lambda: 400, raising=True)
        out = _invoke_read("list_devices", {"count": 60})
        first_page_len = len(out["devices"])
        assert 0 < first_page_len < 50  # confirms byte budget, not item budget, drove the shrink
        assert out["_pagination"]["offset"] == 0
        assert "next_cursor" in out

        out2 = _invoke_read("list_devices", {"count": 60}, cursor=out["next_cursor"])
        assert "error" not in out2
        assert out2["_pagination"]["offset"] == first_page_len
        next_end = first_page_len + len(out2["devices"])
        expected_next = [
            {"serial": f"sn-{i}"} for i in range(first_page_len, next_end)
        ]
        assert out2["devices"] == expected_next


# ---------------------------------------------------------------------------
# Cursor validation failure modes
# ---------------------------------------------------------------------------


class TestCursorValidationFailures:
    def _first_cursor(self, wired_cursor_router, monkeypatch) -> str:
        monkeypatch.setenv(router._RESPONSE_BUDGET_ITEMS_ENV, "10")
        out = _invoke_read("list_items", {"count": 100})
        assert "next_cursor" in out
        return out["next_cursor"]

    def test_tampered_cursor_rejected(self, wired_cursor_router, monkeypatch):
        cursor = self._first_cursor(wired_cursor_router, monkeypatch)
        payload_part, sig_part = cursor.split(".")
        tampered = payload_part + "x" + "." + sig_part
        out = _invoke_read("list_items", {"count": 100}, cursor=tampered)
        assert out["status"] == "invalid_cursor"
        assert "error" in out

    def test_flipped_signature_rejected(self, wired_cursor_router, monkeypatch):
        cursor = self._first_cursor(wired_cursor_router, monkeypatch)
        payload_part, sig_part = cursor.split(".")
        # Flip a character well before the end (avoids the base64 last-symbol
        # padding-bit edge case, where altering only unused trailing bits can
        # decode back to the same bytes).
        idx = 0
        flipped_char = "A" if sig_part[idx] != "A" else "B"
        flipped_sig = flipped_char + sig_part[1:]
        out = _invoke_read("list_items", {"count": 100}, cursor=f"{payload_part}.{flipped_sig}")
        assert out["status"] == "invalid_cursor"

    def test_expired_cursor_rejected(self, wired_cursor_router, monkeypatch):
        monkeypatch.setattr(router, "_cursor_ttl_seconds", lambda: 1, raising=True)
        cursor = self._first_cursor(wired_cursor_router, monkeypatch)
        time.sleep(1.2)
        out = _invoke_read("list_items", {"count": 100}, cursor=cursor)
        assert out["status"] == "invalid_cursor"
        assert "expired" in out["error"].lower()

    def test_wrong_tool_name_cursor_rejected(self, wired_cursor_router, monkeypatch):
        cursor = self._first_cursor(wired_cursor_router, monkeypatch)
        out = _invoke_read("list_devices", {"count": 100}, cursor=cursor)
        assert out["status"] == "invalid_cursor"

    def test_changed_arguments_cursor_rejected(self, wired_cursor_router, monkeypatch):
        cursor = self._first_cursor(wired_cursor_router, monkeypatch)
        out = _invoke_read("list_items", {"count": 999}, cursor=cursor)
        assert out["status"] == "invalid_cursor"

    def test_restart_key_mismatch_rejected(self, wired_cursor_router, monkeypatch):
        cursor = self._first_cursor(wired_cursor_router, monkeypatch)
        new_key = router.secrets.token_bytes(32)
        monkeypatch.setattr(router, "_CURSOR_HMAC_KEY", new_key, raising=True)
        out = _invoke_read("list_items", {"count": 100}, cursor=cursor)
        assert out["status"] == "invalid_cursor"
        assert "restart" in out["error"].lower() or "invalid" in out["error"].lower()

    def test_oversized_cursor_string_rejected(self, wired_cursor_router):
        oversized = "a" * (router._CURSOR_MAX_LENGTH + 1)
        out = _invoke_read("list_items", {"count": 100}, cursor=oversized)
        assert out["status"] == "invalid_cursor"

    def test_malformed_cursor_missing_separator_rejected(self, wired_cursor_router):
        out = _invoke_read("list_items", {"count": 100}, cursor="not-a-real-cursor")
        assert out["status"] == "invalid_cursor"

    def test_invalid_cursor_never_calls_the_backend(self, wired_cursor_router, monkeypatch):
        calls: list[str] = []
        original = wired_cursor_router._tool_manager.call_tool

        async def spy(name, args, context=None):
            calls.append(name)
            return await original(name, args, context=context)

        monkeypatch.setattr(wired_cursor_router._tool_manager, "call_tool", spy, raising=True)
        out = _invoke_read("list_items", {"count": 100}, cursor="totally-bogus")
        assert out["status"] == "invalid_cursor"
        assert calls == []


# ---------------------------------------------------------------------------
# Capability/permission separation
# ---------------------------------------------------------------------------


class TestCursorCapabilitySeparation:
    def test_diagnostic_tool_never_emits_cursor_even_when_oversized(
        self, wired_cursor_router, monkeypatch
    ):
        monkeypatch.setenv(router._RESPONSE_BUDGET_ITEMS_ENV, "10")
        out = _invoke_read("diagnostic_big_list", {})
        assert out.get("status") == "blocked"
        assert "next_cursor" not in out

    def test_write_tool_never_emits_cursor_through_invoke_tool(
        self, wired_cursor_router, monkeypatch
    ):
        monkeypatch.setenv(router._RESPONSE_BUDGET_ITEMS_ENV, "10")
        out = _invoke("write_big_list", {})
        assert isinstance(out, dict)
        assert "next_cursor" not in out

    def test_destructive_tool_never_emits_cursor_through_invoke_tool(
        self, wired_cursor_router, monkeypatch
    ):
        monkeypatch.setenv(router._RESPONSE_BUDGET_ITEMS_ENV, "10")
        out = _invoke("destructive_big_list", {})
        assert isinstance(out, dict)
        assert "next_cursor" not in out

    def test_invoke_tool_never_emits_cursor_even_for_read_tool(
        self, wired_cursor_router, monkeypatch
    ):
        # invoke_tool has no cursor parameter and never sets enable_cursor,
        # even when dispatching a capability "read" tool with an oversized
        # response.
        monkeypatch.setenv(router._RESPONSE_BUDGET_ITEMS_ENV, "10")
        out = _invoke("list_items", {"count": 100})
        assert isinstance(out, dict)
        assert out["_response_bounds"]["truncated"] is True
        assert "next_cursor" not in out

    def test_invoke_tool_has_no_cursor_parameter_at_all(self):
        import inspect

        sig = inspect.signature(router.invoke_tool)
        assert "cursor" not in sig.parameters

    def test_write_and_destructive_tools_are_blocked_before_dispatch(self, wired_cursor_router):
        # Belt-and-suspenders: invoke_read_tool must still refuse
        # write/destructive tools outright (pre-existing behavior),
        # regardless of the new cursor parameter.
        out = _invoke_read("write_big_list", {})
        assert out["status"] == "blocked"
        out2 = _invoke_read("destructive_big_list", {})
        assert out2["status"] == "blocked"


# ---------------------------------------------------------------------------
# Cursor payload does not leak secrets/identifiers
# ---------------------------------------------------------------------------


class TestCursorPayloadRedaction:
    def test_decoded_cursor_payload_has_no_raw_arguments_or_secrets(
        self, wired_cursor_router, monkeypatch
    ):
        monkeypatch.setenv(router._RESPONSE_BUDGET_ITEMS_ENV, "10")
        secret_marker = "TOP-SECRET-FILTER-VALUE-12345"
        out = _invoke_read("list_items", {"count": 100, "filter": secret_marker})
        cursor = out["next_cursor"]
        payload_b64, _sig_b64 = cursor.split(".")
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))

        assert set(payload.keys()) == {"v", "exp", "off", "t", "a"}
        raw = json.dumps(payload)
        assert secret_marker not in raw
        assert "list_items" not in raw
        assert "count" not in raw
        assert "filter" not in raw
        assert isinstance(payload["off"], int)
        digest_len = router._CURSOR_DIGEST_HEX_CHARS
        assert isinstance(payload["t"], str) and len(payload["t"]) == digest_len
        assert isinstance(payload["a"], str) and len(payload["a"]) == digest_len

    def test_cursor_string_itself_has_no_plaintext_secret_substring(
        self, wired_cursor_router, monkeypatch
    ):
        monkeypatch.setenv(router._RESPONSE_BUDGET_ITEMS_ENV, "10")
        secret_marker = "ANOTHER-SECRET-TOKEN-999"
        out = _invoke_read("list_items", {"count": 100, "filter": secret_marker})
        assert secret_marker not in out["next_cursor"]
        assert "list_items" not in out["next_cursor"]


# ---------------------------------------------------------------------------
# Non-resumable oversized single item (no endless-loop cursor)
# ---------------------------------------------------------------------------


class TestNonResumableOversizedItem:
    def test_huge_single_item_reports_non_resumable_with_no_cursor(
        self, wired_cursor_router, monkeypatch
    ):
        monkeypatch.setenv(router._RESPONSE_BUDGET_BYTES_ENV, "1024")
        out = _invoke_read("huge_single_item", {})
        assert "next_cursor" not in out
        assert out["_response_bounds"]["resumable"] is False
        assert out["_response_bounds"]["reason"] == "byte_budget"
        assert "preview" in out

    def test_small_result_unaffected_and_no_cursor(self, wired_cursor_router):
        out = _invoke_read("small_result", {})
        assert out == {"ok": True}
        assert "next_cursor" not in out


# ---------------------------------------------------------------------------
# Direct unit tests for the cursor encode/decode helpers
# ---------------------------------------------------------------------------


class TestCursorHelpersUnit:
    def test_round_trip_encode_decode(self):
        cursor = router._encode_continuation_cursor(
            name="some_tool", arguments={"a": 1}, next_offset=42
        )
        offset = router._decode_and_verify_continuation_cursor(
            cursor, name="some_tool", arguments={"a": 1}
        )
        assert offset == 42

    def test_cursor_length_is_bounded(self):
        cursor = router._encode_continuation_cursor(
            name="some_tool", arguments={"a": 1}, next_offset=42
        )
        assert len(cursor) <= router._CURSOR_MAX_LENGTH

    def test_decode_rejects_wrong_arguments(self):
        cursor = router._encode_continuation_cursor(
            name="some_tool", arguments={"a": 1}, next_offset=42
        )
        with pytest.raises(router.CursorError):
            router._decode_and_verify_continuation_cursor(
                cursor, name="some_tool", arguments={"a": 2}
            )

    def test_decode_rejects_none_or_empty(self):
        with pytest.raises(router.CursorError):
            router._decode_and_verify_continuation_cursor("", name="t", arguments={})

    def test_decode_rejects_missing_dot(self):
        with pytest.raises(router.CursorError):
            router._decode_and_verify_continuation_cursor("nodothere", name="t", arguments={})
