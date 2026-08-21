from __future__ import annotations

import asyncio
import json

import pytest

import hpe_networking_mcp.mcp_servers.mist as mist


class _FakeWebSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.closed = False

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        if not self.messages:
            await asyncio.Future()
        message = self.messages.pop(0)
        if isinstance(message, BaseException):
            raise message
        return message


class _FakeConnect:
    def __init__(self, websocket, calls):
        self.websocket = websocket
        self.calls = calls

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        websocket = self.websocket

        class _Context:
            async def __aenter__(self):
                return websocket

            async def __aexit__(self, exc_type, exc, tb):
                websocket.closed = True

        return _Context()


def _configure(monkeypatch):
    monkeypatch.setenv("MIST_HOST", "https://api.gc1.mist.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret-token")
    monkeypatch.delenv("MIST_SESSION_COOKIE", raising=False)
    monkeypatch.delenv("MIST_CSRF_TOKEN", raising=False)


def _event(site="site1", device="device1", session="session1", **data):
    return json.dumps(
        {
            "event": "data",
            "channel": f"/sites/{site}/devices/{device}/cmd",
            "data": {"session": session, **data},
        }
    )


def test_collect_diagnostic_uses_regional_url_and_token_auth(monkeypatch):
    _configure(monkeypatch)
    calls = []
    websocket = _FakeWebSocket([_event(finished=True, raw="done")])
    monkeypatch.setattr(mist, "_mist_websocket_connect", _FakeConnect(websocket, calls))

    out = asyncio.run(
        mist.mist_collect_diagnostic_results("site1", "device1", "session1")
    )

    assert out["status"] == "completed"
    assert calls[0]["url"] == "wss://api-ws.gc1.mist.com/api-ws/v1/stream"
    assert calls[0]["additional_headers"] == {"Authorization": "Token secret-token"}
    assert websocket.sent == [{"subscribe": "/sites/site1/devices/device1/cmd"}]
    assert websocket.closed is True


def test_collect_diagnostic_uses_complete_session_auth(monkeypatch):
    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.delenv("MIST_API_TOKEN", raising=False)
    monkeypatch.setenv("MIST_SESSION_COOKIE", "sessionid=session-secret")
    monkeypatch.setenv("MIST_CSRF_TOKEN", "csrf-secret")
    calls = []
    websocket = _FakeWebSocket([_event(finished=True)])
    monkeypatch.setattr(mist, "_mist_websocket_connect", _FakeConnect(websocket, calls))

    out = asyncio.run(
        mist.mist_collect_diagnostic_results("site1", "device1", "session1")
    )

    assert out["status"] == "completed"
    assert calls[0]["additional_headers"] == {
        "Cookie": "sessionid=session-secret",
        "X-CSRFToken": "csrf-secret",
    }


def test_collect_diagnostic_correlates_site_device_and_session(monkeypatch):
    _configure(monkeypatch)
    calls = []
    websocket = _FakeWebSocket(
        [
            _event(site="other", finished=True),
            _event(device="other", finished=True),
            _event(session="other", finished=True),
            _event(raw="part"),
            _event(finished=True, raw="done"),
        ]
    )
    monkeypatch.setattr(mist, "_mist_websocket_connect", _FakeConnect(websocket, calls))

    out = asyncio.run(
        mist.mist_collect_diagnostic_results(
            "site1", "device1", "session1", max_events=10
        )
    )

    assert out["status"] == "completed"
    assert out["unrelated_events"] == 3
    assert [event["data"]["raw"] for event in out["events"]] == ["part", "done"]


def test_collect_diagnostic_decodes_nested_event_and_redacts(monkeypatch):
    _configure(monkeypatch)
    calls = []
    nested = _event(raw='{"finished":true,"token":"secret-token"}')
    outer = json.dumps({"event": "data", "channel": "/ignored", "data": nested})
    websocket = _FakeWebSocket([outer])
    monkeypatch.setattr(mist, "_mist_websocket_connect", _FakeConnect(websocket, calls))

    out = asyncio.run(
        mist.mist_collect_diagnostic_results("site1", "device1", "session1")
    )

    assert out["status"] == "completed"
    assert "secret-token" not in json.dumps(out)
    # The nested "raw" blob's own "token" field is now caught by two
    # legitimate, overlapping defenses: shared.redact_sensitive's key-based
    # nested-blob detection (marker "******") fires first and removes the
    # plaintext, so _redact_diagnostic_data's known-value substring pass
    # (marker "[REDACTED]") no longer finds anything left to replace.
    # Assert the actual security property -- the plaintext token is gone --
    # rather than coupling to which specific marker won the race.
    raw = out["events"][0]["data"]["raw"]
    assert "secret-token" not in raw
    assert "[REDACTED]" in raw or "******" in raw


def test_collect_diagnostic_skips_malformed_messages(monkeypatch):
    _configure(monkeypatch)
    calls = []
    websocket = _FakeWebSocket(["not-json", json.dumps({"event": "data"}), _event(finished=True)])
    monkeypatch.setattr(mist, "_mist_websocket_connect", _FakeConnect(websocket, calls))

    out = asyncio.run(
        mist.mist_collect_diagnostic_results("site1", "device1", "session1")
    )

    assert out["status"] == "completed"
    assert out["malformed_events"] == 2


def test_collect_diagnostic_timeout_is_explicit_and_closes(monkeypatch):
    _configure(monkeypatch)
    calls = []
    websocket = _FakeWebSocket([])
    monkeypatch.setattr(mist, "_mist_websocket_connect", _FakeConnect(websocket, calls))

    out = asyncio.run(
        mist.mist_collect_diagnostic_results(
            "site1", "device1", "session1", timeout_seconds=0.01
        )
    )

    assert out["status"] == "timeout"
    assert out["completed"] is False
    assert "error" in out
    assert websocket.closed is True


def test_collect_diagnostic_unmatched_timeout_is_not_success(monkeypatch):
    _configure(monkeypatch)
    calls = []
    websocket = _FakeWebSocket([_event(session="other", finished=True)])
    monkeypatch.setattr(mist, "_mist_websocket_connect", _FakeConnect(websocket, calls))

    out = asyncio.run(
        mist.mist_collect_diagnostic_results(
            "site1", "device1", "session1", timeout_seconds=0.01
        )
    )

    assert out["status"] == "timeout"
    assert out["completed"] is False
    assert out["matched_events"] == 0
    assert out["unrelated_events"] == 1


def test_collect_diagnostic_receive_error_closes_connection(monkeypatch):
    _configure(monkeypatch)
    calls = []
    websocket = _FakeWebSocket([RuntimeError("stream failed")])
    monkeypatch.setattr(mist, "_mist_websocket_connect", _FakeConnect(websocket, calls))

    out = asyncio.run(
        mist.mist_collect_diagnostic_results("site1", "device1", "session1")
    )

    assert out["status"] == "connection_error"
    assert "stream failed" in out["error"]
    assert websocket.closed is True


def test_collect_diagnostic_event_and_byte_bounds(monkeypatch):
    _configure(monkeypatch)
    calls = []
    websocket = _FakeWebSocket([_event(raw="one"), _event(raw="two")])
    monkeypatch.setattr(mist, "_mist_websocket_connect", _FakeConnect(websocket, calls))

    event_limited = asyncio.run(
        mist.mist_collect_diagnostic_results(
            "site1", "device1", "session1", max_events=1
        )
    )

    assert event_limited["status"] == "event_limit"
    assert len(event_limited["events"]) == 1
    assert websocket.closed is True

    calls = []
    websocket = _FakeWebSocket([_event(raw="x" * 2000)])
    monkeypatch.setattr(mist, "_mist_websocket_connect", _FakeConnect(websocket, calls))
    byte_limited = asyncio.run(
        mist.mist_collect_diagnostic_results(
            "site1", "device1", "session1", max_bytes=1024
        )
    )

    assert byte_limited["status"] == "byte_limit"
    assert byte_limited["events"] == []
    assert websocket.closed is True


def test_collect_diagnostic_rejects_unsupported_host_and_partial_session(monkeypatch):
    monkeypatch.setenv("MIST_HOST", "https://example.com")
    monkeypatch.setenv("MIST_API_TOKEN", "secret")
    out = asyncio.run(
        mist.mist_collect_diagnostic_results("site1", "device1", "session1")
    )
    assert out["status"] == "configuration_error"
    assert "do not support configured host" in out["error"]

    monkeypatch.setenv("MIST_HOST", "https://api.mist.com")
    monkeypatch.delenv("MIST_API_TOKEN", raising=False)
    monkeypatch.setenv("MIST_SESSION_COOKIE", "sessionid=secret")
    monkeypatch.delenv("MIST_CSRF_TOKEN", raising=False)
    out = asyncio.run(
        mist.mist_collect_diagnostic_results("site1", "device1", "session1")
    )
    assert out["status"] == "configuration_error"
    assert "requires both" in out["error"]


def test_collect_diagnostic_cancellation_closes_connection(monkeypatch):
    _configure(monkeypatch)
    calls = []
    websocket = _FakeWebSocket([])
    monkeypatch.setattr(mist, "_mist_websocket_connect", _FakeConnect(websocket, calls))

    async def _cancel():
        task = asyncio.create_task(
            mist.mist_collect_diagnostic_results(
                "site1", "device1", "session1", timeout_seconds=10
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_cancel())
    assert websocket.closed is True
