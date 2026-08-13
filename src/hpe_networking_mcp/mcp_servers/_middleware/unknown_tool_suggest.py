"""Unknown-tool recovery middleware.

When an MCP client guesses a tool name, MCPServer raises a bare
``ToolError("Unknown tool: ...")``. Returning a structured hint helps small
models recover by using the router's discovery flow instead of concluding the
capability is unavailable.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any


_UNKNOWN_RE = re.compile(r"Unknown tool: (?P<name>[A-Za-z0-9_.:-]+)")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {"get", "list", "set", "find", "create", "delete", "update", "tool"}


def _tokens(value: str) -> set[str]:
    return {token for token in _TOKEN_RE.findall(value.lower().replace("_", " ")) if token not in _STOPWORDS}


def _fallback_suggestions(name: str, available_tools: Iterable[str], limit: int) -> list[dict[str, Any]]:
    wanted = _tokens(name)
    if not wanted:
        return []
    scored: list[tuple[float, str]] = []
    for tool_name in available_tools:
        candidate = _tokens(tool_name)
        if not candidate:
            continue
        overlap = wanted & candidate
        if not overlap:
            continue
        score = len(overlap) / max(len(wanted | candidate), 1)
        scored.append((score, tool_name))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [{"name": tool_name, "score": round(score, 4)} for score, tool_name in scored[:limit]]


class UnknownToolSuggestMiddleware:
    """Return structured suggestions when a caller invokes an unknown tool."""

    def __init__(
        self,
        available_tools: Mapping[str, Any] | Callable[[], Iterable[str]],
        *,
        suggestion_provider: Callable[[str, int], list[dict[str, Any]]] | None = None,
        limit: int = 3,
    ) -> None:
        self._available_tools = available_tools
        self._suggestion_provider = suggestion_provider
        self._limit = max(1, min(limit, 10))

    def _tool_names(self) -> Iterable[str]:
        tools = self._available_tools() if callable(self._available_tools) else self._available_tools
        return tools.keys() if isinstance(tools, Mapping) else tools

    def before_call(self, name: str, arguments: dict[str, Any]) -> None:
        return None

    def after_call(self, name: str, arguments: dict[str, Any], result: Any) -> None:
        return None

    def on_error(self, name: str, arguments: dict[str, Any], exc: BaseException) -> dict[str, Any] | None:
        match = _UNKNOWN_RE.search(str(exc))
        if not match:
            return None
        requested = match.group("name")
        suggestions = (
            self._suggestion_provider(requested, self._limit)
            if self._suggestion_provider is not None
            else _fallback_suggestions(requested, self._tool_names(), self._limit)
        )
        return {
            "error": f"Unknown tool: {requested}",
            "hint": (
                "Use find_tool to discover available tools, then invoke_read_tool "
                "for read-only results or invoke_tool for intentional writes."
            ),
            "suggestions": suggestions,
        }
