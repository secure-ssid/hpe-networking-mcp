"""argparse entry point for the standalone hpe-networking-mcp client.

Usage examples:

  hpe-mcp tools list
  hpe-mcp tools find "wireless client"
  hpe-mcp rag ask "How do I configure WPA3?" --source mist_docs
  hpe-mcp api lookup "create WLAN"
  hpe-mcp invoke-read ask_docs --args '{"question":"..."}'
  hpe-mcp skills list
  hpe-mcp docs add ./notes.md --collection personal
  hpe-mcp shell
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import sys
from typing import Sequence

from hpe_networking_mcp import optional_deps
from hpe_networking_mcp.cli_client.banner import print_banner
from hpe_networking_mcp.cli_client.commands import (
    cmd_ai_reason,
    cmd_api_lookup,
    cmd_architect_plan,
    cmd_diagram,
    cmd_docs_add,
    cmd_docs_ingest,
    cmd_docs_list,
    cmd_docs_remove,
    cmd_docs_search,
    cmd_docs_search_content,
    cmd_docs_search_internal,
    cmd_find_tool_server,
    cmd_invoke,
    cmd_migrate_plan,
    cmd_rag_ask,
    cmd_skills_list,
    cmd_skills_show,
    cmd_status,
    cmd_tool_explore,
    cmd_tools_list,
    cmd_troubleshoot,
    create_reasoning_service,
    ensure_connected,
    parse_args_json,
)
from hpe_networking_mcp.cli_client.config import load_client_config
from hpe_networking_mcp.cli_client.output import console, print_error
from hpe_networking_mcp.cli_client.repl_input import configure_readline, read_line
from hpe_networking_mcp.cli_client.safety import SafetyPolicy
from hpe_networking_mcp.cli_client.sessions import SessionManager


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hpe-mcp",
        description=(
            "Standalone client for hpe-networking-mcp (and other MCP servers). "
            "Run with no arguments to open plain streaming AI chat."
        ),
    )
    p.add_argument(
        "--profile",
        "-p",
        default=None,
        help="Named server profile (default: local-router)",
    )
    p.add_argument("--config", default=None, help="Path to client config JSON")
    p.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    p.add_argument("--quiet", "-q", action="store_true", help="Suppress banner")
    p.add_argument(
        "--provider",
        choices=["heuristic", "openai", "anthropic", "ollama"],
        default=None,
        help="AI provider for interactive/AI commands (env/config may set the default)",
    )
    p.add_argument(
        "--model",
        default=None,
        help="AI model name (provider-specific; env/config may set the default)",
    )
    p.add_argument(
        "--repl",
        action="store_true",
        help="Use the gum/readline command shell instead of streaming chat",
    )
    p.add_argument(
        "--tui",
        action="store_true",
        help="Use the legacy full-screen Textual TUI",
    )
    p.add_argument(
        "--allow-writes",
        action="store_true",
        help="Permit write-capable tools (still requires --yes unless interactive)",
    )
    p.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Confirm write operations non-interactively",
    )

    # Not required: bare `hpe-mcp` opens the interactive shell.
    sub = p.add_subparsers(dest="command", required=False)

    # tools
    tools = sub.add_parser("tools", help="List or search tools")
    tools_sub = tools.add_subparsers(dest="tools_cmd", required=True)
    t_list = tools_sub.add_parser("list", help="List connected tools")
    t_list.add_argument("query", nargs="?", default=None)
    t_find = tools_sub.add_parser("find", help="Find tools (router find_tool or local filter)")
    t_find.add_argument("query")

    # invoke / invoke-read
    inv = sub.add_parser("invoke", help="Call a tool (respects write policy)")
    inv.add_argument("tool")
    inv.add_argument("--args", default="{}", help="JSON object of arguments")

    invr = sub.add_parser("invoke-read", help="Call a tool; refuse if not read-only")
    invr.add_argument("tool")
    invr.add_argument("--args", default="{}", help="JSON object of arguments")

    # rag
    rag = sub.add_parser("rag", help="RAG helpers")
    rag_sub = rag.add_subparsers(dest="rag_cmd", required=True)
    rag_ask = rag_sub.add_parser("ask", help="ask_docs")
    rag_ask.add_argument("question", nargs="+")
    rag_ask.add_argument("--source", default=None)

    # api
    api = sub.add_parser("api", help="API catalog helpers")
    api_sub = api.add_subparsers(dest="api_cmd", required=True)
    api_lookup = api_sub.add_parser("lookup", help="lookup_api")
    api_lookup.add_argument("query", nargs="+")

    # skills
    skills = sub.add_parser("skills", help="Local SKILL.md workflows")
    skills_sub = skills.add_subparsers(dest="skills_cmd", required=True)
    skills_sub.add_parser("list")
    sk_show = skills_sub.add_parser("show")
    sk_show.add_argument("name")

    # docs
    docs = sub.add_parser("docs", help="Personal document collections")
    docs_sub = docs.add_subparsers(dest="docs_cmd", required=True)
    d_list = docs_sub.add_parser("list")
    d_list.add_argument("--collection", default=None)
    d_add = docs_sub.add_parser("add")
    d_add.add_argument("source")
    d_add.add_argument("--collection", default="personal")
    d_add.add_argument("--title", default=None)
    d_remove = docs_sub.add_parser("remove", help="Remove a stored document by id")
    d_remove.add_argument("doc_id")
    d_remove.add_argument(
        "--keep-file",
        action="store_true",
        help="Drop the index entry but keep the stored file on disk",
    )
    d_search = docs_sub.add_parser("search")
    d_search.add_argument("query", nargs="+")
    d_search.add_argument("--collection", default=None)
    d_content = docs_sub.add_parser("search-content")
    d_content.add_argument("query", nargs="+")
    d_content.add_argument("--collection", default=None)
    d_ingest = docs_sub.add_parser(
        "ingest", help="Extract/chunk/embed a folder into the local personal index"
    )
    d_ingest.add_argument("folder")
    d_ingest.add_argument("--collection", default="internal")
    d_search_internal = docs_sub.add_parser(
        "search-internal", help="Hybrid search over the local personal index"
    )
    d_search_internal.add_argument("query", nargs="+")
    d_search_internal.add_argument("--collection", default="internal")

    # diagram
    diag = sub.add_parser(
        "diagram", help="Generate network design diagrams (Draw.io/Graphviz/NeXt)"
    )
    diag.add_argument("prompt", nargs="*", default=[], help="Diagram description or title")
    diag.add_argument("--format", "-f", choices=["drawio", "graphviz", "nextui"], default=None)
    diag.add_argument("--vendor", "-v", default=None)
    diag.add_argument("--title", "-t", default=None)

    # ai / reason
    ai_p = sub.add_parser("ai", help="AI multi-turn reasoning and tool dispatch")
    ai_p.add_argument("prompt", nargs="+", help="Goal or prompt for AI expert")
    ai_p.add_argument(
        "--provider",
        choices=["heuristic", "openai", "anthropic", "ollama"],
        default=argparse.SUPPRESS,
    )
    ai_p.add_argument("--model", default=argparse.SUPPRESS)

    # troubleshoot
    tb_p = sub.add_parser("troubleshoot", help="Automated root cause diagnostic reasoning")
    tb_p.add_argument("query", nargs="+", help="Symptom, client MAC, or error description")
    tb_p.add_argument("--site-id", default=None)

    # migrate
    mg_p = sub.add_parser("migrate", help="Multi-vendor migration blueprint & syntax translation")
    mg_p.add_argument(
        "source_vendor", nargs="?", default="aos-s", help="Source vendor (cisco, aos-s, procurve)"
    )

    # architect / design
    ar_p = sub.add_parser(
        "architect", help="Campus / DC Fabric architecture design & BOM synthesis"
    )
    ar_p.add_argument(
        "environment",
        nargs="?",
        default="campus",
        choices=["campus", "datacenter", "branch", "fabric"],
    )
    ar_p.add_argument("--ports", type=int, default=200)
    ar_p.add_argument("--aps", type=int, default=50)
    ar_p.add_argument("--evpn", action="store_true")

    # tool / explore
    tool_p = sub.add_parser("tool", help="Inspect tool schema and parameters")
    tool_p.add_argument("tool_name", help="Name of tool to inspect")

    # status
    sub.add_parser("status", help="Show connection status and profile details")

    # profiles
    prof = sub.add_parser("profiles", help="Show configured server profiles")
    prof.add_argument("profiles_cmd", nargs="?", default="list", choices=["list"])

    # TUI / shell.  Repeat mode flags here so both
    # ``hpe-mcp --tui shell`` and ``hpe-mcp shell --tui`` work.
    shell_p = sub.add_parser("shell", help="Interactive plain streaming chat")
    shell_p.add_argument("--repl", action="store_true", default=argparse.SUPPRESS)
    shell_p.add_argument("--tui", action="store_true", default=argparse.SUPPRESS)
    shell_p.add_argument(
        "--provider",
        choices=["heuristic", "openai", "anthropic", "ollama"],
        default=argparse.SUPPRESS,
    )
    shell_p.add_argument("--model", default=argparse.SUPPRESS)
    sub.add_parser("version", help="Print version and exit")

    return p


def _safety_from_args(args: argparse.Namespace) -> SafetyPolicy:
    return SafetyPolicy(
        read_only_default=not bool(getattr(args, "allow_writes", False)),
        allow_writes=bool(getattr(args, "allow_writes", False)),
        confirmed=bool(getattr(args, "yes", False)),
    )


async def _run_connected(args: argparse.Namespace) -> int:
    try:
        cfg = load_client_config(
            config_path=args.config,
            profile_override=args.profile,
            provider_override=getattr(args, "provider", None),
            model_override=getattr(args, "model", None),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print_error(f"configuration error: {exc}", json_mode=bool(args.json), code="config_error")
        return 2
    safety = _safety_from_args(args)
    json_mode = bool(args.json)

    if args.command == "version":
        from hpe_networking_mcp.cli_client.banner import package_version

        print(package_version())
        return 0

    if args.command == "profiles":
        rows = []
        for name, prof in sorted(cfg.profiles.items()):
            rows.append(
                {
                    "name": name,
                    "transport": prof.transport,
                    "command": prof.command,
                    "url": prof.url,
                    "description": prof.description,
                    "default": name == cfg.default_profile,
                }
            )
        if json_mode:
            from hpe_networking_mcp.cli_client.output import print_ok

            print_ok(
                {
                    "profiles": rows,
                    "config_path": (str(cfg.config_path) if cfg.config_path else None),
                },
                json_mode=True,
            )
        else:
            from rich.table import Table

            table = Table(title="Server profiles")
            table.add_column("Name", style="cyan")
            table.add_column("Transport")
            table.add_column("Target")
            table.add_column("Default")
            table.add_column("Description")
            for r in rows:
                target = r["url"] or " ".join(filter(None, [r["command"]]))
                table.add_row(
                    r["name"],
                    r["transport"],
                    str(target)[:60],
                    "yes" if r["default"] else "",
                    r["description"][:50],
                )
            console.print(table)
        return 0

    # Offline commands (no MCP session)
    if args.command == "skills":
        if args.skills_cmd == "list":
            return cmd_skills_list(json_mode=json_mode)
        return cmd_skills_show(args.name, json_mode=json_mode)

    if args.command == "status":
        return cmd_status(None, cfg, current_profile=args.profile, json_mode=json_mode)

    if args.command == "troubleshoot":
        return cmd_troubleshoot(" ".join(args.query), site_id=args.site_id, json_mode=json_mode)

    if args.command == "migrate":
        return cmd_migrate_plan(args.source_vendor, json_mode=json_mode)

    if args.command == "architect":
        return cmd_architect_plan(
            environment=args.environment,
            ports=args.ports,
            aps=args.aps,
            evpn=args.evpn,
            json_mode=json_mode,
        )

    if args.command == "docs":
        if args.docs_cmd == "list":
            return cmd_docs_list(collection=args.collection, json_mode=json_mode)
        if args.docs_cmd == "add":
            return cmd_docs_add(
                args.source,
                collection=args.collection,
                title=args.title,
                json_mode=json_mode,
            )
        if args.docs_cmd == "remove":
            return cmd_docs_remove(
                args.doc_id,
                keep_file=args.keep_file,
                json_mode=json_mode,
            )
        if args.docs_cmd == "search":
            return cmd_docs_search(
                " ".join(args.query),
                collection=args.collection,
                json_mode=json_mode,
            )
        if args.docs_cmd == "search-content":
            return cmd_docs_search_content(
                " ".join(args.query),
                collection=args.collection,
                json_mode=json_mode,
            )
        if args.docs_cmd == "ingest":
            return cmd_docs_ingest(
                args.folder,
                collection=args.collection,
                json_mode=json_mode,
            )
        if args.docs_cmd == "search-internal":
            return cmd_docs_search_internal(
                " ".join(args.query),
                collection=args.collection,
                json_mode=json_mode,
            )

    if args.command == "shell":
        # TUI is started from main() on the main thread. This path is the
        # plain readline fallback (--repl) only.
        return await run_repl(cfg, safety, quiet=args.quiet, json_default=json_mode)

    # Connected commands
    print_banner(
        console,
        profile=args.profile or cfg.default_profile,
        mode=args.command,
        quiet=args.quiet or json_mode,
    )

    async with SessionManager.create(namespace=True) as mgr:
        try:
            prof = await ensure_connected(mgr, cfg, profile=args.profile)
        except Exception as exc:  # noqa: BLE001
            print_error(f"connect failed: {exc}", json_mode=json_mode, code="connect_failed")
            return 1

        if not json_mode and not args.quiet:
            console.print(
                "[dim]connected[/] "
                f"profile={prof.name} transport={prof.transport} "
                f"tools={len(mgr.tools)}"
            )

        if args.command == "diagram":
            return await cmd_diagram(
                mgr,
                safety,
                prompt=" ".join(args.prompt),
                format=args.format,
                vendor=args.vendor,
                title=args.title,
                json_mode=json_mode,
            )

        if args.command == "ai":
            return await cmd_ai_reason(
                mgr,
                safety,
                prompt=" ".join(args.prompt),
                provider=cfg.ai_provider,
                model=cfg.ai_model,
                json_mode=json_mode,
            )

        if args.command == "tool":
            return await cmd_tool_explore(
                mgr,
                safety,
                tool_name=args.tool_name,
                json_mode=json_mode,
            )

        if args.command == "tools":
            if args.tools_cmd == "list":
                return await cmd_tools_list(mgr, query=args.query, json_mode=json_mode)
            return await cmd_find_tool_server(mgr, safety, query=args.query, json_mode=json_mode)

        if args.command == "invoke":
            try:
                tool_args = parse_args_json(args.args)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                print_error(f"bad --args: {exc}", json_mode=json_mode)
                return 2
            return await cmd_invoke(
                mgr,
                safety,
                tool=args.tool,
                args=tool_args,
                allow_writes=args.allow_writes,
                force_write=args.allow_writes and args.yes,
                json_mode=json_mode,
            )

        if args.command == "invoke-read":
            # Temporarily force read-only policy
            ro_safety = SafetyPolicy(read_only_default=True, allow_writes=False, confirmed=False)
            try:
                tool_args = parse_args_json(args.args)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                print_error(f"bad --args: {exc}", json_mode=json_mode)
                return 2
            return await cmd_invoke(
                mgr,
                ro_safety,
                tool=args.tool,
                args=tool_args,
                json_mode=json_mode,
            )

        if args.command == "rag" and args.rag_cmd == "ask":
            return await cmd_rag_ask(
                mgr,
                safety,
                question=" ".join(args.question),
                source=args.source,
                json_mode=json_mode,
            )

        if args.command == "api" and args.api_cmd == "lookup":
            return await cmd_api_lookup(
                mgr,
                safety,
                query=" ".join(args.query),
                json_mode=json_mode,
            )

        print_error(f"unhandled command {args.command}", json_mode=json_mode)
        return 2


async def _stream_terminal_request(
    service,
    prompt: str,
    *,
    json_mode: bool = False,
    cancel_event: asyncio.Event | None = None,
) -> int:
    """Render observable reasoning events without exposing hidden thinking."""
    events = []
    wrote_text = False
    async for event in service.stream(prompt, cancel_event=cancel_event):
        events.append(event)
        if json_mode:
            continue
        if event.kind == "text_delta":
            console.print(event.content, end="", markup=False, soft_wrap=True)
            wrote_text = True
        elif event.kind == "tool_call":
            if wrote_text:
                console.print()
                wrote_text = False
            console.print(
                f"[dim]→ {event.tool_name or 'tool'} "
                f"({'read-only' if event.is_read_only else 'write/unknown'})[/]"
            )
        elif event.kind == "tool_result":
            console.print(f"[dim]↳ {event.content[:240]}[/]")
        elif event.kind == "error":
            if wrote_text:
                console.print()
                wrote_text = False
            console.print(f"[red]{event.content}[/]")
        elif event.kind == "cancelled":
            if wrote_text:
                console.print()
                wrote_text = False
            console.print("[yellow]request cancelled[/]")
    if not json_mode:
        if wrote_text:
            console.print()
        return 0

    from hpe_networking_mcp.cli_client.output import print_ok

    print_ok(
        {
            "prompt": prompt,
            "metadata": service.metadata,
            "events": [
                {
                    "type": event.kind,
                    "turn": event.turn_index,
                    "content": event.content,
                    "tool": event.tool_name,
                    "allowed": event.allowed,
                    "is_read_only": event.is_read_only,
                    "usage": event.usage,
                    "metadata": event.metadata,
                }
                for event in events
            ],
            "usage": service.total_usage,
        },
        json_mode=True,
    )
    return 0


async def run_chat(
    cfg,
    safety: SafetyPolicy,
    *,
    quiet: bool = False,
    json_default: bool = False,
    provider: str | None = None,
    model: str | None = None,
) -> int:
    """Run the default plain terminal chat loop with model-backed streaming.

    A non-TTY invocation has no interactive prompt and exits without
    connecting or making an AI request.  This keeps startup/blank stdin safe
    for shell probes and process supervisors.
    """

    if not sys.stdin.isatty():
        return 0
    configure_readline()
    if not quiet and not json_default:
        print_banner(console, profile=cfg.default_profile, mode="chat")

    async with SessionManager.create(namespace=True) as mgr:
        service = None
        connected = False
        while True:
            try:
                line = await asyncio.to_thread(read_line, "hpe-mcp> ")
            except (EOFError, KeyboardInterrupt):
                console.print()
                return 0
            line = (line or "").strip()
            if not line:
                continue
            if line.lower() in {"exit", "quit", ":q", "/exit", "/quit"}:
                return 0
            if line.lower() in {"help", "/help", "?"}:
                console.print(
                    "Type a networking question to chat. "
                    "Use `hpe-mcp --repl` for the legacy command shell, "
                    "`hpe-mcp --tui` for Textual, or `hpe-mcp ai <prompt>` "
                    "for one-shot mode."
                )
                continue

            if not connected:
                try:
                    prof = await ensure_connected(mgr, cfg)
                    connected = True
                    if not quiet and not json_default:
                        console.print(
                            f"[green]connected[/] {prof.name} "
                            f"({prof.transport}) · {len(mgr.tools)} tools"
                        )
                except Exception as exc:  # noqa: BLE001 - connection boundary
                    console.print(f"[red]connect failed:[/] {exc}")
                    continue
                service = create_reasoning_service(
                    mgr,
                    safety,
                    provider=provider or cfg.ai_provider,
                    model=model or cfg.ai_model,
                )

            if line.startswith("/"):
                console.print(
                    "[yellow]This plain chat path accepts questions only; "
                    "use --repl or --tui for command controls.[/]"
                )
                continue
            assert service is not None
            cancel_event = asyncio.Event()
            try:
                await _stream_terminal_request(
                    service,
                    line,
                    json_mode=json_default,
                    cancel_event=cancel_event,
                )
            except KeyboardInterrupt:
                cancel_event.set()
                service.cancel()
                console.print("\n[yellow]request cancelled[/]")


async def run_repl(
    cfg,
    safety: SafetyPolicy,
    *,
    quiet: bool = False,
    json_default: bool = False,
    provider: str | None = None,
    model: str | None = None,
) -> int:
    """Minimal interactive shell over a live session."""
    if not sys.stdin.isatty():
        return 0
    from hpe_networking_mcp.cli_client.banner import package_version
    from hpe_networking_mcp.cli_client.gum_shell import GumShell

    gum = GumShell()

    # readline history still useful even when gum handles the prompt.
    configure_readline()

    if not quiet:
        gum.header(package_version(), profile=cfg.default_profile)
        gum.print_shortcuts()

    async with SessionManager.create(namespace=True) as mgr:
        service = None
        try:
            with gum.spin("connecting…"):
                prof = await ensure_connected(mgr, cfg)
            gum.connected(prof.name, prof.transport, len(mgr.tools))
            service = create_reasoning_service(
                mgr,
                safety,
                provider=provider or cfg.ai_provider,
                model=model or cfg.ai_model,
            )
        except Exception as exc:  # noqa: BLE001
            gum.error(f"not connected: {exc}")
            gum.info("use: connect [profile]")

        while True:
            try:
                line = await gum.prompt()
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            line = line.strip()
            if not line:
                continue
            if line in {"exit", "quit", ":q"}:
                break
            if line in {"help", "?"}:
                console.print(
                    "[bold]shortcuts[/]\n"
                    "  ask <question> [--source mist_docs]   RAG / docs Q&A\n"
                    "  api <query>                           OpenAPI lookup\n"
                    "  find <query>                          find tools\n"
                    "  tools                                 list tools\n"
                    "[bold]also[/]\n"
                    "  rag ask | tools list|find | api lookup | invoke-read\n"
                    "  skills list|show | docs list|add|remove|search\n"
                    "  connect [profile] | profiles | exit"
                )
                continue

            try:
                argv = shlex.split(line)
            except ValueError as exc:
                print_error(str(exc))
                continue

            head = argv[0].lstrip("/")
            if not head:
                continue
            json_mode = json_default or "--json" in argv
            argv = [a for a in argv if a != "--json"]

            try:
                if head in {"exit", "quit", ":q", "q"} and len(argv) == 1:
                    break
                if head in {"help", "?"} and len(argv) == 1:
                    console.print(
                        "[bold]shortcuts[/]\n"
                        "  ask <question> [--source mist_docs]   RAG / docs Q&A\n"
                        "  diagram <description>                 Network design diagram\n"
                        "  tool <name>                           Inspect tool schema\n"
                        "  api <query>                           OpenAPI lookup\n"
                        "  find <query>                          find tools\n"
                        "  tools [list|find]                     list tools\n"
                        "[bold]also[/]\n"
                        "  rag ask | tools list|find | api lookup | invoke-read\n"
                        "  skills [list|show] | docs [list|add|remove|search]\n"
                        "  status | connect [profile] | profiles | exit\n"
                        "[dim]Note: Any plain question uses the configured AI backend.[/]\n"
                    )
                    continue
                if head == "connect":
                    name = argv[1] if len(argv) > 1 else None
                    with gum.spin("connecting…"):
                        prof = await ensure_connected(mgr, cfg, profile=name)
                    gum.connected(prof.name, prof.transport, len(mgr.tools))
                    continue
                if head == "profiles":
                    for n, p in sorted(cfg.profiles.items()):
                        mark = "*" if n == cfg.default_profile else " "
                        console.print(f"{mark} {n:16} {p.transport:16} {p.description}")
                    continue
                if head == "status":
                    cmd_status(mgr, cfg, current_profile=None, json_mode=json_mode)
                    continue
                if head == "diagram":
                    await cmd_diagram(
                        mgr,
                        safety,
                        prompt=" ".join(argv[1:]),
                        json_mode=json_mode,
                    )
                    continue
                if head in {"ai", "reason"}:
                    if service is None:
                        service = create_reasoning_service(
                            mgr,
                            safety,
                            provider=provider or cfg.ai_provider,
                            model=model or cfg.ai_model,
                        )
                    await _stream_terminal_request(
                        service,
                        " ".join(argv[1:]),
                        json_mode=json_mode,
                    )
                    continue
                if head in {"troubleshoot", "tb"}:
                    cmd_troubleshoot(" ".join(argv[1:]), json_mode=json_mode)
                    continue
                if head in {"migrate", "migration"}:
                    vendor = argv[1] if len(argv) > 1 else "aos-s"
                    cmd_migrate_plan(vendor, json_mode=json_mode)
                    continue
                if head in {"architect", "design"}:
                    env = argv[1] if len(argv) > 1 else "campus"
                    cmd_architect_plan(env, json_mode=json_mode)
                    continue
                if head in {"tool", "explore"} and len(argv) >= 2:
                    await cmd_tool_explore(
                        mgr,
                        safety,
                        tool_name=argv[1],
                        json_mode=json_mode,
                    )
                    continue
                if head == "tools" and len(argv) >= 2 and argv[1] == "list":
                    await cmd_tools_list(
                        mgr,
                        query=argv[2] if len(argv) > 2 else None,
                        json_mode=json_mode,
                    )
                    continue
                if head == "tools" and len(argv) >= 3 and argv[1] == "find":
                    await cmd_find_tool_server(
                        mgr,
                        safety,
                        query=" ".join(argv[2:]),
                        json_mode=json_mode,
                    )
                    continue
                if head == "rag" and len(argv) >= 3 and argv[1] == "ask":
                    source = None
                    rest = argv[2:]
                    if "--source" in rest:
                        i = rest.index("--source")
                        source = rest[i + 1]
                        rest = rest[:i] + rest[i + 2 :]
                    await cmd_rag_ask(
                        mgr,
                        safety,
                        question=" ".join(rest),
                        source=source,
                        json_mode=json_mode,
                    )
                    continue
                if head == "api" and len(argv) >= 3 and argv[1] == "lookup":
                    await cmd_api_lookup(
                        mgr,
                        safety,
                        query=" ".join(argv[2:]),
                        json_mode=json_mode,
                    )
                    continue
                # Short aliases for common actions
                if head in {"ask", "search_docs"} and len(argv) >= 2:
                    source = None
                    rest = argv[1:]
                    if "--source" in rest:
                        i = rest.index("--source")
                        source = rest[i + 1]
                        rest = rest[:i] + rest[i + 2 :]
                    await cmd_rag_ask(
                        mgr,
                        safety,
                        question=" ".join(rest),
                        source=source,
                        json_mode=json_mode,
                    )
                    continue
                if head == "api" and len(argv) >= 2 and argv[1] != "lookup":
                    await cmd_api_lookup(
                        mgr,
                        safety,
                        query=" ".join(argv[1:]),
                        json_mode=json_mode,
                    )
                    continue
                if head == "find" and len(argv) >= 2:
                    await cmd_find_tool_server(
                        mgr,
                        safety,
                        query=" ".join(argv[1:]),
                        json_mode=json_mode,
                    )
                    continue
                if head == "tools" and (len(argv) == 1 or (len(argv) == 2 and argv[1] in {"list"})):
                    await cmd_tools_list(mgr, json_mode=json_mode)
                    continue
                if head == "invoke-read" and len(argv) >= 2:
                    raw_args = argv[2] if len(argv) > 2 else "{}"
                    await cmd_invoke(
                        mgr,
                        SafetyPolicy(read_only_default=True),
                        tool=argv[1],
                        args=parse_args_json(raw_args),
                        json_mode=json_mode,
                    )
                    continue
                if head == "skills":
                    if len(argv) == 1 or (len(argv) >= 2 and argv[1] == "list"):
                        cmd_skills_list(json_mode=json_mode)
                        continue
                    if len(argv) >= 3 and argv[1] == "show":
                        cmd_skills_show(argv[2], json_mode=json_mode)
                        continue
                    if len(argv) == 2:
                        cmd_skills_show(argv[1], json_mode=json_mode)
                        continue
                if head == "docs":
                    if len(argv) == 1 or (len(argv) >= 2 and argv[1] == "list"):
                        coll = argv[2] if len(argv) > 2 else None
                        cmd_docs_list(collection=coll, json_mode=json_mode)
                        continue
                    if len(argv) >= 3 and argv[1] == "add":
                        cmd_docs_add(argv[2], json_mode=json_mode)
                        continue
                    if len(argv) >= 3 and argv[1] == "remove":
                        cmd_docs_remove(argv[2], json_mode=json_mode)
                        continue
                    if len(argv) >= 3 and argv[1] == "search":
                        cmd_docs_search(" ".join(argv[2:]), json_mode=json_mode)
                        continue
                    if len(argv) >= 2:
                        cmd_docs_search(" ".join(argv[1:]), json_mode=json_mode)
                        continue
                # Plain non-empty input is model-backed chat. Explicit
                # `rag ask`/`ask` remains available above for docs lookup.
                if service is None:
                    service = create_reasoning_service(
                        mgr,
                        safety,
                        provider=provider or cfg.ai_provider,
                        model=model or cfg.ai_model,
                    )
                await _stream_terminal_request(service, line, json_mode=json_mode)
            except Exception as exc:  # noqa: BLE001
                if json_mode:
                    print_error(f"{type(exc).__name__}: {exc}", json_mode=True)
                else:
                    gum.error(f"{type(exc).__name__}: {exc}")

    return 0


_COMMANDS = frozenset(
    {
        "tools",
        "tool",
        "diagram",
        "ai",
        "troubleshoot",
        "migrate",
        "architect",
        "status",
        "invoke",
        "invoke-read",
        "rag",
        "api",
        "skills",
        "docs",
        "profiles",
        "shell",
        "version",
    }
)


def _normalize_argv(argv: Sequence[str] | None) -> list[str]:
    """Default bare / flag-only invocations to the interactive shell."""
    import sys

    raw = list(argv) if argv is not None else sys.argv[1:]
    if any(a in {"-h", "--help"} for a in raw):
        return raw
    # Already has a subcommand somewhere after optional global flags.
    if any(a in _COMMANDS for a in raw):
        return raw
    # No args, or only global flags → shell.
    return [*raw, "shell"]


def main(argv: Sequence[str] | None = None) -> int:
    raw = _normalize_argv(argv)
    parser = build_parser()
    args = parser.parse_args(raw)
    if not getattr(args, "command", None):
        args.command = "shell"

    # The default interactive path is the plain streaming chat loop.  The
    # legacy readline shell and Textual UI remain explicit opt-in modes.
    if args.command == "shell":
        try:
            cfg = load_client_config(
                config_path=args.config,
                profile_override=args.profile,
                provider_override=getattr(args, "provider", None),
                model_override=getattr(args, "model", None),
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print_error(f"configuration error: {exc}", json_mode=args.json, code="config_error")
            return 2
        safety = _safety_from_args(args)
        if not sys.stdin.isatty():
            return 0
        try:
            if getattr(args, "tui", False):
                # `textual` lives in the `tui` extra; every other CLI mode
                # (default, --repl) runs without it, so a missing package is
                # a message about --tui, not a broken install.
                try:
                    from hpe_networking_mcp.cli_client.tui import run_tui
                except ImportError as exc:
                    raise optional_deps.missing(
                        "`hpe-mcp --tui`", module="textual", extra="tui"
                    ) from exc

                return run_tui(
                    cfg,
                    safety,
                    profile=args.profile,
                    provider=cfg.ai_provider,
                    model=cfg.ai_model,
                )
            if getattr(args, "repl", False):
                return asyncio.run(
                    run_repl(
                        cfg,
                        safety,
                        quiet=args.quiet,
                        json_default=args.json,
                        provider=cfg.ai_provider,
                        model=cfg.ai_model,
                    )
                )
            return asyncio.run(
                run_chat(
                    cfg,
                    safety,
                    quiet=args.quiet,
                    json_default=args.json,
                    provider=cfg.ai_provider,
                    model=cfg.ai_model,
                )
            )
        except optional_deps.MissingOptionalDependency as exc:
            print_error(
                str(exc), json_mode=bool(args.json), code="optional_dependency_missing"
            )
            return 2
        except ImportError as exc:
            # Not the TUI extra: chat/--repl reached here too, and blaming
            # Textual for an unrelated import failure sends the reader to the
            # wrong fix.
            print_error(f"import failed: {exc}", json_mode=bool(args.json), code="import_failed")
            return 2
        except KeyboardInterrupt:
            return 130

    try:
        return asyncio.run(_run_connected(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
