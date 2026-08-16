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
    # Streaming adapters may receive malformed/incomplete argument fragments.
    # Keep that state so the service can refuse dispatch rather than treating
    # bad input as an empty object.
    arguments_valid: bool = True
    arguments_error: str | None = None


@dataclass
class ToolCallDelta:
    """A partial function call received from a streaming provider."""

    index: int = 0
    call_id: str = ""
    tool_name: str = ""
    arguments_fragment: str = ""


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
    # Provider thinking is adapter-internal; ReasoningService never forwards it.
    thought_content: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tool_call_deltas: list[ToolCallDelta] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class AiResponse:
    content: str
    # Kept for adapter/legacy compatibility; not a client-facing guarantee.
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

    @property
    def provider(self) -> str:
        """Stable provider identifier used by the shared reasoning service."""
        return self.name.split(":", 1)[0]

    @property
    def model(self) -> str:
        """Configured model identifier, or the backend name for rule engines."""
        return self.name.split(":", 1)[1] if ":" in self.name else self.name

    @property
    def metadata(self) -> dict[str, str]:
        """Provider/model metadata safe to expose to clients and UIs."""
        return {
            "provider": self.provider,
            "model": self.model,
            "backend": self.name,
        }

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
