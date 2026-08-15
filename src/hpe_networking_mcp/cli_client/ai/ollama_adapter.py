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
        model: str = "llama3.2:latest",
        timeout: float = 120.0,
    ) -> None:
        self._base_url = (base_url or os.getenv("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
        self._model = model or os.getenv("OLLAMA_MODEL", "llama3.2:latest")
        self._timeout = timeout

    @property
    def name(self) -> str:
        return f"ollama:{self._model}"

    def _format_messages(self, messages: list[ChatMessage], system_prompt: str | None) -> list[dict[str, Any]]:
        formatted: list[dict[str, Any]] = []
        if system_prompt:
            formatted.append({"role": "system", "content": system_prompt})
        for m in messages:
            formatted.append({"role": m.role.value, "content": m.content})
        return formatted

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

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

        msg = data.get("message", {})
        content = msg.get("content") or ""

        return AiResponse(
            content=content,
            thought_trace="",
            tool_calls=[],
            finish_reason=data.get("done_reason", "stop"),
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
                        yield AiStreamChunk(
                            delta_content=delta,
                            finish_reason="stop" if done else None,
                        )
                    except Exception:
                        continue
