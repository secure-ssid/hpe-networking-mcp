"""Shared command handlers used by one-shot CLI and the REPL."""

from __future__ import annotations

import json
from typing import Any

from hpe_networking_mcp.cli_client.ai import get_ai_backend
from hpe_networking_mcp.cli_client.ai.agent_loop import AgentReasoningLoop
from hpe_networking_mcp.cli_client.config import ClientConfig, ServerProfile
from hpe_networking_mcp.cli_client.diagram_workflow import (
    DiagramPreferences,
    execute_diagram_export,
    parse_diagram_intent,
)
from hpe_networking_mcp.cli_client.documents import DocumentStore
from hpe_networking_mcp.cli_client.output import (
    console,
    format_tool_schema,
    print_docs_table,
    print_error,
    print_ok,
    print_skills_table,
    print_tool_result,
    print_tools_table,
    tool_result_to_text,
)
from hpe_networking_mcp.cli_client.safety import SafetyPolicy
from hpe_networking_mcp.cli_client.sessions import ConnectionState, SessionManager
from hpe_networking_mcp.cli_client.skills import discover_skills, get_skill
from hpe_networking_mcp.pipeline.reasoning import (
    create_troubleshooting_plan,
    format_architecture_recommendation_markdown,
    format_migration_plan_markdown,
    format_troubleshooting_report,
    plan_migration,
    synthesize_architecture,
)


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
        print_tool_result(text, tool_name=resolved)
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


def cmd_docs_search_content(query: str, *, collection: str | None = None, json_mode: bool = False) -> int:
    store = DocumentStore()
    hits = store.search_content(query, collection=collection)
    if json_mode:
        print_ok({"matches": hits, "count": len(hits)}, json_mode=True)
        return 0
    if not hits:
        console.print("[dim]No matching text found in stored documents.[/]")
        return 0
    for h in hits:
        console.print(f"[bold cyan]{h['title']}[/]  [dim]({h['collection']})[/]")
        console.print(f"  {h['snippet']}")
    return 0


def cmd_docs_ingest(
    folder: str,
    *,
    collection: str = "internal",
    json_mode: bool = False,
) -> int:
    """Extract/chunk/embed every supported file under ``folder`` into the
    local-only personal index (never the shared repo corpus, never uploaded).
    """
    from pathlib import Path

    from hpe_networking_mcp.cli_client.personal_ingest import ingest_folder

    path = Path(folder).expanduser()
    if not path.is_dir():
        print_error(f"not a directory: {path}", json_mode=json_mode, code="docs_ingest_failed")
        return 1

    def _progress(msg: str) -> None:
        if not json_mode:
            console.print(f"[dim]{msg}[/]")

    result = ingest_folder(path, collection=collection, progress=None if json_mode else _progress)
    payload = {
        "collection": collection,
        "files_seen": result.files_seen,
        "files_ingested": result.files_ingested,
        "files_skipped_unchanged": result.files_skipped_unchanged,
        "files_skipped_duplicate": result.files_skipped_duplicate,
        "files_skipped_unsupported": result.files_skipped_unsupported,
        "files_failed": result.files_failed,
        "chunks_written": result.chunks_written,
        "errors": result.errors or {},
    }
    if json_mode:
        print_ok(payload, json_mode=True)
        return 0
    console.print(
        f"[bold green]ingested[/] {result.files_ingested} file(s), "
        f"{result.chunks_written} chunk(s) → collection '{collection}'"
    )
    console.print(
        f"[dim]seen={result.files_seen} unchanged={result.files_skipped_unchanged} "
        f"duplicate={result.files_skipped_duplicate} "
        f"unsupported={result.files_skipped_unsupported} "
        f"failed={result.files_failed}[/]"
    )
    if result.errors:
        for path_str, err in result.errors.items():
            console.print(f"[red]failed:[/] {path_str}: {err}")
    return 0 if result.files_failed == 0 else 1


def cmd_docs_search_internal(
    query: str,
    *,
    collection: str = "internal",
    json_mode: bool = False,
) -> int:
    """Hybrid search over the local-only personal index built by ``docs ingest``."""
    from hpe_networking_mcp.cli_client.personal_ingest import search_personal

    hits = search_personal(query, collection=collection)
    if json_mode:
        print_ok({"hits": hits, "count": len(hits)}, json_mode=True)
        return 0
    if not hits:
        console.print(
            "[dim]No matches. Have you run `hpe-mcp docs ingest <folder>` yet?[/]"
        )
        return 0
    for h in hits:
        title = h.get("title") or h.get("file_path", "")
        console.print(f"[bold cyan]{title}[/]")
        text = str(h.get("text", ""))
        console.print(f"  {text[:240]}{'…' if len(text) > 240 else ''}")
    return 0


async def cmd_diagram(
    mgr: SessionManager,
    safety: SafetyPolicy,
    *,
    prompt: str = "",
    format: str | None = None,
    vendor: str | None = None,
    title: str | None = None,
    json_mode: bool = False,
) -> int:
    """Run guided diagram generation from prompt or parameters."""
    pref = parse_diagram_intent(prompt)
    if format:
        pref.format = format
    if vendor:
        pref.vendor = vendor
    if title:
        pref.title = title
    result = await execute_diagram_export(mgr, safety, pref)
    if json_mode:
        print_ok(result, json_mode=True)
        return 0 if result.get("ok") else 1
    if not result.get("ok"):
        print_error(result.get("error", "Diagram generation failed"), json_mode=False)
        return 1
    console.print(result.get("text", "Diagram generated"))
    return 0


async def cmd_tool_explore(
    mgr: SessionManager,
    safety: SafetyPolicy,
    *,
    tool_name: str,
    json_mode: bool = False,
) -> int:
    """Inspect tool metadata, schema, and annotations."""
    try:
        resolved = mgr.resolve_tool_name(tool_name)
        tool_obj: Any = mgr.tools[resolved]
    except KeyError:
        # Fallback to searching the backend tool catalog via find_tool
        resolved = tool_name
        tool_obj = None
        if _has_tool(mgr, "find_tool"):
            try:
                res = await mgr.call_tool(
                    "find_tool",
                    {"query": tool_name, "include_schema": True, "top_k": 5},
                )
                structured = getattr(res, "structuredContent", None) or getattr(
                    res, "structured_content", None
                )
                hits = structured if isinstance(structured, list) else []
                if not hits and getattr(res, "content", None):
                    for b in res.content:
                        t = getattr(b, "text", None)
                        if t and (t.startswith("[") or t.startswith("{")):
                            try:
                                parsed = json.loads(t)
                                if isinstance(parsed, list):
                                    hits = parsed
                            except Exception:
                                pass
                for h in hits:
                    if isinstance(h, dict) and (h.get("name") == tool_name or tool_name in h.get("name", "")):
                        tool_obj = h
                        resolved = h.get("name", tool_name)
                        break
                if tool_obj is None and hits and isinstance(hits[0], dict):
                    tool_obj = hits[0]
                    resolved = hits[0].get("name", tool_name)
            except Exception:
                tool_obj = None

        if tool_obj is None:
            print_error(f"unknown tool '{tool_name}'", json_mode=json_mode, code="unknown_tool")
            return 2

    if json_mode:
        from hpe_networking_mcp.cli_client.safety import tool_is_read_only

        if isinstance(tool_obj, dict):
            schema = tool_obj.get("schema") or {}
            desc = tool_obj.get("description", "")
            ro = bool(tool_obj.get("read_only", tool_obj.get("capability") == "read"))
        else:
            schema = getattr(tool_obj, "inputSchema", None) or getattr(tool_obj, "input_schema", None) or {}
            desc = getattr(tool_obj, "description", "")
            ro = tool_is_read_only(tool_obj)

        print_ok(
            {
                "tool": resolved,
                "description": desc,
                "read_only": ro,
                "schema": schema,
            },
            json_mode=True,
        )
        return 0
    text = format_tool_schema(resolved, tool_obj)
    from rich.markdown import Markdown

    console.print(Markdown(text))
    return 0


def cmd_status(
    mgr: SessionManager | None,
    cfg: ClientConfig,
    *,
    current_profile: str | None = None,
    json_mode: bool = False,
) -> int:
    """Display connection status and configuration."""
    prof_name = current_profile or (mgr.active_profile if mgr else None) or cfg.default_profile
    is_connected = mgr is not None and mgr.state == ConnectionState.CONNECTED
    status_dict = {
        "connected": is_connected,
        "state": mgr.state.value if mgr else "disconnected",
        "profile": prof_name,
        "tools_visible": len(mgr.tools) if mgr else 0,
        "last_error": mgr.last_error if mgr else None,
        "safety": "read-only default",
        "personal_docs_mode": "local metadata + content search",
    }
    if json_mode:
        print_ok(status_dict, json_mode=True)
        return 0

    lines = [
        "## Client Status",
        "",
        f"- **Connection State:** `{status_dict['state']}`",
        f"- **Active Profile:** `{status_dict['profile']}`",
        f"- **Visible Tools:** {status_dict['tools_visible']}",
        f"- **Safety Mode:** {status_dict['safety']}",
        f"- **Personal Documents:** {status_dict['personal_docs_mode']}",
    ]
    if status_dict["last_error"]:
        lines.append(f"- **Last Connection Error:** [red]{status_dict['last_error']}[/]")

    from rich.markdown import Markdown

    console.print(Markdown("\n".join(lines)))
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


async def cmd_ai_reason(
    mgr: SessionManager,
    safety: SafetyPolicy,
    *,
    prompt: str,
    provider: str = "heuristic",
    model: str | None = None,
    json_mode: bool = False,
) -> int:
    """Execute AI reasoning loop with available MCP tools."""
    ai = get_ai_backend(provider=provider, model=model)
    loop = AgentReasoningLoop(ai_backend=ai, session_manager=mgr)
    steps_log = []

    final_answer = ""
    async for step in loop.run(prompt):
        steps_log.append({
            "turn": step.turn_index,
            "type": step.step_type,
            "content": step.content,
            "tool": step.tool_name,
        })
        if step.step_type == "answer":
            final_answer = step.content
        elif not json_mode:
            if step.step_type == "thought":
                console.print(f"[dim italic]🧠 {step.content}[/]")
            elif step.step_type == "tool_call":
                console.print(f"[bold yellow]⚙️ {step.content}[/]")
            elif step.step_type == "tool_result":
                console.print(f"[dim]↳ Result: {step.content[:200]}...[/]")
            elif step.step_type == "error":
                console.print(f"[bold red]❌ {step.content}[/]")

    if json_mode:
        print_ok({"prompt": prompt, "answer": final_answer, "steps": steps_log}, json_mode=True)
        return 0

    if final_answer:
        from rich.markdown import Markdown

        console.print(Markdown(final_answer))
    return 0


def cmd_troubleshoot(
    query: str,
    *,
    site_id: str | None = None,
    json_mode: bool = False,
) -> int:
    """Generate structured troubleshooting plan and root cause analysis."""
    plan = create_troubleshooting_plan(query, site_id=site_id)
    if json_mode:
        print_ok(plan.to_dict(), json_mode=True)
        return 0
    report = format_troubleshooting_report(plan)
    from rich.markdown import Markdown

    console.print(Markdown(report))
    return 0


def cmd_migrate_plan(
    source_vendor: str,
    *,
    json_mode: bool = False,
) -> int:
    """Generate multi-vendor to AOS-CX migration blueprint."""
    plan_dict = plan_migration(source_vendor)
    if json_mode:
        print_ok(plan_dict, json_mode=True)
        return 0
    report = format_migration_plan_markdown(plan_dict)
    from rich.markdown import Markdown

    console.print(Markdown(report))
    return 0


def cmd_architect_plan(
    environment: str = "campus",
    *,
    ports: int = 200,
    aps: int = 50,
    evpn: bool = False,
    json_mode: bool = False,
) -> int:
    """Synthesize network architecture design and Bill of Materials."""
    rec = synthesize_architecture(
        environment=environment,
        scale_ap_count=aps,
        scale_switch_port_count=ports,
        require_evpn=evpn,
    )
    if json_mode:
        print_ok({
            "topology_type": rec.topology_type,
            "title": rec.title,
            "description": rec.description,
            "hardware": rec.recommended_hardware,
            "principles": rec.key_design_principles,
            "advantages": rec.advantages,
        }, json_mode=True)
        return 0
    report = format_architecture_recommendation_markdown(rec)
    from rich.markdown import Markdown

    console.print(Markdown(report))
    return 0
