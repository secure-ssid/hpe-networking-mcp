"""RateLimitMiddleware — token-bucket cap on tool call rate.

Central applies a **10 requests per second account-wide** limit (source:
https://developer.arubanetworks.com/new-central/docs/getting-started-with-rest-apis).
The limit is shared across *all* tokens for the same account, so our 5+
MCP servers plus any human scripts all draw from the same bucket.

To stay comfortably under the cap, the default rate here is 8/s — the
remaining ~2/s headroom absorbs transient bursts (a handful of tool
calls issued in parallel by a Claude client running multiple subagents).

Uses a simple token bucket: tokens refill at ``rate`` per second up to
``burst`` max. On empty bucket we await the earliest refill time without
blocking the asyncio event loop.
This is **per-process**, so if multiple server processes run on the same
host each has its own bucket. That's fine — we still cut peak rate
roughly by the number of processes, which is the real blast-radius
concern. A fully-correct shared limiter would need a cross-process
coordinator (Redis / file lock) and isn't worth the complexity here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Collection
from typing import Any

logger = logging.getLogger(__name__)


class RateLimitMiddleware:
    """Token-bucket rate limiter around every tool call."""

    def __init__(
        self,
        rate: float = 8.0,
        burst: int | None = None,
        *,
        on_wait: Callable[[float], None] | None = None,
        exempt_names: Collection[str] | None = None,
    ):
        """
        Args:
            rate: Steady-state token refill rate, tokens per second.
            burst: Max tokens in the bucket. Defaults to ``max(2, int(rate))``
                so short bursts up to ``rate`` calls can fire immediately.
            on_wait: Optional observer invoked with the actual wait duration
                (seconds) every time a call has to sleep for a token. Unset
                by default -- existing callers/behavior are unchanged. Never
                called with argument/result content, only the numeric wait;
                any exception it raises is logged and swallowed so a broken
                observer can never break rate limiting itself.
            exempt_names: Optional set of tool names this middleware must not
                charge a token for. Unset by default -- every tool is charged,
                as before. Its only intended use is a *dispatching* tool (the
                router's invoke_read_tool / invoke_tool / invoke_read_tool_batch)
                whose real API cost is one token per backend call it makes, not
                one per outer MCP call: those charge through :meth:`acquire`
                at the dispatch seam instead, so exempting them here prevents
                double-charging while making a batch of N backend calls cost N
                tokens instead of 1.
        """
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate}")
        self.rate = rate
        self.burst = burst if burst is not None else max(2, int(rate))
        self._tokens: float = float(self.burst)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        self._on_wait = on_wait
        self.exempt_names = frozenset(exempt_names or ())

    async def acquire(self) -> None:
        """Wait until a token is available, then consume one.

        Public so a caller that fans one MCP call out into several backend
        calls can charge the bucket per *backend* call (see ``exempt_names``).
        """
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # How long until we'd have 1 token?
                wait = (1.0 - self._tokens) / self.rate
            # Sleep outside the lock so other calls can refill too.
            logger.debug("rate limit: sleeping %.3fs", wait)
            await asyncio.sleep(wait)
            if self._on_wait is not None:
                try:
                    self._on_wait(wait)
                except Exception:
                    logger.warning("rate limit on_wait observer failed", exc_info=True)

    # Backwards-compatible alias for the pre-public spelling.
    _acquire = acquire

    async def before_call(self, name: str, arguments: dict[str, Any]) -> None:
        if name in self.exempt_names:
            return None
        await self.acquire()
        return None

    def after_call(self, name: str, arguments: dict[str, Any], result: Any) -> None:
        return None

    def on_error(self, name: str, arguments: dict[str, Any], exc: BaseException) -> None:
        return None
