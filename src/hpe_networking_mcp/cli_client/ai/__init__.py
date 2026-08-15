"""AI Reasoning & Multi-Turn Execution Adapters."""

from __future__ import annotations

from hpe_networking_mcp.cli_client.ai.agent_loop import AgentLoopStep, AgentReasoningLoop
from hpe_networking_mcp.cli_client.ai.anthropic_adapter import AnthropicAdapter
from hpe_networking_mcp.cli_client.ai.base import (
    AiBackend,
    AiResponse,
    AiStreamChunk,
    ChatMessage,
    MessageRole,
    ToolCallRequest,
)
from hpe_networking_mcp.cli_client.ai.heuristic_engine import HeuristicReasoningEngine
from hpe_networking_mcp.cli_client.ai.ollama_adapter import OllamaAdapter
from hpe_networking_mcp.cli_client.ai.openai_adapter import OpenAiAdapter


def get_ai_backend(provider: str = "heuristic", model: str | None = None) -> AiBackend:
    """Factory for instantiating AI reasoning backends."""
    p = provider.lower().strip()
    if p in ("heuristic", "offline", "local-rules"):
        return HeuristicReasoningEngine()
    if p in ("openai", "azure", "gpt"):
        return OpenAiAdapter(model=model or "gpt-4o")
    if p in ("anthropic", "claude"):
        return AnthropicAdapter(model=model or "claude-3-7-sonnet-20250219")
    if p in ("ollama", "local"):
        return OllamaAdapter(model=model or "llama3.2:latest")
    return HeuristicReasoningEngine()


__all__ = [
    "AgentLoopStep",
    "AgentReasoningLoop",
    "AiBackend",
    "AiResponse",
    "AiStreamChunk",
    "AnthropicAdapter",
    "ChatMessage",
    "HeuristicReasoningEngine",
    "MessageRole",
    "OllamaAdapter",
    "OpenAiAdapter",
    "ToolCallRequest",
    "get_ai_backend",
]
