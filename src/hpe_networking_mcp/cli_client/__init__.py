"""Standalone multi-MCP client core for the HPE Networking MCP CLI.

This package is the transport/UI-independent foundation used by one-shot
commands and the interactive REPL. It intentionally does not depend on
Copilot CLI or any hosted AI provider.
"""

from __future__ import annotations

__all__ = [
    "banner",
    "commands",
    "config",
    "documents",
    "output",
    "repl_input",
    "safety",
    "sessions",
    "skills",
    "tui",
]
