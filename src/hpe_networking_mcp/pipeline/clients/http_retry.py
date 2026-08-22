"""Shared HTTP retry-header parsing and a GET-only async retry helper."""

from __future__ import annotations

import asyncio
import email.utils
import logging
import math
import random
import time

import httpx

logger = logging.getLogger(__name__)


def parse_retry_after(value: str) -> float | None:
    """Parse Retry-After delta-seconds or an HTTP-date into seconds."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        pass
    else:
        return max(0.0, seconds) if math.isfinite(seconds) else None
    try:
        target = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    return max(0.0, target.timestamp() - time.time())


#: Transient upstream statuses worth one bounded retry on a safe verb.
RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})

_RETRY_INITIAL_DELAY = 0.5
_RETRY_MAX_DELAY = 8.0
#: Cap for a server-supplied Retry-After hint. Deliberately far above the
#: computed-backoff cap: retrying earlier than the server asked just earns
#: another 429, but an unbounded hint would stall a tool call indefinitely.
_RETRY_AFTER_MAX_DELAY = 60.0


async def get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_retries: int = 2,
    **kwargs,
) -> httpx.Response:
    """Issue one GET, retrying transient failures with bounded backoff.

    GET-only by construction -- there is no method parameter, so no caller
    can route a non-safe verb through this helper. A mutation accepted
    server-side and then throttled must never be re-sent automatically
    (double-apply risk); write paths keep their single-attempt behavior.

    Retries :data:`RETRYABLE_STATUSES` (429/502/503/504) and
    ``httpx.TransportError`` (covers a silently dropped keep-alive connection
    on a pooled client). ``Retry-After`` (delta-seconds or HTTP-date, via
    :func:`parse_retry_after`) is honored as-is; otherwise the delay is
    exponential with ±20% jitter, capped at 8s.
    """
    delay = _RETRY_INITIAL_DELAY
    for attempt in range(max_retries + 1):
        try:
            response = await client.get(url, **kwargs)
        except httpx.TransportError:
            if attempt == max_retries:
                raise
            wait = min(delay, _RETRY_MAX_DELAY) * random.uniform(0.8, 1.2)
            logger.warning(
                "Transport error on GET %s — retrying in %.1fs (attempt %d/%d)",
                url,
                wait,
                attempt + 1,
                max_retries,
            )
            await asyncio.sleep(wait)
            delay = min(delay * 2, _RETRY_MAX_DELAY)
            continue
        if response.status_code not in RETRYABLE_STATUSES or attempt == max_retries:
            return response
        hint = parse_retry_after(response.headers.get("Retry-After", ""))
        if hint is not None:
            wait = min(hint, _RETRY_AFTER_MAX_DELAY)
        else:
            wait = min(delay, _RETRY_MAX_DELAY) * random.uniform(0.8, 1.2)
        logger.warning(
            "Transient %d on GET %s — retrying in %.1fs (attempt %d/%d)",
            response.status_code,
            url,
            wait,
            attempt + 1,
            max_retries,
        )
        await asyncio.sleep(wait)
        delay = min(delay * 2, _RETRY_MAX_DELAY)
    raise AssertionError("unreachable: the final attempt returns or raises above")


#: Request kwargs that carry a body. A GET with a body is not the plain read
#: path, and ``httpx``'s ``.get()`` rejects these kwargs outright.
_BODY_KWARGS = frozenset({"json", "content", "data", "files"})


async def request_read_retried(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs,
) -> httpx.Response:
    """Dispatch for generic executors: retry coverage for reads only.

    A bodiless ``GET`` routes through :func:`get_with_retry`; every other
    shape -- any non-GET method, or a GET carrying a body -- keeps the
    single-attempt behavior. The retry guardrail (never re-send a possible
    mutation) therefore holds even when the caller's ``method`` is dynamic.
    """
    if method.upper() == "GET" and not (_BODY_KWARGS & kwargs.keys()):
        return await get_with_retry(client, url, **kwargs)
    return await client.request(method, url, **kwargs)
