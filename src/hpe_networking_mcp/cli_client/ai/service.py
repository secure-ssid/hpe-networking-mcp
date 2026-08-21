"""Reusable event-driven AI orchestration for the built-in clients.

The service owns conversation memory, provider metadata, bounded MCP tool
results, cancellation checks, and the client-side safety gate.  It deliberately
does not emit provider ``thought_trace``/thinking blocks: callers receive
answer deltas and observable tool activity, not a chain-of-thought contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from hpe_networking_mcp.cli_client.ai.base import (
    AiBackend,
    ChatMessage,
    MessageRole,
    ToolCallDelta,
    ToolCallRequest,
)
from hpe_networking_mcp.cli_client.output import tool_result_to_text
from hpe_networking_mcp.cli_client.safety import SafetyDecision, SafetyPolicy

DEFAULT_SYSTEM_PROMPT = (
    "You are the HPE Networking AI Expert. Diagnose network health, analyze "
    "topologies, recommend switch and AP hardware, and plan migrations. "
    "Use available MCP tools when live data is needed. With a low-token "
    "router, discover backend operations with find_tool and call reads with "
    "invoke_read_tool; use invoke_tool only when the user has explicitly "
    "approved a write. Prefer read-only diagnostics. Never claim that a "
    "write occurred unless the tool result confirms it, and do not reveal "
    "private chain-of-thought."
)


@dataclass
class ConversationMemory:
    """Bounded conversation history shared by terminal and TUI sessions."""

    max_messages: int = 80
    _messages: list[ChatMessage] = field(default_factory=list)

    @property
    def messages(self) -> list[ChatMessage]:
        """Return a copy suitable for passing to an adapter."""
        return list(self._messages)

    def snapshot(self) -> list[ChatMessage]:
        return self.messages

    @classmethod
    def from_messages(
        cls,
        messages: Iterable[ChatMessage],
        *,
        max_messages: int = 80,
    ) -> ConversationMemory:
        memory = cls(max_messages=max_messages)
        memory.extend(messages)
        return memory

    def append(self, message: ChatMessage) -> None:
        self._messages.append(message)
        limit = max(1, int(self.max_messages))
        if len(self._messages) > limit:
            self._messages = self._messages[-limit:]

    def extend(self, messages: Iterable[ChatMessage]) -> None:
        for message in messages:
            self.append(message)

    def clear(self) -> None:
        self._messages.clear()

    def __len__(self) -> int:
        return len(self._messages)


@dataclass
class ReasoningEvent:
    """Observable service event.

    ``kind`` is one of ``started``, ``text_delta``, ``tool_call``,
    ``tool_result``, ``completed``, ``error``, or ``cancelled``.  The event
    intentionally has no thought/hidden-reasoning event type.
    """

    kind: str
    content: str = ""
    turn_index: int = 0
    provider: str = ""
    model: str = ""
    tool_name: str | None = None
    tool_args: dict[str, Any] = field(default_factory=dict)
    tool_result: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    allowed: bool | None = None
    is_read_only: bool | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def type(self) -> str:
        """Alias useful to clients that call event kinds ``type``."""
        return self.kind


@dataclass
class ReasoningResult:
    """Collected result of one service request."""

    content: str
    events: list[ReasoningEvent]
    provider: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    cancelled: bool = False

    @property
    def metadata(self) -> dict[str, str]:
        return {"provider": self.provider, "model": self.model}


ToolDispatch = Callable[[str, dict[str, Any]], Awaitable[Any] | Any]


class _ServiceCancelled(Exception):
    """Internal cooperative cancellation marker."""


def _accumulate_usage(total: dict[str, int], incremental: dict[str, int]) -> dict[str, int]:
    result = dict(total)
    for key, value in incremental.items():
        if isinstance(value, (int, float)):
            result[key] = result.get(key, 0) + int(value)
    return result


def _result_is_error(result: Any, text: str) -> bool:
    if isinstance(result, Exception):
        return True
    if getattr(result, "isError", False) or getattr(result, "is_error", False):
        return True
    if isinstance(result, dict) and (
        result.get("ok") is False or result.get("is_error") or "error" in result or "code" in result
    ):
        return True
    return text.lower().startswith("[error]") or text.lower().startswith("tool execution error:")


def _result_metadata(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    keys = (
        "ok",
        "is_error",
        "error",
        "code",
        "status",
        "message",
        "count",
        "next_cursor",
    )
    data = {key: result[key] for key in keys if key in result}
    if not data:
        return ""
    return f"[Result Meta: {json.dumps(data, separators=(',', ':'), default=str)}]\n"


def bound_tool_result(result: Any, max_chars: int = 4000) -> str:
    """Convert an MCP result to text with a strict character budget.

    Error markers and small envelope metadata are retained before the body is
    truncated.  The returned value is always at most ``max_chars`` characters.
    """

    limit = max(1, int(max_chars))
    if isinstance(result, Exception):
        text = f"Tool execution error: {type(result).__name__}: {result}"
    else:
        try:
            text = tool_result_to_text(result, max_chars=max(limit * 4, limit))
        except Exception as exc:  # pragma: no cover - defensive formatter boundary
            text = f"Tool result formatting error: {type(exc).__name__}: {exc}"
    text = str(text)
    if len(text) <= limit:
        return text

    marker = "\n… [truncated]"
    error_prefix = "[ERROR PRESERVED] " if _result_is_error(result, text) else ""
    metadata = _result_metadata(result)
    prefix = error_prefix + metadata
    # For very small caller budgets, the budget itself is authoritative.
    if limit <= len(marker):
        return marker[:limit]
    body_budget = max(0, limit - len(prefix) - len(marker))
    body = text[:body_budget].rstrip()
    bounded = f"{prefix}{body}{marker}"
    if len(bounded) <= limit:
        return bounded
    # A metadata envelope can itself be large. Keep the marker and trim the
    # prefix/body together rather than violating the advertised bound.
    return bounded[: max(0, limit - len(marker))] + marker


class _ToolMetadata:
    """Small adapter for catalog entries returned as dictionaries."""

    def __init__(self, name: str, data: dict[str, Any]) -> None:
        self.name = name
        self.description = data.get("description", "")
        self.annotations = data.get("annotations")
        self.read_only = data.get("read_only")
        self.capability = data.get("capability")
        self.inputSchema = data.get("inputSchema") or data.get("input_schema") or {"type": "object"}


class ReasoningService:
    """Provider-neutral, event-driven model + MCP orchestration service.

    Built-in client API:

    * ``stream(prompt, cancel_event=...)`` yields :class:`ReasoningEvent`.
    * ``complete(prompt, cancel_event=...)`` collects the same events into a
      :class:`ReasoningResult`.
    * ``memory`` retains bounded :class:`ChatMessage` history across prompts.
    * ``metadata`` exposes only provider/model/backend identifiers.

    MCP writes are checked through the supplied :class:`SafetyPolicy`; the
    service never treats a model request as approval.
    """

    def __init__(
        self,
        ai_backend: AiBackend,
        session_manager: Any | None = None,
        *,
        safety_policy: SafetyPolicy | None = None,
        memory: ConversationMemory | None = None,
        tool_dispatch: ToolDispatch | None = None,
        tool_specs: Iterable[Any] | None = None,
        max_turns: int = 6,
        max_result_chars: int = 4000,
        system_prompt: str | None = None,
    ) -> None:
        self.ai_backend = ai_backend
        self.session_manager = session_manager
        self.safety_policy = safety_policy or SafetyPolicy()
        self.memory = memory or ConversationMemory()
        self.tool_dispatch = tool_dispatch
        self.tool_specs = list(tool_specs or ())
        self.max_turns = max(1, int(max_turns))
        env_limit = os.environ.get("HPE_MCP_MAX_RESULT_CHARS", "").strip()
        if env_limit.isdigit() and int(env_limit) > 0:
            max_result_chars = int(env_limit)
        self.max_result_chars = max(1, int(max_result_chars))
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self._total_usage: dict[str, int] = {}
        self._cancel_event: asyncio.Event | None = None
        self._tool_aliases: dict[str, str] = {}

    @property
    def provider(self) -> str:
        return self.ai_backend.provider

    @property
    def model(self) -> str:
        return self.ai_backend.model

    @property
    def metadata(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "model": self.model,
            "backend": self.ai_backend.name,
        }

    @property
    def total_usage(self) -> dict[str, int]:
        return dict(self._total_usage)

    def cancel(self) -> None:
        """Request cooperative cancellation of the active stream."""
        if self._cancel_event is not None:
            self._cancel_event.set()

    def clear_memory(self) -> None:
        self.memory.clear()

    async def _cancelled(self, cancel_event: asyncio.Event | None) -> bool:
        requested = cancel_event and cancel_event.is_set()
        internal = self._cancel_event and self._cancel_event.is_set()
        return bool(requested or internal)

    async def _available_tools(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if self.session_manager is None:
            items: Iterable[Any] = self.tool_specs
            if not self.tool_specs:
                return [], {}
        else:
            items = ()

        if self.session_manager is not None:
            list_method = getattr(self.session_manager, "list_all_tools", None)
            if callable(list_method):
                value = list_method()
                items = await value if inspect.isawaitable(value) else value
            else:
                raw_tools = getattr(self.session_manager, "tools", {})
                items = raw_tools.values() if isinstance(raw_tools, dict) else raw_tools

        specs: list[dict[str, Any]] = []
        by_name: dict[str, Any] = {}
        self._tool_aliases = {}
        manager_tools = getattr(self.session_manager, "tools", {})
        manager_items = manager_tools.items() if isinstance(manager_tools, dict) else ()
        for item in items or ():
            name = (
                str(item.get("name", ""))
                if isinstance(item, dict)
                else str(getattr(item, "name", ""))
            )
            if not name:
                continue
            aliases = [str(key) for key, value in manager_items if value is item]
            preferred_name = next(
                (alias for alias in aliases if "." in alias),
                aliases[0] if aliases else name,
            )
            model_name = re.sub(r"[^A-Za-z0-9_-]", "_", preferred_name)
            if model_name != preferred_name:
                if model_name in self._tool_aliases:
                    model_name = f"{model_name}_{len(self._tool_aliases)}"
                self._tool_aliases[model_name] = preferred_name
            description = (
                item.get("description", "")
                if isinstance(item, dict)
                else getattr(item, "description", "")
            ) or ""
            schema = (
                item.get("inputSchema") or item.get("input_schema")
                if isinstance(item, dict)
                else getattr(item, "inputSchema", None) or getattr(item, "input_schema", None)
            ) or {"type": "object"}
            specs.append(
                {
                    "name": model_name,
                    "description": description,
                    "inputSchema": schema,
                }
            )
            metadata = _ToolMetadata(preferred_name, item) if isinstance(item, dict) else item
            by_name[name] = metadata
            by_name[preferred_name] = metadata
            by_name[model_name] = metadata
        return specs, by_name

    def _resolve_tool(self, name: str, by_name: dict[str, Any]) -> tuple[str, Any | None]:
        manager = self.session_manager
        resolved = self._tool_aliases.get(name, name)
        if manager is not None:
            resolver = getattr(manager, "resolve_tool_name", None)
            if callable(resolver):
                try:
                    resolved = resolver(resolved)
                except KeyError:
                    resolved = self._tool_aliases.get(name, name)
        if resolved in by_name:
            return resolved, by_name[resolved]
        suffix_hits = [key for key in by_name if key == name or key.endswith(f".{name}")]
        if len(suffix_hits) == 1:
            return suffix_hits[0], by_name[suffix_hits[0]]
        return resolved, by_name.get(resolved)

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if self.tool_dispatch is not None:
            result = self.tool_dispatch(name, arguments)
            return await result if inspect.isawaitable(result) else result
        if self.session_manager is None:
            raise RuntimeError("no MCP session or tool dispatcher is configured")
        return await self.session_manager.call_tool(name, arguments)

    async def _call_tool_cancellable(
        self,
        name: str,
        arguments: dict[str, Any],
        cancel_event: asyncio.Event | None,
    ) -> Any:
        if cancel_event is None:
            return await self._call_tool(name, arguments)
        tool_task = asyncio.create_task(self._call_tool(name, arguments))
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {tool_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            tool_task.cancel()
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tool_task
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task
            raise
        if cancel_task in done and cancel_event.is_set():
            tool_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await tool_task
            raise _ServiceCancelled
        cancel_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_task
        return await tool_task

    async def _next_stream_chunk_cancellable(
        self,
        iterator: AsyncIterator[Any],
        cancel_event: asyncio.Event | None,
    ) -> Any:
        """Wait for one provider chunk while allowing cancellation to interrupt it."""
        if cancel_event is None:
            return await anext(iterator)

        chunk_task = asyncio.ensure_future(anext(iterator))
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {chunk_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            chunk_task.cancel()
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await chunk_task
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task
            raise
        if cancel_task in done and cancel_event.is_set():
            chunk_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await chunk_task
            close = getattr(iterator, "aclose", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    value = close()
                    if inspect.isawaitable(value):
                        await value
            raise _ServiceCancelled
        cancel_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_task
        return await chunk_task

    async def _await_cancellable(
        self,
        awaitable: Awaitable[Any],
        cancel_event: asyncio.Event | None,
    ) -> Any:
        """Await a legacy non-streaming backend call with cancellation support."""
        if cancel_event is None:
            return await awaitable

        operation_task = asyncio.ensure_future(awaitable)
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {operation_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            operation_task.cancel()
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await operation_task
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task
            raise
        if cancel_task in done and cancel_event.is_set():
            operation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await operation_task
            raise _ServiceCancelled
        cancel_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_task
        return await operation_task

    @staticmethod
    def _merge_delta_text(existing: str, incoming: str) -> str:
        """Merge repeated or cumulative provider fields without duplication."""
        if not incoming:
            return existing
        if not existing:
            return incoming
        if incoming == existing or incoming.startswith(existing):
            return incoming
        if existing.startswith(incoming):
            return existing
        return existing + incoming

    @staticmethod
    def _merge_tool_call_deltas(deltas: list[ToolCallDelta]) -> list[ToolCallRequest]:
        grouped: dict[int, ToolCallDelta] = {}
        for delta in deltas:
            current = grouped.setdefault(delta.index, ToolCallDelta(index=delta.index))
            current.call_id = ReasoningService._merge_delta_text(current.call_id, delta.call_id)
            current.tool_name = ReasoningService._merge_delta_text(
                current.tool_name, delta.tool_name
            )
            current.arguments_fragment += delta.arguments_fragment
        calls: list[ToolCallRequest] = []
        for index in sorted(grouped):
            delta = grouped[index]
            arguments_valid = True
            arguments_error: str | None = None
            try:
                arguments = json.loads(delta.arguments_fragment or "{}")
                if not isinstance(arguments, dict):
                    arguments_valid = False
                    arguments_error = "tool arguments must be a JSON object"
                    arguments = {}
            except json.JSONDecodeError as exc:
                arguments_valid = False
                arguments_error = f"invalid JSON arguments: {exc.msg}"
                arguments = {}
            calls.append(
                ToolCallRequest(
                    call_id=delta.call_id or f"stream-call-{index}",
                    tool_name=delta.tool_name,
                    arguments=arguments,
                    arguments_valid=arguments_valid,
                    arguments_error=arguments_error,
                )
            )
        return calls

    @staticmethod
    def _dedupe_tool_calls(calls: list[ToolCallRequest]) -> list[ToolCallRequest]:
        unique: list[ToolCallRequest] = []
        seen: set[tuple[str, str, str]] = set()
        for call in calls:
            key = (
                call.call_id,
                call.tool_name,
                json.dumps(
                    (call.arguments, call.arguments_valid, call.arguments_error),
                    sort_keys=True,
                    default=str,
                ),
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(call)
        return unique

    async def stream(
        self,
        prompt: str,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[ReasoningEvent]:
        """Stream one non-empty prompt and retain its conversation memory."""

        text_prompt = (prompt or "").strip()
        if not text_prompt:
            return

        self._cancel_event = cancel_event or asyncio.Event()
        self._total_usage = {}
        yield ReasoningEvent(
            kind="started",
            provider=self.provider,
            model=self.model,
            metadata=dict(self.metadata),
        )
        if await self._cancelled(cancel_event):
            yield ReasoningEvent(
                kind="cancelled",
                provider=self.provider,
                model=self.model,
                metadata=dict(self.metadata),
            )
            return
        try:
            tools, tool_map = await self._available_tools()
        except Exception:
            # A catalog failure should not turn ordinary model chat into a
            # request failure; the backend can still answer without tools.
            tools, tool_map = [], {}
        messages = self.memory.snapshot()
        self.memory.append(ChatMessage(role=MessageRole.USER, content=text_prompt))
        messages = self.memory.snapshot()

        last_content = ""
        for turn in range(1, self.max_turns + 1):
            if await self._cancelled(cancel_event):
                yield ReasoningEvent(
                    kind="cancelled",
                    turn_index=turn,
                    provider=self.provider,
                    model=self.model,
                    usage=dict(self._total_usage),
                    metadata=dict(self.metadata),
                )
                return

            content_parts: list[str] = []
            tool_calls: list[ToolCallRequest] = []
            tool_deltas: list[ToolCallDelta] = []
            response_usage: dict[str, int] = {}
            saw_chunk = False
            try:
                stream_iterator = self.ai_backend.complete_stream(
                    messages=messages,
                    tools=tools or None,
                    system_prompt=self.system_prompt,
                ).__aiter__()
                while True:
                    try:
                        chunk = await self._next_stream_chunk_cancellable(
                            stream_iterator,
                            self._cancel_event,
                        )
                    except StopAsyncIteration:
                        break
                    if chunk is None:
                        continue
                    saw_chunk = True
                    if await self._cancelled(cancel_event):
                        yield ReasoningEvent(
                            kind="cancelled",
                            turn_index=turn,
                            provider=self.provider,
                            model=self.model,
                            usage=dict(self._total_usage),
                            metadata=dict(self.metadata),
                        )
                        return
                    response_usage = _accumulate_usage(response_usage, chunk.usage)
                    if chunk.delta_content:
                        content_parts.append(chunk.delta_content)
                        yield ReasoningEvent(
                            kind="text_delta",
                            content=chunk.delta_content,
                            turn_index=turn,
                            provider=self.provider,
                            model=self.model,
                            usage=dict(chunk.usage),
                        )
                    tool_calls.extend(chunk.tool_calls)
                    tool_deltas.extend(chunk.tool_call_deltas)
                if not saw_chunk:
                    # Keep simple/legacy adapters compatible when they only
                    # implement complete() meaningfully.
                    response = await self._await_cancellable(
                        self.ai_backend.complete(
                            messages=messages,
                            tools=tools or None,
                            system_prompt=self.system_prompt,
                        ),
                        self._cancel_event,
                    )
                    response_usage = _accumulate_usage(response_usage, response.usage)
                    if response.content:
                        content_parts.append(response.content)
                        yield ReasoningEvent(
                            kind="text_delta",
                            content=response.content,
                            turn_index=turn,
                            provider=self.provider,
                            model=self.model,
                            usage=dict(response.usage),
                        )
                    tool_calls.extend(response.tool_calls)
            except _ServiceCancelled:
                yield ReasoningEvent(
                    kind="cancelled",
                    turn_index=turn,
                    provider=self.provider,
                    model=self.model,
                    usage=dict(self._total_usage),
                    metadata=dict(self.metadata),
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - backend boundary
                yield ReasoningEvent(
                    kind="error",
                    content=f"AI completion error: {type(exc).__name__}: {exc}",
                    turn_index=turn,
                    provider=self.provider,
                    model=self.model,
                    usage=dict(self._total_usage),
                    metadata=dict(self.metadata),
                )
                return

            self._total_usage = _accumulate_usage(self._total_usage, response_usage)
            if tool_deltas:
                tool_calls.extend(self._merge_tool_call_deltas(tool_deltas))
            tool_calls = self._dedupe_tool_calls(tool_calls)
            last_content = "".join(content_parts)

            if not tool_calls:
                self.memory.append(ChatMessage(role=MessageRole.ASSISTANT, content=last_content))
                yield ReasoningEvent(
                    kind="completed",
                    content=last_content,
                    turn_index=turn,
                    provider=self.provider,
                    model=self.model,
                    usage=dict(self._total_usage),
                    metadata={**self.metadata, "finish_reason": "stop"},
                )
                return

            self.memory.append(
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    content=last_content,
                    tool_calls=tool_calls,
                )
            )
            messages = self.memory.snapshot()

            for tool_call in tool_calls:
                if await self._cancelled(cancel_event):
                    yield ReasoningEvent(
                        kind="cancelled",
                        turn_index=turn,
                        provider=self.provider,
                        model=self.model,
                        usage=dict(self._total_usage),
                        metadata=dict(self.metadata),
                    )
                    return

                resolved_name, tool_obj = self._resolve_tool(tool_call.tool_name, tool_map)
                if not tool_call.arguments_valid or not isinstance(tool_call.arguments, dict):
                    arguments_error = tool_call.arguments_error
                    if arguments_error is None and not isinstance(tool_call.arguments, dict):
                        arguments_error = "tool arguments must be a JSON object"
                    decision = SafetyDecision(
                        allowed=False,
                        reason=(
                            "invalid tool arguments; dispatch refused"
                            + (
                                f": {arguments_error}"
                                if arguments_error
                                else ""
                            )
                        ),
                        requires_confirm=False,
                        is_read_only=False,
                    )
                elif tool_obj is None:
                    decision = SafetyDecision(
                        allowed=False,
                        reason=f"unknown tool {tool_call.tool_name!r}; dispatch refused",
                        requires_confirm=False,
                        is_read_only=False,
                    )
                else:
                    decision = self.safety_policy.check(tool_obj)
                yield ReasoningEvent(
                    kind="tool_call",
                    content=f"Calling tool `{tool_call.tool_name}`",
                    turn_index=turn,
                    provider=self.provider,
                    model=self.model,
                    tool_name=resolved_name,
                    tool_args=dict(tool_call.arguments),
                    allowed=decision.allowed,
                    is_read_only=decision.is_read_only,
                    metadata={"safety_reason": decision.reason},
                )

                if not decision.allowed:
                    bounded = bound_tool_result(
                        {"ok": False, "error": decision.reason},
                        self.max_result_chars,
                    )
                    self.memory.append(
                        ChatMessage(
                            role=MessageRole.TOOL,
                            name=resolved_name,
                            tool_call_id=tool_call.call_id,
                            content=bounded,
                        )
                    )
                    messages = self.memory.snapshot()
                    yield ReasoningEvent(
                        kind="tool_result",
                        content=bounded,
                        turn_index=turn,
                        provider=self.provider,
                        model=self.model,
                        tool_name=resolved_name,
                        tool_result=bounded,
                        allowed=False,
                        is_read_only=decision.is_read_only,
                        metadata={"safety_reason": decision.reason},
                    )
                    continue

                try:
                    raw_result = await self._call_tool_cancellable(
                        resolved_name,
                        tool_call.arguments,
                        self._cancel_event,
                    )
                    bounded = bound_tool_result(raw_result, self.max_result_chars)
                    is_error = _result_is_error(raw_result, bounded)
                except _ServiceCancelled:
                    yield ReasoningEvent(
                        kind="cancelled",
                        turn_index=turn,
                        provider=self.provider,
                        model=self.model,
                        usage=dict(self._total_usage),
                        metadata=dict(self.metadata),
                    )
                    return
                except Exception as exc:  # noqa: BLE001 - tool boundary
                    bounded = bound_tool_result(exc, self.max_result_chars)
                    is_error = True

                self.memory.append(
                    ChatMessage(
                        role=MessageRole.TOOL,
                        name=resolved_name,
                        tool_call_id=tool_call.call_id,
                        content=bounded,
                    )
                )
                messages = self.memory.snapshot()
                yield ReasoningEvent(
                    kind="tool_result",
                    content=bounded,
                    turn_index=turn,
                    provider=self.provider,
                    model=self.model,
                    tool_name=resolved_name,
                    tool_result=bounded,
                    allowed=True,
                    is_read_only=decision.is_read_only,
                    metadata={"is_error": is_error},
                )

        yield ReasoningEvent(
            kind="completed",
            content=last_content,
            turn_index=self.max_turns,
            provider=self.provider,
            model=self.model,
            usage=dict(self._total_usage),
            metadata={**self.metadata, "finish_reason": "max_turns"},
        )

    def run(
        self,
        prompt: str,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[ReasoningEvent]:
        """Compatibility alias for ``stream``."""
        return self.stream(prompt, cancel_event=cancel_event)

    async def complete(
        self,
        prompt: str,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> ReasoningResult:
        """Collect a stream into a result while preserving the same event API."""
        events: list[ReasoningEvent] = []
        content_parts: list[str] = []
        async for event in self.stream(prompt, cancel_event=cancel_event):
            events.append(event)
            if event.kind == "text_delta":
                content_parts.append(event.content)
        completed = next(
            (event for event in reversed(events) if event.kind == "completed"),
            None,
        )
        if completed is not None and not content_parts:
            content_parts.append(completed.content)
        return ReasoningResult(
            content="".join(content_parts),
            events=events,
            provider=self.provider,
            model=self.model,
            usage=dict(self._total_usage),
            cancelled=any(event.kind == "cancelled" for event in events),
        )


__all__ = [
    "ConversationMemory",
    "DEFAULT_SYSTEM_PROMPT",
    "ReasoningEvent",
    "ReasoningResult",
    "ReasoningService",
    "bound_tool_result",
]
