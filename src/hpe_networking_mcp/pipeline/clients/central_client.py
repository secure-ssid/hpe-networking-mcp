"""Aruba Central REST API client.

Wraps HTTP calls with automatic token refresh and 429/5xx retry+backoff.

Ported from aruba-central-portal/utils/central_api_client.py.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from hpe_networking_mcp.pipeline.clients.http_retry import parse_retry_after as _parse_retry_after
from hpe_networking_mcp.pipeline.clients.token_manager import TokenManager

logger = logging.getLogger(__name__)

_DIAGNOSTIC_TROUBLESHOOTING_ACTIONS = frozenset(
    {
        "aaa",
        "cableTest",
        "getArpTable",
        "http",
        "https",
        "iperf",
        "locate",
        "nslookup",
        "ping",
        "pingSweep",
        "showCommands",
        "speedtest",
        "tcp",
        "traceroute",
    }
)
_TROUBLESHOOTING_DEVICE_SEGMENTS = frozenset({"aps", "cx", "aos-s", "gateways"})
_TROUBLESHOOTING_SERIAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _is_allowed_diagnostic_request(method: str, endpoint: str) -> bool:
    if method.upper() != "POST":
        return False
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        return False
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        return False
    parts = parsed.path.strip("/").split("/")
    return (
        len(parts) == 5
        and parts[0] == "network-troubleshooting"
        and parts[1] in {"v1", "v1alpha1"}
        and parts[2] in _TROUBLESHOOTING_DEVICE_SEGMENTS
        and bool(_TROUBLESHOOTING_SERIAL_RE.fullmatch(parts[3]))
        and parts[4] in _DIAGNOSTIC_TROUBLESHOOTING_ACTIONS
    )


def _post_error(response: httpx.Response) -> Exception:
    """Build the error raised for a failed POST.

    Attaches ``.response`` — mirroring httpx.HTTPStatusError — so callers
    doing ``getattr(exc, "response", None).text`` see the real body.
    """
    exc = Exception(f"{response.status_code} {response.reason_phrase} — {response.text[:500]}")
    exc.response = response  # type: ignore[attr-defined]
    return exc


def error_body(exc: Exception) -> str:
    """Response body text from an HTTP-error exception, or "" if there is none."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return ""
    return getattr(resp, "text", "") or ""


class ResponseParseError(ValueError):
    """Raised when a 2xx response carries a body that is not valid JSON.

    Previously these were logged and swallowed as ``{}``, which callers could
    not distinguish from a genuinely empty (204 / zero-length) response — a
    gateway error page returned with a 200 looked like a successful no-op.
    The exception carries the originating ``response`` (mirroring
    ``httpx.HTTPStatusError``) plus a truncated body preview for diagnosis.
    """

    def __init__(self, response: httpx.Response, reason: str) -> None:
        body = response.text or ""
        preview = body[:300]
        if len(body) > 300:
            preview = f"{preview}... [truncated {len(body) - 300} chars]"
        content_type = response.headers.get("Content-Type", "")
        super().__init__(
            f"Malformed JSON in HTTP {response.status_code} response from "
            f"{response.request.method if response.request else '?'} "
            f"{response.request.url if response.request else '?'} "
            f"(Content-Type={content_type!r}, body_len={len(body)}): {reason} — "
            f"body preview: {preview!r}"
        )
        self.response = response
        self.reason = reason
        self.body_preview = preview


_INITIAL_RETRY_DELAY = 60  # seconds — Central rate-limit window
_MAX_RETRY_DELAY = 300
# 5xx retry uses a much smaller floor — these are usually transient, not
# quota exhaustion. Exponential backoff with jitter.
_SERVER_ERROR_INITIAL_DELAY = 1.0
_SERVER_ERROR_MAX_DELAY = 30.0

# Endpoints already warned about, so a deprecated endpoint on a hot path logs
# once per process instead of once per call (what the docstring always
# promised). Keyed by (endpoint, Deprecation, Sunset) so a *changed* notice
# still surfaces. Guarded by a lock because CentralClient is shared across
# threads via hpe_networking_mcp.mcp_servers.shared.get_client().
# Human-facing platform names for write-gate errors, so a blocked write reads
# "Central write requests are disabled" / "GLP write requests are disabled".
_PLATFORM_DISPLAY_NAMES = {"central": "Central", "glp": "GLP"}

_deprecation_warned: set[tuple[str, str, str]] = set()
_deprecation_warned_lock = threading.Lock()


def reset_deprecation_warning_cache() -> None:
    """Clear the process-wide deprecation-warning dedup cache (tests only)."""
    with _deprecation_warned_lock:
        _deprecation_warned.clear()


# ``RateLimit-Reset`` (IETF draft) is delta-seconds, but the older
# ``X-RateLimit-Reset`` convention that several gateways in front of Central
# use carries an *absolute* Unix epoch instead. Treating an epoch as
# delta-seconds yields a ~55-year wait, so values large enough to only make
# sense as timestamps are converted back into a delta here. Thresholds:
# >= 1e12 -> epoch milliseconds, >= 1e9 (2001-09-09) -> epoch seconds.
_EPOCH_SECONDS_THRESHOLD = 1_000_000_000.0
_EPOCH_MILLIS_THRESHOLD = 1_000_000_000_000.0


def _parse_rate_limit_reset(value: Optional[str]) -> Optional[float]:
    """Parse a ``RateLimit-Reset`` / ``X-RateLimit-Reset`` header into the
    number of seconds until the quota window resets.

    Accepts delta-seconds, an absolute Unix epoch (seconds or milliseconds),
    or an HTTP-date. Returns ``None`` when the value is unparseable.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        return _parse_retry_after(text)
    if numeric >= _EPOCH_MILLIS_THRESHOLD:
        return max(0.0, numeric / 1000.0 - time.time())
    if numeric >= _EPOCH_SECONDS_THRESHOLD:
        return max(0.0, numeric - time.time())
    return max(0.0, numeric)


@dataclass(frozen=True)
class RateLimitStatus:
    """Parsed rate-limit response metadata (RateLimit-* / X-RateLimit-*)."""

    limit: Optional[int]
    remaining: Optional[int]
    reset_seconds: Optional[float]
    raw_reset: Optional[str]


@dataclass(frozen=True)
class DeprecationStatus:
    """Parsed API-deprecation response metadata (Deprecation / Sunset / Link)."""

    deprecation: Optional[str]
    sunset: Optional[str]
    link: Optional[str]


def _parse_int_header(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def _extract_rate_limit(headers: Any) -> Optional[RateLimitStatus]:
    """Parse rate-limit headers, preferring the IETF draft names with a
    fallback to the older ``X-RateLimit-*`` convention some gateways use."""
    limit = headers.get("RateLimit-Limit") or headers.get("X-RateLimit-Limit")
    remaining = headers.get("RateLimit-Remaining") or headers.get("X-RateLimit-Remaining")
    reset = headers.get("RateLimit-Reset") or headers.get("X-RateLimit-Reset")
    if limit is None and remaining is None and reset is None:
        return None
    return RateLimitStatus(
        limit=_parse_int_header(limit),
        remaining=_parse_int_header(remaining),
        reset_seconds=_parse_rate_limit_reset(reset),
        raw_reset=reset,
    )


def _extract_deprecation(headers: Any) -> Optional[DeprecationStatus]:
    """Parse RFC 8594 ``Deprecation``/``Sunset`` headers plus a ``Link``
    header carrying a deprecation-notice URL, if Central sends one."""
    deprecation = headers.get("Deprecation")
    sunset = headers.get("Sunset")
    link = headers.get("Link")
    if not deprecation and not sunset:
        return None
    return DeprecationStatus(deprecation=deprecation, sunset=sunset, link=link)


class CentralClient:
    """HTTP client for Aruba Central REST APIs with token refresh and retry."""

    def __init__(
        self,
        base_url: str,
        token_manager: TokenManager,
        write_platform: str = "central",
    ):
        """
        Args:
            base_url: API host root (no trailing slash required).
            token_manager: supplies/refreshes the bearer token.
            write_platform: which platform write gate governs non-GET verbs on
                this transport. Defaults to ``"central"``. GLPClient wraps this
                same transport but passes ``"glp"`` so GreenLake writes are
                gated by ``HPE_MCP_GLP_V2BETA1_WRITES`` and are *not*
                collaterally blocked (nor accidentally enabled) by
                ``HPE_MCP_CENTRAL_WRITES``.
        """
        self.base_url = base_url.rstrip("/")
        self.token_manager = token_manager
        self.write_platform = write_platform
        self.timeout = 30.0
        self.session = httpx.Client(timeout=self.timeout)
        self.session.headers.update({"Content-Type": "application/json"})
        # Generation observed at the moment the current Authorization header
        # was set — passed back to TokenManager on a 401 retry so concurrent
        # 401s against the same stale token collapse into one real refresh
        # instead of one per request. See TokenManager.get_access_token().
        self._token_generation = 0
        # Most recent rate-limit / deprecation response metadata, updated on
        # every response (success or failure). Side-channel only — never
        # merged into a tool's returned JSON, so existing callers' response
        # shapes are unaffected. Domain tools may read these opportunistically.
        self.last_rate_limit: Optional[RateLimitStatus] = None
        self.last_deprecation: Optional[DeprecationStatus] = None
        self._refresh_auth_header()

    def _enforce_write_gate(
        self,
        method: str,
        endpoint: str,
        *,
        diagnostic: bool = False,
    ) -> None:
        """Raise ``PermissionError`` if this transport's platform write gate
        is closed. The message names the platform and *its own* env var, so a
        blocked GLP write never tells the operator to flip a Central flag.

        ``diagnostic=True`` is reserved for trusted, explicitly annotated
        non-mutating POST operations and is honored only for the allowlisted
        Central troubleshooting action paths above.
        """
        if method.upper() in ("GET", "HEAD", "OPTIONS"):
            return
        if (
            self.write_platform == "central"
            and diagnostic
            and _is_allowed_diagnostic_request(method, endpoint)
        ):
            return
        from hpe_networking_mcp.mcp_servers.shared import (
            platform_write_enable_instruction,
            platform_write_gate_state,
            platform_writes_allowed,
        )

        platform = self.write_platform
        if platform_writes_allowed(platform):
            return
        gate = platform_write_gate_state(platform)
        display = _PLATFORM_DISPLAY_NAMES.get(platform, platform)
        raise PermissionError(
            f"{display} write requests are disabled "
            f"({gate['env_var']} resolved to {gate['state']!r} via {gate['source']}). "
            f"{platform_write_enable_instruction(platform, gate['env_var'])} "
            f"to enable {display} writes. "
            "This gate is independent of the other platforms' write gates."
        )

    def _refresh_auth_header(self) -> int:
        token, generation = self.token_manager.get_access_token_with_generation()
        self._token_generation = generation
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        return generation

    def _ensure_valid_token(self) -> int:
        return self._refresh_auth_header()

    def _record_response_metadata(self, response: httpx.Response, endpoint: str) -> None:
        """Capture rate-limit / deprecation metadata from ``response`` and
        log a warning the first time a deprecated endpoint is hit in a
        given process (avoids per-call log spam on hot paths)."""
        rate_limit = _extract_rate_limit(response.headers)
        if rate_limit is not None:
            self.last_rate_limit = rate_limit
        deprecation = _extract_deprecation(response.headers)
        if deprecation is not None:
            self.last_deprecation = deprecation
            key = (
                endpoint,
                deprecation.deprecation or "",
                deprecation.sunset or "",
            )
            with _deprecation_warned_lock:
                first_time = key not in _deprecation_warned
                if first_time:
                    _deprecation_warned.add(key)
            if first_time:
                logger.warning(
                    "Deprecated endpoint called: %s (Deprecation=%r Sunset=%r Link=%r)",
                    endpoint,
                    deprecation.deprecation,
                    deprecation.sunset,
                    deprecation.link,
                )

    def rate_limit_status(self) -> Optional[dict[str, Any]]:
        """Most recent rate-limit metadata as a plain dict, or ``None`` if
        no response has carried rate-limit headers yet."""
        if self.last_rate_limit is None:
            return None
        return {
            "limit": self.last_rate_limit.limit,
            "remaining": self.last_rate_limit.remaining,
            "reset_seconds": self.last_rate_limit.reset_seconds,
            "raw_reset": self.last_rate_limit.raw_reset,
        }

    def deprecation_status(self) -> Optional[dict[str, Any]]:
        """Most recent API-deprecation metadata as a plain dict, or ``None``
        if no response has carried deprecation headers yet."""
        if self.last_deprecation is None:
            return None
        return {
            "deprecation": self.last_deprecation.deprecation,
            "sunset": self.last_deprecation.sunset,
            "link": self.last_deprecation.link,
        }

    def _request(
        self,
        method: str,
        endpoint: str,
        max_retries: int = 3,
        *,
        diagnostic: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """Issue an HTTP request, honoring Retry-After on 429 and backing
        off on transient 5xx errors.

        Retry policy:
        - 429: wait ``Retry-After`` if the header is present (clamped to
          ``_MAX_RETRY_DELAY``); otherwise use the legacy 60s → 300s
          1.5× backoff path for compatibility.
        - 502/503/504: exponential backoff (1s → 30s) with ±20% jitter.
        - Any other status: return immediately (callers decide).

        Only idempotent semantics are retried; POST and PATCH are retried
        here only on 429 (the request hasn't been accepted yet — the
        Central gateway rejects before the handler runs) and on 5xx the
        caller opts in with ``retry_5xx=True``.
        """
        self._enforce_write_gate(method, endpoint, diagnostic=diagnostic)

        # Caller opt-in to retry 5xx on non-GET verbs. GET/HEAD retry 5xx
        # unconditionally because they're safe.
        retry_5xx = kwargs.pop("retry_5xx", None)
        if retry_5xx is None:
            retry_5xx = method.upper() in ("GET", "HEAD")

        url = f"{self.base_url}{endpoint}"
        retry_429_delay = _INITIAL_RETRY_DELAY
        retry_5xx_delay = _SERVER_ERROR_INITIAL_DELAY

        for attempt in range(max_retries + 1):
            request_generation = self._ensure_valid_token()
            response = self.session.request(method, url, **kwargs)
            self._record_response_metadata(response, endpoint)

            if response.status_code == 401 and attempt < max_retries:
                logger.warning(
                    "Unauthorized (401) on %s %s — forcing token refresh (attempt %d/%d)",
                    method,
                    url,
                    attempt + 1,
                    max_retries,
                )
                # Pass the generation observed when this request's token was
                # set — if another thread already refreshed since then, this
                # collapses into a no-op check instead of a redundant fetch.
                self.token_manager.get_access_token(
                    force_refresh=True,
                    observed_generation=request_generation,
                )
                self._refresh_auth_header()
                continue

            if response.status_code == 429 and attempt < max_retries:
                # Prefer the server's hint if present.
                hint = _parse_retry_after(response.headers.get("Retry-After", ""))
                wait = hint if hint is not None else retry_429_delay
                wait = min(wait, _MAX_RETRY_DELAY)
                logger.warning(
                    "Rate limit (429) on %s %s — waiting %.1fs (attempt %d/%d, Retry-After=%r)",
                    method,
                    url,
                    wait,
                    attempt + 1,
                    max_retries,
                    response.headers.get("Retry-After"),
                )
                time.sleep(wait)
                # Grow the no-header fallback so repeated 429s don't
                # hammer the API.
                retry_429_delay = min(int(retry_429_delay * 1.5), _MAX_RETRY_DELAY)
                continue

            if (
                retry_5xx
                and response.status_code in (502, 503, 504)
                and attempt < max_retries
            ):
                jitter = 1.0 + random.uniform(-0.2, 0.2)
                wait = min(retry_5xx_delay * jitter, _SERVER_ERROR_MAX_DELAY)
                logger.warning(
                    "Transient server error %d on %s %s — waiting %.2fs "
                    "(attempt %d/%d)",
                    response.status_code,
                    method,
                    url,
                    wait,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(wait)
                retry_5xx_delay = min(retry_5xx_delay * 2, _SERVER_ERROR_MAX_DELAY)
                continue

            return response

        return response  # last response after all retries

    async def _arequest(
        self,
        method: str,
        endpoint: str,
        max_retries: int = 3,
        *,
        diagnostic: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """Async counterpart to ``_request`` for MCP tools running on an event loop."""
        self._enforce_write_gate(method, endpoint, diagnostic=diagnostic)

        retry_5xx = kwargs.pop("retry_5xx", None)
        if retry_5xx is None:
            retry_5xx = method.upper() in ("GET", "HEAD")

        url = f"{self.base_url}{endpoint}"
        retry_429_delay = _INITIAL_RETRY_DELAY
        retry_5xx_delay = _SERVER_ERROR_INITIAL_DELAY
        extra_headers = kwargs.pop("headers", None)

        async with httpx.AsyncClient(timeout=self.timeout) as session:
            for attempt in range(max_retries + 1):
                token, generation = await asyncio.to_thread(
                    self.token_manager.get_access_token_with_generation
                )
                headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
                if extra_headers:
                    headers.update(extra_headers)

                response = await session.request(method, url, headers=headers, **kwargs)
                self._record_response_metadata(response, endpoint)

                if response.status_code == 401 and attempt < max_retries:
                    logger.warning(
                        "Unauthorized (401) on %s %s — forcing token refresh (attempt %d/%d)",
                        method,
                        url,
                        attempt + 1,
                        max_retries,
                    )
                    # Same generation-aware collapse as the sync path — see
                    # TokenManager.get_access_token()'s observed_generation.
                    await asyncio.to_thread(
                        self.token_manager.get_access_token,
                        True,
                        observed_generation=generation,
                    )
                    continue

                if response.status_code == 429 and attempt < max_retries:
                    hint = _parse_retry_after(response.headers.get("Retry-After", ""))
                    wait = hint if hint is not None else retry_429_delay
                    wait = min(wait, _MAX_RETRY_DELAY)
                    logger.warning(
                        "Rate limit (429) on %s %s — waiting %.1fs (attempt %d/%d, Retry-After=%r)",
                        method,
                        url,
                        wait,
                        attempt + 1,
                        max_retries,
                        response.headers.get("Retry-After"),
                    )
                    await asyncio.sleep(wait)
                    retry_429_delay = min(int(retry_429_delay * 1.5), _MAX_RETRY_DELAY)
                    continue

                if (
                    retry_5xx
                    and response.status_code in (502, 503, 504)
                    and attempt < max_retries
                ):
                    jitter = 1.0 + random.uniform(-0.2, 0.2)
                    wait = min(retry_5xx_delay * jitter, _SERVER_ERROR_MAX_DELAY)
                    logger.warning(
                        "Transient server error %d on %s %s — waiting %.2fs "
                        "(attempt %d/%d)",
                        response.status_code,
                        method,
                        url,
                        wait,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(wait)
                    retry_5xx_delay = min(retry_5xx_delay * 2, _SERVER_ERROR_MAX_DELAY)
                    continue

                return response

        return response

    def get(self, endpoint: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        logger.debug(
            "GET %s%s params_keys=%s",
            self.base_url,
            endpoint,
            sorted((params or {}).keys()),
        )
        response = self._request("GET", endpoint, params=params)
        response.raise_for_status()
        return _parse_json(response)

    async def aget(self, endpoint: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        logger.debug(
            "GET(async) %s%s params_keys=%s",
            self.base_url,
            endpoint,
            sorted((params or {}).keys()),
        )
        response = await self._arequest("GET", endpoint, params=params)
        response.raise_for_status()
        return _parse_json(response)

    def post(
        self,
        endpoint: str,
        data: Optional[dict[str, Any] | list[Any]] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        logger.debug(
            "POST %s%s body_type=%s body_keys=%s",
            self.base_url,
            endpoint,
            type(data).__name__ if data is not None else None,
            sorted(data.keys()) if isinstance(data, dict) else None,
        )
        response = self._request("POST", endpoint, json=data, params=params)
        if not response.is_success:
            raise _post_error(response)
        return _parse_json(response)

    def post_async(
        self,
        endpoint: str,
        data: Optional[dict[str, Any] | list[Any]] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> str:
        """POST to an async endpoint; returns the Location header value (task URI)."""
        logger.debug("POST(async) %s%s", self.base_url, endpoint)
        response = self._request("POST", endpoint, json=data, params=params)
        if not response.is_success:
            raise _post_error(response)
        location = response.headers.get("Location", "")
        logger.info("POST async Location: %s", location)
        return location

    def patch(
        self,
        endpoint: str,
        data: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        logger.debug("PATCH %s%s", self.base_url, endpoint)
        response = self._request("PATCH", endpoint, json=data, params=params)
        response.raise_for_status()
        return _parse_json(response)

    def put(
        self,
        endpoint: str,
        data: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        logger.debug("PUT %s%s", self.base_url, endpoint)
        response = self._request("PUT", endpoint, json=data, params=params)
        response.raise_for_status()
        return _parse_json(response)

    def delete(
        self,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        logger.debug("DELETE %s%s", self.base_url, endpoint)
        response = self._request("DELETE", endpoint, params=params)
        response.raise_for_status()
        return _parse_json(response)

def _parse_json(response: httpx.Response) -> dict[str, Any]:
    """Decode a successful response body.

    An empty body (204 / zero-length 200) is a legitimate "no content" result
    and still maps to ``{}``. A *non-empty* body that is not valid JSON is a
    real defect — an HTML error page, a truncated payload, or a gateway
    interstitial served with a 2xx — and now raises ``ResponseParseError``
    instead of being silently reported to callers as an empty success.
    """
    if not response.text or not response.text.strip():
        return {}
    try:
        result = response.json()
    except ValueError as exc:
        logger.error(
            "Failed to parse JSON from %s (status=%d body_len=%d): %s",
            response.request.url if response.request else "?",
            response.status_code,
            len(response.text or ""),
            exc,
        )
        raise ResponseParseError(response, str(exc)) from exc
    return result if isinstance(result, dict) else {"items": result}
