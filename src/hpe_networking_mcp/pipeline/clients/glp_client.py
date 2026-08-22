"""GreenLake Platform (GLP) client.

Handles device-management and subscription-management operations including
async task polling (202 Accepted → GET /tasks/{task_id}).
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any
from urllib.parse import quote

from hpe_networking_mcp.pipeline.clients.central_client import CentralClient
from hpe_networking_mcp.pipeline.clients.token_manager import TokenManager

logger = logging.getLogger(__name__)

_GLP_BASE_URL = "https://global.api.greenlake.hpe.com"
_TASK_POLL_INTERVAL = 10  # seconds
_TASK_POLL_TIMEOUT = 300  # 5 minutes

# Feature flag for the v2beta1 device PATCH write path. Default OFF.
# When unset or "0"/"false", the mutations raise NotImplementedError so
# accidental callers can't fire writes. Set to "1" or "true" once the
# caller has sandbox-validated the payload shape and transactional
# rollback story for their use case.
_V2BETA1_WRITES_FLAG = "HPE_MCP_GLP_V2BETA1_WRITES"


def _writes_enabled() -> bool:
    """Whether guarded GLP writes may execute.

    Resolves through the shared *GLP* platform write gate so this flag is the
    single source of truth for GreenLake writes and is fully independent of
    Central's gate (``HPE_MCP_CENTRAL_WRITES``) — flipping Central's flag
    must never enable or disable GLP writes. Falls back to reading the env var
    directly if the MCP layer is unavailable (plain pipeline usage), keeping
    the historical accepted values.
    """
    try:
        from hpe_networking_mcp.mcp_servers.shared import platform_writes_allowed

        return platform_writes_allowed("glp")
    except Exception:  # pragma: no cover - MCP layer not importable
        return os.environ.get(_V2BETA1_WRITES_FLAG, "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )


def glp_write_gate_message(action: str, detail: str) -> str:
    """Build the standard 'GLP writes are gated' message for ``action``.

    Explicitly names the GLP flag (never Central's) so an operator reading a
    blocked-write error is pointed at the right knob.
    """
    return (
        f"{action} is gated behind {_V2BETA1_WRITES_FLAG}=1 and was not performed. "
        f"This gate is specific to GreenLake writes and is independent of "
        f"HPE_MCP_CENTRAL_WRITES. {detail}"
    )


def _compact_exception_message(exc: Exception, max_chars: int = 240) -> str:
    """Return a compact, structured exception message for tool-friendly output."""
    response = getattr(exc, "response", None)
    if response is None:
        return str(exc)
    try:
        body = response.json()
    except Exception:
        body = response.text
    body_text = str(body or "").strip()
    if len(body_text) > max_chars:
        body_text = f"{body_text[:max_chars]}... [truncated {len(body_text) - max_chars} chars]"
    reason = getattr(response, "reason_phrase", None) or getattr(response, "reason", "")
    return f"HTTP {response.status_code} {reason}: {body_text}"


# Async-operation lifecycle states. The authoritative set for
# ``GET /devices/v1/async-operations/{id}`` (committed GLP manifest,
# device-management.json) is INITIALIZED / RUNNING -> SUCCEEDED | FAILED |
# TIMEOUT. The extra synonyms cover the sibling GLP services that reuse the
# async-operation shape with slightly different vocabulary.
_TERMINAL_SUCCESS_STATES = frozenset({"succeeded", "success", "completed", "complete", "ok"})
_TERMINAL_FAILURE_STATES = frozenset(
    {
        "failed",
        "failure",
        "error",
        "errored",
        "timeout",
        "timedout",
        "timed_out",
        "cancelled",
        "canceled",
        "aborted",
    }
)
_IN_PROGRESS_STATES = frozenset(
    {
        "initialized",
        "initializing",
        "running",
        "in_progress",
        "inprogress",
        "pending",
        "queued",
        "created",
        "accepted",
        "started",
    }
)


def _extract_task_status(result: Any) -> str | None:
    """Normalize an async-operation status/state token to lowercase.

    Reads ``status`` first, then ``state`` (used by some GLP services).
    Returns ``None`` when the payload is not a dict, carries neither field,
    or carries a non-scalar / blank value — i.e. a malformed response that
    must not be mistaken for "still running".
    """
    if not isinstance(result, dict):
        return None
    for key in ("status", "state"):
        value = result.get(key)
        if isinstance(value, str):
            token = value.strip().lower()
            if token:
                return token
        elif isinstance(value, (int, float, bool)):
            return str(value).strip().lower()
    return None


# ---------------------------------------------------------------------------
# Audit Log (v2beta1) — the ONLY audit-log surface in the committed manifest
# ---------------------------------------------------------------------------
#
# src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/glp.json declares exactly three audit-log
# operations, all under v2beta1:
#     GET /audit-log/v2beta1/logs           (getAuditLogs)
#     GET /audit-log/v2beta1/logs/{id}      (getAuditLog)
#     GET /audit-log/v2beta1/logs/{id}/details (getAuditLogDetails)
# There is no /audit-log/v1/... operation and no `category` query parameter:
# the list operation's documented query params are filter, select, limit,
# offset and sort, and `category` is a *filter field* (`category eq '<x>'`,
# also supporting `in`). The helpers below translate the historical
# `category=` keyword into a conformant filter expression so existing callers
# keep working against the documented endpoint.
AUDIT_LOG_BASE_PATH = "/audit-log/v2beta1/logs"


def _odata_quote(value: str) -> str:
    """Escape a string for use inside an OData single-quoted literal."""
    return str(value).replace("'", "''")


_PAGINATION_INT_FIELDS = ("count", "offset", "total")


def _pagination_fields(result: Any, items: list) -> dict[str, Any]:
    """Extract GLP offset-pagination metadata (count/offset/total/next).

    GLP list responses (e.g. DevicesGetResponse) carry ``count``/``offset``/
    ``total``; some services additionally return an opaque ``next`` cursor.
    Only fields actually present are surfaced, except ``count`` which defaults
    to the returned page length so callers always have a page size.
    """
    meta: dict[str, Any] = {}
    if isinstance(result, dict):
        for key in _PAGINATION_INT_FIELDS:
            value = result.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                meta[key] = value
        nxt = result.get("next")
        if nxt is None:
            pagination = result.get("pagination")
            if isinstance(pagination, dict):
                nxt = pagination.get("next")
        if nxt is not None:
            meta["next"] = nxt
    meta.setdefault("count", len(items))
    return meta


def _audit_log_filter(category: str | None, filter: str | None) -> str | None:
    """Combine a legacy ``category`` argument and a caller filter expression.

    ``category`` becomes ``category eq '<value>'`` per the getAuditLogs filter
    grammar; a caller-supplied ``filter`` is ANDed with it. Returns ``None``
    when neither is supplied, so no empty ``filter=`` is sent.
    """
    clauses: list[str] = []
    if category:
        clauses.append(f"category eq '{_odata_quote(category)}'")
    if filter:
        clauses.append(f"({filter})" if clauses else filter)
    if not clauses:
        return None
    return " and ".join(clauses)


def _task_failure_detail(result: Any) -> str:
    """Compact, log-safe failure detail from a terminal async-operation body."""
    if not isinstance(result, dict):
        return str(result)[:300]
    for key in ("error", "errorDetails", "message", "errorMessage", "results"):
        value = result.get(key)
        if value:
            return f"{key}={str(value)[:300]}"
    return str(result)[:300]


class GLPClient:
    """Client for HPE GreenLake Platform device and subscription management APIs."""

    def __init__(
        self,
        token_manager: TokenManager,
        workspace_id: str,
        base_url: str = _GLP_BASE_URL,
    ):
        # write_platform="glp" so the transport-level write gate consults the
        # GLP flag, not Central's — a read-only Central deployment must not
        # silently disable (or a permissive one enable) GreenLake writes.
        self._client = CentralClient(
            base_url=base_url,
            token_manager=token_manager,
            write_platform="glp",
        )
        self.workspace_id = workspace_id
        # Per-instance serial -> deviceId cache. NOT at class scope (that
        # would share across all GLPClient instances in the process; in
        # prod there's only one singleton so harmless, but the per-test
        # isolation and the docstring's "process-local" claim both want
        # the per-instance form).
        self._device_id_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Device management
    # ------------------------------------------------------------------

    def get_device(self, serial_number: str) -> dict[str, Any] | None:
        """Look up a device in GLP by serial number.

        Returns None only when GLP confirms no match (the API returns 200 +
        empty items, never 404, for a filter miss). Transient failures
        (auth, 5xx, network, malformed body) re-raise so callers report the
        real error instead of misdiagnosing "device not found".
        """
        try:
            result = self._client.get(
                "/devices/v1/devices",
                params={"filter": f"serialNumber eq '{_odata_quote(serial_number)}'"},
            )
        except Exception as exc:
            msg = _compact_exception_message(exc)
            logger.warning("GLP get_device failed for %s: %s", serial_number, msg)
            raise RuntimeError(
                f"GLP device lookup failed for {serial_number}: {msg}"
            ) from exc
        items = result.get("items", result.get("devices", []))
        return items[0] if items else None

    def add_device(self, serial_number: str, mac_address: str | None = None) -> str:
        """Add a single device to the GLP workspace. Returns async-operation ID."""
        return self.add_devices([{"serialNumber": serial_number, "macAddress": mac_address}])

    def add_devices(self, devices: list[dict[str, Any]]) -> str:
        """Add one or more network devices to the GLP workspace in a single call.

        Args:
            devices: List of dicts with 'serialNumber' (required) and 'macAddress' (required).

        Returns:
            Async-operation ID for polling with poll_task().
        """
        for d in devices:
            if not d.get("macAddress"):
                raise ValueError("macAddress is required for network devices")
        network = [
            {k: v for k, v in d.items() if v is not None}
            for d in devices
        ]
        body: dict[str, Any] = {"network": network, "compute": [], "storage": []}
        location = self._client.post_async("/devices/v1/devices", data=body)
        # Location: /devices/v1/async-operations/{id}
        task_id = location.rstrip("/").split("/")[-1]
        serials = [d.get("serialNumber") for d in devices]
        logger.info(
            "GLP add_devices serial_count=%d sample=%s -> async-op id=%s",
            len(serials),
            serials[:5],
            task_id,
        )
        return task_id

    def poll_task(
        self,
        task_id: str,
        timeout: int = _TASK_POLL_TIMEOUT,
        interval: int = _TASK_POLL_INTERVAL,
    ) -> dict[str, Any]:
        """Poll a GLP async-operation until it reaches a terminal state.

        Per ``GET /devices/v1/async-operations/{id}`` in the committed GLP
        manifest, the terminal states are ``SUCCEEDED``, ``FAILED`` and
        ``TIMEOUT``; ``INITIALIZED`` and ``RUNNING`` are in-flight. Some GLP
        services report the same value under ``state`` rather than ``status``,
        so both keys are read.

        Returns:
            The final task response dict on success.

        Raises:
            RuntimeError: the operation reached a terminal failure state, the
                response carried no usable status/state field (malformed), or
                the poll deadline expired. Timeout errors name the last
                observed status so an unrecognized in-flight state is
                diagnosable rather than an opaque hang.
        """
        deadline = time.time() + timeout
        last_status: str | None = None
        warned_unknown: set[str] = set()
        polled = False

        while True:
            result = self._client.get(f"/devices/v1/async-operations/{task_id}")
            polled = True
            status = _extract_task_status(result)
            logger.debug("GLP async-op %s status=%r", task_id, status)

            if status is None:
                raise RuntimeError(
                    f"GLP async-op {task_id} returned a malformed response with no "
                    "usable 'status'/'state' field — cannot determine completion. "
                    f"Payload keys: "
                    f"{sorted(result)[:20] if isinstance(result, dict) else type(result).__name__}"
                )

            last_status = status
            if status in _TERMINAL_SUCCESS_STATES:
                return result
            if status in _TERMINAL_FAILURE_STATES:
                raise RuntimeError(
                    f"GLP async-op {task_id} failed in terminal state {status!r}: "
                    f"{_task_failure_detail(result)}"
                )
            if status not in _IN_PROGRESS_STATES and status not in warned_unknown:
                warned_unknown.add(status)
                logger.warning(
                    "GLP async-op %s reported unrecognized status %r — treating as "
                    "in-progress and continuing to poll until the deadline.",
                    task_id,
                    status,
                )

            if time.time() + interval >= deadline:
                break
            time.sleep(interval)

        raise RuntimeError(
            f"GLP async-op {task_id} timed out after {timeout}s "
            f"(polled={polled}, last status={last_status!r})"
        )

    # ------------------------------------------------------------------
    # Device ID resolution (serial → GLP device UUID)
    # ------------------------------------------------------------------
    #
    # Central-style callers pass serial numbers. GLP v2beta1 identifies
    # devices by their GLP UUID (the `id` field on each device record),
    # not by serial. We resolve on demand with a small in-memory cache.
    # The cache is process-local and **not** persisted — a restart
    # re-fetches, which is fine and keeps stale mappings from sticking.

    # Serial numbers are restricted to ASCII alphanumerics, dashes, and
    # underscores in practice (HPE spec; matches every serial format we've
    # seen on AOS-S / CX / AP / gateway hardware). We reject anything else
    # defensively before interpolating into an OData filter, so a serial
    # containing ``'`` can't terminate the quoted string and inject query
    # fragments. This is belt-and-suspenders — the filter value itself is
    # unlikely to be attacker-controlled in normal MCP use.
    _SERIAL_SAFE_CHARS = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    )

    @classmethod
    def _is_safe_serial(cls, serial_number: str) -> bool:
        return bool(serial_number) and all(c in cls._SERIAL_SAFE_CHARS for c in serial_number)

    def resolve_device_id(self, serial_number: str) -> str | None:
        """Return the GLP device UUID for ``serial_number``, or None if not found.

        Looks up via ``GET /devices/v1/devices?filter=serialNumber eq '<s>'``.
        Results are memoised for the lifetime of this client instance.
        Rejects malformed serials before interpolating into the filter so a
        rogue caller can't break out of the quoted string.
        """
        if not self._is_safe_serial(serial_number):
            logger.warning(
                "resolve_device_id: rejecting serial %r (must be ASCII alnum/dash/underscore)",
                serial_number,
            )
            return None
        cached = self._device_id_cache.get(serial_number)
        if cached is not None:
            return cached
        try:
            result = self._client.get(
                "/devices/v1/devices",
                params={"filter": f"serialNumber eq '{serial_number}'", "limit": 1},
            )
            items = result.get("items", [])
            if not items:
                return None
            device_id = items[0].get("id")
            if not device_id:
                return None
            self._device_id_cache[serial_number] = device_id
            return device_id
        except Exception as exc:
            logger.warning("resolve_device_id(%s) failed: %s", serial_number, exc)
            return None

    # ------------------------------------------------------------------
    # v2beta1 device PATCH — archive, unarchive, subscription assign/unassign
    # ------------------------------------------------------------------
    #
    # Per the official Devices v2beta1 spec
    # (https://developer.greenlake.hpe.com/docs/greenlake/services/device-management/public/openapi/nbapi-inventory-latest/devices-v2beta1/patchdevicesv2beta1),
    # these four operations all share one endpoint:
    #
    #     PATCH /devices/v2beta1/devices?id={uuid}[&id=...]
    #     Content-Type: application/merge-patch+json
    #
    # Body shapes:
    #   archive:            {"archived": true}   — MUST be the only field
    #   unarchive:          {"archived": false}
    #   assign subscription:{"subscription": [{"id": "<subscriptionId>"}]}
    #   unassign all:       {"subscription": []}
    #
    # Response is 202 Accepted with Location header pointing at
    # /devices/v1/async-operations/{id} — which our existing
    # ``poll_task`` already knows how to poll.
    #
    # All four go through ``_patch_devices_v2beta1`` so the feature flag
    # gate, payload validation, deviceId resolution, and async polling
    # live in exactly one place.

    def _patch_devices_v2beta1(
        self,
        serial_number: str,
        body: dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Internal helper: PATCH /devices/v2beta1/devices?id=<resolved-id>.

        ``dry_run=True`` adds the manifest-declared ``dry-run`` query
        parameter (patchDevicesV2beta1 documents it as ``dry-run``, boolean,
        default false: "the request is validated but not executed"). The
        response is returned verbatim and is **not** polled — a validated
        request creates no async-operation to poll.
        """
        if not _writes_enabled():
            raise NotImplementedError(
                glp_write_gate_message(
                    "GLP v2beta1 device PATCH",
                    "Set that env var after sandbox-validating payload + rollback "
                    f"for your use case. Payload that would have fired: {body} "
                    f"(dry_run={dry_run})",
                )
            )

        device_id = self.resolve_device_id(serial_number)
        if device_id is None:
            raise RuntimeError(
                f"Could not resolve serial {serial_number!r} to a GLP device ID. "
                "Check the device is registered in this workspace."
            )

        # PATCH with merge-patch+json. Central _request accepts custom
        # headers via kwargs; set the content type explicitly.
        params: dict[str, Any] = {"id": device_id}
        if dry_run:
            params["dry-run"] = "true"
        resp = self._client._request(
            "PATCH",
            "/devices/v2beta1/devices",
            params=params,
            json=body,
            headers={"Content-Type": "application/merge-patch+json"},
        )

        if resp.status_code not in (200, 202):
            raise RuntimeError(
                f"GLP PATCH /devices/v2beta1/devices id={device_id} returned "
                f"HTTP {resp.status_code}: {resp.text[:300]}"
            )

        if dry_run:
            try:
                payload = resp.json()
            except Exception:
                payload = {"rawResponse": resp.text[:500]}
            return {
                "status": "dry_run",
                "dry_run": True,
                "http_status": resp.status_code,
                "device_id": device_id,
                "request_body": body,
                "response": payload,
            }

        # 202 → async; poll the Location header's async-operation.
        if resp.status_code == 202:
            location = resp.headers.get("Location", "")
            task_id = location.rstrip("/").split("/")[-1]
            if not task_id:
                raise RuntimeError(
                    "GLP PATCH returned 202 but no Location header — cannot poll."
                )
            return self.poll_task(task_id)

        # 200 = synchronous completion (rare; spec allows it)
        try:
            return resp.json()
        except Exception:
            return {"status": "completed", "rawResponse": resp.text[:500]}

    def _resolve_subscription_id(self, subscription: str) -> str:
        """Return a subscription UUID for ``subscription``.

        Accepts either a GLP subscription UUID (returned as-is) or a
        subscription key string, which is resolved to its UUID via
        ``GET /subscriptions/v1/subscriptions?filter=key eq '<key>'``.
        Raises ValueError if a key can't be resolved.
        """
        try:
            uuid.UUID(subscription)
            return subscription
        except (ValueError, AttributeError, TypeError):
            pass
        # Same defense resolve_device_id applies to serials: reject anything
        # that could break out of the quoted OData filter string (keys are
        # ASCII alphanumeric / dash / underscore in practice), and escape the
        # value before interpolation as belt-and-suspenders.
        if not self._is_safe_serial(subscription):
            raise ValueError(
                f"Invalid subscription key {subscription!r}: expected a GLP "
                "subscription UUID or an alphanumeric key (letters, digits, "
                "dash, underscore)."
            )
        result = self._client.get(
            "/subscriptions/v1/subscriptions",
            params={"filter": f"key eq '{_odata_quote(subscription)}'"},
        )
        items = result.get("items", result.get("subscriptions", []))
        if not items:
            raise ValueError(
                f"Could not resolve subscription key {subscription!r} to a GLP "
                "subscription ID. Check the key exists in this workspace."
            )
        return items[0]["id"]

    def assign_subscription(
        self,
        serial_number: str,
        subscription_id: str,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Assign a subscription to a device via v2beta1 device PATCH.

        Args:
            serial_number: Device serial (resolved to GLP UUID internally).
            subscription_id: The GLP subscription UUID, or a subscription key
                string (resolved to its UUID internally). Use
                ``list_subscriptions()`` to find either.
            dry_run: Send the manifest-declared ``dry-run`` query parameter so
                GLP validates the request without applying it. Returns a
                ``{"status": "dry_run", ...}`` preview instead of a polled
                async-operation result.

        Guarded by ``HPE_MCP_GLP_V2BETA1_WRITES=1`` (independent of
        ``HPE_MCP_CENTRAL_WRITES``).
        """
        resolved_id = self._resolve_subscription_id(subscription_id)
        return self._patch_devices_v2beta1(
            serial_number,
            {"subscription": [{"id": resolved_id}]},
            dry_run=dry_run,
        )

    def unassign_subscription(
        self, serial_number: str, *, dry_run: bool = False
    ) -> dict[str, Any]:
        """Remove **all** subscriptions from a device via v2beta1 device PATCH.

        Sends ``{"subscription": []}`` per the GLP Devices v2beta1 spec.
        ``dry_run=True`` validates without applying (manifest-declared
        ``dry-run`` query parameter). Guarded by
        ``HPE_MCP_GLP_V2BETA1_WRITES=1``.
        """
        return self._patch_devices_v2beta1(
            serial_number,
            {"subscription": []},
            dry_run=dry_run,
        )

    def archive_device(
        self, serial_number: str, *, dry_run: bool = False
    ) -> dict[str, Any]:
        """Archive a device via v2beta1 device PATCH.

        Sends ``{"archived": true}`` — per spec this MUST be the only field
        in the body. Incompatible with combining in a single call with any
        other device mutation. ``dry_run=True`` validates without applying
        (manifest-declared ``dry-run`` query parameter).

        Guarded by ``HPE_MCP_GLP_V2BETA1_WRITES=1``.
        """
        return self._patch_devices_v2beta1(
            serial_number, {"archived": True}, dry_run=dry_run
        )

    def unarchive_device(
        self, serial_number: str, *, dry_run: bool = False
    ) -> dict[str, Any]:
        """Unarchive a device via v2beta1 device PATCH.

        Sends ``{"archived": false}``. ``dry_run=True`` validates without
        applying (manifest-declared ``dry-run`` query parameter). Guarded by
        ``HPE_MCP_GLP_V2BETA1_WRITES=1``.
        """
        return self._patch_devices_v2beta1(
            serial_number, {"archived": False}, dry_run=dry_run
        )

    # ------------------------------------------------------------------
    # GLP read — devices, subscriptions, users, audit logs
    # ------------------------------------------------------------------

    def list_devices_page(
        self,
        limit: int = 100,
        offset: int = 0,
        filter: str | None = None,
    ) -> dict[str, Any]:
        """List devices in the GLP workspace with pagination metadata.

        Returns ``{"items": [...], "count", "offset", "total", "next"}`` —
        surfacing whatever offset-pagination fields GLP returned so callers
        can tell whether more pages exist.
        """
        try:
            params: dict[str, Any] = {"limit": limit, "offset": offset}
            if filter:
                params["filter"] = filter
            result = self._client.get("/devices/v1/devices", params=params)
            items = result.get("items", result.get("devices", []))
            return {"items": items, **_pagination_fields(result, items)}
        except Exception as exc:
            msg = _compact_exception_message(exc)
            logger.warning("GLP list_devices failed: %s", msg)
            raise RuntimeError(f"GLP list_devices failed: {msg}") from exc

    def list_devices(
        self,
        limit: int = 100,
        offset: int = 0,
        filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """List devices in the GLP workspace (items only; back-compat shape)."""
        return self.list_devices_page(limit=limit, offset=offset, filter=filter)["items"]

    def list_subscriptions_page(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List subscriptions in the GLP workspace with pagination metadata."""
        try:
            result = self._client.get(
                "/subscriptions/v1/subscriptions",
                params={"limit": limit, "offset": offset},
            )
            items = result.get("items", result.get("subscriptions", []))
            return {"items": items, **_pagination_fields(result, items)}
        except Exception as exc:
            msg = _compact_exception_message(exc)
            logger.warning("GLP list_subscriptions failed: %s", msg)
            raise RuntimeError(f"GLP list_subscriptions failed: {msg}") from exc

    def list_subscriptions(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List subscriptions (items only; back-compat shape)."""
        return self.list_subscriptions_page(limit=limit, offset=offset)["items"]

    def get_subscription(self, subscription_id: str) -> dict[str, Any] | None:
        """Fetch a single subscription by ID."""
        try:
            return self._client.get(f"/subscriptions/v1/subscriptions/{subscription_id}")
        except Exception as exc:
            logger.warning("GLP get_subscription failed for %s: %s", subscription_id, exc)
            return None

    def list_users_page(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List users in the GLP workspace with pagination metadata."""
        try:
            result = self._client.get(
                "/identity/v1/users",
                params={"limit": limit, "offset": offset},
            )
            items = result.get("items", result.get("users", []))
            return {"items": items, **_pagination_fields(result, items)}
        except Exception as exc:
            logger.warning("GLP list_users failed: %s", exc)
            raise RuntimeError(f"GLP list_users failed: {exc}") from exc

    def list_users(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List users (items only; back-compat shape)."""
        return self.list_users_page(limit=limit, offset=offset)["items"]

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        """Fetch a single user by ID."""
        try:
            return self._client.get(f"/identity/v1/users/{user_id}")
        except Exception as exc:
            logger.warning("GLP get_user failed for %s: %s", user_id, exc)
            return None

    def list_audit_logs_page(
        self,
        limit: int = 100,
        offset: int = 0,
        category: str | None = None,
        filter: str | None = None,
        select: str | None = None,
        sort: str | None = None,
    ) -> dict[str, Any]:
        """List audit log entries for the GLP workspace with pagination metadata.

        Hits ``GET /audit-log/v2beta1/logs`` — the only audit-log list
        operation in the committed manifest. ``category`` is not a query
        parameter there; it is translated into the documented filter
        expression ``category eq '<value>'`` and ANDed with any caller
        ``filter``. ``select`` and ``sort`` are passed through unchanged.
        """
        try:
            params: dict[str, Any] = {"limit": limit, "offset": offset}
            combined = _audit_log_filter(category, filter)
            if combined:
                params["filter"] = combined
            if select:
                params["select"] = select
            if sort:
                params["sort"] = sort
            result = self._client.get(AUDIT_LOG_BASE_PATH, params=params)
            items = result.get("items", result.get("logs", []))
            return {"items": items, **_pagination_fields(result, items)}
        except Exception as exc:
            logger.warning("GLP list_audit_logs failed: %s", exc)
            raise RuntimeError(f"GLP list_audit_logs failed: {exc}") from exc

    def list_audit_logs(
        self,
        limit: int = 100,
        offset: int = 0,
        category: str | None = None,
        filter: str | None = None,
        select: str | None = None,
        sort: str | None = None,
    ) -> list[dict[str, Any]]:
        """List audit log entries (items only; back-compat shape)."""
        return self.list_audit_logs_page(
            limit=limit,
            offset=offset,
            category=category,
            filter=filter,
            select=select,
            sort=sort,
        )["items"]

    def get_audit_log(self, audit_log_id: str) -> dict[str, Any] | None:
        """Fetch a single audit-log entry (``GET /audit-log/v2beta1/logs/{id}``)."""
        return self.get_audit_log_v2beta1(audit_log_id)

    def get_audit_log_detail(self, audit_log_id: str) -> dict[str, Any] | None:
        """Fetch audit-log entry details (``.../logs/{id}/details``).

        The manifest documents the path segment as plural ``details``; the
        older singular ``/detail`` spelling this client used never existed.
        """
        return self.get_audit_log_v2beta1_detail(audit_log_id)

    # ------------------------------------------------------------------
    # GLP read — devices v2beta1, device groups v2beta1, audit-log v2beta1
    # ------------------------------------------------------------------
    #
    # Devices v2beta1 read paths mirror the write path already documented
    # above (patchdevicesv2beta1): GET/PATCH share the same
    # /devices/v2beta1/devices collection. Device Groups (v2beta1) is the
    # sibling collection resource under the same Devices service. The
    # audit-log helpers below are confirmed against the committed manifest
    # (getAuditLogs / getAuditLog / getAuditLogDetails, all v2beta1) — see the
    # AUDIT_LOG_BASE_PATH note near the top of this module for the filter
    # grammar and why `category` is not a query parameter.

    def list_devices_v2beta1(
        self,
        limit: int = 100,
        offset: int = 0,
        filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """List devices via the v2beta1 Devices collection."""
        try:
            params: dict[str, Any] = {"limit": limit, "offset": offset}
            if filter:
                params["filter"] = filter
            result = self._client.get("/devices/v2beta1/devices", params=params)
            return result.get("items", result.get("devices", []))
        except Exception as exc:
            msg = _compact_exception_message(exc)
            logger.warning("GLP list_devices_v2beta1 failed: %s", msg)
            raise RuntimeError(f"GLP list_devices_v2beta1 failed: {msg}") from exc

    def get_device_v2beta1(self, device_id: str) -> dict[str, Any] | None:
        """Fetch a single device via the v2beta1 Devices collection by GLP device ID."""
        try:
            return self._client.get(f"/devices/v2beta1/devices/{device_id}")
        except Exception as exc:
            logger.warning("GLP get_device_v2beta1 failed for %s: %s", device_id, exc)
            return None

    def group_devices_v2beta1(
        self,
        group_by: str,
        limit: int = 100,
        offset: int = 0,
        filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Group devices by a documented v2beta1 device attribute."""
        try:
            params: dict[str, Any] = {
                "group-by": group_by,
                "limit": limit,
                "offset": offset,
            }
            if filter:
                params["filter"] = filter
            result = self._client.get("/devices/v2beta1/devices/group", params=params)
            return result.get("items", result.get("groups", []))
        except Exception as exc:
            msg = _compact_exception_message(exc)
            logger.warning("GLP group_devices_v2beta1 failed: %s", msg)
            raise RuntimeError(f"GLP group_devices_v2beta1 failed: {msg}") from exc

    def list_audit_logs_v2beta1(
        self,
        limit: int = 100,
        offset: int = 0,
        category: str | None = None,
        filter: str | None = None,
        select: str | None = None,
        sort: str | None = None,
    ) -> list[dict[str, Any]]:
        """List audit log entries via the v2beta1 Audit Log service.

        Same operation as :meth:`list_audit_logs` (getAuditLogs); kept as a
        distinct name for callers that pinned the explicit-version spelling.
        ``category`` is translated to the documented ``category eq '<value>'``
        filter expression rather than sent as an undocumented query param.
        """
        try:
            params: dict[str, Any] = {"limit": limit, "offset": offset}
            combined = _audit_log_filter(category, filter)
            if combined:
                params["filter"] = combined
            if select:
                params["select"] = select
            if sort:
                params["sort"] = sort
            result = self._client.get(AUDIT_LOG_BASE_PATH, params=params)
            return result.get("items", result.get("logs", []))
        except Exception as exc:
            msg = _compact_exception_message(exc)
            logger.warning("GLP list_audit_logs_v2beta1 failed: %s", msg)
            raise RuntimeError(f"GLP list_audit_logs_v2beta1 failed: {msg}") from exc

    def get_audit_log_v2beta1(self, audit_log_id: str) -> dict[str, Any] | None:
        """Fetch a single audit-log entry by ID via the v2beta1 Audit Log service."""
        try:
            return self._client.get(f"{AUDIT_LOG_BASE_PATH}/{quote(str(audit_log_id), safe='')}")
        except Exception as exc:
            logger.warning("GLP get_audit_log_v2beta1 failed for %s: %s", audit_log_id, exc)
            return None

    def get_audit_log_v2beta1_detail(self, audit_log_id: str) -> dict[str, Any] | None:
        """Fetch full detail for a v2beta1 audit-log entry (entries with details enabled)."""
        try:
            audit_id = quote(str(audit_log_id), safe="")
            return self._client.get(f"{AUDIT_LOG_BASE_PATH}/{audit_id}/details")
        except Exception as exc:
            logger.warning(
                "GLP get_audit_log_v2beta1_detail failed for %s: %s", audit_log_id, exc
            )
            return None

    # ------------------------------------------------------------------
    # GLP write — workspace contact/location PATCH, subscription add
    # ------------------------------------------------------------------
    #
    # Both gated by the same _V2BETA1_WRITES_FLAG as the device PATCH path
    # above — one flag for all "sandbox-validate before firing" GLP writes.

    def update_workspace_contact(
        self,
        workspace_id: str,
        contact: dict[str, Any],
    ) -> dict[str, Any]:
        """PATCH the contact record for a GLP workspace.

        Endpoint mirrors the confirmed-working GET at the same path
        (get_glp_workspace_contact / /workspaces/v1/workspaces/{id}/contact).
        Guarded by ``HPE_MCP_GLP_V2BETA1_WRITES=1`` (reused — same "sandbox
        validate first" contract as the device PATCH path).
        """
        if not _writes_enabled():
            raise NotImplementedError(
                f"GLP workspace-contact writes are gated behind {_V2BETA1_WRITES_FLAG}=1. "
                f"Payload that would have been sent: {contact}"
            )
        resp = self._client._request(
            "PATCH",
            f"/workspaces/v1/workspaces/{workspace_id}/contact",
            json=contact,
        )
        if not resp.is_success:
            raise RuntimeError(
                f"GLP PATCH workspace contact {workspace_id} returned "
                f"HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            return resp.json()
        except Exception:
            return {"status": "completed", "rawResponse": resp.text[:500]}

    def add_subscriptions(
        self,
        subscription_keys: list[str],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Add one or more subscription keys to the workspace in a single call.

        The ``dry-run`` query parameter matches the committed manifest entry
        for ``POST /subscriptions/v1/subscriptions`` (postSubscriptionsV1) —
        confirmed against ``src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/glp.json``,
        which documents the parameter name as ``dry-run`` (not ``dryRun``).
        The nested ``subscriptions[].key`` body shape is not independently
        re-verified against the Subscriptions v1 spec text for this exact
        operation (the manifest only documents the top-level
        ``subscriptions`` property, not its item schema). Guarded by
        ``HPE_MCP_GLP_V2BETA1_WRITES=1`` even when ``dry_run=True`` is
        requested through the live API (server-side dry-run still reaches
        the tenant); local-only preview should be done by the caller before
        invoking this method.
        """
        if not _writes_enabled():
            raise NotImplementedError(
                f"GLP subscription-add writes are gated behind {_V2BETA1_WRITES_FLAG}=1. "
                f"Payload that would have been sent: keys={subscription_keys} dry_run={dry_run}"
            )
        body = {"subscriptions": [{"key": key} for key in subscription_keys]}
        params = {"dry-run": "true"} if dry_run else None
        resp = self._client._request(
            "POST", "/subscriptions/v1/subscriptions", json=body, params=params
        )
        if not resp.is_success:
            raise RuntimeError(
                f"GLP POST subscriptions returned HTTP {resp.status_code}: "
                f"{resp.text[:300]}"
            )
        try:
            return resp.json()
        except Exception:
            return {"status": "completed", "rawResponse": resp.text[:500]}

    # ------------------------------------------------------------------
    # GLP write — RBAC role assignments / scope groups, user lifecycle,
    # auto-subscription settings (identity v1, authorization v1beta1,
    # subscriptions v1). All confirmed against the committed manifest at
    # src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/glp.json
    # (createRoleAssignmentV1beta1,
    # updateRoleAssignmentV1beta1, deleteRoleAssignmentV1beta1,
    # createScopeGroupV1beta1, updateScopeGroupV1beta1, deleteScopeGroupV1beta1,
    # addScopeGroupScopesV1beta1, deleteScopeGroupScopesV1beta1,
    # invite_user_to_account_identity_v1_users_post,
    # update_user_preferences_identity_v1_users__id__put,
    # disassociate_platform_user_identity_v1_users__id__delete,
    # updateAutoSubscriptionsV1). Gated by the same _V2BETA1_WRITES_FLAG as
    # the writes above — one flag for all "sandbox-validate before firing"
    # GLP writes.
    # ------------------------------------------------------------------

    def create_role_assignment(self, role_assignment: dict[str, Any]) -> dict[str, Any]:
        """POST /authorization/v1beta1/role-assignments.

        Body is passed through as-is; per the spec it must include
        ``principal``, ``role``, and ``scope`` (see the developer guide
        linked from get_glp_role_assignment for how to find those
        identifiers). Guarded by ``HPE_MCP_GLP_V2BETA1_WRITES=1``.
        """
        if not _writes_enabled():
            raise NotImplementedError(
                f"GLP role-assignment create is gated behind {_V2BETA1_WRITES_FLAG}=1. "
                f"Payload that would have been sent: {role_assignment}"
            )
        resp = self._client._request(
            "POST", "/authorization/v1beta1/role-assignments", json=role_assignment
        )
        if not resp.is_success:
            raise RuntimeError(
                f"GLP POST role-assignments returned HTTP {resp.status_code}: "
                f"{resp.text[:300]}"
            )
        try:
            return resp.json()
        except Exception:
            return {"status": "completed", "rawResponse": resp.text[:500]}

    def update_role_assignment(
        self, role_assignment_id: str, role_assignment: dict[str, Any]
    ) -> dict[str, Any]:
        """PUT /authorization/v1beta1/role-assignments/{id}.

        Per the spec, the body must still include the immutable ``id``,
        ``principal``, and ``role`` attributes alongside the updated
        ``scope``. Guarded by ``HPE_MCP_GLP_V2BETA1_WRITES=1``.
        """
        if not _writes_enabled():
            raise NotImplementedError(
                f"GLP role-assignment update is gated behind {_V2BETA1_WRITES_FLAG}=1. "
                f"Payload that would have been sent: {role_assignment}"
            )
        resp = self._client._request(
            "PUT",
            f"/authorization/v1beta1/role-assignments/{role_assignment_id}",
            json=role_assignment,
        )
        if not resp.is_success:
            raise RuntimeError(
                f"GLP PUT role-assignments/{role_assignment_id} returned "
                f"HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            return resp.json()
        except Exception:
            return {"status": "completed", "rawResponse": resp.text[:500]}

    def delete_role_assignment(self, role_assignment_id: str) -> dict[str, Any]:
        """DELETE /authorization/v1beta1/role-assignments/{id}.

        Guarded by ``HPE_MCP_GLP_V2BETA1_WRITES=1``.
        """
        if not _writes_enabled():
            raise NotImplementedError(
                f"GLP role-assignment delete is gated behind {_V2BETA1_WRITES_FLAG}=1. "
                f"Would have deleted id={role_assignment_id}"
            )
        resp = self._client._request(
            "DELETE", f"/authorization/v1beta1/role-assignments/{role_assignment_id}"
        )
        if not resp.is_success:
            raise RuntimeError(
                f"GLP DELETE role-assignments/{role_assignment_id} returned "
                f"HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            return resp.json()
        except Exception:
            return {"status": "completed", "rawResponse": resp.text[:500]}

    def create_scope_group(self, scope_group: dict[str, Any]) -> dict[str, Any]:
        """POST /authorization/v1beta1/scope-groups.

        Body is passed through as-is; per the spec it must include ``name``
        (a scope group cannot nest another scope group). Guarded by
        ``HPE_MCP_GLP_V2BETA1_WRITES=1``.
        """
        if not _writes_enabled():
            raise NotImplementedError(
                f"GLP scope-group create is gated behind {_V2BETA1_WRITES_FLAG}=1. "
                f"Payload that would have been sent: {scope_group}"
            )
        resp = self._client._request(
            "POST", "/authorization/v1beta1/scope-groups", json=scope_group
        )
        if not resp.is_success:
            raise RuntimeError(
                f"GLP POST scope-groups returned HTTP {resp.status_code}: "
                f"{resp.text[:300]}"
            )
        try:
            return resp.json()
        except Exception:
            return {"status": "completed", "rawResponse": resp.text[:500]}

    def update_scope_group(
        self, scope_group_id: str, scope_group: dict[str, Any]
    ) -> dict[str, Any]:
        """PUT /authorization/v1beta1/scope-groups/{id}.

        Per the spec, the body must still include the immutable ``id``
        attribute. Guarded by ``HPE_MCP_GLP_V2BETA1_WRITES=1``.
        """
        if not _writes_enabled():
            raise NotImplementedError(
                f"GLP scope-group update is gated behind {_V2BETA1_WRITES_FLAG}=1. "
                f"Payload that would have been sent: {scope_group}"
            )
        resp = self._client._request(
            "PUT",
            f"/authorization/v1beta1/scope-groups/{scope_group_id}",
            json=scope_group,
        )
        if not resp.is_success:
            raise RuntimeError(
                f"GLP PUT scope-groups/{scope_group_id} returned "
                f"HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            return resp.json()
        except Exception:
            return {"status": "completed", "rawResponse": resp.text[:500]}

    def delete_scope_group(self, scope_group_id: str) -> dict[str, Any]:
        """DELETE /authorization/v1beta1/scope-groups/{id}.

        Guarded by ``HPE_MCP_GLP_V2BETA1_WRITES=1``.
        """
        if not _writes_enabled():
            raise NotImplementedError(
                f"GLP scope-group delete is gated behind {_V2BETA1_WRITES_FLAG}=1. "
                f"Would have deleted id={scope_group_id}"
            )
        resp = self._client._request(
            "DELETE", f"/authorization/v1beta1/scope-groups/{scope_group_id}"
        )
        if not resp.is_success:
            raise RuntimeError(
                f"GLP DELETE scope-groups/{scope_group_id} returned "
                f"HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            return resp.json()
        except Exception:
            return {"status": "completed", "rawResponse": resp.text[:500]}

    def add_scope_group_scopes(
        self, scope_group_id: str, items: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """POST /authorization/v1beta1/scope-groups/{id}/scopes/batch.

        ``items`` is required by the spec (``{"items": [...]}``). This
        operation is synchronous and non-atomic per the spec. Guarded by
        ``HPE_MCP_GLP_V2BETA1_WRITES=1``.
        """
        if not _writes_enabled():
            raise NotImplementedError(
                f"GLP scope-group add-scopes is gated behind {_V2BETA1_WRITES_FLAG}=1. "
                f"Payload that would have been sent: items={items}"
            )
        resp = self._client._request(
            "POST",
            f"/authorization/v1beta1/scope-groups/{scope_group_id}/scopes/batch",
            json={"items": items},
        )
        if not resp.is_success:
            raise RuntimeError(
                f"GLP POST scope-groups/{scope_group_id}/scopes/batch returned "
                f"HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            return resp.json()
        except Exception:
            return {"status": "completed", "rawResponse": resp.text[:500]}

    def delete_scope_group_scopes(
        self, scope_group_id: str, items: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """DELETE /authorization/v1beta1/scope-groups/{id}/scopes/bulk.

        ``items`` is required by the spec (``{"items": [...]}``) — this is a
        DELETE with a request body, so it goes through ``_request`` directly
        rather than the bodyless ``CentralClient.delete`` helper. This
        operation is synchronous and non-atomic per the spec. Guarded by
        ``HPE_MCP_GLP_V2BETA1_WRITES=1``.
        """
        if not _writes_enabled():
            raise NotImplementedError(
                f"GLP scope-group delete-scopes is gated behind {_V2BETA1_WRITES_FLAG}=1. "
                f"Payload that would have been sent: items={items}"
            )
        resp = self._client._request(
            "DELETE",
            f"/authorization/v1beta1/scope-groups/{scope_group_id}/scopes/bulk",
            json={"items": items},
        )
        if not resp.is_success:
            raise RuntimeError(
                f"GLP DELETE scope-groups/{scope_group_id}/scopes/bulk returned "
                f"HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            return resp.json()
        except Exception:
            return {"status": "completed", "rawResponse": resp.text[:500]}

    def invite_user(
        self, email: str, send_welcome_email: bool | None = None
    ) -> dict[str, Any]:
        """POST /identity/v1/users — invite a user to the workspace.

        Guarded by ``HPE_MCP_GLP_V2BETA1_WRITES=1``.
        """
        body: dict[str, Any] = {"email": email}
        if send_welcome_email is not None:
            body["sendWelcomeEmail"] = send_welcome_email
        if not _writes_enabled():
            raise NotImplementedError(
                f"GLP user invite is gated behind {_V2BETA1_WRITES_FLAG}=1. "
                f"Payload that would have been sent: {body}"
            )
        resp = self._client._request("POST", "/identity/v1/users", json=body)
        if not resp.is_success:
            raise RuntimeError(
                f"GLP POST identity/v1/users returned HTTP {resp.status_code}: "
                f"{resp.text[:300]}"
            )
        try:
            return resp.json()
        except Exception:
            return {"status": "completed", "rawResponse": resp.text[:500]}

    def update_user_preferences(
        self, user_id: str, idle_timeout: int, language: str
    ) -> dict[str, Any]:
        """PUT /identity/v1/users/{id} — update a user's preferences.

        Both ``idleTimeout`` and ``language`` are the only two properties
        documented for this operation, and PUT semantics mean the full
        preference set is replaced — both are required here rather than
        left as optional partial-update fields. Guarded by
        ``HPE_MCP_GLP_V2BETA1_WRITES=1``.
        """
        body = {"idleTimeout": idle_timeout, "language": language}
        if not _writes_enabled():
            raise NotImplementedError(
                f"GLP user-preferences update is gated behind {_V2BETA1_WRITES_FLAG}=1. "
                f"Payload that would have been sent: {body}"
            )
        resp = self._client._request(
            "PUT", f"/identity/v1/users/{user_id}", json=body
        )
        if not resp.is_success:
            raise RuntimeError(
                f"GLP PUT identity/v1/users/{user_id} returned "
                f"HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            return resp.json()
        except Exception:
            return {"status": "completed", "rawResponse": resp.text[:500]}

    def disassociate_user(self, user_id: str) -> dict[str, Any]:
        """DELETE /identity/v1/users/{id} — remove a user from the workspace.

        Guarded by ``HPE_MCP_GLP_V2BETA1_WRITES=1``.
        """
        if not _writes_enabled():
            raise NotImplementedError(
                f"GLP user disassociate is gated behind {_V2BETA1_WRITES_FLAG}=1. "
                f"Would have deleted user_id={user_id}"
            )
        resp = self._client._request("DELETE", f"/identity/v1/users/{user_id}")
        if not resp.is_success:
            raise RuntimeError(
                f"GLP DELETE identity/v1/users/{user_id} returned "
                f"HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            return resp.json()
        except Exception:
            return {"status": "completed", "rawResponse": resp.text[:500]}

    def list_auto_subscription_settings(self) -> list[dict[str, Any]]:
        """GET /subscriptions/v1/auto-subscription-settings.

        Lists all configured auto-subscription settings in the workspace.
        """
        try:
            result = self._client.get("/subscriptions/v1/auto-subscription-settings")
            return result.get("items", result.get("autoSubscriptionSettings", []))
        except Exception as exc:
            logger.warning("GLP list_auto_subscription_settings failed: %s", exc)
            raise RuntimeError(
                f"GLP list_auto_subscription_settings failed: {exc}"
            ) from exc

    def get_auto_subscription_setting(self, setting_id: str) -> dict[str, Any] | None:
        """GET /subscriptions/v1/auto-subscription-settings/{id}."""
        try:
            return self._client.get(
                f"/subscriptions/v1/auto-subscription-settings/{setting_id}"
            )
        except Exception as exc:
            logger.warning(
                "GLP get_auto_subscription_setting failed for %s: %s", setting_id, exc
            )
            return None

    def update_auto_subscription_settings(
        self, setting_id: str, settings: dict[str, Any]
    ) -> dict[str, Any]:
        """PATCH /subscriptions/v1/auto-subscription-settings/{id}.

        Content-Type is ``application/merge-patch+json`` per the spec. The
        manifest's declared request-body property (``autoSubscriptionSettings``)
        and required property (``autoSubscriptions``) don't match each other —
        not independently re-verified against the Subscriptions v1 spec text
        for this exact operation, so ``settings`` is passed through as-is
        rather than guessing the correct wrapper key; treat a 400/422 here as
        "shape not confirmed on this tenant" and fall back to glp_get to
        inspect current settings first. To remove a configured
        deviceType/tier combination, pass ``tier`` as null for that
        deviceType per the spec. Guarded by
        ``HPE_MCP_GLP_V2BETA1_WRITES=1``.
        """
        if not _writes_enabled():
            raise NotImplementedError(
                "GLP auto-subscription-settings update is gated behind "
                f"{_V2BETA1_WRITES_FLAG}=1. Payload that would have been sent: {settings}"
            )
        resp = self._client._request(
            "PATCH",
            f"/subscriptions/v1/auto-subscription-settings/{setting_id}",
            json=settings,
            headers={"Content-Type": "application/merge-patch+json"},
        )
        if not resp.is_success:
            raise RuntimeError(
                f"GLP PATCH auto-subscription-settings/{setting_id} returned "
                f"HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            return resp.json()
        except Exception:
            return {"status": "completed", "rawResponse": resp.text[:500]}
