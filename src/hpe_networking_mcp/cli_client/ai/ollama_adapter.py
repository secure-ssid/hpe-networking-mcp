"""Local Ollama LLM reasoning adapter."""

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
    ToolCallRequest,
)


class OllamaAdapter(AiBackend):
    """Adapter for local Ollama server (Llama 3, DeepSeek-R1, Qwen 2.5, Phi-4)."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._base_url = (base_url or os.getenv("OLLAMA_HOST", "http://localhost:11434")).rstrip(
            "/"
        )
        self._model = model or os.getenv("OLLAMA_MODEL", "llama3.2:latest")
        self._timeout = timeout

    @property
    def name(self) -> str:
        return f"ollama:{self._model}"

    def _format_messages(
        self, messages: list[ChatMessage], system_prompt: str | None
    ) -> list[dict[str, Any]]:
        formatted: list[dict[str, Any]] = []
        if system_prompt:
            formatted.append({"role": "system", "content": system_prompt})
        for m in messages:
            item: dict[str, Any] = {"role": m.role.value, "content": m.content}
            if m.tool_call_id:
                item["tool_call_id"] = m.tool_call_id
            if m.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.tool_name,
                            "arguments": call.arguments,
                        },
                    }
                    for call in m.tool_calls
                ]
            if m.role == MessageRole.TOOL and m.name:
                item["name"] = m.name
            formatted.append(item)
        return formatted

    def _format_tools(self, tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get(
                        "inputSchema",
                        tool.get("parameters", {"type": "object", "properties": {}}),
                    ),
                },
            }
            for tool in tools
            if tool.get("name")
        ]

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
    ) -> AiResponse:
        url = f"{self._base_url}/api/chat"
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": self._format_messages(messages, system_prompt),
            "stream": False,
        }
        formatted_tools = self._format_tools(tools)
        if formatted_tools:
            payload["tools"] = formatted_tools

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

        msg = data.get("message", {})
        content = msg.get("content") or ""
        tool_calls: list[ToolCallRequest] = []
        for raw_tool_call in msg.get("tool_calls", []) or []:
            function = raw_tool_call.get("function", {}) or {}
            raw_arguments = function.get("arguments", {})
            arguments_valid = True
            arguments_error: str | None = None
            if isinstance(raw_arguments, str):
                try:
                    raw_arguments = json.loads(raw_arguments)
                except json.JSONDecodeError as exc:
                    arguments_valid = False
                    arguments_error = f"invalid JSON arguments: {exc.msg}"
                    raw_arguments = {}
            if not isinstance(raw_arguments, dict):
                arguments_valid = False
                arguments_error = "tool arguments must be a JSON object"
                raw_arguments = {}
            tool_calls.append(
                ToolCallRequest(
                    call_id=str(raw_tool_call.get("id", "") or ""),
                    tool_name=str(function.get("name", "") or ""),
                    arguments=raw_arguments,
                    arguments_valid=arguments_valid,
                    arguments_error=arguments_error,
                )
            )

        usage: dict[str, int] = {}
        if "prompt_eval_count" in data or "eval_count" in data:
            prompt_toks = int(data.get("prompt_eval_count") or 0)
            comp_toks = int(data.get("eval_count") or 0)
            usage = {
                "prompt_tokens": prompt_toks,
                "completion_tokens": comp_toks,
                "total_tokens": prompt_toks + comp_toks,
            }

        return AiResponse(
            content=content,
            thought_trace="",
            tool_calls=tool_calls,
            finish_reason=data.get("done_reason", "stop"),
            usage=usage,
        )

    async def complete_stream(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[AiStreamChunk]:
        url = f"{self._base_url}/api/chat"
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": self._format_messages(messages, system_prompt),
            "stream": True,
        }
        formatted_tools = self._format_tools(tools)
        if formatted_tools:
            payload["tools"] = formatted_tools

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream("POST", url, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                        msg = chunk.get("message", {})
                        delta = msg.get("content", "")
                        done = chunk.get("done", False)
                        tool_calls: list[ToolCallRequest] = []
                        for raw_tool_call in msg.get("tool_calls", []) or []:
                            function = raw_tool_call.get("function", {}) or {}
                            raw_arguments = function.get("arguments", {})
                            arguments_valid = True
                            arguments_error: str | None = None
                            if isinstance(raw_arguments, str):
                                try:
                                    raw_arguments = json.loads(raw_arguments)
                                except json.JSONDecodeError as exc:
                                    arguments_valid = False
                                    arguments_error = f"invalid JSON arguments: {exc.msg}"
                                    raw_arguments = {}
                            if not isinstance(raw_arguments, dict):
                                arguments_valid = False
                                arguments_error = "tool arguments must be a JSON object"
                                raw_arguments = {}
                            tool_calls.append(
                                ToolCallRequest(
                                    call_id=str(raw_tool_call.get("id", "") or ""),
                                    tool_name=str(function.get("name", "") or ""),
                                    arguments=raw_arguments,
                                    arguments_valid=arguments_valid,
                                    arguments_error=arguments_error,
                                )
                            )
                        usage: dict[str, int] = {}
                        if done and ("prompt_eval_count" in chunk or "eval_count" in chunk):
                            prompt_toks = int(chunk.get("prompt_eval_count") or 0)
                            comp_toks = int(chunk.get("eval_count") or 0)
                            usage = {
                                "prompt_tokens": prompt_toks,
                                "completion_tokens": comp_toks,
                                "total_tokens": prompt_toks + comp_toks,
                            }
                        yield AiStreamChunk(
                            delta_content=delta,
                            tool_calls=tool_calls,
                            finish_reason="stop" if done else None,
                            usage=usage,
                        )
                    except Exception:
                        continue
