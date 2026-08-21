"""Bounded Rich / JSON output helpers for the CLI client."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console(stderr=False)
err_console = Console(stderr=True)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+\n")
_REDACT_KV_RE = re.compile(
    r"(?i)(password|passphrase|token|secret|client_secret|api[-_]?key|authorization)"
    r"(\s*[:=\s]\s*[\"']?)([^\"'\s,;]+)([\"']?)"
)
# Basic-auth style userinfo embedded in a URL: scheme://user:password@host
_URL_USERINFO_RE = re.compile(r"(?i)(https?://[^/\s\"'@]*?:)([^/\s\"'@]+)(@)")


def redact_sensitive_text(text: str) -> str:
    """Redact passwords, tokens, secrets, and URL-embedded credentials."""
    if not text:
        return ""
    text = _URL_USERINFO_RE.sub(r"\1***REDACTED***\3", text)
    return _REDACT_KV_RE.sub(r"\1\2***REDACTED***\4", text)


def format_duration(seconds: float) -> str:
    """Render an elapsed-time span as a short human string (e.g. ``3m12s``)."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def format_usage(usage: dict[str, int]) -> str:
    """Render token usage dict as a compact human string."""
    if not usage:
        return "none"
    if "total_tokens" in usage:
        prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0))
        comp = usage.get("completion_tokens", usage.get("output_tokens", 0))
        tot = usage.get("total_tokens", 0)
        return f"prompt={prompt}, completion={comp}, total={tot}"
    if "input_tokens" in usage or "output_tokens" in usage:
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        return f"input={inp}, output={out}, total={inp + out}"
    parts = [f"{k}={v}" for k, v in usage.items()]
    return ", ".join(parts)


def emit_json(payload: Any, *, stream: Any = None) -> None:
    out = stream or sys.stdout
    json.dump(payload, out, indent=2, sort_keys=True, default=str)
    out.write("\n")


def _strip_html(text: str) -> str:
    cleaned = _HTML_TAG_RE.sub("", text)
    cleaned = cleaned.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&")
    cleaned = cleaned.replace("&nbsp;", " ")
    return _WS_RE.sub("\n", cleaned).strip()


def _try_parse_json(text: str) -> Any | None:
    s = text.strip()
    if not s or s[0] not in "{[":
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def _unwrap_payload(data: Any) -> Any:
    """Peel common router/tool envelopes down to the useful body."""
    if not isinstance(data, dict):
        return data
    # Envelope: {"ok": true, "result": {...}} or {"data": {...}}
    for key in ("result", "data", "payload", "body"):
        inner = data.get(key)
        if isinstance(inner, dict) and (
            "answer" in inner or "citations" in inner or "items" in inner
        ):
            return inner
    return data


def format_rag_payload(data: dict[str, Any]) -> str | None:
    """Return human text for ask_docs / search_docs shaped payloads."""
    answer = data.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        # search_docs style: hits/results list
        hits = data.get("hits") or data.get("results") or data.get("documents")
        if isinstance(hits, list) and hits:
            lines = ["## Search results", ""]
            for i, hit in enumerate(hits[:12], 1):
                if not isinstance(hit, dict):
                    lines.append(f"{i}. {hit}")
                    continue
                path = hit.get("file_path") or hit.get("path") or hit.get("title") or "?"
                src = hit.get("source") or ""
                score = hit.get("score")
                snippet = hit.get("text") or hit.get("snippet") or hit.get("content") or ""
                meta = f" ({src})" if src else ""
                sc = f"  score={score}" if score is not None else ""
                lines.append(f"{i}. `{path}`{meta}{sc}")
                if snippet:
                    lines.append(f"   {_strip_html(str(snippet))[:240]}")
            return "\n".join(lines)
        return None

    answer_text = _strip_html(answer)
    lines = [answer_text, ""]
    citations = data.get("citations") or data.get("sources") or []
    if isinstance(citations, list) and citations:
        lines.append("### Sources")
        for i, cite in enumerate(citations[:8], 1):
            if isinstance(cite, dict):
                path = cite.get("file_path") or cite.get("path") or cite.get("title") or "?"
                src = cite.get("source") or ""
                score = cite.get("score")
                bit = f"{i}. `{path}`"
                if src:
                    bit += f"  ·  {src}"
                if score is not None:
                    try:
                        bit += f"  ·  {float(score):.2f}"
                    except (TypeError, ValueError):
                        bit += f"  ·  {score}"
                lines.append(bit)
            else:
                lines.append(f"{i}. {cite}")
    mode = data.get("mode")
    if mode:
        lines.append("")
        lines.append(f"_mode: {mode}_")
    return "\n".join(lines).strip()


def format_lookup_payload(data: dict[str, Any] | list[Any]) -> str | None:
    """Pretty OpenAPI lookup results when present."""
    items: list[Any]
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("results") or data.get("items") or data.get("endpoints") or []
        if not items and any(k in data for k in ("method", "path", "operation_id")):
            items = [data]
    else:
        return None
    if not isinstance(items, list) or not items:
        return None
    lines = ["## API matches", ""]
    for i, item in enumerate(items[:15], 1):
        if not isinstance(item, dict):
            lines.append(f"{i}. {item}")
            continue
        method = (item.get("method") or item.get("http_method") or "").upper()
        path = item.get("path") or item.get("url") or ""
        op = item.get("operation_id") or item.get("operationId") or ""
        summary = item.get("summary") or item.get("description") or ""
        file_path = item.get("file_path") or item.get("spec") or ""
        head = f"{i}. **{method} {path}**".strip()
        if op:
            head += f"  `{op}`"
        lines.append(head)
        if summary:
            lines.append(f"   {_strip_html(str(summary))[:200]}")
        if file_path:
            lines.append(f"   _{file_path}_")
    return "\n".join(lines)


def format_diagram_result(data: dict[str, Any]) -> str | None:
    """Format network diagram export results nicely."""
    approach = data.get("approach") or ""
    written = data.get("written") or []
    if "diagram" not in approach and not written and "export" not in data:
        return None

    lines = ["## Network Diagram Export", ""]
    if approach:
        lines.append(f"- **Approach:** `{approach}`")
    saved = data.get("saved")
    if saved is not None:
        lines.append(f"- **Saved to disk:** {'Yes' if saved else 'No (inline only)'}")
    out_dir = data.get("output_dir")
    if out_dir:
        lines.append(f"- **Output folder:** `{out_dir}`")

    if written and isinstance(written, list):
        lines.append("")
        lines.append("### Generated Files")
        for item in written:
            if isinstance(item, dict):
                p = item.get("path") or item.get("filename") or "?"
                b = item.get("bytes")
                size_str = f" ({b:,} bytes)" if isinstance(b, (int, float)) else ""
                lines.append(f"- `{p}`{size_str}")
            else:
                lines.append(f"- `{item}`")

    export = data.get("export")
    if isinstance(export, dict):
        editable = export.get("editable_in") or []
        if editable and isinstance(editable, list):
            lines.append("")
            lines.append(f"**Editable in:** {', '.join(editable)}")
        preview = export.get("preview_html") or export.get("preview_url")
        if preview:
            lines.append(f"- **Preview available:** `{preview}`")

    lines.append("")
    lines.append(
        "💡 *Tip: Open `.drawio` files directly in https://app.diagrams.net "
        "or the VS Code Draw.io extension.*"
    )
    return "\n".join(lines)


def format_tool_schema(name: str, tool_obj: Any) -> str:
    """Format full tool schema into a human-readable summary table."""
    from hpe_networking_mcp.cli_client.safety import tool_is_read_only

    if isinstance(tool_obj, dict):
        desc = tool_obj.get("description") or "(no description)"
        schema = (
            tool_obj.get("schema")
            or tool_obj.get("inputSchema")
            or tool_obj.get("input_schema")
            or {}
        )
        ro = bool(tool_obj.get("read_only", tool_obj.get("capability") == "read"))
    else:
        desc = getattr(tool_obj, "description", None) or "(no description)"
        raw_schema = (
            getattr(tool_obj, "inputSchema", None) or getattr(tool_obj, "input_schema", None) or {}
        )
        if hasattr(raw_schema, "model_dump"):
            schema = raw_schema.model_dump()
        elif hasattr(raw_schema, "properties") or hasattr(raw_schema, "__dict__"):
            schema = {
                "properties": getattr(raw_schema, "properties", {}),
                "required": getattr(raw_schema, "required", []),
            }
        elif isinstance(raw_schema, dict):
            schema = raw_schema
        else:
            schema = {}
        ro = tool_is_read_only(tool_obj)

    lines = [
        f"## Tool: `{name}`",
        "",
        f"- **Mode:** {'Read-Only' if ro else 'Write / Destructive'}",
        f"- **Description:** {desc}",
        "",
        "### Parameters",
        "",
    ]

    properties = schema.get("properties") if isinstance(schema, dict) else {}
    required = set(schema.get("required") or []) if isinstance(schema, dict) else set()

    if not properties:
        lines.append("*(This tool takes no arguments)*")
    else:
        for p_name, p_info in properties.items():
            req_tag = "**Required**" if p_name in required else "*optional*"
            if isinstance(p_info, dict):
                p_type = p_info.get("type", "any")
                p_desc = p_info.get("description", "")
                enum_vals = p_info.get("enum")
            else:
                p_type = getattr(p_info, "type", "any")
                p_desc = getattr(p_info, "description", "")
                enum_vals = getattr(p_info, "enum", None)
            lines.append(f"- **`{p_name}`** ({p_type}) — {req_tag}")
            if p_desc:
                lines.append(f"  {p_desc}")
            if enum_vals:
                lines.append(f"  *Accepted values:* `{'`, `'.join(map(str, enum_vals))}`")

    return "\n".join(lines)


def prettify_tool_text(text: str) -> str:
    """If tool text is JSON with known shapes, render a human view."""
    data = _try_parse_json(text)
    if data is None:
        return text
    data = _unwrap_payload(data)
    if isinstance(data, dict):
        diag = format_diagram_result(data)
        if diag:
            return diag
        rag = format_rag_payload(data)
        if rag:
            return rag
        lookup = format_lookup_payload(data)
        if lookup:
            return lookup
        # find_tool style list under tools/results
        tools = data.get("tools") or data.get("results")
        if isinstance(tools, list) and tools and isinstance(tools[0], dict):
            lines = ["## Tools", ""]
            for t in tools[:20]:
                name = t.get("name") or "?"
                desc = (t.get("description") or "")[:120]
                match = t.get("match") or ""
                lines.append(f"- **{name}**" + (f"  _{match}_" if match else ""))
                if desc:
                    lines.append(f"  {desc}")
            return "\n".join(lines)
    if isinstance(data, list):
        lookup = format_lookup_payload(data)
        if lookup:
            return lookup
    try:
        return json.dumps(data, indent=2, sort_keys=True, default=str)
    except TypeError:
        return text


def tool_result_to_text(result: Any, *, max_chars: int = 12_000) -> str:
    """Flatten an MCP CallToolResult (or similar) into plain text."""
    if result is None:
        return ""
    # Prefer structured content when present.
    structured = getattr(result, "structuredContent", None) or getattr(
        result, "structured_content", None
    )
    if structured is not None:
        if isinstance(structured, (dict, list)):
            pretty = prettify_tool_text(json.dumps(structured))
            text = pretty
        else:
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
        text = prettify_tool_text(text)
    if getattr(result, "isError", False) or getattr(result, "is_error", False):
        text = f"[error]\n{text}"
    if len(text) > max_chars:
        text = text[: max_chars - 20] + "\n… [truncated]"
    return text


def print_tool_result(text: str, *, tool_name: str | None = None) -> None:
    """Render tool output: markdown for RAG, plain/json otherwise."""
    if tool_name:
        console.print(f"[dim]→ {tool_name}[/]")
    stripped = text.lstrip()
    # Heuristic: markdown-ish answer body
    if stripped.startswith("#") or "\n### " in text or "\n## " in text:
        console.print(Markdown(text))
        return
    if stripped.startswith("{") or stripped.startswith("["):
        console.print_json(text) if _try_parse_json(text) is not None else console.print(text)
        return
    # Prose answers from RAG without headings
    if len(text) > 120 and "\n### Sources" in text:
        console.print(Markdown(text))
        return
    if len(text) > 200 and not stripped.startswith("["):
        console.print(
            Panel(Markdown(text), border_style="cyan", title="answer", title_align="left")
        )
        return
    console.print(text)


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
