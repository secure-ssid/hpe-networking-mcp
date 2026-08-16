"""AI Reasoning & Multi-Turn Execution Adapters."""

from __future__ import annotations

import os
from typing import Any

from hpe_networking_mcp.cli_client.ai.agent_loop import AgentLoopStep, AgentReasoningLoop
from hpe_networking_mcp.cli_client.ai.anthropic_adapter import AnthropicAdapter
from hpe_networking_mcp.cli_client.ai.base import (
    AiBackend,
    AiResponse,
    AiStreamChunk,
    ChatMessage,
    MessageRole,
    ToolCallDelta,
    ToolCallRequest,
)
from hpe_networking_mcp.cli_client.ai.heuristic_engine import HeuristicReasoningEngine
from hpe_networking_mcp.cli_client.ai.ollama_adapter import OllamaAdapter
from hpe_networking_mcp.cli_client.ai.openai_adapter import OpenAiAdapter
from hpe_networking_mcp.cli_client.ai.service import (
    DEFAULT_SYSTEM_PROMPT,
    ConversationMemory,
    ReasoningEvent,
    ReasoningResult,
    ReasoningService,
    bound_tool_result,
)


def get_ai_backend(
    provider: str | None = None,
    model: str | None = None,
    *,
    config: Any | None = None,
) -> AiBackend:
    """Instantiate a client-side AI backend from explicit/env selection."""
    if config is not None:
        provider = (
            provider or getattr(config, "ai_provider", None) or getattr(config, "provider", None)
        )
        model = model or getattr(config, "ai_model", None) or getattr(config, "model", None)
    p = (
        (
            provider
            or os.environ.get("HPE_MCP_AI_PROVIDER")
            or os.environ.get("HPE_MCP_AI_BACKEND")
            or os.environ.get("HPE_MCP_PROVIDER")
            or "heuristic"
        )
        .lower()
        .strip()
    )
    selected_model = model or os.environ.get("HPE_MCP_AI_MODEL") or os.environ.get("HPE_MCP_MODEL")
    if p in ("heuristic", "offline", "local-rules"):
        return HeuristicReasoningEngine()
    if p in ("openai", "azure", "gpt"):
        return OpenAiAdapter(model=selected_model)
    if p in ("anthropic", "claude"):
        return AnthropicAdapter(model=selected_model)
    if p in ("ollama", "local"):
        return OllamaAdapter(model=selected_model)
    raise ValueError(
        f"unsupported AI provider {p!r}; choose heuristic, openai, anthropic, or ollama"
    )


__all__ = [
    "AgentLoopStep",
    "AgentReasoningLoop",
    "AiBackend",
    "AiResponse",
    "AiStreamChunk",
    "AnthropicAdapter",
    "ChatMessage",
    "ConversationMemory",
    "DEFAULT_SYSTEM_PROMPT",
    "HeuristicReasoningEngine",
    "MessageRole",
    "OllamaAdapter",
    "OpenAiAdapter",
    "ReasoningEvent",
    "ReasoningResult",
    "ReasoningService",
    "ToolCallDelta",
    "ToolCallRequest",
    "bound_tool_result",
    "get_ai_backend",
]
