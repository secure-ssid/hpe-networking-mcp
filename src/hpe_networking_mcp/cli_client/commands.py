"""Shared command handlers used by one-shot CLI and the REPL."""

from __future__ import annotations

import json
from typing import Any

from hpe_networking_mcp.cli_client.config import ClientConfig, ServerProfile
from hpe_networking_mcp.cli_client.documents import DocumentStore
from hpe_networking_mcp.cli_client.output import (
    console,
    print_docs_table,
    print_error,
    print_ok,
    print_skills_table,
    print_tools_table,
    tool_result_to_text,
)
from hpe_networking_mcp.cli_client.safety import SafetyPolicy
from hpe_networking_mcp.cli_client.sessions import SessionManager
from hpe_networking_mcp.cli_client.skills import discover_skills, get_skill


async def ensure_connected(
    mgr: SessionManager,
    cfg: ClientConfig,
    *,
    profile: str | None = None,
) -> ServerProfile:
    prof = cfg.get(profile)
    await mgr.connect(prof)
    return prof


async def cmd_tools_list(
    mgr: SessionManager,
    *,
    query: str | None = None,
    json_mode: bool = False,
) -> int:
    tools = mgr.tools
    if json_mode:
        from hpe_networking_mcp.cli_client.safety import tool_is_read_only

        payload = []
        q = (query or "").lower().strip()
        for name, tool in sorted(tools.items()):
            desc = getattr(tool, "description", None) or ""
            if q and q not in name.lower() and q not in desc.lower():
                continue
            payload.append(
                {
                    "name": name,
                    "description": desc,
                    "read_only": tool_is_read_only(tool),
                }
            )
        print_ok({"tools": payload, "count": len(payload)}, json_mode=True)
        return 0
    print_tools_table(tools, query=query)
    return 0


async def cmd_invoke(
    mgr: SessionManager,
    safety: SafetyPolicy,
    *,
    tool: str,
    args: dict[str, Any] | None = None,
    allow_writes: bool = False,
    json_mode: bool = False,
    force_write: bool = False,
) -> int:
    try:
        resolved = mgr.resolve_tool_name(tool)
    except KeyError as exc:
        print_error(str(exc), json_mode=json_mode, code="unknown_tool")
        return 2

    tool_obj = mgr.tools[resolved]
    decision = safety.check(tool_obj, force_write=force_write or allow_writes)
    if not decision.allowed:
        print_error(decision.reason, json_mode=json_mode, code="write_blocked")
        return 3

    try:
        result = await mgr.call_tool(resolved, args or {})
    except Exception as exc:  # noqa: BLE001 - surface to operator
        print_error(f"{type(exc).__name__}: {exc}", json_mode=json_mode, code="call_failed")
        return 1

    text = tool_result_to_text(result)
    is_err = bool(getattr(result, "isError", False) or getattr(result, "is_error", False))
    if json_mode:
        print_ok(
            {
                "tool": resolved,
                "is_error": is_err,
                "text": text,
                "read_only": decision.is_read_only,
            },
            json_mode=True,
        )
    else:
        console.print(f"[dim]→ {resolved}[/]")
        console.print(text)
    return 1 if is_err else 0


def _has_tool(mgr: SessionManager, bare_name: str) -> bool:
    try:
        mgr.resolve_tool_name(bare_name)
        return True
    except KeyError:
        return False


async def _invoke_backend_read(
    mgr: SessionManager,
    safety: SafetyPolicy,
    *,
    backend_tool: str,
    arguments: dict[str, Any],
    json_mode: bool = False,
) -> int:
    """Call a backend tool via the low-token router when present.

    Prefer ``invoke_read_tool(name=..., arguments=...)`` (router minimal mode).
    Fall back to a direct tool name when the server exposes the full catalog.
    """
    if _has_tool(mgr, "invoke_read_tool"):
        return await cmd_invoke(
            mgr,
            safety,
            tool="invoke_read_tool",
            args={"name": backend_tool, "arguments": arguments},
            json_mode=json_mode,
        )
    return await cmd_invoke(
        mgr,
        safety,
        tool=backend_tool,
        args=arguments,
        json_mode=json_mode,
    )


async def cmd_rag_ask(
    mgr: SessionManager,
    safety: SafetyPolicy,
    *,
    question: str,
    source: str | None = None,
    json_mode: bool = False,
) -> int:
    args: dict[str, Any] = {"question": question}
    if source:
        args["source"] = source
    return await _invoke_backend_read(
        mgr,
        safety,
        backend_tool="ask_docs",
        arguments=args,
        json_mode=json_mode,
    )


async def cmd_api_lookup(
    mgr: SessionManager,
    safety: SafetyPolicy,
    *,
    query: str,
    json_mode: bool = False,
) -> int:
    return await _invoke_backend_read(
        mgr,
        safety,
        backend_tool="lookup_api",
        arguments={"query": query},
        json_mode=json_mode,
    )


async def cmd_find_tool_server(
    mgr: SessionManager,
    safety: SafetyPolicy,
    *,
    query: str,
    json_mode: bool = False,
) -> int:
    """Call the router's find_tool when present; else local name filter."""
    if not _has_tool(mgr, "find_tool"):
        return await cmd_tools_list(mgr, query=query, json_mode=json_mode)
    return await cmd_invoke(
        mgr,
        safety,
        tool="find_tool",
        args={"query": query},
        json_mode=json_mode,
    )


def cmd_skills_list(*, json_mode: bool = False) -> int:
    skills = discover_skills()
    if json_mode:
        print_ok(
            {
                "skills": [
                    {"name": s.name, "description": s.description, "path": str(s.path)}
                    for s in skills
                ]
            },
            json_mode=True,
        )
        return 0
    print_skills_table(skills)
    return 0


def cmd_skills_show(name: str, *, json_mode: bool = False) -> int:
    try:
        skill = get_skill(name)
    except KeyError as exc:
        print_error(str(exc), json_mode=json_mode, code="unknown_skill")
        return 2
    if json_mode:
        print_ok(
            {
                "name": skill.name,
                "description": skill.description,
                "path": str(skill.path),
                "body": skill.body,
                "meta": skill.meta,
            },
            json_mode=True,
        )
        return 0
    console.print(f"[bold cyan]{skill.name}[/]  [dim]{skill.path}[/]")
    if skill.description:
        console.print(skill.description)
    console.print()
    console.print(skill.body)
    return 0


def cmd_docs_list(*, collection: str | None = None, json_mode: bool = False) -> int:
    store = DocumentStore()
    docs = store.list(collection)
    if json_mode:
        print_ok({"documents": [d.to_dict() for d in docs]}, json_mode=True)
        return 0
    print_docs_table(docs)
    return 0


def cmd_docs_add(
    source: str,
    *,
    collection: str = "personal",
    title: str | None = None,
    json_mode: bool = False,
) -> int:
    store = DocumentStore()
    try:
        if source.startswith("http://") or source.startswith("https://"):
            rec = store.add_uri_record(source, collection=collection, title=title)
        else:
            rec = store.add_file(source, collection=collection, title=title)
    except (OSError, ValueError) as exc:
        print_error(str(exc), json_mode=json_mode, code="docs_add_failed")
        return 1
    print_ok(
        {"document": rec.to_dict()},
        json_mode=json_mode,
        text=f"added {rec.id} → {rec.collection}",
    )
    return 0


def cmd_docs_search(query: str, *, collection: str | None = None, json_mode: bool = False) -> int:
    store = DocumentStore()
    hits = store.search(query, collection=collection)
    if json_mode:
        print_ok({"documents": [d.to_dict() for d in hits], "count": len(hits)}, json_mode=True)
        return 0
    print_docs_table(hits)
    return 0


def parse_args_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    raw = raw.strip()
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("--args must be a JSON object")
    return data
