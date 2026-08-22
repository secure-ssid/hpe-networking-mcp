"""MCP server — bounded Aruba Central Streaming API collection (1 tool).

Central Streaming uses WSS endpoints and CloudEvents encoded with Google
Protocol Buffers. This backend keeps the transport useful without requiring
generated protobuf classes: JSON fixtures are normalized, while binary
CloudEvents are reported as bounded opaque payloads unless a caller explicitly
requests raw preservation. The tool never writes to Central and uses the same
OAuth token manager as the REST clients.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from mcp.server.mcpserver import MCPServer
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import PayloadTooBig

from hpe_networking_mcp.mcp_servers.shared import (
    READ_ONLY,
    get_client,
    redact_sensitive,
    validate_product_base_url,
)

mcp = MCPServer("central-streaming")

_MAX_TIMEOUT = 120.0
_MAX_EVENTS = 200
_MAX_BYTES = 1_048_576
_MAX_RECONNECTS = 5
_MAX_EVENT_TYPES = 10
_MAX_EVENT_TYPE_LENGTH = 255
_INITIAL_RECONNECT_DELAY = 0.25
_MAX_RECONNECT_DELAY = 5.0
_CONNECTION_SOURCE = (
    "developer_docs/"
    "developer_arubanetworks_com_new-central_docs_streaming-api-connection-management.md"
)

_TOPICS: dict[str, dict[str, Any]] = {
    "ap-monitoring": {
        "path": "/network-monitoring/v1alpha1/ap-events",
        "event_prefix": "com.hpe.greenlake.network-monitoring.v1alpha1.",
        "source": (
            "developer_docs/"
            "developer_arubanetworks_com_new-central_docs_streaming-api-event-ap-monitoring.md"
        ),
    },
    "audit-trail": {
        "path": "/network-services/v1alpha1/audit-trail-events",
        "event_prefix": "com.hpe.greenlake.network-services.v1alpha1.audit-trail.",
        "source": (
            "developer_docs/"
            "developer_arubanetworks_com_new-central_docs_streaming-api-event-audit-trail.md"
        ),
    },
    "geofence": {
        "path": "/network-services/v1alpha1/geofence",
        "event_types": {
            "com.hpe.greenlake.network-services.v1alpha1.wifi-client-geofence-crossed",
            "com.hpe.greenlake.network-services.v1alpha1.asset-tag-geofence-crossed",
        },
        "source": (
            "developer_docs/"
            "developer_arubanetworks_com_new-central_docs_streaming-api-event-geofence.md"
        ),
    },
    "location": {
        "path": "/network-services/v1alpha1/location",
        "event_types": {
            "com.hpe.greenlake.network-services.v1alpha1.wifi-client-locations.created",
            "com.hpe.greenlake.network-services.v1alpha1.asset-tags.last-known-location.created",
        },
        "source": (
            "developer_docs/"
            "developer_arubanetworks_com_new-central_docs_streaming-api-event-location.md"
        ),
    },
    "location-analytics": {
        "path": "/network-services/v1alpha1/rssi-events",
        "event_types": {
            "com.hpe.greenlake.network-services.v1alpha1.rssi.raw-rssi",
            "com.hpe.greenlake.network-services.v1alpha1.rssi.proximity-rssi",
        },
        "source": (
            "developer_docs/"
            "developer_arubanetworks_com_new-central_docs_streaming-api-event-location-analytics.md"
        ),
    },
}

_WEBSOCKET_CONNECT = websocket_connect


def _validation_error(message: str) -> dict[str, Any]:
    return {"status": "validation_error", "completed": False, "error": message}


def _redact_stream_value(value: Any, token: str | None) -> Any:
    redacted = redact_sensitive(value)
    if isinstance(redacted, dict):
        return {
            str(key): _redact_stream_value(item, token)
            for key, item in redacted.items()
        }
    if isinstance(redacted, list):
        return [_redact_stream_value(item, token) for item in redacted]
    if isinstance(redacted, str) and token:
        return redacted.replace(token, "[REDACTED]")
    return redacted


def _topic_event_allowed(topic: str, event_type: str) -> bool:
    config = _TOPICS[topic]
    exact = config.get("event_types")
    if isinstance(exact, set):
        return event_type in exact
    prefix = config.get("event_prefix")
    return isinstance(prefix, str) and event_type.startswith(prefix)


def _validate_event_types(
    topic: str,
    event_types: list[str] | None,
) -> tuple[list[str] | None, str | None]:
    if event_types is None:
        return None, None
    if not event_types or len(event_types) > _MAX_EVENT_TYPES:
        return None, f"event_types must contain between 1 and {_MAX_EVENT_TYPES} values."
    normalized: list[str] = []
    for event_type in event_types:
        value = str(event_type).strip()
        if (
            not value
            or len(value) > _MAX_EVENT_TYPE_LENGTH
            or value in normalized
            or not _topic_event_allowed(topic, value)
        ):
            return None, (
                f"Unsupported event type for topic {topic!r}: {event_type!r}. "
                "Use the event types documented for that Central stream."
            )
        normalized.append(value)
    return normalized, None


def _stream_url(
    base_url: str,
    topic: str,
    event_types: list[str] | None,
) -> tuple[str | None, str | None]:
    try:
        trusted = validate_product_base_url(base_url, product="Central")
    except ValueError as exc:
        return None, str(exc)
    parsed = urlsplit(trusted)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        return None, (
            "Central Streaming requires an HTTPS Central API origin without "
            "a custom path, query, or fragment."
        )
    query = ""
    if event_types:
        query = urlencode({"event-types": ",".join(event_types)})
    return urlunsplit(("wss", parsed.netloc, _TOPICS[topic]["path"], query, "")), None


def _event_bytes(message: str | bytes) -> int:
    if isinstance(message, bytes):
        return len(message)
    return len(message.encode("utf-8"))


def _first_value(value: Any, keys: tuple[str, ...]) -> Any:
    if not isinstance(value, dict):
        return None
    for key in keys:
        candidate = value.get(key)
        if candidate not in (None, ""):
            return candidate
    return None


def _find_nested(value: Any, keys: tuple[str, ...], depth: int = 0) -> Any:
    if depth > 3 or not isinstance(value, dict):
        return None
    direct = _first_value(value, keys)
    if direct is not None:
        return direct
    for child_key in ("data", "payload", "event", "proto_data", "attributes"):
        child = value.get(child_key)
        found = _find_nested(child, keys, depth + 1)
        if found is not None:
            return found
    return None


def _decode_json_message(message: str | bytes) -> dict[str, Any] | None:
    try:
        text = message.decode("utf-8") if isinstance(message, bytes) else message
        value = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _normalize_json_event(
    value: dict[str, Any],
    *,
    topic: str,
    event_types: list[str] | None,
    include_raw: bool,
    token: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    event_type = _find_nested(
        value,
        ("type", "event_type", "eventType", "cloud_event_type"),
    )
    event_type = str(event_type) if event_type not in (None, "") else None
    if event_type and not _topic_event_allowed(topic, event_type):
        return None, "event_type_mismatch"
    if event_types and event_type and event_type not in event_types:
        return None, "event_type_filtered"

    payload = value.get("data")
    if payload is None:
        payload = value.get("payload")
    if payload is None:
        payload = value

    normalized: dict[str, Any] = {
        "topic": topic,
        "event_type": event_type or (event_types[0] if len(event_types or []) == 1 else None),
        "source": _find_nested(value, ("source", "subject", "origin")),
        "timestamp": _find_nested(
            value,
            ("time", "timestamp", "occurred_at", "occurredAt", "created_at"),
        ),
        "device_id": _find_nested(
            payload,
            ("device_id", "deviceId", "serial", "serial_number", "serialNumber"),
        ),
        "client_id": _find_nested(
            payload,
            ("client_id", "clientId", "client_mac", "clientMac", "sta_eth_mac"),
        ),
        "decoded": True,
        "payload": _redact_stream_value(payload, token),
    }
    normalized = {
        key: value for key, value in normalized.items() if value not in (None, "")
    }
    if include_raw:
        normalized["raw_payload"] = _redact_stream_value(value, token)
    return normalized, None


def _normalize_opaque_event(
    message: bytes,
    *,
    topic: str,
    event_types: list[str] | None,
    include_raw: bool,
) -> dict[str, Any]:
    normalized: dict[str, Any] = {
        "topic": topic,
        "event_type": event_types[0] if len(event_types or []) == 1 else None,
        "decoded": False,
        "payload_encoding": "protobuf",
        "decode_note": (
            "Central CloudEvents use protobuf. This bounded collector preserves "
            "opaque binary events; topic-specific protobuf decoding is not bundled."
        ),
    }
    if include_raw:
        normalized["raw_payload_base64"] = base64.b64encode(message).decode("ascii")
    return {key: value for key, value in normalized.items() if value is not None}


def _output_size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))


def _base_result(
    *,
    topic: str,
    endpoint: str,
    event_types: list[str] | None,
    source: str,
    started: float,
) -> dict[str, Any]:
    return {
        "status": "connection_error",
        "completed": False,
        "topic": topic,
        "endpoint": endpoint,
        "event_types": event_types or [],
        "source": source,
        "documentation_sources": [source, _CONNECTION_SOURCE],
        "events": [],
        "received_events": 0,
        "matched_events": 0,
        "malformed_events": 0,
        "filtered_events": 0,
        "received_bytes": 0,
        "attempts": 0,
        "reconnects": 0,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


@mcp.tool(annotations=READ_ONLY)
async def central_collect_streaming_events(
    topic: str,
    advanced_subscription: bool = False,
    event_types: list[str] | None = None,
    timeout_seconds: float = 30.0,
    max_events: int = 50,
    max_bytes: int = 131_072,
    max_reconnects: int = 2,
    include_raw: bool = False,
) -> dict[str, Any]:
    """Collect bounded read-only events from an Aruba Central WSS stream.

    Supported topics are ``ap-monitoring``, ``audit-trail``, ``geofence``,
    ``location``, and ``location-analytics``. Central requires an Advance Tier
    subscription; callers must pass ``advanced_subscription=True`` as an
    explicit preflight assertion. Optional ``event_types`` are validated
    against the documented topic and sent as the ``event-types`` query
    parameter. Central messages are protobuf CloudEvents; JSON fixtures are
    normalized, while binary messages are returned as bounded opaque events.

    The collector refreshes the shared Central OAuth token before connecting,
    reconnects only within the total timeout, and never returns a successful
    result for a timeout, malformed stream, connection failure, or bound
    exhaustion. Raw payloads are omitted by default and are always bounded by
    ``max_bytes`` when requested.
    """
    topic = str(topic or "").strip().lower()
    if topic not in _TOPICS:
        return _validation_error(
            f"Unsupported topic {topic!r}; expected one of {', '.join(_TOPICS)}."
        )
    if not advanced_subscription:
        return {
            "status": "subscription_error",
            "completed": False,
            "topic": topic,
            "error": (
                "Central Streaming APIs require an Advance Tier Central "
                "Subscription. Pass advanced_subscription=True only after "
                "verifying the tenant subscription."
            ),
        }
    if not 0 < timeout_seconds <= _MAX_TIMEOUT:
        return _validation_error(
            f"timeout_seconds must be greater than 0 and at most {_MAX_TIMEOUT:g}."
        )
    if not 1 <= max_events <= _MAX_EVENTS:
        return _validation_error(f"max_events must be between 1 and {_MAX_EVENTS}.")
    if not 1_024 <= max_bytes <= _MAX_BYTES:
        return _validation_error(f"max_bytes must be between 1024 and {_MAX_BYTES}.")
    if not 0 <= max_reconnects <= _MAX_RECONNECTS:
        return _validation_error(
            f"max_reconnects must be between 0 and {_MAX_RECONNECTS}."
        )
    normalized_types, event_type_error = _validate_event_types(topic, event_types)
    if event_type_error:
        return _validation_error(event_type_error)

    started = time.monotonic()
    try:
        client = get_client()
        token = await asyncio.to_thread(client.token_manager.get_access_token)
        websocket_url, url_error = _stream_url(client.base_url, topic, normalized_types)
    except Exception as exc:
        return {
            "status": "configuration_error",
            "completed": False,
            "topic": topic,
            "error": f"Central Streaming preflight failed: {redact_sensitive(str(exc))}",
        }
    if url_error or not websocket_url:
        return {
            "status": "configuration_error",
            "completed": False,
            "topic": topic,
            "error": url_error or "Central Streaming endpoint could not be built.",
        }

    result = _base_result(
        topic=topic,
        endpoint=websocket_url,
        event_types=normalized_types,
        source=_TOPICS[topic]["source"],
        started=started,
    )
    result["returned_bytes"] = 0
    last_error = "Central WebSocket closed before a terminal event was received."

    while result["attempts"] <= max_reconnects and result["status"] == "connection_error":
        remaining = timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            result["status"] = "timeout"
            last_error = "Timed out before a terminal Central Streaming event was received."
            break
        result["attempts"] += 1
        try:
            async with _WEBSOCKET_CONNECT(
                websocket_url,
                additional_headers={"Authorization": f"Bearer {token}"},
                open_timeout=min(remaining, 10.0),
                close_timeout=5.0,
                max_size=max_bytes,
                max_queue=4,
            ) as websocket:
                while result["received_events"] < max_events:
                    remaining = timeout_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        result["status"] = "timeout"
                        last_error = (
                            "Timed out before a terminal Central Streaming event "
                            "was received."
                        )
                        break
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=remaining)
                    except (TimeoutError, asyncio.TimeoutError):
                        result["status"] = "timeout"
                        last_error = (
                            "Timed out before a terminal Central Streaming event "
                            "was received."
                        )
                        break
                    result["received_events"] += 1
                    message_size = _event_bytes(message)
                    result["received_bytes"] += message_size
                    if result["received_bytes"] > max_bytes:
                        result["status"] = "byte_limit"
                        last_error = "Central Streaming byte limit reached before completion."
                        break

                    decoded = _decode_json_message(message)
                    if decoded is not None:
                        normalized, reason = _normalize_json_event(
                            decoded,
                            topic=topic,
                            event_types=normalized_types,
                            include_raw=include_raw,
                            token=token,
                        )
                        if reason in {"event_type_filtered", "event_type_mismatch"}:
                            result["filtered_events"] += 1
                            continue
                        if normalized is None:
                            result["malformed_events"] += 1
                            continue
                    elif isinstance(message, bytes):
                        normalized = _normalize_opaque_event(
                            message,
                            topic=topic,
                            event_types=normalized_types,
                            include_raw=include_raw,
                        )
                    else:
                        result["malformed_events"] += 1
                        continue

                    normalized_size = _output_size(normalized)
                    if result["returned_bytes"] + normalized_size > max_bytes:
                        result["status"] = "byte_limit"
                        last_error = (
                            "Central Streaming normalized event exceeded max_bytes."
                        )
                        break
                    result["events"].append(normalized)
                    result["returned_bytes"] += normalized_size
                    result["matched_events"] += 1
                if result["status"] != "connection_error":
                    break
                if result["received_events"] >= max_events:
                    result["status"] = "event_limit"
                    last_error = (
                        "Central Streaming event limit reached before completion."
                    )
                    break
        except asyncio.CancelledError:
            raise
        except PayloadTooBig:
            result["status"] = "byte_limit"
            last_error = "A Central Streaming message exceeded max_bytes."
            break
        except Exception as exc:
            last_error = f"Central Streaming connection failed: {redact_sensitive(str(exc))}"

        if result["status"] != "connection_error":
            break
        if result["attempts"] > max_reconnects:
            result["status"] = "reconnect_exhausted" if max_reconnects else "connection_error"
            break
        delay = min(
            _INITIAL_RECONNECT_DELAY * (2 ** (result["attempts"] - 1)),
            _MAX_RECONNECT_DELAY,
        )
        remaining = timeout_seconds - (time.monotonic() - started)
        if remaining <= delay:
            result["status"] = "timeout"
            last_error = "Timed out while waiting to reconnect to Central Streaming."
            break
        result["reconnects"] += 1
        await asyncio.sleep(delay)

    result["completed"] = result["status"] == "completed"
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    if result["status"] == "connection_error" or result["status"] == "reconnect_exhausted":
        result["error"] = last_error
    elif result["status"] != "completed":
        result["error"] = last_error
    return result


if __name__ == "__main__":
    from hpe_networking_mcp.mcp_servers._cache_hygiene import stable_list_tools
    from hpe_networking_mcp.mcp_servers._middleware import (
        NullStripMiddleware,
        RateLimitMiddleware,
        ResponseEnvelopeMiddleware,
        SecretTokenizeMiddleware,
        install_middleware,
    )

    stable_list_tools(mcp)
    install_middleware(
        mcp,
        [
            NullStripMiddleware(),
            RateLimitMiddleware(rate=8.0),
            ResponseEnvelopeMiddleware(),
            SecretTokenizeMiddleware(),
        ],
    )
    from hpe_networking_mcp.mcp_servers.shared import run_server

    run_server(mcp)
