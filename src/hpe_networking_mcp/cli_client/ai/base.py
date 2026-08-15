"""Base interfaces and data structures for AI reasoning backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ToolCallRequest:
    call_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass
class ChatMessage:
    role: MessageRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)


@dataclass
class AiStreamChunk:
    delta_content: str = ""
    thought_content: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str | None = None


@dataclass
class AiResponse:
    content: str
    thought_trace: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)


class AiBackend(ABC):
    """Abstract interface for model reasoning backends (cloud, local, or heuristic)."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider/engine identifier."""
        ...

    @abstractmethod
    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
    ) -> AiResponse:
        """Generate a complete response."""
        ...

    @abstractmethod
    async def complete_stream(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[AiStreamChunk]:
        """Stream response chunks and tool call requests."""
        ...
