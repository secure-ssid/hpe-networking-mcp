"""Multi-turn autonomous AI reasoning and tool-dispatch execution loop."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from hpe_networking_mcp.cli_client.ai.base import (
    AiBackend,
    ChatMessage,
    MessageRole,
)
from hpe_networking_mcp.cli_client.sessions import SessionManager


@dataclass
class AgentLoopStep:
    turn_index: int
    step_type: str  # "thought", "tool_call", "tool_result", "answer", "error"
    content: str
    tool_name: str | None = None
    tool_args: dict[str, Any] = field(default_factory=dict)
    tool_result: Any = None


class AgentReasoningLoop:
    """Coordinates an AI backend with MCP tools over multiple reasoning turns."""

    def __init__(
        self,
        ai_backend: AiBackend,
        session_manager: SessionManager,
        max_turns: int = 6,
        system_prompt: str | None = None,
    ) -> None:
        self._ai = ai_backend
        self._session_mgr = session_manager
        self._max_turns = max_turns
        self._system_prompt = system_prompt or (
            "You are the HPE Networking AI Expert. You diagnose network health, analyze topologies, "
            "recommend switch and AP hardware, plan migrations, and invoke MCP tools safely. "
            "Always prefer read-only diagnostics and explain your reasoning clearly."
        )

    async def run(self, user_prompt: str) -> AsyncIterator[AgentLoopStep]:
        messages: list[ChatMessage] = [
            ChatMessage(role=MessageRole.USER, content=user_prompt)
        ]

        # Collect available tools from active MCP session
        available_tools = []
        try:
            tools_list = await self._session_mgr.list_all_tools()
            for t in tools_list:
                available_tools.append({
                    "name": t.name,
                    "description": t.description or "",
                    "inputSchema": t.inputSchema if hasattr(t, "inputSchema") else {"type": "object"},
                })
        except Exception:
            available_tools = []

        for turn in range(1, self._max_turns + 1):
            try:
                response = await self._ai.complete(
                    messages=messages,
                    tools=available_tools if available_tools else None,
                    system_prompt=self._system_prompt,
                )
            except Exception as e:
                yield AgentLoopStep(
                    turn_index=turn,
                    step_type="error",
                    content=f"AI completion error: {e}",
                )
                break

            if response.thought_trace:
                yield AgentLoopStep(
                    turn_index=turn,
                    step_type="thought",
                    content=response.thought_trace,
                )

            if not response.tool_calls:
                yield AgentLoopStep(
                    turn_index=turn,
                    step_type="answer",
                    content=response.content,
                )
                break

            # Process tool calls
            messages.append(
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    content=response.content,
                    tool_calls=response.tool_calls,
                )
            )

            for tc in response.tool_calls:
                yield AgentLoopStep(
                    turn_index=turn,
                    step_type="tool_call",
                    content=f"Calling tool `{tc.tool_name}` with args: {json.dumps(tc.arguments)}",
                    tool_name=tc.tool_name,
                    tool_args=tc.arguments,
                )

                try:
                    # Execute tool via MCP session manager
                    result = await self._session_mgr.call_tool(tc.tool_name, tc.arguments)
                    res_text = json.dumps(result, indent=2) if isinstance(result, (dict, list)) else str(result)
                except Exception as ex:
                    res_text = f"Tool execution error: {ex}"

                yield AgentLoopStep(
                    turn_index=turn,
                    step_type="tool_result",
                    content=res_text[:1000] + ("..." if len(res_text) > 1000 else ""),
                    tool_name=tc.tool_name,
                    tool_result=result if "result" in locals() else None,
                )

                messages.append(
                    ChatMessage(
                        role=MessageRole.TOOL,
                        name=tc.tool_name,
                        tool_call_id=tc.call_id,
                        content=res_text,
                    )
                )
