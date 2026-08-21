"""Anthropic Claude LLM reasoning adapter."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from hpe_networking_mcp.cli_client.ai.base import (
    AiBackend,
    AiResponse,
    AiStreamChunk,
    ChatMessage,
    MessageRole,
    ToolCallDelta,
    ToolCallRequest,
)


class AnthropicAdapter(AiBackend):
    """Adapter for Anthropic Claude models with tool use and thinking support."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self._base_url = (base_url or "https://api.anthropic.com/v1").rstrip("/")
        self._model = model or os.getenv("ANTHROPIC_MODEL", "claude-3-7-sonnet-20250219")
        self._timeout = timeout

    @property
    def name(self) -> str:
        return f"anthropic:{self._model}"

    def _format_messages(self, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        formatted: list[dict[str, Any]] = []
        for m in messages:
            if m.role == MessageRole.SYSTEM:
                continue
            role = "user" if m.role in (MessageRole.USER, MessageRole.TOOL) else "assistant"
            if m.role == MessageRole.TOOL:
                formatted.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id or "tool_call_0",
                                "content": m.content,
                            }
                        ],
                    }
                )
            elif m.tool_calls:
                content_blocks: list[dict[str, Any]] = []
                if m.content:
                    content_blocks.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.call_id,
                            "name": tc.tool_name,
                            "input": tc.arguments,
                        }
                    )
                formatted.append({"role": "assistant", "content": content_blocks})
            else:
                formatted.append({"role": role, "content": m.content})
        return formatted

    def _format_tools(self, tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        formatted = []
        for t in tools:
            schema = t.get("inputSchema", t.get("parameters", {"type": "object", "properties": {}}))
            formatted.append(
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": schema,
                }
            )
        return formatted

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
    ) -> AiResponse:
        url = f"{self._base_url}/messages"
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 4096,
            "messages": self._format_messages(messages),
        }
        if system_prompt:
            payload["system"] = system_prompt

        formatted_tools = self._format_tools(tools)
        if formatted_tools:
            payload["tools"] = formatted_tools

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        text_parts = []
        thought_parts = []
        tool_calls: list[ToolCallRequest] = []

        for block in data.get("content", []):
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "thinking":
                thought_parts.append(block.get("thinking", ""))
            elif btype == "tool_use":
                arguments = block.get("input", {})
                arguments_valid = isinstance(arguments, dict)
                tool_calls.append(
                    ToolCallRequest(
                        call_id=block.get("id", ""),
                        tool_name=block.get("name", ""),
                        arguments=arguments if arguments_valid else {},
                        arguments_valid=arguments_valid,
                        arguments_error=(
                            None if arguments_valid else "tool arguments must be a JSON object"
                        ),
                    )
                )

        return AiResponse(
            content="\n".join(text_parts),
            thought_trace="\n".join(thought_parts),
            tool_calls=tool_calls,
            finish_reason=data.get("stop_reason", "end_turn"),
            usage=data.get("usage", {}),
        )

    async def complete_stream(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[AiStreamChunk]:
        url = f"{self._base_url}/messages"
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 4096,
            "messages": self._format_messages(messages),
            "stream": True,
        }
        if system_prompt:
            payload["system"] = system_prompt
        formatted_tools = self._format_tools(tools)
        if formatted_tools:
            payload["tools"] = formatted_tools

        current_tool_index = 0
        current_tool_id = ""
        current_tool_name = ""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                resp.raise_for_status()
                event_name = ""
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    try:
                        data = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    event_type = data.get("type") or event_name
                    if event_type == "content_block_start":
                        block = data.get("content_block", {}) or {}
                        if block.get("type") == "tool_use":
                            current_tool_index = int(data.get("index", 0) or 0)
                            current_tool_id = str(block.get("id", "") or "")
                            current_tool_name = str(block.get("name", "") or "")
                            yield AiStreamChunk(
                                tool_call_deltas=[
                                    ToolCallDelta(
                                        index=current_tool_index,
                                        call_id=current_tool_id,
                                        tool_name=current_tool_name,
                                    )
                                ]
                            )
                    elif event_type == "content_block_delta":
                        delta = data.get("delta", {}) or {}
                        delta_type = delta.get("type")
                        if delta_type == "text_delta":
                            yield AiStreamChunk(delta_content=delta.get("text", "") or "")
                        elif delta_type == "thinking_delta":
                            yield AiStreamChunk(thought_content=delta.get("thinking", "") or "")
                        elif delta_type == "input_json_delta":
                            yield AiStreamChunk(
                                tool_call_deltas=[
                                    ToolCallDelta(
                                        index=current_tool_index,
                                        call_id=current_tool_id,
                                        tool_name=current_tool_name,
                                        arguments_fragment=delta.get("partial_json", "") or "",
                                    )
                                ]
                            )
                    elif event_type == "message_delta":
                        usage_data = data.get("usage", {}) or {}
                        usage = {
                            str(key): int(value)
                            for key, value in usage_data.items()
                            if isinstance(value, (int, float))
                        }
                        yield AiStreamChunk(
                            finish_reason=(data.get("delta", {}) or {}).get("stop_reason"),
                            usage=usage,
                        )
                    elif event_type == "message_stop":
                        yield AiStreamChunk(finish_reason="stop")
