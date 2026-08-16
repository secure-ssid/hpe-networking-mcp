"""Multi-turn autonomous AI reasoning and tool-dispatch execution loop."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from hpe_networking_mcp.cli_client.ai.base import (
    AiBackend,
)
from hpe_networking_mcp.cli_client.ai.service import ConversationMemory, ReasoningService
from hpe_networking_mcp.cli_client.safety import SafetyPolicy
from hpe_networking_mcp.cli_client.sessions import SessionManager

__all__ = ["AgentLoopStep", "AgentReasoningLoop", "ReasoningService"]

DEFAULT_SYSTEM_PROMPT = (
    "You are the HPE Networking AI Expert. You diagnose network health, analyze topologies, "
    "recommend switch and AP hardware, plan migrations, and invoke MCP tools safely.\n"
    "When interacting with MCP tools via the low-token router contract:\n"
    "1. Use `find_tool(query=...)` to discover relevant tools from the backend catalog.\n"
    "2. For read-only tools, invoke via `invoke_read_tool(name=..., arguments=...)`.\n"
    "3. For write or destructive tools, invoke via `invoke_tool(name=..., arguments=...)`.\n"
    "4. If domain tools are directly available in your tool list, call them directly.\n"
    "Always prefer read-only diagnostics, confirm destructive actions, and provide concise "
    "user-facing rationale without exposing private chain-of-thought."
)


@dataclass
class AgentLoopStep:
    turn_index: int
    step_type: str  # "thought", "tool_call", "tool_result", "answer", "error", "cancelled"
    content: str
    tool_name: str | None = None
    tool_args: dict[str, Any] = field(default_factory=dict)
    tool_result: Any = None
    usage: dict[str, int] = field(default_factory=dict)
    provider: str = ""
    model: str = ""


def _accumulate_usage(total: dict[str, int], incremental: dict[str, int]) -> dict[str, int]:
    """Sum numerical token usage fields across turns."""
    res = dict(total)
    for k, v in incremental.items():
        if isinstance(v, (int, float)):
            res[k] = res.get(k, 0) + int(v)
    return res


def _format_tool_result_content(
    res_text: str,
    result: Any,
    max_chars: int,
) -> tuple[str, str]:
    """Format tool result string for LLM message and UI display while preserving errors/metadata.

    Returns:
        (llm_content, display_content)
    """
    is_error = False
    if isinstance(result, Exception):
        is_error = True
    elif result is not None:
        if getattr(result, "isError", False) or getattr(result, "is_error", False):
            is_error = True
        elif isinstance(result, dict) and (
            result.get("ok") is False
            or result.get("is_error")
            or "error" in result
            or "code" in result
        ):
            is_error = True
    if res_text.startswith("Tool execution error:") or "[error]" in res_text.lower():
        is_error = True

    if len(res_text) <= max_chars:
        llm_text = res_text
        disp_text = res_text
    else:
        budget = max(1, max_chars)
        meta_prefix = ""
        if isinstance(result, dict):
            meta_keys = [
                "ok",
                "is_error",
                "error",
                "code",
                "status",
                "message",
                "count",
                "next_cursor",
            ]
            preserved_meta = {k: result[k] for k in meta_keys if k in result}
            if preserved_meta:
                meta_prefix = (
                    f"[Result Meta: {json.dumps(preserved_meta, separators=(',', ':'))}]\n"
                )
        if is_error and "[ERROR PRESERVED]" not in meta_prefix:
            meta_prefix = "[ERROR PRESERVED] " + meta_prefix

        marker = f"\n... [showing X of {len(res_text)} chars, truncated]"
        if budget <= len(marker):
            return marker[:budget], marker[:budget]
        avail_for_body = max(0, budget - len(meta_prefix) - len(marker))
        body_slice = res_text[:avail_for_body].rstrip()
        marker = f"\n... [showing {len(body_slice)} of {len(res_text)} chars, truncated]"
        truncated = meta_prefix + body_slice + marker
        if len(truncated) > budget:
            truncated = truncated[: max(0, budget - len(marker))] + marker
        llm_text = truncated
        disp_text = truncated

    return llm_text, disp_text


class AgentReasoningLoop:
    """Compatibility facade over :class:`ReasoningService`.

    New terminal code should consume ``ReasoningService.stream`` directly.
    This adapter preserves the older ``AgentLoopStep`` event shape for callers
    that still use the legacy one-shot loop.
    """

    def __init__(
        self,
        ai_backend: AiBackend,
        session_manager: SessionManager,
        max_turns: int = 6,
        system_prompt: str | None = None,
        max_result_chars: int = 2000,
        safety_policy: SafetyPolicy | None = None,
        memory: ConversationMemory | None = None,
    ) -> None:
        self._service = ReasoningService(
            ai_backend=ai_backend,
            session_manager=session_manager,
            safety_policy=safety_policy,
            memory=memory,
            max_turns=max_turns,
            max_result_chars=max_result_chars,
            system_prompt=system_prompt or DEFAULT_SYSTEM_PROMPT,
        )

    @property
    def total_usage(self) -> dict[str, int]:
        """Cumulative token usage across all turns in this loop run."""
        return self._service.total_usage

    async def run(self, user_prompt: str) -> AsyncIterator[AgentLoopStep]:
        answer_parts: list[str] = []
        async for event in self._service.stream(user_prompt):
            base = {
                "turn_index": event.turn_index,
                "usage": dict(event.usage),
                "provider": event.provider,
                "model": event.model,
            }
            if event.kind == "started":
                yield AgentLoopStep(
                    **base,
                    step_type="thought",
                    content="Model reasoning started; private details omitted.",
                )
            elif event.kind == "text_delta":
                answer_parts.append(event.content)
            elif event.kind == "tool_call":
                yield AgentLoopStep(
                    **base,
                    step_type="tool_call",
                    content=(
                        f"Calling tool `{event.tool_name}` with args: {json.dumps(event.tool_args)}"
                    ),
                    tool_name=event.tool_name,
                    tool_args=event.tool_args,
                )
            elif event.kind == "tool_result":
                yield AgentLoopStep(
                    **base,
                    step_type="tool_result",
                    content=event.content,
                    tool_name=event.tool_name,
                    tool_result=event.tool_result,
                )
            elif event.kind == "completed":
                yield AgentLoopStep(
                    **base,
                    step_type="answer",
                    content=event.content or "".join(answer_parts),
                )
            elif event.kind in {"error", "cancelled"}:
                yield AgentLoopStep(
                    **base,
                    step_type=event.kind,
                    content=event.content or "Request cancelled.",
                )
