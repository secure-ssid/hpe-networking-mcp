from __future__ import annotations

import asyncio
import json

import hpe_networking_mcp.mcp_servers.central_streaming as streaming


class _FakeClient:
    base_url = "https://central.example.test"

    class _Tokens:
        def get_access_token(self):
            return "central-secret"

    token_manager = _Tokens()


class _FakeWebSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.closed = False

    async def recv(self):
        if not self.messages:
            await asyncio.Future()
        message = self.messages.pop(0)
        if isinstance(message, BaseException):
            raise message
        return message


class _FakeConnect:
    def __init__(self, sockets):
        self.sockets = list(sockets)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        socket = self.sockets.pop(0)

        class _Context:
            async def __aenter__(self):
                return socket

            async def __aexit__(self, exc_type, exc, tb):
                socket.closed = True

        return _Context()


def _configure(monkeypatch):
    monkeypatch.setattr(streaming, "get_client", lambda: _FakeClient())


def _event(
    event_type="com.hpe.greenlake.network-monitoring.v1alpha1.ap.updated",
    padding=0,
):
    return json.dumps(
        {
            "id": "event-1",
            "source": "central",
            "type": event_type,
            "time": "2026-08-20T12:00:00Z",
            "data": {
                "serial_number": "AP1",
                "status": "up",
                **({"padding": "x" * padding} if padding else {}),
            },
        }
    )


def test_preflight_requires_advanced_subscription(monkeypatch):
    called = False

    def fail_client():
        nonlocal called
        called = True
        raise AssertionError("client must not be touched")

    monkeypatch.setattr(streaming, "get_client", fail_client)
    out = asyncio.run(
        streaming.central_collect_streaming_events("ap-monitoring")
    )
    assert out["status"] == "subscription_error"
    assert called is False


def test_rejects_topic_and_event_type_before_auth(monkeypatch):
    _configure(monkeypatch)
    invalid_topic = asyncio.run(
        streaming.central_collect_streaming_events(
            "unknown", advanced_subscription=True
        )
    )
    assert invalid_topic["status"] == "validation_error"
    invalid_event = asyncio.run(
        streaming.central_collect_streaming_events(
            "geofence",
            advanced_subscription=True,
            event_types=["com.hpe.greenlake.network-services.v1alpha1.audit-trail.configuration"],
        )
    )
    assert invalid_event["status"] == "validation_error"


def test_connects_with_bearer_and_event_filter_and_normalizes_json(monkeypatch):
    _configure(monkeypatch)
    connector = _FakeConnect([_FakeWebSocket([_event(), _event()])])
    monkeypatch.setattr(streaming, "_WEBSOCKET_CONNECT", connector)

    out = asyncio.run(
        streaming.central_collect_streaming_events(
            "ap-monitoring",
            advanced_subscription=True,
            event_types=["com.hpe.greenlake.network-monitoring.v1alpha1.ap.updated"],
            max_events=2,
        )
    )

    assert out["status"] == "event_limit"
    assert out["events"][0]["device_id"] == "AP1"
    assert connector.calls[0]["additional_headers"] == {
        "Authorization": "Bearer central-secret"
    }
    assert "event-types=" in connector.calls[0]["url"]
    assert "central-secret" not in json.dumps(out)


def test_reconnects_after_connection_failure(monkeypatch):
    _configure(monkeypatch)
    connector = _FakeConnect(
        [
            _FakeWebSocket([RuntimeError("closed")]),
            _FakeWebSocket([_event()]),
        ]
    )
    monkeypatch.setattr(streaming, "_WEBSOCKET_CONNECT", connector)

    out = asyncio.run(
        streaming.central_collect_streaming_events(
            "ap-monitoring",
            advanced_subscription=True,
            timeout_seconds=2,
            max_events=1,
            max_reconnects=1,
        )
    )
    assert out["status"] == "event_limit"
    assert out["reconnects"] == 1
    assert out["attempts"] == 2


def test_binary_payload_is_bounded_opaque_and_redacted_from_output(monkeypatch):
    _configure(monkeypatch)
    connector = _FakeConnect([_FakeWebSocket([b"\x08\xff\x00"])])
    monkeypatch.setattr(streaming, "_WEBSOCKET_CONNECT", connector)
    out = asyncio.run(
        streaming.central_collect_streaming_events(
            "audit-trail",
            advanced_subscription=True,
            max_events=1,
            include_raw=True,
        )
    )
    assert out["status"] == "event_limit"
    assert out["events"][0]["decoded"] is False
    assert out["events"][0]["payload_encoding"] == "protobuf"
    assert out["events"][0]["raw_payload_base64"]


def test_event_and_byte_bounds_are_explicit(monkeypatch):
    _configure(monkeypatch)
    connector = _FakeConnect([_FakeWebSocket([_event(), _event()])])
    monkeypatch.setattr(streaming, "_WEBSOCKET_CONNECT", connector)
    event_limited = asyncio.run(
        streaming.central_collect_streaming_events(
            "ap-monitoring", advanced_subscription=True, max_events=1
        )
    )
    assert event_limited["status"] == "event_limit"
    assert len(event_limited["events"]) == 1

    connector = _FakeConnect([_FakeWebSocket([_event(padding=2000)])])
    monkeypatch.setattr(streaming, "_WEBSOCKET_CONNECT", connector)
    byte_limited = asyncio.run(
        streaming.central_collect_streaming_events(
            "ap-monitoring", advanced_subscription=True, max_bytes=1024
        )
    )
    assert byte_limited["status"] == "byte_limit"
    assert byte_limited["events"] == []


def test_timeout_is_not_success(monkeypatch):
    _configure(monkeypatch)
    connector = _FakeConnect([_FakeWebSocket([])])
    monkeypatch.setattr(streaming, "_WEBSOCKET_CONNECT", connector)
    out = asyncio.run(
        streaming.central_collect_streaming_events(
            "location", advanced_subscription=True, timeout_seconds=0.01
        )
    )
    assert out["status"] == "timeout"
    assert out["completed"] is False
