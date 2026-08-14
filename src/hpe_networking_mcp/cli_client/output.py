"""Bounded Rich / JSON output helpers for the CLI client."""

from __future__ import annotations

import json
import sys
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

console = Console(stderr=False)
err_console = Console(stderr=True)


def emit_json(payload: Any, *, stream: Any = None) -> None:
    out = stream or sys.stdout
    json.dump(payload, out, indent=2, sort_keys=True, default=str)
    out.write("\n")


def tool_result_to_text(result: Any, *, max_chars: int = 12_000) -> str:
    """Flatten an MCP CallToolResult (or similar) into plain text."""
    if result is None:
        return ""
    # Prefer structured content when present.
    structured = getattr(result, "structuredContent", None) or getattr(
        result, "structured_content", None
    )
    if structured is not None:
        try:
            text = json.dumps(structured, indent=2, sort_keys=True, default=str)
        except TypeError:
            text = str(structured)
    else:
        parts: list[str] = []
        content = getattr(result, "content", None)
        if content:
            for block in content:
                if isinstance(block, dict):
                    btype = block.get("type")
                    raw_text = block.get("text")
                else:
                    btype = getattr(block, "type", None)
                    raw_text = getattr(block, "text", None)
                if btype == "text" or raw_text is not None:
                    parts.append(str(raw_text if raw_text is not None else block))
                else:
                    parts.append(str(block))
        else:
            parts.append(str(result))
        text = "\n".join(parts)
    if getattr(result, "isError", False) or getattr(result, "is_error", False):
        text = f"[error]\n{text}"
    if len(text) > max_chars:
        text = text[: max_chars - 20] + "\n… [truncated]"
    return text


def print_tools_table(tools: dict[str, Any], *, query: str | None = None) -> None:
    table = Table(title="MCP tools", show_lines=False)
    table.add_column("Name", style="cyan", no_wrap=False)
    table.add_column("Read?", justify="center")
    table.add_column("Description")
    from hpe_networking_mcp.cli_client.safety import tool_is_read_only

    q = (query or "").lower().strip()
    rows = 0
    for name in sorted(tools):
        tool = tools[name]
        desc = (getattr(tool, "description", None) or "")[:120]
        if q and q not in name.lower() and q not in desc.lower():
            continue
        ro = tool_is_read_only(tool)
        table.add_row(name, "yes" if ro else "no", desc)
        rows += 1
    if rows == 0:
        console.print("[dim]No tools matched.[/]")
        return
    console.print(table)


def print_skills_table(skills: list[Any]) -> None:
    table = Table(title="Skills")
    table.add_column("Name", style="cyan")
    table.add_column("Description")
    table.add_column("Path", style="dim")
    for s in skills:
        table.add_row(s.name, (s.description or "")[:100], str(s.path))
    console.print(table)


def print_docs_table(docs: list[Any]) -> None:
    table = Table(title="Personal documents")
    table.add_column("ID", style="cyan")
    table.add_column("Collection")
    table.add_column("Title")
    table.add_column("Source", style="dim")
    for d in docs:
        table.add_row(d.id, d.collection, d.title or "", d.source_uri[:80])
    console.print(table)


def print_error(msg: str, *, json_mode: bool = False, code: str = "error") -> None:
    if json_mode:
        emit_json({"ok": False, "error": {"code": code, "message": msg}})
    else:
        err_console.print(Text(msg, style="bold red"))


def print_ok(payload: dict[str, Any], *, json_mode: bool = False, text: str | None = None) -> None:
    if json_mode:
        emit_json({"ok": True, **payload})
    elif text is not None:
        console.print(text)
    else:
        console.print_json(data=payload)
