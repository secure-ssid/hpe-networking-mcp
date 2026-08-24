"""Opt-in, bounded, in-process metrics for router tool calls.

Set ``HPE_MCP_METRICS=1`` to enable in-process collection: request
counts, latency aggregates (sum/max/fixed-width buckets), outcome counts,
truncation-event counts, and rate-limit wait aggregates, all bucketed by a
*bounded* ``(tool, backend)`` label pair.

This module never reads tool arguments, tool result *values*, or exception
messages -- only:

- the router-level or dispatched-backend tool name (an allow-listed,
  finite catalog resolved by an injected ``label_resolver``, defaulting to
  ``(name, "router", "unknown")`` when none is wired in),
- the owning backend server name,
- a bounded capability enum (``read``/``write``/``destructive``/
  ``diagnostic``/``unknown``),
- a bounded outcome enum (see ``hpe_networking_mcp.mcp_servers._middleware._outcome``),
- wall-clock durations, and
- the presence (not the value) of an existing ``truncated: true`` bounding
  marker already emitted by ``hpe_networking_mcp.mcp_servers.shared.bound_collection_response``
  / ``bounded_response_payload``.

``MetricsRegistry`` additionally caps the *number of distinct label
combinations* it will ever track (``max_series``, default 512); once that
cap is reached, any further unseen ``(tool, backend)`` combination is
folded into a single fixed overflow bucket instead of growing the registry
without bound. This is a deliberate defense-in-depth measure independent of
whatever a caller's ``label_resolver`` returns.

HTTP exposure of a snapshot is a *separate* opt-in gated by
``HPE_MCP_METRICS_HTTP`` -- see
``hpe_networking_mcp.mcp_servers.shared.run_server`` -- and is only ever registered alongside
the existing ``/livez``/``/readyz``/``/healthz`` routes on the
streamable-HTTP transport. The route serves Prometheus text exposition
(``render_prometheus``) by default and the bounded JSON snapshot
(``schema_version: 1``, explicitly unstable) via format negotiation.
Stdio transport never touches this at all, so
enabling collection here never adds unsolicited stdio output.
"""

from __future__ import annotations

import contextvars
import os
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from ._outcome import KNOWN_OUTCOMES, classify_error_outcome, classify_outcome

_ENV_FLAG = "HPE_MCP_METRICS"
_LABEL_RE = re.compile(r"[^a-z0-9_.-]+")
_MAX_LABEL_LEN = 64
_DEFAULT_MAX_SERIES = 512
_OVERFLOW_LABEL = "_overflow_"
# Fixed-width latency buckets (milliseconds, inclusive upper bound) -- a
# small, constant-size histogram, never one bucket per observed value.
_LATENCY_BUCKETS_MS: tuple[int, ...] = (10, 25, 50, 100, 250, 500, 1000, 2500, 5000)

KNOWN_CAPABILITIES = frozenset({"read", "write", "destructive", "diagnostic", "unknown"})


def metrics_enabled() -> bool:
    """Whether in-process metrics collection is opted into for this process."""
    return os.environ.get(_ENV_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def _sanitize_label(value: Any, *, default: str = "unknown") -> str:
    """Bound and normalize a label value -- never the raw input.

    Lower-cases, restricts to a small safe character set, and truncates to
    ``_MAX_LABEL_LEN``. This is defense-in-depth: callers should already
    only pass known tool/backend names, but a label must never be able to
    smuggle an argument value, identifier, or secret into a metric even if
    a future ``label_resolver`` misbehaves.
    """
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    text = _LABEL_RE.sub("_", text)[:_MAX_LABEL_LEN]
    return text or default


LabelResolver = Callable[[str, dict[str, Any]], "tuple[str, str, str]"]


class MetricsRegistry:
    """Bounded counters/aggregates. Thread- and asyncio-safe.

    Every mutable collection here is capped: at most ``max_series``
    distinct ``(tool, backend)`` combinations are tracked; each bucket's
    inner ``outcomes``/``capabilities``/``latency_buckets`` maps are keyed
    only by the small fixed enums above, never by anything unbounded.
    """

    def __init__(self, max_series: int = _DEFAULT_MAX_SERIES):
        if max_series <= 0:
            raise ValueError(f"max_series must be positive, got {max_series}")
        self.max_series = max_series
        self._lock = threading.RLock()
        self._series: dict[tuple[str, str], dict[str, Any]] = {}
        self._rate_limit_wait_count = 0
        self._rate_limit_wait_sum_ms = 0.0
        self._rate_limit_wait_max_ms = 0.0
        self._started_monotonic = time.monotonic()

    def _resolve_key(self, tool: str, backend: str) -> tuple[str, str]:
        key = (tool, backend)
        if key in self._series or len(self._series) < self.max_series:
            return key
        return (_OVERFLOW_LABEL, _OVERFLOW_LABEL)

    def _bucket_locked(self, key: tuple[str, str]) -> dict[str, Any]:
        bucket = self._series.get(key)
        if bucket is None:
            bucket = {
                "tool": key[0],
                "backend": key[1],
                "requests": 0,
                "errors": 0,
                "outcomes": {},
                "capabilities": {},
                "latency_count": 0,
                "latency_sum_ms": 0.0,
                "latency_max_ms": 0.0,
                "latency_buckets": {str(edge): 0 for edge in _LATENCY_BUCKETS_MS},
                "latency_over_max": 0,
                "truncated_events": 0,
            }
            self._series[key] = bucket
        return bucket

    def record_call(
        self,
        *,
        tool: str,
        backend: str,
        capability: str,
        outcome: str,
        duration_ms: float | None,
        truncated: bool,
    ) -> None:
        """Record one completed/failed tool call. Never blocks meaningfully
        (a plain in-memory dict update under a short-lived lock)."""
        tool_l = _sanitize_label(tool)
        backend_l = _sanitize_label(backend)
        capability_l = capability if capability in KNOWN_CAPABILITIES else "unknown"
        outcome_l = outcome if outcome in KNOWN_OUTCOMES else "success"
        with self._lock:
            key = self._resolve_key(tool_l, backend_l)
            bucket = self._bucket_locked(key)
            bucket["requests"] += 1
            bucket["outcomes"][outcome_l] = bucket["outcomes"].get(outcome_l, 0) + 1
            bucket["capabilities"][capability_l] = bucket["capabilities"].get(capability_l, 0) + 1
            if outcome_l != "success":
                bucket["errors"] += 1
            if truncated:
                bucket["truncated_events"] += 1
            if duration_ms is not None and duration_ms >= 0:
                bucket["latency_count"] += 1
                bucket["latency_sum_ms"] += duration_ms
                bucket["latency_max_ms"] = max(bucket["latency_max_ms"], duration_ms)
                for edge in _LATENCY_BUCKETS_MS:
                    if duration_ms <= edge:
                        bucket["latency_buckets"][str(edge)] += 1
                        break
                else:
                    bucket["latency_over_max"] += 1

    def record_rate_limit_wait(self, seconds: float) -> None:
        """Record one observed rate-limit sleep. Global aggregate -- no
        per-tool label, since the token bucket is shared process-wide."""
        if seconds is None or seconds < 0:
            return
        ms = float(seconds) * 1000.0
        with self._lock:
            self._rate_limit_wait_count += 1
            self._rate_limit_wait_sum_ms += ms
            self._rate_limit_wait_max_ms = max(self._rate_limit_wait_max_ms, ms)

    def snapshot(self) -> dict[str, Any]:
        """Bounded, JSON-serializable snapshot. Safe to serve over HTTP."""
        with self._lock:
            series = []
            for bucket in self._series.values():
                copy = dict(bucket)
                copy["outcomes"] = dict(bucket["outcomes"])
                copy["capabilities"] = dict(bucket["capabilities"])
                copy["latency_buckets"] = dict(bucket["latency_buckets"])
                series.append(copy)
            rate_limit = {
                "wait_count": self._rate_limit_wait_count,
                "wait_sum_ms": round(self._rate_limit_wait_sum_ms, 3),
                "wait_max_ms": round(self._rate_limit_wait_max_ms, 3),
            }
            uptime_seconds = round(time.monotonic() - self._started_monotonic, 3)
        return {
            "schema_version": 1,
            "uptime_seconds": uptime_seconds,
            "series_count": len(series),
            "series_cap": self.max_series,
            "series": series,
            "rate_limit": rate_limit,
        }

    def reset(self) -> None:
        """Test-only: clear all recorded state."""
        with self._lock:
            self._series.clear()
            self._rate_limit_wait_count = 0
            self._rate_limit_wait_sum_ms = 0.0
            self._rate_limit_wait_max_ms = 0.0
            self._started_monotonic = time.monotonic()


def _escape_label_value(value: Any) -> str:
    """Escape a label value for the Prometheus text format.

    Registry labels are already sanitized to ``[a-z0-9_.-]`` by
    ``_sanitize_label``; this is defense-in-depth against any snapshot
    producer that did not pass through the registry.
    """
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_sample_value(value: Any) -> str:
    if isinstance(value, bool):  # guard: bool is an int subclass
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    return repr(float(value))


def render_prometheus(snapshot: dict[str, Any]) -> str:
    """Render a ``MetricsRegistry.snapshot()`` as Prometheus text (0.0.4).

    Pure function over the snapshot dict -- no registry access, no new
    dependencies. The mapping is honest to what the registry stores:
    outcomes and capabilities are *separate marginal* counts per series
    (never a joint capability x outcome count), so they render as two
    distinct counter families rather than one fabricated joint series.

    The registry records per-bin (exclusive) latency bucket counts --
    ``record_call`` stops at the first matching edge -- while Prometheus
    histograms need cumulative ``le`` counts. This renderer accumulates
    left-to-right and maps ``latency_over_max`` into ``le="+Inf"`` so that
    ``sum(le counts) == latency_count`` always holds.
    """
    lines: list[str] = []

    def emit_header(name: str, help_text: str, type_: str) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {type_}")

    def emit(name: str, labels: dict[str, str], value: Any) -> None:
        if labels:
            inner = ",".join(f'{k}="{_escape_label_value(v)}"' for k, v in labels.items())
            lines.append(f"{name}{{{inner}}} {_format_sample_value(value)}")
        else:
            lines.append(f"{name} {_format_sample_value(value)}")

    emit_header(
        "hpe_mcp_tool_calls_total",
        "Tool calls by tool, backend, and outcome.",
        "counter",
    )
    emit_header(
        "hpe_mcp_tool_capability_calls_total",
        "Tool calls by tool, backend, and capability.",
        "counter",
    )
    emit_header(
        "hpe_mcp_tool_call_latency_ms",
        "Tool call latency in milliseconds (fixed buckets).",
        "histogram",
    )
    emit_header(
        "hpe_mcp_tool_call_truncated_total",
        "Tool calls whose result carried a truncation marker.",
        "counter",
    )
    for series in snapshot.get("series", []):
        base = {"tool": series.get("tool", "unknown"), "backend": series.get("backend", "unknown")}
        for outcome, count in sorted(series.get("outcomes", {}).items()):
            emit("hpe_mcp_tool_calls_total", {**base, "outcome": outcome}, count)
        for capability, count in sorted(series.get("capabilities", {}).items()):
            emit("hpe_mcp_tool_capability_calls_total", {**base, "capability": capability}, count)
        cumulative = 0
        for edge in _LATENCY_BUCKETS_MS:
            cumulative += int(series.get("latency_buckets", {}).get(str(edge), 0))
            emit("hpe_mcp_tool_call_latency_ms_bucket", {**base, "le": str(edge)}, cumulative)
        latency_count = int(series.get("latency_count", 0))
        emit("hpe_mcp_tool_call_latency_ms_bucket", {**base, "le": "+Inf"}, latency_count)
        emit("hpe_mcp_tool_call_latency_ms_sum", base, series.get("latency_sum_ms", 0.0))
        emit("hpe_mcp_tool_call_latency_ms_count", base, latency_count)
        emit("hpe_mcp_tool_call_truncated_total", base, series.get("truncated_events", 0))

    rate_limit = snapshot.get("rate_limit", {})
    emit_header("hpe_mcp_rate_limit_waits_total", "Observed rate-limit sleeps.", "counter")
    emit("hpe_mcp_rate_limit_waits_total", {}, rate_limit.get("wait_count", 0))
    emit_header(
        "hpe_mcp_rate_limit_wait_ms_total",
        "Cumulative milliseconds spent in rate-limit sleeps.",
        "counter",
    )
    emit("hpe_mcp_rate_limit_wait_ms_total", {}, rate_limit.get("wait_sum_ms", 0.0))
    emit_header("hpe_mcp_rate_limit_wait_max_ms", "Longest single rate-limit sleep.", "gauge")
    emit("hpe_mcp_rate_limit_wait_max_ms", {}, rate_limit.get("wait_max_ms", 0.0))

    emit_header("hpe_mcp_metrics_series", "Distinct (tool, backend) series tracked.", "gauge")
    emit("hpe_mcp_metrics_series", {}, snapshot.get("series_count", 0))
    emit_header("hpe_mcp_metrics_series_cap", "Maximum series before overflow folding.", "gauge")
    emit("hpe_mcp_metrics_series_cap", {}, snapshot.get("series_cap", 0))
    emit_header("hpe_mcp_metrics_uptime_seconds", "Seconds since registry start.", "gauge")
    emit("hpe_mcp_metrics_uptime_seconds", {}, snapshot.get("uptime_seconds", 0.0))

    return "\n".join(lines) + "\n"


_default_registry: MetricsRegistry | None = None
_default_registry_lock = threading.Lock()


def get_default_registry() -> MetricsRegistry:
    """Process-wide default registry, shared between ``MetricsMiddleware``
    and the optional ``/metrics`` HTTP snapshot route so both refer to the
    same in-memory state without any extra wiring."""
    global _default_registry
    with _default_registry_lock:
        if _default_registry is None:
            _default_registry = MetricsRegistry()
        return _default_registry


def _truncated_marker(section: Any) -> bool:
    return isinstance(section, dict) and section.get("truncated") is True


def _result_is_truncated(result: Any) -> bool:
    """Whether ``result`` carries an existing, already-computed truncation
    marker (``_pagination``/``_response_bounds``) -- checked shallowly at
    the top level and one level under a response-envelope ``data`` key.
    Never inspects list/collection *contents*, only these known control
    fields, so this can never leak identifiers or secrets."""
    if not isinstance(result, dict):
        return False
    if _truncated_marker(result.get("_pagination")) or _truncated_marker(
        result.get("_response_bounds")
    ):
        return True
    data = result.get("data")
    if isinstance(data, dict):
        if _truncated_marker(data.get("_pagination")) or _truncated_marker(
            data.get("_response_bounds")
        ):
            return True
    return False


class MetricsMiddleware:
    """Opt-in bounded metrics around every tool call.

    Inert (every hook a no-op) unless ``HPE_MCP_METRICS=1`` -- default
    behavior/performance is unchanged when disabled.
    """

    def __init__(
        self,
        registry: MetricsRegistry | None = None,
        *,
        label_resolver: LabelResolver | None = None,
    ) -> None:
        self.registry = registry if registry is not None else get_default_registry()
        self._label_resolver = label_resolver
        self._starts: contextvars.ContextVar[list[float] | None] = contextvars.ContextVar(
            f"hpe_mcp_metrics_starts_{id(self)}",
            default=None,
        )

    def _labels(self, name: str, arguments: dict[str, Any]) -> tuple[str, str, str]:
        if self._label_resolver is not None:
            try:
                resolved = self._label_resolver(name, arguments)
            except Exception:
                resolved = None
            if resolved is not None:
                return resolved
        return (name, "router", "unknown")

    def before_call(self, name: str, arguments: dict[str, Any]) -> None:
        if metrics_enabled():
            starts = list(self._starts.get() or [])
            starts.append(time.monotonic())
            self._starts.set(starts)
        return None

    def _duration_ms(self) -> float | None:
        starts = list(self._starts.get() or [])
        if not starts:
            return None
        started = starts.pop()
        self._starts.set(starts)
        return round((time.monotonic() - started) * 1000, 3)

    def after_call(self, name: str, arguments: dict[str, Any], result: Any) -> None:
        duration_ms = self._duration_ms()
        if not metrics_enabled():
            return None
        tool, backend, capability = self._labels(name, arguments)
        self.registry.record_call(
            tool=tool,
            backend=backend,
            capability=capability,
            outcome=classify_outcome(result),
            duration_ms=duration_ms,
            truncated=_result_is_truncated(result),
        )
        return None

    def on_error(self, name: str, arguments: dict[str, Any], exc: BaseException) -> None:
        duration_ms = self._duration_ms()
        if not metrics_enabled():
            return None
        tool, backend, capability = self._labels(name, arguments)
        self.registry.record_call(
            tool=tool,
            backend=backend,
            capability=capability,
            outcome=classify_error_outcome(exc),
            duration_ms=duration_ms,
            truncated=False,
        )
        return None
